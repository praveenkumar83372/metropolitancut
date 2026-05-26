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

# Optional: residential proxy to bypass datacenter IP blocks
# Set in GitHub Secrets as:  PROXY_URL=http://user:pass@host:port
# Free option: use webshare.io free tier (10 residential proxies)
# or any socks5/http residential proxy
PROXY_URL = os.getenv("PROXY_URL", "")

LOCAL_FILE = "walking_tour.mp4"

logger = logging.getLogger(__name__)
_stream_lock = asyncio.Lock()


# ─────────────────────────────────────────────────────────────────
# DOWNLOAD  —  Proxy + client fallback chain
# ─────────────────────────────────────────────────────────────────

def download_video(url: str, out_path: str) -> tuple[bool, str]:
    format_string = (
        "bestvideo[height<=2160][ext=mp4]+bestaudio[ext=m4a]/"
        "bestvideo[height<=2160]+bestaudio/"
        "bestvideo[height<=1080]+bestaudio/"
        "best"
    )

    # All clients to try — on a residential IP any of these work;
    # android_vr first because it skips JS player entirely
    clients = ["android_vr", "tv", "web_embedded", "ios", "android", "web"]

    # Build proxy-aware option sets:
    #   Pass 1  — with proxy (if configured)
    #   Pass 2  — without proxy (fallback, in case proxy itself fails)
    proxy_passes = []
    if PROXY_URL:
        proxy_passes.append(("with proxy", PROXY_URL))
    proxy_passes.append(("no proxy", None))

    for pass_label, proxy in proxy_passes:
        for client in clients:
            label = f"{client} / {pass_label}"
            logger.info(f"Trying: [{label}]")

            if os.path.exists(out_path):
                os.remove(out_path)

            ydl_opts = {
                "format": format_string,
                "outtmpl": out_path,
                "merge_output_format": "mp4",
                "quiet": False,
                "no_warnings": False,
                "noprogress": True,
                "extractor_args": {
                    "youtube": {"player_client": [client]}
                },
                "retries": 3,
                "fragment_retries": 3,
            }

            if proxy:
                ydl_opts["proxy"] = proxy

            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    if (
                        info
                        and os.path.exists(out_path)
                        and os.path.getsize(out_path) > 0
                    ):
                        w = info.get("width", 0)
                        h = info.get("height", 0)
                        resolution = f"{w}x{h}" if w and h else "unknown"
                        logger.info(f"✅ Success [{label}] @ {resolution}")
                        return True, resolution
            except yt_dlp.utils.DownloadError as e:
                logger.warning(f"[{label}] failed: {e}")
                continue
            except Exception as e:
                logger.error(f"[{label}] unexpected error: {e}")
                continue

    if os.path.exists(out_path):
        os.remove(out_path)
    return False, "error"


# ─────────────────────────────────────────────────────────────────
# STREAM  —  16:9 → 9:16 center crop
# ─────────────────────────────────────────────────────────────────

def stream_to_youtube(file_path: str, rtmp_destination: str) -> bool:
    ffmpeg_cmd = [
        "ffmpeg", "-re", "-i", file_path,
        "-vf", "crop=ih*9/16:ih:(iw-ih*9/16)/2:0,scale=1080:1920",
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

        proxy_note = f"\n🔀 Proxy: `{PROXY_URL.split('@')[-1]}`" if PROXY_URL else "\n⚠️ No proxy set"
        await update.message.reply_text(
            f"📥 *Downloading video...*\n\n`{video_url}`{proxy_note}",
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
            tip = (
                "💡 Add a `PROXY_URL` secret (residential proxy) to bypass YouTube's datacenter IP block."
                if not PROXY_URL
                else "💡 The proxy may be blocked too — try a different residential proxy."
            )
            await update.message.reply_text(
                f"❌ All download strategies failed.\n\n{tip}",
                parse_mode="Markdown",
            )
            return

        vinfo = get_video_info(LOCAL_FILE)
        await update.message.reply_text(
            f"🎬 *Download complete\\! Going LIVE\\.\\.\\.*\n\n"
            f"📐 Source: `{source_res}`\n"
            f"📐 Output: `1080×1920` \\(9:16 vertical\\)\n"
            f"⏱ Duration: `{vinfo['duration']}`\n"
            f"💾 Size: `{vinfo['size']}`",
            parse_mode="MarkdownV2",
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
    status = "🔴 *Stream is currently LIVE.*" if _stream_lock.locked() else "⚪ *No stream running.*"
    await update.message.reply_text(status, parse_mode="Markdown")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎥 *Metropolitan Shorts Live Bot*\n\n"
        "/stream `<url>` — Download & Go Live\n"
        "/status — Check if stream is running\n"
        "/help — Show this message",
        parse_mode="Markdown",
    )