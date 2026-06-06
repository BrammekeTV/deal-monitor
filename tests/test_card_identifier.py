"""
tests/test_card_identifier.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Unit tests for language abbreviation detection, condition extraction,
and the normalize_cardmarket_url minCondition parameter.
"""

from __future__ import annotations

import pytest

from services.card_identifier import identify_card


# ---------------------------------------------------------------------------
# Language abbreviation detection
# ---------------------------------------------------------------------------

class TestLanguageAbbreviations:
    """Tests for ISO 639-1 two-letter language codes in listing titles."""

    def test_it_maps_to_italian(self) -> None:
        fp = identify_card("Charizard ex 006/197 OBF Obsidian Flames IT")
        assert fp.language == "Italian"

    def test_de_maps_to_german(self) -> None:
        fp = identify_card("Pikachu 044/185 Vivid Voltage DE")
        assert fp.language == "German"

    def test_fr_maps_to_french(self) -> None:
        fp = identify_card("Carte Pokemon Mewtwo ex 232/165 SV2a 151 FR")
        assert fp.language == "French"

    def test_ja_maps_to_japanese(self) -> None:
        fp = identify_card("Pokemon Charizard 4/102 Base Set JA")
        assert fp.language == "Japanese"

    def test_ko_maps_to_korean(self) -> None:
        fp = identify_card("Umbreon VMAX 215/203 Evolving Skies KO")
        assert fp.language == "Korean"

    def test_pt_maps_to_portuguese(self) -> None:
        fp = identify_card("Pikachu 025/165 SV1a PT")
        assert fp.language == "Portuguese"

    def test_ru_maps_to_russian(self) -> None:
        fp = identify_card("Mewtwo ex 232/165 151 RU")
        assert fp.language == "Russian"

    def test_nl_maps_to_dutch(self) -> None:
        fp = identify_card("Eevee 155/159 Silver Tempest NL")
        assert fp.language == "Dutch"

    def test_en_maps_to_english(self) -> None:
        fp = identify_card("Pikachu 044/185 Vivid Voltage EN")
        assert fp.language == "English"

    def test_full_word_deutsch_maps_to_german(self) -> None:
        fp = identify_card("Pokemon Pikachu 044/185 Deutsch")
        assert fp.language == "German"

    def test_full_word_english_maps_to_english(self) -> None:
        fp = identify_card("Pokemon Umbreon VMAX 215/203 Evolving Skies English")
        assert fp.language == "English"

    def test_no_language_returns_none(self) -> None:
        fp = identify_card("Charizard ex 006/197 OBF Obsidian Flames")
        assert fp.language is None


# ---------------------------------------------------------------------------
# Condition extraction
# ---------------------------------------------------------------------------

class TestConditionExtraction:
    """Tests for card condition detection in listing titles."""

    def test_mint_full_word(self) -> None:
        fp = identify_card("Charizard ex 006/197 Mint")
        assert fp.condition == "Mint"
        assert fp.condition_code == 1

    def test_near_mint_full_phrase(self) -> None:
        fp = identify_card("Pikachu 044/185 Near Mint")
        assert fp.condition == "Near Mint"
        assert fp.condition_code == 2

    def test_nm_parenthesized(self) -> None:
        fp = identify_card("Umbreon VMAX 215/203 (NM)")
        assert fp.condition == "Near Mint"
        assert fp.condition_code == 2

    def test_excellent_full_word(self) -> None:
        fp = identify_card("Charizard 4/102 Base Set Excellent")
        assert fp.condition == "Excellent"
        assert fp.condition_code == 3

    def test_ex_parenthesized(self) -> None:
        fp = identify_card("Pikachu 025/165 (EX)")
        assert fp.condition == "Excellent"
        assert fp.condition_code == 3

    def test_good_full_word(self) -> None:
        fp = identify_card("Umbreon 155/159 Good")
        assert fp.condition == "Good"
        assert fp.condition_code == 4

    def test_gd_parenthesized(self) -> None:
        fp = identify_card("Eevee 010/159 (GD)")
        assert fp.condition == "Good"
        assert fp.condition_code == 4

    def test_light_played_full_phrase(self) -> None:
        fp = identify_card("Pikachu 044/185 Light Played")
        assert fp.condition == "Light Played"
        assert fp.condition_code == 5

    def test_lp_parenthesized(self) -> None:
        fp = identify_card("Charizard 4/102 Base Set (LP)")
        assert fp.condition == "Light Played"
        assert fp.condition_code == 5

    def test_played_full_word(self) -> None:
        fp = identify_card("Mewtwo 003/165 Played")
        assert fp.condition == "Played"
        assert fp.condition_code == 6

    def test_pl_parenthesized(self) -> None:
        fp = identify_card("Umbreon 215/203 (PL)")
        assert fp.condition == "Played"
        assert fp.condition_code == 6

    def test_poor_full_word(self) -> None:
        fp = identify_card("Charizard 4/102 Poor")
        assert fp.condition == "Poor"
        assert fp.condition_code == 7

    def test_po_parenthesized(self) -> None:
        fp = identify_card("Pikachu 025/165 (PO)")
        assert fp.condition == "Poor"
        assert fp.condition_code == 7

    def test_no_condition_returns_none(self) -> None:
        fp = identify_card("Charizard ex 006/197 OBF Obsidian Flames")
        assert fp.condition is None
        assert fp.condition_code is None


# ---------------------------------------------------------------------------
# minCondition URL parameter via normalize_cardmarket_url
# ---------------------------------------------------------------------------

class TestNormalizeCardmarketUrlCondition:
    """Tests for minCondition param added by normalize_cardmarket_url."""

    _BASE = (
        "https://www.cardmarket.com/en/Pokemon/Products/Singles"
        "/Obsidian-Flames/Charizard-ex-6"
    )

    def _norm(self, **kwargs: object) -> str:
        from scraper.cardmarket import normalize_cardmarket_url
        return normalize_cardmarket_url(self._BASE, **kwargs)  # type: ignore[arg-type]

    def test_no_condition_no_param(self) -> None:
        assert "minCondition" not in self._norm()

    def test_mint_adds_min_condition_1(self) -> None:
        assert "minCondition=1" in self._norm(min_condition=1)

    def test_near_mint_adds_min_condition_2(self) -> None:
        assert "minCondition=2" in self._norm(min_condition=2)

    def test_excellent_adds_min_condition_3(self) -> None:
        assert "minCondition=3" in self._norm(min_condition=3)

    def test_good_adds_min_condition_4(self) -> None:
        assert "minCondition=4" in self._norm(min_condition=4)

    def test_light_played_adds_min_condition_5(self) -> None:
        assert "minCondition=5" in self._norm(min_condition=5)

    def test_played_adds_min_condition_6(self) -> None:
        assert "minCondition=6" in self._norm(min_condition=6)

    def test_poor_no_min_condition_param(self) -> None:
        """Poor (code=7) should not add a minCondition param."""
        assert "minCondition" not in self._norm(min_condition=7)

    def test_none_no_min_condition_param(self) -> None:
        assert "minCondition" not in self._norm(min_condition=None)

    def test_base_params_still_present(self) -> None:
        url = self._norm(min_condition=2)
        assert "sellerCountry=23" in url
        assert "language=1" in url


class TestIssue172Regressions:
    """Regression tests for issue #172 comment examples."""

    def test_alolan_form_notation_is_parsed(self) -> None:
        fp = identify_card("Persian Gx d’alola 071/064")
        assert fp.card_name == "Alolan Persian GX"
        assert fp.collector_number == "071/064"

    def test_lowercase_de_preposition_is_not_language(self) -> None:
        fp = identify_card("Arven's Mabosstiff Dogrino ex de Pepper sv9a 081 Pokemon JP")
        assert fp.language == "Japanese"
        assert fp.set_code == "SV9A"
        assert fp.collector_number == "081"

    def test_promo_number_before_name_still_extracts_card_name(self) -> None:
        fp = identify_card("Pokemon Promo MEP 080 Fennekin")
        assert fp.card_name == "Fennekin"
        assert fp.set_code == "MEP"
        assert fp.collector_number == "080"

    def test_number_before_name_is_supported(self) -> None:
        fp = identify_card("Pokémon Pokemon Karte Card 12/112 Raichu")
        assert fp.card_name == "Raichu"
        assert fp.collector_number == "12/112"

    def test_radiante_translation_maps_to_radiant_prefix(self) -> None:
        fp = identify_card("Pokémon Charizard Radiante 020/159 Holo Español")
        assert fp.card_name == "Radiant Charizard"
        assert fp.language == "Spanish"

    def test_ambiguous_ar_token_not_used_as_set_code(self) -> None:
        fp = identify_card("Chatot sv5k 081/071 AR - Pokemon Sammelkarte")
        assert fp.card_name == "Chatot"
        assert fp.set_code != "AR"
        assert fp.collector_number == "081/071"
