"""
services/price_comparison.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Price Comparison Service.

Compares the Vinted listing price against the Cardmarket From price and
determines whether the listing represents a profitable buying opportunity.

Decision rule:
  Vinted price < Cardmarket From price  →  profitable  →  send Discord alert
  Vinted price >= Cardmarket From price  →  not profitable  →  no alert
"""

from __future__ import annotations

from dataclasses import dataclass

from scraper.cardmarket import CardmarketPriceData
from scraper.base import Listing
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class ComparisonResult:
    """Result of comparing a Vinted price against a Cardmarket From price."""

    vinted_price: float
    cardmarket_from_price: float
    is_profitable: bool

    # Calculated differences (only meaningful when is_profitable is True)
    absolute_difference: float   # cardmarket_from - vinted (positive = profit)
    percentage_difference: float  # percentage by which vinted is cheaper

    # Dutch seller context
    dutch_sellers_available: bool

    def summary(self) -> str:
        """Return a short human-readable summary."""
        if self.is_profitable:
            return (
                f"PROFITABLE: Vinted €{self.vinted_price:.2f} vs "
                f"Cardmarket €{self.cardmarket_from_price:.2f} "
                f"(saving €{self.absolute_difference:.2f} / {self.percentage_difference:.1f}%)"
            )
        return (
            f"NOT PROFITABLE: Vinted €{self.vinted_price:.2f} vs "
            f"Cardmarket €{self.cardmarket_from_price:.2f}"
        )


def compare_prices(
    listing: Listing,
    cm_data: CardmarketPriceData,
) -> ComparisonResult:
    """Compare Vinted listing price against Cardmarket From price.

    Parameters
    ----------
    listing:
        The Vinted listing being evaluated.
    cm_data:
        Scraped Cardmarket pricing data for the matched product.

    Returns
    -------
    ComparisonResult
        Contains ``is_profitable=True`` when Vinted price is strictly below
        the Cardmarket From price.
    """
    vinted_price = listing.price
    from_price = cm_data.from_price or 0.0

    if from_price <= 0:
        logger.warning(
            "Price comparison: Cardmarket From price is 0 for '%s' – "
            "cannot determine profitability",
            listing.title[:60],
        )
        return ComparisonResult(
            vinted_price=vinted_price,
            cardmarket_from_price=from_price,
            is_profitable=False,
            absolute_difference=0.0,
            percentage_difference=0.0,
            dutch_sellers_available=cm_data.dutch_sellers_available,
        )

    is_profitable = vinted_price < from_price
    absolute_diff = from_price - vinted_price
    pct_diff = (absolute_diff / from_price) * 100 if from_price > 0 else 0.0

    result = ComparisonResult(
        vinted_price=vinted_price,
        cardmarket_from_price=from_price,
        is_profitable=is_profitable,
        absolute_difference=absolute_diff,
        percentage_difference=pct_diff,
        dutch_sellers_available=cm_data.dutch_sellers_available,
    )

    logger.info(
        "Price comparison for '%s': %s",
        listing.title[:60], result.summary(),
    )
    return result
