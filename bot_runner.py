import os
import time
import logging
import asyncio
from telegram.ext import Application, CommandHandler, MessageHandler, filters

from shortstream import (
    cmd_stream, cmd_status, cmd_help,
    cmd_url, cmd_playlist,
    handle_file, handle_text,
    check_and_resume_stream,
    TELEGRAM_TOKEN,
)

from longstream import (
    cmd_h,
    check_and_resume_stream_h,
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def main():
    if not TELEGRAM_TOKEN:
        logger.error("FATAL: TELEGRAM_BOT_TOKEN is not set in GitHub Secrets.")
        return

    logger.info("Starting Metropolitan Shorts + Landscape Live Bot...")

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .write_timeout(120.0)
        .pool_timeout(30.0)
        .build()
    )

    # ── Vertical Shorts commands ──────────────────────────────
    app.add_handler(CommandHandler("stream",   cmd_stream))
    app.add_handler(CommandHandler("url",      cmd_url))
    app.add_handler(CommandHandler("playlist", cmd_playlist))
    app.add_handler(CommandHandler("status",   cmd_status))
    app.add_handler(CommandHandler("help",     cmd_help))

    # ── /v  →  vertical 9:16 Shorts stream ───────────────────
    app.add_handler(CommandHandler("v", cmd_v_handler))

    # ── /h  →  horizontal 16:9 Landscape stream ──────────────
    app.add_handler(CommandHandler("h", cmd_h))

    # ── Plain text URL (no command) ───────────────────────────
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        handle_text,
    ))

    # ── Direct file upload ────────────────────────────────────
    app.add_handler(MessageHandler(
        filters.VIDEO
        | filters.AUDIO
        | filters.VOICE
        | filters.Document.ALL,
        handle_file,
    ))

    # ── Auto-resume on runner restart ─────────────────────────
    await check_and_resume_stream(app.bot)
    await check_and_resume_stream_h(app.bot)

    logger.info("✅ Bot ready — /v <link> for Shorts | /h <link> for Landscape")

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    while True:
        await asyncio.sleep(3600)


# ── /v handler (inline — routes to shortstream's handle_text) ─
async def cmd_v_handler(update, context):
    """
    /v <gdrive_link>  →  vertical 9:16 Shorts stream (1080×1920)
    """
    if not context.args:
        await update.message.reply_text(
            "Usage: `/v <google_drive_link>`\n"
            "Example: `/v https://drive.google.com/file/d/xxxx/view`",
            parse_mode="Markdown",
        )
        return

    # Reuse shortstream's full handle_text pipeline
    update.message.text = context.args[0].strip()
    await handle_text(update, context)


# ── Entry point ───────────────────────────────────────────────

if __name__ == "__main__":
    while True:
        try:
            asyncio.run(main())
        except Exception as e:
            logger.error(f"Bot crashed: {e} — restarting in 10s...")
            time.sleep(10)