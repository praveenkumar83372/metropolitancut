import os
import re
import subprocess
import logging
import asyncio
import time

from dotenv import load_dotenv
from telegram import Update, Message
from telegram.ext import ContextTypes

load_dotenv()

# ─────────────────────────────────────────────────────────────
# ENV
# ─────────────────────────────────────────────────────────────

TELEGRAM_TOKEN     = os.getenv("TELEGRAM_BOT_TOKEN")
YOUTUBE_STREAM_URL = os.getenv("YOUTUBE_STREAM_URL")

LOCAL_FILE = "stream_input.mp4"
STATE_FILE = "stream_state.txt"
URL_FILE   = "source_url.txt"

logger = logging.getLogger(__name__)

_stream_lock = asyncio.Lock()

# ─────────────────────────────────────────────────────────────
# STATE MANAGEMENT
# ─────────────────────────────────────────────────────────────

def load_state() -> float:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return float(f.read().strip())
        except Exception as e:
            logger.error(f"State read error: {e}")
    return 0.0


def save_state(seek_time: float):
    try:
        with open(STATE_FILE, "w") as f:
            f.write(str(seek_time))
        logger.info(f"💾 Saved stream state at {seek_time}s")

        subprocess.run(["git", "config", "--global", "user.name",  "ShortsBotWorker"])
        subprocess.run(["git", "config", "--global", "user.email", "bot@worker.com"])
        subprocess.run(["git", "add", STATE_FILE])

        if os.path.exists(URL_FILE):
            subprocess.run(["git", "add", URL_FILE])

        subprocess.run(["git", "commit", "-m", f"checkpoint {seek_time}s [skip ci]"])
        subprocess.run(["git", "push"])

    except Exception as e:
        logger.error(f"Failed saving state: {e}")


def clear_state():
    logger.info("🧼 Clearing stream states...")
    try:
        subprocess.run(["git", "config", "--global", "user.name",  "ShortsBotWorker"])
        subprocess.run(["git", "config", "--global", "user.email", "bot@worker.com"])

        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
            subprocess.run(["git", "rm", STATE_FILE])

        if os.path.exists(URL_FILE):
            os.remove(URL_FILE)
            subprocess.run(["git", "rm", URL_FILE])

        subprocess.run(["git", "commit", "-m", "cleanup stream state [skip ci]"])
        subprocess.run(["git", "push"])

    except Exception as e:
        logger.error(f"Cleanup error: {e}")

# ─────────────────────────────────────────────────────────────
# GOOGLE DRIVE
# ─────────────────────────────────────────────────────────────

def extract_gdrive_id(url: str) -> str | None:
    patterns = [
        r"/file/d/([a-zA-Z0-9_-]{25,})",
        r"id=([a-zA-Z0-9_-]{25,})",
        r"/d/([a-zA-Z0-9_-]{25,})",
    ]
    for pat in patterns:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None

# ─────────────────────────────────────────────────────────────
# STREAM ENGINE  —  2K / Ultra-HD Shorts quality
#
# Target output : 1080 × 1920  (9:16 Shorts)
# Effective res : 2× sharpness via lanczos + unsharp mask
# Bitrate       : 8 Mbps video  (YouTube 1080p60 recommended)
# Audio         : 192 k AAC stereo
# Latency       : ultrafast preset + zerolatency tune
# Timestamps    : +genpts prevents PTS gaps on resume seeks
# ─────────────────────────────────────────────────────────────

def stream_to_youtube(
    file_path: str,
    rtmp_destination: str,
    start_offset: float = 0.0,
) -> bool:

    # ── Seek strategy ───────────────────────────────────────
    # >300 s  →  fast pre-input keyframe seek (no full decode)
    # ≤300 s  →  output-side seek (frame accurate)
    pre_seek  = ["-ss", str(start_offset)] if start_offset > 300 else []
    post_seek = ["-ss", str(start_offset)] if 0 < start_offset <= 300 else []

    ffmpeg_cmd = [
        "ffmpeg",

        # ── Pre-input fast seek (large offsets only) ─────────
        *pre_seek,

        # ── Real-time pacing ────────────────────────────────
        "-re",

        # ── Input ───────────────────────────────────────────
        "-i", file_path,

        # ── Output-side accurate seek (short offsets only) ──
        *post_seek,

        # ── Use all available CPU threads ───────────────────
        "-threads", "0",

        # ────────────────────────────────────────────────────
        # VIDEO FILTERS
        #  1. Perfect 9:16 centre-crop (handles any AR source)
        #  2. Upscale/downscale to 1080×1920 with Lanczos
        #     (much sharper than bicubic for Shorts)
        #  3. Light unsharp to crisp edges post-scale
        #  4. Slight contrast + saturation lift
        # ────────────────────────────────────────────────────
        "-vf",
        (
            "crop=ih*9/16:ih:(iw-ih*9/16)/2:0,"
            "scale=1080:1920:flags=lanczos,"
            "unsharp=5:5:0.6:3:3:0.0,"
            "eq=contrast=1.04:saturation=1.08"
        ),

        # ────────────────────────────────────────────────────
        # VIDEO ENCODE
        # ultrafast keeps real-time on weak shared runners.
        # 8 Mbps sits in YouTube's "recommended" tier for
        # 1080p Shorts → triggers 2K/HQ serve path.
        # ────────────────────────────────────────────────────
        "-c:v",        "libx264",
        "-preset",     "ultrafast",
        "-tune",       "zerolatency",
        "-b:v",        "8000k",
        "-maxrate",    "8500k",
        "-bufsize",    "16000k",

        # FPS
        "-r",          "30",

        # Pixel format
        "-pix_fmt",    "yuv420p",

        # YouTube-required keyframe interval (every 2 s at 30 fps)
        "-g",          "60",
        "-keyint_min", "60",
        "-sc_threshold", "0",

        # Profile / level
        "-profile:v",  "high",
        "-level",      "4.2",        # 4.2 supports 1080p30 HQ fully

        # ────────────────────────────────────────────────────
        # AUDIO  —  192 k for clean stereo, no quality loss
        # ────────────────────────────────────────────────────
        "-c:a",  "aac",
        "-b:a",  "192k",
        "-ar",   "44100",
        "-ac",   "2",

        # ────────────────────────────────────────────────────
        # STABILITY / MUXER
        # ────────────────────────────────────────────────────
        "-max_muxing_queue_size", "4096",

        # Re-generate timestamps at seek boundaries → no PTS gaps
        "-fflags",    "+genpts",

        # Low-latency FLV flags
        "-flvflags",  "no_duration_filesize",

        # Output
        "-f",  "flv",
        rtmp_destination,
    ]

    logger.info(f"🚀 2K Shorts stream starting at offset {start_offset}s")

    process = subprocess.Popen(
        ffmpeg_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        universal_newlines=True,
    )

    start_run_time       = time.time()
    max_allowable_runtime = 20700          # ~5 h 45 m — safe GitHub limit

    while True:
        line = process.stdout.readline()
        if line:
            logger.info(line.strip())
        if not line and process.poll() is not None:
            break

        elapsed = time.time() - start_run_time
        if elapsed >= max_allowable_runtime:
            logger.warning("⚠️ GitHub runtime limit approaching — saving checkpoint…")
            process.terminate()
            save_state(start_offset + elapsed)
            return False

    if process.returncode == 0:
        clear_state()
        return True

    return False

# ─────────────────────────────────────────────────────────────
# VIDEO VALIDATION
# ─────────────────────────────────────────────────────────────

def is_valid_video(file_path: str) -> bool:
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1",
                file_path,
            ],
            capture_output=True, text=True, timeout=15,
        )
        return "duration=" in r.stdout
    except Exception:
        return False


def get_video_info(file_path: str) -> dict:
    info = {"duration": "unknown", "size": "unknown", "resolution": "unknown"}
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration:stream=width,height",
                "-of", "default=noprint_wrappers=1",
                file_path,
            ],
            capture_output=True, text=True, timeout=15,
        )
        for line in r.stdout.splitlines():
            if line.startswith("duration="):
                secs = float(line.split("=")[1])
                h, rem = divmod(int(secs), 3600)
                m, s   = divmod(rem, 60)
                info["duration"] = f"{h}h {m}m {s}s" if h else f"{m}m {s}s"
            if line.startswith("width="):
                info["w"] = line.split("=")[1]
            if line.startswith("height="):
                info["h"] = line.split("=")[1]

        if "w" in info and "h" in info:
            info["resolution"] = f"{info['w']}x{info['h']}"

        info["size"] = f"{os.path.getsize(file_path) / (1024*1024):.1f} MB"
    except Exception:
        pass
    return info

# ─────────────────────────────────────────────────────────────
# DOWNLOAD ENGINE
# ─────────────────────────────────────────────────────────────

async def _download_url(url: str, dest: str, message: Message) -> bool:
    import urllib.request

    is_gdrive = "drive.google.com" in url or "docs.google.com" in url

    if is_gdrive:
        await message.reply_text("☁️ Downloading Google Drive video…")
        file_id = extract_gdrive_id(url)
        if not file_id:
            await message.reply_text("❌ Invalid Google Drive link.")
            return False
        gdrive_url = f"https://drive.google.com/uc?id={file_id}"
    else:
        gdrive_url = None

    loop = asyncio.get_event_loop()

    def _dl():
        if is_gdrive:
            result = subprocess.run(
                ["gdown", gdrive_url, "-O", dest],
                capture_output=True, text=True,
            )
            return result.returncode == 0
        else:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=300) as resp, open(dest, "wb") as out:
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
            return True

    try:
        ok = await loop.run_in_executor(None, _dl)
        if not ok:
            await message.reply_text("❌ Download failed.")
            return False
        return True
    except Exception as e:
        await message.reply_text(f"❌ Download failed:\n`{e}`", parse_mode="Markdown")
        return False

# ─────────────────────────────────────────────────────────────
# AUTO RESUME
# ─────────────────────────────────────────────────────────────

async def check_and_resume_stream(bot):
    seek_time = load_state()
    if seek_time > 0.0 and os.path.exists(URL_FILE):
        try:
            with open(URL_FILE, "r") as f:
                saved_url = f.read().strip()
            logger.info(f"🔄 Resume checkpoint found at {seek_time}s")

            gdrive_id = extract_gdrive_id(saved_url)
            dl_res = subprocess.run(
                ["gdown", f"https://drive.google.com/uc?id={gdrive_id}", "-O", LOCAL_FILE],
                capture_output=True,
            )
            if dl_res.returncode == 0 and os.path.exists(LOCAL_FILE):
                stream_to_youtube(LOCAL_FILE, YOUTUBE_STREAM_URL, seek_time)
        except Exception as e:
            logger.error(f"Resume error: {e}")

# ─────────────────────────────────────────────────────────────
# MAIN STREAM WRAPPER
# ─────────────────────────────────────────────────────────────

async def _do_stream(message: Message, file_path: str, start_offset: float = 0.0):
    loop = asyncio.get_event_loop()

    if not is_valid_video(file_path):
        if os.path.exists(file_path):
            os.remove(file_path)
        await message.reply_text("❌ File is not a valid video.")
        return

    vinfo = get_video_info(file_path)

    await message.reply_text(
        f"🎬 *2K Shorts Stream Started!*\n\n"
        f"📐 Source: `{vinfo['resolution']}`\n"
        f"📐 Output: `1080×1920 (9:16)`\n"
        f"🔥 Quality: `2K / Ultra-HD Shorts`\n"
        f"📡 Bitrate: `8 Mbps`\n"
        f"🎥 Codec: `H.264 High L4.2`\n"
        f"🔊 Audio: `192 k AAC`\n"
        f"⏱ Duration: `{vinfo['duration']}`\n"
        f"💾 Size: `{vinfo['size']}`\n"
        f"📍 Resume from: `{start_offset}s`",
        parse_mode="Markdown",
    )

    try:
        await loop.run_in_executor(
            None,
            stream_to_youtube,
            file_path,
            YOUTUBE_STREAM_URL,
            start_offset,
        )
    except Exception as e:
        await message.reply_text(f"❌ Stream error:\n`{e}`", parse_mode="Markdown")
    finally:
        if os.path.exists(STATE_FILE):
            logger.info("Checkpoint preserved — next runner will resume.")
        else:
            if os.path.exists(file_path):
                os.remove(file_path)

# ─────────────────────────────────────────────────────────────
# FILE HANDLER
# ─────────────────────────────────────────────────────────────

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚠️ Large Telegram uploads are unstable.\n"
        "Please send a Google Drive link instead."
    )

# ─────────────────────────────────────────────────────────────
# TEXT URL HANDLER
# ─────────────────────────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    if (
        not text.startswith(("http://", "https://"))
        or any(b in text for b in ("youtube.com", "youtu.be"))
    ):
        return

    if not YOUTUBE_STREAM_URL:
        await update.message.reply_text("❌ Missing YOUTUBE_STREAM_URL secret.")
        return

    if _stream_lock.locked():
        await update.message.reply_text("⏳ A stream is already running.")
        return

    async with _stream_lock:
        await update.message.reply_text("📥 Preparing 2K Shorts livestream…")

        if os.path.exists(LOCAL_FILE):
            os.remove(LOCAL_FILE)

        with open(URL_FILE, "w") as f:
            f.write(text)

        ok = await _download_url(text, LOCAL_FILE, update.message)

        if not ok or not os.path.exists(LOCAL_FILE) or os.path.getsize(LOCAL_FILE) == 0:
            return

        await _do_stream(update.message, LOCAL_FILE, 0.0)

# ─────────────────────────────────────────────────────────────
# COMMANDS  (stubs — implement as needed)
# ─────────────────────────────────────────────────────────────

async def cmd_stream(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📎 Send a Google Drive direct link to start streaming."
    )

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    seek = load_state()
    if seek > 0.0:
        await update.message.reply_text(f"🔄 Stream checkpoint found at `{seek}s`.", parse_mode="Markdown")
    elif _stream_lock.locked():
        await update.message.reply_text("📡 Stream is currently live.")
    else:
        await update.message.reply_text("💤 No active stream.")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "*Metropolitan Shorts Live Bot*\n\n"
        "• Paste a Google Drive link → stream starts automatically\n"
        "/stream — show this prompt\n"
        "/status — check stream state\n"
        "/help   — this message",
        parse_mode="Markdown",
    )

async def cmd_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Alias: treat args as a URL
    if context.args:
        update.message.text = context.args[0]
        await handle_text(update, context)
    else:
        await update.message.reply_text("Usage: /url <google_drive_link>")

async def cmd_playlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎵 Playlist feature coming soon.")