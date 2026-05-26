import os
import subprocess
import logging
import asyncio
from dotenv import load_dotenv
from telegram import Update, Message
from telegram.ext import ContextTypes

load_dotenv()

TELEGRAM_TOKEN     = os.getenv("TELEGRAM_BOT_TOKEN")
YOUTUBE_STREAM_URL = os.getenv("YOUTUBE_STREAM_URL")

LOCAL_FILE = "stream_input.mp4"

logger = logging.getLogger(__name__)
_stream_lock = asyncio.Lock()


# ─────────────────────────────────────────────────────────────────
# STREAM  —  Any resolution → 1080×1920 9:16 upscaled
# ─────────────────────────────────────────────────────────────────

def stream_to_youtube(file_path: str, rtmp_destination: str) -> bool:
    ffmpeg_cmd = [
        "ffmpeg", "-re", "-i", file_path,
        "-vf", (
            "crop=ih*9/16:ih:(iw-ih*9/16)/2:0,"
            "scale=1080:1920:flags=lanczos,"
            "unsharp=5:5:0.8:3:3:0.4"
        ),
        "-c:v", "libx264", "-preset", "slow",
        "-b:v", "8000k", "-maxrate", "9000k", "-bufsize", "18000k",
        "-pix_fmt", "yuv420p", "-g", "60", "-keyint_min", "60",
        "-sc_threshold", "0", "-r", "30",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-f", "flv", rtmp_destination,
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
            ["ffprobe", "-v", "error",
             "-show_entries", "format=duration:stream=width,height",
             "-of", "default=noprint_wrappers=1", file_path],
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
            info["resolution"] = f"{info['w']}x{info['h']}"
        size_mb = os.path.getsize(file_path) / (1024 * 1024)
        info["size"] = f"{size_mb:.1f} MB"
    except Exception:
        pass
    return info


def is_valid_video(file_path: str) -> bool:
    """Check ffprobe can actually read the file as video/audio."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration", "-of", "default=noprint_wrappers=1", file_path],
            capture_output=True, text=True, timeout=15,
        )
        return "duration=" in r.stdout
    except Exception:
        return False


async def _do_stream(message: Message, file_path: str):
    """Core streaming logic shared by all upload paths."""
    loop = asyncio.get_event_loop()

    # Validate the file is actually a real media file
    if not is_valid_video(file_path):
        await message.reply_text(
            "❌ File doesn't appear to be a valid video/audio file.\n"
            "Please send an `.mp4`, `.mkv`, `.mov`, `.mp3`, `.m4a` etc."
        )
        if os.path.exists(file_path):
            os.remove(file_path)
        return

    vinfo = get_video_info(file_path)
    await message.reply_text(
        f"🎬 *File ready! Going LIVE...*\n\n"
        f"📐 Source: `{vinfo['resolution']}`\n"
        f"📐 Output: `1080x1920` (9:16, upscaled)\n"
        f"⏱ Duration: `{vinfo['duration']}`\n"
        f"💾 Size: `{vinfo['size']}`",
        parse_mode="Markdown",
    )

    try:
        stream_ok = await loop.run_in_executor(
            None, stream_to_youtube, file_path, YOUTUBE_STREAM_URL
        )
    except Exception as e:
        await message.reply_text(f"❌ Stream error:\n`{e}`", parse_mode="Markdown")
        stream_ok = False
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)

    if stream_ok:
        await message.reply_text("✅ *Stream ended naturally.*", parse_mode="Markdown")
    else:
        await message.reply_text("⚠️ *Stream ended with an error.*", parse_mode="Markdown")


# ─────────────────────────────────────────────────────────────────
# FILE HANDLER  —  user sends a video/audio file directly
# ─────────────────────────────────────────────────────────────────

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not YOUTUBE_STREAM_URL:
        await update.message.reply_text("❌ YOUTUBE_STREAM_URL secret is missing.")
        return

    if _stream_lock.locked():
        await update.message.reply_text("⚠️ A stream is already LIVE right now.")
        return

    msg = update.message
    tg_file = None
    is_audio_only = False

    if msg.video:
        tg_file = msg.video
    elif msg.document:
        mime = (msg.document.mime_type or "").lower()
        name = (msg.document.file_name or "").lower()
        tg_file = msg.document
        if mime.startswith("audio/") or name.endswith((".mp3", ".m4a", ".aac", ".ogg", ".flac", ".wav")):
            is_audio_only = True
    elif msg.audio:
        tg_file = msg.audio
        is_audio_only = True
    elif msg.voice:
        tg_file = msg.voice
        is_audio_only = True

    if not tg_file:
        await msg.reply_text(
            "❓ I didn't receive a media file.\n\n"
            "Please send your video/audio file directly in this chat.\n"
            "Don't use any command — just attach and send the file."
        )
        return

    file_size_mb = getattr(tg_file, "file_size", 0) / (1024 * 1024)
    file_name = getattr(tg_file, "file_name", "uploaded file")

    # Telegram bot API hard limit is 20 MB download via getFile
    # For larger files users must upload as a document or use /url
    if file_size_mb > 2000:
        await msg.reply_text(
            f"❌ File too large ({file_size_mb:.0f} MB).\n"
            "Telegram bots can handle up to ~2 GB.\n"
            "For bigger files use: `/url <direct_download_link>`",
            parse_mode="Markdown",
        )
        return

    async with _stream_lock:
        loop = asyncio.get_event_loop()

        await msg.reply_text(
            f"📥 *Downloading your file...*\n\n"
            f"📎 `{file_name}`\n"
            f"💾 `{file_size_mb:.1f} MB`",
            parse_mode="Markdown",
        )

        if os.path.exists(LOCAL_FILE):
            os.remove(LOCAL_FILE)

        try:
            tg_file_obj = await context.bot.get_file(tg_file.file_id)
            await tg_file_obj.download_to_drive(LOCAL_FILE)
        except Exception as e:
            await msg.reply_text(f"❌ Download from Telegram failed:\n`{e}`", parse_mode="Markdown")
            return

        # Wrap audio-only files with a black video track
        if is_audio_only:
            await msg.reply_text("🎵 Audio detected — adding black background for stream...")
            wrapped = LOCAL_FILE + "_wrapped.mp4"
            try:
                r = subprocess.run([
                    "ffmpeg", "-y",
                    "-f", "lavfi", "-i", "color=c=black:s=1920x1080:r=30",
                    "-i", LOCAL_FILE,
                    "-shortest",
                    "-c:v", "libx264", "-preset", "ultrafast", "-tune", "stillimage",
                    "-c:a", "aac", "-b:a", "192k",
                    "-pix_fmt", "yuv420p",
                    wrapped,
                ], capture_output=True, timeout=600)
                if r.returncode == 0 and os.path.exists(wrapped):
                    os.remove(LOCAL_FILE)
                    os.rename(wrapped, LOCAL_FILE)
                else:
                    err = r.stderr.decode(errors="ignore")[-300:]
                    await msg.reply_text(f"⚠️ Audio wrap failed:\n`{err}`\nTrying direct stream...", parse_mode="Markdown")
            except Exception as e:
                await msg.reply_text(f"⚠️ Audio wrap error: `{e}` — trying direct stream...", parse_mode="Markdown")

        await _do_stream(msg, LOCAL_FILE)


# ─────────────────────────────────────────────────────────────────
# /url command  —  direct download link for large files
# ─────────────────────────────────────────────────────────────────

async def cmd_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Download from a direct URL (Dropbox, own server, Google Drive direct link)."""
    if not YOUTUBE_STREAM_URL:
        await update.message.reply_text("❌ YOUTUBE_STREAM_URL secret is missing.")
        return

    if _stream_lock.locked():
        await update.message.reply_text("⚠️ A stream is already LIVE right now.")
        return

    if not context.args:
        await update.message.reply_text(
            "Usage: `/url <direct_download_link>`\n\n"
            "Examples:\n"
            "• Dropbox: change `?dl=0` → `?dl=1` at end of URL\n"
            "• Google Drive: `https://drive.google.com/uc?export=download&id=FILE_ID`\n"
            "• Your server: `https://yourserver.com/video.mp4`\n\n"
            "⚠️ YouTube links won't work here — upload the file directly instead.",
            parse_mode="Markdown",
        )
        return

    raw_url = context.args[0]

    # Block YouTube URLs explicitly
    blocked = ("youtube.com", "youtu.be", "youtube-nocookie.com")
    if any(b in raw_url for b in blocked):
        await update.message.reply_text(
            "❌ YouTube URLs are not supported — YouTube blocks downloads from server IPs.\n\n"
            "Instead:\n"
            "1. Download the video on your phone/PC\n"
            "2. Send the file directly to this bot\n"
            "3. Or upload to Dropbox/Drive and send that link with `/url`"
        )
        return

    if not raw_url.startswith(("http://", "https://")):
        await update.message.reply_text("❌ Please provide a valid http/https URL.")
        return

    async with _stream_lock:
        loop = asyncio.get_event_loop()

        await update.message.reply_text(
            f"📥 *Downloading from URL...*\n\n`{raw_url}`",
            parse_mode="Markdown",
        )

        if os.path.exists(LOCAL_FILE):
            os.remove(LOCAL_FILE)

        try:
            import urllib.request

            def _dl(url, dest):
                req = urllib.request.Request(
                    url, headers={"User-Agent": "Mozilla/5.0"}
                )
                with urllib.request.urlopen(req, timeout=300) as resp, \
                     open(dest, "wb") as out:
                    while True:
                        chunk = resp.read(1024 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)

            await loop.run_in_executor(None, _dl, raw_url, LOCAL_FILE)

        except Exception as e:
            await update.message.reply_text(
                f"❌ Download failed:\n`{e}`\n\n"
                "Make sure the URL is a *direct* download link, not a preview page.",
                parse_mode="Markdown",
            )
            return

        if not os.path.exists(LOCAL_FILE) or os.path.getsize(LOCAL_FILE) == 0:
            await update.message.reply_text("❌ Downloaded file is empty.")
            return

        await _do_stream(update.message, LOCAL_FILE)


# ─────────────────────────────────────────────────────────────────
# /stream  —  now just explains the correct usage
# ─────────────────────────────────────────────────────────────────

async def cmd_stream(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *How to stream:*\n\n"
        "🎬 *Upload a file* — just send a video/audio file directly in this chat. No command needed.\n\n"
        "🔗 *Large file via URL* — use:\n"
        "`/url <direct_download_link>`\n"
        "_(Dropbox, Google Drive direct, your own server)_\n\n"
        "❌ YouTube links won't work — YouTube blocks server IPs.\n"
        "Download the video first, then send it here.",
        parse_mode="Markdown",
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status = "🔴 *Stream is currently LIVE.*" if _stream_lock.locked() else "⚪ *No stream running.*"
    await update.message.reply_text(status, parse_mode="Markdown")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎥 *Metropolitan Shorts Live Bot*\n\n"
        "*To stream:*\n"
        "Just send a video or audio file here — no command needed!\n\n"
        "*For large files (>2 GB):*\n"
        "`/url <direct_link>` — Dropbox, Drive, your server\n\n"
        "*Commands:*\n"
        "/status — Is a stream running?\n"
        "/help — This message\n\n"
        "*Output:* Any input → `1080x1920` 9:16 @ 8 Mbps\n"
        "YouTube shows it as Full HD regardless of source quality.",
        parse_mode="Markdown",
    )