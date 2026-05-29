"""
utils/price_lookup.py
~~~~~~~~~~~~~~~~~~~~~
Live market price comparison from eBay and Cardmarket.

eBay uses the public Finding API (requires a free App ID).
Cardmarket is scraped via Playwright + BeautifulSoup (no credentials needed)
using the approach from DrankRock/AutoScrape's cardmarket_parser plugin.

Both lookups are optional:
- eBay is skipped when ``EBAY_APP_ID`` is not set.
- Cardmarket is skipped when no Playwright ``Browser`` is provided.

Results are cached in-process for ``settings.price_lookup_cache_ttl``
seconds to avoid hammering the sites on every new listing.
"""

from __future__ import annotations

import time
import urllib.parse
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import aiohttp

from config.settings import settings
from utils.logger import get_logger

if TYPE_CHECKING:
    from playwright.async_api import Browser

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class PriceResult:
    """Aggregated price data from one platform."""

    platform: str           # "eBay" | "Cardmarket"
    query: str
    avg_price: float
    min_price: float
    max_price: float
    currency: str
    search_url: str
    sample_count: int
    # Individual sold/listed prices for transparency
    prices: list[float] = field(default_factory=list)


# ---------------------------------------------------------------------------
# In-process cache
# ---------------------------------------------------------------------------

_cache: dict[str, tuple[float, list[PriceResult]]] = {}


def _cache_key(query: str) -> str:
    return query.lower().strip()


def _cached(query: str) -> list[PriceResult] | None:
    key = _cache_key(query)
    entry = _cache.get(key)
    if entry is None:
        return None
    ts, results = entry
    if time.monotonic() - ts > settings.price_lookup_cache_ttl:
        del _cache[key]
        return None
    return results


def _store_cache(query: str, results: list[PriceResult]) -> None:
    _cache[_cache_key(query)] = (time.monotonic(), results)


# ---------------------------------------------------------------------------
# eBay Finding API
# ---------------------------------------------------------------------------

_EBAY_FINDING_URL = (
    "https://svcs.ebay.com/services/search/FindingService/v1"
)

_EBAY_SITE_GLOBAL_ID: dict[int, str] = {
    0: "US",
    3: "GB",
    77: "DE",
    71: "FR",
    101: "IT",
    186: "ES",
}


async def _ebay_lookup(
    session: aiohttp.ClientSession, query: str
) -> PriceResult | None:
    """Fetch recently *sold* eBay listings for *query* via the Finding API."""
    app_id = settings.ebay_app_id
    if not app_id:
        logger.debug("eBay App ID not set – skipping eBay price lookup")
        return None

    params: dict[str, str] = {
        "OPERATION-NAME": "findCompletedItems",
        "SERVICE-VERSION": "1.0.0",
        "SECURITY-APPNAME": app_id,
        "RESPONSE-DATA-FORMAT": "JSON",
        "REST-PAYLOAD": "",
        "keywords": query,
        "itemFilter(0).name": "SoldItemsOnly",
        "itemFilter(0).value": "true",
        "sortOrder": "EndTimeSoonest",
        "paginationInput.entriesPerPage": str(settings.ebay_sample_size),
        "GLOBAL-ID": f"EBAY-{_EBAY_SITE_GLOBAL_ID.get(settings.ebay_site_id, 'GB')}",
    }

    search_url = (
        f"https://www.ebay.co.uk/sch/i.html"
        f"?_nkw={urllib.parse.quote_plus(query)}&LH_Sold=1&LH_Complete=1"
    )

    try:
        async with session.get(
            _EBAY_FINDING_URL,
            params=params,
            timeout=aiohttp.ClientTimeout(total=10),
            headers={"User-Agent": "deal-monitor/1.0"},
        ) as resp:
            if resp.status != 200:
                logger.warning("eBay Finding API returned HTTP %d", resp.status)
                return None
            data: dict[str, Any] = await resp.json(content_type=None)
    except Exception as exc:  # noqa: BLE001
        logger.warning("eBay lookup failed: %s", exc)
        return None

    try:
        response = data["findCompletedItemsResponse"][0]
        ack = response.get("ack", [None])[0]
        if ack != "Success":
            logger.debug("eBay API ack=%s for query '%s'", ack, query)
            return None

        items = response.get("searchResult", [{}])[0].get("item", [])
        if not items:
            logger.debug("eBay: no completed items for '%s'", query)
            return None

        prices: list[float] = []
        currency = "GBP"
        for item in items:
            selling = item.get("sellingStatus", [{}])[0]
            state = selling.get("sellingState", [""])[0]
            if state != "EndedWithSales":
                continue
            cp = selling.get("currentPrice", [{}])[0]
            currency = cp.get("@currencyId", currency)
            try:
                prices.append(float(cp.get("__value__", 0)))
            except (ValueError, TypeError):
                pass

        if not prices:
            return None

        avg = sum(prices) / len(prices)
        return PriceResult(
            platform="eBay",
            query=query,
            avg_price=round(avg, 2),
            min_price=round(min(prices), 2),
            max_price=round(max(prices), 2),
            currency=currency,
            search_url=search_url,
            sample_count=len(prices),
            prices=prices,
        )
    except (KeyError, IndexError, TypeError) as exc:
        logger.warning("eBay response parsing failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Cardmarket scraper (Playwright + BeautifulSoup)
# ---------------------------------------------------------------------------

async def _cardmarket_lookup(
    browser: "Browser", query: str
) -> PriceResult | None:
    """Scrape Cardmarket product prices for *query* using Playwright + BS4."""
    from scraper.cardmarket import CardmarketPriceScraper

    scraper = CardmarketPriceScraper(browser)
    prices = await scraper.lookup(query, sample_size=settings.cardmarket_sample_size)

    if not prices:
        return None

    avg = sum(prices) / len(prices)
    search_url = (
        f"https://www.cardmarket.com/en/Pokemon/Products/Search"
        f"?searchString={urllib.parse.quote_plus(query)}"
    )
    return PriceResult(
        platform="Cardmarket",
        query=query,
        avg_price=round(avg, 2),
        min_price=round(min(prices), 2),
        max_price=round(max(prices), 2),
        currency="EUR",
        search_url=search_url,
        sample_count=len(prices),
        prices=prices,
    )


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

async def lookup_prices(
    session: aiohttp.ClientSession,
    query: str,
    browser: "Browser | None" = None,
) -> list[PriceResult]:
    """Return live price results for *query* from all enabled platforms.

    *session* is used for the eBay Finding API.
    *browser* is a Playwright Browser instance used for Cardmarket scraping;
    if ``None`` the Cardmarket lookup is skipped.

    Results are cached for ``settings.price_lookup_cache_ttl`` seconds.
    Returns an empty list if no platforms are enabled or all fail.
    """
    cached = _cached(query)
    if cached is not None:
        logger.debug("Price lookup cache hit for '%s'", query)
        return cached

    results: list[PriceResult] = []

    if settings.ebay_enabled:
        ebay_result = await _ebay_lookup(session, query)
        if ebay_result:
            results.append(ebay_result)
            logger.info(
                "eBay: '%s' → avg %.2f %s (%d sold)",
                query,
                ebay_result.avg_price,
                ebay_result.currency,
                ebay_result.sample_count,
            )

    if settings.cardmarket_enabled and browser is not None:
        cm_result = await _cardmarket_lookup(browser, query)
        if cm_result:
            results.append(cm_result)
            logger.info(
                "Cardmarket: '%s' → avg %.2f EUR (%d price points)",
                query,
                cm_result.avg_price,
                cm_result.sample_count,
            )
    elif settings.cardmarket_enabled and browser is None:
        logger.debug(
            "Cardmarket lookup skipped for '%s' – no browser available", query
        )

    _store_cache(query, results)
    return results


def best_market_value(price_results: list[PriceResult]) -> float | None:
    """Return the lowest average price across all platform results.

    This is used as the 'estimated market value' for deal scoring when
    live data is available, replacing the static config values.
    """
    avgs = [r.avg_price for r in price_results if r.avg_price > 0]
    return min(avgs) if avgs else None
