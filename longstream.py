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

LOCAL_FILE_H  = "stream_input_h.mp4"
STATE_FILE_H  = "stream_state_h.txt"
URL_FILE_H    = "source_url_h.txt"

logger = logging.getLogger(__name__)

_stream_lock_h = asyncio.Lock()

# ─────────────────────────────────────────────────────────────
# STATE MANAGEMENT
# ─────────────────────────────────────────────────────────────

def load_state_h() -> float:
    if os.path.exists(STATE_FILE_H):
        try:
            with open(STATE_FILE_H, "r") as f:
                return float(f.read().strip())
        except Exception as e:
            logger.error(f"State read error: {e}")
    return 0.0


def save_state_h(seek_time: float):
    try:
        with open(STATE_FILE_H, "w") as f:
            f.write(str(seek_time))
        logger.info(f"💾 Saved landscape stream state at {seek_time}s")

        subprocess.run(["git", "config", "--global", "user.name",  "LongBotWorker"])
        subprocess.run(["git", "config", "--global", "user.email", "bot@worker.com"])
        subprocess.run(["git", "add", STATE_FILE_H])

        if os.path.exists(URL_FILE_H):
            subprocess.run(["git", "add", URL_FILE_H])

        subprocess.run(["git", "commit", "-m", f"landscape checkpoint {seek_time}s [skip ci]"])
        subprocess.run(["git", "push"])

    except Exception as e:
        logger.error(f"Failed saving landscape state: {e}")


def clear_state_h():
    logger.info("🧼 Clearing landscape stream states...")
    try:
        subprocess.run(["git", "config", "--global", "user.name",  "LongBotWorker"])
        subprocess.run(["git", "config", "--global", "user.email", "bot@worker.com"])

        if os.path.exists(STATE_FILE_H):
            os.remove(STATE_FILE_H)
            subprocess.run(["git", "rm", STATE_FILE_H])

        if os.path.exists(URL_FILE_H):
            os.remove(URL_FILE_H)
            subprocess.run(["git", "rm", URL_FILE_H])

        subprocess.run(["git", "commit", "-m", "cleanup landscape state [skip ci]"])
        subprocess.run(["git", "push"])

    except Exception as e:
        logger.error(f"Cleanup error: {e}")

# ─────────────────────────────────────────────────────────────
# GOOGLE DRIVE
# ─────────────────────────────────────────────────────────────

def extract_gdrive_id_h(url: str) -> str | None:
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
# STREAM ENGINE  —  16:9 Landscape / YouTube Live
#
# Target output : 1920 × 1080  (16:9 standard)
# Bitrate       : 8 Mbps video
# Audio         : 192 k AAC stereo
# ─────────────────────────────────────────────────────────────

def stream_to_youtube_h(
    file_path: str,
    rtmp_destination: str,
    start_offset: float = 0.0,
) -> bool:

    pre_seek  = ["-ss", str(start_offset)] if start_offset > 300 else []
    post_seek = ["-ss", str(start_offset)] if 0 < start_offset <= 300 else []

    ffmpeg_cmd = [
        "ffmpeg",
        *pre_seek,
        "-re",
        "-i", file_path,
        *post_seek,
        "-threads", "0",

        # ── VIDEO FILTERS ──────────────────────────────────
        # 1. Centre-crop to 16:9
        # 2. Scale to 1920×1080 with Lanczos
        # 3. Light sharpen + subtle contrast lift
        "-vf",
        (
            "crop=iw:iw*9/16:(ih-iw*9/16)/2:0,"   # crop height to 16:9
            "scale=1920:1080:flags=lanczos,"
            "unsharp=5:5:0.5:3:3:0.0,"
            "eq=contrast=1.03:saturation=1.06"
        ),

        # ── VIDEO ENCODE ───────────────────────────────────
        "-c:v",        "libx264",
        "-preset",     "ultrafast",
        "-tune",       "zerolatency",
        "-b:v",        "8000k",
        "-maxrate",    "8500k",
        "-bufsize",    "16000k",
        "-r",          "30",
        "-pix_fmt",    "yuv420p",
        "-g",          "60",
        "-keyint_min", "60",
        "-sc_threshold", "0",
        "-profile:v",  "high",
        "-level",      "4.2",

        # ── AUDIO ──────────────────────────────────────────
        "-c:a",  "aac",
        "-b:a",  "192k",
        "-ar",   "44100",
        "-ac",   "2",

        # ── STABILITY ──────────────────────────────────────
        "-max_muxing_queue_size", "4096",
        "-fflags",    "+genpts",
        "-flvflags",  "no_duration_filesize",
        "-f",  "flv",
        rtmp_destination,
    ]

    logger.info(f"🚀 Landscape stream starting at offset {start_offset}s")

    process = subprocess.Popen(
        ffmpeg_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        universal_newlines=True,
    )

    start_run_time        = time.time()
    max_allowable_runtime = 20700  # ~5h 45m

    while True:
        line = process.stdout.readline()
        if line:
            logger.info(line.strip())
        if not line and process.poll() is not None:
            break

        elapsed = time.time() - start_run_time
        if elapsed >= max_allowable_runtime:
            logger.warning("⚠️ Runtime limit — saving landscape checkpoint…")
            process.terminate()
            save_state_h(start_offset + elapsed)
            return False

    if process.returncode == 0:
        clear_state_h()
        return True

    return False

# ─────────────────────────────────────────────────────────────
# VIDEO VALIDATION  (reused helpers)
# ─────────────────────────────────────────────────────────────

def is_valid_video_h(file_path: str) -> bool:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error",
             "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1", file_path],
            capture_output=True, text=True, timeout=15,
        )
        return "duration=" in r.stdout
    except Exception:
        return False


def get_video_info_h(file_path: str) -> dict:
    info = {"duration": "unknown", "size": "unknown", "resolution": "unknown"}
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error",
             "-show_entries", "format=duration:stream=width,height",
             "-of", "default=noprint_wrappers=1", file_path],
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

async def _download_url_h(url: str, dest: str, message: Message) -> bool:
    import urllib.request

    is_gdrive = "drive.google.com" in url or "docs.google.com" in url

    if is_gdrive:
        await message.reply_text("☁️ Downloading Google Drive video (landscape)…")
        file_id = extract_gdrive_id_h(url)
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

async def check_and_resume_stream_h(bot):
    seek_time = load_state_h()
    if seek_time > 0.0 and os.path.exists(URL_FILE_H):
        try:
            with open(URL_FILE_H, "r") as f:
                saved_url = f.read().strip()
            logger.info(f"🔄 Landscape resume checkpoint at {seek_time}s")

            gdrive_id = extract_gdrive_id_h(saved_url)
            dl_res = subprocess.run(
                ["gdown", f"https://drive.google.com/uc?id={gdrive_id}", "-O", LOCAL_FILE_H],
                capture_output=True,
            )
            if dl_res.returncode == 0 and os.path.exists(LOCAL_FILE_H):
                stream_to_youtube_h(LOCAL_FILE_H, YOUTUBE_STREAM_URL, seek_time)
        except Exception as e:
            logger.error(f"Landscape resume error: {e}")

# ─────────────────────────────────────────────────────────────
# MAIN STREAM WRAPPER
# ─────────────────────────────────────────────────────────────

async def _do_stream_h(message: Message, file_path: str, start_offset: float = 0.0):
    loop = asyncio.get_event_loop()

    if not is_valid_video_h(file_path):
        if os.path.exists(file_path):
            os.remove(file_path)
        await message.reply_text("❌ File is not a valid video.")
        return

    vinfo = get_video_info_h(file_path)

    await message.reply_text(
        f"🎬 *Landscape Stream Started!*\n\n"
        f"📐 Source: `{vinfo['resolution']}`\n"
        f"📐 Output: `1920×1080 (16:9)`\n"
        f"🔥 Quality: `Full HD Landscape`\n"
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
            stream_to_youtube_h,
            file_path,
            YOUTUBE_STREAM_URL,
            start_offset,
        )
    except Exception as e:
        await message.reply_text(f"❌ Stream error:\n`{e}`", parse_mode="Markdown")
    finally:
        if os.path.exists(STATE_FILE_H):
            logger.info("Landscape checkpoint preserved — next runner will resume.")
        else:
            if os.path.exists(file_path):
                os.remove(file_path)

# ─────────────────────────────────────────────────────────────
# /h COMMAND HANDLER  —  entry point from bot_runner
# ─────────────────────────────────────────────────────────────

async def cmd_h(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Usage: /h <google_drive_link>
    Streams the video in 16:9 landscape (1920×1080) to YouTube.
    """
    if not context.args:
        await update.message.reply_text(
            "Usage: `/h <google_drive_link>`\n"
            "Example: `/h https://drive.google.com/file/d/xxxx/view`",
            parse_mode="Markdown",
        )
        return

    url = context.args[0].strip()

    if not url.startswith(("http://", "https://")):
        await update.message.reply_text("❌ Please provide a valid URL.")
        return

    if not YOUTUBE_STREAM_URL:
        await update.message.reply_text("❌ Missing YOUTUBE_STREAM_URL secret.")
        return

    if _stream_lock_h.locked():
        await update.message.reply_text("⏳ A landscape stream is already running.")
        return

    async with _stream_lock_h:
        await update.message.reply_text("📥 Preparing landscape livestream (16:9)…")

        if os.path.exists(LOCAL_FILE_H):
            os.remove(LOCAL_FILE_H)

        with open(URL_FILE_H, "w") as f:
            f.write(url)

        ok = await _download_url_h(url, LOCAL_FILE_H, update.message)

        if not ok or not os.path.exists(LOCAL_FILE_H) or os.path.getsize(LOCAL_FILE_H) == 0:
            return

        await _do_stream_h(update.message, LOCAL_FILE_H, 0.0)