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

Bulk lot listings bypass score-based evaluation and are only posted when
the estimated price per card is ≤ €0.01 (see :meth:`should_post_bulk`).

Per the card-price-validation requirements, no hardcoded default market
values are used.  The ``default`` key in ``market_values`` config is
intentionally ignored.  A live market price (from eBay or Cardmarket) is
required for a non-bulk individual-card listing to score above zero.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from config.settings import settings
from scraper.base import Listing
from utils.logger import get_logger

if TYPE_CHECKING:
    from utils.price_lookup import PriceResult

logger = get_logger(__name__)

# Maximum price per card (EUR) for a bulk lot to qualify for posting.
BULK_MAX_PRICE_PER_CARD: float = 0.01


class DealScorer:
    """Stateless deal evaluator.

    The ``settings`` singleton is read fresh on each ``score()`` call, so
    config hot-reloads take effect without restarting.
    """

    def estimate_market_value(
        self, listing: Listing, live_value: float | None = None
    ) -> float | None:
        """Look up an estimated market value for the listing.

        If *live_value* is provided (from eBay/Cardmarket), it is returned
        directly.  Otherwise the static ``market_values`` config table is
        checked for a matching title substring (longest match wins).

        The ``default`` key is **never** used as a fallback because the
        card-price-validation rules require real market data for every
        valuation.  ``None`` is returned when no live price is available
        and no specific static entry matches the title.
        """
        if live_value is not None and live_value > 0:
            return live_value

        title_lower = listing.title.lower()
        best_key = ""
        best_value: float | None = None

        for key, value in settings.market_values.items():
            # Skip the catch-all default – we never use it.
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

    def should_post_bulk(self, listing: Listing) -> bool:
        """Return True when a bulk lot qualifies for posting.

        A bulk listing passes only when:
        - Hard filters pass (blacklist, max price, seller rating).
        - ``listing.price_per_card`` is known and ≤ ``BULK_MAX_PRICE_PER_CARD``.

        When the card count is unknown the listing is unverifiable and is
        rejected unless ``settings.allow_low_confidence`` is enabled.
        """
        if not self.passes_filters(listing):
            return False

        if listing.price_per_card is None:
            if settings.allow_low_confidence:
                logger.info(
                    "Listing %s bulk lot with unknown card count – posting "
                    "because allow_low_confidence=true",
                    listing.listing_id,
                )
                return True
            logger.debug(
                "Listing %s bulk lot rejected: card count unknown",
                listing.listing_id,
            )
            return False

        qualifies = listing.price_per_card <= BULK_MAX_PRICE_PER_CARD
        logger.debug(
            "Listing %s bulk lot price/card=%.4f – %s",
            listing.listing_id,
            listing.price_per_card,
            "PASS" if qualifies else "FAIL",
        )
        return qualifies

    def cardmarket_deal_type(
        self, listing: Listing, cm_result: "PriceResult"
    ) -> tuple[bool, str]:
        """Check Cardmarket-specific deal conditions and return (should_post, label).

        Deal tiers (all relative to the best available market value):

        * **Major Opportunity** – listing price ≥ 50 % below market value
        * **Exceptional Deal**  – listing price ≥ 30 % below market value
        * **Great Deal**        – listing price ≥ 20 % below market value
        * **Good Deal**         – listing price ≥ 10 % below market value

        The market value used as reference follows the hierarchy:
        Trend → Market Price → 30-day Avg → other CM values.

        Listings with ``"Low"`` confidence are rejected regardless of discount.
        Multiple conditions can be satisfied simultaneously; all matching
        labels are included in the returned description.  Returns
        ``(True, label)`` when at least one condition is met, otherwise
        ``(False, "")``.
        """
        # Reject Low-confidence results – we cannot be sure of the match.
        if listing.confidence == "Low":
            logger.debug(
                "Listing %s skipped Cardmarket deal check – confidence is Low",
                listing.listing_id,
            )
            return False, ""

        price = listing.price
        labels: list[str] = []

        # Determine the reference market value using the hierarchy.
        ref_value: float | None = None
        ref_label: str = ""
        for v, lbl in (
            (cm_result.price_trend, "trend"),
            (cm_result.market_price, "market price"),
            (cm_result.avg_30_days, "30-day avg"),
            (cm_result.avg_price, "avg"),
            (cm_result.avg_7_days, "7-day avg"),
            (cm_result.avg_1_day, "1-day avg"),
            (cm_result.from_price, "from price"),
            (cm_result.suggested_price, "suggested price"),
        ):
            if v and v > 0:
                ref_value = v
                ref_label = lbl
                break

        if ref_value is None or ref_value <= 0:
            return False, ""

        discount = (1 - price / ref_value) * 100

        if discount >= 50:
            labels.append(
                f"🚨 Major Opportunity ({discount:.1f}% below {ref_label})"
            )
        elif discount >= 30:
            labels.append(
                f"🏆 Exceptional Deal ({discount:.1f}% below {ref_label})"
            )
        elif discount >= 20:
            labels.append(
                f"💥 Great Deal ({discount:.1f}% below {ref_label})"
            )
        elif discount >= 10:
            labels.append(
                f"👍 Good Deal ({discount:.1f}% below {ref_label})"
            )

        # Market Floor Deal: listing is below the current from-price
        # (kept as an additive signal, independent of the main tier check)
        if (
            cm_result.from_price
            and cm_result.from_price > 0
            and price < cm_result.from_price
            and not any("from price" in lbl for lbl in labels)
        ):
            floor_diff = cm_result.from_price - price
            labels.append(
                f"🔻 Market Floor Deal (€{floor_diff:.2f} below from-price)"
            )

        should_post = bool(labels)
        label = "  ".join(labels)
        if should_post:
            logger.debug(
                "Listing %s Cardmarket deal: %s", listing.listing_id, label
            )
        return should_post, label

    def score(self, listing: Listing, live_market_value: float | None = None) -> int:
        """Compute and return the deal score (0–100).

        Also sets ``listing.score`` and ``listing.estimated_market_value``
        as side effects.

        *live_market_value* may be supplied from a real-time eBay/Cardmarket
        lookup.  When provided it takes priority over static config values.

        If no live market value is found **and** no specific static entry
        matches the title, ``listing.estimated_market_value`` is set to
        ``None`` and the listing scores 0 (unverified).  The caller can
        still allow it through by enabling ``settings.allow_low_confidence``.
        """
        if not self.passes_filters(listing):
            listing.score = 0
            return 0

        emv = self.estimate_market_value(listing, live_value=live_market_value)
        listing.estimated_market_value = emv

        # No market value available and live data was not found → unverified.
        if emv is None:
            if not settings.allow_low_confidence:
                logger.debug(
                    "Listing %s: no market value available – marking unverified",
                    listing.listing_id,
                )
                listing.score = 0
                if not listing.valuation_explanation:
                    listing.valuation_explanation = (
                        "No live market price found and no specific static "
                        "market value matches this listing."
                    )
                if listing.confidence == "Low":
                    pass  # already set
                listing.confidence = "Low"
                return 0
            # allow_low_confidence: score without a discount component.
            emv = 0.0

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
