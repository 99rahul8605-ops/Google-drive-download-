"""
Entrypoint:
1. Starts Telegram local Bot API server (port 8081) — removes 50MB limit, allows up to 2GB
2. Starts health check HTTP server (port 8080) — for Render port detection
3. Starts the Telegram bot
"""

import os
import sys
import time
import logging
import subprocess
import requests
from server import run_in_background
from bot import main

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
TELEGRAM_API_ID = os.environ.get("TELEGRAM_API_ID", "")
TELEGRAM_API_HASH = os.environ.get("TELEGRAM_API_HASH", "")


def start_local_api_server():
    """Start the Telegram local Bot API server."""
    if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
        logger.error(
            "TELEGRAM_API_ID and TELEGRAM_API_HASH are required for local Bot API server!\n"
            "Get them from https://my.telegram.org"
        )
        sys.exit(1)

    cmd = [
        "telegram-bot-api",
        "--api-id", TELEGRAM_API_ID,
        "--api-hash", TELEGRAM_API_HASH,
        "--local",
        "--http-port", "8081",
        "--dir", "/tmp/tgdata",
    ]

    logger.info("Starting Telegram local Bot API server on port 8081...")
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Wait until the local server is ready
    for _ in range(30):
        try:
            r = requests.get(f"http://localhost:8081/bot{BOT_TOKEN}/getMe", timeout=2)
            if r.status_code in (200, 401):  # 401 = bad token but server is up
                logger.info("Local Bot API server is ready!")
                return proc
        except Exception:
            pass
        time.sleep(1)

    logger.error("Local Bot API server did not start in time!")
    sys.exit(1)


if __name__ == "__main__":
    # 1. Start local Telegram Bot API server
    api_proc = start_local_api_server()

    # 2. Start health check server (for Render)
    logger.info("Starting health check server on port 8080...")
    run_in_background()

    # 3. Start the bot
    logger.info("Starting Telegram bot...")
    try:
        main()
    finally:
        api_proc.terminate()
