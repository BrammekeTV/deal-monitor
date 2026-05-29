"""
utils/deal_scorer.py
~~~~~~~~~~~~~~~~~~~~
Configurable scoring logic that rates how good a Vinted deal is.

Score is an integer 0–100.  Listings scoring >= settings.min_score
will be posted to Discord.

Scoring breakdown (approximate):
  - 0–10  : Price below max threshold
  - 0–30  : Discount vs estimated market value
  - 0–20  : Positive keyword matches
  - 0–10  : Bundle bonus
  - 0–10  : Seller rating bonus
  - -100  : Blacklist keyword → score becomes 0, listing discarded
"""

from __future__ import annotations

from config.settings import settings
from scraper.base import Listing
from utils.logger import get_logger

logger = get_logger(__name__)


class DealScorer:
    """Stateless deal evaluator.

    The ``settings`` singleton is read fresh on each ``score()`` call, so
    config hot-reloads take effect without restarting.
    """

    def estimate_market_value(self, listing: Listing, live_value: float | None = None) -> float:
        """Look up an estimated market value for the listing.

        If *live_value* is provided (from eBay/Cardmarket), it takes priority
        over the static config table.  Otherwise checks config.market_values
        for the best (longest) matching substring against the listing title.
        Falls back to the 'default' key.
        """
        if live_value is not None and live_value > 0:
            return live_value

        title_lower = listing.title.lower()
        best_key = ""
        best_value = settings.market_values.get("default", 30.0)

        for key, value in settings.market_values.items():
            if key == "default":
                continue
            if key in title_lower and len(key) > len(best_key):
                best_key = key
                best_value = value

        return best_value

    def is_blacklisted(self, listing: Listing) -> bool:
        """Return True if any blacklist keyword appears in the title."""
        title_lower = listing.title.lower()
        for kw in settings.blacklist_keywords:
            if kw in title_lower:
                logger.debug(
                    "Listing %s blacklisted (keyword: '%s')", listing.listing_id, kw
                )
                return True
        return False

    def passes_filters(self, listing: Listing) -> bool:
        """Check hard filters (blacklist, max price, seller rating)."""
        # Blacklisted keywords?
        if self.is_blacklisted(listing):
            return False
        # Above max price?
        if listing.price > settings.max_price:
            logger.debug(
                "Listing %s price %.2f exceeds max %.2f",
                listing.listing_id,
                listing.price,
                settings.max_price,
            )
            return False
        # Below minimum seller rating?
        if (
            settings.min_seller_rating > 0
            and listing.seller_rating is not None
            and listing.seller_rating < settings.min_seller_rating
        ):
            logger.debug(
                "Listing %s seller rating %.1f below min %.1f",
                listing.listing_id,
                listing.seller_rating,
                settings.min_seller_rating,
            )
            return False
        return True

    def score(self, listing: Listing, live_market_value: float | None = None) -> int:
        """Compute and return the deal score (0–100).

        Also sets ``listing.score`` and ``listing.estimated_market_value``
        as side effects.

        *live_market_value* may be supplied from a real-time eBay/Cardmarket
        lookup.  When provided it takes priority over static config values.
        """
        if not self.passes_filters(listing):
            listing.score = 0
            return 0

        emv = self.estimate_market_value(listing, live_value=live_market_value)
        listing.estimated_market_value = emv

        total = 0
        title_lower = listing.title.lower()

        # 1. Base score just for being under max price.
        total += 10

        # 2. Discount relative to estimated market value (up to 30 points).
        if emv > 0 and listing.price < emv:
            discount_pct = (1 - listing.price / emv) * 100
            discount_score = min(30, int(discount_pct * 30 / 100))
            total += discount_score
            logger.debug(
                "Listing %s: %.1f%% below EMV → +%d",
                listing.listing_id,
                discount_pct,
                discount_score,
            )

        # 3. Positive keyword hits (up to 20 points).
        kw_hits = sum(1 for kw in settings.positive_keywords if kw in title_lower)
        keyword_score = min(20, kw_hits * 5)
        total += keyword_score

        # 4. Bundle bonus (up to 10 points).
        is_bundle = any(kw in title_lower for kw in settings.bundle_keywords)
        if is_bundle:
            total += 10

        # 5. Seller rating bonus (up to 10 points).
        if listing.seller_rating is not None:
            rating_score = min(10, int(listing.seller_rating * 2))
            total += rating_score

        total = min(100, total)
        listing.score = total
        logger.debug("Listing %s scored %d/100", listing.listing_id, total)
        return total
