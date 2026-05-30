"""
main.py
~~~~~~~
Entry point for the Pokémon deal-monitor Discord bot.

Usage::

    python main.py

Environment variables (see .env.example):
    DISCORD_BOT_TOKEN      – Required. Your bot token from Discord Developer Portal.
    DISCORD_CHANNEL_ID     – Required. Channel ID to post deals into.
    DISCORD_WEBHOOK_URL    – Optional. Fallback webhook URL.
    LOG_LEVEL              – Optional. Logging level (DEBUG/INFO/WARNING). Default: INFO.
    DATABASE_PATH          – Optional. Path to the SQLite database file.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

from bot.client import create_bot
from bot.cogs.check_card import CheckCardCog
from bot.cogs.filters import FiltersCog
from bot.cogs.maintenance import MaintenanceCog
from bot.cogs.monitor import MonitorCog
from bot.cogs.review import ReviewCog
from bot.cogs.test_cardmarket import TestCardmarketCog
from config.settings import settings
from database.db import Database
from utils.logger import configure_logging, get_logger

# ---------------------------------------------------------------------------
# Configure logging first so all subsequent imports see it.
# ---------------------------------------------------------------------------
configure_logging(level=os.getenv("LOG_LEVEL", "INFO"))
logger = get_logger(__name__)


async def main() -> None:
    """Bootstrap the database, load cogs, and run the bot."""
    # Validate essential config.
    if not settings.discord_token:
        logger.critical("DISCORD_BOT_TOKEN is not set. Exiting.")
        sys.exit(1)

    if not settings.discord_channel_id:
        logger.warning(
            "DISCORD_CHANNEL_ID is not set – deals will only be posted via webhook."
        )

    # Open database.
    db_path = os.getenv("DATABASE_PATH", "data/deals.db")
    db = Database(db_path=Path(db_path))
    await db.connect()
    logger.info("Database ready at %s", db_path)

    # Restore filter overrides persisted from previous sessions.
    overrides = await db.get_all_filters()
    for key, value in overrides.items():
        try:
            setattr(settings, key, float(value) if "." in value else int(float(value)))
            logger.debug("Restored filter override: %s=%s", key, value)
        except Exception:  # noqa: BLE001
            pass

    # Create bot and register cogs.
    bot = create_bot()
    await bot.add_cog(ReviewCog(bot, db))
    await bot.add_cog(MonitorCog(bot, db))
    await bot.add_cog(FiltersCog(bot, db))
    await bot.add_cog(MaintenanceCog(bot))
    await bot.add_cog(CheckCardCog(bot))
    await bot.add_cog(TestCardmarketCog(bot))

    # Graceful shutdown handler.
    loop = asyncio.get_running_loop()

    def _shutdown(*_) -> None:  # noqa: ANN002
        logger.info("Shutdown signal received")
        loop.create_task(_graceful_shutdown(bot, db))

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler.
            pass

    try:
        await bot.start(settings.discord_token)
    except Exception as exc:  # noqa: BLE001
        logger.critical("Bot crashed: %s", exc, exc_info=True)
    finally:
        await db.close()
        logger.info("Shutdown complete")


async def _graceful_shutdown(bot, db: Database) -> None:  # noqa: ANN001
    logger.info("Closing bot connection…")
    await bot.close()
    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
