import os
import re
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

# In-memory playlist
_playlist: list[dict] = []


# ─────────────────────────────────────────────────────────────────
# GOOGLE DRIVE
# ─────────────────────────────────────────────────────────────────

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


def make_gdrive_direct(url: str) -> str | None:
    if "drive.google.com" not in url and "docs.google.com" not in url:
        return None
    file_id = extract_gdrive_id(url)
    if not file_id:
        return None
    return f"https://drive.google.com/uc?export=download&id={file_id}&confirm=t"


# ─────────────────────────────────────────────────────────────────
# STREAM  —  Any resolution → 1080×1920 9:16
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
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration", "-of", "default=noprint_wrappers=1", file_path],
            capture_output=True, text=True, timeout=15,
        )
        return "duration=" in r.stdout
    except Exception:
        return False


async def _download_url(url: str, dest: str, message: Message) -> bool:
    import urllib.request

    gdrive_direct = make_gdrive_direct(url)
    if gdrive_direct:
        await message.reply_text(
            "☁️ *Google Drive link detected — converting to direct download...*",
            parse_mode="Markdown",
        )
        download_url = gdrive_direct
    else:
        download_url = url

    loop = asyncio.get_event_loop()

    def _dl(u, d):
        req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=300) as resp, open(d, "wb") as out:
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)

    try:
        await loop.run_in_executor(None, _dl, download_url, dest)
        return True
    except Exception as e:
        await message.reply_text(
            f"❌ Download failed:\n`{e}`\n\n"
            "For Google Drive: make sure the file is shared as *'Anyone with the link'*.",
            parse_mode="Markdown",
        )
        return False


async def _do_stream(message: Message, file_path: str):
    loop = asyncio.get_event_loop()

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
# PLAIN TEXT HANDLER  —  user pastes a URL directly in chat
# ─────────────────────────────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    if not text.startswith(("http://", "https://")):
        await update.message.reply_text(
            "❓ I only understand URLs or file uploads.\n\n"
            "Paste a Google Drive link, use `/url <link>`, or just upload a file directly.",
            parse_mode="Markdown",
        )
        return

    blocked = ("youtube.com", "youtu.be")
    if any(b in text for b in blocked):
        await update.message.reply_text(
            "❌ YouTube URLs are not supported.\n"
            "Download the video first, then send it here or upload to Google Drive."
        )
        return

    if not YOUTUBE_STREAM_URL:
        await update.message.reply_text("❌ YOUTUBE_STREAM_URL secret is missing.")
        return

    if _stream_lock.locked():
        await update.message.reply_text("⚠️ A stream is already LIVE right now.")
        return

    async with _stream_lock:
        await update.message.reply_text(
            f"🔗 *URL detected — downloading...*\n\n`{text}`",
            parse_mode="Markdown",
        )

        if os.path.exists(LOCAL_FILE):
            os.remove(LOCAL_FILE)

        ok = await _download_url(text, LOCAL_FILE, update.message)
        if not ok:
            return

        if not os.path.exists(LOCAL_FILE) or os.path.getsize(LOCAL_FILE) == 0:
            await update.message.reply_text("❌ Downloaded file is empty.")
            return

        await _do_stream(update.message, LOCAL_FILE)


# ─────────────────────────────────────────────────────────────────
# /url  —  stream a single URL
# ─────────────────────────────────────────────────────────────────

async def cmd_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not YOUTUBE_STREAM_URL:
        await update.message.reply_text("❌ YOUTUBE_STREAM_URL secret is missing.")
        return

    if _stream_lock.locked():
        await update.message.reply_text("⚠️ A stream is already LIVE right now.")
        return

    if not context.args:
        await update.message.reply_text(
            "Usage: `/url <link>`\n\n"
            "Supported:\n"
            "• *Google Drive* share link\n"
            "• *Dropbox*: change `?dl=0` → `?dl=1`\n"
            "• *Direct link*: `https://yourserver.com/video.mp4`\n\n"
            "⚠️ YouTube links won't work.",
            parse_mode="Markdown",
        )
        return

    raw_url = context.args[0]

    blocked = ("youtube.com", "youtu.be", "youtube-nocookie.com")
    if any(b in raw_url for b in blocked):
        await update.message.reply_text(
            "❌ YouTube URLs are not supported.\n"
            "Download the video first, then send it here or upload to Google Drive."
        )
        return

    if not raw_url.startswith(("http://", "https://")):
        await update.message.reply_text("❌ Please provide a valid http/https URL.")
        return

    async with _stream_lock:
        await update.message.reply_text(
            f"📥 *Downloading...*\n\n`{raw_url}`",
            parse_mode="Markdown",
        )

        if os.path.exists(LOCAL_FILE):
            os.remove(LOCAL_FILE)

        ok = await _download_url(raw_url, LOCAL_FILE, update.message)
        if not ok:
            return

        if not os.path.exists(LOCAL_FILE) or os.path.getsize(LOCAL_FILE) == 0:
            await update.message.reply_text("❌ Downloaded file is empty.")
            return

        await _do_stream(update.message, LOCAL_FILE)


# ─────────────────────────────────────────────────────────────────
# /playlist  —  queue of URLs
# ─────────────────────────────────────────────────────────────────

async def cmd_playlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _playlist
    args = context.args

    if not args:
        if not _playlist:
            await update.message.reply_text(
                "📋 *Playlist is empty.*\n\n"
                "Add videos with:\n`/playlist add <Google Drive or direct URL>`",
                parse_mode="Markdown",
            )
            return
        lines = [f"📋 *Playlist ({len(_playlist)} items):*\n"]
        for i, item in enumerate(_playlist, 1):
            lines.append(f"{i}. {item['label']}")
        lines.append("\nUse `/playlist start` to stream all in order.")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return

    sub = args[0].lower()

    if sub == "add":
        if len(args) < 2:
            await update.message.reply_text("Usage: `/playlist add <url>`", parse_mode="Markdown")
            return
        url = args[1]
        if not url.startswith(("http://", "https://")):
            await update.message.reply_text("❌ Invalid URL.")
            return
        blocked = ("youtube.com", "youtu.be")
        if any(b in url for b in blocked):
            await update.message.reply_text("❌ YouTube URLs are not supported.")
            return
        gdrive_id = extract_gdrive_id(url)
        label = f"Drive:{gdrive_id[:12]}…" if gdrive_id else url[:60]
        _playlist.append({"url": url, "label": label})
        await update.message.reply_text(
            f"✅ Added to playlist (#{len(_playlist)}):\n`{label}`",
            parse_mode="Markdown",
        )
        return

    if sub == "remove":
        if len(args) < 2 or not args[1].isdigit():
            await update.message.reply_text("Usage: `/playlist remove <number>`", parse_mode="Markdown")
            return
        idx = int(args[1]) - 1
        if idx < 0 or idx >= len(_playlist):
            await update.message.reply_text("❌ Invalid item number.")
            return
        removed = _playlist.pop(idx)
        await update.message.reply_text(f"🗑 Removed: `{removed['label']}`", parse_mode="Markdown")
        return

    if sub == "clear":
        _playlist.clear()
        await update.message.reply_text("🗑 Playlist cleared.")
        return

    if sub == "start":
        if not YOUTUBE_STREAM_URL:
            await update.message.reply_text("❌ YOUTUBE_STREAM_URL secret is missing.")
            return
        if _stream_lock.locked():
            await update.message.reply_text("⚠️ A stream is already LIVE right now.")
            return
        if not _playlist:
            await update.message.reply_text("📋 Playlist is empty. Add URLs first.")
            return

        total = len(_playlist)
        await update.message.reply_text(
            f"▶️ *Starting playlist — {total} video(s)...*",
            parse_mode="Markdown",
        )

        async with _stream_lock:
            items = list(_playlist)
            for i, item in enumerate(items, 1):
                await update.message.reply_text(
                    f"🎬 *Playing {i}/{total}:*\n`{item['label']}`",
                    parse_mode="Markdown",
                )

                if os.path.exists(LOCAL_FILE):
                    os.remove(LOCAL_FILE)

                ok = await _download_url(item["url"], LOCAL_FILE, update.message)
                if not ok:
                    await update.message.reply_text(f"⏭ Skipping item {i} due to download error.")
                    continue

                if not os.path.exists(LOCAL_FILE) or os.path.getsize(LOCAL_FILE) == 0:
                    await update.message.reply_text(f"⏭ Skipping item {i} — empty file.")
                    continue

                await _do_stream(update.message, LOCAL_FILE)

            await update.message.reply_text(
                f"✅ *Playlist finished! All {total} video(s) streamed.*",
                parse_mode="Markdown",
            )
        return

    await update.message.reply_text(
        "Unknown subcommand.\n\n"
        "`/playlist` — show list\n"
        "`/playlist add <url>` — add video\n"
        "`/playlist remove <n>` — remove item\n"
        "`/playlist clear` — clear all\n"
        "`/playlist start` — stream all",
        parse_mode="Markdown",
    )


# ─────────────────────────────────────────────────────────────────
# COMMANDS
# ─────────────────────────────────────────────────────────────────

async def cmd_stream(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *How to stream:*\n\n"
        "🔗 *Paste a Google Drive link directly in chat* — just send the URL, no command needed!\n\n"
        "🎬 *Single video via command:*\n"
        "`/url <Google Drive or direct link>`\n\n"
        "📋 *Playlist (multiple videos):*\n"
        "`/playlist add <url>` — add to queue\n"
        "`/playlist` — view queue\n"
        "`/playlist start` — stream all in order\n\n"
        "📁 *Upload directly:*\n"
        "Just send a video/audio file here — no command needed.\n\n"
        "❌ YouTube links won't work — download the video first.",
        parse_mode="Markdown",
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _stream_lock.locked():
        status = "🔴 *Stream is currently LIVE.*"
    else:
        pl_count = len(_playlist)
        status = "⚪ *No stream running.*"
        if pl_count:
            status += f"\n📋 Playlist has {pl_count} item(s) queued."
    await update.message.reply_text(status, parse_mode="Markdown")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎥 *Metropolitan Shorts Live Bot*\n\n"
        "*Easiest way — just paste a Google Drive link in chat!*\n\n"
        "*Commands:*\n"
        "`/url <link>` — stream one video\n"
        "`/playlist add <url>` — add to queue\n"
        "`/playlist` — view queue\n"
        "`/playlist remove <n>` — remove item\n"
        "`/playlist clear` — clear all\n"
        "`/playlist start` — stream all in order\n"
        "`/stream` — how-to guide\n"
        "`/status` — is a stream running?\n"
        "`/help` — this message\n\n"
        "*Upload directly:* just send a video/audio file here\n\n"
        "*Output:* Any input → `1080x1920` 9:16 @ 8 Mbps",
        parse_mode="Markdown",
    )