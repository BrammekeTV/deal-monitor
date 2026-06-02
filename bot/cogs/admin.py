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
        # Maps message_id → True when that message awaits a 🗑️ purge reaction.
        self._pending_purge_messages: dict[int, bool] = {}

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
    # /catalog  — mapping counts per set
    # ------------------------------------------------------------------

    @app_commands.command(
        name="catalog",
        description="[Admin] Show how many learned mappings exist per set",
    )
    @_ADMIN_ONLY
    async def catalog_summary(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        rows = await self.db.get_mapping_counts_per_set()

        if not rows:
            await interaction.followup.send("No learned mappings yet.", ephemeral=True)
            return

        total = sum(r["count"] for r in rows)
        lines = [f"`{r['set_code']}` — **{r['count']}**" for r in rows[:30]]
        if len(rows) > 30:
            lines.append(f"… and {len(rows) - 30} more sets")

        embed = discord.Embed(
            title=f"Mappings per Set ({total} total across {len(rows)} sets)",
            description="\n".join(lines),
            colour=discord.Colour.blurple(),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # /catalog_product_ids  — list all mapped Product IDs
    # ------------------------------------------------------------------

    @app_commands.command(
        name="catalog_product_ids",
        description="[Admin] List all mapped Cardmarket Product IDs",
    )
    @_ADMIN_ONLY
    async def list_catalog_product_ids(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        rows = await self.db.get_all_catalog_id_slugs()
        product_rows = [r for r in rows if r.get("id_product")]

        if not product_rows:
            await interaction.followup.send("No mapped Product IDs found yet.", ephemeral=True)
            return

        lines = [
            f"`[{r['id']}]` `{r['id_product']}` — `{r['product_slug'] or '—'}`"
            for r in product_rows[:20]
        ]
        if len(product_rows) > 20:
            lines.append(f"… and {len(product_rows) - 20} more")

        embed = discord.Embed(
            title=f"🃏 Mapped Product IDs ({len(product_rows)} total)",
            colour=discord.Colour.green(),
        )
        embed.add_field(name="Row ID · Product ID · Slug", value="\n".join(lines), inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # /catalog_expansion_ids  — list all mapped Expansion IDs
    # ------------------------------------------------------------------

    @app_commands.command(
        name="catalog_expansion_ids",
        description="[Admin] List all mapped Cardmarket Expansion IDs",
    )
    @_ADMIN_ONLY
    async def list_catalog_expansion_ids(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        rows = await self.db.get_all_catalog_id_slugs()
        expansion_rows = [r for r in rows if r.get("id_expansion")]

        if not expansion_rows:
            await interaction.followup.send("No mapped Expansion IDs found yet.", ephemeral=True)
            return

        all_lines = [
            f"`[{r['id']}]` `{r['id_expansion']}` — `{r['set_slug'] or '—'}` ×{r.get('match_count', 1)}"
            for r in expansion_rows
        ]

        # Split lines into chunks that fit within Discord's embed field value limit.
        _FIELD_LIMIT = 1024
        chunks: list[list[str]] = []
        current: list[str] = []
        current_len = 0
        for line in all_lines:
            # +1 for the newline separator between lines
            needed = len(line) + (1 if current else 0)
            if current and current_len + needed > _FIELD_LIMIT:
                chunks.append(current)
                current = [line]
                current_len = len(line)
            else:
                current.append(line)
                current_len += needed
        if current:
            chunks.append(current)

        total = len(expansion_rows)
        for i, chunk in enumerate(chunks):
            page_info = f" (page {i + 1}/{len(chunks)})" if len(chunks) > 1 else ""
            embed = discord.Embed(
                title=f"📦 Mapped Expansion IDs ({total} total){page_info}",
                colour=discord.Colour.blue(),
            )
            embed.add_field(name="Row ID · Expansion ID · Slug", value="\n".join(chunk), inline=False)
            await interaction.followup.send(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # /delete_catalog_id  — remove a catalog ID mapping by row ID
    # ------------------------------------------------------------------

    @app_commands.command(
        name="delete_catalog_id",
        description="[Admin] Delete a catalog ID mapping by its row ID",
    )
    @app_commands.describe(row_id="The row ID of the catalog mapping to delete (shown in catalog_product_ids / catalog_expansion_ids)")
    @_ADMIN_ONLY
    async def delete_catalog_id(
        self, interaction: discord.Interaction, row_id: int
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        deleted = await self.db.delete_catalog_id_slug(row_id)
        if deleted:
            await interaction.followup.send(
                f"✅ Catalog ID mapping row `{row_id}` deleted.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"⚠️ No catalog ID mapping with row ID `{row_id}` found.",
                ephemeral=True,
            )

    # ------------------------------------------------------------------
    # /catalog_low_confidence  — show low-confidence expansion ID mappings
    # ------------------------------------------------------------------

    @app_commands.command(
        name="catalog_low_confidence",
        description="[Admin] Show expansion ID mappings with low confidence (multiple IDs per slug)",
    )
    @_ADMIN_ONLY
    async def catalog_low_confidence(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=False)
        rows = await self.db.get_low_confidence_expansion_slugs()

        # Only surface rows where a slug is claimed by more than one expansion ID.
        conflict_rows = [r for r in rows if r.get("id_count_for_slug", 1) > 1]

        if not conflict_rows:
            await interaction.followup.send(
                "✅ No conflicting expansion ID mappings found. All slugs are mapped by a single ID.",
            )
            return

        lines: list[str] = []
        for r in conflict_rows[:25]:
            pct = int(r["confidence"] * 100)
            lines.append(
                f"`[{r['id']}]` `{r['id_expansion']}` → `{r['set_slug']}` "
                f"×{r['match_count']} / {r['total_for_slug']} ({pct}%)"
            )
        if len(conflict_rows) > 25:
            lines.append(f"… and {len(conflict_rows) - 25} more")

        embed = discord.Embed(
            title=f"⚠️ Low-confidence expansion ID mappings ({len(conflict_rows)} rows)",
            description=(
                "These slugs are claimed by more than one expansion ID.\n"
                "React with 🗑️ to automatically purge the lowest-match-count row for each slug."
            ),
            colour=discord.Colour.orange(),
        )
        embed.add_field(
            name="Row ID · Expansion ID · Slug · Matches (% of total)",
            value="\n".join(lines),
            inline=False,
        )

        msg = await interaction.followup.send(embed=embed, wait=True)
        # wait=True makes followup.send return the sent Message so we can track its ID.
        if msg is not None:
            try:
                await msg.add_reaction("🗑️")
                self._pending_purge_messages[msg.id] = True
            except discord.HTTPException:
                pass

    # ------------------------------------------------------------------
    # Reaction listener – 🗑️ on low-confidence message triggers purge
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        """Purge lowest-match-count expansion rows when admin reacts with 🗑️."""
        if str(payload.emoji) != "🗑️":
            return
        if payload.message_id not in self._pending_purge_messages:
            return
        if payload.user_id == self.bot.user.id:  # type: ignore[union-attr]
            return

        # Verify the reacting user has administrator permissions.
        guild = self.bot.get_guild(payload.guild_id)  # type: ignore[arg-type]
        if guild is None:
            return
        member = guild.get_member(payload.user_id)
        if member is None or not member.guild_permissions.administrator:
            return

        del self._pending_purge_messages[payload.message_id]

        deleted = await self.db.purge_lowest_count_expansion_rows()
        channel = self.bot.get_channel(payload.channel_id)
        if channel is None:
            return

        if not deleted:
            await channel.send("ℹ️ No rows were purged (no conflicting slug mappings found).")  # type: ignore[union-attr]
            return

        summary_lines = [
            f"`[{r['id']}]` `{r['id_expansion']}` → `{r['set_slug']}` ×{r['match_count']}"
            for r in deleted
        ]
        embed = discord.Embed(
            title=f"🗑️ Purged {len(deleted)} low-confidence expansion mapping(s)",
            description="\n".join(summary_lines[:25]),
            colour=discord.Colour.red(),
        )
        await channel.send(embed=embed)  # type: ignore[union-attr]

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
