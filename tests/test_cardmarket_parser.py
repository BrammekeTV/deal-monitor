"""
tests/test_cardmarket_parser.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Unit tests for the Cardmarket HTML price parser.

Run with:  python -m pytest tests/test_cardmarket_parser.py -v
"""

from __future__ import annotations

import pytest

from scraper.cardmarket import (
    _clean_price_string,
    _parse_price_to_float,
    _parse_product_page,
    _extract_card_metadata,
    normalize_cardmarket_url,
)
from bs4 import BeautifulSoup


# ---------------------------------------------------------------------------
# _clean_price_string
# ---------------------------------------------------------------------------

class TestCleanPriceString:
    def test_empty(self) -> None:
        assert _clean_price_string("") == ""

    def test_normal_euro(self) -> None:
        assert _clean_price_string("3,50 €") == "3,50 €"

    def test_mojibake_euro(self) -> None:
        assert _clean_price_string("3,50 â\x82¬") == "3,50 €"

    def test_mojibake_gbp(self) -> None:
        assert _clean_price_string("3.50 Â£") == "3.50 £"

    def test_strips_whitespace(self) -> None:
        assert _clean_price_string("  5,00 €  ") == "5,00 €"


# ---------------------------------------------------------------------------
# _parse_price_to_float
# ---------------------------------------------------------------------------

class TestParsePriceToFloat:
    def test_empty(self) -> None:
        assert _parse_price_to_float("") == 0.0

    def test_decimal_comma(self) -> None:
        assert _parse_price_to_float("13,98 €") == pytest.approx(13.98)

    def test_decimal_dot(self) -> None:
        assert _parse_price_to_float("13.98") == pytest.approx(13.98)

    def test_thousands_dot_decimal_comma(self) -> None:
        assert _parse_price_to_float("1.234,56 €") == pytest.approx(1234.56)

    def test_whole_number(self) -> None:
        assert _parse_price_to_float("10 €") == pytest.approx(10.0)

    def test_only_currency(self) -> None:
        assert _parse_price_to_float("€") == 0.0

    def test_no_digits(self) -> None:
        assert _parse_price_to_float("N/A") == 0.0

    def test_zero(self) -> None:
        assert _parse_price_to_float("0,00 €") == 0.0

    def test_large_price(self) -> None:
        assert _parse_price_to_float("2.500,00 €") == pytest.approx(2500.0)


# ---------------------------------------------------------------------------
# _parse_product_page – standard English Cardmarket page
# ---------------------------------------------------------------------------

_STANDARD_EN_HTML = """
<html><body>
<h1>Charizard ex</h1>
<ol class="breadcrumb">
  <li><a href="/en/Pokemon">Pokémon</a></li>
  <li><a href="/en/Pokemon/Products/Singles">Singles</a></li>
  <li><a href="/en/Pokemon/Products/Singles/Scarlet-Violet">Scarlet &amp; Violet</a></li>
  <li><a href="/en/Pokemon/Products/Singles/Scarlet-Violet/Charizard-ex">Charizard ex</a></li>
</ol>
<dl class="info-list-container">
  <dt>Available items</dt>
  <dd><span class="badge">42</span></dd>
  <dt>From</dt>
  <dd><span class="font-weight-bold color-primary">3,50 €</span></dd>
  <dt>Price Trend</dt>
  <dd><span class="font-weight-bold color-primary">4,20 €</span></dd>
  <dt>30-days Average Price</dt>
  <dd><span class="font-weight-bold color-primary">4,15 €</span></dd>
  <dt>7-days Average Price</dt>
  <dd><span class="font-weight-bold color-primary">4,05 €</span></dd>
  <dt>1-day Average Price</dt>
  <dd><span class="font-weight-bold color-primary">3,80 €</span></dd>
</dl>
</body></html>
"""


class TestParseProductPageEnglish:
    def setup_method(self) -> None:
        self.result = _parse_product_page(_STANDARD_EN_HTML)

    def test_has_lowest_price(self) -> None:
        assert "lowest_price" in self.result
        assert self.result["lowest_price"] == pytest.approx(3.50)

    def test_has_price_trend(self) -> None:
        assert "price_trend" in self.result
        assert self.result["price_trend"] == pytest.approx(4.20)

    def test_has_avg_30_days(self) -> None:
        assert "avg_30_days" in self.result
        assert self.result["avg_30_days"] == pytest.approx(4.15)

    def test_has_avg_7_days(self) -> None:
        assert "avg_7_days" in self.result
        assert self.result["avg_7_days"] == pytest.approx(4.05)

    def test_has_avg_1_day(self) -> None:
        assert "avg_1_day" in self.result
        assert self.result["avg_1_day"] == pytest.approx(3.80)

    def test_has_card_name(self) -> None:
        assert "card_name" in self.result
        assert "Charizard" in self.result["card_name"]

    def test_has_set_name(self) -> None:
        assert "set_name" in self.result
        assert "Scarlet" in self.result["set_name"]


# ---------------------------------------------------------------------------
# Dutch (NL) Cardmarket labels
# ---------------------------------------------------------------------------

_DUTCH_HTML = """
<html><body>
<h1>Charizard ex - 006/165</h1>
<dl class="info-list-container">
  <dt>Beschikbare items</dt>
  <dd><span class="badge">10</span></dd>
  <dt>Vanaf</dt>
  <dd><span>5,00 €</span></dd>
  <dt>Prijstrend</dt>
  <dd><span>6,00 €</span></dd>
  <dt>30 dagen gemiddelde</dt>
  <dd><span>5,80 €</span></dd>
  <dt>7 dagen gemiddelde</dt>
  <dd><span>5,50 €</span></dd>
  <dt>1 dag gemiddelde</dt>
  <dd><span>5,20 €</span></dd>
</dl>
</body></html>
"""


class TestParseProductPageDutch:
    def setup_method(self) -> None:
        self.result = _parse_product_page(_DUTCH_HTML)

    def test_vanaf_maps_to_lowest_price(self) -> None:
        assert "lowest_price" in self.result
        assert self.result["lowest_price"] == pytest.approx(5.00)

    def test_prijstrend_maps_to_price_trend(self) -> None:
        assert "price_trend" in self.result
        assert self.result["price_trend"] == pytest.approx(6.00)

    def test_30_dagen_maps_to_avg_30(self) -> None:
        assert "avg_30_days" in self.result
        assert self.result["avg_30_days"] == pytest.approx(5.80)

    def test_7_dagen_maps_to_avg_7(self) -> None:
        assert "avg_7_days" in self.result
        assert self.result["avg_7_days"] == pytest.approx(5.50)

    def test_1_dag_maps_to_avg_1(self) -> None:
        assert "avg_1_day" in self.result
        assert self.result["avg_1_day"] == pytest.approx(5.20)

    def test_card_number_extracted(self) -> None:
        # "006/165" is in the h1 title.
        assert "card_number" in self.result
        assert self.result["card_number"] == "006/165"


# ---------------------------------------------------------------------------
# German labels
# ---------------------------------------------------------------------------

_GERMAN_HTML = """
<html><body>
<dl class="info-list-container">
  <dt>Ab</dt>
  <dd><span>2,00 €</span></dd>
  <dt>Preistrend</dt>
  <dd><span>3,00 €</span></dd>
  <dt>Ø 30 Tage</dt>
  <dd><span>2,90 €</span></dd>
  <dt>Ø 7 Tage</dt>
  <dd><span>2,80 €</span></dd>
  <dt>Ø 1 Tag</dt>
  <dd><span>2,70 €</span></dd>
</dl>
</body></html>
"""


class TestParseProductPageGerman:
    def setup_method(self) -> None:
        self.result = _parse_product_page(_GERMAN_HTML)

    def test_ab_maps_to_lowest_price(self) -> None:
        assert "lowest_price" in self.result
        assert self.result["lowest_price"] == pytest.approx(2.00)

    def test_preistrend_maps_to_price_trend(self) -> None:
        # "trend" is a substring of "Preistrend"
        assert "price_trend" in self.result
        assert self.result["price_trend"] == pytest.approx(3.00)

    def test_30_tage_maps_to_avg_30(self) -> None:
        assert "avg_30_days" in self.result
        assert self.result["avg_30_days"] == pytest.approx(2.90)


# ---------------------------------------------------------------------------
# Nested span price extraction
# ---------------------------------------------------------------------------

_NESTED_SPAN_HTML = """
<html><body>
<dl class="info-list-container">
  <dt>From</dt>
  <dd>
    <span class="wrapper">
      <i class="icon"></i>
      <span class="price-value">8,99 €</span>
    </span>
  </dd>
  <dt>Price Trend</dt>
  <dd>
    <span class="wrapper">
      <span class="price-value">10,50 €</span>
    </span>
  </dd>
</dl>
</body></html>
"""


class TestNestedSpanExtraction:
    def setup_method(self) -> None:
        self.result = _parse_product_page(_NESTED_SPAN_HTML)

    def test_lowest_price_from_nested_span(self) -> None:
        assert "lowest_price" in self.result
        assert self.result["lowest_price"] == pytest.approx(8.99)

    def test_price_trend_from_nested_span(self) -> None:
        assert "price_trend" in self.result
        assert self.result["price_trend"] == pytest.approx(10.50)


# ---------------------------------------------------------------------------
# dl inside a wrapper div (alternative structure)
# ---------------------------------------------------------------------------

_WRAPPED_DL_HTML = """
<html><body>
<div class="info-list-container">
  <dl>
    <dt>From</dt>
    <dd><span>1,00 €</span></dd>
    <dt>Price Trend</dt>
    <dd><span>2,00 €</span></dd>
  </dl>
</div>
</body></html>
"""


class TestWrappedDl:
    def setup_method(self) -> None:
        self.result = _parse_product_page(_WRAPPED_DL_HTML)

    def test_lowest_price(self) -> None:
        assert "lowest_price" in self.result
        assert self.result["lowest_price"] == pytest.approx(1.00)

    def test_price_trend(self) -> None:
        assert "price_trend" in self.result
        assert self.result["price_trend"] == pytest.approx(2.00)


# ---------------------------------------------------------------------------
# JSON fallback when info-list-container is absent
# ---------------------------------------------------------------------------

_JSON_FALLBACK_HTML = """
<html><body>
<script>
var productData = {"lowPrice": 7.5, "trendPrice": 9.0, "avg30Days": 8.5};
</script>
</body></html>
"""


class TestJsonFallback:
    def setup_method(self) -> None:
        self.result = _parse_product_page(_JSON_FALLBACK_HTML)

    def test_lowest_price_from_json(self) -> None:
        assert "lowest_price" in self.result
        assert self.result["lowest_price"] == pytest.approx(7.5)

    def test_price_trend_from_json(self) -> None:
        assert "price_trend" in self.result
        assert self.result["price_trend"] == pytest.approx(9.0)

    def test_avg_30_from_json(self) -> None:
        assert "avg_30_days" in self.result
        assert self.result["avg_30_days"] == pytest.approx(8.5)


# ---------------------------------------------------------------------------
# Empty / error pages return {}
# ---------------------------------------------------------------------------

class TestEmptyPage:
    def test_empty_html(self) -> None:
        assert _parse_product_page("") == {}

    def test_no_price_container(self) -> None:
        html = "<html><body><h1>Not Found</h1></body></html>"
        assert _parse_product_page(html) == {}

    def test_container_without_prices(self) -> None:
        html = """
        <html><body>
        <dl class="info-list-container">
          <dt>Available items</dt>
          <dd><span>5</span></dd>
        </dl>
        </body></html>
        """
        # "Available items" doesn't match any price label, so no price keys.
        result = _parse_product_page(html)
        price_keys = {"lowest_price", "price_trend", "avg_30_days", "avg_7_days", "avg_1_day"}
        assert not price_keys.intersection(result.keys())


# ---------------------------------------------------------------------------
# Mojibake price values
# ---------------------------------------------------------------------------

_MOJIBAKE_HTML = """
<html><body>
<dl class="info-list-container">
  <dt>From</dt>
  <dd><span>4,50 â\x82¬</span></dd>
  <dt>Price Trend</dt>
  <dd><span>5,00 â\x82¬</span></dd>
</dl>
</body></html>
"""


class TestMojibakePrices:
    def setup_method(self) -> None:
        self.result = _parse_product_page(_MOJIBAKE_HTML)

    def test_lowest_price_decoded(self) -> None:
        assert "lowest_price" in self.result
        assert self.result["lowest_price"] == pytest.approx(4.50)

    def test_price_trend_decoded(self) -> None:
        assert "price_trend" in self.result
        assert self.result["price_trend"] == pytest.approx(5.00)


# ---------------------------------------------------------------------------
# 1-day label does not accidentally match 30-day or 7-day labels
# ---------------------------------------------------------------------------

class TestOneDayLabelIsolation:
    def test_all_labels_present_no_overlap(self) -> None:
        html = """
        <html><body>
        <dl class="info-list-container">
          <dt>From</dt>
          <dd><span>1,00 €</span></dd>
          <dt>Price Trend</dt>
          <dd><span>2,00 €</span></dd>
          <dt>30-days Average Price</dt>
          <dd><span>3,00 €</span></dd>
          <dt>7-days Average Price</dt>
          <dd><span>4,00 €</span></dd>
          <dt>1-day Average Price</dt>
          <dd><span>5,00 €</span></dd>
        </dl>
        </body></html>
        """
        result = _parse_product_page(html)
        assert result.get("lowest_price") == pytest.approx(1.00)
        assert result.get("price_trend") == pytest.approx(2.00)
        assert result.get("avg_30_days") == pytest.approx(3.00)
        assert result.get("avg_7_days") == pytest.approx(4.00)
        assert result.get("avg_1_day") == pytest.approx(5.00)


# ---------------------------------------------------------------------------
# normalize_cardmarket_url
# ---------------------------------------------------------------------------

_CM_PRODUCT = (
    "https://www.cardmarket.com/en/Pokemon/Products/Singles"
    "/Destined-Rivals/Team-Rockets-Mewtwo-ex-V1-DRI081"
)
_CM_PRODUCT_NORMALIZED = _CM_PRODUCT + "?sellerCountry=23&language=1"


class TestNormalizeCardmarketUrl:
    def test_bare_url_gets_both_params(self) -> None:
        result = normalize_cardmarket_url(_CM_PRODUCT)
        assert "sellerCountry=23" in result
        assert "language=1" in result

    def test_already_has_both_params_unchanged(self) -> None:
        url = _CM_PRODUCT + "?sellerCountry=23&language=1"
        assert normalize_cardmarket_url(url) == url

    def test_already_has_seller_country_adds_only_language(self) -> None:
        url = _CM_PRODUCT + "?sellerCountry=5"
        result = normalize_cardmarket_url(url)
        assert "sellerCountry=5" in result
        assert "language=1" in result
        assert "sellerCountry=23" not in result  # existing value kept

    def test_non_cardmarket_url_unchanged(self) -> None:
        url = "https://www.ebay.co.uk/itm/12345"
        assert normalize_cardmarket_url(url) == url

    def test_empty_string_unchanged(self) -> None:
        assert normalize_cardmarket_url("") == ""

    def test_existing_params_preserved(self) -> None:
        url = _CM_PRODUCT + "?sortBy=price_asc"
        result = normalize_cardmarket_url(url)
        assert "sortBy=price_asc" in result
        assert "sellerCountry=23" in result
        assert "language=1" in result


# ---------------------------------------------------------------------------
# build_cardmarket_url
# ---------------------------------------------------------------------------

from scraper.cardmarket import build_cardmarket_url  # noqa: E402


class TestBuildCardmarketUrl:
    def test_promo_svp_pikachu(self) -> None:
        """Pikachu (SVP 214) → SV-Black-Star-Promos/Pikachu-SVP214"""
        url = build_cardmarket_url("Pikachu", "SVP", "214", promo=True)
        assert url is not None
        assert "SV-Black-Star-Promos" in url
        assert "Pikachu-SVP214" in url
        assert "sellerCountry=23" in url
        assert "language=1" in url

    def test_promo_auto_detected_no_slash(self) -> None:
        """When collector_number has no '/', the format auto-detects as promo."""
        url = build_cardmarket_url("Pikachu", "SVP", "214", promo=False)
        assert url is not None
        # No slash in number → auto-detected as promo: set_code+number suffix
        assert "Pikachu-SVP214" in url

    def test_standard_card_with_slash_number(self) -> None:
        """Standard card: Charizard ex OBF 125/197 → Obsidian-Flames/Charizard-ex-125"""
        url = build_cardmarket_url("Charizard ex", "OBF", "125/197")
        assert url is not None
        assert "Obsidian-Flames" in url
        assert "Charizard-ex-125" in url
        assert "sellerCountry=23" in url

    def test_unknown_set_code_returns_none(self) -> None:
        url = build_cardmarket_url("Pikachu", "UNKNOWN999", "001")
        assert url is None

    def test_swsh_promo(self) -> None:
        url = build_cardmarket_url("Pikachu", "SWSHP", "088", promo=True)
        assert url is not None
        assert "SWSH-Black-Star-Promos" in url
        assert "Pikachu-SWSHP088" in url

    def test_card_name_spaces_replaced(self) -> None:
        url = build_cardmarket_url("Raikou V", "SVP", "001", promo=True)
        assert url is not None
        assert "Raikou-V-SVP001" in url

    def test_returns_full_https_url(self) -> None:
        url = build_cardmarket_url("Mewtwo ex", "SVP", "100", promo=True)
        assert url is not None
        assert url.startswith("https://www.cardmarket.com/en/Pokemon/Products/Singles/")


# ---------------------------------------------------------------------------
# contains_psa
# ---------------------------------------------------------------------------

from scraper.cardmarket import contains_psa, extract_psa_grade, _parse_psa_listing_price  # noqa: E402


class TestContainsPsa:
    def test_uppercase(self) -> None:
        assert contains_psa("PSA 10 Charizard") is True

    def test_lowercase(self) -> None:
        assert contains_psa("psa 9 pikachu") is True

    def test_mixed_case(self) -> None:
        assert contains_psa("Psa 10 Mewtwo") is True

    def test_not_present(self) -> None:
        assert contains_psa("Charizard ex NM") is False

    def test_empty_string(self) -> None:
        assert contains_psa("") is False

    def test_partial_word_no_match(self) -> None:
        # "EPSA" should NOT match – we require word boundary.
        assert contains_psa("EPSA card") is False

    def test_psa_in_description(self) -> None:
        assert contains_psa("Nice card PSA graded") is True


# ---------------------------------------------------------------------------
# extract_psa_grade
# ---------------------------------------------------------------------------

class TestExtractPsaGrade:
    def test_psa_10(self) -> None:
        assert extract_psa_grade("PSA 10 Charizard") == 10

    def test_psa_9(self) -> None:
        assert extract_psa_grade("PSA 9 Pikachu") == 9

    def test_psa_no_space(self) -> None:
        assert extract_psa_grade("PSA10") == 10

    def test_lowercase_psa(self) -> None:
        assert extract_psa_grade("psa 9 card") == 9

    def test_mixed_case(self) -> None:
        assert extract_psa_grade("Psa 10") == 10

    def test_no_grade(self) -> None:
        assert extract_psa_grade("PSA graded card") is None

    def test_no_psa(self) -> None:
        assert extract_psa_grade("Charizard NM") is None

    def test_grade_8(self) -> None:
        assert extract_psa_grade("PSA 8 Charizard") == 8

    def test_first_grade_returned(self) -> None:
        # Only the first occurrence is returned.
        assert extract_psa_grade("PSA 9 or PSA 10") == 9

    def test_grade_in_longer_sentence(self) -> None:
        assert extract_psa_grade("This is a PSA 10 graded Pikachu card") == 10


# ---------------------------------------------------------------------------
# _parse_psa_listing_price
# ---------------------------------------------------------------------------

# Typical Cardmarket article row with MT badge and PSA 10 in comment.
_PSA_10_MT_HTML = """
<html><body>
<div class="article-row">
  <div class="col-sellerProductInfo">
    <div class="product-attributes">
      <a class="badge badge-slot-condition badge-article-condition-mt" title="Mint">MT</a>
    </div>
    <p class="product-comments">PSA 10</p>
  </div>
  <div class="col-offer-price">
    <div class="price-container">
      <span class="font-weight-bold color-primary">85,00 €</span>
    </div>
  </div>
</div>
</body></html>
"""

# Row with MT badge but PSA 9 – should NOT match a PSA 10 search.
_PSA_9_MT_HTML = """
<html><body>
<div class="article-row">
  <div class="product-attributes">
    <span class="badge badge-article-condition-mt">MT</span>
  </div>
  <p class="product-comments">PSA 9 Pikachu</p>
  <div class="price-container">
    <span class="font-weight-bold">45,00 €</span>
  </div>
</div>
</body></html>
"""

# Row with NM badge (not MT) and PSA 10 – should NOT match.
_PSA_10_NM_HTML = """
<html><body>
<div class="article-row">
  <div class="product-attributes">
    <a class="badge badge-article-condition-nm" title="Near Mint">NM</a>
  </div>
  <p class="product-comments">PSA 10</p>
  <div class="price-container">
    <span>30,00 €</span>
  </div>
</div>
</body></html>
"""

# Multiple rows: first is PSA 9 MT, second is PSA 10 MT.
_MULTI_ROW_HTML = """
<html><body>
<div class="article-row">
  <span class="badge badge-article-condition-mt">MT</span>
  <p class="product-comments">PSA 9</p>
  <div class="price-container"><span>45,00 €</span></div>
</div>
<div class="article-row">
  <span class="badge badge-article-condition-mt">MT</span>
  <p class="product-comments">PSA 10</p>
  <div class="price-container"><span>95,00 €</span></div>
</div>
</body></html>
"""

# Row using title="Mint" attribute on condition badge instead of class.
_MT_TITLE_ATTR_HTML = """
<html><body>
<div class="article-row">
  <span class="badge" title="Mint">MT</span>
  <p class="product-comments">PSA 10 Charizard</p>
  <div class="price-container"><span>120,00 €</span></div>
</div>
</body></html>
"""

# No article rows at all – should return None gracefully.
_NO_ROWS_HTML = """
<html><body>
<dl class="info-list-container">
  <dt>From</dt><dd><span>10,00 €</span></dd>
</dl>
</body></html>
"""


class TestParsePsaListingPrice:
    def test_mt_psa10_returns_price(self) -> None:
        price = _parse_psa_listing_price(_PSA_10_MT_HTML, 10)
        assert price == pytest.approx(85.00)

    def test_mt_psa9_returns_price(self) -> None:
        price = _parse_psa_listing_price(_PSA_9_MT_HTML, 9)
        assert price == pytest.approx(45.00)

    def test_psa_grade_mismatch_returns_none(self) -> None:
        # Searching for PSA 10 but row has PSA 9.
        price = _parse_psa_listing_price(_PSA_9_MT_HTML, 10)
        assert price is None

    def test_nm_condition_returns_none(self) -> None:
        # NM row with PSA 10 description should not match MT search.
        price = _parse_psa_listing_price(_PSA_10_NM_HTML, 10)
        assert price is None

    def test_multiple_rows_first_match_returned(self) -> None:
        # Searching for PSA 10 – should skip PSA 9 row and find PSA 10.
        price = _parse_psa_listing_price(_MULTI_ROW_HTML, 10)
        assert price == pytest.approx(95.00)

    def test_multiple_rows_psa9(self) -> None:
        # Searching for PSA 9 – should return the first row.
        price = _parse_psa_listing_price(_MULTI_ROW_HTML, 9)
        assert price == pytest.approx(45.00)

    def test_title_attribute_mint(self) -> None:
        # MT detected via title="Mint" attribute.
        price = _parse_psa_listing_price(_MT_TITLE_ATTR_HTML, 10)
        assert price == pytest.approx(120.00)

    def test_no_rows_returns_none(self) -> None:
        price = _parse_psa_listing_price(_NO_ROWS_HTML, 10)
        assert price is None

    def test_empty_html_returns_none(self) -> None:
        price = _parse_psa_listing_price("", 10)
        assert price is None


# ---------------------------------------------------------------------------
# _parse_first_listing_price
# ---------------------------------------------------------------------------

from scraper.cardmarket import _parse_first_listing_price, _is_reverse_holo_url  # noqa: E402

_RH_SINGLE_ROW_HTML = """
<html><body>
<div class="article-row">
  <div class="col-sellerProductInfo">
    <p class="product-comments">Reverse Holo NM</p>
  </div>
  <div class="col-offer-price">
    <div class="price-container">
      <span class="font-weight-bold color-primary">4,50 €</span>
    </div>
  </div>
</div>
</body></html>
"""

_RH_MULTI_ROW_HTML = """
<html><body>
<div class="article-row">
  <div class="price-container"><span>4,50 €</span></div>
</div>
<div class="article-row">
  <div class="price-container"><span>5,00 €</span></div>
</div>
<div class="article-row">
  <div class="price-container"><span>6,00 €</span></div>
</div>
</body></html>
"""

_RH_NO_ROWS_HTML = """
<html><body>
<dl class="info-list-container">
  <dt>From</dt><dd><span>3,00 €</span></dd>
</dl>
</body></html>
"""


class TestParseFirstListingPrice:
    def test_single_row_returns_price(self) -> None:
        price = _parse_first_listing_price(_RH_SINGLE_ROW_HTML)
        assert price == pytest.approx(4.50)

    def test_multi_row_returns_first(self) -> None:
        price = _parse_first_listing_price(_RH_MULTI_ROW_HTML)
        assert price == pytest.approx(4.50)

    def test_no_rows_returns_none(self) -> None:
        price = _parse_first_listing_price(_RH_NO_ROWS_HTML)
        assert price is None

    def test_empty_html_returns_none(self) -> None:
        price = _parse_first_listing_price("")
        assert price is None


# ---------------------------------------------------------------------------
# _is_reverse_holo_url
# ---------------------------------------------------------------------------

_CM_BASE_URL = (
    "https://www.cardmarket.com/en/Pokemon/Products/Singles"
    "/Scarlet-Violet/Charizard-ex-125"
)


class TestIsReverseHoloUrl:
    def test_with_reverse_holo_y(self) -> None:
        url = _CM_BASE_URL + "?sellerCountry=23&language=1&isReverseHolo=Y"
        assert _is_reverse_holo_url(url) is True

    def test_without_reverse_holo(self) -> None:
        url = _CM_BASE_URL + "?sellerCountry=23&language=1"
        assert _is_reverse_holo_url(url) is False

    def test_reverse_holo_n(self) -> None:
        url = _CM_BASE_URL + "?isReverseHolo=N"
        assert _is_reverse_holo_url(url) is False

    def test_reverse_holo_lowercase_value(self) -> None:
        url = _CM_BASE_URL + "?isReverseHolo=y"
        assert _is_reverse_holo_url(url) is True

    def test_empty_url(self) -> None:
        assert _is_reverse_holo_url("") is False
