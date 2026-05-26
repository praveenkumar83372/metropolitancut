import os
import subprocess
import logging
import asyncio
import urllib.request
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
# STREAM  —  Any resolution input → 1080p 9:16 output
#            (upscales/downscales whatever you feed it)
# ─────────────────────────────────────────────────────────────────
#
# Why not "4K output":
#   YouTube requires 3840×2160 @ 20–51 Mbps for true 4K.
#   GitHub Actions has ~1 Gbps upload but the CPU can't encode
#   4K x264 in real-time. 1080p @ 8 Mbps is the sweet spot —
#   YouTube will serve it as "1080p HD" regardless of source res.
#
# The crop+scale filter:
#   1. crop=ih*9/16:ih:(iw-ih*9/16)/2:0  → center-crop to 9:16
#   2. scale=1080:1920:flags=lanczos      → upscale/downscale cleanly

def stream_to_youtube(file_path: str, rtmp_destination: str) -> bool:
    ffmpeg_cmd = [
        "ffmpeg", "-re", "-i", file_path,
        # ── Video ──────────────────────────────────────────────
        "-vf", (
            "crop=ih*9/16:ih:(iw-ih*9/16)/2:0,"
            "scale=1080:1920:flags=lanczos,"          # lanczos = best upscale quality
            "unsharp=5:5:0.8:3:3:0.4"                # sharpen after upscale
        ),
        "-c:v", "libx264",
        "-preset", "slow",          # better quality at same bitrate
        "-b:v", "8000k",
        "-maxrate", "9000k",
        "-bufsize", "18000k",
        "-pix_fmt", "yuv420p",
        "-g", "60",
        "-keyint_min", "60",
        "-sc_threshold", "0",
        "-r", "30",
        # ── Audio ──────────────────────────────────────────────
        "-c:a", "aac",
        "-b:a", "192k",
        "-ar", "48000",
        "-ac", "2",
        # ── Output ─────────────────────────────────────────────
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
            info["resolution"] = f"{info['w']}×{info['h']}"
        size_mb = os.path.getsize(file_path) / (1024 * 1024)
        info["size"] = f"{size_mb:.1f} MB"
    except Exception:
        pass
    return info


async def _do_stream(message: Message, file_path: str):
    """Core streaming logic — shared by all /stream variants."""
    loop = asyncio.get_event_loop()

    vinfo = get_video_info(file_path)
    await message.reply_text(
        f"🎬 *File ready\\! Going LIVE\\.\\.\\.*\n\n"
        f"📐 Source: `{vinfo['resolution']}`\n"
        f"📐 Output: `1080×1920` \\(9:16, upscaled\\)\n"
        f"⏱ Duration: `{vinfo['duration']}`\n"
        f"💾 Size: `{vinfo['size']}`",
        parse_mode="MarkdownV2",
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
# COMMAND: /stream  —  now accepts a direct download URL
#          (Google Drive, Dropbox, your own server, etc.)
#          For large files (walking tours, 11hr videos) share a
#          direct link — no 50 MB Telegram limit
# ─────────────────────────────────────────────────────────────────

async def cmd_stream(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not YOUTUBE_STREAM_URL:
        await update.message.reply_text("❌ YOUTUBE_STREAM_URL secret is missing.")
        return

    if _stream_lock.locked():
        await update.message.reply_text("⚠️ A stream is already LIVE right now.")
        return

    if not context.args:
        await update.message.reply_text(
            "❌ No URL provided.\n\n"
            "Usage:\n"
            "  `/stream <direct_download_url>`\n\n"
            "Or just *send/forward a video file* to this bot — "
            "no command needed\\!",
            parse_mode="MarkdownV2",
        )
        return

    raw_url = context.args[0]

    # ── Validate it looks like a URL ───────────────────────────
    if not raw_url.startswith(("http://", "https://")):
        await update.message.reply_text(
            "❌ That doesn't look like a URL.\n"
            "Send a direct download link, or just upload a video file here."
        )
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
            def _download_url(url, dest):
                headers = {"User-Agent": "Mozilla/5.0"}
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=300) as resp, \
                     open(dest, "wb") as out:
                    while True:
                        chunk = resp.read(1024 * 1024)  # 1 MB chunks
                        if not chunk:
                            break
                        out.write(chunk)

            await loop.run_in_executor(None, _download_url, raw_url, LOCAL_FILE)

        except Exception as e:
            await update.message.reply_text(
                f"❌ Download failed:\n`{e}`\n\n"
                "Make sure the URL is a *direct* download link "
                "\\(not a YouTube/Google Drive preview page\\)\\.",
                parse_mode="MarkdownV2",
            )
            return

        if not os.path.exists(LOCAL_FILE) or os.path.getsize(LOCAL_FILE) == 0:
            await update.message.reply_text("❌ Downloaded file is empty.")
            return

        await _do_stream(update.message, LOCAL_FILE)


# ─────────────────────────────────────────────────────────────────
# FILE HANDLER  —  user sends/forwards a video or audio file
#                  Works for files up to 2 GB (Telegram bot limit)
#                  For larger files use /stream <direct_url>
# ─────────────────────────────────────────────────────────────────

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Triggered when the user sends any of:
      • Video file  (.mp4, .mkv, .avi, .mov …)
      • Audio file  (.mp3, .m4a, .aac, .ogg …)
      • Document    (any file sent as a document)

    Audio-only files are wrapped by FFmpeg with a black video
    track so they can still be streamed as a video.
    """
    if not YOUTUBE_STREAM_URL:
        await update.message.reply_text("❌ YOUTUBE_STREAM_URL secret is missing.")
        return

    if _stream_lock.locked():
        await update.message.reply_text("⚠️ A stream is already LIVE right now.")
        return

    msg = update.message

    # ── Pick the right Telegram file object ───────────────────
    tg_file = None
    is_audio_only = False

    if msg.video:
        tg_file = msg.video
    elif msg.document:
        mime = msg.document.mime_type or ""
        tg_file = msg.document
        if mime.startswith("audio/") or (
            msg.document.file_name or ""
        ).lower().endswith((".mp3", ".m4a", ".aac", ".ogg", ".flac", ".wav")):
            is_audio_only = True
    elif msg.audio:
        tg_file = msg.audio
        is_audio_only = True
    elif msg.voice:
        tg_file = msg.voice
        is_audio_only = True

    if not tg_file:
        await msg.reply_text(
            "❓ Send me a video or audio file and I'll stream it live\\!\n\n"
            "Or use `/stream <direct_url>` for large files \\(\\>2 GB\\)\\.",
            parse_mode="MarkdownV2",
        )
        return

    file_size_mb = getattr(tg_file, "file_size", 0) / (1024 * 1024)
    file_name = getattr(tg_file, "file_name", "uploaded file")

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
            await msg.reply_text(f"❌ Download failed:\n`{e}`", parse_mode="Markdown")
            return

        # ── Wrap audio-only in a black video track ─────────────
        if is_audio_only:
            await msg.reply_text(
                "🎵 Audio file detected — wrapping with black background for stream..."
            )
            audio_wrapped = LOCAL_FILE + "_wrapped.mp4"
            try:
                wrap_result = subprocess.run([
                    "ffmpeg", "-y",
                    "-f", "lavfi", "-i", "color=c=black:s=1080x1920:r=30",
                    "-i", LOCAL_FILE,
                    "-shortest",
                    "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "aac", "-b:a", "192k",
                    "-pix_fmt", "yuv420p",
                    audio_wrapped,
                ], capture_output=True, timeout=300)
                if wrap_result.returncode == 0 and os.path.exists(audio_wrapped):
                    os.remove(LOCAL_FILE)
                    os.rename(audio_wrapped, LOCAL_FILE)
                else:
                    await msg.reply_text(
                        "⚠️ Audio wrapping failed — trying to stream as-is..."
                    )
            except Exception as e:
                await msg.reply_text(f"⚠️ Audio wrap error: `{e}`", parse_mode="Markdown")

        await _do_stream(msg, LOCAL_FILE)


# ─────────────────────────────────────────────────────────────────
# OTHER COMMANDS
# ─────────────────────────────────────────────────────────────────

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status = "🔴 *Stream is currently LIVE.*" if _stream_lock.locked() else "⚪ *No stream running.*"
    await update.message.reply_text(status, parse_mode="Markdown")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎥 *Metropolitan Shorts Live Bot*\n\n"
        "*Upload a file:*\n"
        "Just send a video or audio file — the bot streams it immediately\\.\n\n"
        "*Large files \\(\\>2 GB\\):*\n"
        "`/stream <direct_download_url>`\n"
        "Use a direct link from Dropbox, Google Drive \\(direct\\), "
        "or your own server\\.\n\n"
        "*Other commands:*\n"
        "/status — Check if a stream is running\n"
        "/help — Show this message\n\n"
        "*Output format:*\n"
        "Any input resolution → upscaled to `1080×1920` \\(9:16 vertical\\) "
        "@ 8 Mbps — YouTube displays it as Full HD\\.",
        parse_mode="MarkdownV2",
    )