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


def _confidence_emoji(confidence: str) -> str:
    return {"High": "🟢", "Medium": "🟡", "Low": "🔴"}.get(confidence, "⚪")


def build_listing_embed(
    listing: Listing,
    price_results: list["PriceResult"] | None = None,
) -> discord.Embed:
    """Create a Discord Embed from a scored Listing.

    Optional *price_results* from live eBay/Cardmarket lookups are shown
    as comparison fields so users can judge the deal at a glance.

    For bulk lots the embed highlights estimated card count and price per
    card rather than (or in addition to) the deal score.
    """
    colour = settings.embed_colour

    if listing.is_bulk_lot:
        label = "📦 Bulk Lot"
    else:
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

    # Bulk lot specifics: card count + price per card
    if listing.is_bulk_lot:
        if listing.estimated_card_count is not None:
            embed.add_field(
                name="🃏 Est. Card Count",
                value=str(listing.estimated_card_count),
                inline=True,
            )
        if listing.price_per_card is not None:
            embed.add_field(
                name="💳 Price / Card",
                value=f"€{listing.price_per_card:.4f}",
                inline=True,
            )

    # Estimated market value + discount (individual cards)
    if not listing.is_bulk_lot and listing.estimated_market_value:
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
            if result.platform == "Cardmarket" and any(
                v is not None
                for v in (
                    result.from_price,
                    result.price_trend,
                    result.avg_30_days,
                    result.avg_7_days,
                    result.avg_1_day,
                )
            ):
                # Rich Cardmarket block: show each metric individually.
                lines: list[str] = [f"[Product page]({result.search_url})"]
                if result.from_price:
                    lines.append(f"**From:** €{result.from_price:.2f}")
                if result.price_trend:
                    disc = (
                        (1 - listing.price / result.price_trend) * 100
                        if listing.price > 0 else 0.0
                    )
                    trend_str = f"€{result.price_trend:.2f}"
                    if disc > 0:
                        trend_str += f"  ✅ **{disc:.1f}% cheaper**"
                    elif disc < 0:
                        trend_str += f"  ⚠️ {abs(disc):.1f}% above"
                    lines.append(f"**Price Trend:** {trend_str}")
                if result.avg_30_days:
                    disc30 = (
                        (1 - listing.price / result.avg_30_days) * 100
                        if listing.price > 0 else 0.0
                    )
                    avg30_str = f"€{result.avg_30_days:.2f}"
                    if disc30 > 0:
                        avg30_str += f"  ✅ **{disc30:.1f}% cheaper**"
                    elif disc30 < 0:
                        avg30_str += f"  ⚠️ {abs(disc30):.1f}% above"
                    lines.append(f"**30-day Avg:** {avg30_str}")
                if result.avg_7_days:
                    lines.append(f"**7-day Avg:** €{result.avg_7_days:.2f}")
                if result.avg_1_day:
                    lines.append(f"**1-day Avg:** €{result.avg_1_day:.2f}")
                embed.add_field(
                    name="🃏 Cardmarket Prices",
                    value="\n".join(lines),
                    inline=False,
                )
            else:
                # Generic block for eBay or Cardmarket without named metrics.
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

    # Deal score (only meaningful for individual card listings)
    if not listing.is_bulk_lot:
        embed.add_field(
            name="⭐ Deal Score",
            value=f"{bar}  **{listing.score}/100**",
            inline=False,
        )

    # Confidence level + explanation
    conf_emoji = _confidence_emoji(listing.confidence)
    embed.add_field(
        name=f"{conf_emoji} Confidence",
        value=listing.confidence,
        inline=True,
    )
    if listing.valuation_explanation:
        embed.add_field(
            name="📝 Valuation Notes",
            value=listing.valuation_explanation[:1024],
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


def build_review_embed(
    listing: Listing,
    candidates: list[dict] | None = None,
    failure_reason: str | None = None,
) -> discord.Embed:
    """Embed for an unidentified listing posted to the #card-review channel.

    Community members can react or reply with reference URLs to help identify
    the listing.
    """
    embed = discord.Embed(
        title=f"🔍 Unidentified Listing: {listing.title}",
        url=listing.url,
        colour=0xFFA500,
        timestamp=datetime.now(timezone.utc),
        description=(
            "This listing could not be automatically identified.\n"
            "Reply to this message with a reference URL (Cardmarket, eBay, "
            "PriceCharting, or TCGPlayer) to help match it.\n\n"
            "✅ Correct Match  ❌ Incorrect Match  🔍 Needs More Research"
        ),
    )

    embed.add_field(
        name="💰 Asking Price",
        value=f"**{listing.price:.2f} {listing.currency}**",
        inline=True,
    )

    conf_emoji = _confidence_emoji(listing.confidence)
    embed.add_field(
        name=f"{conf_emoji} Confidence",
        value=listing.confidence,
        inline=True,
    )

    reason = failure_reason or listing.valuation_explanation or "No market data found."
    embed.add_field(
        name="❓ Why Failed",
        value=reason[:1024],
        inline=False,
    )

    if listing.description:
        embed.add_field(
            name="📝 Description",
            value=listing.description[:512],
            inline=False,
        )

    if listing.ocr_text:
        embed.add_field(
            name="🔤 OCR Results",
            value=listing.ocr_text[:512],
            inline=False,
        )

    if candidates:
        lines = []
        for c in candidates[:3]:
            val = f"€{c['market_value']:.2f}" if c.get("market_value") else "price unknown"
            ref = c.get("reference_url") or ""
            lines.append(f"• **{c['card_name']}** ({val}) — [reference]({ref})" if ref else f"• **{c['card_name']}** ({val})")
        embed.add_field(
            name="📚 Memory Matches",
            value="\n".join(lines),
            inline=False,
        )

    if listing.images:
        embed.set_image(url=listing.images[0])

    seller_text = listing.seller_name or "Unknown"
    embed.set_footer(text=f"Vinted  •  ID {listing.listing_id}  •  Seller: {seller_text}")
    return embed


def build_reference_confirmation_embed(
    unidentified: dict,
    reference_url: str,
    platform: str,
    submitted_by: discord.User | discord.Member,
) -> discord.Embed:
    """Embed posted by the bot when a user submits a reference URL."""
    embed = discord.Embed(
        title=f"🔗 Reference Submitted: {platform}",
        colour=0x5865F2,
        timestamp=datetime.now(timezone.utc),
        description=(
            f"**{submitted_by.display_name}** submitted a reference for:\n"
            f"**{unidentified['title']}**\n\n"
            f"React ✅ to **approve** this match or ❌ to **reject** it."
        ),
    )
    embed.add_field(name="📎 Reference URL", value=reference_url[:1024], inline=False)
    embed.add_field(
        name="💰 Listing Price",
        value=f"{unidentified['price']:.2f} {unidentified.get('currency', 'EUR')}",
        inline=True,
    )
    embed.add_field(name="🏪 Platform", value=platform, inline=True)
    embed.set_footer(text=f"Listing ID: {unidentified['id']}")
    return embed


def build_deal_escalation_embed(
    unidentified: dict,
    market_value: float,
    reference_url: str,
) -> discord.Embed:
    """Embed posted to the main deals channel after a manual review finds a deal."""
    price = unidentified["price"]
    currency = unidentified.get("currency", "EUR")
    discount_pct = (1 - price / market_value) * 100 if market_value > 0 else 0.0
    profit_margin = market_value - price

    embed = discord.Embed(
        title="🔥 Manual Review Found a Deal",
        url=unidentified["url"],
        colour=0xFF4500,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="🃏 Card", value=unidentified["title"], inline=False)
    embed.add_field(
        name="📊 Market Value",
        value=f"€{market_value:.2f}",
        inline=True,
    )
    embed.add_field(
        name="💰 Listing Price",
        value=f"{price:.2f} {currency}",
        inline=True,
    )
    embed.add_field(
        name="📉 Discount",
        value=f"{discount_pct:.1f}%",
        inline=True,
    )
    embed.add_field(
        name="💹 Profit Margin",
        value=f"€{profit_margin:.2f}",
        inline=True,
    )
    embed.add_field(name="🏪 Source", value="Vinted (via community review)", inline=True)
    embed.add_field(name="🔗 Reference", value=reference_url[:1024], inline=False)
    embed.add_field(
        name="🛒 Original Listing",
        value=unidentified["url"],
        inline=False,
    )

    # Show first image if available
    try:
        import json
        images = json.loads(unidentified.get("images") or "[]")
        if images:
            embed.set_thumbnail(url=images[0])
    except (ValueError, TypeError):
        pass

    embed.set_footer(text="Identified through community review")
    return embed


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
