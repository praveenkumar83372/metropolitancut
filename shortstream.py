import os
import subprocess
import logging
import asyncio
from dotenv import load_dotenv
import yt_dlp
from telegram import Update
from telegram.ext import ContextTypes

load_dotenv()

TELEGRAM_TOKEN     = os.getenv("TELEGRAM_BOT_TOKEN")
YOUTUBE_STREAM_URL = os.getenv("YOUTUBE_STREAM_URL")

LOCAL_FILE = "walking_tour.mp4"

logger = logging.getLogger(__name__)

_stream_lock = asyncio.Lock()


# ─────────────────────────────────────────────────────────────────
# DOWNLOAD  —  Ordered by datacenter-IP friendliness
# ─────────────────────────────────────────────────────────────────
#
# Why these clients work on GitHub Actions (Azure datacenter IPs):
#
#   android_vr  → REQUIRE_JS_PLAYER=False, NO GVS PO token policy
#                 (Oculus Quest user-agent, YouTube doesn't bot-check VR)
#
#   tv          → NO GVS PO token policy at all (TVHTML5 Cobalt UA)
#                 YouTube treats smart-TV clients differently
#
#   web_embedded → thirdParty embedUrl set to reddit.com, no REQUIRE_AUTH,
#                  no GVS PO token policy — bypasses sign-in check
#
#   ios / android → fallback; they DO require PO token for GVS but
#                   yt-dlp will still attempt delivery without one

def download_video(url: str, out_path: str) -> tuple[bool, str]:
    format_string = (
        "bestvideo[height<=2160][ext=mp4]+bestaudio[ext=m4a]/"
        "bestvideo[height<=2160]+bestaudio/"
        "bestvideo[height<=1080]+bestaudio/"
        "best"
    )

    strategies = [
        # ── Best bet on datacenter: no JS player + no PO token needed ──
        {
            "name": "android_vr",
            "extractor_args": {"youtube": {"player_client": ["android_vr"]}},
        },
        # ── TV client: no PO token policy, Cobalt UA ─────────────────
        {
            "name": "tv",
            "extractor_args": {"youtube": {"player_client": ["tv"]}},
        },
        # ── Embedded web: reddit embedUrl, no sign-in check ──────────
        {
            "name": "web_embedded",
            "extractor_args": {
                "youtube": {
                    "player_client": ["web_embedded"],
                    "player_skip": ["configs"],
                }
            },
        },
        # ── iOS mobile: different bot rules ──────────────────────────
        {
            "name": "ios",
            "extractor_args": {"youtube": {"player_client": ["ios"]}},
        },
        # ── Android: last resort ─────────────────────────────────────
        {
            "name": "android",
            "extractor_args": {"youtube": {"player_client": ["android"]}},
        },
    ]

    for strategy in strategies:
        logger.info(f"Trying client: [{strategy['name']}]")
        if os.path.exists(out_path):
            os.remove(out_path)

        ydl_opts = {
            "format": format_string,
            "outtmpl": out_path,
            "merge_output_format": "mp4",
            "quiet": False,
            "no_warnings": False,
            "noprogress": True,
            "extractor_args": strategy["extractor_args"],
            "retries": 3,
            "fragment_retries": 3,
            # Suppress "Sign in to confirm" error — let next strategy try
            "ignoreerrors": False,
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if info and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                    w = info.get("width", 0)
                    h = info.get("height", 0)
                    resolution = f"{w}x{h}" if w and h else "unknown"
                    logger.info(
                        f"✅ Download succeeded via [{strategy['name']}] @ {resolution}"
                    )
                    return True, resolution
        except yt_dlp.utils.DownloadError as e:
            logger.warning(f"[{strategy['name']}] failed: {e}")
            continue
        except Exception as e:
            logger.error(f"[{strategy['name']}] unexpected error: {e}")
            continue

    if os.path.exists(out_path):
        os.remove(out_path)
    return False, "error"


# ─────────────────────────────────────────────────────────────────
# STREAM  —  16:9 → 9:16 center crop
# ─────────────────────────────────────────────────────────────────

def stream_to_youtube(file_path: str, rtmp_destination: str) -> bool:
    ffmpeg_cmd = [
        "ffmpeg",
        "-re",
        "-i", file_path,
        "-vf", "crop=ih*9/16:ih:(iw-ih*9/16)/2:0,scale=1080:1920",
        "-c:v", "libx264",
        "-preset", "slow",
        "-b:v", "8000k",
        "-maxrate", "9000k",
        "-bufsize", "18000k",
        "-pix_fmt", "yuv420p",
        "-g", "60",
        "-keyint_min", "60",
        "-sc_threshold", "0",
        "-r", "30",
        "-c:a", "aac",
        "-b:a", "192k",
        "-ar", "48000",
        "-ac", "2",
        "-f", "flv",
        rtmp_destination,
    ]
    logger.info("FFmpeg streaming started...")
    result = subprocess.run(ffmpeg_cmd)
    return result.returncode == 0


# ─────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────

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
                m, s = divmod(rem, 60)
                info["duration"] = f"{h}h {m}m {s}s" if h else f"{m}m {s}s"
            if line.startswith("width="):
                info["w"] = line.split("=")[1]
            if line.startswith("height="):
                info["h"] = line.split("=")[1]
        if "w" in info and "h" in info:
            info["resolution"] = f"{info['w']}×{info['h']}"
        size_mb = os.path.getsize(file_path) / (1024 * 1024)
        info["size"] = f"{size_mb:.0f} MB"
    except Exception:
        pass
    return info


# ─────────────────────────────────────────────────────────────────
# COMMANDS
# ─────────────────────────────────────────────────────────────────

async def cmd_stream(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "❌ No URL provided.\nUsage: /stream <youtube_url>"
        )
        return

    if not YOUTUBE_STREAM_URL:
        await update.message.reply_text("❌ YOUTUBE_STREAM_URL secret is missing.")
        return

    if _stream_lock.locked():
        await update.message.reply_text("⚠️ A stream is already LIVE right now.")
        return

    video_url = context.args[0]

    async with _stream_lock:
        loop = asyncio.get_event_loop()
        await update.message.reply_text(
            f"📥 *Downloading video...*\n\n`{video_url}`",
            parse_mode="Markdown",
        )

        try:
            ok, source_res = await loop.run_in_executor(
                None, download_video, video_url, LOCAL_FILE
            )
        except Exception as e:
            await update.message.reply_text(
                f"❌ Download failed:\n`{e}`", parse_mode="Markdown"
            )
            return

        if not ok:
            await update.message.reply_text(
                "❌ All download strategies failed.\n"
                "The video may be private, age-restricted, or region-blocked.",
                parse_mode="Markdown",
            )
            return

        vinfo = get_video_info(LOCAL_FILE)
        await update.message.reply_text(
            f"🎬 *Download complete! Going LIVE...*\n\n"
            f"📐 Source: `{source_res}`\n"
            f"📐 Output: `1080×1920` (9:16 vertical)\n"
            f"⏱ Duration: `{vinfo['duration']}`\n"
            f"💾 Size: `{vinfo['size']}`",
            parse_mode="Markdown",
        )

        try:
            stream_ok = await loop.run_in_executor(
                None, stream_to_youtube, LOCAL_FILE, YOUTUBE_STREAM_URL
            )
        except Exception as e:
            await update.message.reply_text(
                f"❌ Stream error:\n`{e}`", parse_mode="Markdown"
            )
            stream_ok = False
        finally:
            if os.path.exists(LOCAL_FILE):
                os.remove(LOCAL_FILE)

        if stream_ok:
            await update.message.reply_text(
                "✅ *Stream ended naturally.*", parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(
                "⚠️ *Stream ended with an error.*", parse_mode="Markdown"
            )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _stream_lock.locked():
        await update.message.reply_text("🔴 *Stream is currently LIVE.*", parse_mode="Markdown")
    else:
        await update.message.reply_text("⚪ *No stream running.*", parse_mode="Markdown")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎥 *Metropolitan Shorts Live Bot*\n\n"
        "/stream `<url>` — Download & Go Live\n"
        "/status — Check if stream is running\n"
        "/help — Show this message",
        parse_mode="Markdown",
    )