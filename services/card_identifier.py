"""
services/card_identifier.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Card Identification Engine.

Parses a raw Vinted listing title into a structured card fingerprint and
generates a normalised string key that can be used for database lookups and
fuzzy matching.

Relies on ``utils.card_analyzer`` for the low-level parsing primitives that
are already tested and kept unchanged.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from utils.card_analyzer import extract_card_info
from utils.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Rarity / holo keywords
# ---------------------------------------------------------------------------
_RARITY_KEYWORDS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bholo\s*rare\b", re.I), "Holo Rare"),
    (re.compile(r"\breverse\s*holo\b", re.I), "Reverse Holo"),
    (re.compile(r"\bholo\b", re.I), "Holo"),
    (re.compile(r"\bsar\b", re.I), "SAR"),
    (re.compile(r"\bsr\b", re.I), "SR"),
    (re.compile(r"\brar[e]?\b", re.I), "Rare"),
    (re.compile(r"\buncommon\b", re.I), "Uncommon"),
    (re.compile(r"\bcommon\b", re.I), "Common"),
    (re.compile(r"\bsecret\s*rare\b", re.I), "Secret Rare"),
    (re.compile(r"\bultra\s*rare\b", re.I), "Ultra Rare"),
    (re.compile(r"\bfull\s*art\b", re.I), "Full Art"),
    (re.compile(r"\balt\s*art\b", re.I), "Alt Art"),
    (re.compile(r"\bspecial\s*illustration\b", re.I), "Special Illustration Rare"),
    (re.compile(r"\bhyper\s*rare\b", re.I), "Hyper Rare"),
    (re.compile(r"\bshiny\b", re.I), "Shiny"),
    (re.compile(r"\bgold\b", re.I), "Gold"),
    (re.compile(r"\brainbow\s*rare\b", re.I), "Rainbow Rare"),
]

_REVERSE_HOLO_RE = re.compile(r"\breverse\s*holo\b|\breverse\b|\bRH\b", re.I)
_FIRST_EDITION_RE = re.compile(r"\b(1st\s*ed(?:ition)?|first\s*edition)\b", re.I)

# ---------------------------------------------------------------------------
# Grading keywords
# ---------------------------------------------------------------------------
_GRADE_RE = re.compile(
    r"\b(PSA|BGS|CGC|SGC|GMA)\s*(\d+(?:\.\d+)?)\b", re.I
)

# ---------------------------------------------------------------------------
# Language keywords
# ---------------------------------------------------------------------------
_LANGUAGE_MAP: dict[str, str] = {
    "english": "English",
    "japanese": "Japanese",
    "deutsch": "German",
    "german": "German",
    "french": "French",
    "français": "French",
    "spanish": "Spanish",
    "español": "Spanish",
    "italian": "Italian",
    "italiano": "Italian",
    "portuguese": "Portuguese",
    "korean": "Korean",
    "dutch": "Dutch",
    "nederlands": "Dutch",
    "nl": "Dutch",
}
_LANGUAGE_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in _LANGUAGE_MAP) + r")\b",
    re.I,
)

# ---------------------------------------------------------------------------
# Game keywords
# ---------------------------------------------------------------------------
_GAME_RE = re.compile(
    r"\b(pokemon|magic(?:\s*the\s*gathering)?|mtg|yugioh|yu-gi-oh|flesh\s*and\s*blood|fab)\b",
    re.I,
)
_GAME_MAP: dict[str, str] = {
    "pokemon": "Pokemon",
    "magic the gathering": "Magic: The Gathering",
    "mtg": "Magic: The Gathering",
    "yugioh": "Yu-Gi-Oh!",
    "yu-gi-oh": "Yu-Gi-Oh!",
    "flesh and blood": "Flesh and Blood",
    "fab": "Flesh and Blood",
}


@dataclass
class CardFingerprint:
    """Structured card identity extracted from a Vinted listing title."""

    game: str | None = None
    card_name: str | None = None
    set_name: str | None = None
    set_code: str | None = None
    collector_number: str | None = None
    language: str | None = None
    rarity: str | None = None
    is_holo: bool = False
    is_reverse_holo: bool = False
    is_first_edition: bool = False
    is_promo: bool = False
    grade_authority: str | None = None  # PSA / BGS / CGC
    grade_value: str | None = None

    # Raw title stored for fallback matching
    raw_title: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "game": self.game,
            "card_name": self.card_name,
            "set_name": self.set_name,
            "set_code": self.set_code,
            "collector_number": self.collector_number,
            "language": self.language,
            "rarity": self.rarity,
            "is_holo": self.is_holo,
            "is_reverse_holo": self.is_reverse_holo,
            "is_first_edition": self.is_first_edition,
            "is_promo": self.is_promo,
            "grade_authority": self.grade_authority,
            "grade_value": self.grade_value,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    def normalised_key(self) -> str:
        """Return a short human-readable normalised key for display/logging."""
        parts: list[str] = []
        if self.card_name:
            parts.append(self.card_name)
        if self.set_name:
            parts.append(self.set_name)
        elif self.set_code:
            parts.append(self.set_code)
        if self.collector_number:
            parts.append(self.collector_number)
        if self.rarity:
            parts.append(self.rarity)
        return " | ".join(parts) if parts else self.raw_title[:80]

    def fingerprint_hash(self) -> str:
        """Return a short SHA-256 hash of the most identifying fields."""
        key_parts = [
            (self.card_name or "").lower().strip(),
            (self.set_code or "").upper().strip(),
            (self.collector_number or "").strip(),
        ]
        raw = "|".join(key_parts)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def token_set(self) -> list[str]:
        """Return a list of normalised tokens useful for fuzzy matching."""
        tokens: list[str] = []
        if self.card_name:
            tokens.extend(t.lower() for t in self.card_name.split())
        if self.set_name:
            tokens.extend(t.lower() for t in self.set_name.split())
        if self.set_code:
            tokens.append(self.set_code.lower())
        if self.collector_number:
            tokens.append(self.collector_number.lower())
        if self.rarity:
            tokens.append(self.rarity.lower())
        return tokens

    @property
    def is_identifiable(self) -> bool:
        """True if enough data exists to attempt a Cardmarket lookup."""
        return bool(self.card_name) and bool(self.set_name or self.set_code or self.collector_number)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def identify_card(title: str) -> CardFingerprint:
    """Parse a Vinted listing title and return a structured CardFingerprint.

    Uses ``utils.card_analyzer.extract_card_info`` for the core parsing and
    augments with game detection, rarity, language, and grading extraction.
    """
    fp = CardFingerprint(raw_title=title)

    if not title or not title.strip():
        return fp

    # --- Core fields from card_analyzer ---
    info = extract_card_info(title)
    fp.card_name = info.get("card_name")
    fp.set_name = info.get("set_name")
    fp.set_code = info.get("set_code")
    fp.collector_number = info.get("collector_number")

    # --- Game ---
    m = _GAME_RE.search(title)
    if m:
        raw_game = m.group(0).lower().strip()
        # Normalise multi-word keys
        for key, value in _GAME_MAP.items():
            if raw_game == key or raw_game.replace(" ", "") == key.replace(" ", ""):
                fp.game = value
                break
        if not fp.game:
            fp.game = "Pokemon"  # sensible default for this bot
    else:
        fp.game = "Pokemon"

    # --- Rarity ---
    for pattern, rarity_label in _RARITY_KEYWORDS:
        if pattern.search(title):
            fp.rarity = rarity_label
            break

    # --- Holo / Reverse holo ---
    if _REVERSE_HOLO_RE.search(title):
        fp.is_reverse_holo = True
        fp.is_holo = True
    elif fp.rarity and "holo" in fp.rarity.lower():
        fp.is_holo = True

    # --- First edition ---
    if _FIRST_EDITION_RE.search(title):
        fp.is_first_edition = True

    # --- Promo ---
    if fp.set_code and re.search(r"promo|svp|swshp|xy[a-z]*p\b", fp.set_code, re.I):
        fp.is_promo = True
    elif re.search(r"\bpromo\b", title, re.I):
        fp.is_promo = True

    # --- Language ---
    lm = _LANGUAGE_RE.search(title)
    if lm:
        fp.language = _LANGUAGE_MAP.get(lm.group(0).lower())

    # --- Grading ---
    gm = _GRADE_RE.search(title)
    if gm:
        fp.grade_authority = gm.group(1).upper()
        fp.grade_value = gm.group(2)

    logger.debug(
        "Fingerprint for '%s': %s", title[:60], fp.normalised_key()
    )
    return fp
