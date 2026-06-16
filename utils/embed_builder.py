"""
utils/embed_builder.py
~~~~~~~~~~~~~~~~~~~~~~
Builds Discord Embed objects for the deal-monitor bot.

Embeds:
  build_profit_alert_embed      – profitable Vinted vs Cardmarket listing
  build_not_profitable_embed    – non-profitable result (used after manual review)
  build_review_embed            – listing sent to review queue (Cardmarket unknown)
  build_review_resolved_embed   – confirmation after user supplies Cardmarket URL
  build_error_embed             – Cardmarket scraping / processing error
  build_status_embed            – bot health status
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from scraper.base import Listing
    from scraper.cardmarket import CardmarketPriceData
    from services.card_identifier import CardFingerprint
    from services.price_comparison import ComparisonResult


# ---------------------------------------------------------------------------
# Colour palette
# ---------------------------------------------------------------------------
_COLOUR_PROFIT = 0x00FF7F   # bright green – profitable listing
_COLOUR_REVIEW = 0xFFA500   # orange – needs review
_COLOUR_ERROR  = 0xFF4444   # red – error
_COLOUR_INFO   = 0x5865F2   # blurple – status / info
_COLOUR_NEUTRAL = 0x808080  # grey – not profitable (used after manual review)


# ---------------------------------------------------------------------------
# Profit alert embed
# ---------------------------------------------------------------------------

def build_profit_alert_embed(
    listing: "Listing",
    cm_data: "CardmarketPriceData",
    comparison: "ComparisonResult",
    *,
    match_confidence: float | None = None,
    match_source: str | None = None,
    fingerprint: "CardFingerprint | None" = None,
) -> discord.Embed:
    """Build a rich embed for a profitable Vinted vs Cardmarket listing."""
    embed = discord.Embed(
        title=f"🔥 Profitable Listing: {listing.title[:200]}",
        url=listing.url,
        colour=_COLOUR_PROFIT,
        timestamp=datetime.now(timezone.utc),
    )

    # ── Vinted information ────────────────────────────────────────────────
    embed.add_field(
        name="🛍️ Vinted Listing",
        value=(
            f"**Title:** {listing.title[:200]}\n"
            f"**URL:** [View on Vinted]({listing.url})\n"
            f"**Price:** €{listing.price:.2f} {listing.currency}\n"
            f"**Seller:** {listing.seller_name or 'Unknown'}"
        ),
        inline=False,
    )

    # ── Cardmarket information ────────────────────────────────────────────
    cm_lines = [
        f"**Product:** {cm_data.product_name or 'Unknown'}",
        f"**URL:** [View on Cardmarket]({cm_data.product_url})",
    ]
    if cm_data.product_id:
        cm_lines.append(f"**idProduct:** `{cm_data.product_id}`")
    if cm_data.id_expansion:
        cm_lines.append(f"**idExpansion:** `{cm_data.id_expansion}`")
    cm_lines.append(f"**From Price:** €{cm_data.from_price:.2f}")
    if cm_data.price_trend:
        cm_lines.append(f"**Price Trend:** €{cm_data.price_trend:.2f}")
    if cm_data.avg_30_days:
        cm_lines.append(f"**30-Day Avg:** €{cm_data.avg_30_days:.2f}")
    if cm_data.avg_7_days:
        cm_lines.append(f"**7-Day Avg:** €{cm_data.avg_7_days:.2f}")
    if cm_data.avg_1_day:
        cm_lines.append(f"**1-Day Avg:** €{cm_data.avg_1_day:.2f}")

    embed.add_field(
        name="🃏 Cardmarket",
        value="\n".join(cm_lines),
        inline=False,
    )

    # ── Extracted card info ───────────────────────────────────────────────
    if fingerprint:
        fp_lines = []
        if fingerprint.card_name:
            fp_lines.append(f"Card: **{fingerprint.card_name}**")
        if fingerprint.set_name:
            fp_lines.append(f"Set: **{fingerprint.set_name}**")
        if fingerprint.set_code:
            fp_lines.append(f"Set Code: **{fingerprint.set_code}**")
        if fingerprint.collector_number:
            fp_lines.append(f"Number: **{fingerprint.collector_number}**")
        if fingerprint.condition:
            fp_lines.append(f"Condition: **{fingerprint.condition}**")
        if fingerprint.rarity:
            fp_lines.append(f"Rarity: **{fingerprint.rarity}**")
        if fingerprint.language:
            fp_lines.append(f"Language: **{fingerprint.language}**")
        if fingerprint.grade_authority:
            fp_lines.append(f"Grade: **{fingerprint.grade_authority} {fingerprint.grade_value}**")
        if fp_lines:
            embed.add_field(
                name="🔎 Card Info",
                value="\n".join(fp_lines),
                inline=False,
            )

    # ── Comparison ────────────────────────────────────────────────────────
    embed.add_field(
        name="📊 Price Comparison",
        value=(
            f"**Vinted Item Price:** €{comparison.vinted_price:.2f}\n"
            f"**Protection Fee:** €{comparison.protection_fee:.2f} "
            f"(5% + €0.70)\n"
            f"**Shipping:** €{comparison.shipping_min:.2f} – €{comparison.shipping_max:.2f}\n"
            f"**Total Cost:** €{comparison.total_cost_min:.2f} – €{comparison.total_cost_max:.2f}\n"
            f"**Cardmarket From:** €{comparison.cardmarket_from_price:.2f}\n"
            f"**Profit (worst case):** €{comparison.absolute_difference:.2f} "
            f"({comparison.percentage_difference:.1f}%)"
        ),
        inline=False,
    )

    # ── Additional information ────────────────────────────────────────────
    extra_lines = []
    dutch_status = "✅ Dutch sellers available" if comparison.dutch_sellers_available else "⚠️ No Dutch sellers – global pricing used"
    extra_lines.append(dutch_status)
    if match_confidence is not None:
        extra_lines.append(f"**Match Confidence:** {match_confidence:.0%}")
    if match_source:
        source_labels = {
            "database": "📚 Learned mapping",
            "constructed": "🔧 Auto-constructed URL",
            "manual": "👤 User-supplied URL",
        }
        extra_lines.append(f"**Match Source:** {source_labels.get(match_source, match_source)}")

    embed.add_field(
        name="ℹ️ Additional Info",
        value="\n".join(extra_lines),
        inline=False,
    )

    if listing.thumbnail:
        embed.set_thumbnail(url=listing.thumbnail)

    embed.set_footer(text=f"Vinted ID: {listing.listing_id}")
    return embed


# ---------------------------------------------------------------------------
# Not-profitable result embed (after manual review)
# ---------------------------------------------------------------------------

def build_not_profitable_embed(
    listing: "Listing",
    cm_data: "CardmarketPriceData",
    comparison: "ComparisonResult",
    *,
    fingerprint: "CardFingerprint | None" = None,
    bid_price: float | None = None,
) -> discord.Embed:
    """Build an embed shown when Vinted price >= Cardmarket From price.

    When *bid_price* is provided and would be profitable, an additional field
    is added suggesting the buyer bid at that price.
    """
    embed = discord.Embed(
        title=f"📉 Not Profitable: {listing.title[:200]}",
        url=listing.url,
        colour=_COLOUR_NEUTRAL,
        timestamp=datetime.now(timezone.utc),
        description=(
            "The Vinted listing price is **at or above** the current "
            "Cardmarket From price. No profit opportunity detected."
        ),
    )

    embed.add_field(
        name="💰 Prices",
        value=(
            f"**Vinted Item Price:** €{comparison.vinted_price:.2f}\n"
            f"**Protection Fee:** €{comparison.protection_fee:.2f} (5% + €0.70)\n"
            f"**Shipping:** €{comparison.shipping_min:.2f} – €{comparison.shipping_max:.2f}\n"
            f"**Total Cost:** €{comparison.total_cost_min:.2f} – €{comparison.total_cost_max:.2f}\n"
            f"**Cardmarket From:** €{comparison.cardmarket_from_price:.2f}"
        ),
        inline=True,
    )

    cm_detail_lines = []
    if cm_data.product_id:
        cm_detail_lines.append(f"idProduct: `{cm_data.product_id}`")
    if cm_data.id_expansion:
        cm_detail_lines.append(f"idExpansion: `{cm_data.id_expansion}`")
    if cm_data.price_trend:
        cm_detail_lines.append(f"Price Trend: €{cm_data.price_trend:.2f}")
    if cm_data.avg_30_days:
        cm_detail_lines.append(f"30-Day Avg: €{cm_data.avg_30_days:.2f}")
    if cm_data.avg_7_days:
        cm_detail_lines.append(f"7-Day Avg: €{cm_data.avg_7_days:.2f}")
    if cm_data.avg_1_day:
        cm_detail_lines.append(f"1-Day Avg: €{cm_data.avg_1_day:.2f}")
    if cm_detail_lines:
        embed.add_field(
            name="🃏 Cardmarket Pricing",
            value="\n".join(cm_detail_lines),
            inline=True,
        )

    if not comparison.dutch_sellers_available:
        embed.add_field(
            name="⚠️ Note",
            value="No Dutch sellers available – global pricing was used.",
            inline=False,
        )

    # ── 80 % bid suggestion ────────────────────────────────────────────────
    if bid_price is not None:
        from services.price_comparison import calculate_vinted_total_cost, VINTED_SHIPPING_MAX
        bid_total_max = calculate_vinted_total_cost(bid_price, VINTED_SHIPPING_MAX)
        if comparison.cardmarket_from_price > 0 and bid_total_max < comparison.cardmarket_from_price:
            saving = round(comparison.cardmarket_from_price - bid_total_max, 2)
            embed.add_field(
                name="💡 Bid suggestion",
                value=(
                    f"Bidding at **€{bid_price:.2f}** (80% of asking price) "
                    f"would be **profitable** — total cost €{bid_total_max:.2f} vs "
                    f"Cardmarket €{comparison.cardmarket_from_price:.2f} "
                    f"(saving **€{saving:.2f}**)."
                ),
                inline=False,
            )

    # ── Extracted card info ───────────────────────────────────────────────
    if fingerprint:
        fp_lines = []
        if fingerprint.card_name:
            fp_lines.append(f"Card: **{fingerprint.card_name}**")
        if fingerprint.set_name:
            fp_lines.append(f"Set: **{fingerprint.set_name}**")
        if fingerprint.set_code:
            fp_lines.append(f"Set Code: **{fingerprint.set_code}**")
        if fingerprint.collector_number:
            fp_lines.append(f"Number: **{fingerprint.collector_number}**")
        if fingerprint.condition:
            fp_lines.append(f"Condition: **{fingerprint.condition}**")
        if fingerprint.rarity:
            fp_lines.append(f"Rarity: **{fingerprint.rarity}**")
        if fingerprint.language:
            fp_lines.append(f"Language: **{fingerprint.language}**")
        if fingerprint.grade_authority:
            fp_lines.append(f"Grade: **{fingerprint.grade_authority} {fingerprint.grade_value}**")
        if fp_lines:
            embed.add_field(
                name="🔎 Card Info",
                value="\n".join(fp_lines),
                inline=False,
            )

    embed.add_field(
        name="🔗 Cardmarket",
        value=f"[View product]({cm_data.product_url})",
        inline=False,
    )

    if listing.thumbnail:
        embed.set_thumbnail(url=listing.thumbnail)
    embed.set_footer(text=f"Vinted ID: {listing.listing_id}")
    return embed


# ---------------------------------------------------------------------------
# Bulk lot alert embed
# ---------------------------------------------------------------------------

def build_bulk_alert_embed(
    listing: "Listing",
    card_count: int,
    price_per_card: float,
) -> discord.Embed:
    """Build an embed for a bulk card lot that meets the price-per-card threshold."""
    embed = discord.Embed(
        title=f"📦 Bulk Lot Deal: {listing.title[:200]}",
        url=listing.url,
        colour=_COLOUR_PROFIT,
        timestamp=datetime.now(timezone.utc),
        description=(
            f"Bulk lot at **€{price_per_card:.4f} per card** "
            f"(≤ €0.01 threshold)."
        ),
    )
    embed.add_field(
        name="💰 Pricing",
        value=(
            f"**Listing Price:** €{listing.price:.2f}\n"
            f"**Estimated Cards:** {card_count}\n"
            f"**Price per Card:** €{price_per_card:.4f}"
        ),
        inline=True,
    )
    if listing.seller_name:
        embed.add_field(
            name="👤 Seller",
            value=listing.seller_name,
            inline=True,
        )
    if listing.thumbnail:
        embed.set_thumbnail(url=listing.thumbnail)
    embed.set_footer(text=f"Vinted ID: {listing.listing_id}")
    return embed


# ---------------------------------------------------------------------------
# Review queue embed
# ---------------------------------------------------------------------------

def build_review_embed(
    listing: "Listing",
    fingerprint: "CardFingerprint | None" = None,
    failure_reason: str | None = None,
    matching_attempts: list[dict] | None = None,
    id_expansion: int | None = None,
) -> discord.Embed:
    """Build an embed for a listing that could not be automatically identified.

    Users should reply to this message with the correct Cardmarket product URL.
    """
    embed = discord.Embed(
        title=f"🔍 Needs Review: {listing.title[:200]}",
        url=listing.url,
        colour=_COLOUR_REVIEW,
        timestamp=datetime.now(timezone.utc),
        description=(
            "This Vinted listing could **not** be automatically matched to a "
            "Cardmarket product.\n\n"
            "**Reply to this message with the correct Cardmarket product URL** "
            "to identify the card. The bot will validate the URL, scrape pricing "
            "data, and save the mapping for future listings.\n\n"
            "Example: `https://www.cardmarket.com/en/Pokemon/Products/Singles/...`"
        ),
    )

    # ── Vinted data ───────────────────────────────────────────────────────
    embed.add_field(
        name="🛍️ Vinted Listing",
        value=(
            f"**Title:** {listing.title[:200]}\n"
            f"**Price:** €{listing.price:.2f} {listing.currency}\n"
            f"**Seller:** {listing.seller_name or 'Unknown'}\n"
            f"**URL:** [View listing]({listing.url})"
        ),
        inline=False,
    )

    # ── Extracted fingerprint ─────────────────────────────────────────────
    if fingerprint:
        def _val(v: object) -> str:
            return f"**{v}**" if v else "—"

        grade_str = (
            f"**{fingerprint.grade_authority} {fingerprint.grade_value}**"
            if fingerprint.grade_authority
            else "—"
        )
        fp_lines = [
            f"Card Name: {_val(fingerprint.card_name)}",
            f"Set: {_val(fingerprint.set_name)}",
        ]
        if id_expansion is not None:
            fp_lines.append(f"idExpansion: **`{id_expansion}`**")
        fp_lines += [
            f"Set Code: {_val(fingerprint.set_code)}",
            f"Number: {_val(fingerprint.collector_number)}",
            f"Condition: {_val(fingerprint.condition)}",
            f"Rarity: {_val(fingerprint.rarity)}",
            f"Language: {_val(fingerprint.language)}",
            f"Grade: {grade_str}",
        ]
        embed.add_field(
            name="🔎 Extracted Card Info",
            value="\n".join(fp_lines),
            inline=False,
        )

    # ── Failure reason ────────────────────────────────────────────────────
    if failure_reason:
        embed.add_field(
            name="❓ Why Matching Failed",
            value=failure_reason[:512],
            inline=False,
        )

    # ── Matching attempts ─────────────────────────────────────────────────
    if matching_attempts:
        attempt_lines = []
        for attempt in matching_attempts[:5]:
            method = attempt.get("method", "unknown")
            result = attempt.get("result", "failed")
            detail = attempt.get("detail", "")
            attempt_lines.append(f"• **{method}**: {result}" + (f" – {detail}" if detail else ""))
        embed.add_field(
            name="🔄 Matching Attempts",
            value="\n".join(attempt_lines),
            inline=False,
        )

    # ── Description snippet ───────────────────────────────────────────────
    if listing.description:
        embed.add_field(
            name="📝 Listing Description",
            value=listing.description[:512],
            inline=False,
        )

    if listing.images:
        embed.set_image(url=listing.images[0])

    embed.set_footer(text=f"Vinted ID: {listing.listing_id}  •  Reply with Cardmarket URL to identify")
    return embed


# ---------------------------------------------------------------------------
# Unidentified listing embed (not found in catalog or learning DB)
# ---------------------------------------------------------------------------

def build_unidentified_embed(
    listing: "Listing",
    fingerprint: "CardFingerprint | None" = None,
    id_expansion: int | None = None,
) -> discord.Embed:
    """Build an embed for a listing not found in the product catalog or learning DB.

    Users should reply with the correct Cardmarket product URL or product ID
    so the bot can identify the card, scrape pricing, and learn the mapping.
    """
    embed = discord.Embed(
        title=f"❓ Unidentified Listing: {listing.title[:200]}",
        url=listing.url,
        colour=_COLOUR_REVIEW,
        timestamp=datetime.now(timezone.utc),
        description=(
            "This listing was **not found** in the Cardmarket product catalog "
            "or the learning database.\n\n"
            "**Reply to this message with the correct Cardmarket product URL** "
            "so the bot can identify the card, compare prices, and save the "
            "mapping for future runs.\n\n"
            "Example: `https://www.cardmarket.com/en/Pokemon/Products/Singles/...`"
        ),
    )

    # ── Vinted data ───────────────────────────────────────────────────────
    embed.add_field(
        name="🛍️ Vinted Listing",
        value=(
            f"**Title:** {listing.title[:200]}\n"
            f"**Price:** €{listing.price:.2f} {listing.currency}\n"
            f"**Seller:** {listing.seller_name or 'Unknown'}\n"
            f"**URL:** [View listing]({listing.url})"
        ),
        inline=False,
    )

    # ── Extracted fingerprint ─────────────────────────────────────────────
    if fingerprint:
        def _val(v: object) -> str:
            return f"**{v}**" if v else "—"

        fp_lines = [
            f"Card Name: {_val(fingerprint.card_name)}",
            f"Set: {_val(fingerprint.set_name)}",
        ]
        if id_expansion is not None:
            fp_lines.append(f"idExpansion: **`{id_expansion}`**")
        fp_lines += [
            f"Set Code: {_val(fingerprint.set_code)}",
            f"Number: {_val(fingerprint.collector_number)}",
            f"Condition: {_val(fingerprint.condition)}",
            f"Rarity: {_val(fingerprint.rarity)}",
            f"Language: {_val(fingerprint.language)}",
        ]
        embed.add_field(
            name="🔎 Extracted Card Info",
            value="\n".join(fp_lines),
            inline=False,
        )

    # ── Description snippet ───────────────────────────────────────────────
    if listing.description:
        embed.add_field(
            name="📝 Listing Description",
            value=listing.description[:512],
            inline=False,
        )

    if listing.images:
        embed.set_image(url=listing.images[0])

    embed.set_footer(
        text=f"Vinted ID: {listing.listing_id}  •  Reply with Cardmarket URL to identify"
    )
    return embed


# ---------------------------------------------------------------------------

def build_review_resolved_embed(
    listing_title: str,
    listing_url: str,
    listing_price: float,
    listing_currency: str,
    cm_data: "CardmarketPriceData",
    comparison: "ComparisonResult",
    resolved_by: str,
    fingerprint: "CardFingerprint | None" = None,
) -> discord.Embed:
    """Build a confirmation embed after a user supplies the correct Cardmarket URL."""
    colour = _COLOUR_PROFIT if comparison.is_profitable else _COLOUR_NEUTRAL
    profit_indicator = "✅ **Profitable!**" if comparison.is_profitable else "📉 Not profitable"

    embed = discord.Embed(
        title=f"🔗 Review Resolved: {listing_title[:200]}",
        colour=colour,
        timestamp=datetime.now(timezone.utc),
        description=(
            f"**{resolved_by}** provided the Cardmarket URL for this listing.\n"
            f"The mapping has been saved for future automatic matching.\n\n"
            f"{profit_indicator}"
        ),
    )

    embed.add_field(
        name="🛍️ Vinted",
        value=(
            f"**Title:** {listing_title[:200]}\n"
            f"**Price:** €{listing_price:.2f} {listing_currency}\n"
            f"**URL:** [View listing]({listing_url})"
        ),
        inline=False,
    )

    cm_lines = [
        f"**Product:** {cm_data.product_name or 'Unknown'}",
    ]
    if fingerprint and fingerprint.condition:
        cm_lines.append(f"**Condition filter:** {fingerprint.condition}")
    cm_lines.extend([
        f"**URL:** [View on Cardmarket]({cm_data.product_url})",
        f"**From Price:** €{cm_data.from_price:.2f}",
    ])
    if cm_data.price_trend:
        cm_lines.append(f"**Price Trend:** €{cm_data.price_trend:.2f}")
    if cm_data.avg_30_days:
        cm_lines.append(f"**30-Day Avg:** €{cm_data.avg_30_days:.2f}")
    if cm_data.avg_7_days:
        cm_lines.append(f"**7-Day Avg:** €{cm_data.avg_7_days:.2f}")
    if cm_data.avg_1_day:
        cm_lines.append(f"**1-Day Avg:** €{cm_data.avg_1_day:.2f}")

    embed.add_field(
        name="🃏 Cardmarket",
        value="\n".join(cm_lines),
        inline=False,
    )

    embed.add_field(
        name="📊 Comparison",
        value=(
            f"**Vinted Item Price:** €{comparison.vinted_price:.2f}\n"
            f"**Protection Fee:** €{comparison.protection_fee:.2f} (5% + €0.70)\n"
            f"**Shipping:** €{comparison.shipping_min:.2f} – €{comparison.shipping_max:.2f}\n"
            f"**Total Cost:** €{comparison.total_cost_min:.2f} – €{comparison.total_cost_max:.2f}\n"
            f"**Cardmarket From:** €{comparison.cardmarket_from_price:.2f}\n"
            f"**Difference (worst case):** €{comparison.absolute_difference:.2f} "
            f"({'saving' if comparison.is_profitable else 'overpaying'} "
            f"{comparison.percentage_difference:.1f}%)"
        ),
        inline=False,
    )

    if not comparison.dutch_sellers_available:
        embed.add_field(
            name="⚠️ Dutch Sellers",
            value="No Dutch sellers available – global pricing was used.",
            inline=False,
        )

    embed.set_footer(text="Mapping saved to learning database")
    return embed


# ---------------------------------------------------------------------------
# Error embed
# ---------------------------------------------------------------------------

def build_error_embed(
    listing_title: str | None,
    listing_url: str | None,
    cardmarket_url: str | None,
    failure_step: str,
    error_message: str,
    *,
    http_status: int | None = None,
    stack_trace: str | None = None,
    fingerprint: "CardFingerprint | None" = None,
    id_expansion: int | None = None,
) -> discord.Embed:
    """Build a structured error embed for the logging channel.

    When *fingerprint* is provided and the failure is related to Cardmarket URL
    generation, the embed includes extracted card info and instructions for the
    user to reply with the correct Cardmarket URL.
    """
    embed = discord.Embed(
        title=f"⚠️ Cardmarket Scrape Error: {failure_step}",
        colour=_COLOUR_ERROR,
        timestamp=datetime.now(timezone.utc),
    )

    if listing_title:
        embed.add_field(name="📋 Listing", value=listing_title[:200], inline=False)
    if listing_url:
        embed.add_field(name="🔗 Vinted URL", value=listing_url[:500], inline=False)
    if cardmarket_url:
        embed.add_field(name="🃏 Generated Cardmarket URL", value=cardmarket_url[:500], inline=False)

    embed.add_field(name="❌ Error", value=error_message[:1024], inline=False)

    if http_status:
        embed.add_field(name="🌐 HTTP Status", value=str(http_status), inline=True)

    # ── Extracted card info ───────────────────────────────────────────────
    if fingerprint:
        def _val(v: object) -> str:
            return f"**{v}**" if v else "—"

        grade_str = (
            f"**{fingerprint.grade_authority} {fingerprint.grade_value}**"
            if fingerprint.grade_authority
            else "—"
        )
        fp_lines = [
            f"Card Name: {_val(fingerprint.card_name)}",
            f"Set: {_val(fingerprint.set_name)}",
        ]
        if id_expansion is not None:
            fp_lines.append(f"idExpansion: **`{id_expansion}`**")
        fp_lines += [
            f"Set Code: {_val(fingerprint.set_code)}",
            f"Collector Number: {_val(fingerprint.collector_number)}",
            f"Condition: {_val(fingerprint.condition)}",
            f"Rarity: {_val(fingerprint.rarity)}",
            f"Language: {_val(fingerprint.language)}",
            f"Grade: {grade_str}",
        ]
        embed.add_field(
            name="🔎 Extracted Card Info",
            value="\n".join(fp_lines),
            inline=False,
        )

    # ── Correction instructions ───────────────────────────────────────────
    if cardmarket_url:
        embed.add_field(
            name="💡 How to Fix",
            value=(
                "**Reply to this message** with the correct Cardmarket product URL.\n"
                "The bot will validate it, scrape the pricing data, and permanently "
                "learn the correct URL for this card.\n\n"
                "Example: `https://www.cardmarket.com/en/Pokemon/Products/Singles/...`"
            ),
            inline=False,
        )

    if stack_trace:
        # Truncate to fit Discord's embed field limit
        truncated = stack_trace[-900:]
        embed.add_field(
            name="📄 Stack Trace (last 900 chars)",
            value=f"```\n{truncated}\n```",
            inline=False,
        )

    embed.set_footer(text=f"Step: {failure_step}  •  Reply with correct Cardmarket URL to fix")
    return embed


# ---------------------------------------------------------------------------
# Check-listing embed  (on-demand /check_listing command)
# ---------------------------------------------------------------------------

# Human-readable labels for filter reasons.
_FILTER_REASON_LABELS: dict[str, str] = {
    "non_card_item": "🚫 Non-card item – this listing appears to be merchandise, accessories, or other non-TCG content.",
    "lot_listing": "📦 Lot listing – this listing contains multiple cards and is not processed as a single-card deal.",
    "graded_listing": "🏅 Graded listing – graded cards (PSA/BGS/CGC etc.) are not compared against raw Cardmarket prices.",
    "japanese_listing": "🇯🇵 Japanese listing – Japanese-language cards are outside the scope of this bot.",
    "price_out_of_range": "💶 Price out of range – the listing price falls outside the configured min/max filter.",
    "no_pokemon_name": "🔤 No Pokémon name detected – no recognisable Pokémon name was found in the title.",
    "unresolvable_no_identifiers": "🔍 Unresolvable – no set code, set name, or collector number could be extracted from the title.",
    "unidentified": "❓ Unidentified – no matching Cardmarket product was found in the catalog or the learning database.",
}


def build_check_listing_embed(
    listing: "Listing",
    *,
    fingerprint: "CardFingerprint | None" = None,
    cm_data: "CardmarketPriceData | None" = None,
    comparison: "ComparisonResult | None" = None,
    match_confidence: float | None = None,
    match_source: str | None = None,
    filter_reason: str | None = None,
    filter_detail: str | None = None,
    cm_url: str | None = None,
    cm_error: str | None = None,
) -> discord.Embed:
    """Build a comprehensive analysis embed for the /check_listing command.

    Handles every possible pipeline outcome in a single function:
    - Filter rejection (non-card, lot, graded, Japanese, price, no-pokemon-name)
    - Unresolvable / unidentified card
    - Cardmarket scrape error (URL known, price unavailable)
    - Full result with profitability data
    """
    # ── Determine colour and headline ────────────────────────────────────
    if filter_reason:
        colour = _COLOUR_REVIEW
        headline = "🔍 Check Result"
    elif cm_data is None:
        colour = _COLOUR_NEUTRAL
        headline = "🔍 Check Result"
    elif comparison is not None and comparison.is_profitable:
        colour = _COLOUR_PROFIT
        headline = "🔍 Check Result – ✅ Profitable"
    else:
        colour = _COLOUR_NEUTRAL
        headline = "🔍 Check Result – 📉 Not Profitable"

    embed = discord.Embed(
        title=f"{headline}: {listing.title[:180]}",
        url=listing.url,
        colour=colour,
        timestamp=datetime.now(timezone.utc),
    )

    # ── Vinted listing details ────────────────────────────────────────────
    embed.add_field(
        name="🛍️ Vinted Listing",
        value=(
            f"**Title:** {listing.title[:200]}\n"
            f"**Price:** €{listing.price:.2f} {listing.currency}\n"
            f"**Seller:** {listing.seller_name or 'Unknown'}\n"
            f"**URL:** [View on Vinted]({listing.url})"
        ),
        inline=False,
    )

    # ── Filter / skip reason ──────────────────────────────────────────────
    if filter_reason:
        reason_text = _FILTER_REASON_LABELS.get(filter_reason, filter_reason)
        if filter_detail:
            reason_text = f"{reason_text}\n{filter_detail}"
        embed.add_field(name="⚠️ Result", value=reason_text, inline=False)

    # ── Extracted card fingerprint ────────────────────────────────────────
    if fingerprint:
        def _val(v: object) -> str:
            return f"**{v}**" if v else "—"

        fp_lines = [
            f"Card Name: {_val(fingerprint.card_name)}",
            f"Set: {_val(fingerprint.set_name)}",
            f"Set Code: {_val(fingerprint.set_code)}",
            f"Number: {_val(fingerprint.collector_number)}",
        ]
        if fingerprint.condition:
            fp_lines.append(f"Condition: {_val(fingerprint.condition)}")
        if fingerprint.rarity:
            fp_lines.append(f"Rarity: {_val(fingerprint.rarity)}")
        if fingerprint.language:
            fp_lines.append(f"Language: {_val(fingerprint.language)}")
        if fingerprint.grade_authority:
            fp_lines.append(f"Grade: **{fingerprint.grade_authority} {fingerprint.grade_value}**")
        embed.add_field(
            name="🔎 Identified Card Info",
            value="\n".join(fp_lines),
            inline=False,
        )

    # ── Cardmarket data ───────────────────────────────────────────────────
    if cm_data is not None:
        cm_lines = [
            f"**Product:** {cm_data.product_name or 'Unknown'}",
            f"**URL:** [View on Cardmarket]({cm_data.product_url})",
        ]
        if cm_data.product_id:
            cm_lines.append(f"**idProduct:** `{cm_data.product_id}`")
        if cm_data.id_expansion:
            cm_lines.append(f"**idExpansion:** `{cm_data.id_expansion}`")
        cm_lines.append(f"**From Price:** €{cm_data.from_price:.2f}")
        if cm_data.price_trend:
            cm_lines.append(f"**Price Trend:** €{cm_data.price_trend:.2f}")
        if cm_data.avg_30_days:
            cm_lines.append(f"**30-Day Avg:** €{cm_data.avg_30_days:.2f}")
        if cm_data.avg_7_days:
            cm_lines.append(f"**7-Day Avg:** €{cm_data.avg_7_days:.2f}")
        if cm_data.avg_1_day:
            cm_lines.append(f"**1-Day Avg:** €{cm_data.avg_1_day:.2f}")
        embed.add_field(name="🃏 Cardmarket", value="\n".join(cm_lines), inline=False)
    elif cm_url:
        # We have the CM URL but could not fetch prices (scrape error or DB-only hit)
        cm_lines = [f"**URL:** [View on Cardmarket]({cm_url})"]
        if cm_error:
            cm_lines.append(f"**Scrape Error:** {cm_error[:200]}")
        embed.add_field(name="🃏 Cardmarket (URL only)", value="\n".join(cm_lines), inline=False)

    # ── Price comparison ──────────────────────────────────────────────────
    if comparison is not None:
        profit_marker = "✅ **Profitable**" if comparison.is_profitable else "📉 **Not Profitable**"
        embed.add_field(
            name=f"📊 Price Comparison  ·  {profit_marker}",
            value=(
                f"**Vinted Item Price:** €{comparison.vinted_price:.2f}\n"
                f"**Protection Fee:** €{comparison.protection_fee:.2f} (5% + €0.70)\n"
                f"**Shipping:** €{comparison.shipping_min:.2f} – €{comparison.shipping_max:.2f}\n"
                f"**Total Cost:** €{comparison.total_cost_min:.2f} – €{comparison.total_cost_max:.2f}\n"
                f"**Cardmarket From:** €{comparison.cardmarket_from_price:.2f}\n"
                f"**Difference (worst case):** €{comparison.absolute_difference:.2f} "
                f"({'saving' if comparison.is_profitable else 'overpaying'} "
                f"{comparison.percentage_difference:.1f}%)"
            ),
            inline=False,
        )

    # ── Match metadata ────────────────────────────────────────────────────
    if match_confidence is not None or match_source is not None:
        meta_lines = []
        if match_confidence is not None:
            meta_lines.append(f"**Confidence:** {match_confidence:.0%}")
        if match_source:
            source_labels = {
                "catalog": "📂 Cardmarket Product Catalog",
                "database": "📚 Learned mapping",
                "constructed": "🔧 Auto-constructed URL",
                "manual": "👤 User-supplied URL",
            }
            meta_lines.append(f"**Source:** {source_labels.get(match_source, match_source)}")
        if comparison is not None:
            dutch = "✅ Dutch sellers available" if comparison.dutch_sellers_available else "⚠️ No Dutch sellers – global pricing used"
            meta_lines.append(dutch)
        embed.add_field(name="ℹ️ Match Info", value="\n".join(meta_lines), inline=False)

    if listing.thumbnail:
        embed.set_thumbnail(url=listing.thumbnail)

    embed.set_footer(text=f"Vinted ID: {listing.listing_id}  •  /check_listing on-demand analysis")
    return embed


# ---------------------------------------------------------------------------
# Status embed
# ---------------------------------------------------------------------------

def build_status_embed(
    *,
    listings_checked: int,
    listings_profitable: int,
    listings_reviewed: int,
    mappings_count: int,
    last_run: datetime | None,
    next_run: datetime | None,
    search_terms: list[str],
    paused: bool = False,
) -> discord.Embed:
    """Build a bot health/status embed."""
    embed = discord.Embed(
        title="📡 Deal Monitor Status",
        colour=_COLOUR_INFO,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(
        name="⏸️ Status",
        value="**Paused**" if paused else "**Running**",
        inline=True,
    )
    embed.add_field(name="📊 Listings Checked", value=str(listings_checked), inline=True)
    embed.add_field(name="🔥 Profitable Found", value=str(listings_profitable), inline=True)
    embed.add_field(name="🔍 Sent to Review", value=str(listings_reviewed), inline=True)
    embed.add_field(name="📚 Learned Mappings", value=str(mappings_count), inline=True)
    embed.add_field(
        name="🕐 Last Run",
        value=discord.utils.format_dt(last_run, "R") if last_run else "Never",
        inline=True,
    )
    embed.add_field(
        name="⏭️ Next Run",
        value=discord.utils.format_dt(next_run, "R") if next_run else "Unknown",
        inline=True,
    )
    embed.add_field(
        name="🔎 Search Terms",
        value=", ".join(f"`{t}`" for t in search_terms) or "None",
        inline=False,
    )
    return embed
