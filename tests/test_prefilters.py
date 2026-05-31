from __future__ import annotations

from services.card_identifier import identify_card
from utils.card_analyzer import (
    is_graded_listing,
    is_japanese_listing,
    is_lot_listing,
    is_non_card_item,
)


def test_non_card_item_keywords_are_filtered() -> None:
    assert is_non_card_item("Pokémon Ultra Clear Soft Sleeves") is True
    assert is_non_card_item("Gameboy - Pokémon Yellow") is True


def test_lot_listing_patterns_are_filtered() -> None:
    assert is_lot_listing("Lot de 100 cartes Pokémon") is True
    assert is_lot_listing("3x Japanse full art bundle") is True
    assert is_lot_listing("4 carte fiamme spettrali") is True


def test_graded_listing_patterns_are_filtered() -> None:
    assert is_graded_listing("PSA 10 Charizard") is True
    assert is_graded_listing("Gastly Art Rare GRAAD 9.5") is True


def test_japanese_listing_patterns_are_filtered() -> None:
    assert is_japanese_listing("Venusaur 200/165 151 Jap") is True
    assert is_japanese_listing("ポケモンカード リザードン") is True


def test_multilanguage_name_translation_before_matching() -> None:
    fp = identify_card("Carte Pokémon Leveinard 113/165 MEW")
    assert fp.card_name == "Chansey"
