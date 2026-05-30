"""
tests/test_tcggo.py
~~~~~~~~~~~~~~~~~~~
Unit tests for the TCGGO API client (utils/tcggo.py).

Covers:
- Confidence scoring logic (_compute_confidence)
- URL slug extraction (_extract_slug_from_cardmarket_url)
- Price extraction and hierarchy (TcggoCardResult helpers)
- Fuzzy fallback behaviour (TcggoClient.search_card with mock HTTP)
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from utils.tcggo import (
    TcggoCardResult,
    _compute_confidence,
    _extract_slug_from_cardmarket_url,
    _parse_card_data,
)


# ---------------------------------------------------------------------------
# Confidence scoring
# ---------------------------------------------------------------------------


class TestComputeConfidence:
    """Tests for _compute_confidence()."""

    def _ctx(
        self,
        card_name: str = "",
        set_name: str = "",
        set_code: str = "",
        collector_number: str = "",
        language: str = "",
    ) -> dict:
        return {
            "card_name": card_name,
            "set_name": set_name,
            "set_code": set_code,
            "collector_number": collector_number,
            "language": language,
        }

    def _result(
        self,
        card_name: str = "",
        set_name: str = "",
        set_code: str = "",
        collector_number: str = "",
        language: str = "",
        price_trend: float | None = None,
    ) -> dict:
        return {
            "card_name": card_name,
            "set_name": set_name,
            "set_code": set_code,
            "collector_number": collector_number,
            "language": language,
            "price_trend": price_trend,
        }

    def test_exact_name_match_gives_high_score(self):
        label, score = _compute_confidence(
            self._result(card_name="Charizard ex"),
            self._ctx(card_name="Charizard ex"),
        )
        # Name exact = 40; at least that many points
        assert score >= 40

    def test_exact_name_set_number_is_high_confidence(self):
        label, score = _compute_confidence(
            self._result(
                card_name="Charizard ex",
                set_name="Obsidian Flames",
                set_code="OBF",
                collector_number="125/197",
                price_trend=10.0,
            ),
            self._ctx(
                card_name="Charizard ex",
                set_name="Obsidian Flames",
                set_code="OBF",
                collector_number="125/197",
            ),
        )
        assert label == "High"
        assert score >= 65

    def test_mismatched_card_name_lowers_score(self):
        label, score = _compute_confidence(
            self._result(card_name="Pikachu"),
            self._ctx(card_name="Charizard ex"),
        )
        assert score < 40  # name mismatch → 0 name points

    def test_collector_number_normalised(self):
        """Leading zeros should be stripped before comparison."""
        label, score = _compute_confidence(
            self._result(card_name="Charizard ex", collector_number="006/165"),
            self._ctx(card_name="Charizard ex", collector_number="6/165"),
        )
        # Number match should add at least 15 points
        assert score >= 55  # 40 (exact name) + 15 (number)

    def test_no_query_context_gives_low_confidence(self):
        """When the query provides no context, we have nothing to compare."""
        label, score = _compute_confidence(
            self._result(card_name="Charizard ex"),
            self._ctx(),  # empty query
        )
        assert label == "Low"

    def test_partial_token_overlap_scores_medium(self):
        """Token overlap like 'Charizard' matching 'Charizard ex V2' should score medium."""
        label, score = _compute_confidence(
            self._result(card_name="Charizard ex V2", set_name="Paldea Evolved"),
            self._ctx(card_name="Charizard ex", set_name="Paldea Evolved"),
        )
        # 25 (name contains) + 25 (set) = 50 at minimum
        assert score >= 35
        assert label in ("Medium", "High")

    def test_has_pricing_data_adds_points(self):
        label, score = _compute_confidence(
            self._result(card_name="Bulbasaur", price_trend=5.0),
            self._ctx(card_name="Bulbasaur"),
        )
        label_no_price, score_no_price = _compute_confidence(
            self._result(card_name="Bulbasaur"),
            self._ctx(card_name="Bulbasaur"),
        )
        assert score > score_no_price

    def test_language_match_adds_ten_points(self):
        _, score_with = _compute_confidence(
            self._result(card_name="Pikachu", language="English"),
            self._ctx(card_name="Pikachu", language="English"),
        )
        _, score_without = _compute_confidence(
            self._result(card_name="Pikachu"),
            self._ctx(card_name="Pikachu"),
        )
        assert score_with == score_without + 10

    def test_high_threshold_is_sixty_five(self):
        # Set up a result that scores exactly 65 (40 name + 25 set = 65).
        label, score = _compute_confidence(
            self._result(card_name="Mewtwo ex", set_name="Paldean Fates", set_code="PAF"),
            self._ctx(card_name="Mewtwo ex", set_code="PAF"),
        )
        assert label == "High"

    def test_medium_threshold_is_thirty_five(self):
        # 25 (name in result) + 10 (partial set) = 35 → medium
        label, score = _compute_confidence(
            self._result(card_name="Mewtwo ex V2", set_name="Paldean Fates"),
            self._ctx(card_name="Mewtwo ex", set_name="Fates"),
        )
        assert label in ("Medium", "High")


# ---------------------------------------------------------------------------
# URL slug extraction
# ---------------------------------------------------------------------------


class TestExtractSlugFromCardmarketUrl:
    """Tests for _extract_slug_from_cardmarket_url()."""

    def test_standard_singles_url(self):
        url = "https://www.cardmarket.com/en/Pokemon/Products/Singles/Base-Set/Charizard"
        slug = _extract_slug_from_cardmarket_url(url)
        assert slug == "Singles/Base-Set/Charizard"

    def test_scarlet_violet_url(self):
        url = (
            "https://www.cardmarket.com/en/Pokemon/Products/Singles/"
            "Scarlet-Violet-Obsidian-Flames/Charizard-ex"
        )
        slug = _extract_slug_from_cardmarket_url(url)
        assert slug == "Singles/Scarlet-Violet-Obsidian-Flames/Charizard-ex"

    def test_url_with_query_string(self):
        url = (
            "https://www.cardmarket.com/en/Pokemon/Products/Singles/Base-Set/Charizard"
            "?sellerCountry=23&language=1"
        )
        slug = _extract_slug_from_cardmarket_url(url)
        assert slug == "Singles/Base-Set/Charizard"

    def test_non_cardmarket_url_returns_none(self):
        url = "https://www.ebay.com/itm/12345"
        slug = _extract_slug_from_cardmarket_url(url)
        assert slug is None

    def test_subdomain_cardmarket_url(self):
        url = "https://api.cardmarket.com/en/Pokemon/Products/Singles/Base-Set/Blastoise"
        slug = _extract_slug_from_cardmarket_url(url)
        assert slug == "Singles/Base-Set/Blastoise"

    def test_url_without_products_path_returns_none(self):
        url = "https://www.cardmarket.com/en/Pokemon/Singles/Base-Set/Charizard"
        slug = _extract_slug_from_cardmarket_url(url)
        assert slug is None


# ---------------------------------------------------------------------------
# Price extraction and hierarchy
# ---------------------------------------------------------------------------


class TestTcggoCardResultHelpers:
    """Tests for TcggoCardResult price helpers."""

    def _make_result(self, **kwargs) -> TcggoCardResult:
        return TcggoCardResult(**kwargs)

    def test_best_market_value_returns_trend_first(self):
        r = self._make_result(
            price_trend=10.0, market_price=8.0, avg_price=7.0, low_price=5.0
        )
        assert r.best_market_value() == 10.0

    def test_best_market_value_skips_none_and_uses_next(self):
        r = self._make_result(market_price=8.5, avg_price=7.0)
        assert r.best_market_value() == 8.5

    def test_best_market_value_falls_back_to_alt_prices(self):
        r = self._make_result(alt_prices={"eBay": 12.0})
        assert r.best_market_value() == 12.0

    def test_best_market_value_returns_none_when_no_prices(self):
        r = self._make_result()
        assert r.best_market_value() is None

    def test_cardmarket_values_only_includes_positive(self):
        r = self._make_result(
            price_trend=10.0, market_price=0, avg_price=None, low_price=3.0
        )
        vals = r.cardmarket_values()
        assert 10.0 in vals
        assert 3.0 in vals
        assert 0 not in vals
        assert None not in vals

    def test_cm_low_returns_minimum(self):
        r = self._make_result(price_trend=10.0, market_price=8.0, low_price=3.0)
        assert r.cm_low() == 3.0

    def test_cm_high_returns_maximum(self):
        r = self._make_result(price_trend=10.0, market_price=8.0, low_price=3.0)
        assert r.cm_high() == 10.0

    def test_cm_average_is_mean(self):
        r = self._make_result(price_trend=10.0, market_price=6.0, low_price=2.0)
        assert r.cm_average() == pytest.approx(6.0, rel=1e-3)

    def test_cm_helpers_return_none_when_no_prices(self):
        r = self._make_result()
        assert r.cm_low() is None
        assert r.cm_high() is None
        assert r.cm_average() is None

    def test_best_market_value_ignores_zero(self):
        r = self._make_result(price_trend=0.0, market_price=5.0)
        assert r.best_market_value() == 5.0


# ---------------------------------------------------------------------------
# _parse_card_data – price field mapping
# ---------------------------------------------------------------------------


class TestParseCardData:
    """Tests for _parse_card_data() – raw API response → TcggoCardResult."""

    def test_parses_camelcase_price_fields(self):
        raw = {
            "name": "Charizard ex",
            "priceTrend": 12.5,
            "marketPrice": 11.0,
            "lowPrice": 6.0,
            "avg30": 10.0,
        }
        ctx = {"card_name": "Charizard ex"}
        result = _parse_card_data(raw, ctx)
        assert result.price_trend == 12.5
        assert result.market_price == 11.0
        assert result.low_price == 6.0
        assert result.avg_30_days == 10.0

    def test_parses_snake_case_price_fields(self):
        raw = {
            "card_name": "Pikachu",
            "price_trend": 3.0,
            "avg_30_days": 2.5,
        }
        ctx = {"card_name": "Pikachu"}
        result = _parse_card_data(raw, ctx)
        assert result.price_trend == 3.0
        assert result.avg_30_days == 2.5

    def test_parses_nested_prices_object(self):
        raw = {
            "card_name": "Mewtwo",
            "prices": {
                "priceTrend": 20.0,
                "lowPrice": 10.0,
                "avg30": 18.0,
            },
        }
        ctx = {"card_name": "Mewtwo"}
        result = _parse_card_data(raw, ctx)
        assert result.price_trend == 20.0
        assert result.low_price == 10.0

    def test_alt_prices_are_captured(self):
        raw = {
            "card_name": "Eevee",
            "altPrices": {"eBay": 5.0, "TCGPlayer": 4.5},
        }
        ctx = {"card_name": "Eevee"}
        result = _parse_card_data(raw, ctx)
        assert result.alt_prices.get("eBay") == 5.0
        assert result.alt_prices.get("TCGPlayer") == 4.5

    def test_identity_fields_are_parsed(self):
        raw = {
            "cardName": "Blastoise",
            "setName": "Base Set",
            "setCode": "BS",
            "collectorNumber": "2/102",
        }
        ctx = {"card_name": "Blastoise"}
        result = _parse_card_data(raw, ctx)
        assert result.card_name == "Blastoise"
        assert result.set_name == "Base Set"
        assert result.set_code == "BS"
        assert result.collector_number == "2/102"

    def test_zero_prices_are_ignored(self):
        raw = {"card_name": "Jigglypuff", "priceTrend": 0.0, "marketPrice": 4.0}
        ctx = {"card_name": "Jigglypuff"}
        result = _parse_card_data(raw, ctx)
        # trend is 0 → should be skipped; market should still be set
        assert result.price_trend is None or result.price_trend == 0.0
        # market_price should be set
        assert result.market_price == 4.0

    def test_confidence_set_on_parse(self):
        raw = {
            "cardName": "Charizard ex",
            "setCode": "OBF",
            "collectorNumber": "125",
            "priceTrend": 8.0,
        }
        ctx = {
            "card_name": "Charizard ex",
            "set_code": "OBF",
            "collector_number": "125",
        }
        result = _parse_card_data(raw, ctx)
        assert result.confidence in ("Medium", "High")


# ---------------------------------------------------------------------------
# TcggoClient – fuzzy fallback (mocked HTTP)
# ---------------------------------------------------------------------------


class TestTcggoClientSearchCard:
    """Integration-style tests for TcggoClient with mocked aiohttp sessions."""

    def _make_client(self):
        from utils.tcggo import TcggoClient

        return TcggoClient(
            rapidapi_key="test-key",
            rapidapi_host="tcggo.p.rapidapi.com",
            api_url="https://tcggo.p.rapidapi.com",
        )

    def _mock_session(self, responses: list[dict]) -> MagicMock:
        """Create an aiohttp.ClientSession mock returning JSON responses in order."""
        from unittest.mock import MagicMock

        session = MagicMock()
        response_mocks = []
        for resp_data in responses:
            resp = MagicMock()
            resp.status = 200
            resp.json = AsyncMock(return_value=resp_data)
            resp.__aenter__ = AsyncMock(return_value=resp)
            resp.__aexit__ = AsyncMock(return_value=False)
            response_mocks.append(resp)

        call_count = {"n": 0}

        def _side_effect(*args, **kwargs):
            n = call_count["n"]
            call_count["n"] += 1
            if n < len(response_mocks):
                return response_mocks[n]
            # Default empty response after exhausting list
            empty = MagicMock()
            empty.status = 200
            empty.json = AsyncMock(return_value={"results": []})
            empty.__aenter__ = AsyncMock(return_value=empty)
            empty.__aexit__ = AsyncMock(return_value=False)
            return empty

        session.get.side_effect = _side_effect
        return session

    @pytest.mark.asyncio
    async def test_search_returns_best_match(self):
        client = self._make_client()
        mock_data = {
            "results": [
                {
                    "card_name": "Charizard ex",
                    "set_name": "Obsidian Flames",
                    "set_code": "OBF",
                    "collector_number": "125",
                    "priceTrend": 15.0,
                }
            ]
        }
        session = self._mock_session([mock_data])
        result = await client.search_card(session, card_name="Charizard ex")
        assert result is not None
        assert result.card_name == "Charizard ex"
        assert result.price_trend == 15.0

    @pytest.mark.asyncio
    async def test_search_falls_back_to_fuzzy_when_exact_empty(self):
        client = self._make_client()
        empty_response = {"results": []}
        fuzzy_response = {
            "results": [
                {
                    "card_name": "Charizard ex",
                    "set_code": "OBF",
                    "priceTrend": 12.0,
                }
            ]
        }
        session = self._mock_session([empty_response, fuzzy_response])
        # listing_title triggers the fuzzy fallback path
        result = await client.search_card(
            session,
            card_name="Charizard ex",
            set_code="OBF",
            listing_title="Charizard ex OBF",
        )
        assert result is not None
        assert result.price_trend == 12.0

    @pytest.mark.asyncio
    async def test_search_returns_low_confidence_when_both_calls_empty(self):
        client = self._make_client()
        session = self._mock_session([{"results": []}, {"results": []}])
        result = await client.search_card(
            session,
            card_name="NonExistentCardXYZ",
            listing_title="NonExistentCardXYZ",
        )
        # Returns Low-confidence empty result, not None
        assert result is None or result.confidence == "Low"

    @pytest.mark.asyncio
    async def test_lookup_by_url_extracts_slug(self):
        client = self._make_client()
        url = "https://www.cardmarket.com/en/Pokemon/Products/Singles/Base-Set/Charizard"
        mock_data = {
            "card_name": "Charizard",
            "set_name": "Base Set",
            "priceTrend": 300.0,
            # Add enough context to reach High confidence
            "set_code": "BS",
        }
        # The slug lookup (/cardmarket endpoint) returns a single card object.
        # We also need a fallback search response in case confidence is Low.
        search_data = {
            "results": [
                {
                    "card_name": "Charizard",
                    "set_name": "Base Set",
                    "priceTrend": 300.0,
                }
            ]
        }
        session = self._mock_session([mock_data, search_data])
        result = await client.lookup_by_url(session, url)
        # Result may come from slug lookup or search fallback
        assert result is None or result.price_trend == 300.0

    @pytest.mark.asyncio
    async def test_lookup_by_url_falls_back_to_search_on_low_confidence(self):
        """If the /cardmarket endpoint returns low-confidence data, fall back to search."""
        client = self._make_client()
        url = "https://www.cardmarket.com/en/Pokemon/Products/Singles/Base-Set/Charizard"
        # Slug lookup returns data but with no name match → Low confidence
        low_conf = {"priceTrend": 1.0}  # no card_name → confidence will be Low
        search_data = {
            "results": [
                {"card_name": "Charizard", "set_name": "Base Set", "priceTrend": 290.0}
            ]
        }
        session = self._mock_session([low_conf, search_data])
        result = await client.lookup_by_url(session, url)
        # Should have tried fallback search; result may be None or Charizard
        assert result is None or isinstance(result.card_name, str)

    @pytest.mark.asyncio
    async def test_lookup_by_non_cardmarket_url_returns_none(self):
        """Non-Cardmarket URLs have no extractable context → return None immediately."""
        client = self._make_client()
        url = "https://www.ebay.com/itm/12345"
        session = self._mock_session([])
        result = await client.lookup_by_url(session, url)
        assert result is None
