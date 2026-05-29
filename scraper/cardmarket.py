"""
scraper/cardmarket.py
~~~~~~~~~~~~~~~~~~~~~
Playwright-based price scraper for Cardmarket (cardmarket.com).

Cardmarket does not provide a public API, so we scrape their search
results page directly using the same browser instance that already runs
for the Vinted scraper.

The scraper navigates to the Pokémon product search page, waits for the
results table to load, and reads the trend price from each product row.
"""

from __future__ import annotations

import asyncio
import random
import re
from urllib.parse import quote_plus

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

# Cardmarket uses a React/server-rendered hybrid; these selectors target the
# rendered product table on the search results page.
#
# Primary selector: each product row in the results table.
_ROW_SEL = "div.table-body div.row"
# Within a row, the product name.
_NAME_SEL = "div.col-seller a, div.col-title a, .product-title a"
# Trend price element – Cardmarket renders prices as formatted strings like
# "1,23 €". We look for the element carrying the trend/from price.
_PRICE_SEL = (
    "div.col-price span.color-primary,"
    "span.font-weight-bold.color-primary,"
    "div.price-container span"
)
# Cookie / consent banner accept button.
_COOKIE_SEL = (
    "button#cmVendorAcceptBtn,"
    "button[data-role='acceptAll'],"
    "button.btn-primary[class*='consent'],"
    "#onetrust-accept-btn-handler"
)


def _parse_cm_price(raw: str) -> float | None:
    """Convert Cardmarket price strings like '12,34 €' or '1.234,56 €' to float."""
    # Strip everything except digits, commas and dots.
    cleaned = re.sub(r"[^\d.,]", "", raw.strip())
    if not cleaned:
        return None
    # Cardmarket uses comma as decimal separator, dot as thousands separator.
    # e.g. "1.234,56" → 1234.56
    if "," in cleaned and "." in cleaned:
        # Thousands dot present: remove dots first, then swap comma.
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


class CardmarketPriceScraper:
    """Scrapes Cardmarket search results for Pokémon product prices.

    Designed to reuse an existing Playwright ``Browser`` instance (shared
    with the Vinted scraper) so we don't launch a second browser process.
    """

    def __init__(self, browser: Browser) -> None:
        self._browser = browser
        self._cookie_accepted = False

    async def lookup(self, query: str, sample_size: int = 5) -> list[float] | None:
        """Return up to *sample_size* trend prices for *query* on Cardmarket.

        Returns ``None`` on failure, or an empty list if no prices were found.
        """
        url = _CM_SEARCH_URL.format(query=quote_plus(query))
        logger.info("Cardmarket: searching %s", url)

        context = await self._new_context()
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            await self._accept_cookies(page)
            await asyncio.sleep(random.uniform(1.5, 3.0))

            # Wait for product rows to appear.
            try:
                await page.wait_for_selector(_ROW_SEL, timeout=12_000)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Cardmarket: no product rows found for '%s'", query
                )
                return None

            rows = await page.query_selector_all(_ROW_SEL)
            logger.debug("Cardmarket: found %d rows for '%s'", len(rows), query)

            prices: list[float] = []
            for row in rows[:sample_size]:
                price_el = await row.query_selector(_PRICE_SEL)
                if not price_el:
                    continue
                raw = (await price_el.inner_text()).strip()
                price = _parse_cm_price(raw)
                if price and price > 0:
                    prices.append(price)

            if not prices:
                logger.debug("Cardmarket: no prices parsed for '%s'", query)
                return None

            return prices

        except Exception as exc:  # noqa: BLE001
            logger.warning("Cardmarket scrape error for '%s': %s", query, exc)
            return None
        finally:
            await context.close()

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
