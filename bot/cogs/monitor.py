"""
bot/cogs/monitor.py
~~~~~~~~~~~~~~~~~~~
Background monitoring cog.

Every cycle (random interval between interval_min and interval_max seconds):
  1. For each configured search term, call VintedScraper.search().
  2. For each new listing:
     a. Check if already processed – skip if seen.
     b. Extract a card fingerprint from the title.
     c. Look up a Cardmarket product URL via CardmarketResolver:
        - DB lookup (learned mapping)
        - Constructed URL from fingerprint
        - If neither works → send to review queue
     d. Scrape the Cardmarket product page.
     e. Compare Vinted price against Cardmarket From price.
     f. If profitable → send profit alert to deals channel.
     g. If not profitable → skip silently.
     h. Mark listing as seen.
  3. On Cardmarket scraping error → log to error channel, mark seen, continue.
"""

from __future__ import annotations

import asyncio
import random
import traceback
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import discord
from discord.ext import commands
from playwright.async_api import async_playwright

from config.settings import settings
from database.db import Database
from scraper.cardmarket import CardmarketScrapeError, CardmarketScraper
from scraper.vinted import VintedScraper
from services.card_identifier import identify_card
from services.cardmarket_resolver import CardmarketResolver
from services.price_comparison import compare_prices
from utils.embed_builder import (
    build_error_embed,
    build_profit_alert_embed,
    build_status_embed,
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

        # Services (initialised in cog_load)
        self._vinted: VintedScraper | None = None
        self._cardmarket: CardmarketScraper | None = None
        self._resolver: CardmarketResolver | None = None
        self._playwright = None
        self._browser = None

        # Background task handle
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def cog_load(self) -> None:
        """Start the monitoring loop when the cog is loaded."""
        # Initialise Vinted scraper.
        self._vinted = VintedScraper()
        await self._vinted.setup()

        # Initialise Playwright browser for Cardmarket scraping.
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=settings.headless
        )
        self._cardmarket = CardmarketScraper(self._browser)

        # Initialise resolver with DB.
        self._resolver = CardmarketResolver(self.db)
        await self._resolver.load()

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
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

        logger.info("MonitorCog: stopped")

    # ------------------------------------------------------------------
    # Public accessor (used by review cog and slash commands)
    # ------------------------------------------------------------------

    @property
    def resolver(self) -> CardmarketResolver | None:
        return self._resolver

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

        while not self.bot.is_closed():
            try:
                await self._run_cycle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "MonitorCog: unexpected error in monitoring loop: %s",
                    exc, exc_info=True,
                )
                await self._send_error(
                    failure_step="monitor_loop",
                    error_message=str(exc),
                    stack_trace=traceback.format_exc(),
                )

            # Random sleep between cycles.
            delay = random.randint(settings.interval_min, settings.interval_max)
            self._next_run = datetime.now(timezone.utc).replace(
                second=datetime.now(timezone.utc).second + delay % 60
            )
            logger.debug("MonitorCog: sleeping %d seconds before next cycle", delay)
            await asyncio.sleep(delay)

    async def _run_cycle(self) -> None:
        """Execute one full scrape cycle over all configured search terms."""
        self._last_run = datetime.now(timezone.utc)
        logger.info("MonitorCog: starting scrape cycle")

        for term in settings.search_terms:
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

        # ── 2. Card identification ────────────────────────────────────────
        fingerprint = identify_card(listing.title)

        # ── 3. Cardmarket URL resolution ──────────────────────────────────
        matching_attempts: list[dict] = []

        resolved = await self._resolver.resolve(fingerprint, listing.title)  # type: ignore[union-attr]

        if resolved is None:
            # No mapping found → send to review queue.
            await self._send_to_review(listing, fingerprint, matching_attempts)
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

        # ── 4. Cardmarket scraping ────────────────────────────────────────
        try:
            cm_data = await self._cardmarket.scrape_url(resolved.url)  # type: ignore[union-attr]
        except CardmarketScrapeError as exc:
            logger.warning(
                "MonitorCog: Cardmarket scrape failed for '%s': %s",
                listing.title[:60], exc,
            )
            # Log error and abort – do NOT crash the loop or send to review.
            error_log_id = await self.db.log_error(
                listing_id=listing.listing_id,
                listing_title=listing.title,
                listing_url=listing.url,
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

        # ── 5. Price comparison ───────────────────────────────────────────
        comparison = compare_prices(listing, cm_data)

        # ── 6. Store mapping if auto-constructed (now validated) ──────────
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

        # ── 7. Post profit alert or skip ──────────────────────────────────
        if comparison.is_profitable:
            self._listings_profitable += 1
            await self._send_profit_alert(listing, cm_data, comparison, resolved)
        else:
            logger.info(
                "MonitorCog: not profitable – Vinted €%.2f vs CM €%.2f for '%s'",
                comparison.vinted_price,
                comparison.cardmarket_from_price,
                listing.title[:60],
            )

        # ── 8. Mark as seen ───────────────────────────────────────────────
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
    # Review queue
    # ------------------------------------------------------------------

    async def _send_to_review(
        self,
        listing: "Listing",
        fingerprint,
        matching_attempts: list[dict],
    ) -> None:
        """Send a listing to the review queue Discord channel."""
        from utils.embed_builder import build_review_embed

        self._listings_reviewed += 1

        # Determine failure reason
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
        )

        # Post to review channel.
        review_channel = self._get_review_channel()
        if review_channel is None:
            logger.debug(
                "MonitorCog: review channel not configured – skipping review post for %s",
                listing.listing_id,
            )
            return

        embed = build_review_embed(
            listing,
            fingerprint=fingerprint,
            failure_reason=failure_reason,
            matching_attempts=matching_attempts,
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

    # ------------------------------------------------------------------
    # Discord posting helpers
    # ------------------------------------------------------------------

    async def _send_profit_alert(
        self,
        listing: "Listing",
        cm_data,
        comparison,
        resolved,
    ) -> None:
        """Post a profit alert embed to the deals channel."""
        embed = build_profit_alert_embed(
            listing,
            cm_data,
            comparison,
            match_confidence=resolved.confidence,
            match_source=resolved.source,
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
        embed = build_error_embed(
            listing_title=listing_title,
            listing_url=listing_url,
            cardmarket_url=cardmarket_url,
            failure_step=failure_step,
            error_message=error_message,
            http_status=http_status,
            stack_trace=stack_trace,
            fingerprint=fingerprint,
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

    def _get_review_channel(self) -> discord.TextChannel | None:
        ch_id = settings.discord_review_channel_id
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

    # ------------------------------------------------------------------
    # Slash commands
    # ------------------------------------------------------------------

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
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
