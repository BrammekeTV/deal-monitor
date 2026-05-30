"""
bot/cogs/test_cardmarket.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~
Slash command ``/test-cardmarket`` — verifies that the Cardmarket API
integration is working by performing a live test search via the TCGGO client.

Usage::

    /test-cardmarket [query]

When *query* is omitted the command searches for ``Charizard ex`` as a known
reference card.  The result (or any error) is reported back to the user so the
integration can be diagnosed without restarting the bot.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger(__name__)


class TestCardmarketCog(commands.Cog, name="test_cardmarket"):
    """Provides the /test-cardmarket slash command."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    def _get_monitor(self):
        """Return the MonitorCog instance to reuse its TCGGO client."""
        return self.bot.cogs.get("Monitor")

    @app_commands.command(
        name="test-cardmarket",
        description=(
            "Test the Cardmarket API integration. "
            "Performs a live search and reports the result."
        ),
    )
    @app_commands.describe(
        query="Card name to look up (default: 'Charizard ex')",
    )
    async def test_cardmarket(
        self, interaction: discord.Interaction, query: str = "Charizard ex"
    ) -> None:
        await interaction.response.defer(thinking=True)

        monitor = self._get_monitor()

        # Check that the client and HTTP session are available.
        if monitor is None or monitor._http is None:
            await interaction.followup.send(
                "⚠️ The monitor is not running yet – please wait a moment and try again.",
                ephemeral=True,
            )
            return

        if monitor._tcggo_client is None:
            await interaction.followup.send(
                "❌ **Cardmarket API not configured.**\n\n"
                "The `RAPIDAPI_KEY` environment variable is not set.\n"
                "Obtain a key at <https://rapidapi.com> and subscribe to the "
                "`cardmarket-api-tcg` API, then add it to your `.env` file:\n"
                "```\nRAPIDAPI_KEY=your_key_here\n```",
                ephemeral=True,
            )
            return

        tcggo = monitor._tcggo_client
        http = monitor._http

        result = None
        error_msg: str | None = None
        try:
            result = await tcggo.search_card(http, listing_title=query)
        except Exception as exc:  # noqa: BLE001
            logger.warning("test_cardmarket: lookup failed for %r: %s", query, exc)
            error_msg = str(exc)

        if error_msg:
            await interaction.followup.send(
                f"❌ **API call failed for** `{discord.utils.escape_markdown(query)}`\n"
                f"```\n{error_msg[:1800]}\n```",
            )
            return

        if result is None:
            await interaction.followup.send(
                f"⚠️ **No results** for `{discord.utils.escape_markdown(query)}`.\n\n"
                "The API is reachable but returned no matching card.\n"
                "Try a different search term or check that your subscription "
                "includes the `/pokemon/cards/search` endpoint.",
            )
            return

        # Build a summary embed showing the API result.
        confidence_colour = {
            "High": discord.Colour.green(),
            "Medium": discord.Colour.yellow(),
            "Low": discord.Colour.orange(),
        }.get(result.confidence, discord.Colour.greyple())

        embed = discord.Embed(
            title=f"✅ Cardmarket API — test result for: {discord.utils.escape_markdown(query)}",
            colour=confidence_colour,
        )

        # Card identity
        id_parts: list[str] = []
        if result.card_name:
            id_parts.append(f"**Name:** {result.card_name}")
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

        # Confidence
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
            price_lines.append(f"💡 **Suggested:** €{result.suggested_price:.2f}")
        for platform, price in result.alt_prices.items():
            if isinstance(price, (int, float)) and price > 0:
                price_lines.append(f"🌐 **{platform}:** €{price:.2f}")

        if price_lines:
            embed.add_field(
                name="Cardmarket Prices", value="\n".join(price_lines), inline=False
            )
        else:
            embed.add_field(
                name="Cardmarket Prices",
                value=(
                    "No pricing data in this result.\n"
                    "The API is reachable but returned no price fields for this card."
                ),
                inline=False,
            )

        best = result.best_market_value()
        if best is not None:
            embed.add_field(
                name="Best Market Value",
                value=f"€{best:.2f}",
                inline=True,
            )

        if result.cardmarket_url:
            embed.add_field(
                name="Cardmarket",
                value=f"[View listing]({result.cardmarket_url})",
                inline=False,
            )

        embed.set_footer(text="Prices via Cardmarket API (cardmarket-api-tcg)")

        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TestCardmarketCog(bot))
