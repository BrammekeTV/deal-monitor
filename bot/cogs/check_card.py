"""
bot/cogs/check_card.py
~~~~~~~~~~~~~~~~~~~~~~
Slash command ``/check-card`` — look up a Pokémon card by name or Cardmarket
URL and display all available pricing data from the TCGGO API.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from config.settings import settings

logger = logging.getLogger(__name__)


class CheckCardCog(commands.Cog, name="check_card"):
    """Provides the /check-card slash command."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_monitor(self):
        """Return the MonitorCog instance so we can reuse its TCGGO client."""
        return self.bot.cogs.get("Monitor")

    # ------------------------------------------------------------------
    # Command
    # ------------------------------------------------------------------

    @app_commands.command(
        name="check-card",
        description="Look up a card's market prices via TCGGO. Accepts a card name or a Cardmarket URL.",
    )
    @app_commands.describe(
        query="Card name, Cardmarket URL, or set + collector number (e.g. 'Charizard sv3pt5 006')",
    )
    async def check_card(self, interaction: discord.Interaction, query: str) -> None:
        await interaction.response.defer(thinking=True)

        monitor = self._get_monitor()
        if monitor is None or monitor._tcggo_client is None or monitor._http is None:
            await interaction.followup.send(
                "⚠️ The Cardmarket API client is not available right now.  "
                "Make sure `RAPIDAPI_KEY` is set in the bot configuration.",
                ephemeral=True,
            )
            return

        tcggo = monitor._tcggo_client
        http = monitor._http

        # Decide whether the query looks like a URL.
        is_url = query.startswith("http://") or query.startswith("https://")

        result = None
        try:
            if is_url:
                result = await tcggo.lookup_by_url(http, query)
            else:
                result = await tcggo.search_card(http, listing_title=query)
        except Exception as exc:  # noqa: BLE001
            logger.warning("check_card: TCGGO lookup failed for %r: %s", query, exc)

        if result is None:
            embed = discord.Embed(
                title="Card Not Found",
                description=(
                    f"No card matching **{discord.utils.escape_markdown(query)}** "
                    "could be found in the TCGGO database."
                ),
                colour=discord.Colour.red(),
            )
            await interaction.followup.send(embed=embed)
            return

        # Build the response embed.
        confidence_colour = {
            "High": discord.Colour.green(),
            "Medium": discord.Colour.yellow(),
            "Low": discord.Colour.orange(),
        }.get(result.confidence, discord.Colour.greyple())

        embed = discord.Embed(
            title=result.card_name or query,
            colour=confidence_colour,
        )

        # Card identification
        id_parts: list[str] = []
        if result.set_name:
            id_parts.append(f"**Set:** {result.set_name}")
        if result.set_code:
            id_parts.append(f"**Set code:** {result.set_code}")
        if result.collector_number:
            id_parts.append(f"**#:** {result.collector_number}")
        if result.language:
            id_parts.append(f"**Language:** {result.language}")
        if result.rarity:
            id_parts.append(f"**Rarity:** {result.rarity}")
        if id_parts:
            embed.add_field(name="Card Info", value="\n".join(id_parts), inline=False)

        # Confidence indicator
        confidence_emoji = {"High": "🟢", "Medium": "🟡", "Low": "🔴"}.get(
            result.confidence, "⚪"
        )
        embed.add_field(
            name="Match Confidence",
            value=f"{confidence_emoji} **{result.confidence}** (score: {result.confidence_score})",
            inline=False,
        )

        # Pricing
        price_lines: list[str] = []
        if result.price_trend is not None:
            price_lines.append(f"📈 **Trend Price:** €{result.price_trend:.2f}")
        if result.market_price is not None:
            price_lines.append(f"💹 **Market Price:** €{result.market_price:.2f}")
        if result.avg_price is not None:
            price_lines.append(f"📊 **Average Price:** €{result.avg_price:.2f}")
        if result.avg_30_days is not None:
            price_lines.append(f"📅 **30-day Avg:** €{result.avg_30_days:.2f}")
        if result.avg_7_days is not None:
            price_lines.append(f"📅 **7-day Avg:** €{result.avg_7_days:.2f}")
        if result.avg_1_day is not None:
            price_lines.append(f"📅 **1-day Avg:** €{result.avg_1_day:.2f}")
        if result.low_price is not None:
            price_lines.append(f"⬇️ **Low Price:** €{result.low_price:.2f}")
        if result.suggested_price is not None:
            price_lines.append(f"💡 **Suggested Price:** €{result.suggested_price:.2f}")

        # Alternative marketplace prices
        if result.alt_prices:
            for platform, price in result.alt_prices.items():
                if isinstance(price, (int, float)) and price > 0:
                    price_lines.append(f"🌐 **{platform}:** €{price:.2f}")

        if price_lines:
            embed.add_field(
                name="Cardmarket Prices", value="\n".join(price_lines), inline=False
            )
        else:
            embed.add_field(
                name="Cardmarket Prices", value="No pricing data available.", inline=False
            )

        # Best market value summary
        best = result.best_market_value()
        if best is not None:
            lo, avg, hi = result.cm_low(), result.cm_average(), result.cm_high()
            range_parts = []
            if lo is not None and lo != best:
                range_parts.append(f"Low: €{lo:.2f}")
            if avg is not None and avg != best:
                range_parts.append(f"Avg: €{avg:.2f}")
            if hi is not None and hi != best:
                range_parts.append(f"High: €{hi:.2f}")
            summary = f"**Best value:** €{best:.2f}"
            if range_parts:
                summary += f"  •  {', '.join(range_parts)}"
            embed.add_field(name="Market Summary", value=summary, inline=False)

        # Link back to Cardmarket if we have one
        if result.cardmarket_url:
            embed.add_field(
                name="Cardmarket",
                value=f"[View listing]({result.cardmarket_url})",
                inline=False,
            )

        embed.set_footer(text="Prices via TCGGO / Cardmarket API")

        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CheckCardCog(bot))
