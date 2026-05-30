"""
bot/cogs/review.py
~~~~~~~~~~~~~~~~~~
Manual Review Learning System.

Workflow A – unresolvable listings (review channel):
  1. MonitorCog posts listings it cannot resolve to the #review-listings channel.
  2. A user replies to that message with the correct Cardmarket product URL.
  3. This cog detects the reply, validates the URL, normalises it with
     ``?sellerCountry=23&language=1``, scrapes Cardmarket pricing data, runs
     price comparison, and posts the result.
  4. The validated mapping is permanently stored in the learning database so
     future listings matching the same card are resolved automatically.

Workflow B – Cardmarket scrape errors (log channel):
  1. MonitorCog posts a scrape-error embed to the log channel and stores the
     Discord message ID in the error_log table.
  2. A user replies to that message with the correct Cardmarket product URL.
  3. This cog validates the URL, scrapes pricing data, stores the mapping and
     the correction, and attempts to extract a reusable slug-prefix rule.
  4. The prefix rule (e.g. Team Rocket → "TR") is saved and automatically
     applied to future URL constructions for the same set.

Message format the bot looks for in replies:
  Any reply to a known bot message that contains a cardmarket.com URL.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse
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


def _extract_product_slug(url: str) -> str | None:
    """Return the last path segment (product slug) from a Cardmarket URL.

    Example:
        _extract_product_slug("https://www.cardmarket.com/en/Pokemon/Products/Singles/Team-Rocket/Dark-Raichu-83?...")
        → "Dark-Raichu-83"
    """
    if not url:
        return None
    path = urlparse(url).path.rstrip("/")
    if not path:
        return None
    return path.split("/")[-1]


def _analyze_correction_pattern(
    generated_url: str,
    corrected_url: str,
    fingerprint,
) -> tuple[str | None, str | None, str | None]:
    """Analyse a URL correction to discover a reusable slug-prefix rule.

    Compares the *generated_url* product slug with the *corrected_url* product
    slug and the card's collector number to detect if the corrected version
    uses a prefix before the number (e.g. ``83`` → ``TR83``).

    Returns ``(learned_prefix, failed_slug, correct_slug)`` where
    *learned_prefix* is ``None`` when no reusable pattern was detected.

    Examples::

        _analyze_correction_pattern(
            "…/Dark-Raichu-83?…",
            "…/Dark-Raichu-TR83?…",
            fingerprint,  # collector_number="83/82"
        )
        → ("TR", "Dark-Raichu-83", "Dark-Raichu-TR83")

        _analyze_correction_pattern(
            "…/Krabby-51?…",
            "…/Krabby-FO51?…",
            fingerprint,  # collector_number="51"
        )
        → ("FO", "Krabby-51", "Krabby-FO51")
    """
    failed_slug = _extract_product_slug(generated_url)
    correct_slug = _extract_product_slug(corrected_url)

    if not failed_slug or not correct_slug or failed_slug == correct_slug:
        return None, failed_slug, correct_slug

    # Get bare collector number (strip fractional part and non-digits).
    collector_number = fingerprint.collector_number or ""
    bare_num = re.sub(r"[^0-9]", "", collector_number.split("/")[0]) if collector_number else ""

    if not bare_num:
        return None, failed_slug, correct_slug

    # The failed slug should end with the bare number.
    # The correct slug should end with {prefix}{bare_num} where prefix != "".
    # Both should share a common card-name prefix.
    if not failed_slug.endswith(f"-{bare_num}"):
        return None, failed_slug, correct_slug

    # The card-name part of both slugs (everything before the number segment).
    card_slug_part = failed_slug[: -(len(bare_num) + 1)]  # strip "-<num>"

    # The corrected number segment is correct_slug minus the common card-name part.
    if not correct_slug.startswith(card_slug_part + "-"):
        return None, failed_slug, correct_slug

    corrected_num_part = correct_slug[len(card_slug_part) + 1:]  # after "-"

    # corrected_num_part should be "{prefix}{bare_num}" where prefix is non-empty.
    if not corrected_num_part.endswith(bare_num):
        return None, failed_slug, correct_slug

    prefix = corrected_num_part[: -len(bare_num)]
    if not prefix or not prefix.isalpha():
        # Only accept alphabetic prefixes (e.g. "TR", "FO", "BS").
        return None, failed_slug, correct_slug

    return prefix, failed_slug, correct_slug


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
        """Listen for Cardmarket URL replies in the review or log channel."""
        if message.author.bot:
            return

        # Must be a reply to another message.
        if message.reference is None:
            return

        review_channel_id = settings.discord_review_channel_id
        log_channel_id = settings.discord_log_channel_id

        if review_channel_id and message.channel.id == review_channel_id:
            await self._handle_review_reply(message)
        elif log_channel_id and message.channel.id == log_channel_id:
            await self._handle_correction_reply(message)

    async def _handle_review_reply(self, message: discord.Message) -> None:
        """Handle a reply in the review channel (unresolvable listing)."""
        referenced_id = str(message.reference.message_id)  # type: ignore[union-attr]

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

    async def _handle_correction_reply(self, message: discord.Message) -> None:
        """Handle a reply in the log channel (correction for a scrape error)."""
        referenced_id = str(message.reference.message_id)  # type: ignore[union-attr]

        # Look up the error_log entry for this Discord message.
        error_item = await self.db.get_error_by_message_id(referenced_id)
        if error_item is None:
            return  # Not a reply to a known error message.

        # Extract Cardmarket URL from the reply.
        cm_url = _extract_cardmarket_url(message.content)
        if not cm_url:
            await message.reply(
                "⚠️ No valid Cardmarket product URL found in your message.\n"
                "Please reply with the correct URL like: "
                "`https://www.cardmarket.com/en/Pokemon/Products/Singles/...`",
                mention_author=False,
            )
            return

        await self._process_correction_reply(
            error_item=error_item,
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
    # Correction processing (log channel replies)
    # ------------------------------------------------------------------

    async def _process_correction_reply(
        self,
        error_item: dict,
        cardmarket_url: str,
        submitted_by: discord.Member | discord.User,
        reply_message: discord.Message,
    ) -> None:
        """Validate a user-supplied correction, scrape it, learn from it, and
        retry normal processing using the corrected URL."""
        listing_id = error_item.get("listing_id")
        listing_title = error_item.get("listing_title") or ""
        listing_url = error_item.get("listing_url") or ""
        generated_cm_url = error_item.get("cardmarket_url") or ""

        normalised_url = normalize_cardmarket_url(cardmarket_url)

        logger.info(
            "ReviewCog: processing correction '%s' for listing '%s' (submitted by %s)",
            normalised_url, listing_title[:60], submitted_by.display_name,
        )

        # ── Scrape the corrected Cardmarket URL ───────────────────────────
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
                "ReviewCog: Cardmarket scrape of correction failed for '%s': %s",
                normalised_url, exc,
            )
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
            await reply_message.reply(
                f"⚠️ Failed to scrape the corrected Cardmarket URL: **{exc.message}**\n"
                f"Step: `{exc.step}`\n"
                "Please verify the URL is a valid Cardmarket product page and try again.",
                mention_author=False,
            )
            return
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "ReviewCog: unexpected error scraping correction '%s': %s",
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

        # Reconstruct a minimal Listing for comparison. Use the price stored
        # in the error_log row; fall back to 0.0 for old entries that predate
        # the listing_price column.
        listing_price = float(error_item.get("listing_price") or 0.0)
        listing_currency = error_item.get("listing_currency") or "EUR"
        stub_listing = Listing(
            listing_id=listing_id or "correction",
            title=listing_title,
            price=listing_price,
            currency=listing_currency,
            url=listing_url,
        )
        comparison = compare_prices(stub_listing, cm_data)

        # ── Identify card fingerprint from the listing title ──────────────
        fingerprint = identify_card(listing_title)

        # ── Learn prefix pattern from the correction ──────────────────────
        learned_prefix, failed_slug, correct_slug = _analyze_correction_pattern(
            generated_url=generated_cm_url,
            corrected_url=normalised_url,
            fingerprint=fingerprint,
        )

        if learned_prefix is not None and fingerprint.set_code and monitor.resolver:
            await monitor.resolver.store_prefix_rule(
                set_code=fingerprint.set_code,
                prefix=learned_prefix,
                set_name=fingerprint.set_name,
            )
            logger.info(
                "ReviewCog: learned prefix rule set_code=%r prefix=%r from correction by %s",
                fingerprint.set_code, learned_prefix, submitted_by.display_name,
            )

        # ── Store correction in the database ─────────────────────────────
        original_identifier = _extract_product_slug(generated_cm_url)
        corrected_identifier = _extract_product_slug(normalised_url)

        await self.db.add_correction(
            listing_id=listing_id,
            listing_title=listing_title,
            listing_url=listing_url,
            generated_cardmarket_url=generated_cm_url,
            failed_slug=failed_slug or original_identifier,
            correct_cardmarket_url=normalised_url,
            correct_slug=corrected_identifier,
            product_name=cm_data.product_name,
            set_name=fingerprint.set_name,
            set_code=fingerprint.set_code,
            collector_number=fingerprint.collector_number,
            original_identifier=original_identifier,
            corrected_identifier=corrected_identifier,
            learned_prefix=learned_prefix,
            corrected_by=submitted_by.display_name,
        )

        # ── Store validated card mapping ──────────────────────────────────
        if monitor.resolver:
            await monitor.resolver.store_mapping(
                fingerprint=fingerprint,
                raw_title=listing_title,
                cardmarket_url=cm_data.product_url,
                product_name=cm_data.product_name,
                product_id=cm_data.product_id,
                validated_by=f"correction:{submitted_by.display_name}",
                confidence=1.0,
                listing_url=listing_url,
            )

        # ── Confirm to the user ───────────────────────────────────────────
        result_embed = build_review_resolved_embed(
            listing_title=listing_title,
            listing_url=listing_url,
            listing_price=listing_price,
            listing_currency=listing_currency,
            cm_data=cm_data,
            comparison=comparison,
            resolved_by=submitted_by.display_name,
        )
        if learned_prefix is not None:
            result_embed.add_field(
                name="🧠 Learned Pattern",
                value=(
                    f"Set `{fingerprint.set_code}` uses prefix `{learned_prefix}` "
                    "before collector numbers. "
                    "Future cards from this set will use this rule automatically."
                ),
                inline=False,
            )

        try:
            await reply_message.reply(embed=result_embed, mention_author=False)
        except discord.HTTPException as exc:
            logger.error("ReviewCog: failed to post correction confirmation: %s", exc)

        # ── Post to deals channel if profitable ───────────────────────────
        if comparison.is_profitable:
            from utils.embed_builder import build_profit_alert_embed

            profit_embed = build_profit_alert_embed(
                stub_listing,
                cm_data,
                comparison,
                match_confidence=1.0,
                match_source="correction",
            )
            deals_channel = self._get_deals_channel()
            if deals_channel:
                try:
                    await deals_channel.send(embed=profit_embed)
                except discord.HTTPException as exc:
                    logger.error(
                        "ReviewCog: failed to post profit embed for correction: %s", exc
                    )

        logger.info(
            "ReviewCog: correction processed for listing '%s' by %s (prefix=%r)",
            listing_title[:60], submitted_by.display_name, learned_prefix,
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
