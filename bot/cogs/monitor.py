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

import aiohttp
import discord
from discord.ext import commands, tasks

from config.settings import settings
from database.db import Database
from scraper.vinted import VintedScraper
from utils.deal_scorer import DealScorer
from utils.embed_builder import build_listing_embed
from utils.logger import get_logger
from utils.price_lookup import best_market_value, lookup_prices

if TYPE_CHECKING:
    from scraper.base import Listing

logger = get_logger(__name__)


class MonitorCog(commands.Cog, name="Monitor"):
    """Background task that polls Vinted and posts deals to Discord."""

    def __init__(self, bot: commands.Bot, db: Database) -> None:
        self.bot = bot
        self.db = db
        self.scraper = VintedScraper()
        self.scorer = DealScorer()

        # Runtime statistics (exposed to the status slash command).
        self.listings_checked: int = 0
        self.listings_posted: int = 0
        self.last_run: datetime | None = None
        self.next_run: datetime | None = None

        # aiohttp session for webhook delivery.
        self._http: aiohttp.ClientSession | None = None

    # ------------------------------------------------------------------
    # Cog lifecycle
    # ------------------------------------------------------------------

    async def cog_load(self) -> None:
        logger.info("MonitorCog loading – starting scraper and background task")
        await self.scraper.setup()
        self._http = aiohttp.ClientSession()
        self._monitor_loop.start()

    async def cog_unload(self) -> None:
        logger.info("MonitorCog unloading")
        self._monitor_loop.cancel()
        await self.scraper.teardown()
        if self._http:
            await self._http.close()

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

        # Fetch live market prices from eBay / Cardmarket.
        price_results = []
        if self._http:
            price_results = await lookup_prices(
                self._http, listing.title, browser=self.scraper._browser
            )

        live_value = best_market_value(price_results) if price_results else None

        # Score (uses live market value when available).
        score = self.scorer.score(listing, live_market_value=live_value)
        logger.debug(
            "Listing %s '%s' £%.2f → score %d",
            listing.listing_id,
            listing.title,
            listing.price,
            score,
        )

        # Always mark as seen so we don't re-evaluate it.
        await self.db.mark_seen(
            listing_id=listing.listing_id,
            title=listing.title,
            url=listing.url,
            price=listing.price,
            seller=listing.seller_name,
            currency=listing.currency,
            score=score,
            posted_to_discord=score >= settings.min_score,
            terms=[term],
        )

        # Post to Discord / webhook if score qualifies.
        if score >= settings.min_score:
            await self._post_listing(listing, channel, price_results=price_results)

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
                    "Posted deal: %s (%s) score=%d",
                    listing.title,
                    listing.url,
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
