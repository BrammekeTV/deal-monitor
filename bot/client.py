"""
bot/client.py
~~~~~~~~~~~~~
Creates and configures the discord.py Bot instance.
"""

from __future__ import annotations

import discord
from discord.ext import commands

from utils.logger import get_logger

logger = get_logger(__name__)


def create_bot() -> commands.Bot:
    """Instantiate and return the Discord bot with all intents required."""
    intents = discord.Intents.default()
    intents.message_content = True

    bot = commands.Bot(
        command_prefix="!",  # Kept for legacy; slash commands are preferred.
        intents=intents,
        help_command=None,
        description="Pokémon deal monitor – Vinted edition",
    )

    @bot.event
    async def on_ready() -> None:
        logger.info("Logged in as %s (ID: %s)", bot.user, bot.user.id if bot.user else "?")
        # Sync slash commands with Discord.
        # Guild-scoped sync is instant; global sync can take up to an hour.
        from config.settings import settings

        try:
            if settings.discord_guild_id:
                guild = discord.Object(id=settings.discord_guild_id)
                bot.tree.copy_global_to(guild=guild)
                synced = await bot.tree.sync(guild=guild)
                logger.info(
                    "Synced %d slash command(s) to guild %d",
                    len(synced),
                    settings.discord_guild_id,
                )
            else:
                synced = await bot.tree.sync()
                logger.info("Synced %d slash command(s) globally", len(synced))
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to sync slash commands: %s", exc)

    @bot.event
    async def on_disconnect() -> None:
        logger.warning("Bot disconnected from Discord")

    @bot.event
    async def on_resumed() -> None:
        logger.info("Bot reconnected (session resumed)")

    @bot.event
    async def on_error(event: str, *args, **kwargs) -> None:  # noqa: ANN002
        logger.exception("Unhandled error in event '%s'", event)

    return bot
