import os
import time
import logging
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from shortstream import (
    cmd_stream, cmd_status, cmd_help,
    cmd_url, cmd_playlist,
    handle_file, handle_text,
    TELEGRAM_TOKEN,
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def main():
    if not TELEGRAM_TOKEN:
        logger.error("FATAL: TELEGRAM_BOT_TOKEN is not set in GitHub Secrets.")
        return

    logger.info("Starting Metropolitan Shorts Live Bot...")

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .write_timeout(120.0)
        .pool_timeout(30.0)
        .build()
    )

    # Commands
    app.add_handler(CommandHandler("stream",   cmd_stream))
    app.add_handler(CommandHandler("url",      cmd_url))
    app.add_handler(CommandHandler("playlist", cmd_playlist))
    app.add_handler(CommandHandler("status",   cmd_status))
    app.add_handler(CommandHandler("help",     cmd_help))

    # Plain text URL pasted directly in chat (must be before file handler)
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        handle_text,
    ))

    # Direct file upload — no command needed
    app.add_handler(MessageHandler(
        filters.VIDEO
        | filters.AUDIO
        | filters.VOICE
        | filters.Document.ALL,
        handle_file,
    ))

    logger.info("✅ Bot ready — paste a Drive link or send a file to start streaming!")

    app.run_polling(
        drop_pending_updates=True,
        close_loop=True,
    )


if __name__ == "__main__":
    while True:
        try:
            main()
        except Exception as e:
            logger.error(f"Bot crashed: {e} — restarting in 10s...")
            time.sleep(10)