import os
import time
import logging
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from shortstream import (
    cmd_stream, cmd_status, cmd_help,
    cmd_url,
    handle_file,
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
    app.add_handler(CommandHandler("stream", cmd_stream))
    app.add_handler(CommandHandler("url",    cmd_url))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("help",   cmd_help))

    # Any video/audio/document sent directly — no command needed
    app.add_handler(MessageHandler(
        filters.VIDEO
        | filters.AUDIO
        | filters.VOICE
        | filters.Document.ALL,   # catch ALL documents so nothing slips through
        handle_file,
    ))

    logger.info("✅ Bot ready — just send a video/audio file to start streaming!")

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