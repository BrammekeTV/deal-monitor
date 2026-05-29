"""
scraper/vinted.py
~~~~~~~~~~~~~~~~~
Vinted marketplace scraper using Playwright for browser automation.

Vinted is a dynamic SPA, so we use a real browser to:
  1. Accept the cookie banner on first visit.
  2. Navigate to the search URL with filters.
  3. Parse listing cards from the rendered DOM.

Anti-bot mitigations applied:
  - Randomised delays between page actions.
  - Stealth user-agent via playwright-stealth (if installed).
  - Random viewport sizes.
  - Retry logic with exponential back-off.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
from typing import AsyncIterator
from urllib.parse import quote_plus, urlparse

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from config.settings import settings
from scraper.base import BaseScraper, Listing
from utils.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VINTED_BASE_URL = "https://www.vinted.com"
VINTED_SEARCH_URL = VINTED_BASE_URL + "/catalog?search_text={query}&order=newest_first"

# Selectors – these may change if Vinted updates their markup.
# Prefer data attributes and aria labels over fragile class names.
_ITEM_CARD_SEL = "[data-testid='item-box']"
_COOKIE_ACCEPT_SEL = "button[data-testid='gdpr-accept-all-button']"

# Fallback CSS selectors for listing card fields.
_CARD_TITLE_SEL = "[data-testid='item-box--description'] [class*='title']"
_CARD_PRICE_SEL = "[data-testid='item-box--price'] [class*='price']"
_CARD_LINK_SEL = "a[href*='/items/']"
_CARD_IMAGE_SEL = "img[src*='images']"

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

# Maps the registered domain (eTLD+1) to ISO 4217 currency code.
_DOMAIN_CURRENCY: dict[str, str] = {
    "vinted.co.uk": "GBP",
    "vinted.pl": "PLN",
    "vinted.cz": "CZK",
    "vinted.se": "SEK",
    "vinted.dk": "DKK",
    "vinted.no": "NOK",
    "vinted.hu": "HUF",
    "vinted.ro": "RON",
}


def _currency_for_base_url(base_url: str) -> str:
    """Return the ISO 4217 currency code that corresponds to a Vinted domain.

    Parses the *hostname* of the URL properly so that a substring match
    cannot be tricked by a crafted path component (e.g. ``/vinted.co.uk``).
    """
    hostname = urlparse(base_url).hostname or ""
    # Strip leading "www." to normalise.
    if hostname.startswith("www."):
        hostname = hostname[4:]
    return _DOMAIN_CURRENCY.get(hostname, "EUR")


def _get_base_urls() -> list[str]:
    """Return the list of Vinted base URLs to scrape based on country settings."""
    countries = settings.countries
    if not countries:
        return [VINTED_BASE_URL]
    urls = []
    for code in countries:
        base = _COUNTRY_DOMAINS.get(code.upper())
        if base:
            urls.append(base)
        else:
            logger.warning("Unknown country code %s – skipping", code)
    return urls or [VINTED_BASE_URL]


async def _random_delay(min_s: float | None = None, max_s: float | None = None) -> None:
    lo = min_s if min_s is not None else settings.page_delay_min
    hi = max_s if max_s is not None else settings.page_delay_max
    await asyncio.sleep(random.uniform(lo, hi))


def _random_viewport() -> dict[str, int]:
    widths = [1280, 1366, 1440, 1600, 1920]
    w = random.choice(widths)
    return {"width": w, "height": int(w * 0.5625)}  # 16:9 ratio


def _extract_listing_id(url: str) -> str | None:
    """Extract numeric Vinted listing ID from a URL."""
    match = re.search(r"/items/(\d+)", url)
    return match.group(1) if match else None


def _parse_price(raw: str) -> float | None:
    """Convert a price string like '€ 12,50' or '12.50' to float."""
    cleaned = re.sub(r"[^\d.,]", "", raw).replace(",", ".")
    # Handle edge cases like '12.50.00'
    parts = cleaned.split(".")
    if len(parts) > 2:
        # Only last two decimal digits are cents
        cleaned = "".join(parts[:-1]) + "." + parts[-1]
    try:
        return float(cleaned)
    except ValueError:
        return None


class VintedScraper(BaseScraper):
    """Playwright-based scraper for Vinted."""

    name = "vinted"

    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._cookie_accepted: set[str] = set()  # per-domain

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def setup(self) -> None:
        """Launch the browser."""
        logger.info("Launching %s browser (headless=%s)", settings.browser, settings.headless)
        self._playwright = await async_playwright().start()
        launcher = getattr(self._playwright, settings.browser)
        self._browser = await launcher.launch(
            headless=settings.headless,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        logger.info("Browser ready")

    async def teardown(self) -> None:
        """Close the browser and Playwright instance."""
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        logger.info("Browser closed")

    # ------------------------------------------------------------------
    # Cookie consent
    # ------------------------------------------------------------------

    async def _accept_cookies(self, page: Page, base_url: str) -> None:
        """Click the GDPR cookie-accept button if present."""
        if base_url in self._cookie_accepted:
            return
        try:
            btn = page.locator(_COOKIE_ACCEPT_SEL)
            if await btn.count() > 0:
                await btn.first.click()
                logger.debug("Cookie banner accepted on %s", base_url)
                await _random_delay(1.0, 2.0)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Cookie accept failed (probably not shown): %s", exc)
        self._cookie_accepted.add(base_url)

    # ------------------------------------------------------------------
    # Core search
    # ------------------------------------------------------------------

    async def search(self, query: str, max_results: int = 30) -> AsyncIterator[Listing]:  # type: ignore[override]
        """Yield Listing objects matching *query* from all configured countries."""
        if not self._browser:
            raise RuntimeError("VintedScraper.setup() has not been called")

        base_urls = _get_base_urls()

        for base_url in base_urls:
            async for listing in self._search_domain(query, max_results, base_url):
                yield listing
            # Polite delay between domains.
            if len(base_urls) > 1:
                await _random_delay(2.0, 4.0)

    async def _search_domain(
        self, query: str, max_results: int, base_url: str
    ) -> AsyncIterator[Listing]:
        """Scrape one Vinted domain for *query*."""
        context = await self._new_context()
        page = await context.new_page()

        try:
            # First visit the homepage to get cookies / consent.
            await self._navigate_with_retry(page, base_url)
            await self._accept_cookies(page, base_url)

            search_url = f"{base_url}/catalog?search_text={quote_plus(query)}&order=newest_first"
            logger.info("Searching: %s", search_url)
            await self._navigate_with_retry(page, search_url)
            await _random_delay()

            # Wait for item cards to load.
            try:
                await page.wait_for_selector(_ITEM_CARD_SEL, timeout=15_000)
            except Exception:  # noqa: BLE001
                logger.warning("No item cards found for query '%s' on %s", query, base_url)
                return

            cards = await page.query_selector_all(_ITEM_CARD_SEL)
            logger.info("Found %d cards for '%s' on %s", len(cards), query, base_url)

            count = 0
            for card in cards[:max_results]:
                listing = await self._parse_card(card, base_url)
                if listing:
                    count += 1
                    yield listing

            logger.debug("Yielded %d listings for '%s' on %s", count, query, base_url)

        except Exception as exc:  # noqa: BLE001
            logger.error("Error searching '%s' on %s: %s", query, base_url, exc, exc_info=True)
        finally:
            await context.close()

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    async def _parse_card(self, card, base_url: str) -> Listing | None:
        """Extract a Listing from a DOM card element."""
        try:
            # URL + ID
            link_el = await card.query_selector(_CARD_LINK_SEL)
            if not link_el:
                return None
            href = await link_el.get_attribute("href") or ""
            url = href if href.startswith("http") else base_url + href
            listing_id = _extract_listing_id(url)
            if not listing_id:
                return None

            # Title
            title_el = await card.query_selector(_CARD_TITLE_SEL)
            title = (await title_el.inner_text()).strip() if title_el else "Unknown"
            if not title:
                # Fallback: use image alt text.
                img_el = await card.query_selector(_CARD_IMAGE_SEL)
                if img_el:
                    title = (await img_el.get_attribute("alt") or "Unknown").strip()

            # Price
            price_el = await card.query_selector(_CARD_PRICE_SEL)
            price_raw = (await price_el.inner_text()).strip() if price_el else "0"
            price = _parse_price(price_raw) or 0.0

            # Currency – derive from the domain portion of the base URL only.
            currency = _currency_for_base_url(base_url)

            # Images
            images: list[str] = []
            img_els = await card.query_selector_all("img")
            for img_el in img_els:
                src = await img_el.get_attribute("src")
                if src and "images" in src:
                    images.append(src)

            # Seller – Vinted cards don't always expose the seller directly;
            # we fall back to extracting it from the URL path or skip it.
            seller_match = re.search(r"vinted\.[a-z.]+/([^/]+)/items/", url)
            seller_name = seller_match.group(1) if seller_match else None

            return Listing(
                listing_id=listing_id,
                title=title,
                price=price,
                currency=currency,
                url=url,
                seller_name=seller_name,
                seller_rating=None,  # Requires visiting the profile page
                images=images,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Failed to parse card: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Detail page (optional enrichment)
    # ------------------------------------------------------------------

    async def get_listing(self, url: str) -> Listing | None:
        """Fetch full listing details by navigating to the listing page."""
        if not self._browser:
            raise RuntimeError("VintedScraper.setup() has not been called")

        context = await self._new_context()
        page = await context.new_page()
        try:
            await self._navigate_with_retry(page, url)
            await _random_delay()

            # Extract data from the __NEXT_DATA__ JSON blob if available (fastest).
            listing = await self._extract_next_data(page, url)
            if listing:
                return listing

            # Fallback: manual DOM extraction.
            return await self._extract_listing_dom(page, url)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to fetch listing %s: %s", url, exc, exc_info=True)
            return None
        finally:
            await context.close()

    async def _extract_next_data(self, page: Page, url: str) -> Listing | None:
        """Try to parse the __NEXT_DATA__ JSON embedded by Next.js."""
        try:
            data_raw: str = await page.evaluate(
                "() => document.getElementById('__NEXT_DATA__')?.textContent || ''"
            )
            if not data_raw:
                return None
            data = json.loads(data_raw)
            item = (
                data.get("props", {})
                .get("pageProps", {})
                .get("item")
            )
            if not item:
                return None

            listing_id = str(item.get("id", ""))
            title = item.get("title", "Unknown")
            price = float(item.get("price_numeric", 0))
            currency = item.get("currency", "EUR")
            seller = item.get("user", {}).get("login")
            seller_rating_raw = item.get("user", {}).get("feedback_reputation")
            seller_rating = float(seller_rating_raw) * 5 if seller_rating_raw else None
            photos = [
                p.get("url", "")
                for p in item.get("photos", [])
                if p.get("url")
            ]
            description = item.get("description", "")

            return Listing(
                listing_id=listing_id,
                title=title,
                price=price,
                currency=currency,
                url=url,
                seller_name=seller,
                seller_rating=seller_rating,
                images=photos,
                description=description,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("__NEXT_DATA__ extraction failed: %s", exc)
            return None

    async def _extract_listing_dom(self, page: Page, url: str) -> Listing | None:
        """Fallback DOM scrape for a listing detail page."""
        try:
            listing_id = _extract_listing_id(url)
            if not listing_id:
                return None
            title_el = await page.query_selector("h1")
            title = (await title_el.inner_text()).strip() if title_el else "Unknown"
            price_el = await page.query_selector("[itemprop='price']")
            price_raw = (
                await price_el.get_attribute("content") or "0"
                if price_el
                else "0"
            )
            price = float(price_raw) if price_raw else 0.0
            images = await page.evaluate(
                """() => Array.from(document.querySelectorAll('img[src*="images"]'))
                         .map(i => i.src)"""
            )
            return Listing(
                listing_id=listing_id,
                title=title,
                price=price,
                currency="EUR",
                url=url,
                images=images or [],
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("DOM extraction failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _new_context(self) -> BrowserContext:
        vp = _random_viewport()
        return await self._browser.new_context(  # type: ignore[union-attr]
            viewport=vp,
            locale="en-GB",
            timezone_id="Europe/Amsterdam",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            java_script_enabled=True,
        )

    async def _navigate_with_retry(
        self, page: Page, url: str, retries: int | None = None
    ) -> None:
        """Navigate to *url* with retry + exponential back-off."""
        max_retries = retries if retries is not None else settings.max_retries
        for attempt in range(1, max_retries + 1):
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                return
            except Exception as exc:  # noqa: BLE001
                if attempt == max_retries:
                    raise
                wait = settings.retry_delay * (2 ** (attempt - 1))
                logger.warning(
                    "Navigation failed (attempt %d/%d): %s – retrying in %ds",
                    attempt,
                    max_retries,
                    exc,
                    wait,
                )
                await asyncio.sleep(wait)
