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
        """Scarlet & Violet mixed-case set code format (e.g. SV2a)."""
        info = extract_card_info(
            "Karte Pokemon Mewtwo ex 232/165 SV2a 151"
        )
        assert info["card_name"] == "Mewtwo ex"
        assert info["collector_number"] == "232/165"
        assert info["set_code"] == "SV2a"
        assert info["set_name"] == "151"

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
        assert all(v is None for k, v in info.items() if k != "card_name_matched")

    def test_title_with_only_garbage(self):
        """Titles with no recognisable card data return None for all fields."""
        info = extract_card_info("random stuff for sale")
        assert info["collector_number"] is None

    # ------------------------------------------------------------------
    # Promo-style collector numbers (e.g. SVP 214)
    # ------------------------------------------------------------------

    def test_promo_pikachu_svp_with_parens(self):
        """Pikachu (SVP 214) → card_name='Pikachu', set_code='SVP', collector_number='214'"""
        info = extract_card_info("Pikachu (SVP 214)")
        assert info["card_name"] == "Pikachu"
        assert info["set_code"] == "SVP"
        assert info["collector_number"] == "214"
        assert info["set_name"] is None

    def test_promo_pikachu_svp_no_parens(self):
        """Pikachu SVP 214 → same result as with parens."""
        info = extract_card_info("Pikachu SVP 214")
        assert info["card_name"] == "Pikachu"
        assert info["set_code"] == "SVP"
        assert info["collector_number"] == "214"

    def test_promo_pikachu_svp_no_space(self):
        """Pikachu SVP214 (no space) → card_name='Pikachu', set_code='SVP', number='214'"""
        info = extract_card_info("Pikachu SVP214")
        assert info["card_name"] == "Pikachu"
        assert info["set_code"] == "SVP"
        assert info["collector_number"] == "214"

    def test_promo_swsh_promo(self):
        """Pikachu SWSHP 088 → set_code='SWSHP'"""
        info = extract_card_info("Pikachu SWSHP 088")
        assert info["card_name"] == "Pikachu"
        assert info["set_code"] == "SWSHP"
        assert info["collector_number"] == "088"

    def test_standard_number_takes_priority_over_promo(self):
        """When a standard xxx/yyy number exists it takes priority over promo detection."""
        info = extract_card_info("Pikachu 044/185 Vivid Voltage")
        assert info["collector_number"] == "044/185"
        assert info["set_code"] is None  # no set code in this title

    def test_vmax_not_treated_as_promo_code(self):
        """VMAX must NOT be parsed as a promo set code."""
        info = extract_card_info("Pikachu VMAX 044/185 Vivid Voltage")
        assert info["card_name"] == "Pikachu VMAX"
        assert info["collector_number"] == "044/185"


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
        code, name = _parse_set_info("– SV2a 151")
        assert code == "SV2a"
        assert name == "151"

    def test_set_code_only(self):
        # When only a set code is given, _parse_set_info should return both the
        # code and its canonical display name (fixes issues #113–#116).
        code, name = _parse_set_info("OBF")
        assert code == "OBF"
        assert name == "Obsidian Flames"

    def test_set_code_only_surging_sparks(self):
        code, name = _parse_set_info("SSP")
        assert code == "SSP"
        assert name == "Surging Sparks"

    def test_set_code_only_aquapolis(self):
        code, name = _parse_set_info("AQ")
        assert code == "AQ"
        assert name == "Aquapolis"

    def test_set_code_only_ex_power_keepers(self):
        code, name = _parse_set_info("PK")
        assert code == "PK"
        assert name == "EX Power Keepers"


class TestSubsetCollectorNumber:
    """Tests for subset/sub-set collector number formats (e.g. TG09/TG30, SV49/SV94)."""

    def test_trainer_gallery_format(self):
        """Trainer Gallery numbers like TG09/TG30 (Silver Tempest)."""
        info = extract_card_info("Umbreon TG25/TG30 Silver Tempest")
        assert info["card_name"] == "Umbreon"
        assert info["collector_number"] == "TG25/TG30"
        assert info["card_name_matched"] is True

    def test_shiny_vault_format(self):
        """Shiny Vault numbers like SV49/SV94 (Hidden Fates)."""
        info = extract_card_info("Charizard SV49/SV94 Hidden Fates")
        assert info["card_name"] == "Charizard"
        assert info["collector_number"] == "SV49/SV94"
        assert info["card_name_matched"] is True

    def test_crown_zenith_gallery_format(self):
        """Crown Zenith Galarian Gallery numbers like GG12/GG70."""
        info = extract_card_info("Pikachu GG12/GG70 Crown Zenith")
        assert info["card_name"] == "Pikachu"
        assert info["collector_number"] == "GG12/GG70"
        assert info["card_name_matched"] is True

    def test_subset_number_with_set_name_after(self):
        """Set name after a subset collector number is captured."""
        info = extract_card_info("Mew TG29/TG30 Silver Tempest")
        assert info["collector_number"] == "TG29/TG30"
        assert info["set_name"] == "Silver Tempest"


class TestPokemonNameMatching:
    """Tests for Pokémon name parsing with prefixes and suffixes."""

    def test_bare_name_matched(self):
        info = extract_card_info("Pikachu 001/025 Happy Birthday")
        assert info["card_name"] == "Pikachu"
        assert info["card_name_matched"] is True

    def test_name_with_suffix(self):
        info = extract_card_info("Charizard ex 006/197 OBF Obsidian Flames")
        assert info["card_name"] == "Charizard ex"
        assert info["card_name_matched"] is True

    def test_name_with_vmax_suffix(self):
        info = extract_card_info("Umbreon VMAX 215/203 Evolving Skies")
        assert info["card_name"] == "Umbreon VMAX"
        assert info["card_name_matched"] is True

    def test_name_with_prefix(self):
        info = extract_card_info("Alolan Ninetales 012/072 Shining Fates")
        assert info["card_name"] == "Alolan Ninetales"
        assert info["card_name_matched"] is True

    def test_name_with_prefix_and_suffix(self):
        info = extract_card_info("Radiant Charizard 011/078 Pokemon GO")
        assert info["card_name"] == "Radiant Charizard"
        assert info["card_name_matched"] is True

    def test_multiword_pokemon_name(self):
        info = extract_card_info("Iron Hands ex 130/182 PAR Paradox Rift")
        assert info["card_name"] == "Iron Hands ex"
        assert info["card_name_matched"] is True

    def test_unknown_name_not_matched(self):
        """A made-up name should still be returned but marked as not matched."""
        info = extract_card_info("Fictosaur ex 001/100 OBF Obsidian Flames")
        assert info["card_name"] == "Fictosaur ex"
        assert info["card_name_matched"] is False


class TestSetCodeValidation:
    """Tests that unknown set codes are not returned as set_code."""

    def test_condition_prefix_stripped_from_set_name(self):
        """NM is a condition token; the remainder becomes the set name."""
        code, name = _parse_set_info("NM Obsidian Flames")
        assert code is None
        assert name == "Obsidian Flames"

    def test_known_code_accepted(self):
        code, name = _parse_set_info("OBF Obsidian Flames")
        assert code == "OBF"
        assert name == "Obsidian Flames"

    def test_standalone_condition_token_returns_no_set(self):
        """A standalone condition abbreviation returns no set info."""
        code, name = _parse_set_info("NM")
        assert code is None
        assert name is None


class TestNoisyTitleNoCollectorNumber:
    """Regression tests for path-4 (no collector number) card name extraction.

    Previously, the full title was returned as the card name when noise tokens
    such as years or grade certifiers appeared after the card name.
    """

    def test_pokemon_name_before_year_extracted(self):
        """'Haxorus 2012 Noble victories Holo' → card_name='Haxorus'."""
        info = extract_card_info("Haxorus 2012 Noble victories Holo")
        assert info["card_name"] == "Haxorus"
        assert info["card_name_matched"] is True

    def test_pokemon_name_before_year_set_name_extracted(self):
        """Set info after the year break point should be captured."""
        info = extract_card_info("Haxorus 2012 Noble victories Holo")
        # set_name contains 'Noble victories' (may include trailing 'Holo')
        assert info["set_name"] is not None
        assert "noble victories" in info["set_name"].lower()

    def test_pokemon_name_before_grade_extracted(self):
        """'Stargazer Pikachu & Friends CGC 9 ...' → card_name preserves full name."""
        title = (
            "Pokemon Stargazer Pikachu & Friends CGC 9 "
            "Astronomical Observatory 2025 Rare Japan"
        )
        info = extract_card_info(title)
        assert info["card_name"] == "Stargazer Pikachu & Friends"
        assert info["card_name_matched"] is True

    def test_grade_match_captured(self):
        """Grade should still be captured by card_identifier, not card_analyzer."""
        # card_analyzer doesn't return grade – that's card_identifier's job.
        # Just verify card_name is clean.
        info = extract_card_info("Charizard PSA 10 Base Set")
        assert info["card_name"] == "Charizard"
        assert info["card_name_matched"] is True

    def test_no_break_point_falls_back(self):
        """When no break point exists, the original fallback behaviour is used."""
        info = extract_card_info("Pikachu V")
        assert info["card_name"] == "Pikachu V"
        assert info["card_name_matched"] is True


class TestVintedListingFixes:
    """Regression tests for the three Vinted listing parsing bugs."""

    def test_gg_subset_prefix_infers_crown_zenith(self):
        """GG41/GG70 with no set text after the number → Crown Zenith inferred."""
        info = extract_card_info("Raikou GG41/GG70")
        assert info["collector_number"] == "GG41/GG70"
        assert info["set_code"] == "CRZ"
        assert info["set_name"] == "Crown Zenith"

    def test_jp_etat_nm_stripped_from_set(self):
        """'JP • État NM' after the collector number must not become the set name."""
        info = extract_card_info("Reshiram ex 029/193 JP • État NM")
        assert info["card_name"] == "Reshiram ex"
        assert info["collector_number"] == "029/193"
        # Language+condition noise should be stripped; no set info remains.
        assert info["set_code"] is None
        assert info["set_name"] is None

    def test_gym_challenge_trailing_noise_stripped(self):
        """Trailing noise words after a known set name must be discarded."""
        info = extract_card_info(
            "Pokémon Blaine's Arcanine 1/132 – Gym Challenge Holo Vintage Originale"
        )
        assert info["card_name"] == "Blaine's Arcanine"
        assert info["collector_number"] == "1/132"
        assert info["set_name"] == "Gym Challenge"


class TestIssue64HashCollectorNumber:
    """Regression tests for issue #64 – '#N' style collector numbers."""

    def test_wotc_hash_number(self):
        """'Kadabra #46 Pokemon Base Set 2' should extract number 46 and set name."""
        info = extract_card_info("Kadabra #46 Pokemon Base Set 2")
        assert info["card_name"] == "Kadabra"
        assert info["collector_number"] == "46"
        assert info["set_name"] == "Base Set 2"
        assert info["card_name_matched"] is True

    def test_hash_number_with_space(self):
        """'Pikachu # 25 Base Set' – space between # and digits is allowed."""
        info = extract_card_info("Pikachu # 25 Base Set")
        assert info["card_name"] == "Pikachu"
        assert info["collector_number"] == "25"
        assert info["card_name_matched"] is True

    def test_hash_number_no_set(self):
        """'Charizard #4' with no set info – set_name should be None."""
        info = extract_card_info("Charizard #4")
        assert info["card_name"] == "Charizard"
        assert info["collector_number"] == "4"
        assert info["set_name"] is None


class TestIssue66BuriedSetName:
    """Regression tests for issue #66 – set name buried in noisy text."""

    def test_trainer_gallery_noisy_after(self):
        """Set name 'Crown Zenith' buried after Trainer Gallery subset number."""
        info = extract_card_info(
            "Pikachu VMAX TG29/TG30 Trainer Gallery Oro Crown Zenith Galarian Gallery Ultra Rare"
        )
        assert info["card_name"] == "Pikachu VMAX"
        assert info["collector_number"] == "TG29/TG30"
        assert info["set_name"] == "Crown Zenith"


class TestIssue67LeadingDashAndRarityAsSet:
    """Regression tests for issue #67 – leading dash on card name and rarity as set name."""

    def test_leading_dash_stripped(self):
        """'Pokémon - Xatu 49/115 - Rare Holo' → card_name='Xatu' (no dash)."""
        info = extract_card_info("Pokémon - Xatu 49/115 - Rare Holo")
        assert info["card_name"] == "Xatu"
        assert info["collector_number"] == "49/115"

    def test_rarity_not_treated_as_set_name(self):
        """'Rare Holo' after the collector number should NOT become the set name."""
        info = extract_card_info("Pokémon - Xatu 49/115 - Rare Holo")
        assert info["set_name"] is None
        assert info["set_code"] is None

    def test_rarity_suffix_alone_returns_none(self):
        """_parse_set_info with only rarity text returns (None, None)."""
        assert _parse_set_info("Rare Holo") == (None, None)
        assert _parse_set_info("Holo Rare") == (None, None)
        assert _parse_set_info("Holo") == (None, None)
        assert _parse_set_info("Ultra Rare") == (None, None)



class TestConditionFullWordStripping:
    """Tests that full English condition words are treated as noise, not set names."""

    # --- _parse_set_info: standalone full condition words ---

    def test_mint_alone_returns_no_set(self):
        """A standalone 'Mint' condition word returns no set info."""
        code, name = _parse_set_info("Mint")
        assert code is None
        assert name is None

    def test_near_mint_alone_returns_no_set(self):
        """A standalone 'Near Mint' returns no set info."""
        code, name = _parse_set_info("Near Mint")
        assert code is None
        assert name is None

    def test_excellent_alone_returns_no_set(self):
        """A standalone 'Excellent' returns no set info."""
        code, name = _parse_set_info("Excellent")
        assert code is None
        assert name is None

    def test_good_alone_returns_no_set(self):
        """A standalone 'Good' returns no set info."""
        code, name = _parse_set_info("Good")
        assert code is None
        assert name is None

    def test_light_played_alone_returns_no_set(self):
        """A standalone 'Light Played' returns no set info."""
        code, name = _parse_set_info("Light Played")
        assert code is None
        assert name is None

    def test_played_alone_returns_no_set(self):
        """A standalone 'Played' returns no set info."""
        code, name = _parse_set_info("Played")
        assert code is None
        assert name is None

    def test_poor_alone_returns_no_set(self):
        """A standalone 'Poor' returns no set info."""
        code, name = _parse_set_info("Poor")
        assert code is None
        assert name is None

    # --- _parse_set_info: full condition word + set name ---

    def test_mint_prefix_stripped_set_name_found(self):
        """'Mint Obsidian Flames' → condition stripped, set name detected."""
        code, name = _parse_set_info("Mint Obsidian Flames")
        assert code is None
        assert name == "Obsidian Flames"

    def test_near_mint_prefix_stripped_set_name_found(self):
        """'Near Mint Obsidian Flames' → condition stripped, set name detected."""
        code, name = _parse_set_info("Near Mint Obsidian Flames")
        assert code is None
        assert name == "Obsidian Flames"

    def test_light_played_prefix_stripped_set_name_found(self):
        """'Light Played Obsidian Flames' → condition stripped, set name detected."""
        code, name = _parse_set_info("Light Played Obsidian Flames")
        assert code is None
        assert name == "Obsidian Flames"

    # --- extract_card_info: full condition word after lang code ---

    def test_jp_near_mint_stripped_from_set(self):
        """'JP Near Mint' after the collector number must not become the set name."""
        info = extract_card_info("Reshiram ex 029/193 JP Near Mint")
        assert info["card_name"] == "Reshiram ex"
        assert info["collector_number"] == "029/193"
        assert info["set_code"] is None
        assert info["set_name"] is None

    def test_en_mint_stripped_from_set(self):
        """'EN Mint' after the collector number must not become the set name."""
        info = extract_card_info("Pikachu 025/165 EN Mint")
        assert info["collector_number"] == "025/165"
        assert info["set_code"] is None
        assert info["set_name"] is None

    def test_jp_excellent_stripped_from_set(self):
        """'JP Excellent' after the collector number must not become the set name."""
        info = extract_card_info("Charizard 006/197 JP Excellent")
        assert info["collector_number"] == "006/197"
        assert info["set_code"] is None
        assert info["set_name"] is None


class TestIssue111PipeSeparatedTitle:
    """Issue #111 – card name wrongly includes rarity tokens when a pipe is used as separator."""

    def test_pipe_separated_reverse_holo(self):
        """'Oddish | Reverse Holo | EX Unseen Forces | 64/115' → card_name == 'Oddish'."""
        info = extract_card_info("Oddish | Reverse Holo | EX Unseen Forces | 64/115")
        assert info["card_name"] == "Oddish"
        assert info["collector_number"] == "64/115"
        assert info["card_name_matched"] is True

    def test_pipe_separated_holo(self):
        """Pipe-delimited title with plain Holo rarity strips correctly."""
        info = extract_card_info("Charizard | Holo | Base Set | 4/102")
        assert info["card_name"] == "Charizard"
        assert info["collector_number"] == "4/102"

    def test_pipe_separated_ultra_rare(self):
        """Pipe-delimited title with Ultra Rare strips correctly."""
        info = extract_card_info("Mewtwo | Ultra Rare | Scarlet & Violet | 205/198")
        assert info["card_name"] == "Mewtwo"
        assert info["collector_number"] == "205/198"


class TestIssueSetBeforeNumber:
    """Regression tests for listings where the set name appears BEFORE the
    collector number and the card name appears AFTER it.

    Example title format: "<set_name> <number> <card_name> [extra]"
    """

    def test_set_name_before_number_card_after(self):
        """'Ascended heroes 44/217 sneasel ball&normal' → sneasel / Ascended Heroes / ASC."""
        info = extract_card_info("Ascended heroes 44/217 sneasel ball&normal")
        assert info["card_name"] == "Sneasel"
        assert info["collector_number"] == "44/217"
        assert info["set_name"] == "Ascended Heroes"
        assert info["set_code"] == "ASC"
        assert info["card_name_matched"] is True

    def test_set_name_before_number_no_trailing_noise(self):
        """'Ascended heroes 44/217 Sneasel' (clean title) should also resolve correctly."""
        info = extract_card_info("Ascended heroes 44/217 Sneasel")
        assert info["card_name"] == "Sneasel"
        assert info["collector_number"] == "44/217"
        assert info["set_name"] == "Ascended Heroes"
        assert info["set_code"] == "ASC"

    def test_other_known_set_before_number(self):
        """'Obsidian Flames 125/197 Charizard ex' – set precedes number, card follows."""
        info = extract_card_info("Obsidian Flames 125/197 Charizard ex")
        assert info["card_name"] == "Charizard ex"
        assert info["collector_number"] == "125/197"
        assert info["set_name"] == "Obsidian Flames"
        assert info["card_name_matched"] is True
