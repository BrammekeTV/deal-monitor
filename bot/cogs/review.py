"""
bot/cogs/review.py
~~~~~~~~~~~~~~~~~~
Manual Review Learning System.

Workflow:
  1. MonitorCog posts listings it cannot resolve to the #review-listings channel.
  2. A user replies to that message with the correct Cardmarket product URL.
  3. This cog detects the reply, validates the URL, normalises it with
     ``?sellerCountry=23&language=1``, scrapes Cardmarket pricing data, runs
     price comparison, and posts the result.
  4. The validated mapping is permanently stored in the learning database so
     future listings matching the same card are resolved automatically.

Message format the bot looks for in replies:
  Any reply to a review message that contains a cardmarket.com URL.
"""

from __future__ import annotations

import re
import traceback
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from config.settings import settings
from database.db import Database
from scraper.cardmarket import CardmarketScrapeError, normalize_cardmarket_url
from services.card_identifier import identify_card
from utils.embed_builder import (
    build_error_embed,
    build_review_resolved_embed,
)
from utils.logger import get_logger

if TYPE_CHECKING:
    from bot.cogs.monitor import MonitorCog

logger = get_logger(__name__)

# Regex to find Cardmarket URLs in message text.
_CM_URL_RE = re.compile(
    r"https?://(?:www\.)?cardmarket\.com/[^\s<>\"']+",
    re.IGNORECASE,
)


def _extract_cardmarket_url(text: str) -> str | None:
    """Return the first Cardmarket product URL found in *text*, or None."""
    m = _CM_URL_RE.search(text)
    if not m:
        return None
    url = m.group(0).rstrip(".,;)")  # Strip common trailing punctuation
    # Must be a product page (not a search / category page)
    if "/Products/Singles/" not in url:
        return None
    return url


class ReviewCog(commands.Cog, name="Review"):
    """Handles manual review replies to identify unresolved listings."""

    def __init__(self, bot: commands.Bot, db: Database) -> None:
        self.bot = bot
        self.db = db

    def _get_monitor(self) -> "MonitorCog | None":
        return self.bot.cogs.get("Monitor")  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Message listener
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Listen for Cardmarket URL replies in the review channel."""
        if message.author.bot:
            return

        review_channel_id = settings.discord_review_channel_id
        if not review_channel_id or message.channel.id != review_channel_id:
            return

        # Must be a reply to another message.
        if message.reference is None:
            return

        referenced_id = str(message.reference.message_id)

        # Look up the review queue item associated with this Discord message.
        review_item = await self.db.get_review_item_by_message(referenced_id)
        if review_item is None:
            return  # Not a reply to a known review message.

        # If already resolved, ignore subsequent replies.
        if review_item.get("status") == "resolved":
            return

        # Extract Cardmarket URL from the reply.
        cm_url = _extract_cardmarket_url(message.content)
        if not cm_url:
            await message.reply(
                "⚠️ No valid Cardmarket product URL found in your message.\n"
                "Please reply with a URL like: "
                "`https://www.cardmarket.com/en/Pokemon/Products/Singles/...`",
                mention_author=False,
            )
            return

        # Process the supplied URL.
        await self._process_review_reply(
            review_item=review_item,
            cardmarket_url=cm_url,
            submitted_by=message.author,
            reply_message=message,
        )

    # ------------------------------------------------------------------
    # Processing
    # ------------------------------------------------------------------

    async def _process_review_reply(
        self,
        review_item: dict,
        cardmarket_url: str,
        submitted_by: discord.Member | discord.User,
        reply_message: discord.Message,
    ) -> None:
        """Validate, scrape, compare, store and respond to a review reply."""
        listing_id = review_item["listing_id"]
        listing_title = review_item["title"]
        listing_url = review_item["url"]
        listing_price = float(review_item.get("price") or 0.0)
        listing_currency = review_item.get("currency") or "EUR"

        # Always normalise the URL (add sellerCountry=23&language=1).
        normalised_url = normalize_cardmarket_url(cardmarket_url)

        logger.info(
            "ReviewCog: processing URL '%s' for listing '%s' (submitted by %s)",
            normalised_url, listing_title[:60], submitted_by.display_name,
        )

        # ── Scrape Cardmarket ─────────────────────────────────────────────
        monitor = self._get_monitor()
        if monitor is None or monitor.cardmarket_scraper is None:
            await reply_message.reply(
                "⚠️ The Cardmarket scraper is not available right now. "
                "Please try again in a moment.",
                mention_author=False,
            )
            return

        try:
            cm_data = await monitor.cardmarket_scraper.scrape_url(normalised_url)
        except CardmarketScrapeError as exc:
            logger.warning(
                "ReviewCog: Cardmarket scrape failed for '%s': %s",
                normalised_url, exc,
            )
            # Log error.
            await self.db.log_error(
                listing_id=listing_id,
                listing_title=listing_title,
                listing_url=listing_url,
                cardmarket_url=normalised_url,
                failure_step=exc.step,
                http_status=exc.http_status,
                error_message=exc.message,
                stack_trace=exc.stack_trace,
            )
            # Post error to log channel.
            await self._send_error_to_log(exc, listing_title, listing_url, normalised_url)

            await reply_message.reply(
                f"⚠️ Failed to scrape Cardmarket: **{exc.message}**\n"
                f"Step: `{exc.step}`\n"
                "Please verify the URL is a valid Cardmarket product page and try again.",
                mention_author=False,
            )
            return
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "ReviewCog: unexpected error scraping '%s': %s",
                normalised_url, exc, exc_info=True,
            )
            await reply_message.reply(
                f"⚠️ An unexpected error occurred while scraping Cardmarket: {exc}",
                mention_author=False,
            )
            return

        # ── Price comparison ──────────────────────────────────────────────
        from scraper.base import Listing
        from services.price_comparison import compare_prices

        # Build a minimal Listing object for price comparison.
        stub_listing = Listing(
            listing_id=listing_id,
            title=listing_title,
            price=listing_price,
            currency=listing_currency,
            url=listing_url,
        )
        comparison = compare_prices(stub_listing, cm_data)

        # ── Store learning mapping ────────────────────────────────────────
        fingerprint = identify_card(listing_title)
        if monitor.resolver:
            await monitor.resolver.store_mapping(
                fingerprint=fingerprint,
                raw_title=listing_title,
                cardmarket_url=cm_data.product_url,
                product_name=cm_data.product_name,
                product_id=cm_data.product_id,
                validated_by=f"user:{submitted_by.display_name}",
                confidence=1.0,
                listing_url=listing_url,
                seller_name=review_item.get("seller_name"),
                price=listing_price,
            )

        # ── Mark review as resolved ───────────────────────────────────────
        await self.db.resolve_review_item(
            listing_id,
            cardmarket_url=cm_data.product_url,
            resolved_by=submitted_by.display_name,
        )

        # ── Post result embed ─────────────────────────────────────────────
        result_embed = build_review_resolved_embed(
            listing_title=listing_title,
            listing_url=listing_url,
            listing_price=listing_price,
            listing_currency=listing_currency,
            cm_data=cm_data,
            comparison=comparison,
            resolved_by=submitted_by.display_name,
        )
        try:
            await reply_message.reply(embed=result_embed, mention_author=False)
        except discord.HTTPException as exc:
            logger.error("ReviewCog: failed to post result embed: %s", exc)

        # ── If profitable, also post to deals channel ─────────────────────
        if comparison.is_profitable:
            from utils.embed_builder import build_profit_alert_embed
            from services.cardmarket_resolver import ResolvedUrl

            profit_embed = build_profit_alert_embed(
                stub_listing,
                cm_data,
                comparison,
                match_confidence=1.0,
                match_source="manual",
            )
            deals_channel = self._get_deals_channel()
            if deals_channel:
                try:
                    await deals_channel.send(embed=profit_embed)
                    logger.info(
                        "ReviewCog: posted profitable listing from review: %s",
                        listing_title[:60],
                    )
                except discord.HTTPException as exc:
                    logger.error(
                        "ReviewCog: failed to post profit embed to deals channel: %s", exc
                    )

        logger.info(
            "ReviewCog: resolved listing '%s' via user review (%s)",
            listing_title[:60], submitted_by.display_name,
        )

    # ------------------------------------------------------------------
    # Channel helpers
    # ------------------------------------------------------------------

    def _get_deals_channel(self) -> discord.TextChannel | None:
        ch_id = settings.discord_channel_id
        if not ch_id:
            return None
        ch = self.bot.get_channel(ch_id)
        return ch if isinstance(ch, discord.TextChannel) else None

    def _get_log_channel(self) -> discord.TextChannel | None:
        ch_id = settings.discord_log_channel_id
        if not ch_id:
            return None
        ch = self.bot.get_channel(ch_id)
        return ch if isinstance(ch, discord.TextChannel) else None

    async def _send_error_to_log(
        self,
        exc: CardmarketScrapeError,
        listing_title: str,
        listing_url: str,
        cardmarket_url: str,
    ) -> None:
        embed = build_error_embed(
            listing_title=listing_title,
            listing_url=listing_url,
            cardmarket_url=cardmarket_url,
            failure_step=exc.step,
            error_message=exc.message,
            http_status=exc.http_status,
            stack_trace=exc.stack_trace,
        )
        channel = self._get_log_channel()
        if channel:
            try:
                await channel.send(embed=embed)
            except discord.HTTPException:
                pass
