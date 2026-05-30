"""
bot/cogs/monitor.py
~~~~~~~~~~~~~~~~~~~
Background monitoring cog.

Every cycle (random interval between interval_min and interval_max):
  1. Iterate over all configured search terms.
  2. For each term, call VintedScraper.search().
  3. Score each listing with DealScorer.
  4. Skip listings already in the database.
  5. Post qualifying listings as Discord embeds.
  6. Mark listings as seen in the database.
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import aiohttp
import discord
from discord.ext import commands, tasks
from playwright.async_api import async_playwright

from config.settings import settings
from database.db import Database
from scraper.vinted import VintedScraper
from utils.card_analyzer import CardAnalyzer
from utils.deal_scorer import DealScorer
from utils.embed_builder import build_listing_embed
from utils.logger import get_logger
from utils.price_lookup import best_market_value, lookup_prices

if TYPE_CHECKING:
    from bot.cogs.review import ReviewCog
    from playwright.async_api import Browser
    from scraper.base import Listing

logger = get_logger(__name__)


class MonitorCog(commands.Cog, name="Monitor"):
    """Background task that polls Vinted and posts deals to Discord."""

    def __init__(self, bot: commands.Bot, db: Database) -> None:
        self.bot = bot
        self.db = db
        self.scraper = VintedScraper()
        self.scorer = DealScorer()
        self.card_analyzer = CardAnalyzer()

        # Runtime statistics (exposed to the status slash command).
        self.listings_checked: int = 0
        self.listings_posted: int = 0
        self.last_run: datetime | None = None
        self.next_run: datetime | None = None

        # aiohttp session for webhook delivery.
        self._http: aiohttp.ClientSession | None = None

        # TCGGO client for Cardmarket API lookups.
        self._tcggo_client = None

        # Playwright browser for Cardmarket scraper fallback.
        self._playwright = None
        self._browser: Browser | None = None

    # ------------------------------------------------------------------
    # Cog lifecycle
    # ------------------------------------------------------------------

    async def cog_load(self) -> None:
        logger.info("MonitorCog loading – starting scraper and background task")
        await self.scraper.setup()
        self._http = aiohttp.ClientSession()

        # Initialise the TCGGO client (used for all Cardmarket price lookups).
        if settings.tcggo_enabled:
            try:
                from utils.tcggo import TcggoClient
                self._tcggo_client = TcggoClient.from_settings()
                if self._tcggo_client.is_configured():
                    logger.info("TCGGO client ready for Cardmarket API lookups")
                else:
                    logger.info(
                        "TCGGO client not configured – set RAPIDAPI_KEY in .env"
                    )
                    self._tcggo_client = None
            except ValueError as exc:
                logger.info("TCGGO client not configured: %s", exc)
                self._tcggo_client = None
            except Exception:
                logger.warning("Failed to initialise TCGGO client", exc_info=True)
                self._tcggo_client = None

        # Only launch a Playwright browser when the scraper fallback is enabled.
        if settings.cardmarket_enabled and settings.cardmarket_scraping_fallback:
            try:
                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-blink-features=AutomationControlled",
                        "--disable-infobars",
                        "--disable-dev-shm-usage",
                    ],
                )
                logger.info("Playwright browser launched for Cardmarket scraper fallback")
            except Exception:
                logger.warning(
                    "Failed to launch Playwright browser – Cardmarket scraper fallback disabled",
                    exc_info=True,
                )
        self._monitor_loop.start()

    async def cog_unload(self) -> None:
        logger.info("MonitorCog unloading")
        self._monitor_loop.cancel()
        await self.scraper.teardown()
        if self._http:
            await self._http.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------

    @tasks.loop(seconds=1)
    async def _monitor_loop(self) -> None:
        """Entry point called by discord.ext.tasks every second.

        The actual work is guarded by a random sleep so the effective
        interval is between interval_min and interval_max seconds.
        """
        interval = random.randint(settings.interval_min, settings.interval_max)
        self.next_run = datetime.now(timezone.utc).replace(
            second=0, microsecond=0
        )
        logger.debug("Next scrape cycle in %d seconds", interval)
        await asyncio.sleep(interval)
        await self._run_cycle()

    @_monitor_loop.before_loop
    async def _before_loop(self) -> None:
        await self.bot.wait_until_ready()
        logger.info("Bot ready – running immediate startup scrape cycle")
        await self._run_cycle()
        logger.info("Startup scrape cycle complete – regular interval loop begins")

    @_monitor_loop.error
    async def _loop_error(self, error: Exception) -> None:
        logger.error("Monitor loop raised an exception: %s", error, exc_info=True)
        # The loop auto-resumes after an error in discord.ext.tasks.

    # ------------------------------------------------------------------
    # Core cycle
    # ------------------------------------------------------------------

    async def _run_cycle(self) -> None:
        """One full scrape cycle across all search terms."""
        self.last_run = datetime.now(timezone.utc)
        logger.info("=== Scrape cycle started ===")

        channel = self._get_channel()

        for term in settings.search_terms:
            try:
                await self._process_term(term, channel)
            except Exception as exc:  # noqa: BLE001
                logger.error("Error processing term '%s': %s", term, exc, exc_info=True)
            # Polite delay between search terms.
            await asyncio.sleep(random.uniform(3.0, 7.0))

        logger.info(
            "=== Cycle done. Checked %d, posted %d ===",
            self.listings_checked,
            self.listings_posted,
        )
        # Prune stale records older than 30 days.
        pruned = await self.db.prune_old(days=30)
        if pruned:
            logger.debug("Pruned %d old listings from DB", pruned)

    async def _process_term(
        self, term: str, channel: discord.TextChannel | None
    ) -> None:
        """Scrape *term* and post qualifying new listings."""
        logger.info("Searching for: '%s'", term)
        async for listing in self.scraper.search(term, max_results=settings.results_per_term):
            self.listings_checked += 1
            try:
                await self._handle_listing(listing, term, channel)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Error handling listing %s: %s", listing.listing_id, exc, exc_info=True
                )

    async def _handle_listing(
        self,
        listing: "Listing",
        term: str,
        channel: discord.TextChannel | None,
    ) -> None:
        # Deduplicate.
        if await self.db.is_seen(listing.listing_id):
            logger.debug("Already seen: %s", listing.listing_id)
            return

        # Analyse whether this is a card listing / bulk lot and estimate
        # card count + price per card before any pricing decisions are made.
        self.card_analyzer.analyze(listing)

        # --- Check identification memory for previously approved matches ---
        memory_matches = await self.db.find_in_memory(listing.title)
        memory_value: float | None = None
        # Extract a direct Cardmarket URL from memory if one exists; this lets
        # the scraper bypass the unreliable search page.
        cm_memory_url: str | None = None
        if memory_matches:
            for match in memory_matches:
                ref = match.get("reference_url") or ""
                try:
                    # Check netloc specifically to avoid matching URLs that
                    # merely contain "cardmarket.com" in the path or query.
                    # Accept "cardmarket.com" and subdomains ("www.cardmarket.com")
                    # but reject spoofs like "evil-cardmarket.com".
                    netloc = urlparse(ref).netloc.lower()
                    if netloc == "cardmarket.com" or netloc.endswith(".cardmarket.com"):
                        cm_memory_url = ref
                        break
                except Exception:  # noqa: BLE001
                    pass
            best = next((m for m in memory_matches if m.get("market_value")), None)
            if best:
                memory_value = best["market_value"]
                listing.confidence = "Medium"
                listing.valuation_explanation = (
                    f"Matched from community memory: {best['card_name']}. "
                    f"Reference: {best.get('reference_url') or 'n/a'}"
                )
                logger.info(
                    "Memory match for listing %s: '%s' (value=%.2f)",
                    listing.listing_id,
                    best["card_name"],
                    memory_value,
                )

        # Fetch live market prices from eBay / Cardmarket (via TCGGO API or scraper fallback).
        price_results = []
        if self._http:
            # When no memory URL exists but card info was extracted, try to build a
            # direct Cardmarket product URL.  This works for any card with a known
            # set code (promos and standard sets alike).
            cm_lookup_url = cm_memory_url
            if not cm_lookup_url and listing.extracted_set_code:
                try:
                    from scraper.cardmarket import build_cardmarket_url, _SET_CODE_TO_SLUG
                    set_code_upper = listing.extracted_set_code.upper()
                    if set_code_upper in _SET_CODE_TO_SLUG:
                        card_name_for_url = listing.extracted_card_name or listing.title
                        collector_num = listing.extracted_collector_number or ""
                        is_promo = "/" not in collector_num
                        built = build_cardmarket_url(
                            card_name_for_url,
                            listing.extracted_set_code,
                            collector_num,
                            promo=is_promo,
                        )
                        if built:
                            cm_lookup_url = built
                            logger.info(
                                "Built Cardmarket URL for listing %s (set=%s num=%s promo=%s): %s",
                                listing.listing_id,
                                listing.extracted_set_code,
                                collector_num or "n/a",
                                is_promo,
                                built,
                            )
                        else:
                            logger.debug(
                                "build_cardmarket_url returned None for listing %s "
                                "(set=%s num=%s)",
                                listing.listing_id,
                                listing.extracted_set_code,
                                collector_num or "n/a",
                            )
                    else:
                        logger.debug(
                            "Set code %r not in known slugs for listing %s – "
                            "falling back to TCGGO text search",
                            listing.extracted_set_code,
                            listing.listing_id,
                        )
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "Error building Cardmarket URL for listing %s",
                        listing.listing_id,
                        exc_info=True,
                    )
            elif not cm_lookup_url and listing.extracted_card_name:
                logger.debug(
                    "Listing %s has card name %r but no set code – using TCGGO text search",
                    listing.listing_id,
                    listing.extracted_card_name,
                )

            price_results = await lookup_prices(
                self._http,
                listing.title,
                browser=self._browser,
                cm_direct_url=cm_lookup_url,
                tcggo_client=self._tcggo_client,
                card_name=listing.extracted_card_name,
                collector_number=listing.extracted_collector_number,
                set_code=listing.extracted_set_code,
                set_name=listing.extracted_set_name,
            )
            logger.debug(
                "Price lookup for listing %s returned %d result(s): %s",
                listing.listing_id,
                len(price_results),
                [r.platform for r in price_results],
            )

        live_value = best_market_value(price_results) if price_results else memory_value

        # Upgrade confidence to High when we have a live market price match.
        if live_value is not None and listing.confidence == "Medium":
            listing.confidence = "High"
            listing.valuation_explanation = (
                listing.valuation_explanation.replace(
                    "Confidence will rise to High if a live market price is matched.",
                    "Live market price matched – confidence upgraded to High.",
                )
            )

        # ----------------------------------------------------------------
        # Decide whether to post this listing.
        # ----------------------------------------------------------------
        should_post: bool
        send_to_review: bool = False

        if listing.is_bulk_lot:
            # Bulk lots bypass the score system; pass only at ≤ €0.01/card.
            should_post = self.scorer.should_post_bulk(listing)
            listing.score = 0  # score not meaningful for bulk lots
            if should_post and not listing.valuation_explanation:
                listing.valuation_explanation = (
                    f"Bulk lot: {listing.estimated_card_count} cards at "
                    f"€{listing.price_per_card:.4f}/card."
                )
        else:
            # Individual card: check Cardmarket deal conditions first (when
            # Cardmarket data is available), then fall back to score threshold.
            cm_result = next(
                (r for r in price_results if r.platform == "Cardmarket"), None
            )

            cm_qualifies = False
            cm_deal_label = ""
            if cm_result is not None:
                cm_qualifies, cm_deal_label = self.scorer.cardmarket_deal_type(
                    listing, cm_result
                )

            score = self.scorer.score(listing, live_market_value=live_value)
            logger.debug(
                "Listing %s '%s' %.2f %s → score %d cm_qualifies=%s",
                listing.listing_id,
                listing.title,
                listing.price,
                listing.currency,
                score,
                cm_qualifies,
            )

            if cm_qualifies:
                # Cardmarket conditions met – always post regardless of score.
                should_post = True
                # Annotate the deal label in the explanation.
                listing.valuation_explanation = (
                    f"Cardmarket deal detected: {cm_deal_label}. "
                    + (listing.valuation_explanation or "")
                ).strip()
                if listing.confidence == "Low":
                    listing.confidence = "Medium"
            elif score == 0 and not settings.allow_low_confidence:
                # Unverified – route to review channel instead of discarding.
                should_post = False
                send_to_review = True
            else:
                should_post = score >= settings.min_score

            # Annotate explanation when market data was found (only if not
            # already set by Cardmarket deal detection above).
            if not cm_qualifies and price_results and listing.estimated_market_value:
                sources = ", ".join(r.platform for r in price_results)
                urls = "  ".join(r.search_url for r in price_results)
                listing.valuation_explanation = (
                    f"Market price sourced from: {sources}. "
                    f"Estimated value: €{listing.estimated_market_value:.2f}. "
                    f"Sources: {urls}"
                )

        # Always mark as seen so we don't re-evaluate it.
        await self.db.mark_seen(
            listing_id=listing.listing_id,
            title=listing.title,
            url=listing.url,
            price=listing.price,
            seller=listing.seller_name,
            currency=listing.currency,
            score=listing.score,
            posted_to_discord=should_post,
            terms=[term],
        )

        # Post to Discord / webhook if it qualifies.
        if should_post:
            await self._post_listing(listing, channel, price_results=price_results)
        elif send_to_review:
            await self._post_for_review(listing)

    async def _post_for_review(self, listing: "Listing") -> None:
        """Delegate an unidentified listing to the ReviewCog."""
        review_cog: "ReviewCog | None" = self.bot.cogs.get("Review")  # type: ignore[assignment]
        if review_cog is None:
            logger.debug(
                "ReviewCog not loaded – unidentified listing %s will not be posted",
                listing.listing_id,
            )
            return
        await review_cog.post_for_review(listing)

    async def _post_listing(
        self,
        listing: "Listing",
        channel: discord.TextChannel | None,
        price_results: list | None = None,
    ) -> None:
        embed = build_listing_embed(listing, price_results=price_results)

        # Try the bot channel first.
        if channel:
            try:
                await channel.send(embed=embed)
                self.listings_posted += 1
                logger.info(
                    "Posted deal: %s (%s) bulk=%s score=%d",
                    listing.title,
                    listing.url,
                    listing.is_bulk_lot,
                    listing.score,
                )
                await self.db.mark_posted(listing.listing_id)
                return
            except discord.HTTPException as exc:
                logger.error("Failed to post to channel: %s", exc)

        # Fallback: webhook.
        if settings.use_webhook and settings.discord_webhook_url and self._http:
            await self._post_via_webhook(listing, embed)

    async def _post_via_webhook(
        self, listing: "Listing", embed: discord.Embed
    ) -> None:
        """Deliver embed via an incoming Discord webhook."""
        payload = {"embeds": [embed.to_dict()]}
        try:
            async with self._http.post(  # type: ignore[union-attr]
                settings.discord_webhook_url,  # type: ignore[arg-type]
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status not in (200, 204):
                    body = await resp.text()
                    logger.error("Webhook delivery failed %d: %s", resp.status, body)
                else:
                    self.listings_posted += 1
                    logger.info("Posted via webhook: %s", listing.url)
                    await self.db.mark_posted(listing.listing_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("Webhook error: %s", exc)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_channel(self) -> discord.TextChannel | None:
        channel_id = settings.discord_channel_id
        if not channel_id:
            return None
        channel = self.bot.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            logger.warning("Channel %d not found or not a text channel", channel_id)
            return None
        return channel
