"""
tests/test_price_lookup.py
~~~~~~~~~~~~~~~~~~~~~~~~~~
Unit tests for utils/price_lookup.py.

Covers:
- eBay Finding API retry behaviour on HTTP 5xx errors
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ebay_response(status: int, json_data: Any) -> MagicMock:
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _mock_session(responses: list[MagicMock]) -> MagicMock:
    session = MagicMock()
    call_count = {"n": 0}

    def _side_effect(*args, **kwargs):
        n = call_count["n"]
        call_count["n"] += 1
        if n < len(responses):
            return responses[n]
        empty = MagicMock()
        empty.status = 200
        empty.json = AsyncMock(return_value={})
        empty.__aenter__ = AsyncMock(return_value=empty)
        empty.__aexit__ = AsyncMock(return_value=False)
        return empty

    session.get.side_effect = _side_effect
    return session


# Minimal valid eBay Finding API success payload with one sold item
_EBAY_SUCCESS = {
    "findCompletedItemsResponse": [
        {
            "ack": ["Success"],
            "searchResult": [
                {
                    "item": [
                        {
                            "sellingStatus": [
                                {
                                    "sellingState": ["EndedWithSales"],
                                    "currentPrice": [
                                        {"@currencyId": "GBP", "__value__": "10.00"}
                                    ],
                                }
                            ]
                        }
                    ]
                }
            ],
        }
    ]
}


# ---------------------------------------------------------------------------
# eBay retry tests
# ---------------------------------------------------------------------------


class TestEbayLookupRetry:
    """Tests for _ebay_lookup retry behaviour on HTTP 5xx."""

    @pytest.mark.asyncio
    async def test_500_retries_and_succeeds(self):
        """A single 500 should be retried; the subsequent 200 returns a result."""
        from utils.price_lookup import _ebay_lookup

        session = _mock_session([
            _make_ebay_response(500, None),
            _make_ebay_response(200, _EBAY_SUCCESS),
        ])

        with patch("utils.price_lookup.settings") as mock_settings, \
             patch("asyncio.sleep", new_callable=AsyncMock):
            mock_settings.ebay_app_id = "test-app-id"
            mock_settings.ebay_sample_size = 10
            mock_settings.ebay_site_id = 3

            result = await _ebay_lookup(session, "Charizard")

        assert result is not None
        assert result.platform == "eBay"
        assert result.avg_price == 10.0

    @pytest.mark.asyncio
    async def test_500_exhausts_retries_returns_none(self):
        """When all attempts return 500, _ebay_lookup should return None."""
        from utils.price_lookup import _ebay_lookup

        session = _mock_session([
            _make_ebay_response(500, None),
            _make_ebay_response(500, None),
            _make_ebay_response(500, None),
            _make_ebay_response(500, None),
        ])

        with patch("utils.price_lookup.settings") as mock_settings, \
             patch("asyncio.sleep", new_callable=AsyncMock):
            mock_settings.ebay_app_id = "test-app-id"
            mock_settings.ebay_sample_size = 10
            mock_settings.ebay_site_id = 3

            result = await _ebay_lookup(session, "Charizard")

        assert result is None

    @pytest.mark.asyncio
    async def test_non_5xx_error_returns_none_immediately(self):
        """A 400 or 401 response should not be retried."""
        from utils.price_lookup import _ebay_lookup

        session = _mock_session([
            _make_ebay_response(401, None),
        ])

        with patch("utils.price_lookup.settings") as mock_settings, \
             patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            mock_settings.ebay_app_id = "test-app-id"
            mock_settings.ebay_sample_size = 10
            mock_settings.ebay_site_id = 3

            result = await _ebay_lookup(session, "Charizard")

        assert result is None
        # No sleep should have been called (no retry for 4xx)
        mock_sleep.assert_not_called()

    @pytest.mark.asyncio
    async def test_success_on_first_attempt(self):
        """A direct 200 success should not trigger any retry sleep."""
        from utils.price_lookup import _ebay_lookup

        session = _mock_session([
            _make_ebay_response(200, _EBAY_SUCCESS),
        ])

        with patch("utils.price_lookup.settings") as mock_settings, \
             patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            mock_settings.ebay_app_id = "test-app-id"
            mock_settings.ebay_sample_size = 10
            mock_settings.ebay_site_id = 3

            result = await _ebay_lookup(session, "Charizard")

        assert result is not None
        mock_sleep.assert_not_called()
