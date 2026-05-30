"""
utils/price_lookup.py
~~~~~~~~~~~~~~~~~~~~~
Live market price comparison from eBay and Cardmarket.

eBay uses the public Finding API (requires a free App ID).
Cardmarket prices are fetched via the TCGGO API (RapidAPI) as the primary
source.  The legacy Playwright + BeautifulSoup scraper is available as an
optional fallback when ``settings.cardmarket_scraping_fallback`` is enabled.

Lookup priority for Cardmarket:
1. TCGGO API (when ``settings.tcggo_enabled`` and credentials are configured)
2. Playwright scraper (when ``settings.cardmarket_scraping_fallback`` is True
   and a Browser instance is provided)

Results are cached in-process for ``settings.price_lookup_cache_ttl``
seconds to avoid hammering the APIs on every new listing.
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
    from utils.tcggo import TcggoClient, TcggoCardResult

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

    # Cardmarket-specific price metrics (only set for platform == "Cardmarket")
    from_price: float | None = None      # Current market floor ("From" price)
    price_trend: float | None = None     # Conservative reference value
    avg_30_days: float | None = None     # Market value (30-day average)
    avg_7_days: float | None = None      # Recent market activity (7-day)
    avg_1_day: float | None = None       # Recent market activity (1-day)
    market_price: float | None = None    # CM Market price (from TCGGO)
    suggested_price: float | None = None # Suggested sell price (from TCGGO)

    # Confidence from the lookup source (TCGGO match quality, etc.)
    source_confidence: str = ""          # "Low" | "Medium" | "High" | ""


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
# TCGGO API lookup (primary Cardmarket source)
# ---------------------------------------------------------------------------

def _tcggo_result_to_price_result(
    tcggo_result: "TcggoCardResult",
    query: str,
    cm_url: str | None = None,
) -> PriceResult | None:
    """Convert a ``TcggoCardResult`` to the generic ``PriceResult`` format."""
    cm_vals = tcggo_result.cardmarket_values()
    if not cm_vals:
        return None

    avg = round(sum(cm_vals) / len(cm_vals), 2)
    search_url = (
        tcggo_result.cardmarket_url
        or cm_url
        or f"https://www.cardmarket.com/en/Pokemon/Products/Search"
          f"?searchString={urllib.parse.quote_plus(query)}"
    )

    return PriceResult(
        platform="Cardmarket",
        query=query,
        avg_price=avg,
        min_price=round(min(cm_vals), 2),
        max_price=round(max(cm_vals), 2),
        currency="EUR",
        search_url=search_url,
        sample_count=len(cm_vals),
        prices=cm_vals,
        from_price=tcggo_result.low_price,
        price_trend=tcggo_result.price_trend,
        avg_30_days=tcggo_result.avg_30_days,
        avg_7_days=tcggo_result.avg_7_days,
        avg_1_day=tcggo_result.avg_1_day,
        market_price=tcggo_result.market_price,
        suggested_price=tcggo_result.suggested_price,
        source_confidence=tcggo_result.confidence,
    )


async def _tcggo_lookup(
    session: aiohttp.ClientSession,
    query: str,
    cm_url: str | None = None,
    tcggo_client: "TcggoClient | None" = None,
) -> PriceResult | None:
    """Fetch Cardmarket prices for *query* via the TCGGO API.

    When *cm_url* is provided the client resolves the URL directly to a TCGGO
    product instead of performing a text search.  When *tcggo_client* is not
    supplied a new ``TcggoClient`` is constructed from settings.
    """
    from utils.tcggo import TcggoClient

    client = tcggo_client
    if client is None:
        try:
            client = TcggoClient.from_settings()
        except ValueError as exc:
            logger.debug("TcggoClient not configured: %s", exc)
            return None

    if not client.is_configured():
        logger.debug("TCGGO credentials not configured – skipping TCGGO lookup")
        return None

    try:
        if cm_url:
            tcggo_result = await client.lookup_by_url(session, cm_url)
        else:
            tcggo_result = await client.search_card(session, listing_title=query)
    except Exception as exc:  # noqa: BLE001
        logger.warning("TCGGO lookup failed for '%s': %s", query, exc)
        return None

    if not tcggo_result:
        logger.debug("TCGGO: no result for query '%s'", query)
        return None

    result = _tcggo_result_to_price_result(tcggo_result, query, cm_url)
    if result:
        logger.info(
            "TCGGO: '%s' → avg %.2f EUR (%d price points, confidence=%s)%s",
            query,
            result.avg_price,
            result.sample_count,
            tcggo_result.confidence,
            " [direct URL]" if cm_url else "",
        )
    return result


# ---------------------------------------------------------------------------
# Cardmarket scraper fallback (Playwright + BeautifulSoup)
# ---------------------------------------------------------------------------

async def _cardmarket_scraper_lookup(
    browser: "Browser", query: str, direct_url: str | None = None
) -> PriceResult | None:
    """Scrape Cardmarket product prices for *query* using Playwright + BS4.

    This is the legacy scraping path, used only when
    ``settings.cardmarket_scraping_fallback`` is enabled and a browser is
    available.  Prefer the TCGGO API path for all new deployments.

    When *direct_url* is supplied (a URL stored in identification memory) the
    scraper navigates directly to that product page, bypassing the unreliable
    Cardmarket search step entirely.
    """
    from scraper.cardmarket import CardmarketPriceScraper, normalize_cardmarket_url

    scraper = CardmarketPriceScraper(browser)

    if direct_url:
        normalized = normalize_cardmarket_url(direct_url)
        prices_dict = await scraper.lookup_url(normalized)
        search_url = normalized
    else:
        prices_dict = await scraper.lookup(query, sample_size=settings.cardmarket_sample_size)
        search_url = (
            f"https://www.cardmarket.com/en/Pokemon/Products/Search"
            f"?searchString={urllib.parse.quote_plus(query)}"
        )

    if not prices_dict:
        return None

    # Build the flat price list (for avg/min/max) in a consistent order.
    prices: list[float] = []
    for key in ("price_trend", "avg_30_days", "avg_7_days", "avg_1_day", "lowest_price"):
        v = prices_dict.get(key)
        if v is not None and v > 0:
            prices.append(v)

    if not prices:
        return None

    avg = sum(prices) / len(prices)
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
        # Cardmarket-specific metrics for deal detection
        from_price=prices_dict.get("lowest_price"),
        price_trend=prices_dict.get("price_trend"),
        avg_30_days=prices_dict.get("avg_30_days"),
        avg_7_days=prices_dict.get("avg_7_days"),
        avg_1_day=prices_dict.get("avg_1_day"),
    )


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

async def lookup_prices(
    session: aiohttp.ClientSession,
    query: str,
    browser: "Browser | None" = None,
    cm_direct_url: str | None = None,
    tcggo_client: "TcggoClient | None" = None,
) -> list[PriceResult]:
    """Return live price results for *query* from all enabled platforms.

    *session* is used for the eBay Finding API and the TCGGO API.
    *browser* is a Playwright Browser instance used for the legacy Cardmarket
    scraper fallback; pass ``None`` (default) when the scraper is not needed.
    *cm_direct_url* is an optional Cardmarket product page URL stored in
    identification memory.
    *tcggo_client* is an optional pre-constructed ``TcggoClient``; when
    ``None`` a new client is constructed from settings on each call.

    Lookup order for Cardmarket:
    1. TCGGO API (when enabled and configured).
    2. Playwright scraper fallback (when ``settings.cardmarket_scraping_fallback``
       is True and a *browser* is provided).

    Results are cached for ``settings.price_lookup_cache_ttl`` seconds.
    Returns an empty list if no platforms are enabled or all fail.
    """
    # Cache key includes the direct URL so URL-based and search-based results
    # are stored separately.
    cache_query = f"{query}|{cm_direct_url}" if cm_direct_url else query
    cached = _cached(cache_query)
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

    if settings.cardmarket_enabled:
        cm_result: PriceResult | None = None

        # --- Primary: TCGGO API ---
        if settings.tcggo_enabled:
            cm_result = await _tcggo_lookup(
                session, query, cm_url=cm_direct_url, tcggo_client=tcggo_client
            )

        # --- Fallback: Playwright scraper ---
        if cm_result is None and settings.cardmarket_scraping_fallback:
            if browser is not None:
                cm_result = await _cardmarket_scraper_lookup(
                    browser, query, direct_url=cm_direct_url
                )
                if cm_result:
                    logger.info(
                        "Cardmarket (scraper fallback): '%s' → avg %.2f EUR (%d price points)%s",
                        query,
                        cm_result.avg_price,
                        cm_result.sample_count,
                        " [direct URL]" if cm_direct_url else "",
                    )
            else:
                logger.debug(
                    "Cardmarket scraper fallback enabled but no browser provided for '%s'",
                    query,
                )
        elif cm_result is None and not settings.tcggo_enabled:
            logger.debug(
                "Cardmarket lookup skipped for '%s' – TCGGO disabled and scraper fallback off",
                query,
            )

        if cm_result:
            results.append(cm_result)

    _store_cache(cache_query, results)
    return results


def best_market_value(price_results: list[PriceResult]) -> float | None:
    """Return the best single Cardmarket market value from *price_results*.

    Preference hierarchy (Cardmarket-specific):
    1. Price Trend
    2. Market Price
    3. 30-day Average
    4. Generic Average
    5. Other Cardmarket values (7-day, 1-day, from/low)
    6. eBay average (fallback)

    When multiple Cardmarket values exist, returns the one highest in the
    hierarchy (not the lowest).  Use ``cm_price_range()`` for min/avg/max.
    """
    for result in price_results:
        if result.platform != "Cardmarket":
            continue
        for v in (
            result.price_trend,
            result.market_price,
            result.avg_30_days,
            result.avg_price,
            result.avg_7_days,
            result.avg_1_day,
            result.from_price,
            result.suggested_price,
        ):
            if v is not None and v > 0:
                return v

    # Fallback to eBay average when no Cardmarket data is available.
    avgs = [r.avg_price for r in price_results if r.avg_price > 0]
    return min(avgs) if avgs else None


def cm_price_range(
    price_results: list[PriceResult],
) -> tuple[float | None, float | None, float | None]:
    """Return *(lowest, average, highest)* of all Cardmarket price points.

    Returns ``(None, None, None)`` when no Cardmarket result is present.
    """
    for result in price_results:
        if result.platform != "Cardmarket":
            continue
        return result.min_price or None, result.avg_price or None, result.max_price or None
    return None, None, None

