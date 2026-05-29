"""
bot/cogs/filters.py
~~~~~~~~~~~~~~~~~~~
Discord slash commands for managing deal filters at runtime.

Commands:
  /status         – Show bot status and statistics.
  /set_filter     – Override a filter value (max_price, min_score, …).
  /get_filters    – List all active filter overrides.
  /del_filter     – Remove a filter override (revert to config.yaml).
  /add_term       – Add a search term for the current session.
  /remove_term    – Remove a search term for the current session.
  /reload_config  – Reload config.yaml from disk.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from config.settings import settings
from database.db import Database
from utils.embed_builder import build_status_embed
from utils.logger import get_logger

if TYPE_CHECKING:
    from bot.cogs.monitor import MonitorCog

logger = get_logger(__name__)

# Keys that can be overridden via /set_filter.
_VALID_FILTER_KEYS = {
    "max_price",
    "min_score",
    "min_seller_rating",
    "discount_threshold_pct",
    "max_retries",
    "interval_min",
    "interval_max",
}


class FiltersCog(commands.Cog, name="Filters"):
    """Slash commands for runtime configuration."""

    def __init__(self, bot: commands.Bot, db: Database) -> None:
        self.bot = bot
        self.db = db

    def _get_monitor(self) -> "MonitorCog | None":
        return self.bot.cogs.get("Monitor")  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # /status
    # ------------------------------------------------------------------

    @app_commands.command(name="status", description="Show deal monitor status")
    async def status(self, interaction: discord.Interaction) -> None:
        monitor = self._get_monitor()
        embed = build_status_embed(
            listings_checked=monitor.listings_checked if monitor else 0,
            listings_posted=monitor.listings_posted if monitor else 0,
            last_run=monitor.last_run if monitor else None,
            next_run=monitor.next_run if monitor else None,
            search_terms=settings.search_terms,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # /set_filter
    # ------------------------------------------------------------------

    @app_commands.command(
        name="set_filter",
        description="Override a filter value (e.g. max_price, min_score)",
    )
    @app_commands.describe(key="Filter key to set", value="New value")
    async def set_filter(
        self, interaction: discord.Interaction, key: str, value: str
    ) -> None:
        if key not in _VALID_FILTER_KEYS:
            valid = ", ".join(sorted(_VALID_FILTER_KEYS))
            await interaction.response.send_message(
                f"❌ Unknown filter key `{key}`. Valid keys: {valid}", ephemeral=True
            )
            return
        try:
            float(value)  # Validate it parses as a number.
        except ValueError:
            await interaction.response.send_message(
                f"❌ Value `{value}` must be a number.", ephemeral=True
            )
            return

        await self.db.set_filter(key, value)
        # Apply to running settings immediately.
        try:
            setattr(settings, key, float(value) if "." in value else int(float(value)))
        except Exception:  # noqa: BLE001
            pass
        logger.info("/set_filter %s=%s by %s", key, value, interaction.user)
        await interaction.response.send_message(
            f"✅ `{key}` set to `{value}`", ephemeral=True
        )

    # ------------------------------------------------------------------
    # /get_filters
    # ------------------------------------------------------------------

    @app_commands.command(
        name="get_filters", description="List all active filter overrides"
    )
    async def get_filters(self, interaction: discord.Interaction) -> None:
        overrides = await self.db.get_all_filters()
        if not overrides:
            await interaction.response.send_message(
                "No filter overrides set. Using config.yaml defaults.", ephemeral=True
            )
            return
        lines = [f"`{k}` = `{v}`" for k, v in sorted(overrides.items())]
        embed = discord.Embed(
            title="Filter Overrides",
            description="\n".join(lines),
            colour=0x5865F2,
            timestamp=datetime.now(timezone.utc),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # /del_filter
    # ------------------------------------------------------------------

    @app_commands.command(
        name="del_filter",
        description="Remove a filter override (revert to config.yaml default)",
    )
    @app_commands.describe(key="Filter key to remove")
    async def del_filter(self, interaction: discord.Interaction, key: str) -> None:
        deleted = await self.db.delete_filter(key)
        if deleted:
            await interaction.response.send_message(
                f"✅ Filter `{key}` removed.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"⚠️ Filter `{key}` was not set.", ephemeral=True
            )

    # ------------------------------------------------------------------
    # /add_term / /remove_term
    # ------------------------------------------------------------------

    @app_commands.command(
        name="add_term", description="Add a search term to the current session"
    )
    @app_commands.describe(term="Search term to add")
    async def add_term(self, interaction: discord.Interaction, term: str) -> None:
        if term not in settings.search_terms:
            settings.search_terms.append(term)
            logger.info("/add_term '%s' by %s", term, interaction.user)
            await interaction.response.send_message(
                f"✅ Added search term `{term}`.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"ℹ️ Term `{term}` already exists.", ephemeral=True
            )

    @app_commands.command(
        name="remove_term", description="Remove a search term from the current session"
    )
    @app_commands.describe(term="Search term to remove")
    async def remove_term(self, interaction: discord.Interaction, term: str) -> None:
        if term in settings.search_terms:
            settings.search_terms.remove(term)
            logger.info("/remove_term '%s' by %s", term, interaction.user)
            await interaction.response.send_message(
                f"✅ Removed search term `{term}`.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"⚠️ Term `{term}` not in current list.", ephemeral=True
            )

    # ------------------------------------------------------------------
    # /reload_config
    # ------------------------------------------------------------------

    @app_commands.command(
        name="reload_config", description="Reload config.yaml from disk"
    )
    async def reload_config(self, interaction: discord.Interaction) -> None:
        try:
            settings.reload()
            logger.info("/reload_config by %s", interaction.user)
            await interaction.response.send_message(
                "✅ Configuration reloaded from `config/config.yaml`.", ephemeral=True
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Config reload failed: %s", exc)
            await interaction.response.send_message(
                f"❌ Reload failed: {exc}", ephemeral=True
            )
