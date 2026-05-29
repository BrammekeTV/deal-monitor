"""
scraper/base.py
~~~~~~~~~~~~~~~
Abstract base class for marketplace scrapers.

Adding a new marketplace (e.g. eBay, Marktplaats) means subclassing
BaseScraper and implementing the abstract methods.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncIterator


@dataclass
class Listing:
    """Represents a single marketplace listing."""

    listing_id: str
    title: str
    price: float
    currency: str
    url: str
    seller_name: str | None = None
    seller_rating: float | None = None  # 0.0 – 5.0
    images: list[str] = field(default_factory=list)
    description: str | None = None
    condition: str | None = None
    location: str | None = None
    # Populated after deal-scoring
    score: int = 0
    estimated_market_value: float | None = None

    @property
    def discount_pct(self) -> float | None:
        """Return percentage below estimated market value (positive = cheaper)."""
        if self.estimated_market_value and self.estimated_market_value > 0:
            return (1 - self.price / self.estimated_market_value) * 100
        return None

    @property
    def thumbnail(self) -> str | None:
        return self.images[0] if self.images else None


class BaseScraper(ABC):
    """Abstract marketplace scraper."""

    name: str = "unknown"

    @abstractmethod
    async def setup(self) -> None:
        """Initialise any resources (e.g. launch browser)."""

    @abstractmethod
    async def teardown(self) -> None:
        """Release resources."""

    @abstractmethod
    async def search(self, query: str, max_results: int = 30) -> AsyncIterator[Listing]:
        """Yield listings matching *query*."""
        # This keeps type checkers happy for the `async for` call sites.
        return
        yield  # noqa: unreachable

    @abstractmethod
    async def get_listing(self, url: str) -> Listing | None:
        """Fetch a single listing by URL (used for detail enrichment)."""
