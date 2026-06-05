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
from scraper.cardmarket import (
    CardmarketScrapeError,
    _LANGUAGE_TO_CM_CODE,
    contains_psa,
    extract_psa_grade,
    normalize_cardmarket_url,
)
from services.card_identifier import identify_card
from services.cardmarket_resolver import set_name_to_code
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


def _derive_set_code_from_url(url: str) -> str | None:
    """Attempt to derive a set code from a Cardmarket Singles URL path.

    Cardmarket set slug (e.g. ``Team-Rocket``) is the segment immediately after
    ``/Singles/``.  We reverse-map the slug back to a set code using the
    ``_SET_CODE_TO_SLUG`` table from ``scraper.cardmarket``.

    Returns a set code string (e.g. ``"TR"``) or ``None`` when no match found.
    """
    try:
        from scraper.cardmarket import _SET_CODE_TO_SLUG  # noqa: PLC0415

        path = urlparse(url).path
        parts = path.split("/")
        # Path structure: /en/Pokemon/Products/Singles/{set-slug}/{product-slug}
        try:
            singles_idx = parts.index("Singles")
            set_slug = parts[singles_idx + 1]
        except (ValueError, IndexError):
            return None
        if not set_slug:
            return None
        # Build reverse mapping (slug → code).  Only do this once per call;
        # it's fast enough for correction processing frequency.
        slug_to_code = {v: k for k, v in _SET_CODE_TO_SLUG.items()}
        return slug_to_code.get(set_slug)
    except Exception:  # noqa: BLE001
        return None


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
        # Tracks bot messages that are awaiting a product ID reply in the
        # unidentified channel.  Key: bot Discord message ID (int).
        # Value: dict with 'review_item', 'cm_url', 'product_slug', 'set_slug'.
        self._pending_product_ids: dict[int, dict] = {}

    def _get_monitor(self) -> "MonitorCog | None":
        return self.bot.cogs.get("Monitor")  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Message listener
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Listen for Cardmarket URL replies in the review, unidentified, or log channel."""
        if message.author.bot:
            return

        # Must be a reply to another message.
        if message.reference is None:
            return

        review_channel_id = settings.discord_review_channel_id
        unidentified_channel_id = settings.discord_unidentified_channel_id
        log_channel_id = settings.discord_log_channel_id
        deals_channel_id = settings.discord_channel_id
        match_channel_id = settings.discord_match_channel_id

        if review_channel_id and message.channel.id == review_channel_id:
            await self._handle_review_reply(message)
        elif unidentified_channel_id and message.channel.id == unidentified_channel_id:
            await self._handle_unidentified_reply(message)
        elif log_channel_id and message.channel.id == log_channel_id:
            await self._handle_correction_reply(message)
        elif deals_channel_id and message.channel.id == deals_channel_id:
            await self._handle_deal_channel_reply(message)
        elif match_channel_id and message.channel.id == match_channel_id:
            await self._handle_deal_channel_reply(message)

    async def _handle_unidentified_reply(self, message: discord.Message) -> None:
        """Handle a reply in the unidentified channel.

        Two-step flow:
        1. User replies to an unidentified-listing embed with a Cardmarket URL.
           → Bot scrapes CM, posts the scraped info, and asks for the idProduct.
        2. User replies to the bot's "awaiting product ID" message with a number.
           → Bot looks up prices.json, stores ID→slug mappings, resolves the item.
        """
        referenced_id = message.reference.message_id  # type: ignore[union-attr]

        # Step 2: reply to one of our "awaiting product ID" messages.
        if referenced_id in self._pending_product_ids:
            await self._handle_unidentified_product_id_reply(
                message, self._pending_product_ids[referenced_id]
            )
            return

        # Step 1: reply to the original unidentified listing embed.
        referenced_id_str = str(referenced_id)
        review_item = await self.db.get_review_item_by_message(referenced_id_str)
        if review_item is None:
            return  # Not a reply to a known review message.

        if review_item.get("status") == "resolved":
            return

        cm_url = _extract_cardmarket_url(message.content)
        if not cm_url:
            await message.reply(
                "⚠️ No valid Cardmarket product URL found in your message.\n"
                "Please reply with a URL like: "
                "`https://www.cardmarket.com/en/Pokemon/Products/Singles/...`",
                mention_author=False,
            )
            return

        await self._handle_unidentified_url_step(
            message=message,
            review_item=review_item,
            cardmarket_url=cm_url,
        )

    async def _handle_review_reply(self, message: discord.Message) -> None:
        """Handle a reply in the review channel (unresolvable listing)."""
        referenced_id = message.reference.message_id  # type: ignore[union-attr]

        # Step 2: reply to one of our "awaiting product ID" messages.
        if referenced_id in self._pending_product_ids:
            await self._handle_unidentified_product_id_reply(
                message, self._pending_product_ids[referenced_id]
            )
            return

        referenced_id = str(referenced_id)

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
    # Unidentified channel – two-step ID mapping flow
    # ------------------------------------------------------------------

    async def _handle_unidentified_url_step(
        self,
        message: discord.Message,
        review_item: dict,
        cardmarket_url: str,
    ) -> None:
        """Step 1 of the unidentified-channel flow: user supplied a CM URL.

        Scrapes Cardmarket, posts the result, then asks the user to supply
        the ``idProduct`` so the catalog ID → URL-slug mapping can be stored.
        """
        # Identify the card first so the normalised Cardmarket URL can be
        # filtered by condition (e.g. minCondition=2 for Near Mint).
        fingerprint = identify_card(
            review_item.get("title", ""), review_item.get("description")
        )
        min_condition: int | None = (
            fingerprint.condition_code
            if fingerprint.condition_code is not None and 1 <= fingerprint.condition_code <= 6
            else None
        )

        normalised_url = normalize_cardmarket_url(
            cardmarket_url,
            language=_LANGUAGE_TO_CM_CODE.get(fingerprint.language) if fingerprint.language else None,
            is_reverse_holo=fingerprint.is_reverse_holo,
            min_condition=min_condition,
        )
        url_path = urlparse(normalised_url).path.rstrip("/")
        path_parts = url_path.split("/")
        product_slug: str | None = None
        set_slug: str | None = None
        try:
            singles_idx = path_parts.index("Singles")
            set_slug = path_parts[singles_idx + 1] if singles_idx + 1 < len(path_parts) else None
            product_slug = path_parts[-1] if len(path_parts) > singles_idx + 1 else None
        except (ValueError, IndexError):
            pass

        logger.info(
            "ReviewCog (unidentified): step-1 URL '%s' slug=%r set=%r (submitted by %s)",
            normalised_url, product_slug, set_slug, message.author.display_name,
        )

        monitor = self._get_monitor()
        if monitor is None or monitor.cardmarket_scraper is None:
            await message.reply(
                "⚠️ The Cardmarket scraper is not available right now. "
                "Please try again in a moment.",
                mention_author=False,
            )
            return

        try:
            cm_data = await monitor.cardmarket_scraper.scrape_url(normalised_url)
        except CardmarketScrapeError as exc:
            logger.warning(
                "ReviewCog (unidentified): Cardmarket scrape failed for '%s': %s",
                normalised_url, exc,
            )
            await message.reply(
                f"⚠️ Failed to scrape Cardmarket: **{exc.message}**\n"
                f"Step: `{exc.step}`\n"
                "Please verify the URL is a valid Cardmarket product page and try again.",
                mention_author=False,
            )
            return
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "ReviewCog (unidentified): unexpected error scraping '%s': %s",
                normalised_url, exc, exc_info=True,
            )
            await message.reply(
                f"⚠️ An unexpected error occurred while scraping Cardmarket: {exc}",
                mention_author=False,
            )
            return

        listing_id = review_item["listing_id"]
        listing_title = review_item["title"]
        listing_url = review_item["url"]
        listing_price = float(review_item.get("price") or 0.0)
        listing_currency = review_item.get("currency") or "EUR"

        # ── PSA-specific listing price (mirrors _process_review_reply) ────────
        combined_text = listing_title + " " + (review_item.get("description") or "")
        psa_grade: int | None = None
        if contains_psa(combined_text):
            psa_grade = extract_psa_grade(combined_text)
            if psa_grade is not None:
                logger.info(
                    "ReviewCog (unidentified): PSA %d detected in listing '%s'",
                    psa_grade, listing_title[:60],
                )

        if psa_grade is not None and psa_grade >= 9:
            from dataclasses import replace as _dc_replace
            psa_price = await monitor.cardmarket_scraper.scrape_psa_listing_price(
                normalised_url, psa_grade
            )
            if psa_price is not None:
                cm_data = _dc_replace(cm_data, from_price=psa_price)

        # ── Price comparison ──────────────────────────────────────────────────
        from scraper.base import Listing
        from services.price_comparison import compare_prices

        stub_listing = Listing(
            listing_id=listing_id,
            title=listing_title,
            price=listing_price,
            currency=listing_currency,
            url=listing_url,
            seller_name=review_item.get("seller_name"),
        )
        comparison = compare_prices(stub_listing, cm_data)

        # ── Store learning mapping ────────────────────────────────────────────
        if monitor.resolver:
            await monitor.resolver.store_mapping(
                fingerprint=fingerprint,
                raw_title=listing_title,
                cardmarket_url=cm_data.product_url or normalised_url,
                product_name=cm_data.product_name,
                product_id=cm_data.product_id,
                validated_by=f"user:{message.author.display_name}",
                confidence=1.0,
                listing_url=listing_url,
                seller_name=review_item.get("seller_name"),
                price=listing_price,
            )

        # ── Mark review as resolved ───────────────────────────────────────────
        await self.db.resolve_review_item(
            listing_id,
            cardmarket_url=cm_data.product_url or normalised_url,
            resolved_by=message.author.display_name,
        )

        # ── Build and post the full resolved embed ────────────────────────────
        result_embed = build_review_resolved_embed(
            listing_title=listing_title,
            listing_url=listing_url,
            listing_price=listing_price,
            listing_currency=listing_currency,
            cm_data=cm_data,
            comparison=comparison,
            resolved_by=message.author.display_name,
            fingerprint=fingerprint,
        )
        # Append slug info so the user knows what was extracted from the URL.
        slug_lines = []
        if product_slug:
            slug_lines.append(f"**Product slug:** `{product_slug}`")
        if set_slug:
            slug_lines.append(f"**Set slug:** `{set_slug}`")
        if slug_lines:
            result_embed.add_field(
                name="🔖 URL slugs",
                value="\n".join(slug_lines),
                inline=False,
            )

        # If the idProduct was extracted automatically from the page HTML, store
        # the catalog mapping right away without requiring a user reply.
        auto_id_product: int | None = cm_data.id_product
        if auto_id_product is not None:
            id_expansion, mapping_lines = await self._lookup_catalog_for_id_product(
                auto_id_product, product_slug, set_slug
            )
            if mapping_lines:
                result_embed.add_field(
                    name="🗺️ Catalog mappings stored",
                    value="\n".join(mapping_lines),
                    inline=False,
                )
            result_embed.set_footer(text="Mapping saved · catalog ID→slug stored automatically")
        else:
            result_embed.set_footer(text="Mapping saved · step 1 of 2 – reply with idProduct to complete catalog mapping")

        try:
            bot_msg = await message.reply(embed=result_embed, mention_author=False)
        except discord.HTTPException as exc:
            logger.error("ReviewCog (unidentified): failed to post step-1 embed: %s", exc)
            return

        # ── If profitable, post to deals channel ─────────────────────────────
        if comparison.is_profitable:
            from utils.embed_builder import build_profit_alert_embed

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
                        "ReviewCog (unidentified): posted profitable listing from unidentified: %s",
                        listing_title[:60],
                    )
                except discord.HTTPException as exc:
                    logger.error(
                        "ReviewCog (unidentified): failed to post profit embed to deals channel: %s",
                        exc,
                    )

        if auto_id_product is not None:
            # Store catalog mappings automatically (id_expansion already fetched above).
            await self.db.store_catalog_id_slugs(
                id_product=auto_id_product,
                product_slug=product_slug,
                id_expansion=id_expansion,
                set_slug=set_slug,
                cardmarket_url=normalised_url,
            )
            logger.info(
                "ReviewCog (unidentified): auto-stored catalog mappings for '%s' "
                "idProduct=%d idExpansion=%s slug=%r set=%r",
                listing_title[:60], auto_id_product, id_expansion, product_slug, set_slug,
            )
            return

        # ── Ask the user for idProduct to complete catalog slug mappings ──────
        try:
            prompt_msg = await bot_msg.reply(
                "*(Optional)* Reply to **this message** with the Cardmarket **`idProduct`** "
                f"for `{product_slug or normalised_url}` to store the catalog ID→slug mapping.\n"
                "You can find `idProduct` in the Cardmarket page source or API response.",
                mention_author=False,
            )
        except discord.HTTPException as exc:
            logger.error("ReviewCog (unidentified): failed to send product-ID prompt: %s", exc)
            return

        # Store pending state keyed by the prompt message so the user replies to it.
        self._pending_product_ids[prompt_msg.id] = {
            "review_item": review_item,
            "cm_url": normalised_url,
            "cm_data": cm_data,
            "product_slug": product_slug,
            "set_slug": set_slug,
            "submitted_by": message.author,
        }
        logger.info(
            "ReviewCog (unidentified): resolved listing '%s', awaiting optional idProduct for msg %s (product_slug=%r)",
            listing_title[:60], prompt_msg.id, product_slug,
        )

    async def _lookup_catalog_for_id_product(
        self,
        product_id: int,
        product_slug: str | None,
        set_slug: str | None,
    ) -> tuple[int | None, list[str]]:
        """Look up *product_id* in the local catalog and return ``(id_expansion, mapping_lines)``.

        *mapping_lines* is a list of human-readable strings describing the
        ID → slug mappings that were found, suitable for embedding in a Discord embed.
        """
        monitor = self._get_monitor()
        catalog = monitor.catalog if monitor else None
        id_expansion: int | None = None
        mapping_lines: list[str] = []

        if catalog and catalog.is_loaded:
            product = catalog.get_product_by_id(product_id)
            if product:
                raw_expansion = product.get("idExpansion")
                if raw_expansion is not None:
                    id_expansion = int(raw_expansion)
            else:
                logger.warning(
                    "ReviewCog: auto-detected idProduct=%d not found in catalog", product_id
                )

        if product_slug:
            mapping_lines.append(f"**idProduct:** `{product_id}`\n> mapped to `{product_slug}`")
        if id_expansion is not None and set_slug:
            mapping_lines.append(f"**idExpansion:** `{id_expansion}`\n> mapped to `{set_slug}`")

        return id_expansion, mapping_lines

    async def _handle_unidentified_product_id_reply(
        self,
        message: discord.Message,
        pending: dict,
    ) -> None:
        """Step 2 of the unidentified-channel flow: user supplied an idProduct.

        Looks up the product in the catalog, posts prices.json info, and stores
        the idProduct → product_slug and idExpansion → set_slug catalog mappings.
        (The listing is already resolved and the deal was already sent in step 1.)
        """
        # Parse idProduct from the message (first integer found).
        product_id_match = re.search(r"\b(\d{4,9})\b", message.content)
        if not product_id_match:
            await message.reply(
                "⚠️ Could not find a valid `idProduct` (a numeric ID, e.g. `299451`) "
                "in your message. Please reply with just the number.",
                mention_author=False,
            )
            return

        product_id = int(product_id_match.group(1))
        review_item = pending["review_item"]
        cm_url: str = pending["cm_url"]
        product_slug: str | None = pending["product_slug"]
        set_slug: str | None = pending["set_slug"]

        logger.info(
            "ReviewCog (unidentified): step-2 idProduct=%d slug=%r set=%r (submitted by %s)",
            product_id, product_slug, set_slug, message.author.display_name,
        )

        # Look up catalog data for this idProduct.
        monitor = self._get_monitor()
        catalog = monitor.catalog if monitor else None
        id_expansion: int | None = None
        price_data = None
        product_found_in_catalog = False

        if catalog and catalog.is_loaded:
            product = catalog.get_product_by_id(product_id)
            if product:
                product_found_in_catalog = True
                id_expansion = product.get("idExpansion")
                if id_expansion is not None:
                    id_expansion = int(id_expansion)
                price_data = catalog.get_price_data(product)
                if price_data is None:
                    logger.warning(
                        "ReviewCog (unidentified): idProduct=%d found in catalog but "
                        "price data unavailable",
                        product_id,
                    )
            else:
                logger.warning(
                    "ReviewCog (unidentified): idProduct=%d not found in catalog", product_id
                )

        # Build the response embed with prices.json data.
        embed = discord.Embed(
            title=f"📦 Catalog data for idProduct {product_id}",
            url=cm_url,
            colour=0x00FF7F,
        )

        if price_data is not None:
            if price_data.product_name:
                embed.add_field(name="Name", value=price_data.product_name, inline=False)
            if price_data.from_price is not None:
                embed.add_field(name="LOW (from)", value=f"€{price_data.from_price:.2f}", inline=True)
            if price_data.price_trend is not None:
                embed.add_field(name="TREND", value=f"€{price_data.price_trend:.2f}", inline=True)
            if price_data.avg_30_days is not None:
                embed.add_field(name="AVG30", value=f"€{price_data.avg_30_days:.2f}", inline=True)
            if price_data.avg_7_days is not None:
                embed.add_field(name="AVG7", value=f"€{price_data.avg_7_days:.2f}", inline=True)
            if price_data.avg_1_day is not None:
                embed.add_field(name="AVG1", value=f"€{price_data.avg_1_day:.2f}", inline=True)
            if price_data.set_name:
                embed.add_field(name="Set", value=price_data.set_name, inline=True)
        else:
            if product_found_in_catalog:
                embed.description = (
                    "⚠️ `idProduct` found in catalog but no price data is available. "
                    "The mapping will still be stored."
                )
            else:
                embed.description = (
                    "⚠️ This `idProduct` was not found in the local catalog. "
                    "The mapping will still be stored."
                )

        # Show ID → slug mappings.
        mapping_lines = []
        if product_slug:
            mapping_lines.append(f"**idProduct:** `{product_id}`\n> mapped to `{product_slug}`")
        if id_expansion is not None and set_slug:
            mapping_lines.append(f"**idExpansion:** `{id_expansion}`\n> mapped to `{set_slug}`")
        if mapping_lines:
            embed.add_field(
                name="🗺️ Mappings stored",
                value="\n".join(mapping_lines),
                inline=False,
            )

        embed.set_footer(text=f"Catalog mapping stored by {message.author.display_name} · step 2 of 2")

        try:
            await message.reply(embed=embed, mention_author=False)
        except discord.HTTPException as exc:
            logger.error("ReviewCog (unidentified): failed to post step-2 embed: %s", exc)

        # Persist the ID → slug mappings.
        await self.db.store_catalog_id_slugs(
            id_product=product_id,
            product_slug=product_slug,
            id_expansion=id_expansion,
            set_slug=set_slug,
            cardmarket_url=cm_url,
        )

        # Remove from pending state.
        ref_id = message.reference.message_id  # type: ignore[union-attr]
        self._pending_product_ids.pop(ref_id, None)

        listing_title = review_item["title"]
        logger.info(
            "ReviewCog (unidentified): stored catalog mappings for '%s' idProduct=%d idExpansion=%s "
            "slug=%r set=%r by %s",
            listing_title[:60], product_id, id_expansion, product_slug, set_slug,
            message.author.display_name,
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

    async def _handle_deal_channel_reply(self, message: discord.Message) -> None:
        """Handle a reply in the deals or match channel (correction for a faulty match)."""
        referenced_id = str(message.reference.message_id)  # type: ignore[union-attr]

        # Look up the deal_messages entry for this Discord message.
        deal_item = await self.db.get_deal_message_by_id(referenced_id)
        if deal_item is None:
            return  # Not a reply to a known deal/match message.

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
            error_item=deal_item,
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

        # Identify the card first so the normalised Cardmarket URL can be
        # filtered by condition (e.g. minCondition=2 for Near Mint).
        fingerprint = identify_card(listing_title, review_item.get("description"))
        min_condition: int | None = (
            fingerprint.condition_code
            if fingerprint.condition_code is not None and 1 <= fingerprint.condition_code <= 6
            else None
        )

        # Always normalise the URL (add sellerCountry=23, correct language, and condition).
        normalised_url = normalize_cardmarket_url(
            cardmarket_url,
            language=_LANGUAGE_TO_CM_CODE.get(fingerprint.language) if fingerprint.language else None,
            is_reverse_holo=fingerprint.is_reverse_holo,
            min_condition=min_condition,
        )

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

        # ── PSA-specific listing price ────────────────────────────────────
        # Mirror the PSA grade check from MonitorCog: if the listing title or
        # description mentions a PSA grade ≥ 9, attempt to fetch the price of
        # the matching MT listing on Cardmarket and use it instead of the
        # generic "From" price.
        combined_text = listing_title + " " + (review_item.get("description") or "")
        psa_grade: int | None = None
        if contains_psa(combined_text):
            psa_grade = extract_psa_grade(combined_text)
            if psa_grade is not None:
                logger.info(
                    "ReviewCog: PSA %d detected in listing '%s'",
                    psa_grade, listing_title[:60],
                )

        if psa_grade is not None and psa_grade >= 9:
            from dataclasses import replace as _dc_replace
            psa_price = await monitor.cardmarket_scraper.scrape_psa_listing_price(
                normalised_url, psa_grade
            )
            if psa_price is not None:
                cm_data = _dc_replace(cm_data, from_price=psa_price)
                logger.info(
                    "ReviewCog: using PSA %d MT listing price €%.2f for '%s'",
                    psa_grade, psa_price, listing_title[:60],
                )
            else:
                logger.info(
                    "ReviewCog: no PSA %d MT listing found on Cardmarket – "
                    "falling back to standard From price for '%s'",
                    psa_grade, listing_title[:60],
                )

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
            seller_name=review_item.get("seller_name"),
        )
        comparison = compare_prices(stub_listing, cm_data)

        # ── Store learning mapping ────────────────────────────────────────
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

        # ── Extract URL slug info for product ID request ──────────────────
        url_path = urlparse(normalised_url).path.rstrip("/")
        path_parts = url_path.split("/")
        product_slug: str | None = None
        set_slug: str | None = None
        try:
            singles_idx = path_parts.index("Singles")
            set_slug = path_parts[singles_idx + 1] if singles_idx + 1 < len(path_parts) else None
            product_slug = path_parts[-1] if len(path_parts) > singles_idx + 1 else None
        except (ValueError, IndexError):
            pass

        # ── Post result embed ─────────────────────────────────────────────
        result_embed = build_review_resolved_embed(
            listing_title=listing_title,
            listing_url=listing_url,
            listing_price=listing_price,
            listing_currency=listing_currency,
            cm_data=cm_data,
            comparison=comparison,
            resolved_by=submitted_by.display_name,
            fingerprint=fingerprint,
        )
        if product_slug or set_slug:
            slug_lines = []
            if product_slug:
                slug_lines.append(f"**Product slug:** `{product_slug}`")
            if set_slug:
                slug_lines.append(f"**Set slug:** `{set_slug}`")
            result_embed.add_field(
                name="🔖 URL slugs",
                value="\n".join(slug_lines),
                inline=False,
            )

        # If the idProduct was extracted automatically from the page HTML, store
        # the catalog mapping right away without requiring a user reply.
        auto_id_product: int | None = cm_data.id_product
        if auto_id_product is not None:
            id_expansion, mapping_lines = await self._lookup_catalog_for_id_product(
                auto_id_product, product_slug, set_slug
            )
            if mapping_lines:
                result_embed.add_field(
                    name="🗺️ Catalog mappings stored",
                    value="\n".join(mapping_lines),
                    inline=False,
                )
            result_embed.set_footer(text="Mapping saved · catalog ID→slug stored automatically")
        else:
            result_embed.set_footer(text="Mapping saved · step 1 of 2 – reply with idProduct to complete catalog mapping")

        try:
            bot_msg = await reply_message.reply(embed=result_embed, mention_author=False)
        except discord.HTTPException as exc:
            logger.error("ReviewCog: failed to post result embed: %s", exc)
            return

        # ── If profitable, also post to deals channel ─────────────────────
        if comparison.is_profitable:
            from utils.embed_builder import build_profit_alert_embed

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

        if auto_id_product is not None:
            # Store catalog mappings automatically (id_expansion already fetched above).
            await self.db.store_catalog_id_slugs(
                id_product=auto_id_product,
                product_slug=product_slug,
                id_expansion=id_expansion,
                set_slug=set_slug,
                cardmarket_url=normalised_url,
            )
            logger.info(
                "ReviewCog: auto-stored catalog mappings for '%s' "
                "idProduct=%d idExpansion=%s slug=%r set=%r (submitted by %s)",
                listing_title[:60], auto_id_product, id_expansion, product_slug, set_slug,
                submitted_by.display_name,
            )
            return

        # ── Ask for idProduct to complete catalog slug mappings ───────────
        try:
            prompt_msg = await bot_msg.reply(
                "*(Optional)* Reply to **this message** with the Cardmarket **`idProduct`** "
                f"for `{product_slug or normalised_url}` to store the catalog ID→slug mapping.\n"
                "You can find `idProduct` in the Cardmarket page source or API response.",
                mention_author=False,
            )
        except discord.HTTPException as exc:
            logger.error("ReviewCog: failed to send product-ID prompt: %s", exc)
            return

        self._pending_product_ids[prompt_msg.id] = {
            "review_item": review_item,
            "cm_url": normalised_url,
            "cm_data": cm_data,
            "product_slug": product_slug,
            "set_slug": set_slug,
            "submitted_by": submitted_by,
        }
        logger.info(
            "ReviewCog: resolved listing '%s' via user review (%s), awaiting optional idProduct for msg %s (product_slug=%r)",
            listing_title[:60], submitted_by.display_name, prompt_msg.id, product_slug,
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

        # Identify the card first so the normalised Cardmarket URL can be
        # filtered by condition.
        fingerprint = identify_card(listing_title)
        min_condition: int | None = (
            fingerprint.condition_code
            if fingerprint.condition_code is not None and 1 <= fingerprint.condition_code <= 6
            else None
        )

        normalised_url = normalize_cardmarket_url(
            cardmarket_url,
            language=_LANGUAGE_TO_CM_CODE.get(fingerprint.language) if fingerprint.language else None,
            is_reverse_holo=fingerprint.is_reverse_holo,
            min_condition=min_condition,
        )

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

        # ── PSA-specific listing price ────────────────────────────────────
        # Mirror the PSA grade check from MonitorCog: if the listing title
        # mentions a PSA grade ≥ 9, attempt to fetch the price of the matching
        # MT listing on Cardmarket and use it instead of the generic "From" price.
        psa_grade: int | None = None
        if contains_psa(listing_title):
            psa_grade = extract_psa_grade(listing_title)
            if psa_grade is not None:
                logger.info(
                    "ReviewCog: PSA %d detected in listing '%s'",
                    psa_grade, listing_title[:60],
                )

        if psa_grade is not None and psa_grade >= 9:
            from dataclasses import replace as _dc_replace
            psa_price = await monitor.cardmarket_scraper.scrape_psa_listing_price(
                normalised_url, psa_grade
            )
            if psa_price is not None:
                cm_data = _dc_replace(cm_data, from_price=psa_price)
                logger.info(
                    "ReviewCog: using PSA %d MT listing price €%.2f for '%s'",
                    psa_grade, psa_price, listing_title[:60],
                )
            else:
                logger.info(
                    "ReviewCog: no PSA %d MT listing found on Cardmarket – "
                    "falling back to standard From price for '%s'",
                    psa_grade, listing_title[:60],
                )

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
            seller_name=error_item.get("listing_seller_name"),
        )
        comparison = compare_prices(stub_listing, cm_data)

        # ── Learn prefix pattern from the correction ──────────────────────
        learned_prefix, failed_slug, correct_slug = _analyze_correction_pattern(
            generated_url=generated_cm_url,
            corrected_url=normalised_url,
            fingerprint=fingerprint,
        )

        # Resolve the effective set code: use the fingerprint's explicit set_code
        # when present, otherwise derive it from the set name (e.g. "Temporal Forces"
        # → "TEF").  This ensures prefix rules are persisted even when the listing
        # title does not include an explicit set code token.
        effective_set_code = fingerprint.set_code or (
            set_name_to_code(fingerprint.set_name) if fingerprint.set_name else None
        )

        if learned_prefix is not None and effective_set_code and monitor.resolver:
            await monitor.resolver.store_prefix_rule(
                set_code=effective_set_code,
                prefix=learned_prefix,
                set_name=fingerprint.set_name,
            )
            logger.info(
                "ReviewCog: learned prefix rule set_code=%r prefix=%r from correction by %s",
                effective_set_code, learned_prefix, submitted_by.display_name,
            )

        # ── P3.2: derive set_code from corrected URL path when fingerprint ─
        # has none.  Attempt a reverse-slug-to-code lookup so the prefix rule
        # is still stored even when the listing title had no set token.
        if learned_prefix is not None and not effective_set_code and monitor.resolver:
            derived_from_url = _derive_set_code_from_url(normalised_url)
            if derived_from_url:
                effective_set_code = derived_from_url
                await monitor.resolver.store_prefix_rule(
                    set_code=effective_set_code,
                    prefix=learned_prefix,
                    set_name=fingerprint.set_name,
                )
                logger.info(
                    "ReviewCog: P3.2 derived set_code=%r from URL, stored prefix=%r",
                    effective_set_code, learned_prefix,
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
            # ── P1.2: Store slug override for direct future lookups ───────────
            corrected_slug = _extract_product_slug(normalised_url)
            if corrected_slug:
                await monitor.resolver.store_slug_override(
                    fingerprint=fingerprint,
                    preferred_slug=corrected_slug,
                    cardmarket_url=normalised_url,
                )
                logger.info(
                    "ReviewCog: stored slug override fingerprint=%r slug=%r",
                    fingerprint.fingerprint_hash(), corrected_slug,
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
            fingerprint=fingerprint,
        )
        if learned_prefix is not None and effective_set_code:
            result_embed.add_field(
                name="🧠 Learned Pattern",
                value=(
                    f"Set `{effective_set_code}` uses prefix `{learned_prefix}` "
                    "before collector numbers. "
                    "Future cards from this set will use this rule automatically."
                ),
                inline=False,
            )

        # ── Extract URL slugs from the corrected URL ──────────────────────
        url_path = urlparse(normalised_url).path.rstrip("/")
        path_parts = url_path.split("/")
        product_slug: str | None = None
        set_slug: str | None = None
        try:
            singles_idx = path_parts.index("Singles")
            set_slug = path_parts[singles_idx + 1] if singles_idx + 1 < len(path_parts) else None
            product_slug = path_parts[-1] if len(path_parts) > singles_idx + 1 else None
        except (ValueError, IndexError):
            pass

        if product_slug or set_slug:
            slug_lines = []
            if product_slug:
                slug_lines.append(f"**Product slug:** `{product_slug}`")
            if set_slug:
                slug_lines.append(f"**Set slug:** `{set_slug}`")
            result_embed.add_field(
                name="🔖 URL slugs",
                value="\n".join(slug_lines),
                inline=False,
            )

        # If the idProduct was extracted automatically from the page HTML, store
        # the catalog mapping right away without requiring a user reply.
        auto_id_product: int | None = cm_data.id_product
        if auto_id_product is not None:
            id_expansion, mapping_lines = await self._lookup_catalog_for_id_product(
                auto_id_product, product_slug, set_slug
            )
            if mapping_lines:
                result_embed.add_field(
                    name="🗺️ Catalog mappings stored",
                    value="\n".join(mapping_lines),
                    inline=False,
                )
            result_embed.set_footer(text="Correction saved · catalog ID→slug stored automatically")
        else:
            result_embed.set_footer(text="Correction saved · step 1 of 2 – reply with idProduct to complete catalog mapping")

        try:
            bot_msg = await reply_message.reply(embed=result_embed, mention_author=False)
        except discord.HTTPException as exc:
            logger.error("ReviewCog: failed to post correction confirmation: %s", exc)
            bot_msg = None

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

        if auto_id_product is not None:
            # Store catalog mappings automatically (id_expansion already fetched above).
            await self.db.store_catalog_id_slugs(
                id_product=auto_id_product,
                product_slug=product_slug,
                id_expansion=id_expansion,
                set_slug=set_slug,
                cardmarket_url=normalised_url,
            )
            logger.info(
                "ReviewCog: correction auto-stored catalog mappings for '%s' "
                "idProduct=%d idExpansion=%s slug=%r set=%r (submitted by %s)",
                listing_title[:60], auto_id_product, id_expansion, product_slug, set_slug,
                submitted_by.display_name,
            )
        elif bot_msg is not None:
            # Ask the user to supply idProduct to complete the catalog mapping.
            try:
                prompt_msg = await bot_msg.reply(
                    "*(Optional)* Reply to **this message** with the Cardmarket **`idProduct`** "
                    f"for `{product_slug or normalised_url}` to store the catalog ID→slug mapping.\n"
                    "You can find `idProduct` in the Cardmarket page source or API response.",
                    mention_author=False,
                )
            except discord.HTTPException as exc:
                logger.error("ReviewCog: failed to send product-ID prompt for correction: %s", exc)
                prompt_msg = None

            if prompt_msg is not None:
                review_item_stub = {
                    "listing_id": listing_id,
                    "title": listing_title,
                    "url": listing_url,
                    "price": listing_price,
                    "currency": listing_currency,
                    "seller_name": error_item.get("listing_seller_name"),
                }
                self._pending_product_ids[prompt_msg.id] = {
                    "review_item": review_item_stub,
                    "cm_url": normalised_url,
                    "cm_data": cm_data,
                    "product_slug": product_slug,
                    "set_slug": set_slug,
                    "submitted_by": submitted_by,
                }

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
