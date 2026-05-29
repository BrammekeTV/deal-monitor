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
  4. Wait for dynamic content (the price info block) to render.
  5. Parse the structured price info block (``.info-list-container``) with
     BeautifulSoup to extract: price trend, lowest price, 30-day / 7-day /
     1-day averages.
  6. Fall back to embedded JSON data in ``<script>`` tags if the HTML block
     is absent.

This two-step approach gives richer, more reliable price data than trying
to scrape prices from the summary search-results table.

A page is considered successfully parsed only when at least one pricing
metric has been extracted.  Search pages, category pages, and error pages
are rejected by the product-URL filter and the empty-prices guard.
"""

from __future__ import annotations

import asyncio
import json
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

# Price info container on product pages; we wait for this to be rendered.
_PRICE_CONTAINER_SEL = ".info-list-container"

# Minimum path-segment depth for a product page URL (not a category/search page).
# e.g. /en/Pokemon/Products/Singles/Base-Set/Charizard  → 6 segments
_MIN_PRODUCT_PATH_DEPTH = 6

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

    Tries two strategies in order:
    1. Parse the ``dl`` inside ``.info-list-container`` (rendered HTML).
    2. Search ``<script>`` tags for embedded JSON price data as a fallback.

    Returns a dict with keys: ``lowest_price``, ``price_trend``,
    ``avg_30_days``, ``avg_7_days``, ``avg_1_day``.  Missing fields are
    omitted from the dict.

    A page is only considered successfully parsed when the returned dict is
    non-empty (i.e. at least one pricing metric was found).
    """
    soup = BeautifulSoup(html_content, "html.parser")
    prices: dict[str, float] = {}

    # ── Strategy 1: rendered info-list-container ──────────────────────────
    container = soup.select_one(_PRICE_CONTAINER_SEL)
    if container:
        # The .info-list-container class may be on the <dl> itself, or the
        # <dl> may be a child.  Handle both cases so the parser works with
        # both the old and current Cardmarket HTML structure.
        dl = container if container.name == "dl" else container.select_one("dl")
        if dl:
            dt_elements = dl.select("dt")
            dd_elements = dl.select("dd")

            price_data: dict[str, str] = {}
            for i in range(min(len(dt_elements), len(dd_elements))):
                key = dt_elements[i].get_text().strip()
                v_el = dd_elements[i].select_one("span")
                value = v_el.get_text().strip() if v_el else dd_elements[i].get_text().strip()
                price_data[key] = _clean_price_string(value)

            # Lowest / From price  (labels: "From" / "De" / "Ab" / "Vanaf")
            for key, value in price_data.items():
                if key.lower() in ("from", "de", "ab", "vanaf"):
                    v = _parse_price_to_float(value)
                    if v > 0:
                        prices["lowest_price"] = v
                    break

            # Price trend  (labels containing "trend" / "tendance" / "prijstrend")
            for key, value in price_data.items():
                if any(t in key.lower() for t in ("trend", "tendance")):
                    v = _parse_price_to_float(value)
                    if v > 0:
                        prices["price_trend"] = v
                    break

            # 30-day average
            for key, value in price_data.items():
                if "30" in key and any(t in key.lower() for t in ("day", "jour", "tage", "dag")):
                    v = _parse_price_to_float(value)
                    if v > 0:
                        prices["avg_30_days"] = v
                    break

            # 7-day average
            for key, value in price_data.items():
                if "7" in key and any(t in key.lower() for t in ("day", "jour", "tage", "dag")):
                    v = _parse_price_to_float(value)
                    if v > 0:
                        prices["avg_7_days"] = v
                    break

            # 1-day average
            for key, value in price_data.items():
                if "1" in key and any(t in key.lower() for t in ("day", "jour", "tage", "dag")):
                    v = _parse_price_to_float(value)
                    if v > 0:
                        prices["avg_1_day"] = v
                    break

    if prices:
        return prices

    logger.debug("Cardmarket: .info-list-container empty or absent – trying JSON fallback")

    # ── Strategy 2: embedded JSON in <script> tags ────────────────────────
    prices = _extract_json_prices(soup)
    if not prices:
        logger.debug("Cardmarket: no pricing data found in page HTML or embedded JSON")
    return prices


def _extract_json_prices(soup: BeautifulSoup) -> dict[str, float]:
    """Attempt to extract price metrics from embedded ``<script>`` JSON blobs.

    Cardmarket (and other SPAs) sometimes embed product data as a JSON object
    assigned to a global variable (e.g. ``window.__cm_state`` or similar).
    We scan every ``<script>`` tag for recognisable price keys.
    """
    prices: dict[str, float] = {}

    # Key mappings: JSON field name patterns → our canonical keys.
    _JSON_KEY_MAP: list[tuple[tuple[str, ...], str]] = [
        (("lowPrice", "lowestPrice", "fromPrice", "minPrice"), "lowest_price"),
        (("trendPrice", "priceTrend", "trend"), "price_trend"),
        (("avg30", "avg30Days", "averagePrice30"), "avg_30_days"),
        (("avg7", "avg7Days", "averagePrice7"), "avg_7_days"),
        (("avg1", "avg1Day", "averagePrice1"), "avg_1_day"),
    ]

    for script_tag in soup.find_all("script"):
        text = script_tag.string or ""
        if not text or len(text) < 20:
            continue

        # Look for JSON objects that contain price-like keys.
        # Extract all {...} blocks and try to parse them.
        for match in re.finditer(r"\{[^{}]{20,}\}", text):
            candidate = match.group(0)
            # Quick pre-filter: skip if no recognisable price key is present.
            if not any(
                k in candidate
                for keys, _ in _JSON_KEY_MAP
                for k in keys
            ):
                continue
            try:
                obj = json.loads(candidate)
            except (ValueError, json.JSONDecodeError):
                continue

            for json_keys, canonical in _JSON_KEY_MAP:
                if canonical in prices:
                    continue
                for jk in json_keys:
                    raw = obj.get(jk)
                    if raw is None:
                        continue
                    try:
                        v = float(raw)
                    except (ValueError, TypeError):
                        v = _parse_price_to_float(str(raw))
                    if v > 0:
                        prices[canonical] = v
                        break

            if len(prices) >= 3:
                # Enough data found; stop scanning.
                return prices

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
    2. Find the first product link (category/search pages are filtered out).
    3. Open that product page.
    4. Wait for the price info block to be rendered (dynamic content).
    5. Parse the price info block (HTML + JSON fallback).
    6. Return the collected prices dict or ``None`` if no metrics found.
    """

    def __init__(self, browser: Browser) -> None:
        self._browser = browser
        self._cookie_accepted = False

    async def lookup(self, query: str, sample_size: int = 5) -> dict[str, float] | None:
        """Return price data for *query* scraped from Cardmarket.

        *sample_size* is accepted for API compatibility but is unused; the
        product-page approach returns up to 5 named price points regardless
        (``price_trend``, ``avg_30_days``, ``avg_7_days``, ``avg_1_day``,
        ``lowest_price``).

        Returns a non-empty ``dict[str, float]`` on success, ``None`` on
        failure.  The page is not considered successfully parsed unless at
        least one pricing metric is extracted.
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
            prices_dict = await self._fetch_product_page(page, product_url)

            if not prices_dict:
                logger.debug("Cardmarket: no prices parsed for '%s'", query)
                return None

            logger.debug("Cardmarket: '%s' → %s", query, prices_dict)
            return prices_dict

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
            await self._accept_cookies(page)
            prices_dict = await self._fetch_product_page(page, url)

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

    async def _fetch_product_page(
        self, page: Page, url: str
    ) -> dict[str, float] | None:
        """Navigate to *url*, wait for dynamic content, and parse prices.

        Waits for ``.info-list-container`` to appear in the DOM so that
        JavaScript-rendered pricing data has a chance to load before we
        snapshot the HTML.  Falls back to a timed delay if the element
        never appears (some pages render differently).
        """
        await page.goto(url, wait_until="load", timeout=30_000)

        # Wait for the price container to be rendered (dynamic content).
        try:
            await page.wait_for_selector(
                _PRICE_CONTAINER_SEL, timeout=10_000, state="visible"
            )
        except Exception:  # noqa: BLE001
            # Element did not appear within the timeout – take what we have.
            logger.debug(
                "Cardmarket: price container not visible within timeout for %s", url
            )
            await asyncio.sleep(random.uniform(1.0, 2.0))

        html = await page.content()
        prices_dict = _parse_product_page(html)
        return prices_dict if prices_dict else None

    async def _find_first_product_url(self, page: Page) -> str | None:
        """Return the href of the first product link on the search results page.

        Filters out category and search pages: a valid individual product URL
        must have at least ``_MIN_PRODUCT_PATH_DEPTH`` path segments.
        """
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
            if href.count("/") >= _MIN_PRODUCT_PATH_DEPTH:
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
        # Rotate through recent Chrome stable versions to reduce fingerprinting.
        chrome_versions = ["124.0.0.0", "125.0.0.0", "131.0.0.0", "132.0.0.0", "136.0.0.0"]
        chrome_ver = random.choice(chrome_versions)
        return await self._browser.new_context(
            viewport={"width": w, "height": int(w * 0.5625)},
            locale="en-GB",
            timezone_id="Europe/Amsterdam",
            user_agent=(
                f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                f"AppleWebKit/537.36 (KHTML, like Gecko) "
                f"Chrome/{chrome_ver} Safari/537.36"
            ),
            java_script_enabled=True,
            extra_http_headers={
                "Accept-Language": "en-GB,en;q=0.9",
            },
        )
