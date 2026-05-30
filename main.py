"""
main.py
~~~~~~~
Entry point for the deal-monitor Discord bot.

Usage::

    python main.py

Environment variables (see .env.example):
    DISCORD_BOT_TOKEN           – Required. Your bot token from Discord Developer Portal.
    DISCORD_CHANNEL_ID          – Required. Channel ID to post profit alerts.
    DISCORD_REVIEW_CHANNEL_ID   – Required. Channel ID for manual review queue.
    DISCORD_LOG_CHANNEL_ID      – Required. Channel ID for error/scraping logs.
    DISCORD_GUILD_ID            – Optional. Enables instant slash-command sync.
    DATABASE_PATH               – Optional. Path to SQLite database (default: data/deals.db).
    LOG_LEVEL                   – Optional. Logging level (DEBUG/INFO/WARNING). Default: INFO.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

from bot.client import create_bot
from bot.cogs.admin import AdminCog
from bot.cogs.monitor import MonitorCog
from bot.cogs.review import ReviewCog
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
        logger.warning("DISCORD_CHANNEL_ID is not set – profit alerts will not be posted.")

    if not settings.discord_review_channel_id:
        logger.warning(
            "DISCORD_REVIEW_CHANNEL_ID is not set – unresolved listings will not be posted."
        )

    if not settings.discord_log_channel_id:
        logger.warning(
            "DISCORD_LOG_CHANNEL_ID is not set – scraping errors will only be logged locally."
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
    # ReviewCog must be added before MonitorCog so the on_message listener
    # is registered before the monitor starts posting review messages.
    bot = create_bot()
    await bot.add_cog(ReviewCog(bot, db))
    await bot.add_cog(MonitorCog(bot, db))
    await bot.add_cog(AdminCog(bot, db))

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
