"""
tests/test_cardmarket_catalog.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Unit tests for services/cardmarket_catalog.py.

All tests exercise in-memory behaviour only (no network I/O).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.cardmarket_catalog import (
    CardmarketCatalog,
    CatalogPriceData,
    _bare_number,
    _card_name_to_slug,
    _expansion_name_to_slug,
    _normalise_expansion_name,
    _normalise_name_for_search,
    _normalise_number_for_search,
)


# ---------------------------------------------------------------------------
# Helper: build minimal sample data
# ---------------------------------------------------------------------------

def _make_products(*entries: dict) -> list[dict]:
    """Return a list of product dicts with sensible defaults filled in."""
    out = []
    for i, e in enumerate(entries, start=1):
        prod = {
            "idProduct": i,
            "name": f"Card {i}",
            "number": str(i),
            "expansionName": "Test Set",
            "idExpansion": 1,
            "rarity": "Rare",
        }
        prod.update(e)
        out.append(prod)
    return out


def _make_prices(*entries: dict) -> list[dict]:
    """Return a list of price guide dicts with sensible defaults filled in."""
    out = []
    for i, e in enumerate(entries, start=1):
        price = {
            "idProduct": i,
            "LOW": 1.0,
            "SELL": 1.5,
            "TREND": 2.0,
            "AVG1": 1.8,
            "AVG7": 1.9,
            "AVG30": 2.0,
        }
        price.update(e)
        out.append(price)
    return out


# ---------------------------------------------------------------------------
# Pure-function helpers
# ---------------------------------------------------------------------------

class TestNormaliseNumberForSearch:
    def test_plain_integer(self):
        assert _normalise_number_for_search("125") == "125"

    def test_strips_leading_zeros(self):
        assert _normalise_number_for_search("025") == "25"

    def test_strips_denominator(self):
        assert _normalise_number_for_search("025/193") == "25"

    def test_strips_denominator_no_leading_zero(self):
        assert _normalise_number_for_search("125/197") == "125"

    def test_empty_string(self):
        assert _normalise_number_for_search("") == ""

    def test_promo_number_with_prefix(self):
        # "SVP 214" → digits only → "214"
        assert _normalise_number_for_search("SVP 214") == "214"


class TestExpansionNameToSlug:
    def test_two_words(self):
        assert _expansion_name_to_slug("Obsidian Flames") == "Obsidian-Flames"

    def test_accented_e(self):
        assert _expansion_name_to_slug("Pokémon GO") == "Pokemon-GO"

    def test_single_word(self):
        assert _expansion_name_to_slug("151") == "151"

    def test_ampersand(self):
        # Non-alphanumeric chars become a single hyphen.
        assert _expansion_name_to_slug("Sun & Moon") == "Sun-Moon"


class TestNormaliseNameForSearch:
    def test_lower_case(self):
        assert _normalise_name_for_search("Charizard ex") == "charizard ex"

    def test_strips_punctuation(self):
        assert _normalise_name_for_search("Pikachu!") == "pikachu"

    def test_preserves_spaces(self):
        assert _normalise_name_for_search("Raikou V SAR") == "raikou v sar"


class TestBareNumber:
    def test_fraction(self):
        assert _bare_number("125/197") == "125"

    def test_plain(self):
        assert _bare_number("025") == "025"

    def test_empty(self):
        assert _bare_number("") == ""


# ---------------------------------------------------------------------------
# CardmarketCatalog._ingest
# ---------------------------------------------------------------------------

class TestCatalogIngest:
    """Tests that _ingest() builds correct in-memory indexes."""

    def _make_catalog(self) -> CardmarketCatalog:
        return CardmarketCatalog(cache_dir=Path("/tmp/_test_catalog_cache"))

    def test_is_loaded_after_ingest(self):
        cat = self._make_catalog()
        assert not cat.is_loaded
        cat._ingest(_make_products({"idProduct": 1}), _make_prices({"idProduct": 1}))
        assert cat.is_loaded

    def test_product_count(self):
        cat = self._make_catalog()
        cat._ingest(
            _make_products({"idProduct": 1}, {"idProduct": 2}),
            _make_prices({"idProduct": 1}, {"idProduct": 2}),
        )
        assert cat.product_count == 2

    def test_number_index_built(self):
        cat = self._make_catalog()
        cat._ingest(
            _make_products({"idProduct": 1, "number": "025/193"}),
            _make_prices({"idProduct": 1}),
        )
        # Normalised "025/193" → "25"
        assert "25" in cat._products_by_number

    def test_price_guide_index_built(self):
        cat = self._make_catalog()
        cat._ingest(
            _make_products({"idProduct": 7}),
            _make_prices({"idProduct": 7, "LOW": 3.50}),
        )
        assert 7 in cat._price_guide
        assert cat._price_guide[7]["LOW"] == 3.50

    def test_ingest_accepts_wrapped_dict_format(self):
        """Some catalog downloads may wrap the list in a dict."""
        cat = self._make_catalog()
        products = {"productList": _make_products({"idProduct": 1})}
        prices = {"priceGuide": _make_prices({"idProduct": 1})}
        cat._ingest(products, prices)
        assert cat.product_count == 1

    def test_loaded_at_updated(self):
        cat = self._make_catalog()
        before = time.time()
        cat._ingest(_make_products({"idProduct": 1}), _make_prices({"idProduct": 1}))
        assert cat._loaded_at >= before


# ---------------------------------------------------------------------------
# CardmarketCatalog.find_product
# ---------------------------------------------------------------------------

class TestFindProduct:
    def _loaded_catalog(self, products, prices=None) -> CardmarketCatalog:
        cat = CardmarketCatalog(cache_dir=Path("/tmp/_test_catalog_cache"))
        cat._ingest(products, prices or _make_prices(*[{"idProduct": p["idProduct"]} for p in products]))
        return cat

    def test_find_by_number(self):
        products = _make_products(
            {"idProduct": 1, "name": "Charizard ex", "number": "125/197", "expansionName": "Obsidian Flames"},
        )
        cat = self._loaded_catalog(products)
        result = cat.find_product("Charizard ex", "125/197")
        assert result is not None
        assert result["idProduct"] == 1

    def test_find_by_number_with_leading_zero(self):
        products = _make_products(
            {"idProduct": 2, "name": "Pikachu", "number": "025/193", "expansionName": "Paldea Evolved"},
        )
        cat = self._loaded_catalog(products)
        result = cat.find_product("Pikachu", "025/193")
        assert result is not None
        assert result["idProduct"] == 2

    def test_returns_none_when_no_number_match(self):
        products = _make_products(
            {"idProduct": 1, "name": "Charizard ex", "number": "125/197"},
        )
        cat = self._loaded_catalog(products)
        result = cat.find_product("Charizard ex", "999/999")
        assert result is None

    def test_set_name_filter_disambiguates(self):
        """When two cards share a number, set_name filters to the correct one."""
        products = _make_products(
            {"idProduct": 1, "name": "Bulbasaur", "number": "001", "expansionName": "Base Set"},
            {"idProduct": 2, "name": "Bulbasaur", "number": "001", "expansionName": "Celebrations"},
        )
        prices = _make_prices({"idProduct": 1}, {"idProduct": 2})
        cat = CardmarketCatalog(cache_dir=Path("/tmp/_test_catalog_cache"))
        cat._ingest(products, prices)
        result = cat.find_product("Bulbasaur", "001", set_name="Celebrations")
        assert result is not None
        assert result["idProduct"] == 2

    def test_returns_none_without_any_identifier(self):
        products = _make_products({"idProduct": 1})
        cat = self._loaded_catalog(products)
        result = cat.find_product("", None)
        assert result is None

    def test_returns_none_when_catalog_empty(self):
        cat = CardmarketCatalog(cache_dir=Path("/tmp/_test_catalog_cache"))
        result = cat.find_product("Charizard ex", "125/197")
        assert result is None


# ---------------------------------------------------------------------------
# CardmarketCatalog.get_price_data
# ---------------------------------------------------------------------------

class TestGetPriceData:
    def _loaded_catalog(self, product: dict, price: dict) -> CardmarketCatalog:
        cat = CardmarketCatalog(cache_dir=Path("/tmp/_test_catalog_cache"))
        cat._ingest([product], [price])
        return cat

    def test_returns_catalog_price_data(self):
        product = {
            "idProduct": 1,
            "name": "Charizard ex",
            "number": "125/197",
            "expansionName": "Obsidian Flames",
        }
        price = {"idProduct": 1, "LOW": 12.50, "TREND": 15.0, "AVG1": 13.0, "AVG7": 14.0, "AVG30": 14.5}
        cat = self._loaded_catalog(product, price)
        result = cat.get_price_data(product)
        assert isinstance(result, CatalogPriceData)
        assert result.from_price == 12.50
        assert result.price_trend == 15.0
        assert result.avg_30_days == 14.5
        assert result.avg_7_days == 14.0
        assert result.avg_1_day == 13.0
        assert result.set_name == "Obsidian Flames"
        assert result.card_number == "125/197"

    def test_returns_none_when_no_price_guide_entry(self):
        product = {"idProduct": 99, "name": "Ghost Card", "number": "001"}
        cat = CardmarketCatalog(cache_dir=Path("/tmp/_test_catalog_cache"))
        cat._ingest([product], [])
        assert cat.get_price_data(product) is None

    def test_returns_none_when_low_price_is_zero(self):
        product = {"idProduct": 1, "name": "No Price", "number": "001"}
        price = {"idProduct": 1, "LOW": 0, "TREND": 1.0}
        cat = self._loaded_catalog(product, price)
        assert cat.get_price_data(product) is None

    def test_is_valid_false_when_no_from_price(self):
        pd = CatalogPriceData(product_id=1, product_name="Test")
        assert not pd.is_valid()

    def test_is_valid_true_when_from_price_positive(self):
        pd = CatalogPriceData(product_id=1, product_name="Test", from_price=5.0)
        assert pd.is_valid()

    def test_product_url_is_built(self):
        product = {
            "idProduct": 1,
            "name": "Charizard ex",
            "number": "125/197",
            "expansionName": "Obsidian Flames",
        }
        price = {"idProduct": 1, "LOW": 10.0, "TREND": 12.0}
        cat = self._loaded_catalog(product, price)
        result = cat.get_price_data(product)
        assert result is not None
        assert result.product_url is not None
        assert "cardmarket.com" in result.product_url
        assert "Obsidian-Flames" in result.product_url


# ---------------------------------------------------------------------------
# CardmarketCatalog.find_and_get_price_data (integration)
# ---------------------------------------------------------------------------

class TestFindAndGetPriceData:
    def _catalog_with(self, products, prices) -> CardmarketCatalog:
        cat = CardmarketCatalog(cache_dir=Path("/tmp/_test_catalog_cache"))
        cat._ingest(products, prices)
        return cat

    def test_returns_price_data_for_known_card(self):
        products = _make_products(
            {"idProduct": 1, "name": "Raikou V", "number": "218/172", "expansionName": "VSTAR Universe"},
        )
        prices = _make_prices({"idProduct": 1, "LOW": 5.0})
        cat = self._catalog_with(products, prices)
        result = cat.find_and_get_price_data("Raikou V", "218/172", set_name="VSTAR Universe")
        assert result is not None
        assert result.from_price == 5.0

    def test_returns_none_for_unknown_card(self):
        cat = CardmarketCatalog(cache_dir=Path("/tmp/_test_catalog_cache"))
        result = cat.find_and_get_price_data("Fake Card", "999/999")
        assert result is None

    def test_returns_none_when_price_missing(self):
        products = _make_products(
            {"idProduct": 1, "name": "Old Card", "number": "001"},
        )
        cat = self._catalog_with(products, [])  # no price guide entries
        result = cat.find_and_get_price_data("Old Card", "001")
        assert result is None


# ---------------------------------------------------------------------------
# CardmarketCatalog.load from disk cache
# ---------------------------------------------------------------------------

class TestLoadFromCache:
    def test_load_from_valid_cache(self, tmp_path):
        products = _make_products({"idProduct": 1, "name": "Test", "number": "001"})
        prices = _make_prices({"idProduct": 1, "LOW": 2.0})
        (tmp_path / "products.json").write_text(json.dumps(products))
        (tmp_path / "prices.json").write_text(json.dumps(prices))

        cat = CardmarketCatalog(cache_dir=tmp_path, refresh_hours=24)
        # Mark cache as fresh by touching files recently (they were just created).
        import asyncio
        asyncio.run(cat.load())
        assert cat.is_loaded
        assert cat.product_count == 1

    def test_load_downloads_when_cache_missing(self, tmp_path):
        """When no cache exists, _download_json is called."""
        products = _make_products({"idProduct": 1})
        prices = _make_prices({"idProduct": 1})

        cat = CardmarketCatalog(cache_dir=tmp_path, refresh_hours=24)

        async def _mock_load():
            cat._ingest(products, prices)

        import asyncio

        with patch.object(cat, "_download_json", new_callable=AsyncMock) as mock_dl:
            mock_dl.side_effect = [products, prices]
            asyncio.run(cat.load())

        assert cat.is_loaded
