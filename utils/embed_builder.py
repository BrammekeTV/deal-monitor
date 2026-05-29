"""
utils/embed_builder.py
~~~~~~~~~~~~~~~~~~~~~~
Builds discord.py Embed objects for Vinted deal listings.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

import discord

from config.settings import settings
from scraper.base import Listing

if TYPE_CHECKING:
    from utils.price_lookup import PriceResult


def _score_bar(score: int) -> str:
    """Return an emoji bar representing the deal score (0–100)."""
    filled = score // 10
    bar = "🟩" * filled + "⬛" * (10 - filled)
    return bar


def _deal_label(score: int) -> str:
    if score >= 80:
        return "🔥 HOT DEAL"
    if score >= 60:
        return "✅ Great Deal"
    if score >= 40:
        return "👍 Good Deal"
    return "ℹ️ Listing"


def build_listing_embed(
    listing: Listing,
    price_results: list["PriceResult"] | None = None,
) -> discord.Embed:
    """Create a Discord Embed from a scored Listing.

    Optional *price_results* from live eBay/Cardmarket lookups are shown
    as comparison fields so users can judge the deal at a glance.
    """
    colour = settings.embed_colour
    label = _deal_label(listing.score)
    bar = _score_bar(listing.score)

    embed = discord.Embed(
        title=f"{label}: {listing.title}",
        url=listing.url,
        colour=colour,
        timestamp=datetime.now(timezone.utc),
    )

    # Price field
    embed.add_field(
        name="💰 Price",
        value=f"**{listing.price:.2f} {listing.currency}**",
        inline=True,
    )

    # Estimated market value + discount
    if listing.estimated_market_value:
        emv = listing.estimated_market_value
        embed.add_field(
            name="📊 Est. Market Value",
            value=f"{emv:.2f} {listing.currency}",
            inline=True,
        )
        discount = listing.discount_pct
        if discount is not None and discount > 0:
            embed.add_field(
                name="📉 Discount",
                value=f"{discount:.1f}% below market",
                inline=True,
            )

    # Live price comparisons from eBay / Cardmarket
    if price_results:
        for result in price_results:
            discount_text = ""
            if result.avg_price > 0 and listing.price > 0:
                diff_pct = (1 - listing.price / result.avg_price) * 100
                if diff_pct > 0:
                    discount_text = f"\n✅ **{diff_pct:.1f}% cheaper** than avg"
                elif diff_pct < 0:
                    discount_text = f"\n⚠️ {abs(diff_pct):.1f}% above avg"
            embed.add_field(
                name=f"🔎 {result.platform} (avg of {result.sample_count} sold)",
                value=(
                    f"[Search results]({result.search_url})\n"
                    f"Avg: **{result.avg_price:.2f} {result.currency}**  "
                    f"| Low: {result.min_price:.2f}  "
                    f"| High: {result.max_price:.2f}"
                    f"{discount_text}"
                ),
                inline=False,
            )

    # Deal score
    embed.add_field(
        name="⭐ Deal Score",
        value=f"{bar}  **{listing.score}/100**",
        inline=False,
    )

    # Seller info
    seller_text = listing.seller_name or "Unknown"
    if listing.seller_rating is not None:
        stars = "⭐" * round(listing.seller_rating)
        seller_text += f" {stars} ({listing.seller_rating:.1f}/5)"
    embed.add_field(name="👤 Seller", value=seller_text, inline=True)

    # Condition / location
    if listing.condition:
        embed.add_field(name="📦 Condition", value=listing.condition, inline=True)
    if listing.location:
        embed.add_field(name="📍 Location", value=listing.location, inline=True)

    # Thumbnail
    if listing.thumbnail:
        embed.set_thumbnail(url=listing.thumbnail)

    # Footer
    embed.set_footer(text=f"Vinted  •  ID {listing.listing_id}")

    return embed


def build_error_embed(title: str, description: str) -> discord.Embed:
    return discord.Embed(title=title, description=description, colour=0xFF4444)


def build_status_embed(
    *,
    listings_checked: int,
    listings_posted: int,
    last_run: datetime | None,
    next_run: datetime | None,
    search_terms: list[str],
) -> discord.Embed:
    """Embed summarising the bot's current status."""
    embed = discord.Embed(title="📡 Deal Monitor Status", colour=0x5865F2)
    embed.add_field(name="Listings checked", value=str(listings_checked), inline=True)
    embed.add_field(name="Deals posted", value=str(listings_posted), inline=True)
    embed.add_field(
        name="Last run",
        value=discord.utils.format_dt(last_run, "R") if last_run else "Never",
        inline=True,
    )
    embed.add_field(
        name="Next run",
        value=discord.utils.format_dt(next_run, "R") if next_run else "Unknown",
        inline=True,
    )
    embed.add_field(
        name="Search terms",
        value=", ".join(f"`{t}`" for t in search_terms) or "None",
        inline=False,
    )
    embed.timestamp = datetime.now(timezone.utc)
    return embed
