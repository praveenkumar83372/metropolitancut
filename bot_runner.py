import os
import time
import logging
from telegram.ext import Application, CommandHandler
from shortstream import cmd_stream, cmd_status, cmd_help, TELEGRAM_TOKEN

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
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
        .write_timeout(30.0)
        .pool_timeout(30.0)
        .build()
    )

    app.add_handler(CommandHandler("stream", cmd_stream))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("help",   cmd_help))

    logger.info("✅ Bot online — waiting for /stream commands...")

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