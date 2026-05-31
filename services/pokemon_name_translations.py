"""
services/pokemon_name_translations.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Helpers for translating common non-English Pokémon and set names to English
before card parsing/matching.
"""

from __future__ import annotations

import re

POKEMON_NAME_TRANSLATIONS: dict[str, str] = {
    # French
    "leveinard": "Chansey",
    "métamorph": "Ditto",
    "metamorph": "Ditto",
    "tarpaud": "Seismitoad",
    "rhinastoc": "Rhyperior",
    "wattzapf": "Helioptile",
    "lapyro": "Litleo",
    # Italian / Spanish / Dutch / German examples seen in DB
    "dracaufeu": "Charizard",
    "carapuce": "Squirtle",
    "bulbizarre": "Bulbasaur",
}

SET_NAME_TRANSLATIONS: dict[str, str] = {
    "ascesa eroica": "Ascended Heroes",
    "fatale flammen": "Phantasmal Flames",
    "forces temporelles": "Temporal Forces",
    "failles paradoxes": "Paradox Rift",
    "destinées de paldea": "Paldean Fates",
    "destinees de paldea": "Paldean Fates",
    "mascarade crépusculaire": "Twilight Masquerade",
    "mascarade crepusculaire": "Twilight Masquerade",
}


def translate_listing_title(title: str) -> str:
    """Return *title* with known non-English Pokémon and set names translated."""
    translated = title

    for source, target in sorted(
        POKEMON_NAME_TRANSLATIONS.items(), key=lambda item: len(item[0]), reverse=True
    ):
        translated = re.sub(
            rf"\b{re.escape(source)}\b",
            target,
            translated,
            flags=re.IGNORECASE,
        )

    for source, target in sorted(
        SET_NAME_TRANSLATIONS.items(), key=lambda item: len(item[0]), reverse=True
    ):
        translated = re.sub(
            re.escape(source),
            target,
            translated,
            flags=re.IGNORECASE,
        )

    return translated
