from __future__ import annotations

import os

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

from services.card_identifier import CardFingerprint
from services.cardmarket_resolver import CardmarketResolver


class _DummyDb:
    pass


def _base_fp(*, condition_code: int | None = None) -> CardFingerprint:
    return CardFingerprint(
        card_name="Charizard ex",
        set_code="OBF",
        collector_number="006/197",
        condition_code=condition_code,
    )


def test_construct_url_defaults_to_near_mint_when_condition_missing() -> None:
    resolver = CardmarketResolver(_DummyDb())  # type: ignore[arg-type]

    resolved = resolver._construct_url(_base_fp(condition_code=None))

    assert resolved is not None
    assert "minCondition=2" in resolved.url


def test_construct_url_uses_detected_condition_when_present() -> None:
    resolver = CardmarketResolver(_DummyDb())  # type: ignore[arg-type]

    resolved = resolver._construct_url(_base_fp(condition_code=4))

    assert resolved is not None
    assert "minCondition=4" in resolved.url


def test_db_lookup_defaults_to_near_mint_when_condition_missing() -> None:
    resolver = CardmarketResolver(_DummyDb())  # type: ignore[arg-type]
    fp = _base_fp(condition_code=None)
    resolver._mappings = [
        {
            "id": 1,
            "vinted_title": "charizard ex 006/197 obsidian flames",
            "fingerprint": fp.fingerprint_hash(),
            "card_name": "Charizard ex",
            "collector_number": "006/197",
            "set_code": "OBF",
            "tokens": "[]",
            "cardmarket_url": (
                "https://www.cardmarket.com/en/Pokemon/Products/Singles/"
                "Obsidian-Flames/Charizard-ex-6"
            ),
            "cardmarket_product_name": "Charizard ex",
        }
    ]

    resolved = resolver._lookup_in_db(fp, "Charizard ex 006/197 OBF Obsidian Flames")

    assert resolved is not None
    assert "minCondition=2" in resolved.url
