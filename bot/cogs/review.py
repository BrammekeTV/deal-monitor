"""
bot/cogs/review.py
~~~~~~~~~~~~~~~~~~
Community review workflow for unidentified listings.

Workflow:
  1. MonitorCog calls ``ReviewCog.post_for_review()`` when a listing cannot
     be automatically identified (score = 0, confidence = Low).
  2. The bot posts a review embed to the configured #card-review channel and
     adds ✅ / ❌ / 🔍 reactions.
  3. Community members can reply to the review message with a reference URL
     from Cardmarket, eBay, PriceCharting, or TCGPlayer.
  4. The bot **immediately auto-approves** the reference, stores it in the
     identification_memory table, and posts a confirmation embed that includes
     scraped Cardmarket prices from the reference URL.
  5. If the listing price is sufficiently below the supplied market value the
     bot immediately posts a 🔥 deal alert to the main deals channel.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from config.settings import settings
from database.db import Database
from utils.embed_builder import (
    build_deal_escalation_embed,
    build_reference_confirmation_embed,
    build_review_embed,
)
from utils.logger import get_logger

if TYPE_CHECKING:
    from bot.cogs.monitor import MonitorCog
    from scraper.base import Listing

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Reaction emojis used in the review workflow
# ---------------------------------------------------------------------------
_REACT_CORRECT = "✅"
_REACT_INCORRECT = "❌"
_REACT_RESEARCH = "🔍"

# ---------------------------------------------------------------------------
# Supported reference URL platforms
# ---------------------------------------------------------------------------
_PLATFORM_PATTERNS: list[tuple[str, str]] = [
    ("cardmarket.com", "Cardmarket"),
    ("ebay.", "eBay"),
    ("pricecharting.com", "PriceCharting"),
    ("tcgplayer.com", "TCGPlayer"),
]

# Regex for extracting URLs from message text
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

# Regex for extracting a price amount from message text
# Matches "€84.50", "$35", "£20.99", "84.50€", "84,50 EUR", etc.
_PRICE_RE = re.compile(
    r"(?:[€$£]\s*(\d+(?:[.,]\d+)?))"
    r"|(?:(\d+(?:[.,]\d+)?)\s*[€$£])"
    r"|(?:(\d+(?:[.,]\d+)?)\s*(?:EUR|USD|GBP|euro))",
    re.IGNORECASE,
)


def _detect_platform(url: str) -> str | None:
    """Return the platform name for a URL, or None if unrecognised."""
    url_lower = url.lower()
    for fragment, name in _PLATFORM_PATTERNS:
        if fragment in url_lower:
            return name
    return None


def _extract_urls(text: str) -> list[str]:
    """Return all HTTP(S) URLs found in *text*."""
    return _URL_RE.findall(text)


def _extract_price(text: str) -> float | None:
    """Return the first price amount found in *text*, or None."""
    m = _PRICE_RE.search(text)
    if not m:
        return None
    raw = m.group(1) or m.group(2) or m.group(3)
    if raw:
        try:
            return float(raw.replace(",", "."))
        except ValueError:
            pass
    return None


class ReviewCog(commands.Cog, name="Review"):
    """Handles community review of unidentified listings."""

    def __init__(self, bot: commands.Bot, db: Database) -> None:
        self.bot = bot
        self.db = db

    def _get_monitor(self) -> "MonitorCog | None":
        return self.bot.cogs.get("Monitor")  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Public API used by MonitorCog
    # ------------------------------------------------------------------

    async def post_for_review(
        self,
        listing: "Listing",
        failure_reason: str | None = None,
    ) -> None:
        """Post *listing* to the #card-review channel and store it in DB.

        Checks identification memory for candidate matches first and
        includes them in the embed if any are found.
        """
        channel = self._get_review_channel()
        if channel is None:
            logger.debug(
                "Review channel not configured – skipping review post for %s",
                listing.listing_id,
            )
            return

        # Look for memory matches to show as candidates.
        candidates = await self.db.find_in_memory(listing.title)

        embed = build_review_embed(
            listing,
            candidates=candidates or None,
            failure_reason=failure_reason,
        )

        try:
            msg = await channel.send(embed=embed)
        except discord.HTTPException as exc:
            logger.error("Failed to post review embed for %s: %s", listing.listing_id, exc)
            return

        # Add community reaction prompts.
        for emoji in (_REACT_CORRECT, _REACT_INCORRECT, _REACT_RESEARCH):
            try:
                await msg.add_reaction(emoji)
            except discord.HTTPException:
                pass

        # Persist the unidentified listing and the review message ID.
        await self.db.add_unidentified_listing(
            listing_id=listing.listing_id,
            title=listing.title,
            url=listing.url,
            price=listing.price,
            currency=listing.currency,
            description=listing.description,
            images=listing.images,
            confidence=listing.confidence,
            failure_reason=failure_reason or listing.valuation_explanation,
            ocr_text=listing.ocr_text,
            review_message_id=str(msg.id),
        )

        logger.info(
            "Posted unidentified listing %s to review channel (msg %s)",
            listing.listing_id,
            msg.id,
        )

    # ------------------------------------------------------------------
    # Event listeners
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Detect reference URL submissions in the #card-review channel."""
        if message.author.bot:
            return

        review_channel_id = settings.discord_review_channel_id
        if not review_channel_id or message.channel.id != review_channel_id:
            return

        # Only process messages that are replies to another message.
        if message.reference is None:
            return

        referenced_id = str(message.reference.message_id)
        unidentified = await self.db.get_unidentified_by_message_id(referenced_id)
        if not unidentified:
            # Not a reply to a known review message – ignore.
            return

        if unidentified["status"] in ("identified", "dismissed"):
            # Already resolved.
            return

        urls = _extract_urls(message.content)
        if not urls:
            return

        for url in urls:
            platform = _detect_platform(url)
            if platform:
                market_value = _extract_price(message.content)
                await self._handle_reference_submission(
                    message=message,
                    unidentified=unidentified,
                    url=url,
                    platform=platform,
                    market_value=market_value,
                )
                # Only handle the first recognisable URL per message.
                break

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        """Handle reactions on original review messages (dismiss / needs-research)."""
        # Ignore the bot's own reactions.
        if payload.user_id == self.bot.user.id:  # type: ignore[union-attr]
            return

        emoji = str(payload.emoji)
        message_id = str(payload.message_id)

        # --- Reaction on an original review message ---
        unidentified = await self.db.get_unidentified_by_message_id(message_id)
        if unidentified:
            if emoji == _REACT_RESEARCH:
                await self.db.update_unidentified_status(
                    unidentified["id"], "research_needed"
                )
                logger.info(
                    "Listing %s marked as needs-more-research by user %s",
                    unidentified["id"],
                    payload.user_id,
                )
            elif emoji == _REACT_INCORRECT:
                await self.db.update_unidentified_status(unidentified["id"], "dismissed")
                logger.info(
                    "Listing %s dismissed by user %s",
                    unidentified["id"],
                    payload.user_id,
                )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _handle_reference_submission(
        self,
        message: discord.Message,
        unidentified: dict,
        url: str,
        platform: str,
        market_value: float | None,
    ) -> None:
        """Persist the reference, auto-approve it, and post a confirmation embed."""
        logger.info(
            "Reference submission from user %s for listing %s: platform=%s url=%s",
            message.author.id,
            unidentified["id"],
            platform,
            url,
        )

        # --- Fetch live price data from the submitted reference URL ----------
        site_prices: dict[str, float] = {}
        if platform == "Cardmarket":
            monitor = self._get_monitor()
            if monitor is not None:
                # Primary: use TCGGO API to fetch prices from the Cardmarket URL.
                if monitor._tcggo_client is not None and monitor._http is not None:
                    logger.debug(
                        "Fetching Cardmarket prices via TCGGO for reference URL: %s", url
                    )
                    try:
                        tcggo_result = await monitor._tcggo_client.lookup_by_url(
                            monitor._http, url
                        )
                        if tcggo_result:
                            logger.info(
                                "TCGGO returned prices for reference URL %s: trend=%.2f",
                                url,
                                tcggo_result.price_trend or 0,
                            )
                            # Map TcggoCardResult fields to the price dict format
                            # expected by the embed builder.
                            if tcggo_result.price_trend:
                                site_prices["price_trend"] = tcggo_result.price_trend
                            if tcggo_result.market_price:
                                site_prices["market_price"] = tcggo_result.market_price
                            if tcggo_result.low_price:
                                site_prices["lowest_price"] = tcggo_result.low_price
                            if tcggo_result.avg_30_days:
                                site_prices["avg_30_days"] = tcggo_result.avg_30_days
                            if tcggo_result.avg_7_days:
                                site_prices["avg_7_days"] = tcggo_result.avg_7_days
                            if tcggo_result.avg_1_day:
                                site_prices["avg_1_day"] = tcggo_result.avg_1_day
                            if market_value is None:
                                market_value = (
                                    site_prices.get("price_trend")
                                    or site_prices.get("market_price")
                                    or site_prices.get("avg_30_days")
                                    or site_prices.get("lowest_price")
                                )
                                logger.debug(
                                    "Auto-derived market value from TCGGO: %.2f",
                                    market_value or 0,
                                )
                        else:
                            logger.warning(
                                "TCGGO returned no result for reference URL: %s", url
                            )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "TCGGO lookup failed for reference URL %s: %s", url, exc,
                            exc_info=True,
                        )
                else:
                    logger.debug(
                        "TCGGO client not available; skipping price fetch for reference URL %s",
                        url,
                    )

                # Fallback: Playwright scraper (only when explicitly configured).
                if not site_prices and monitor._browser is not None:
                    logger.debug(
                        "Trying Playwright scraper fallback for reference URL: %s", url
                    )
                    try:
                        from scraper.cardmarket import CardmarketPriceScraper, normalize_cardmarket_url

                        normalized_url = normalize_cardmarket_url(url)
                        scraper = CardmarketPriceScraper(monitor._browser)
                        result = await scraper.lookup_url(normalized_url)
                        if result:
                            site_prices = result
                            logger.info(
                                "Playwright scraper returned prices for %s: %s",
                                normalized_url,
                                {k: v for k, v in result.items() if v},
                            )
                            if market_value is None:
                                market_value = (
                                    site_prices.get("price_trend")
                                    or site_prices.get("avg_30_days")
                                    or site_prices.get("lowest_price")
                                )
                        else:
                            logger.warning(
                                "Playwright scraper returned no prices for %s", normalized_url
                            )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "Could not fetch Cardmarket prices for reference URL %s: %s",
                            url,
                            exc,
                            exc_info=True,
                        )
        else:
            logger.debug(
                "Platform %s: skipping Cardmarket price fetch for reference URL %s",
                platform,
                url,
            )

        # Persist the reference submission.
        ref_id = await self.db.add_reference_submission(
            listing_id=unidentified["id"],
            submitted_by=str(message.author.id),
            reference_url=url,
            platform=platform,
            market_value=market_value,
        )
        logger.debug("Saved reference submission %d for listing %s", ref_id, unidentified["id"])

        # --- Auto-approve immediately (no reaction required) ---
        ref = {
            "id": ref_id,
            "listing_id": unidentified["id"],
            "reference_url": url,
            "platform": platform,
            "market_value": market_value,
        }
        await self._approve_reference(ref, approved_by=str(message.author.id))

        # Post one confirmation embed that includes the scraped prices.
        embed = build_reference_confirmation_embed(
            unidentified=unidentified,
            reference_url=url,
            platform=platform,
            submitted_by=message.author,
            site_prices=site_prices or None,
            market_value=market_value,
        )

        try:
            await message.channel.send(embed=embed)
        except discord.HTTPException as exc:
            logger.error("Failed to post confirmation embed: %s", exc)
            return

        logger.info(
            "Auto-approved reference %d from user %s for listing %s "
            "(platform=%s value=%s prices_found=%s)",
            ref_id,
            message.author.id,
            unidentified["id"],
            platform,
            market_value,
            bool(site_prices),
        )

    async def _approve_reference(
        self,
        ref: dict,
        approved_by: str,
    ) -> None:
        """Approve a reference submission: update DB, store memory, escalate deal."""
        listing_id: str = ref["listing_id"]
        reference_url: str = ref["reference_url"]
        platform: str = ref.get("platform") or "Unknown"
        market_value: float | None = ref.get("market_value")

        logger.info(
            "Auto-approving reference %d for listing %s (platform=%s value=%s) by user %s",
            ref["id"],
            listing_id,
            platform,
            market_value,
            approved_by,
        )

        await self.db.update_reference_validated(ref["id"], 1)
        await self.db.update_unidentified_status(listing_id, "identified")

        unidentified = await self.db.get_unidentified_listing(listing_id)
        if not unidentified:
            logger.warning(
                "Approved reference %d but listing %s not found in DB",
                ref["id"],
                listing_id,
            )
            return

        # --- Store in identification memory ---
        # Normalise Cardmarket URLs to include standard filter params so that
        # future direct-URL lookups use the correct seller/language filters.
        stored_url = reference_url
        if platform == "Cardmarket":
            from scraper.cardmarket import normalize_cardmarket_url
            stored_url = normalize_cardmarket_url(reference_url)

        await self.db.add_to_memory(
            title_pattern=unidentified["title"],
            card_name=unidentified["title"],
            reference_url=stored_url,
            market_value=market_value,
            source_listing_id=listing_id,
            approved_by=approved_by,
        )

        logger.info(
            "Reference %d approved by user %s: listing %s identified via %s (value=%s)",
            ref["id"],
            approved_by,
            listing_id,
            platform,
            market_value,
        )

        # --- Deal escalation ---
        if market_value is not None and market_value > 0:
            price = unidentified["price"]
            if price < market_value:
                discount_pct = (1 - price / market_value) * 100
                if discount_pct >= settings.discount_threshold_pct:
                    await self._escalate_deal(
                        unidentified=unidentified,
                        market_value=market_value,
                        reference_url=reference_url,
                    )
                else:
                    logger.debug(
                        "Listing %s identified but discount %.1f%% < threshold %.1f%% – no escalation",
                        listing_id,
                        discount_pct,
                        settings.discount_threshold_pct,
                    )
        else:
            logger.debug(
                "Listing %s identified but no market value provided – skipping escalation",
                listing_id,
            )

    async def _escalate_deal(
        self,
        unidentified: dict,
        market_value: float,
        reference_url: str,
    ) -> None:
        """Post a deal alert to the main deals channel."""
        deals_channel = self._get_deals_channel()
        if deals_channel is None:
            logger.warning(
                "Deals channel not configured – cannot escalate listing %s",
                unidentified["id"],
            )
            return

        embed = build_deal_escalation_embed(
            unidentified=unidentified,
            market_value=market_value,
            reference_url=reference_url,
        )

        try:
            await deals_channel.send(embed=embed)
            logger.info(
                "Escalated listing %s to deals channel (market=%.2f price=%.2f)",
                unidentified["id"],
                market_value,
                unidentified["price"],
            )
        except discord.HTTPException as exc:
            logger.error("Failed to escalate deal for %s: %s", unidentified["id"], exc)

    def _get_review_channel(self) -> discord.TextChannel | None:
        channel_id = settings.discord_review_channel_id
        if not channel_id:
            return None
        channel = self.bot.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            logger.warning(
                "Review channel %d not found or not a text channel", channel_id
            )
            return None
        return channel

    def _get_deals_channel(self) -> discord.TextChannel | None:
        channel_id = settings.discord_channel_id
        if not channel_id:
            return None
        channel = self.bot.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            logger.warning(
                "Deals channel %d not found or not a text channel", channel_id
            )
            return None
        return channel
