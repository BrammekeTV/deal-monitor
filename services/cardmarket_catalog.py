"""
services/cardmarket_catalog.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Cardmarket Product Catalog Service.

Downloads and caches two Cardmarket S3 JSON files that together replace the
need to scrape individual product pages via a browser:

* **Product Catalog** (``products_singles_6.json``)
  – one entry per card/printing with ``idProduct``, ``name``, ``number``,
  ``expansionName``, ``idExpansion``, etc.

* **Price Guide** (``price_guide_6.json``)
  – one entry per product with ``idProduct``, ``LOW``, ``TREND``,
  ``AVG1``, ``AVG7``, ``AVG30``.

Both files are refreshed every ``refresh_hours`` hours (default: 24).  Between
refreshes the data is kept in memory and optionally persisted to a local cache
directory so a restart does not require an immediate re-download.

Usage::

    catalog = CardmarketCatalog(cache_dir=Path("data/catalog_cache"))
    await catalog.load()

    # Find a product by card identifier:
    product = catalog.find_product(
        card_name="Charizard ex",
        collector_number="125/197",
        set_name="Obsidian Flames",
    )
    if product:
        price_data = catalog.get_price_data(product)
        # price_data is a CardmarketPriceData instance ready for comparison.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp

from utils.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# S3 download URLs
# ---------------------------------------------------------------------------

_PRODUCT_CATALOG_URL = (
    "https://downloads.s3.cardmarket.com/productCatalog/productList/products_singles_6.json"
)
_PRICE_GUIDE_URL = (
    "https://downloads.s3.cardmarket.com/productCatalog/priceGuide/price_guide_6.json"
)

# HTTP timeout for catalog downloads (the files can be several MB).
_DOWNLOAD_TIMEOUT_SECS = 120


# ---------------------------------------------------------------------------
# Price data container
# ---------------------------------------------------------------------------

@dataclass
class CatalogPriceData:
    """Pricing data for a single product retrieved from the Cardmarket Price Guide."""

    product_id: int
    product_name: str | None

    # Prices from the price guide (all in EUR).
    # ``from_price`` is the global lowest current asking price (``LOW`` field).
    # Note: this is a *global* price, not filtered by Dutch sellers.
    from_price: float | None = None
    price_trend: float | None = None
    avg_30_days: float | None = None
    avg_7_days: float | None = None
    avg_1_day: float | None = None

    # Cardmarket product URL, built from catalog data.
    product_url: str | None = None

    # Metadata
    set_name: str | None = None
    card_number: str | None = None
    id_expansion: int | None = None

    def is_valid(self) -> bool:
        return self.from_price is not None and self.from_price > 0


# ---------------------------------------------------------------------------
# Slug-building helpers (mirrors logic in scraper/cardmarket.py)
# ---------------------------------------------------------------------------

def _expansion_name_to_slug(expansion_name: str) -> str:
    """Convert a Cardmarket expansion name to its URL slug.

    Examples::

        "Obsidian Flames"  → "Obsidian-Flames"
        "Pokémon GO"       → "Pokemon-GO"
        "151"              → "151"
    """
    # Replace accented characters with ASCII equivalents.
    name = expansion_name.replace("é", "e").replace("É", "E")
    # Replace runs of non-alphanumeric characters with a single hyphen.
    slug = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-")
    return slug


def _card_name_to_slug(card_name: str) -> str:
    """Convert a card name to its URL slug component."""
    return re.sub(r"[^A-Za-z0-9]+", "-", card_name).strip("-")


def _bare_number(number_str: str) -> str:
    """Return the bare numeric part of a collector number.

    Examples::

        "125/197"  → "125"
        "025"      → "025"
        "SVP 214"  → "214"
        "214"      → "214"
    """
    if not number_str:
        return ""
    return number_str.split("/")[0].strip()


# ---------------------------------------------------------------------------
# Normalisation helpers for catalog search
# ---------------------------------------------------------------------------

def _normalise_number_for_search(number_str: str) -> str:
    """Return a canonical form of a collector number for index lookup.

    Strips leading zeros and the denominator so "025/193" and "25" both
    map to the same key.
    """
    if not number_str:
        return ""
    bare = number_str.split("/")[0].strip()
    digits = re.sub(r"[^0-9]", "", bare)
    if digits:
        try:
            return str(int(digits))
        except ValueError:
            pass
    return bare.lower()


def _normalise_name_for_search(name: str) -> str:
    """Lower-case, drop punctuation/extra spaces for fuzzy comparison."""
    return re.sub(r"[^a-z0-9 ]", "", name.lower()).strip()


def _normalise_expansion_name(name: str) -> str:
    """Normalise an expansion name for comparison."""
    return re.sub(r"[^a-z0-9 ]", "", name.lower().replace("-", " ").replace("é", "e")).strip()


# ---------------------------------------------------------------------------
# CardmarketCatalog
# ---------------------------------------------------------------------------

class CardmarketCatalog:
    """Downloads, caches and indexes the Cardmarket Product Catalog and Price Guide.

    The catalog covers all Pokémon singles (category 6).  It is refreshed at
    most once every ``refresh_hours`` (default: 24) to avoid hammering the S3
    endpoint; the data is also persisted to disk so a bot restart does not need
    an immediate re-download.
    """

    def __init__(
        self,
        cache_dir: Path | None = None,
        refresh_hours: int = 24,
    ) -> None:
        self._cache_dir = cache_dir or Path("data/catalog_cache")
        self._refresh_hours = refresh_hours

        # Raw data
        self._products: list[dict[str, Any]] = []
        self._price_guide_raw: list[dict[str, Any]] = []

        # Indexes built from raw data
        self._price_guide: dict[int, dict[str, Any]] = {}   # idProduct → price entry
        self._products_by_id: dict[int, dict[str, Any]] = {}  # idProduct → product

        # Search index: normalised_number → list of products
        self._products_by_number: dict[str, list[dict[str, Any]]] = {}

        # Loaded-at timestamp (Unix seconds); 0 means not yet loaded.
        self._loaded_at: float = 0.0

    # ------------------------------------------------------------------
    # Public lifecycle methods
    # ------------------------------------------------------------------

    @property
    def is_loaded(self) -> bool:
        """True when at least one product has been loaded into memory."""
        return bool(self._products)

    @property
    def product_count(self) -> int:
        """Number of products currently in memory."""
        return len(self._products)

    @property
    def expansion_count(self) -> int:
        """Number of unique expansion IDs present in the loaded catalog."""
        return len({
            p.get("idExpansion")
            for p in self._products
            if p.get("idExpansion") is not None
        })

    async def load(self, *, force: bool = False) -> None:
        """Load catalog from disk cache or download from S3.

        If *force* is True the on-disk cache is ignored and fresh data is
        downloaded from S3.  Otherwise the cache is used when it was written
        within the last ``refresh_hours`` hours.
        """
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        products_path = self._cache_dir / "products.json"
        prices_path = self._cache_dir / "prices.json"

        use_cache = (
            not force
            and products_path.exists()
            and prices_path.exists()
            and self._cache_is_fresh(products_path)
        )

        if use_cache:
            logger.info("CardmarketCatalog: loading from disk cache (%s)", self._cache_dir)
            try:
                with products_path.open("r", encoding="utf-8") as fh:
                    raw_products = json.load(fh)
                with prices_path.open("r", encoding="utf-8") as fh:
                    raw_prices = json.load(fh)
                self._ingest(raw_products, raw_prices)
                logger.info(
                    "CardmarketCatalog: loaded %d products from cache", self.product_count
                )
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "CardmarketCatalog: cache load failed (%s) – downloading fresh copy", exc
                )

        logger.info("CardmarketCatalog: downloading product catalog from S3…")
        try:
            raw_products = await self._download_json(_PRODUCT_CATALOG_URL)
            raw_prices = await self._download_json(_PRICE_GUIDE_URL)
        except Exception as exc:  # noqa: BLE001
            logger.error("CardmarketCatalog: download failed: %s", exc)
            # If we have stale cache data, fall back to it rather than leaving the
            # service empty.
            if products_path.exists() and prices_path.exists():
                logger.warning("CardmarketCatalog: using stale disk cache as fallback")
                try:
                    with products_path.open("r", encoding="utf-8") as fh:
                        raw_products = json.load(fh)
                    with prices_path.open("r", encoding="utf-8") as fh:
                        raw_prices = json.load(fh)
                    self._ingest(raw_products, raw_prices)
                    logger.info(
                        "CardmarketCatalog: loaded %d products from stale cache",
                        self.product_count,
                    )
                except Exception as exc2:  # noqa: BLE001
                    logger.error("CardmarketCatalog: stale cache load also failed: %s", exc2)
            return

        # Persist to disk for next startup.
        try:
            with products_path.open("w", encoding="utf-8") as fh:
                json.dump(raw_products, fh)
            with prices_path.open("w", encoding="utf-8") as fh:
                json.dump(raw_prices, fh)
            logger.debug("CardmarketCatalog: cache written to %s", self._cache_dir)
        except Exception as exc:  # noqa: BLE001
            logger.warning("CardmarketCatalog: could not write cache: %s", exc)

        self._ingest(raw_products, raw_prices)
        logger.info(
            "CardmarketCatalog: indexed %d products from S3", self.product_count
        )

    async def refresh_if_stale(self) -> None:
        """Re-download the catalog when it is older than ``refresh_hours``."""
        if not self.is_loaded:
            await self.load()
            return

        age_hours = (time.time() - self._loaded_at) / 3600
        if age_hours >= self._refresh_hours:
            logger.info(
                "CardmarketCatalog: catalog is %.1f h old (threshold %d h) – refreshing",
                age_hours, self._refresh_hours,
            )
            await self.load(force=True)

    # ------------------------------------------------------------------
    # Product search
    # ------------------------------------------------------------------

    def find_product(
        self,
        card_name: str,
        collector_number: str | None,
        *,
        set_name: str | None = None,
        set_code: str | None = None,
    ) -> dict[str, Any] | None:
        """Find the best matching product in the catalog.

        Uses a multi-step strategy:

        1. Narrow candidates by normalised collector number.
        2. If set information is available, further filter by expansion name.
        3. Among remaining candidates, pick the best card-name match.

        Returns the matching product dict (keys include ``idProduct``,
        ``name``, ``number``, ``expansionName``) or ``None`` when no
        suitable match is found.
        """
        if not self.is_loaded:
            return None

        if not collector_number and not set_name and not set_code:
            return None

        # ── Step 1: number-based candidate retrieval ──────────────────────
        candidates: list[dict[str, Any]] = []
        if collector_number:
            norm_num = _normalise_number_for_search(collector_number)
            candidates = list(self._products_by_number.get(norm_num, []))

        if not candidates:
            # No candidates by number alone – can't proceed without more info.
            return None

        # ── Step 2: filter by expansion ───────────────────────────────────
        if set_name or set_code:
            filtered = self._filter_by_expansion(candidates, set_name=set_name, set_code=set_code)
            if filtered:
                candidates = filtered
            # If the filter removes everything, keep all number-matched candidates
            # and rely on name matching below.

        # ── Step 3: single candidate → return immediately ─────────────────
        if len(candidates) == 1:
            return candidates[0]

        # ── Step 4: name matching ─────────────────────────────────────────
        if card_name:
            best = self._best_name_match(candidates, card_name)
            if best:
                return best

        # If still ambiguous, return the first candidate.
        return candidates[0] if candidates else None

    # ------------------------------------------------------------------
    # Price data retrieval
    # ------------------------------------------------------------------

    def get_price_data(self, product: dict[str, Any]) -> CatalogPriceData | None:
        """Return a :class:`CatalogPriceData` for *product*.

        Returns ``None`` when no price guide entry is found for the product or
        when the ``LOW`` (from) price is missing or zero.
        """
        pid = product.get("idProduct")
        if pid is None:
            return None
        pid = int(pid)

        price_entry = self._price_guide.get(pid)
        if not price_entry:
            return None

        from_price = _to_float(price_entry.get("LOW"))
        if not from_price:
            return None

        # Build the product URL from catalog metadata.
        product_url = self._build_product_url(product)

        raw_expansion_id = product.get("idExpansion")
        id_expansion: int | None = int(raw_expansion_id) if raw_expansion_id is not None else None

        return CatalogPriceData(
            product_id=pid,
            product_name=product.get("name"),
            from_price=from_price,
            price_trend=_to_float(price_entry.get("TREND")),
            avg_30_days=_to_float(price_entry.get("AVG30")),
            avg_7_days=_to_float(price_entry.get("AVG7")),
            avg_1_day=_to_float(price_entry.get("AVG1")),
            product_url=product_url,
            set_name=product.get("expansionName"),
            card_number=product.get("number"),
            id_expansion=id_expansion,
        )

    def get_product_by_id(self, product_id: int) -> dict[str, Any] | None:
        """Return the catalog product dict for *product_id*, or ``None``."""
        return self._products_by_id.get(product_id)

    def get_price_data_by_id(self, product_id: int) -> CatalogPriceData | None:
        """Return :class:`CatalogPriceData` for *product_id*, or ``None``."""
        product = self.get_product_by_id(product_id)
        if product is None:
            return None
        return self.get_price_data(product)

    def find_and_get_price_data(
        self,
        card_name: str,
        collector_number: str | None,
        *,
        set_name: str | None = None,
        set_code: str | None = None,
    ) -> CatalogPriceData | None:
        """Convenience wrapper: find product then retrieve its price data.

        Returns ``None`` when no matching product is found or when the product
        has no price guide entry.
        """
        product = self.find_product(
            card_name,
            collector_number,
            set_name=set_name,
            set_code=set_code,
        )
        if product is None:
            return None
        return self.get_price_data(product)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ingest(
        self,
        raw_products: Any,
        raw_prices: Any,
    ) -> None:
        """Parse the raw JSON blobs and build all in-memory indexes."""
        # The product catalog can be a plain list or a dict with a list value.
        if isinstance(raw_products, dict):
            # Common wrapper: {"productList": [...]}
            products_list: list[dict] = (
                raw_products.get("productList")
                or raw_products.get("products")
                or next((v for v in raw_products.values() if isinstance(v, list)), [])
            )
        elif isinstance(raw_products, list):
            products_list = raw_products
        else:
            logger.error(
                "CardmarketCatalog: unexpected product catalog type: %s", type(raw_products)
            )
            return

        # Same normalisation for the price guide.
        if isinstance(raw_prices, dict):
            prices_list: list[dict] = (
                raw_prices.get("priceGuide")
                or raw_prices.get("prices")
                or next((v for v in raw_prices.values() if isinstance(v, list)), [])
            )
        elif isinstance(raw_prices, list):
            prices_list = raw_prices
        else:
            logger.error(
                "CardmarketCatalog: unexpected price guide type: %s", type(raw_prices)
            )
            return

        self._products = products_list
        self._price_guide_raw = prices_list

        # Build price guide index: idProduct → entry.
        # Normalise price-field keys to uppercase so that both the historical
        # S3 format (e.g. "LOW", "TREND") and the current lowercase format
        # (e.g. "low", "trend") are handled transparently.
        _PRICE_UPPER_FIELDS = {"LOW", "SELL", "TREND", "AVG1", "AVG7", "AVG30"}
        self._price_guide = {}
        for entry in prices_list:
            pid = entry.get("idProduct")
            if pid is not None:
                normalised: dict[str, Any] = {}
                for k, v in entry.items():
                    normalised[k.upper() if k.upper() in _PRICE_UPPER_FIELDS else k] = v
                self._price_guide[int(pid)] = normalised

        # Build product indexes
        self._products_by_id = {}
        self._products_by_number = {}
        for product in products_list:
            pid = product.get("idProduct")
            if pid is not None:
                self._products_by_id[int(pid)] = product

            number = product.get("number", "")
            if number:
                norm_num = _normalise_number_for_search(str(number))
                if norm_num:
                    self._products_by_number.setdefault(norm_num, []).append(product)

        self._loaded_at = time.time()

    def _cache_is_fresh(self, path: Path) -> bool:
        """Return True when *path* was modified within ``refresh_hours``."""
        try:
            age_secs = time.time() - path.stat().st_mtime
            return age_secs < self._refresh_hours * 3600
        except OSError:
            return False

    def _filter_by_expansion(
        self,
        candidates: list[dict[str, Any]],
        *,
        set_name: str | None,
        set_code: str | None,
    ) -> list[dict[str, Any]]:
        """Return the subset of *candidates* whose expansion matches *set_name* or *set_code*.

        Matching is done by normalising both sides to lower-case without
        punctuation so that e.g. "Obsidian Flames" matches "obsidian flames"
        and "Obsidian-Flames".

        If *set_code* is provided its slug form (as used in Cardmarket URLs) is
        also compared against the expansion name, which lets callers pass set
        codes like ``"OBF"`` and still find products when the catalog only
        carries expansion names like ``"Obsidian Flames"``.
        """
        # Build a set of normalised search targets.
        search_terms: set[str] = set()
        if set_name:
            search_terms.add(_normalise_expansion_name(set_name))
        if set_code:
            # Convert set_code slug back to a plain name:
            # "OBF" → from _SET_CODE_TO_SLUG → "Obsidian-Flames" → "Obsidian Flames"
            slug = _set_code_to_expansion_name(set_code)
            if slug:
                search_terms.add(_normalise_expansion_name(slug))
            # Also try the raw set code itself (e.g. "OBF") in case the catalog
            # uses it directly.
            search_terms.add(set_code.lower())

        if not search_terms:
            return []

        matched = []
        for product in candidates:
            exp_name = str(product.get("expansionName") or "")
            norm_exp = _normalise_expansion_name(exp_name)
            if any(norm_exp == term or term in norm_exp or norm_exp in term for term in search_terms):
                matched.append(product)
        return matched

    def _best_name_match(
        self,
        candidates: list[dict[str, Any]],
        card_name: str,
    ) -> dict[str, Any] | None:
        """Return the candidate whose ``name`` best matches *card_name*.

        Uses token-overlap scoring for robustness: e.g. "Charizard ex" matches
        "Charizard-ex" even though the punctuation differs.
        """
        norm_target = _normalise_name_for_search(card_name)
        target_tokens = set(norm_target.split())

        best_score = -1.0
        best: dict[str, Any] | None = None

        for product in candidates:
            product_name = str(product.get("name") or "")
            norm_name = _normalise_name_for_search(product_name)

            # Exact match → immediately return.
            if norm_name == norm_target:
                return product

            # Token overlap / Jaccard similarity.
            name_tokens = set(norm_name.split())
            if not name_tokens or not target_tokens:
                continue
            intersection = target_tokens & name_tokens
            union = target_tokens | name_tokens
            score = len(intersection) / len(union)

            if score > best_score:
                best_score = score
                best = product

        # Return only when there is a reasonable match (at least one token in common).
        return best if best_score > 0 else None

    @staticmethod
    def _build_product_url(product: dict[str, Any]) -> str | None:
        """Construct the Cardmarket product page URL from catalog metadata."""
        expansion_name = product.get("expansionName")
        card_name = product.get("name")
        number = product.get("number", "")

        if not expansion_name or not card_name:
            return None

        set_slug = _expansion_name_to_slug(str(expansion_name))
        card_slug = _card_name_to_slug(str(card_name))
        bare_num = _bare_number(str(number))

        # Strip leading zeros from purely numeric bare numbers.
        if bare_num and bare_num.isdigit():
            # Keep leading zeros only when the number is 3 digits or fewer;
            # Cardmarket typically uses 3-digit collector numbers without
            # leading zeros in slugs.  When in doubt, leave as-is so the URL
            # is at worst slightly wrong but still points to the right set page.
            pass  # keep original format from catalog

        product_slug = f"{card_slug}-{bare_num}" if bare_num else card_slug
        return (
            f"https://www.cardmarket.com/en/Pokemon/Products/Singles"
            f"/{set_slug}/{product_slug}"
        )

    async def _download_json(self, url: str) -> Any:
        """Download a JSON file from *url* and return the parsed object."""
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (compatible; DealMonitor/1.0; "
                "+https://github.com/BrammekeTV/deal-monitor)"
            ),
            "Accept": "application/json, */*",
            "Accept-Encoding": "gzip, deflate",
        }
        timeout = aiohttp.ClientTimeout(total=_DOWNLOAD_TIMEOUT_SECS)
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(url) as resp:
                resp.raise_for_status()
                return await resp.json(content_type=None)


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _to_float(value: Any) -> float | None:
    """Safely convert *value* to a positive float, or return ``None``."""
    if value is None:
        return None
    try:
        f = float(value)
        return f if f > 0 else None
    except (ValueError, TypeError):
        return None


def _set_code_to_expansion_name(set_code: str) -> str | None:
    """Look up the Cardmarket expansion name for a given set code.

    Converts the URL slug form from ``_SET_CODE_TO_SLUG`` (e.g.
    ``"Obsidian-Flames"``) to a plain name (``"Obsidian Flames"``).
    """
    # Import here to avoid a circular import (scraper → services).
    try:
        from scraper.cardmarket import _SET_CODE_TO_SLUG  # noqa: PLC0415
    except ImportError:
        return None
    slug = _SET_CODE_TO_SLUG.get(set_code.upper() if set_code else "")
    if not slug:
        return None
    # "Obsidian-Flames" → "Obsidian Flames"
    return slug.replace("-", " ")
