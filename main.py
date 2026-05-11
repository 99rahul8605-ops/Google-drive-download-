"""
Entrypoint:
1. Starts health check HTTP server (port 8080) for Render
2. Starts Pyrogram bot (handles up to 2GB via MTProto)
"""
import logging
from server import run_in_background
from bot import main

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

if __name__ == "__main__":
    run_in_background()
    main()
