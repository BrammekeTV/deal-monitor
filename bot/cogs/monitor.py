"""
bot/cogs/monitor.py
~~~~~~~~~~~~~~~~~~~
Background monitoring cog.

Every cycle (random interval between interval_min and interval_max seconds):
  1. For each configured search term, call VintedScraper.search().
  2. For each new listing:
     a. Check if already processed – skip if seen.
     b. Extract a card fingerprint from the title.
     c. Try Cardmarket Product Catalog lookup (if enabled):
        - Resolves price data directly from the S3 JSON files without any
          browser request.  Falls through to the legacy browser pipeline when
          the catalog does not contain the card.
     d. (Fallback) Look up a Cardmarket product URL via CardmarketResolver:
        - DB lookup (learned mapping)
        - Constructed URL from fingerprint
        - If neither works → send to review queue
     e. (Fallback) Scrape the Cardmarket product page via Flaresolverr / Playwright.
     f. Compare Vinted price against Cardmarket From price.
     g. If profitable → send profit alert to deals channel.
     h. If not profitable → skip silently.
     i. Mark listing as seen.
  3. On Cardmarket scraping error → log to error channel, mark seen, continue.
"""

from __future__ import annotations

import asyncio
import dataclasses
import random
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import discord
from discord.ext import commands
from camoufox.async_api import AsyncCamoufox

from config.settings import settings
from database.db import Database
from scraper.cardmarket import (
    CardmarketScrapeError,
    CardmarketScraper,
    CardmarketPriceData,
    contains_psa,
    extract_psa_grade,
    generate_variant_urls,
    validate_cardmarket_url,
)
from scraper.vinted import VintedScraper
from services.card_identifier import identify_card
from services.cardmarket_catalog import CardmarketCatalog
from services.cardmarket_resolver import CardmarketResolver
from services.price_comparison import compare_prices
from utils.card_analyzer import is_non_card_item
from utils.card_analyzer import CardAnalyzer, is_graded_listing, is_japanese_listing, is_lot_listing, has_pokemon_name
from utils.embed_builder import (
    build_error_embed,
    build_profit_alert_embed,
    build_status_embed,
    build_unidentified_embed,
)
from utils.logger import get_logger

if TYPE_CHECKING:
    from scraper.base import Listing

logger = get_logger(__name__)


class MonitorCog(commands.Cog, name="Monitor"):
    """Background Vinted monitoring with Cardmarket price comparison."""

    def __init__(self, bot: commands.Bot, db: Database) -> None:
        self.bot = bot
        self.db = db

        # Run-time stats
        self._listings_checked = 0
        self._listings_profitable = 0
        self._listings_reviewed = 0
        self._last_run: datetime | None = None
        self._next_run: datetime | None = None

        # Pause / resume
        self._paused: bool = False
        self._resume_event: asyncio.Event = asyncio.Event()
        self._resume_event.set()  # starts unpaused

        # Services (initialised in cog_load)
        self._vinted: VintedScraper | None = None
        self._cardmarket: CardmarketScraper | None = None
        self._resolver: CardmarketResolver | None = None
        self._catalog: CardmarketCatalog | None = None
        self._camoufox: AsyncCamoufox | None = None
        self._browser = None

        # Background task handle
        self._task: asyncio.Task | None = None

        # Persistent status message (issue #47): message ID updated each cycle.
        self._status_message_id: int | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def cog_load(self) -> None:
        """Start the monitoring loop when the cog is loaded."""
        # Initialise Vinted scraper.
        self._vinted = VintedScraper()
        await self._vinted.setup()

        # Initialise Camoufox (patched Firefox) browser for Cardmarket scraping.
        # Camoufox bypasses Cloudflare bot detection via a hardened Firefox binary
        # with randomised fingerprints; it returns a standard Playwright Browser.
        self._camoufox = AsyncCamoufox(
            headless=settings.headless,
            os="windows",
            locale="nl-NL",
        )
        self._browser = await self._camoufox.__aenter__()
        self._cardmarket = CardmarketScraper(self._browser)

        # Initialise resolver with DB.
        self._resolver = CardmarketResolver(self.db)
        await self._resolver.load()

        # Initialise Cardmarket Product Catalog service (if enabled).
        # The catalog resolves prices from S3 JSON files without any browser
        # request, replacing Flaresolverr / Playwright for the price-fetch step.
        if settings.catalog_enabled:
            self._catalog = CardmarketCatalog(
                cache_dir=Path(settings.catalog_cache_dir),
                refresh_hours=settings.catalog_refresh_hours,
            )
            try:
                await self._catalog.load()
                logger.info(
                    "MonitorCog: catalog loaded (%d products)",
                    self._catalog.product_count,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "MonitorCog: catalog load failed (will fall back to browser scraping): %s",
                    exc,
                )

        # Start background loop.
        self._task = asyncio.create_task(self._monitor_loop(), name="monitor_loop")
        logger.info("MonitorCog: started")

    async def cog_unload(self) -> None:
        """Clean up resources when the cog is unloaded."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        if self._vinted:
            await self._vinted.teardown()
        if self._camoufox:
            await self._camoufox.__aexit__(None, None, None)

        logger.info("MonitorCog: stopped")

    # ------------------------------------------------------------------
    # Public accessor (used by review cog and slash commands)
    # ------------------------------------------------------------------

    @property
    def resolver(self) -> CardmarketResolver | None:
        return self._resolver

    @property
    def catalog(self) -> CardmarketCatalog | None:
        return self._catalog

    @property
    def cardmarket_scraper(self) -> CardmarketScraper | None:
        return self._cardmarket

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def _monitor_loop(self) -> None:
        """Run the Vinted → Cardmarket monitoring loop indefinitely."""
        await self.bot.wait_until_ready()
        logger.info("MonitorCog: monitoring loop started")

        # Post initial status immediately so the status channel is cleared and
        # shows the bot is online.  Set _next_run to now so the embed displays
        # "starting now" instead of "Unknown" (issue #82).
        self._next_run = datetime.now(timezone.utc)
        try:
            await self._update_status_message()
        except Exception as exc:  # noqa: BLE001
            logger.warning("MonitorCog: failed to post initial status message: %s", exc)

        while not self.bot.is_closed():
            if self._paused:
                logger.debug("MonitorCog: paused – waiting for resume")
                self._resume_event.clear()
                try:
                    await self._update_status_message()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("MonitorCog: failed to update status message (paused): %s", exc)
                await self._resume_event.wait()
                # Update status immediately after resuming so the channel shows
                # "Running" rather than "Paused" (issue #81).
                try:
                    await self._update_status_message()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("MonitorCog: failed to update status message (resumed): %s", exc)
                continue

            # Update status at the start of each new cycle (issue #81).
            try:
                await self._update_status_message()
            except Exception as exc:  # noqa: BLE001
                logger.warning("MonitorCog: failed to update status message (cycle start): %s", exc)

            try:
                await self._run_cycle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "MonitorCog: unexpected error in monitoring loop: %s",
                    exc, exc_info=True,
                )
                # Wrap _send_error in its own try/except so a failure here
                # cannot crash the monitoring task (issue: bot not entering new run).
                try:
                    await self._send_error(
                        failure_step="monitor_loop",
                        error_message=str(exc),
                        stack_trace=traceback.format_exc(),
                    )
                except Exception as send_exc:  # noqa: BLE001
                    logger.error(
                        "MonitorCog: failed to send loop error notification: %s", send_exc
                    )

            # Calculate next run time before updating the status message so the
            # status embed always shows the correct upcoming schedule.
            delay = random.randint(settings.interval_min, settings.interval_max)
            self._next_run = datetime.now(timezone.utc) + timedelta(seconds=delay)

            # Update the persistent status message (if a status channel is set).
            try:
                await self._update_status_message()
            except Exception as exc:  # noqa: BLE001
                logger.warning("MonitorCog: failed to update status message: %s", exc)
            logger.debug("MonitorCog: sleeping %d seconds before next cycle", delay)
            await asyncio.sleep(delay)

    async def _run_cycle(self) -> None:
        """Execute one full scrape cycle over all configured search terms."""
        self._last_run = datetime.now(timezone.utc)
        logger.info("MonitorCog: starting scrape cycle")

        # Periodic catalog refresh (if enabled and stale).
        if self._catalog is not None:
            try:
                await self._catalog.refresh_if_stale()
            except Exception as exc:  # noqa: BLE001
                logger.warning("MonitorCog: catalog refresh failed: %s", exc)

        # Periodic review-queue expiry (once per day, when configured).
        expiry_days = int(
            getattr(
                settings,
                "pending_ttl_days",
                getattr(settings, "review_queue_expiry_days", 3),
            )
        )
        if expiry_days > 0:
            expired_count = await self.db.expire_old_review_items(expiry_days)
            if expired_count:
                logger.info(
                    "MonitorCog: expired %d stale review queue entries (>%d days)",
                    expired_count,
                    expiry_days,
                )

        for term in settings.search_terms:
            if self._paused:
                logger.info("MonitorCog: paused mid-cycle – aborting remaining search terms")
                return
            try:
                await self._process_search_term(term)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "MonitorCog: error processing term '%s': %s",
                    term, exc, exc_info=True,
                )

    async def _process_search_term(self, term: str) -> None:
        """Search Vinted for *term* and process each listing found."""
        logger.info("MonitorCog: searching Vinted for '%s'", term)
        async for listing in self._vinted.search(term, settings.results_per_term):  # type: ignore[union-attr]
            if self._paused:
                logger.info(
                    "MonitorCog: paused – stopping listing processing for term '%s'", term
                )
                return
            try:
                await self._process_listing(listing)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "MonitorCog: error processing listing '%s': %s",
                    listing.listing_id, exc, exc_info=True,
                )

    # ------------------------------------------------------------------
    # Listing processing pipeline
    # ------------------------------------------------------------------

    async def _process_listing(self, listing: "Listing") -> None:
        """Full pipeline for a single Vinted listing."""
        self._listings_checked += 1

        # ── 1. Deduplication ─────────────────────────────────────────────
        if await self.db.is_seen(listing.listing_id):
            logger.debug("MonitorCog: skipping already-seen listing %s", listing.listing_id)
            return

        logger.info(
            "MonitorCog: processing listing '%s' (€%.2f)",
            listing.title[:60], listing.price,
        )

        min_price = float(getattr(settings, "min_price_eur", 0.50))
        max_price = float(getattr(settings, "max_price_eur", 500.00))
        if listing.price < min_price or listing.price > max_price:
            logger.info(
                "MonitorCog: listing outside configured price range (%.2f-%.2f): €%.2f",
                min_price,
                max_price,
                listing.price,
            )
            await self.db.add_review_item(
                listing_id=listing.listing_id,
                title=listing.title,
                url=listing.url,
                price=listing.price,
                currency=listing.currency,
                seller_name=listing.seller_name,
                failure_reason="price_out_of_range",
                status="skipped",
            )
            await self.db.mark_seen(
                listing_id=listing.listing_id,
                title=listing.title,
                url=listing.url,
                price=listing.price,
                currency=listing.currency,
                seller_name=listing.seller_name,
                fingerprint=None,
            )
            return

        # ── 1b. Non-card pre-filter ───────────────────────────────────────
        # Skip and log non-TCG-card items (merchandise, accessories, etc.)
        if is_non_card_item(listing.title, listing.description or ""):
            logger.info(
                "MonitorCog: non-card item detected, skipping listing '%s'",
                listing.title[:60],
            )
            await self.db.add_review_item(
                listing_id=listing.listing_id,
                title=listing.title,
                url=listing.url,
                price=listing.price,
                currency=listing.currency,
                seller_name=listing.seller_name,
                failure_reason="non_card_item",
                status="skipped",
            )
            await self.db.mark_seen(
                listing_id=listing.listing_id,
                title=listing.title,
                url=listing.url,
                price=listing.price,
                currency=listing.currency,
                seller_name=listing.seller_name,
                fingerprint=None,
            )
            return

        # ── 1c. Lot / graded / Japanese pre-filters ───────────────────────
        if is_lot_listing(listing.title, listing.description or ""):
            # Check whether it's an extremely cheap bulk lot (≤ €0.01/card).
            # If so, forward it as a deal alert rather than discarding it.
            combined_text = listing.title + " " + (listing.description or "")
            count, _count_source = CardAnalyzer()._estimate_card_count(combined_text)
            if count and count > 0:
                price_per_card = listing.price / count
                if price_per_card <= 0.01:
                    logger.info(
                        "MonitorCog: cheap bulk lot detected (€%.4f/card, %d cards) for '%s'",
                        price_per_card, count, listing.title[:60],
                    )
                    await self._send_bulk_deal_alert(listing, count, price_per_card)
                    await self.db.mark_seen(
                        listing_id=listing.listing_id,
                        title=listing.title,
                        url=listing.url,
                        price=listing.price,
                        currency=listing.currency,
                        seller_name=listing.seller_name,
                        fingerprint=None,
                    )
                    return
            await self.db.add_review_item(
                listing_id=listing.listing_id,
                title=listing.title,
                url=listing.url,
                price=listing.price,
                currency=listing.currency,
                seller_name=listing.seller_name,
                failure_reason="lot_listing",
                status="skipped",
            )
            await self.db.mark_seen(
                listing_id=listing.listing_id,
                title=listing.title,
                url=listing.url,
                price=listing.price,
                currency=listing.currency,
                seller_name=listing.seller_name,
                fingerprint=None,
            )
            return

        if is_graded_listing(listing.title, listing.description or ""):
            await self.db.add_review_item(
                listing_id=listing.listing_id,
                title=listing.title,
                url=listing.url,
                price=listing.price,
                currency=listing.currency,
                seller_name=listing.seller_name,
                failure_reason="graded_listing",
                status="skipped",
            )
            await self.db.mark_seen(
                listing_id=listing.listing_id,
                title=listing.title,
                url=listing.url,
                price=listing.price,
                currency=listing.currency,
                seller_name=listing.seller_name,
                fingerprint=None,
            )
            return

        if is_japanese_listing(listing.title, listing.description or ""):
            await self.db.add_review_item(
                listing_id=listing.listing_id,
                title=listing.title,
                url=listing.url,
                price=listing.price,
                currency=listing.currency,
                seller_name=listing.seller_name,
                failure_reason="japanese_listing",
                status="skipped",
            )
            await self.db.mark_seen(
                listing_id=listing.listing_id,
                title=listing.title,
                url=listing.url,
                price=listing.price,
                currency=listing.currency,
                seller_name=listing.seller_name,
                fingerprint=None,
            )
            return

        # ── 1d. Pokemon name pre-filter ───────────────────────────────────
        # Skip listings whose title contains no recognizable Pokemon name.
        if not has_pokemon_name(listing.title):
            logger.debug(
                "MonitorCog: no Pokemon name found in '%s' – skipping",
                listing.title[:80],
            )
            await self.db.mark_seen(
                listing_id=listing.listing_id,
                title=listing.title,
                url=listing.url,
                price=listing.price,
                currency=listing.currency,
                seller_name=listing.seller_name,
                fingerprint=None,
            )
            return

        fingerprint = identify_card(listing.title, description=listing.description)

        # ── 2b. Unresolvable guard ────────────────────────────────────────
        # If we extracted no set info and no collector number, skip CM lookup.
        if (
            fingerprint.collector_number is None
            and fingerprint.set_name is None
            and fingerprint.set_code is None
        ):
            logger.info(
                "MonitorCog: unresolvable listing (no identifiers) '%s'",
                listing.title[:60],
            )
            await self._send_to_review(
                listing,
                fingerprint,
                [],
                failure_reason="unresolvable_no_identifiers",
                status="pending",
            )
            await self.db.mark_seen(
                listing_id=listing.listing_id,
                title=listing.title,
                url=listing.url,
                price=listing.price,
                currency=listing.currency,
                seller_name=listing.seller_name,
                fingerprint=fingerprint.fingerprint_hash(),
            )
            return

        # ── 2c. PSA detection ─────────────────────────────────────────────
        # Combine title and description to check for PSA grade.
        combined_text = listing.title + " " + (listing.description or "")
        psa_grade: int | None = None
        if contains_psa(combined_text):
            psa_grade = extract_psa_grade(combined_text)
            if psa_grade is not None:
                logger.info(
                    "MonitorCog: PSA %d detected in listing '%s'",
                    psa_grade, listing.title[:60],
                )
            else:
                logger.debug(
                    "MonitorCog: PSA keyword found but no grade number in '%s'",
                    listing.title[:60],
                )

        # ── 3. Cardmarket Product Catalog lookup (fast path) ──────────────
        # Try to resolve price data directly from the Cardmarket S3 Product
        # Catalog + Price Guide JSON files.  This avoids any browser request
        # (FlareSolverr / Playwright) and is therefore much faster and more
        # reliable.  PSA-graded listings are excluded because the catalog only
        # carries global market prices, not individual graded-card offers.
        matching_attempts: list[dict] = []

        if (
            self._catalog is not None
            and self._catalog.is_loaded
            and psa_grade is None
        ):
            catalog_price = self._catalog.find_and_get_price_data(
                card_name=fingerprint.card_name or "",
                collector_number=fingerprint.collector_number,
                set_name=fingerprint.set_name,
                set_code=fingerprint.set_code,
            )
            if catalog_price is not None and catalog_price.is_valid():
                logger.info(
                    "MonitorCog: catalog hit for '%s' → %s (€%.2f)",
                    listing.title[:60],
                    catalog_price.product_name,
                    catalog_price.from_price,
                )
                cm_data = CardmarketPriceData(
                    product_url=catalog_price.product_url or "",
                    product_name=catalog_price.product_name,
                    product_id=str(catalog_price.product_id),
                    from_price=catalog_price.from_price,
                    price_trend=catalog_price.price_trend,
                    avg_30_days=catalog_price.avg_30_days,
                    avg_7_days=catalog_price.avg_7_days,
                    avg_1_day=catalog_price.avg_1_day,
                    # Catalog prices are global (not Dutch-seller-filtered).
                    dutch_sellers_available=True,
                    set_name=catalog_price.set_name,
                    card_number=catalog_price.card_number,
                    id_expansion=catalog_price.id_expansion,
                )
                from services.cardmarket_resolver import ResolvedUrl  # noqa: PLC0415
                resolved = ResolvedUrl(
                    url=catalog_price.product_url or "",
                    source="catalog",
                    confidence=0.9,
                )
                # Store a mapping so future lookups can use the DB fast path.
                await self._resolver.store_mapping(  # type: ignore[union-attr]
                    fingerprint=fingerprint,
                    raw_title=listing.title,
                    cardmarket_url=cm_data.product_url,
                    product_name=cm_data.product_name,
                    product_id=cm_data.product_id,
                    validated_by="catalog",
                    confidence=resolved.confidence,
                    listing_url=listing.url,
                    seller_name=listing.seller_name,
                    price=listing.price,
                )
                # Jump straight to price comparison (skip URL resolve + scrape).
                comparison = compare_prices(listing, cm_data)
                if comparison.is_profitable:
                    self._listings_profitable += 1
                    await self._send_profit_alert(
                        listing, cm_data, comparison, resolved, fingerprint
                    )
                else:
                    logger.info(
                        "MonitorCog: not profitable – Vinted €%.2f vs CM €%.2f for '%s'",
                        comparison.vinted_price,
                        comparison.cardmarket_from_price,
                        listing.title[:60],
                    )
                    await self._send_identified_not_profitable(
                        listing, cm_data, comparison, resolved, fingerprint
                    )
                await self.db.mark_seen(
                    listing_id=listing.listing_id,
                    title=listing.title,
                    url=listing.url,
                    price=listing.price,
                    currency=listing.currency,
                    seller_name=listing.seller_name,
                    fingerprint=fingerprint.fingerprint_hash(),
                )
                return

        # ── 4. Cardmarket URL resolution ──────────────────────────────────
        # First try the learning database.  When the fingerprint contains a
        # full set of identifiers (name + set + number) we also attempt
        # URL construction so that well-identified cards are matched
        # automatically and posted to the correct-matches channel rather
        # than always landing in the unidentified channel.
        _has_full_fingerprint = bool(
            fingerprint.card_name
            and (fingerprint.set_code or fingerprint.set_name)
            and fingerprint.collector_number
        )
        if _has_full_fingerprint:
            resolved = await self._resolver.resolve(fingerprint, listing.title)  # type: ignore[union-attr]
        else:
            resolved = self._resolver.resolve_db_only(fingerprint, listing.title)  # type: ignore[union-attr]

        if resolved is None:
            # ── 4a. Cardmarket search training ────────────────────────────
            # When training mode is enabled, search Cardmarket directly and
            # store any correct match found so future lookups skip this step.
            if settings.cardmarket_search_training and self._cardmarket is not None:
                trained = await self._try_cardmarket_search_training(listing, fingerprint)
                if trained:
                    return
            # Not found in catalog or DB → ask user to identify it.
            await self._send_to_unidentified(listing, fingerprint)
            await self.db.mark_seen(
                listing_id=listing.listing_id,
                title=listing.title,
                url=listing.url,
                price=listing.price,
                currency=listing.currency,
                seller_name=listing.seller_name,
                fingerprint=fingerprint.fingerprint_hash(),
            )
            return

        # ── 4b. URL validation (for constructed URLs only) ────────────────
        if resolved.needs_validation:
            url_ok = await validate_cardmarket_url(resolved.url)
            if not url_ok:
                # Try V1/V2 variant URLs before giving up.
                for variant_url in generate_variant_urls(resolved.url):
                    if await validate_cardmarket_url(variant_url):
                        logger.info(
                            "MonitorCog: primary URL invalid, using variant: %s",
                            variant_url,
                        )
                        resolved = dataclasses.replace(resolved, url=variant_url)
                        url_ok = True
                        break

            if not url_ok:
                logger.warning(
                    "MonitorCog: constructed URL returned 404, sending to review: %s",
                    resolved.url,
                )
                await self._send_to_review(
                    listing,
                    fingerprint,
                    matching_attempts,
                    failure_reason=(
                        f"Constructed Cardmarket URL returned 404: {resolved.url}"
                    ),
                )
                await self.db.mark_seen(
                    listing_id=listing.listing_id,
                    title=listing.title,
                    url=listing.url,
                    price=listing.price,
                    currency=listing.currency,
                    seller_name=listing.seller_name,
                    fingerprint=fingerprint.fingerprint_hash(),
                )
                return

        # ── 5. Cardmarket scraping ────────────────────────────────────────
        try:
            cm_data = await self._cardmarket.scrape_url(resolved.url)  # type: ignore[union-attr]
        except CardmarketScrapeError as exc:
            logger.warning(
                "MonitorCog: Cardmarket scrape failed for '%s': %s",
                listing.title[:60], exc,
            )
            # When the page loaded but had no pricing data, try V1-V10 variant
            # URLs before giving up (e.g. Fennekin-MEP080 → Fennekin-V1-MEP080).
            if "no pricing data" in exc.message.lower():
                for variant_url in generate_variant_urls(resolved.url):
                    try:
                        cm_data = await self._cardmarket.scrape_url(variant_url)  # type: ignore[union-attr]
                        logger.info(
                            "MonitorCog: no pricing on primary URL, variant succeeded: %s",
                            variant_url,
                        )
                        resolved = dataclasses.replace(resolved, url=variant_url)
                        break
                    except CardmarketScrapeError:
                        continue
                else:
                    cm_data = None
            else:
                cm_data = None

            if cm_data is None:
                # Log error and abort – do NOT crash the loop or send to review.
                error_log_id = await self.db.log_error(
                    listing_id=listing.listing_id,
                    listing_title=listing.title,
                    listing_url=listing.url,
                    listing_price=listing.price,
                    listing_currency=listing.currency,
                    listing_seller_name=listing.seller_name,
                    cardmarket_url=resolved.url,
                    failure_step=exc.step,
                    http_status=exc.http_status,
                    error_message=exc.message,
                    stack_trace=exc.stack_trace,
                )
                await self._send_error(
                    failure_step=exc.step,
                    error_message=exc.message,
                    listing_title=listing.title,
                    listing_url=listing.url,
                    cardmarket_url=resolved.url,
                    http_status=exc.http_status,
                    stack_trace=exc.stack_trace,
                    fingerprint=fingerprint,
                    error_log_id=error_log_id,
                )
                # Mark as seen so we don't retry every cycle.
                await self.db.mark_seen(
                    listing_id=listing.listing_id,
                    title=listing.title,
                    url=listing.url,
                    price=listing.price,
                    currency=listing.currency,
                    seller_name=listing.seller_name,
                    fingerprint=fingerprint.fingerprint_hash(),
                )
                return

        # ── 5b. PSA-specific listing price ────────────────────────────────
        # For PSA 9 and PSA 10 listings, attempt to find the price of the
        # first Cardmarket offer that has an MT (Mint) condition badge and a
        # description that matches the PSA grade exactly.  This replaces the
        # generic "From" price for a more accurate comparison.
        if psa_grade is not None and psa_grade >= 9:
            psa_price = await self._cardmarket.scrape_psa_listing_price(  # type: ignore[union-attr]
                resolved.url, psa_grade
            )
            if psa_price is not None:
                from dataclasses import replace as _dc_replace  # noqa: PLC0415
                cm_data = _dc_replace(cm_data, from_price=psa_price)
                logger.info(
                    "MonitorCog: using PSA %d MT listing price €%.2f for '%s'",
                    psa_grade, psa_price, listing.title[:60],
                )
            else:
                logger.info(
                    "MonitorCog: no PSA %d MT listing found on Cardmarket – "
                    "falling back to standard From price for '%s'",
                    psa_grade, listing.title[:60],
                )

        # ── 5c. Enrich idExpansion for browser-scraped constructed matches ──
        # The catalog (S3 JSON) carries idExpansion; the browser scraper does
        # not. Populate it so embeds and catalog mappings can show the value.
        if cm_data.id_expansion is None and resolved.source == "constructed":
            _id_exp: int | None = None
            # Try catalog lookup by product_id first (no extra DB round-trip).
            if self._catalog is not None and cm_data.product_id:
                try:
                    _cat_prod = self._catalog.get_product_by_id(int(cm_data.product_id))
                    if _cat_prod:
                        _id_exp = _cat_prod.get("idExpansion")
                except (ValueError, TypeError):
                    pass
            # Fall back to DB lookup by set slug.
            if _id_exp is None:
                _id_exp = await self._lookup_expansion_id(fingerprint)
            if _id_exp is not None:
                cm_data = dataclasses.replace(cm_data, id_expansion=_id_exp)
                logger.info(
                    "MonitorCog: enriched idExpansion=%d for constructed match '%s'",
                    _id_exp, listing.title[:60],
                )

        # ── 6. Price comparison ───────────────────────────────────────────
        comparison = compare_prices(listing, cm_data)

        # ── 7. Store mapping if auto-constructed (now validated) ──────────
        if resolved.source == "constructed":
            await self._resolver.store_mapping(  # type: ignore[union-attr]
                fingerprint=fingerprint,
                raw_title=listing.title,
                cardmarket_url=cm_data.product_url,
                product_name=cm_data.product_name,
                product_id=cm_data.product_id,
                validated_by="auto",
                confidence=resolved.confidence,
                listing_url=listing.url,
                seller_name=listing.seller_name,
                price=listing.price,
            )
            # Also persist catalog ID→slug mappings when idExpansion is known,
            # so future _lookup_expansion_id calls hit the DB without needing
            # another catalog scan.
            if cm_data.product_id and cm_data.id_expansion is not None:
                import re as _re  # noqa: PLC0415
                _url_path = cm_data.product_url.rstrip("/")
                _product_slug = _url_path.split("/")[-1] if _url_path else None
                _set_slug: str | None = None
                if fingerprint.set_name:
                    _raw_slug = fingerprint.set_name.replace("é", "e").replace("É", "E")
                    _set_slug = _re.sub(r"[^A-Za-z0-9]+", "-", _raw_slug).strip("-") or None
                elif fingerprint.set_code:
                    _set_slug = fingerprint.set_code
                try:
                    await self.db.store_catalog_id_slugs(
                        id_product=int(cm_data.product_id),
                        product_slug=_product_slug,
                        id_expansion=cm_data.id_expansion,
                        set_slug=_set_slug,
                        cardmarket_url=cm_data.product_url,
                    )
                    logger.info(
                        "MonitorCog: stored catalog ID→slug for constructed match "
                        "idProduct=%s idExpansion=%d slug=%r set=%r",
                        cm_data.product_id, cm_data.id_expansion,
                        _product_slug, _set_slug,
                    )
                except Exception as _exc:  # noqa: BLE001
                    logger.debug(
                        "MonitorCog: could not store catalog_id_slugs for %s: %s",
                        cm_data.product_url, _exc,
                    )

        # ── 8. Post profit alert or skip ──────────────────────────────────
        if comparison.is_profitable:
            self._listings_profitable += 1
            await self._send_profit_alert(listing, cm_data, comparison, resolved, fingerprint)
        else:
            logger.info(
                "MonitorCog: not profitable – Vinted €%.2f vs CM €%.2f for '%s'",
                comparison.vinted_price,
                comparison.cardmarket_from_price,
                listing.title[:60],
            )
            await self._send_identified_not_profitable(listing, cm_data, comparison, resolved, fingerprint)

        # ── 9. Mark as seen ───────────────────────────────────────────────
        await self.db.mark_seen(
            listing_id=listing.listing_id,
            title=listing.title,
            url=listing.url,
            price=listing.price,
            currency=listing.currency,
            seller_name=listing.seller_name,
            fingerprint=fingerprint.fingerprint_hash(),
        )

    # ------------------------------------------------------------------
    # Cardmarket search training
    # ------------------------------------------------------------------

    async def _try_cardmarket_search_training(
        self,
        listing: "Listing",
        fingerprint,
    ) -> bool:
        """Search Cardmarket for the card and process the result if it matches.

        Returns ``True`` when the listing was fully handled (match found,
        stored, and price comparison performed), ``False`` when no valid
        match was found (caller should send to unidentified channel).
        """
        from rapidfuzz import fuzz  # noqa: PLC0415
        from services.cardmarket_resolver import ResolvedUrl  # noqa: PLC0415

        # Build a search query from available fingerprint components.
        query_parts = []
        if fingerprint.card_name:
            query_parts.append(fingerprint.card_name)
        if fingerprint.set_name:
            query_parts.append(fingerprint.set_name)
        elif fingerprint.set_code:
            query_parts.append(fingerprint.set_code)
        if fingerprint.collector_number:
            query_parts.append(fingerprint.collector_number.split("/")[0])
        query = " ".join(query_parts).strip()
        if not query:
            return False

        logger.info(
            "MonitorCog: training – searching Cardmarket for '%s'",
            query,
        )

        product_url = await self._cardmarket.search_product_url(query)  # type: ignore[union-attr]
        if not product_url:
            logger.info(
                "MonitorCog: training – no Cardmarket URL found for '%s'",
                listing.title[:60],
            )
            return False

        # Scrape the found product page to get price data.
        try:
            cm_data = await self._cardmarket.scrape_url(product_url)  # type: ignore[union-attr]
        except CardmarketScrapeError as exc:
            logger.warning(
                "MonitorCog: training – scrape failed for %s: %s",
                product_url, exc.message,
            )
            return False

        # Validate: the scraped card name must match the fingerprint card name.
        if fingerprint.card_name and cm_data.product_name:
            name_score = fuzz.token_set_ratio(
                fingerprint.card_name.lower(),
                cm_data.product_name.lower(),
            )
            if name_score < settings.cardmarket_fuzzy_threshold:
                logger.info(
                    "MonitorCog: training – name mismatch (score=%.1f) for '%s' vs '%s'",
                    name_score,
                    fingerprint.card_name,
                    cm_data.product_name,
                )
                return False

        # When the fingerprint has a collector number, verify it matches the
        # card number on the scraped Cardmarket page.
        if fingerprint.collector_number and cm_data.card_number:
            fp_num = fingerprint.collector_number.split("/")[0].strip().lstrip("0") or "0"
            cm_num = cm_data.card_number.split("/")[0].strip().lstrip("0") or "0"
            if fp_num != cm_num:
                logger.info(
                    "MonitorCog: training – collector number mismatch (%s vs %s) "
                    "for '%s'",
                    fingerprint.collector_number,
                    cm_data.card_number,
                    listing.title[:60],
                )
                return False

        logger.info(
            "MonitorCog: training – correct match found for '%s' → %s",
            listing.title[:60],
            cm_data.product_url,
        )

        # Store the mapping so future lookups use the DB fast path.
        resolved = ResolvedUrl(
            url=cm_data.product_url,
            source="training",
            confidence=0.85,
        )
        await self._resolver.store_mapping(  # type: ignore[union-attr]
            fingerprint=fingerprint,
            raw_title=listing.title,
            cardmarket_url=cm_data.product_url,
            product_name=cm_data.product_name,
            product_id=cm_data.product_id,
            validated_by="training",
            confidence=resolved.confidence,
            listing_url=listing.url,
            seller_name=listing.seller_name,
            price=listing.price,
        )

        # Perform price comparison and notify via the appropriate channel.
        comparison = compare_prices(listing, cm_data)
        if comparison.is_profitable:
            self._listings_profitable += 1
            await self._send_profit_alert(listing, cm_data, comparison, resolved, fingerprint)
        else:
            logger.info(
                "MonitorCog: training – not profitable – Vinted €%.2f vs CM €%.2f for '%s'",
                comparison.vinted_price,
                comparison.cardmarket_from_price,
                listing.title[:60],
            )
            await self._send_identified_not_profitable(
                listing, cm_data, comparison, resolved, fingerprint
            )

        await self.db.mark_seen(
            listing_id=listing.listing_id,
            title=listing.title,
            url=listing.url,
            price=listing.price,
            currency=listing.currency,
            seller_name=listing.seller_name,
            fingerprint=fingerprint.fingerprint_hash(),
        )
        return True

    # ------------------------------------------------------------------
    # Review queue
    # ------------------------------------------------------------------

    async def _send_to_review(
        self,
        listing: "Listing",
        fingerprint,
        matching_attempts: list[dict],
        *,
        failure_reason: str | None = None,
        status: str = "pending",
    ) -> None:
        """Send a listing to the review queue Discord channel."""
        from utils.embed_builder import build_review_embed

        self._listings_reviewed += 1

        # Determine failure reason (caller can override with explicit message).
        if failure_reason is None:
            if fingerprint.is_identifiable:
                failure_reason = (
                    f"Card fingerprint extracted ('{fingerprint.normalised_key()}') "
                    f"but no Cardmarket URL could be built. "
                    f"Set code '{fingerprint.set_code}' may not be in the known mapping table."
                )
            elif fingerprint.card_name:
                failure_reason = (
                    f"Card name '{fingerprint.card_name}' extracted but no set information found. "
                    "Insufficient data to construct a Cardmarket URL."
                )
            else:
                failure_reason = (
                    "Could not extract card name from listing title. "
                    "Title may be too generic or non-standard."
                )

        # Add to DB review queue.
        await self.db.add_review_item(
            listing_id=listing.listing_id,
            title=listing.title,
            url=listing.url,
            price=listing.price,
            currency=listing.currency,
            seller_name=listing.seller_name,
            description=listing.description,
            images=listing.images,
            fingerprint=fingerprint.fingerprint_hash(),
            failure_reason=failure_reason,
            matching_attempts=matching_attempts,
            status=status,
        )

        if status != "pending":
            return

        # Post to review channel.
        review_channel = self._get_review_channel()
        if review_channel is None:
            logger.debug(
                "MonitorCog: review channel not configured – skipping review post for %s",
                listing.listing_id,
            )
            return

        # Look up mapped expansion ID for the identified set (if any).
        id_expansion = await self._lookup_expansion_id(fingerprint)

        embed = build_review_embed(
            listing,
            fingerprint=fingerprint,
            failure_reason=failure_reason,
            matching_attempts=matching_attempts,
            id_expansion=id_expansion,
        )

        try:
            msg = await review_channel.send(embed=embed)
            await self.db.set_review_discord_message(
                listing.listing_id, str(msg.id), str(review_channel.id)
            )
            logger.info(
                "MonitorCog: sent listing %s to review (msg %s)",
                listing.listing_id, msg.id,
            )
        except discord.HTTPException as exc:
            logger.error(
                "MonitorCog: failed to post review message for %s: %s",
                listing.listing_id, exc,
            )

    async def _send_to_unidentified(
        self,
        listing: "Listing",
        fingerprint,
    ) -> None:
        """Send a listing to the unidentified channel.

        Called when a listing passes all filters but is not found in the
        Cardmarket product catalog or the learning database.  A user can
        reply with the correct Cardmarket URL to identify it.
        """
        failure_reason = "not_in_catalog_or_db"

        # Add to DB review queue so review.py can process the reply.
        await self.db.add_review_item(
            listing_id=listing.listing_id,
            title=listing.title,
            url=listing.url,
            price=listing.price,
            currency=listing.currency,
            seller_name=listing.seller_name,
            description=listing.description,
            images=listing.images,
            fingerprint=fingerprint.fingerprint_hash(),
            failure_reason=failure_reason,
            matching_attempts=[],
            status="pending",
        )

        channel = self._get_unidentified_channel()
        if channel is None:
            logger.debug(
                "MonitorCog: unidentified channel not configured – skipping post for %s",
                listing.listing_id,
            )
            return

        # Look up mapped expansion ID for the identified set (if any).
        id_expansion = await self._lookup_expansion_id(fingerprint)

        embed = build_unidentified_embed(listing, fingerprint=fingerprint, id_expansion=id_expansion)

        try:
            msg = await channel.send(embed=embed)
            await self.db.set_review_discord_message(
                listing.listing_id, str(msg.id), str(channel.id)
            )
            logger.info(
                "MonitorCog: sent listing %s to unidentified channel (msg %s)",
                listing.listing_id, msg.id,
            )
        except discord.HTTPException as exc:
            logger.error(
                "MonitorCog: failed to post unidentified message for %s: %s",
                listing.listing_id, exc,
            )

    # ------------------------------------------------------------------
    # Discord posting helpers
    # ------------------------------------------------------------------

    async def _lookup_expansion_id(self, fingerprint) -> int | None:
        """Return the mapped idExpansion for the fingerprint's set, or None.

        Converts the fingerprint's set name (or set code) to a Cardmarket
        set slug and looks it up in the catalog_id_slugs table.
        """
        import re

        set_name = getattr(fingerprint, "set_name", None)
        if not set_name:
            return None
        # Replicate the slug conversion from cardmarket_catalog._expansion_name_to_slug
        slug = set_name.replace("é", "e").replace("É", "E")
        slug = re.sub(r"[^A-Za-z0-9]+", "-", slug).strip("-")
        if not slug:
            return None
        try:
            row = await self.db.find_catalog_id_by_set_slug(slug)
            if row:
                return row.get("id_expansion")
        except Exception as exc:  # noqa: BLE001
            logger.debug("MonitorCog: expansion ID lookup failed for slug '%s': %s", slug, exc)
        return None

    async def _send_bulk_deal_alert(
        self,
        listing: "Listing",
        card_count: int,
        price_per_card: float,
    ) -> None:
        """Post a bulk-lot deal embed to the deals channel."""
        channel = self._get_deals_channel()
        if channel is None:
            return

        embed = discord.Embed(
            title="📦 Cheap Bulk Lot!",
            colour=discord.Colour.gold(),
        )
        embed.add_field(name="Listing", value=f"[{listing.title[:80]}]({listing.url})", inline=False)
        embed.add_field(name="Price", value=f"€{listing.price:.2f}", inline=True)
        embed.add_field(name="Est. cards", value=str(card_count), inline=True)
        embed.add_field(name="Price/card", value=f"€{price_per_card:.4f}", inline=True)
        if listing.seller_name:
            embed.set_footer(text=f"Seller: {listing.seller_name}")

        try:
            await channel.send(embed=embed)
        except discord.HTTPException as exc:
            logger.error(
                "MonitorCog: failed to post bulk deal alert for %s: %s",
                listing.listing_id, exc,
            )

    async def _send_profit_alert(
        self,
        listing: "Listing",
        cm_data,
        comparison,
        resolved,
        fingerprint=None,
    ) -> None:
        """Post a profit alert embed to the deals channel."""
        embed = build_profit_alert_embed(
            listing,
            cm_data,
            comparison,
            match_confidence=resolved.confidence,
            match_source=resolved.source,
            fingerprint=fingerprint,
        )
        channel = self._get_deals_channel()
        if channel:
            try:
                await channel.send(embed=embed)
            except discord.HTTPException as exc:
                logger.error(
                    "MonitorCog: failed to post profit alert for %s: %s",
                    listing.listing_id, exc,
                )

    async def _send_identified_not_profitable(
        self,
        listing: "Listing",
        cm_data,
        comparison,
        resolved,
        fingerprint=None,
    ) -> None:
        """Post a non-profitable identified listing embed to the match channel."""
        from utils.embed_builder import build_not_profitable_embed

        channel = self._get_match_channel()
        if channel is None:
            return

        embed = build_not_profitable_embed(listing, cm_data, comparison, fingerprint=fingerprint)
        try:
            await channel.send(embed=embed)
        except discord.HTTPException as exc:
            logger.error(
                "MonitorCog: failed to post identified-not-profitable embed for %s: %s",
                listing.listing_id, exc,
            )

    async def _send_error(
        self,
        *,
        failure_step: str,
        error_message: str,
        listing_title: str | None = None,
        listing_url: str | None = None,
        cardmarket_url: str | None = None,
        http_status: int | None = None,
        stack_trace: str | None = None,
        fingerprint=None,
        error_log_id: int | None = None,
    ) -> None:
        """Post a structured error embed to the log channel.

        When *error_log_id* is provided, the Discord message ID is written back
        to the error_log row so that users can reply to the message with the
        correct Cardmarket URL.
        """
        # Look up mapped expansion ID for the identified set (if any).
        id_expansion = await self._lookup_expansion_id(fingerprint) if fingerprint else None

        embed = build_error_embed(
            listing_title=listing_title,
            listing_url=listing_url,
            cardmarket_url=cardmarket_url,
            failure_step=failure_step,
            error_message=error_message,
            http_status=http_status,
            stack_trace=stack_trace,
            fingerprint=fingerprint,
            id_expansion=id_expansion,
        )
        channel = self._get_log_channel()
        if channel:
            try:
                msg = await channel.send(embed=embed)
                if error_log_id is not None:
                    await self.db.update_error_message_id(error_log_id, str(msg.id))
            except discord.HTTPException as exc:
                logger.error("MonitorCog: failed to post error embed: %s", exc)

    def _get_deals_channel(self) -> discord.TextChannel | None:
        ch_id = settings.discord_channel_id
        if not ch_id:
            return None
        ch = self.bot.get_channel(ch_id)
        if not isinstance(ch, discord.TextChannel):
            return None
        return ch

    def _get_match_channel(self) -> discord.TextChannel | None:
        ch_id = settings.discord_match_channel_id
        if not ch_id:
            return None
        ch = self.bot.get_channel(ch_id)
        if not isinstance(ch, discord.TextChannel):
            return None
        return ch

    def _get_review_channel(self) -> discord.TextChannel | None:
        ch_id = settings.discord_review_channel_id
        if not ch_id:
            return None
        ch = self.bot.get_channel(ch_id)
        if not isinstance(ch, discord.TextChannel):
            return None
        return ch

    def _get_unidentified_channel(self) -> discord.TextChannel | None:
        ch_id = settings.discord_unidentified_channel_id
        if not ch_id:
            return None
        ch = self.bot.get_channel(ch_id)
        if not isinstance(ch, discord.TextChannel):
            return None
        return ch

    def _get_log_channel(self) -> discord.TextChannel | None:
        ch_id = settings.discord_log_channel_id
        if not ch_id:
            return None
        ch = self.bot.get_channel(ch_id)
        if not isinstance(ch, discord.TextChannel):
            return None
        return ch

    def _get_status_channel(self) -> discord.TextChannel | None:
        ch_id = settings.discord_status_channel_id
        if not ch_id:
            return None
        ch = self.bot.get_channel(ch_id)
        if not isinstance(ch, discord.TextChannel):
            return None
        return ch

    async def _update_status_message(self) -> None:
        """Post or edit the persistent status embed in the status channel.

        If ``discord_status_channel_id`` is not configured this is a no-op.
        On the first call a new message is sent and its ID is stored in
        ``_status_message_id``.  Subsequent calls edit that message so the
        channel always shows a single up-to-date status embed.
        """
        channel = self._get_status_channel()
        if channel is None:
            return

        mappings = await self.db.get_all_mappings()
        embed = build_status_embed(
            listings_checked=self._listings_checked,
            listings_profitable=self._listings_profitable,
            listings_reviewed=self._listings_reviewed,
            mappings_count=len(mappings),
            last_run=self._last_run,
            next_run=self._next_run,
            search_terms=settings.search_terms,
            paused=self._paused,
        )

        if self._status_message_id is not None:
            try:
                msg = await channel.fetch_message(self._status_message_id)
                await msg.edit(embed=embed)
                return
            except discord.NotFound:
                # Message was deleted; fall through to send a new one.
                self._status_message_id = None
            except discord.HTTPException as exc:
                logger.warning("MonitorCog: could not edit status message: %s", exc)
                return

        # Clear the channel before posting a fresh status message so there is
        # always exactly one status embed in the channel.
        try:
            await channel.purge(limit=None)
        except discord.HTTPException as exc:
            logger.warning("MonitorCog: could not purge status channel: %s", exc)

        msg = await channel.send(embed=embed)
        self._status_message_id = msg.id
        logger.info("MonitorCog: status message created (id=%d)", msg.id)



    @discord.app_commands.command(name="status", description="Show monitoring bot status")
    async def status_command(self, interaction: discord.Interaction) -> None:
        mappings = await self.db.get_all_mappings()
        embed = build_status_embed(
            listings_checked=self._listings_checked,
            listings_profitable=self._listings_profitable,
            listings_reviewed=self._listings_reviewed,
            mappings_count=len(mappings),
            last_run=self._last_run,
            next_run=self._next_run,
            search_terms=settings.search_terms,
            paused=self._paused,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.app_commands.command(name="pause", description="Pause the monitoring loop")
    @discord.app_commands.default_permissions(administrator=True)
    async def pause_command(self, interaction: discord.Interaction) -> None:
        if self._paused:
            await interaction.response.send_message(
                "⏸️ Monitoring is already paused.", ephemeral=True
            )
        else:
            self._paused = True
            self._resume_event.clear()
            logger.info("MonitorCog: monitoring paused by %s", interaction.user)
            await interaction.response.send_message(
                "⏸️ Monitoring **paused**. Use `/resume` to start searches again.",
                ephemeral=True,
            )

    @discord.app_commands.command(name="resume", description="Resume the monitoring loop")
    @discord.app_commands.default_permissions(administrator=True)
    async def resume_command(self, interaction: discord.Interaction) -> None:
        if not self._paused:
            await interaction.response.send_message(
                "▶️ Monitoring is already running.", ephemeral=True
            )
        else:
            self._paused = False
            self._resume_event.set()
            logger.info("MonitorCog: monitoring resumed by %s", interaction.user)
            await interaction.response.send_message(
                "▶️ Monitoring **resumed**. Searches will continue on the next cycle.",
                ephemeral=True,
            )

    @discord.app_commands.command(
        name="run_cycle",
        description="Pause the bot, run one scrape cycle, then pause again",
    )
    @discord.app_commands.default_permissions(administrator=True)
    async def run_cycle_command(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        # Pause the bot (stop the background loop from starting a new cycle).
        was_paused = self._paused
        self._paused = True
        self._resume_event.clear()
        logger.info(
            "MonitorCog: run_cycle triggered by %s (was_paused=%s)",
            interaction.user, was_paused,
        )

        try:
            await self._run_cycle()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("MonitorCog: run_cycle command failed: %s", exc, exc_info=True)
            await interaction.followup.send(
                f"❌ Cycle failed with error: `{exc}`\nThe bot remains **paused**.",
                ephemeral=True,
            )
            return

        logger.info("MonitorCog: run_cycle completed – bot remains paused")
        await interaction.followup.send(
            "✅ Cycle complete. The bot is now **paused**. Use `/resume` to continue.",
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /check_listing — on-demand profitability check
    # ------------------------------------------------------------------

    @discord.app_commands.command(
        name="check_listing",
        description="Check a Vinted listing URL for profitability and Cardmarket data",
    )
    @discord.app_commands.describe(
        url="Vinted listing URL (e.g. https://www.vinted.nl/items/1234567890-card-name)"
    )
    async def check_listing_command(
        self, interaction: discord.Interaction, url: str
    ) -> None:
        """Fetch a Vinted listing and run the full analysis pipeline on demand."""
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.NotFound:
            # The 3-second Discord acknowledgment window already expired (the
            # event loop was busy with a scan cycle).  Nothing we can do – the
            # interaction token is gone.  Log and exit silently; Discord will
            # show "The application did not respond" to the user.
            logger.warning(
                "MonitorCog: /check_listing interaction expired before defer() "
                "could be sent (Unknown Interaction 10062) – user: %s, url: %s",
                interaction.user, url,
            )
            return

        if "vinted." not in url.lower():
            await interaction.followup.send(
                "❌ Please provide a valid Vinted listing URL "
                "(e.g. `https://www.vinted.nl/items/1234567890-card-name`).",
                ephemeral=True,
            )
            return

        if self._vinted is None:
            await interaction.followup.send(
                "❌ The Vinted scraper is not initialised yet. Try again in a moment.",
                ephemeral=True,
            )
            return

        logger.info(
            "MonitorCog: /check_listing requested by %s for URL: %s",
            interaction.user, url,
        )

        listing = await self._vinted.get_listing(url)
        if listing is None:
            await interaction.followup.send(
                f"❌ Could not fetch listing from: `{url}`\n"
                "Make sure it is a direct Vinted item URL "
                "(e.g. `https://www.vinted.nl/items/1234567890-card-name`).",
                ephemeral=True,
            )
            return

        embed = await self._check_listing_pipeline(listing)
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _check_listing_pipeline(self, listing: "Listing") -> discord.Embed:
        """Run the full analysis pipeline for a single listing and return a result embed.

        Mirrors :meth:`_process_listing` – when a listing needs review or cannot
        be identified, it is posted to the appropriate Discord channel and marked
        as seen, exactly as the automated scanner would do.
        """
        from utils.embed_builder import build_check_listing_embed  # noqa: PLC0415
        from services.cardmarket_resolver import ResolvedUrl  # noqa: PLC0415

        # ── Pre-filters (same order as _process_listing) ──────────────────
        min_price = float(getattr(settings, "min_price_eur", 0.50))
        max_price = float(getattr(settings, "max_price_eur", 500.00))
        if listing.price < min_price or listing.price > max_price:
            return build_check_listing_embed(
                listing,
                filter_reason="price_out_of_range",
                filter_detail=(
                    f"Price €{listing.price:.2f} is outside the configured range "
                    f"(€{min_price:.2f} – €{max_price:.2f})."
                ),
            )

        if is_non_card_item(listing.title, listing.description or ""):
            return build_check_listing_embed(listing, filter_reason="non_card_item")

        if is_lot_listing(listing.title, listing.description or ""):
            return build_check_listing_embed(listing, filter_reason="lot_listing")

        if is_graded_listing(listing.title, listing.description or ""):
            return build_check_listing_embed(listing, filter_reason="graded_listing")

        if is_japanese_listing(listing.title, listing.description or ""):
            return build_check_listing_embed(listing, filter_reason="japanese_listing")

        if not has_pokemon_name(listing.title):
            return build_check_listing_embed(listing, filter_reason="no_pokemon_name")

        # ── Fingerprinting ─────────────────────────────────────────────────
        fingerprint = identify_card(listing.title, description=listing.description)

        if (
            fingerprint.collector_number is None
            and fingerprint.set_name is None
            and fingerprint.set_code is None
        ):
            # Send to review channel (same as _process_listing).
            await self._send_to_review(
                listing,
                fingerprint,
                [],
                failure_reason="unresolvable_no_identifiers",
                status="pending",
            )
            await self.db.mark_seen(
                listing_id=listing.listing_id,
                title=listing.title,
                url=listing.url,
                price=listing.price,
                currency=listing.currency,
                seller_name=listing.seller_name,
                fingerprint=fingerprint.fingerprint_hash(),
            )
            embed = build_check_listing_embed(
                listing,
                fingerprint=fingerprint,
                filter_reason="unresolvable_no_identifiers",
            )
            embed.add_field(
                name="📨 Action Taken",
                value="This listing has been posted to the **review channel** for manual identification.",
                inline=False,
            )
            return embed

        # ── PSA detection ──────────────────────────────────────────────────
        combined_text = listing.title + " " + (listing.description or "")
        psa_grade: int | None = None
        if contains_psa(combined_text):
            psa_grade = extract_psa_grade(combined_text)

        # ── Catalog lookup (fast path – no browser required) ──────────────
        cm_data = None
        resolved = None

        if (
            self._catalog is not None
            and self._catalog.is_loaded
            and psa_grade is None
        ):
            catalog_price = self._catalog.find_and_get_price_data(
                card_name=fingerprint.card_name or "",
                collector_number=fingerprint.collector_number,
                set_name=fingerprint.set_name,
                set_code=fingerprint.set_code,
            )
            if catalog_price is not None and catalog_price.is_valid():
                cm_data = CardmarketPriceData(
                    product_url=catalog_price.product_url or "",
                    product_name=catalog_price.product_name,
                    product_id=str(catalog_price.product_id),
                    from_price=catalog_price.from_price,
                    price_trend=catalog_price.price_trend,
                    avg_30_days=catalog_price.avg_30_days,
                    avg_7_days=catalog_price.avg_7_days,
                    avg_1_day=catalog_price.avg_1_day,
                    dutch_sellers_available=True,
                    set_name=catalog_price.set_name,
                    card_number=catalog_price.card_number,
                    id_expansion=catalog_price.id_expansion,
                )
                resolved = ResolvedUrl(
                    url=catalog_price.product_url or "",
                    source="catalog",
                    confidence=0.9,
                )
                # Store a mapping so future lookups can use the DB fast path.
                if self._resolver is not None:
                    await self._resolver.store_mapping(
                        fingerprint=fingerprint,
                        raw_title=listing.title,
                        cardmarket_url=cm_data.product_url,
                        product_name=cm_data.product_name,
                        product_id=cm_data.product_id,
                        validated_by="catalog",
                        confidence=resolved.confidence,
                        listing_url=listing.url,
                        seller_name=listing.seller_name,
                        price=listing.price,
                    )

        # ── DB resolver fallback ───────────────────────────────────────────
        if cm_data is None and self._resolver is not None:
            db_resolved = self._resolver.resolve_db_only(fingerprint, listing.title)
            if db_resolved is not None:
                resolved = db_resolved
                if self._cardmarket is not None:
                    try:
                        cm_data = await self._cardmarket.scrape_url(resolved.url)
                    except CardmarketScrapeError as exc:
                        # We know the CM URL but could not retrieve live prices.
                        return build_check_listing_embed(
                            listing,
                            fingerprint=fingerprint,
                            cm_url=resolved.url,
                            cm_error=exc.message,
                            match_confidence=resolved.confidence,
                            match_source=resolved.source,
                        )
                    # ── PSA price refinement (mirrors _process_listing step 5b) ──
                    if cm_data is not None and psa_grade is not None and psa_grade >= 9:
                        psa_price = await self._cardmarket.scrape_psa_listing_price(
                            resolved.url, psa_grade
                        )
                        if psa_price is not None:
                            cm_data = dataclasses.replace(cm_data, from_price=psa_price)

        # ── No Cardmarket match found → send to unidentified channel ──────
        if cm_data is None:
            await self._send_to_unidentified(listing, fingerprint)
            await self.db.mark_seen(
                listing_id=listing.listing_id,
                title=listing.title,
                url=listing.url,
                price=listing.price,
                currency=listing.currency,
                seller_name=listing.seller_name,
                fingerprint=fingerprint.fingerprint_hash(),
            )
            embed = build_check_listing_embed(
                listing,
                fingerprint=fingerprint,
                cm_url=resolved.url if resolved else None,
                filter_reason="unidentified",
            )
            embed.add_field(
                name="📨 Action Taken",
                value="This listing has been posted to the **unidentified channel** so the community can supply the Cardmarket URL.",
                inline=False,
            )
            return embed

        # ── Store mapping for auto-constructed URLs ────────────────────────
        if resolved is not None and resolved.source == "constructed" and self._resolver is not None:
            await self._resolver.store_mapping(
                fingerprint=fingerprint,
                raw_title=listing.title,
                cardmarket_url=cm_data.product_url,
                product_name=cm_data.product_name,
                product_id=cm_data.product_id,
                validated_by="auto",
                confidence=resolved.confidence,
                listing_url=listing.url,
                seller_name=listing.seller_name,
                price=listing.price,
            )

        # ── Price comparison ───────────────────────────────────────────────
        comparison = compare_prices(listing, cm_data)

        if comparison.is_profitable:
            self._listings_profitable += 1
            await self._send_profit_alert(listing, cm_data, comparison, resolved, fingerprint)
            action_value = "✅ This listing is **profitable** and has been posted to the **deals channel**."
        else:
            await self._send_identified_not_profitable(listing, cm_data, comparison, resolved, fingerprint)
            action_value = "📉 This listing was **identified** but is **not profitable** – posted to the **match channel**."

        await self.db.mark_seen(
            listing_id=listing.listing_id,
            title=listing.title,
            url=listing.url,
            price=listing.price,
            currency=listing.currency,
            seller_name=listing.seller_name,
            fingerprint=fingerprint.fingerprint_hash(),
        )

        embed = build_check_listing_embed(
            listing,
            fingerprint=fingerprint,
            cm_data=cm_data,
            comparison=comparison,
            match_confidence=resolved.confidence if resolved else None,
            match_source=resolved.source if resolved else None,
        )
        embed.add_field(
            name="📨 Action Taken",
            value=action_value,
            inline=False,
        )
        return embed

