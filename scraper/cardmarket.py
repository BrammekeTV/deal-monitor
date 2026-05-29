"""
scraper/cardmarket.py
~~~~~~~~~~~~~~~~~~~~~
Playwright-based price scraper for Cardmarket (cardmarket.com).

Cardmarket does not provide a public API, so we scrape their pages
directly using a Playwright browser (needed to pass Cloudflare).

Approach (inspired by DrankRock/AutoScrape cardmarket_parser plugin):
  1. Navigate to the Pokémon product search page.
  2. Find the URL of the first matching product.
  3. Navigate to that product page.
  4. Parse the structured price info block (``.info-list-container``) with
     BeautifulSoup to extract: price trend, lowest price, 30-day / 7-day /
     1-day averages.

This two-step approach gives richer, more reliable price data than trying
to scrape prices from the summary search-results table.
"""

from __future__ import annotations

import asyncio
import random
import re
from typing import Any
from urllib.parse import quote_plus

from bs4 import BeautifulSoup
from playwright.async_api import Browser, BrowserContext, Page

from utils.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CM_BASE = "https://www.cardmarket.com"
_CM_SEARCH_URL = (
    _CM_BASE
    + "/en/Pokemon/Products/Search"
    + "?searchString={query}&sortBy=price_asc&perSite=20"
)

# Cookie / consent banner accept button selectors.
_COOKIE_SEL = (
    "button#cmVendorAcceptBtn,"
    "button[data-role='acceptAll'],"
    "button.btn-primary[class*='consent'],"
    "#onetrust-accept-btn-handler"
)

# Search-results page: any link pointing to a Pokemon product.
_PRODUCT_LINK_SEL = "a[href*='/en/Pokemon/Products/']"


# ---------------------------------------------------------------------------
# HTML parsing helpers  (AutoScrape / cardmarket_parser inspired)
# ---------------------------------------------------------------------------

def _clean_price_string(price_string: str) -> str:
    """Fix mojibake encoding issues in price strings (â‚¬ → €, etc.)."""
    if not price_string:
        return ""
    return (
        price_string
        .replace("â\x82¬", "€")
        .replace("Â£", "£")
        .strip()
    )


def _parse_price_to_float(price_string: str) -> float:
    """Parse a European-format price string (``"13,98 €"``) into a float.

    Handles both comma-as-decimal (``13,98``) and dot-as-thousands-separator
    (``1.234,56``) formats.
    """
    if not price_string:
        return 0.0
    try:
        cleaned = "".join(c for c in price_string if c.isdigit() or c in ",.").strip()
        if "," in cleaned and "." in cleaned:
            # European with thousands separator: "1.234,56" → "1234.56"
            cleaned = cleaned.replace(".", "").replace(",", ".")
        elif "," in cleaned:
            # Decimal comma only: "13,98" → "13.98"
            cleaned = cleaned.replace(",", ".")
        return float(cleaned)
    except (ValueError, AttributeError):
        return 0.0


def _parse_product_page(html_content: str) -> dict[str, float]:
    """Extract price fields from a Cardmarket product page.

    Returns a dict with keys: ``lowest_price``, ``price_trend``,
    ``avg_30_days``, ``avg_7_days``, ``avg_1_day``.  Missing fields are
    omitted from the dict.
    """
    soup = BeautifulSoup(html_content, "html.parser")
    prices: dict[str, float] = {}

    container = soup.select_one(".info-list-container")
    if not container:
        logger.debug("Cardmarket: .info-list-container not found in page HTML")
        return prices

    dl = container.select_one("dl")
    if not dl:
        return prices

    dt_elements = dl.select("dt")
    dd_elements = dl.select("dd")

    price_data: dict[str, str] = {}
    for i in range(min(len(dt_elements), len(dd_elements))):
        key = dt_elements[i].get_text().strip()
        v_el = dd_elements[i].select_one("span")
        value = v_el.get_text().strip() if v_el else dd_elements[i].get_text().strip()
        price_data[key] = _clean_price_string(value)

    # Lowest price  (labels: "From" / "De" / "Ab")
    for key, value in price_data.items():
        if key.lower() in ("from", "de", "ab"):
            v = _parse_price_to_float(value)
            if v > 0:
                prices["lowest_price"] = v
            break

    # Price trend  (labels containing "trend" / "tendance")
    for key, value in price_data.items():
        if any(t in key.lower() for t in ("trend", "tendance")):
            v = _parse_price_to_float(value)
            if v > 0:
                prices["price_trend"] = v
            break

    # 30-day average
    for key, value in price_data.items():
        if "30" in key and any(t in key.lower() for t in ("day", "jour", "tage")):
            v = _parse_price_to_float(value)
            if v > 0:
                prices["avg_30_days"] = v
            break

    # 7-day average
    for key, value in price_data.items():
        if "7" in key and any(t in key.lower() for t in ("day", "jour", "tage")):
            v = _parse_price_to_float(value)
            if v > 0:
                prices["avg_7_days"] = v
            break

    # 1-day average
    for key, value in price_data.items():
        if "1" in key and any(t in key.lower() for t in ("day", "jour", "tage")):
            v = _parse_price_to_float(value)
            if v > 0:
                prices["avg_1_day"] = v
            break

    return prices


# ---------------------------------------------------------------------------
# Main scraper class
# ---------------------------------------------------------------------------

class CardmarketPriceScraper:
    """Scrapes Cardmarket product pages for Pokémon prices.

    Reuses an existing Playwright ``Browser`` instance (managed externally)
    so only one browser process is needed for the whole application.

    Flow per ``lookup()`` call:
    1. Open search results page for *query*.
    2. Find the first product link.
    3. Open that product page.
    4. Parse the price info block with BeautifulSoup.
    5. Return the collected prices as a plain list of floats.
    """

    def __init__(self, browser: Browser) -> None:
        self._browser = browser
        self._cookie_accepted = False

    async def lookup(self, query: str, sample_size: int = 5) -> list[float] | None:
        """Return price data for *query* scraped from Cardmarket.

        *sample_size* is accepted for API compatibility but the product-page
        approach returns up to 5 price points regardless (trend, lowest,
        avg_30, avg_7, avg_1).

        Returns a non-empty list of floats on success, ``None`` on failure.
        """
        search_url = _CM_SEARCH_URL.format(query=quote_plus(query))
        logger.info("Cardmarket: searching '%s'", query)

        context = await self._new_context()
        page = await context.new_page()
        try:
            # ── Step 1: search page ──────────────────────────────────────────
            await page.goto(search_url, wait_until="domcontentloaded", timeout=30_000)
            await self._accept_cookies(page)
            await asyncio.sleep(random.uniform(1.5, 3.0))

            product_url = await self._find_first_product_url(page)
            if not product_url:
                logger.warning("Cardmarket: no product link found for '%s'", query)
                return None

            # ── Step 2: product page ─────────────────────────────────────────
            logger.debug("Cardmarket: navigating to product page %s", product_url)
            await page.goto(product_url, wait_until="domcontentloaded", timeout=30_000)
            await asyncio.sleep(random.uniform(1.0, 2.0))

            html = await page.content()
            prices_dict = _parse_product_page(html)

            if not prices_dict:
                logger.debug("Cardmarket: no prices parsed for '%s'", query)
                return None

            # Prefer trend price as primary value; add averages and lowest.
            prices: list[float] = []
            for key in ("price_trend", "avg_30_days", "avg_7_days", "avg_1_day", "lowest_price"):
                v = prices_dict.get(key)
                if v is not None and v > 0:
                    prices.append(v)

            logger.debug(
                "Cardmarket: '%s' → prices %s (from %s)",
                query, prices, prices_dict,
            )
            return prices if prices else None

        except Exception as exc:  # noqa: BLE001
            logger.warning("Cardmarket scrape error for '%s': %s", query, exc)
            return None
        finally:
            await context.close()

    async def lookup_url(self, url: str) -> dict[str, float] | None:
        """Fetch price data directly from a Cardmarket product page URL.

        Returns the raw prices dict (``lowest_price``, ``price_trend``,
        ``avg_30_days``, …) on success, or ``None`` on failure.
        """
        logger.info("Cardmarket: fetching prices from URL %s", url)
        context = await self._new_context()
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            await self._accept_cookies(page)
            await asyncio.sleep(random.uniform(1.0, 2.0))

            html = await page.content()
            prices_dict = _parse_product_page(html)

            if not prices_dict:
                logger.debug("Cardmarket lookup_url: no prices parsed from %s", url)
                return None

            logger.debug("Cardmarket lookup_url: %s → %s", url, prices_dict)
            return prices_dict

        except Exception as exc:  # noqa: BLE001
            logger.warning("Cardmarket lookup_url error for %s: %s", url, exc)
            return None
        finally:
            await context.close()

    async def _find_first_product_url(self, page: Page) -> str | None:
        """Return the href of the first product link on the search results page."""
        try:
            # Wait briefly for at least one product link to appear.
            await page.wait_for_selector(_PRODUCT_LINK_SEL, timeout=12_000)
        except Exception:  # noqa: BLE001
            return None

        links = await page.query_selector_all(_PRODUCT_LINK_SEL)
        for link in links:
            href = await link.get_attribute("href")
            if not href:
                continue
            # Filter out category/search links – we want individual product pages.
            # Product pages have at least 5 path segments after /en/Pokemon/Products/.
            if href.count("/") >= 6:
                full_url = href if href.startswith("http") else _CM_BASE + href
                return full_url
        return None

    async def _accept_cookies(self, page: Page) -> None:
        """Dismiss the Cardmarket cookie / consent banner if present."""
        if self._cookie_accepted:
            return
        try:
            btn = page.locator(_COOKIE_SEL)
            if await btn.count() > 0:
                await btn.first.click()
                logger.debug("Cardmarket: cookie banner accepted")
                await asyncio.sleep(1.0)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Cardmarket: cookie accept skipped: %s", exc)
        self._cookie_accepted = True

    async def _new_context(self) -> BrowserContext:
        widths = [1280, 1366, 1440, 1920]
        w = random.choice(widths)
        return await self._browser.new_context(
            viewport={"width": w, "height": int(w * 0.5625)},
            locale="en-GB",
            timezone_id="Europe/Amsterdam",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            java_script_enabled=True,
        )
