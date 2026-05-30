"""
tests/test_card_extraction.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Unit tests for extract_card_info() and _parse_set_info() in utils/card_analyzer.py.
"""

from __future__ import annotations

import pytest

from utils.card_analyzer import _parse_set_info, extract_card_info


class TestExtractCardInfo:
    """Tests for extract_card_info()."""

    def test_full_french_title(self):
        """Full Vinted-style French title with set code and set name."""
        info = extract_card_info(
            "Carte Pokemon Raikou V SAR 218/172 - S12a VSTAR Universe"
        )
        assert info["card_name"] == "Raikou V"
        assert info["collector_number"] == "218/172"
        assert info["set_code"] == "S12a"
        assert info["set_name"] == "VSTAR Universe"

    def test_english_title_no_set_code(self):
        """English title with a set name but no explicit set code."""
        info = extract_card_info("Pokemon Charizard ex 006/197 Obsidian Flames")
        assert info["card_name"] == "Charizard ex"
        assert info["collector_number"] == "006/197"
        assert info["set_code"] is None
        assert info["set_name"] == "Obsidian Flames"

    def test_english_title_vmax(self):
        """VMAX subtype must be part of the card name, not treated as rarity."""
        info = extract_card_info("Pokemon Pikachu VMAX 044/185 Vivid Voltage")
        assert info["card_name"] == "Pikachu VMAX"
        assert info["collector_number"] == "044/185"
        assert info["set_name"] == "Vivid Voltage"

    def test_title_with_all_uppercase_set_code(self):
        """Set code that is all uppercase and 3 chars (e.g. OBF)."""
        info = extract_card_info("Pokemon Charizard ex 125/197 OBF Obsidian Flames")
        assert info["card_name"] == "Charizard ex"
        assert info["collector_number"] == "125/197"
        assert info["set_code"] == "OBF"
        assert info["set_name"] == "Obsidian Flames"

    def test_title_with_sv_set_code(self):
        """Scarlet & Violet set code format (SV1a)."""
        info = extract_card_info(
            "Karte Pokemon Mewtwo ex 232/165 SV1a Scarlet & Violet 151"
        )
        assert info["card_name"] == "Mewtwo ex"
        assert info["collector_number"] == "232/165"
        assert info["set_code"] == "SV1a"
        assert info["set_name"] == "Scarlet & Violet 151"

    def test_title_no_language_prefix(self):
        """Title without a language prefix should still parse correctly."""
        info = extract_card_info("Umbreon VMAX 215/203 Evolving Skies")
        assert info["card_name"] == "Umbreon VMAX"
        assert info["collector_number"] == "215/203"
        assert info["set_name"] == "Evolving Skies"

    def test_title_no_card_number(self):
        """When there is no collector number, card name should be extracted."""
        info = extract_card_info("Carte Pokemon Charizard")
        assert info["card_name"] == "Charizard"
        assert info["collector_number"] is None
        assert info["set_code"] is None
        assert info["set_name"] is None

    def test_title_sar_rarity_stripped(self):
        """SAR rarity suffix between name and number should be removed."""
        info = extract_card_info("Pokemon Lugia V SAR 140/159 Silver Tempest")
        assert info["card_name"] == "Lugia V"
        assert info["collector_number"] == "140/159"

    def test_title_sr_rarity_stripped(self):
        """SR rarity suffix should be stripped from the card name."""
        info = extract_card_info("Pokemon Pikachu SR 025/165 SV1a Scarlet & Violet 151")
        assert info["card_name"] == "Pikachu"
        assert info["collector_number"] == "025/165"

    def test_vstar_in_set_name_not_confused_with_set_code(self):
        """VSTAR in a set name must NOT be parsed as a set code."""
        info = extract_card_info(
            "Carte Pokemon Arceus VSTAR 123/159 - VSTAR Universe"
        )
        assert info["card_name"] == "Arceus VSTAR"
        assert info["collector_number"] == "123/159"
        # VSTAR cannot match the set-code pattern (5 uppercase chars, no digit/lowercase)
        assert info["set_code"] is None
        assert info["set_name"] == "VSTAR Universe"

    def test_dutch_prefix(self):
        """Dutch 'Pokemon kaart' prefix should be stripped."""
        info = extract_card_info("Pokemon kaart Eevee 155/159 Silver Tempest")
        assert info["card_name"] == "Eevee"
        assert info["collector_number"] == "155/159"

    def test_empty_title_returns_nones(self):
        info = extract_card_info("")
        assert all(v is None for v in info.values())

    def test_title_with_only_garbage(self):
        """Titles with no recognisable card data return None for all fields."""
        info = extract_card_info("random stuff for sale")
        assert info["collector_number"] is None


class TestParseSetInfo:
    """Tests for _parse_set_info()."""

    def test_set_code_and_name(self):
        code, name = _parse_set_info("- S12a VSTAR Universe")
        assert code == "S12a"
        assert name == "VSTAR Universe"

    def test_all_uppercase_set_code(self):
        code, name = _parse_set_info("OBF Obsidian Flames")
        assert code == "OBF"
        assert name == "Obsidian Flames"

    def test_set_name_only(self):
        code, name = _parse_set_info("Evolving Skies")
        assert code is None
        assert name == "Evolving Skies"

    def test_vstar_is_not_set_code(self):
        code, name = _parse_set_info("VSTAR Universe")
        assert code is None
        assert name == "VSTAR Universe"

    def test_empty_string(self):
        code, name = _parse_set_info("")
        assert code is None
        assert name is None

    def test_set_code_with_dash_separator(self):
        code, name = _parse_set_info("– SV1a Scarlet & Violet 151")
        assert code == "SV1a"
        assert name == "Scarlet & Violet 151"

    def test_set_code_only(self):
        code, name = _parse_set_info("OBF")
        assert code == "OBF"
        assert name is None
