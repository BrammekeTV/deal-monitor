from __future__ import annotations

import importlib
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def _import_vinted_module(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")
    sys.modules.pop("scraper.vinted", None)
    sys.modules.pop("config.settings", None)
    return importlib.import_module("scraper.vinted")


@pytest.mark.anyio
async def test_setup_patches_browser_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    vinted = _import_vinted_module(monkeypatch)
    client = SimpleNamespace(headers={"Existing": "value"})
    fake_scraper = SimpleNamespace(_client=client)
    create = AsyncMock(return_value=fake_scraper)

    monkeypatch.setattr(vinted.settings, "countries", ["NL"])
    monkeypatch.setattr(vinted._AsyncVintedScraper, "create", create)

    scraper = vinted.VintedScraper()
    await scraper.setup()

    create.assert_awaited_once_with("https://www.vinted.nl")
    assert scraper._scrapers["https://www.vinted.nl"] is fake_scraper
    assert client.headers["Existing"] == "value"
    assert client.headers == {"Existing": "value", **vinted._BROWSER_HEADERS}


@pytest.mark.anyio
async def test_setup_skips_header_patch_when_client_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vinted = _import_vinted_module(monkeypatch)
    fake_scraper = SimpleNamespace()
    create = AsyncMock(return_value=fake_scraper)

    monkeypatch.setattr(vinted.settings, "countries", ["NL"])
    monkeypatch.setattr(vinted._AsyncVintedScraper, "create", create)

    scraper = vinted.VintedScraper()
    await scraper.setup()

    create.assert_awaited_once_with("https://www.vinted.nl")
    assert scraper._scrapers["https://www.vinted.nl"] is fake_scraper
