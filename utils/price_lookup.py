"""
utils/price_lookup.py
~~~~~~~~~~~~~~~~~~~~~
Live market price comparison from eBay and Cardmarket.

eBay uses the public Finding API (requires a free App ID).
Cardmarket uses the MKM API v2.0 with OAuth 1.0 two-legged auth.

Both lookups are optional; if the required credentials are absent the
lookup is silently skipped and the scorer falls back to the static
market_values defined in config.yaml.

Results are cached in-process for ``settings.price_lookup_cache_ttl``
seconds to avoid hammering the APIs on every new listing.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import random
import string
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from config.settings import settings
from utils.logger import get_logger

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


_EBAY_SITE_GLOBAL_ID: dict[int, str] = {
    0: "US",
    3: "GB",
    77: "DE",
    71: "FR",
    101: "IT",
    186: "ES",
}


# ---------------------------------------------------------------------------
# Cardmarket MKM API v2.0
# ---------------------------------------------------------------------------

_CM_API_BASE = "https://api.cardmarket.com/ws/v2.0"


def _cm_oauth_header(
    method: str,
    url: str,
    params: dict[str, str],
    app_token: str,
    app_secret: str,
    access_token: str,
    access_token_secret: str,
) -> str:
    """Build an OAuth 1.0 Authorization header for the MKM API."""
    oauth_params = {
        "oauth_consumer_key": app_token,
        "oauth_token": access_token,
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_nonce": "".join(
            random.choices(string.ascii_letters + string.digits, k=32)
        ),
        "oauth_version": "1.0",
    }

    all_params = {**params, **oauth_params}
    sorted_params = sorted(all_params.items())
    encoded_params = urllib.parse.urlencode(sorted_params)

    base_string = "&".join(
        urllib.parse.quote(s, safe="")
        for s in [method.upper(), url, encoded_params]
    )

    signing_key = "&".join(
        urllib.parse.quote(s, safe="")
        for s in [app_secret, access_token_secret]
    )

    signature = base64.b64encode(
        hmac.new(
            signing_key.encode(),
            base_string.encode(),
            hashlib.sha1,
        ).digest()
    ).decode()

    oauth_params["oauth_signature"] = signature
    header_parts = ", ".join(
        f'{urllib.parse.quote(k, safe="")}="{urllib.parse.quote(v, safe="")}"'
        for k, v in sorted(oauth_params.items())
    )
    return f"OAuth realm=\"{url}\", {header_parts}"


async def _cardmarket_lookup(
    session: aiohttp.ClientSession, query: str
) -> PriceResult | None:
    """Fetch product prices from Cardmarket via the MKM API."""
    app_token = settings.cardmarket_app_token
    app_secret = settings.cardmarket_app_secret
    access_token = settings.cardmarket_access_token
    access_token_secret = settings.cardmarket_access_token_secret

    if not all([app_token, app_secret, access_token, access_token_secret]):
        logger.debug("Cardmarket credentials not set – skipping Cardmarket price lookup")
        return None

    search_url_web = (
        f"https://www.cardmarket.com/en/Pokemon/Products/Search"
        f"?searchString={urllib.parse.quote_plus(query)}"
    )

    endpoint = f"{_CM_API_BASE}/products/find"
    params = {"search": query, "exact": "0", "onlyExactCategory": "0"}
    query_string = urllib.parse.urlencode(params)
    full_url = f"{endpoint}?{query_string}"

    auth_header = _cm_oauth_header(
        method="GET",
        url=endpoint,
        params=params,
        app_token=app_token,  # type: ignore[arg-type]
        app_secret=app_secret,  # type: ignore[arg-type]
        access_token=access_token,  # type: ignore[arg-type]
        access_token_secret=access_token_secret,  # type: ignore[arg-type]
    )

    try:
        async with session.get(
            full_url,
            headers={
                "Authorization": auth_header,
                "User-Agent": "deal-monitor/1.0",
            },
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 204:
                logger.debug("Cardmarket: no products found for '%s'", query)
                return None
            if resp.status != 200:
                logger.warning("Cardmarket API returned HTTP %d", resp.status)
                return None
            data: dict[str, Any] = await resp.json(content_type=None)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Cardmarket lookup failed: %s", exc)
        return None

    try:
        products = data.get("product", [])
        if not products:
            return None

        prices: list[float] = []
        for product in products[: settings.cardmarket_sample_size]:
            price_guide = product.get("priceGuide", {})
            # Prefer 'TREND' (market trend price), fall back to 'AVG'
            for key in ("TREND", "AVG", "AVG1", "LOW"):
                raw = price_guide.get(key)
                if raw is not None:
                    try:
                        prices.append(float(raw))
                    except (ValueError, TypeError):
                        pass
                    break

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
            search_url=search_url_web,
            sample_count=len(prices),
            prices=prices,
        )
    except (KeyError, TypeError) as exc:
        logger.warning("Cardmarket response parsing failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

async def lookup_prices(
    session: aiohttp.ClientSession, query: str
) -> list[PriceResult]:
    """Return live price results for *query* from all enabled platforms.

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

    if settings.cardmarket_enabled:
        cm_result = await _cardmarket_lookup(session, query)
        if cm_result:
            results.append(cm_result)
            logger.info(
                "Cardmarket: '%s' → avg %.2f EUR (%d products)",
                query,
                cm_result.avg_price,
                cm_result.sample_count,
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
