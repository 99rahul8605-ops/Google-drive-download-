"""
Entrypoint: starts the health-check HTTP server (for Render) 
then launches the Telegram bot in the main thread.
"""

import logging
from server import run_in_background
from bot import main

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

if __name__ == "__main__":
    logger.info("Starting health check server...")
    run_in_background()

    logger.info("Starting Telegram bot...")
    main()
