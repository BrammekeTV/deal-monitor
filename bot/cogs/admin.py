"""
bot/cogs/admin.py
~~~~~~~~~~~~~~~~~
Administrator slash commands for managing the bot's learning database,
review queue, and filter settings.
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from config.settings import settings
from database.db import Database
from utils.logger import get_logger

logger = get_logger(__name__)

# Only members with the Administrator permission may use these commands.
_ADMIN_ONLY = app_commands.default_permissions(administrator=True)


def _trunc(text: str, length: int = 60) -> str:
    return text[:length] + "…" if len(text) > length else text


class AdminCog(commands.Cog, name="Admin"):
    """Admin commands for the deal monitor bot."""

    def __init__(self, bot: commands.Bot, db: Database) -> None:
        self.bot = bot
        self.db = db

    # ------------------------------------------------------------------
    # /status  (also exposed in MonitorCog but available here too)
    # ------------------------------------------------------------------

    @app_commands.command(name="admin_status", description="[Admin] Show monitoring bot status")
    @_ADMIN_ONLY
    async def admin_status(self, interaction: discord.Interaction) -> None:
        from utils.embed_builder import build_status_embed

        monitor = self.bot.cogs.get("Monitor")
        if monitor:
            mappings = await self.db.get_all_mappings()
            embed = build_status_embed(
                listings_checked=getattr(monitor, "_listings_checked", 0),
                listings_profitable=getattr(monitor, "_listings_profitable", 0),
                listings_reviewed=getattr(monitor, "_listings_reviewed", 0),
                mappings_count=len(mappings),
                last_run=getattr(monitor, "_last_run", None),
                next_run=getattr(monitor, "_next_run", None),
                search_terms=settings.search_terms,
                paused=getattr(monitor, "_paused", False),
            )
        else:
            embed = discord.Embed(
                title="Bot Status",
                description="Monitor cog is not loaded.",
                colour=discord.Colour.red(),
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # /mappings  — list learned card→Cardmarket mappings
    # ------------------------------------------------------------------

    @app_commands.command(
        name="mappings",
        description="[Admin] List all learned card→Cardmarket mappings",
    )
    @_ADMIN_ONLY
    async def list_mappings(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        mappings = await self.db.get_all_mappings()

        if not mappings:
            await interaction.followup.send("No learned mappings yet.", ephemeral=True)
            return

        # Build a paginated-style embed (up to 25 entries for embed field limit).
        embed = discord.Embed(
            title=f"Learned Mappings ({len(mappings)} total)",
            colour=discord.Colour.blurple(),
        )
        for m in mappings[:25]:
            name = f"[{m['id']}] {_trunc(m['card_name'] or m['raw_title'], 40)}"
            value = (
                f"Set: `{m.get('set_code') or 'N/A'}`  "
                f"Confidence: `{float(m.get('confidence', 0)):.0%}`\n"
                f"[Cardmarket]({m['cardmarket_url']})"
            )
            embed.add_field(name=name, value=value, inline=False)

        if len(mappings) > 25:
            embed.set_footer(text=f"Showing first 25 of {len(mappings)} mappings.")

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # /delete_mapping  — remove a mapping by ID
    # ------------------------------------------------------------------

    @app_commands.command(
        name="delete_mapping",
        description="[Admin] Delete a learned card mapping by ID",
    )
    @app_commands.describe(mapping_id="The numeric ID of the mapping to delete")
    @_ADMIN_ONLY
    async def delete_mapping(
        self, interaction: discord.Interaction, mapping_id: int
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        deleted = await self.db.delete_mapping(mapping_id)
        if deleted:
            # Also reload resolver in-memory cache.
            monitor = self.bot.cogs.get("Monitor")
            if monitor and getattr(monitor, "resolver", None):
                await monitor.resolver.load()  # type: ignore[union-attr]
            await interaction.followup.send(
                f"✅ Mapping `{mapping_id}` deleted and resolver cache refreshed.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"⚠️ No mapping with ID `{mapping_id}` found.",
                ephemeral=True,
            )

    # ------------------------------------------------------------------
    # /pending_reviews  — list items waiting in the review queue
    # ------------------------------------------------------------------

    @app_commands.command(
        name="pending_reviews",
        description="[Admin] List pending review queue items",
    )
    @_ADMIN_ONLY
    async def pending_reviews(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        items = await self.db.get_pending_review_items()

        if not items:
            await interaction.followup.send(
                "✅ No pending reviews.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title=f"Pending Reviews ({len(items)})",
            colour=discord.Colour.orange(),
        )
        for item in items[:25]:
            name = _trunc(item.get("title", "Unknown"), 50)
            discord_msg_id = item.get("discord_message_id")
            review_channel_id = item.get("review_channel_id")
            jump_link = ""
            if discord_msg_id and review_channel_id:
                guild_id = interaction.guild_id or "@me"
                jump_link = (
                    f"[Review message](https://discord.com/channels/{guild_id}"
                    f"/{review_channel_id}/{discord_msg_id})\n"
                )
            value = (
                f"Price: `€{float(item.get('price', 0)):.2f}`\n"
                f"[Vinted listing]({item.get('url', '#')})\n"
                + jump_link
                + f"Reason: {_trunc(item.get('failure_reason', '—'), 80)}"
            )
            embed.add_field(name=name, value=value, inline=False)

        if len(items) > 25:
            embed.set_footer(text=f"Showing first 25 of {len(items)} items.")

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # /set_filter  —  manage keyword/price filters
    # ------------------------------------------------------------------

    @app_commands.command(
        name="set_filter",
        description="[Admin] Add or update a filter setting (key=value)",
    )
    @app_commands.describe(key="Filter key", value="Filter value")
    @_ADMIN_ONLY
    async def set_filter(
        self, interaction: discord.Interaction, key: str, value: str
    ) -> None:
        await self.db.set_filter(key.strip(), value.strip())
        await interaction.response.send_message(
            f"✅ Filter `{key}` set to `{value}`.", ephemeral=True
        )

    # ------------------------------------------------------------------
    # /delete_filter
    # ------------------------------------------------------------------

    @app_commands.command(
        name="delete_filter",
        description="[Admin] Remove a filter setting by key",
    )
    @app_commands.describe(key="Filter key to remove")
    @_ADMIN_ONLY
    async def delete_filter(self, interaction: discord.Interaction, key: str) -> None:
        deleted = await self.db.delete_filter(key.strip())
        if deleted:
            await interaction.response.send_message(
                f"✅ Filter `{key}` removed.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"⚠️ No filter `{key}` found.", ephemeral=True
            )

    # ------------------------------------------------------------------
    # /filters  — list all active filters
    # ------------------------------------------------------------------

    @app_commands.command(
        name="filters",
        description="[Admin] List all active filter settings",
    )
    @_ADMIN_ONLY
    async def list_filters(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        filters = await self.db.get_all_filters()

        if not filters:
            await interaction.followup.send("No filters configured.", ephemeral=True)
            return

        embed = discord.Embed(title="Active Filters", colour=discord.Colour.green())
        for key, val in filters.items():
            embed.add_field(name=key, value=f"`{val}`", inline=True)

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # /error_summary  — aggregate failure report
    # ------------------------------------------------------------------

    @app_commands.command(
        name="error_summary",
        description="[Admin] Show aggregate error counts grouped by failure step",
    )
    @_ADMIN_ONLY
    async def error_summary(self, interaction: discord.Interaction) -> None:
        """Display the top failure patterns from the error log."""
        await interaction.response.defer(ephemeral=True)
        rows = await self.db.get_error_summary()

        if not rows:
            await interaction.followup.send(
                "No errors logged yet.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title="🔴 Error Summary",
            description=(
                "Top error patterns from the error log, grouped by failure step "
                "and message (most frequent first)."
            ),
            colour=discord.Colour.red(),
        )

        for row in rows[:20]:  # Embed field limit is 25; keep some headroom.
            step = row.get("failure_step") or "unknown"
            msg = row.get("error_message") or "—"
            count = row.get("count", 0)
            embed.add_field(
                name=f"`{step}` — {count}×",
                value=_trunc(msg, 80),
                inline=False,
            )

        if len(rows) > 20:
            embed.set_footer(text=f"Showing top 20 of {len(rows)} patterns.")

        await interaction.followup.send(embed=embed, ephemeral=True)
