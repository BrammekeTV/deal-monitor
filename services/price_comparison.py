"""
services/price_comparison.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Price Comparison Service.

Compares the total Vinted cost (item price + buyer protection fee + shipping)
against the Cardmarket From price and determines whether the listing represents
a profitable buying opportunity.

Vinted buyer fees:
  Protection charge = 5% of item price + €0.70
  Shipping          = €2.89 – €5.79 (range; worst-case used for profitability check)

Decision rule:
  total_cost_max < Cardmarket From price  →  profitable  →  send Discord alert
  total_cost_max >= Cardmarket From price →  not profitable  →  no alert
"""

from __future__ import annotations

from dataclasses import dataclass

from scraper.cardmarket import CardmarketPriceData
from scraper.base import Listing
from utils.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Vinted buyer fee constants
# ---------------------------------------------------------------------------

VINTED_PROTECTION_FEE_PCT: float = 0.05    # 5 % of item price
VINTED_PROTECTION_FEE_FIXED: float = 0.70  # fixed €0.70 on top
VINTED_SHIPPING_MIN: float = 2.89          # cheapest shipping tier
VINTED_SHIPPING_MAX: float = 5.79          # most expensive shipping tier


def _protection_fee(item_price: float) -> float:
    """Return the Vinted buyer protection fee for *item_price*."""
    return round(item_price * VINTED_PROTECTION_FEE_PCT + VINTED_PROTECTION_FEE_FIXED, 2)


def calculate_vinted_total_cost(item_price: float, shipping: float) -> float:
    """Return the total amount a buyer pays on Vinted including all fees."""
    return round(item_price + _protection_fee(item_price) + shipping, 2)


@dataclass
class ComparisonResult:
    """Result of comparing a Vinted total cost against a Cardmarket From price."""

    vinted_price: float          # item price only (as listed)
    cardmarket_from_price: float
    is_profitable: bool

    # Vinted fee breakdown
    protection_fee: float        # 5 % of item price + €0.70
    shipping_min: float          # minimum shipping cost
    shipping_max: float          # maximum shipping cost
    total_cost_min: float        # item + protection + min shipping (best case)
    total_cost_max: float        # item + protection + max shipping (worst case)

    # Calculated differences (based on worst-case total cost)
    absolute_difference: float   # cardmarket_from - total_cost_max (positive = profit)
    percentage_difference: float  # percentage by which total worst-case cost is cheaper

    # Dutch seller context
    dutch_sellers_available: bool

    def summary(self) -> str:
        """Return a short human-readable summary."""
        if self.is_profitable:
            return (
                f"PROFITABLE: Vinted total €{self.total_cost_max:.2f} "
                f"(item €{self.vinted_price:.2f} + fee €{self.protection_fee:.2f} + "
                f"ship €{self.shipping_max:.2f}) vs "
                f"Cardmarket €{self.cardmarket_from_price:.2f} "
                f"(saving €{self.absolute_difference:.2f} / {self.percentage_difference:.1f}%)"
            )
        return (
            f"NOT PROFITABLE: Vinted total €{self.total_cost_max:.2f} "
            f"(item €{self.vinted_price:.2f} + fee €{self.protection_fee:.2f} + "
            f"ship €{self.shipping_max:.2f}) vs "
            f"Cardmarket €{self.cardmarket_from_price:.2f}"
        )


def compare_prices(
    listing: Listing,
    cm_data: CardmarketPriceData,
) -> ComparisonResult:
    """Compare Vinted total buying cost against Cardmarket From price.

    Parameters
    ----------
    listing:
        The Vinted listing being evaluated.
    cm_data:
        Scraped Cardmarket pricing data for the matched product.

    Returns
    -------
    ComparisonResult
        Contains ``is_profitable=True`` when the worst-case Vinted total cost
        (item + protection fee + maximum shipping) is strictly below the
        Cardmarket From price.
    """
    item_price = listing.price
    from_price = cm_data.from_price or 0.0

    protection = _protection_fee(item_price)
    total_min = calculate_vinted_total_cost(item_price, VINTED_SHIPPING_MIN)
    total_max = calculate_vinted_total_cost(item_price, VINTED_SHIPPING_MAX)

    if from_price <= 0:
        logger.warning(
            "Price comparison: Cardmarket From price is 0 for '%s' – "
            "cannot determine profitability",
            listing.title[:60],
        )
        return ComparisonResult(
            vinted_price=item_price,
            cardmarket_from_price=from_price,
            is_profitable=False,
            protection_fee=protection,
            shipping_min=VINTED_SHIPPING_MIN,
            shipping_max=VINTED_SHIPPING_MAX,
            total_cost_min=total_min,
            total_cost_max=total_max,
            absolute_difference=0.0,
            percentage_difference=0.0,
            dutch_sellers_available=cm_data.dutch_sellers_available,
        )

    # Use worst-case total (max shipping) for the profitability check so we
    # only flag listings that are genuinely cheaper even in the worst case.
    is_profitable = total_max < from_price
    absolute_diff = from_price - total_max
    pct_diff = (absolute_diff / from_price) * 100 if from_price > 0 else 0.0

    result = ComparisonResult(
        vinted_price=item_price,
        cardmarket_from_price=from_price,
        is_profitable=is_profitable,
        protection_fee=protection,
        shipping_min=VINTED_SHIPPING_MIN,
        shipping_max=VINTED_SHIPPING_MAX,
        total_cost_min=total_min,
        total_cost_max=total_max,
        absolute_difference=absolute_diff,
        percentage_difference=pct_diff,
        dutch_sellers_available=cm_data.dutch_sellers_available,
    )

    logger.info(
        "Price comparison for '%s': %s",
        listing.title[:60], result.summary(),
    )
    return result
