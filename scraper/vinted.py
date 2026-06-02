"""
scraper/vinted.py
~~~~~~~~~~~~~~~~~
Vinted marketplace scraper using the `vinted_scraper` package by Giglium.

Uses Vinted's internal JSON API via httpx (no browser required).
Automatic cookie management and retries are handled by the library.

See: https://github.com/Giglium/vinted_scraper
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator
from urllib.parse import urlparse

from vinted_scraper import AsyncVintedScraper as _AsyncVintedScraper

from config.settings import settings
from scraper.base import BaseScraper, Listing
from utils.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Vinted country subdomains for filtering.
_COUNTRY_DOMAINS: dict[str, str] = {
    "NL": "https://www.vinted.nl",
    "BE": "https://www.vinted.be",
    "DE": "https://www.vinted.de",
    "FR": "https://www.vinted.fr",
    "GB": "https://www.vinted.co.uk",
    "US": "https://www.vinted.com",
    "ES": "https://www.vinted.es",
    "IT": "https://www.vinted.it",
    "PL": "https://www.vinted.pl",
    "PT": "https://www.vinted.pt",
    "LU": "https://www.vinted.lu",
    "AT": "https://www.vinted.at",
    "CZ": "https://www.vinted.cz",
    "SK": "https://www.vinted.sk",
    "HU": "https://www.vinted.hu",
    "HR": "https://www.vinted.hr",
    "RO": "https://www.vinted.ro",
    "LT": "https://www.vinted.lt",
    "LV": "https://www.vinted.lv",
    "FI": "https://www.vinted.fi",
    "SE": "https://www.vinted.se",
    "DK": "https://www.vinted.dk",
    "NO": "https://www.vinted.no",
}


def _get_base_urls() -> list[str]:
    """Return the list of Vinted base URLs to scrape based on country settings."""
    countries = settings.countries
    if not countries:
        return ["https://www.vinted.nl"]
    urls = []
    for code in countries:
        base = _COUNTRY_DOMAINS.get(code.upper())
        if base:
            urls.append(base)
        else:
            logger.warning("Unknown country code %s \u2013 skipping", code)
    return urls or ["https://www.vinted.nl"]


# Headers that mimic a real browser to avoid Vinted's 406 bot-detection.
_BROWSER_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
}


class VintedScraper(BaseScraper):
    """API-based scraper for Vinted using the `vinted_scraper` package."""

    name = "vinted"

    def __init__(self) -> None:
        # Keyed by base URL; populated in setup().
        self._scrapers: dict[str, _AsyncVintedScraper] = {}
        # Kept as None so price_lookup passes it safely (Cardmarket scraping
        # is skipped when browser is None).
        self._browser = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def setup(self) -> None:
        """Initialise one AsyncVintedScraper per configured country domain."""
        base_urls = _get_base_urls()
        for base_url in base_urls:
            try:
                scraper = await _AsyncVintedScraper.create(base_url)
                # Patch headers to avoid Vinted's 406 bot-detection on cookie
                # refresh requests (the library hits the root URL with httpx).
                if hasattr(scraper, "_client"):
                    scraper._client.headers.update(_BROWSER_HEADERS)
                    logger.debug("Patched httpx headers for %s", base_url)
                self._scrapers[base_url] = scraper
                logger.info("AsyncVintedScraper ready for %s", base_url)
            except Exception as exc:  # noqa: BLE001
                logger.error("Failed to initialise scraper for %s: %s", base_url, exc)

    async def teardown(self) -> None:
        """Close all underlying httpx clients."""
        for base_url, scraper in self._scrapers.items():
            try:
                await scraper._client.aclose()
                logger.debug("Closed httpx client for %s", base_url)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Error closing client for %s: %s", base_url, exc)
        self._scrapers.clear()
        logger.info("VintedScraper torn down")

    # ------------------------------------------------------------------
    # Core search
    # ------------------------------------------------------------------

    async def search(  # type: ignore[override]
        self, query: str, max_results: int = 30
    ) -> AsyncIterator[Listing]:
        """Yield Listing objects matching *query* from all configured countries."""
        if not self._scrapers:
            raise RuntimeError(
                "VintedScraper.setup() has not been called or no scrapers initialised"
            )

        base_urls = list(self._scrapers.keys())

        for i, base_url in enumerate(base_urls):
            async for listing in self._search_domain(query, max_results, base_url):
                yield listing
            # Polite delay between domains.
            if i < len(base_urls) - 1:
                await asyncio.sleep(2.0)

    async def _search_domain(
        self, query: str, max_results: int, base_url: str
    ) -> AsyncIterator[Listing]:
        """Yield Listings from a single Vinted domain."""
        scraper = self._scrapers.get(base_url)
        if not scraper:
            logger.warning("No scraper available for %s", base_url)
            return

        try:
            params = {
                "search_text": query,
                "per_page": max_results,
                "order": "newest_first",
            }
            items = await scraper.search(params)
            logger.info(
                "Found %d items for '%s' on %s", len(items), query, base_url
            )

            for vinted_item in items[:max_results]:
                listing = _vinted_item_to_listing(vinted_item, base_url)
                if listing:
                    yield listing

        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Error searching '%s' on %s: %s", query, base_url, exc, exc_info=True
            )
            # Attempt to refresh cookie and continue on next cycle.
            try:
                scraper.session_cookie = await scraper.refresh_cookie()
                logger.info("Cookie refreshed for %s", base_url)
            except Exception as refresh_exc:  # noqa: BLE001
                logger.error(
                    "Cookie refresh failed for %s: %s", base_url, refresh_exc
                )

    # ------------------------------------------------------------------
    # Detail page
    # ------------------------------------------------------------------

    async def get_listing(self, url: str) -> Listing | None:  # type: ignore[override]
        """Fetch full listing details by item ID extracted from the URL."""
        import re

        match = re.search(r"/items/(\d+)", url)
        if not match:
            logger.warning("Cannot extract item ID from URL: %s", url)
            return None

        item_id = match.group(1)

        # Determine the domain from the URL to resolve currency and select
        # the matching scraper.
        hostname = urlparse(url).hostname or ""
        base_url = f"https://{hostname}"

        # Prefer the scraper for the URL's own domain so that the session
        # cookie is valid for that TLD; fall back to the first available one.
        scraper = self._scrapers.get(base_url) or next(iter(self._scrapers.values()), None)
        if not scraper:
            return None

        api_failed = False
        for attempt in range(2):
            try:
                vinted_item = await scraper.item(item_id)
                return _vinted_item_to_listing(vinted_item, base_url)
            except Exception as exc:  # noqa: BLE001
                exc_str = str(exc)
                if attempt == 0 and ("403" in exc_str or "401" in exc_str):
                    logger.warning(
                        "get_listing: auth error for %s (%s) – refreshing cookie and retrying",
                        url, exc_str[:120],
                    )
                    try:
                        scraper.session_cookie = await scraper.refresh_cookie()
                        logger.info("get_listing: cookie refreshed, retrying item %s", item_id)
                    except Exception as refresh_exc:  # noqa: BLE001
                        # Cookie refresh failed (Vinted may be blocking the
                        # refresh request).  Do NOT give up here – the existing
                        # session cookie might still be valid for the item
                        # endpoint (the background search uses the same cookie
                        # and continues to work).  Let the loop proceed to
                        # attempt 1 and retry with the current cookie.
                        logger.warning(
                            "get_listing: cookie refresh failed (%s) – "
                            "retrying with existing cookie",
                            refresh_exc,
                        )
                else:
                    logger.warning(
                        "get_listing: API failed for %s (%s) – falling back to page scrape",
                        url, exc_str[:120],
                    )
                    api_failed = True
                    break

        if api_failed:
            logger.info("get_listing: attempting web page scrape for %s", url)
            listing = await self._fetch_listing_from_page(url, item_id, scraper, base_url)
            if listing is not None:
                return listing
            logger.error("Failed to fetch listing %s via API and page scrape", url)

        return None

    async def _fetch_listing_from_page(
        self, url: str, item_id: str, scraper, base_url: str
    ) -> Listing | None:
        """Fall back to scraping the Vinted item web page when the API returns 403.

        Vinted embeds item data in a ``__NEXT_DATA__`` JSON blob on the page,
        which is accessible without authentication.
        """
        import json
        import re

        try:
            # Request HTML – override Accept so the server returns the full page.
            headers = dict(_BROWSER_HEADERS)
            headers["Accept"] = (
                "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
            )
            resp = await scraper._client.get(url, headers=headers)
            if resp.status_code != 200:
                logger.warning(
                    "_fetch_listing_from_page: %s returned HTTP %d", url, resp.status_code
                )
                return None

            html = resp.text

            # Vinted is a Next.js app – item data is embedded in __NEXT_DATA__.
            m = re.search(
                r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
                html,
                re.DOTALL,
            )
            if not m:
                logger.warning(
                    "_fetch_listing_from_page: __NEXT_DATA__ script not found in page for %s"
                    " (page length: %d chars)",
                    url, len(html),
                )
                return None

            data = json.loads(m.group(1))
            page_props = data.get("props", {}).get("pageProps", {})

            # Try every known key path where Vinted may embed the item dict.
            # The field name has changed across Vinted TLDs and app versions:
            #   - "item" / "itemDto"  (classic Next.js SSR)
            #   - "initialItem"       (seen in some TLD variants)
            #   - "data" → "item"     (nested under a "data" wrapper)
            item: dict | None = None
            for key in ("item", "itemDto", "initialItem"):
                candidate = page_props.get(key)
                if candidate and isinstance(candidate, dict):
                    item = candidate
                    break

            # Also check one level deeper under a "data" wrapper.
            if item is None:
                data_wrapper = page_props.get("data")
                if isinstance(data_wrapper, dict):
                    for key in ("item", "itemDto", "initialItem"):
                        candidate = data_wrapper.get(key)
                        if candidate and isinstance(candidate, dict):
                            item = candidate
                            break

            if item is None:
                logger.warning(
                    "_fetch_listing_from_page: item data not found in pageProps for %s"
                    " (available keys: %s)",
                    url,
                    list(page_props.keys())[:20],
                )
                return None

            listing = _page_dict_to_listing(item, item_id, url, base_url)
            if listing:
                logger.info(
                    "_fetch_listing_from_page: extracted listing for item %s from page",
                    item_id,
                )
                return listing

            logger.warning(
                "_fetch_listing_from_page: _page_dict_to_listing returned None for item %s",
                item_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("_fetch_listing_from_page: failed for %s: %s", url, exc)

        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _vinted_item_to_listing(vinted_item, base_url: str) -> Listing | None:
    """Convert a VintedItem (from the library) into our Listing dataclass."""
    try:
        listing_id = str(vinted_item.id) if vinted_item.id is not None else None
        if not listing_id:
            return None

        title = (vinted_item.title or "Unknown").strip() or "Unknown"
        price = float(vinted_item.price or 0.0)
        currency = vinted_item.currency or "EUR"

        # Build absolute URL.
        raw_url = vinted_item.url or ""
        if raw_url.startswith("http"):
            item_url = raw_url
        elif raw_url:
            item_url = base_url.rstrip("/") + "/" + raw_url.lstrip("/")
        else:
            item_url = f"{base_url.rstrip('/')}/items/{listing_id}"

        # Images – prefer the full-size photo URL.
        images: list[str] = []
        if vinted_item.photos:
            for photo in vinted_item.photos:
                src = getattr(photo, "url", None) or getattr(photo, "full_size_url", None)
                if src and src.startswith("http"):
                    images.append(src)

        # Seller info.
        seller_name: str | None = None
        seller_rating: float | None = None
        if vinted_item.user:
            seller_name = getattr(vinted_item.user, "login", None)
            rep = getattr(vinted_item.user, "feedback_reputation", None)
            if rep is not None:
                seller_rating = float(rep) * 5  # 0-1 scale -> 0-5

        return Listing(
            listing_id=listing_id,
            title=title,
            price=price,
            currency=currency,
            url=item_url,
            seller_name=seller_name,
            seller_rating=seller_rating,
            images=images,
            description=vinted_item.description,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("Failed to convert VintedItem to Listing: %s", exc)
        return None


def _page_dict_to_listing(
    item: dict, item_id: str, page_url: str, base_url: str
) -> Listing | None:
    """Convert a raw item dict extracted from the Vinted page HTML into a Listing.

    Vinted embeds item data as ``__NEXT_DATA__`` JSON on each item page.  The
    structure mirrors the API response but may differ slightly between TLDs and
    over time, so all field accesses are defensive.
    """
    try:
        listing_id = str(item.get("id") or item_id)

        title = (item.get("title") or "Unknown").strip() or "Unknown"

        # Price can be a plain numeric string, a float, or a dict
        # {"amount": "2.50", "currency_code": "EUR"}.
        raw_price = item.get("price") or item.get("item_price") or {}
        if isinstance(raw_price, dict):
            price = float(raw_price.get("amount") or 0)
            currency = (
                raw_price.get("currency_code")
                or item.get("currency")
                or "EUR"
            )
        else:
            price = float(raw_price or 0)
            currency = item.get("currency") or "EUR"

        # Build absolute URL.
        raw_url = item.get("url") or item.get("path") or ""
        if raw_url.startswith("http"):
            item_url = raw_url
        elif raw_url:
            item_url = base_url.rstrip("/") + "/" + raw_url.lstrip("/")
        else:
            item_url = page_url

        # Images.
        images: list[str] = []
        for photo in item.get("photos") or []:
            if isinstance(photo, dict):
                src = (
                    photo.get("url")
                    or photo.get("full_size_url")
                    or photo.get("src")
                )
                if src and src.startswith("http"):
                    images.append(src)

        # Seller info.
        seller_name: str | None = None
        seller_rating: float | None = None
        user = item.get("user")
        if isinstance(user, dict):
            seller_name = user.get("login") or user.get("username")
            rep = user.get("feedback_reputation")
            if rep is not None:
                try:
                    seller_rating = float(rep) * 5  # 0-1 scale -> 0-5
                except (TypeError, ValueError):
                    pass

        return Listing(
            listing_id=listing_id,
            title=title,
            price=price,
            currency=currency,
            url=item_url,
            seller_name=seller_name,
            seller_rating=seller_rating,
            images=images,
            description=item.get("description"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("Failed to convert page item dict to Listing: %s", exc)
        return None
