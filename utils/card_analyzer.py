"""
utils/card_analyzer.py
~~~~~~~~~~~~~~~~~~~~~~
Analyses a Vinted listing to determine whether it contains trading cards,
detects bulk lots, estimates card counts, and assigns a confidence level.

No hardcoded default card prices are used here.  Pricing decisions are
left entirely to live market lookups (price_lookup.py).

Card count estimation priority:
1. Explicit count from title/description  (e.g. "100 cards", "lot of 500")
2. Weight-based estimate                  (1.8 g per card average)
3. Bundle wording heuristics             (e.g. "joblot", "bulk box")
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from utils.logger import get_logger
from utils.pokemon_data import (
    CARD_PREFIXES,
    CARD_SUFFIXES,
    KNOWN_SET_CODES,
    SET_CODE_TO_SET_NAME,
    POKEMON_NAMES,
    _POKEMON_NAME_MAP,
)

if TYPE_CHECKING:
    from scraper.base import Listing

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Pokemon name pre-filter
# ---------------------------------------------------------------------------
# Compiled once at import time.  Sorted longest-first so multi-word names
# (e.g. "Brute Bonnet", "Iron Bundle") are matched before any overlapping
# shorter name fragment.
_POKEMON_NAMES_RE = re.compile(
    r"(?<![A-Za-z])(?:"
    + "|".join(re.escape(n) for n in sorted(_POKEMON_NAME_MAP.keys(), key=len, reverse=True))
    + r")(?![A-Za-z])",
    re.IGNORECASE,
)


def has_pokemon_name(title: str) -> bool:
    """Return True when *title* contains a recognised Pokemon name.

    Checks against :data:`POKEMON_NAMES` / :data:`_POKEMON_NAME_MAP` using a
    pre-compiled regex with letter-boundary anchors so that short names such as
    "Mew" are not spuriously matched inside longer unrelated words.
    """
    return bool(_POKEMON_NAMES_RE.search(title))


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Average weight of a single trading card in grams.
_GRAMS_PER_CARD: float = 1.8

# Keywords that indicate the listing is a non-TCG item (merchandise, storage,
# display items) and should be skipped before card identification.
# Note: "toploader" and "sleeve" are intentionally excluded because sellers
# often bundle them with cards; filtering them would cause false negatives.
NON_CARD_KEYWORDS: tuple[str, ...] = (
    "peluche",
    "plush",
    "t-shirt",
    "tshirt",
    "sleeve",
    "sleeves",
    "soft sleeve",
    "toploader",
    "top loader",
    "classeur",
    "binder",
    "coffret",
    "etb",
    "elite trainer box",
    "booster display",
    "booster box",
    "display",
    "36er display",
    "figure",
    "figurine",
    "playmat",
    "deck box",
    "deckbox",
    "tin",
    "album",
    "display box",
    "blister pack",
    "promo pack",
    "poster",
    "sticker sheet",
    "pin badge",
    "statue",
    "gameboy",
    "game boy",
    "nintendo ds",
    "nintendo switch",
    "switch game",
    "cartucho",
)

def is_non_card_item(title: str, description: str | None = None) -> bool:
    """Return True when *title* (or *description*) clearly indicates a non-TCG item.

    Checks against :data:`NON_CARD_KEYWORDS`.  ``toploader`` and ``sleeve`` are
    deliberately excluded so that card+accessory combo listings are not filtered.
    """
    combined = (title + " " + (description or "")).lower()
    return any(kw in combined for kw in NON_CARD_KEYWORDS)


_LOT_KEYWORDS: tuple[str, ...] = (
    "lot",
    "lotto",
    "bundle",
    "lot de",
    "boite",
    "boîte",
    "display",
)

_LOT_COUNT_RE = re.compile(
    r"(?:\b\d{1,4}\s*x\b|\bx\s*\d{1,4}\b|\b\d{1,4}\s*(?:carte|cartes|karte|karten|carta|cartas)\b)",
    re.IGNORECASE,
)


def is_lot_listing(title: str, description: str | None = None) -> bool:
    """Return True when text suggests a multi-card lot/bundle listing."""
    combined = (title + " " + (description or "")).lower()
    if any(kw in combined for kw in _LOT_KEYWORDS):
        return True
    return _LOT_COUNT_RE.search(combined) is not None


_GRADED_RE = re.compile(
    r"\b(?:psa|bgs|cgc|sgs|ace|pgs|beckett|graad|grad[eé]e?|gradato|graded)\b"
    r"(?:\s*[:#-]?\s*\d{1,2}(?:[.,]\d)?)?",
    re.IGNORECASE,
)


def is_graded_listing(title: str, description: str | None = None) -> bool:
    """Return True when text suggests a graded/slabbed card listing."""
    combined = title + " " + (description or "")
    return _GRADED_RE.search(combined) is not None


_JAPANESE_KEYWORDS: tuple[str, ...] = (
    " jap",
    "jap.",
    "japanese",
    "japonais",
    "giapponese",
    "japanesé",
)
_JAPANESE_CHAR_RE = re.compile(r"[\u3040-\u30ff\u4e00-\u9faf]")


def is_japanese_listing(title: str, description: str | None = None) -> bool:
    """Return True when listing appears to be a Japanese-language card."""
    combined = (" " + title + " " + (description or "")).lower()
    if any(kw in combined for kw in _JAPANESE_KEYWORDS):
        return True
    return _JAPANESE_CHAR_RE.search(title + " " + (description or "")) is not None


# Title/description substrings that indicate the listing is trading cards.
_CARD_KEYWORDS: tuple[str, ...] = (
    "pokemon",
    "pokémon",
    "magic the gathering",
    "mtg",
    "yugioh",
    "yu-gi-oh",
    "trading card",
    "tcg",
    "booster",
    "holo",
    "charizard",
    "pikachu",
    "umbreon",
    "espeon",
    "vmax",
    "vstar",
    "psa",
    "bgs",
    "graded card",
    "card lot",
    "bulk cards",
    "kaarten",        # Dutch
    "karten",         # German
    "cartes",         # French
)

# Substrings that strongly suggest a bulk lot rather than a single card.
_BULK_KEYWORDS: tuple[str, ...] = (
    "lot",
    "bulk",
    "bundle",
    "collection",
    "joblot",
    "job lot",
    "mixed lot",
    "bulk cards",
    "card lot",
    "assorted",
    "random cards",
    "kaarten lot",    # Dutch
    "karten lot",     # German
    "bulk box",
    "100 cards",
    "200 cards",
    "500 cards",
    "1000 cards",
)

# Regex patterns for explicit card counts.
# Matches things like "100 cards", "lot of 500", "x250 cards", "250x", etc.
_COUNT_PATTERNS: list[re.Pattern[str]] = [
    # "500 cards", "100 pokemon cards", "250 card lot"
    re.compile(r"\b(\d{2,5})\s*(?:x\b)?\s*(?:pokemon\s+)?cards?\b", re.IGNORECASE),
    # "lot of 500", "lot of approx 200"
    re.compile(r"lot\s+of\s+(?:approx\.?\s+)?(\d{2,5})", re.IGNORECASE),
    # "x500", "x250" at start of title or after space
    re.compile(r"(?:^|\s)x(\d{2,5})\b", re.IGNORECASE),
    # "250x" (quantity prefix)
    re.compile(r"\b(\d{2,5})x\b", re.IGNORECASE),
    # "approx 300 cards"
    re.compile(r"approx\.?\s+(\d{2,5})\s*cards?\b", re.IGNORECASE),
    # "+-300", "+/-300" (approximate count)
    re.compile(r"[+\-]{1,2}/?[+\-]{0,1}\s*(\d{2,5})\s*cards?\b", re.IGNORECASE),
]

# Regex patterns for weight extraction.
# Matches "1800g", "1.8kg", "2 kg", "500 gram", etc.
_WEIGHT_PATTERNS: list[re.Pattern[str]] = [
    # "1800g", "1800 g", "1800gram"
    re.compile(r"\b(\d+(?:[.,]\d+)?)\s*(?:gram|grams|gr|g)\b", re.IGNORECASE),
    # "1.8kg", "1,8 kg", "2kg"
    re.compile(r"\b(\d+(?:[.,]\d+)?)\s*kg\b", re.IGNORECASE),
]

# ---------------------------------------------------------------------------
# Card identity extraction from listing titles
# ---------------------------------------------------------------------------

# Language + game prefix patterns to strip before extracting card info.
# Covers common French/Dutch/German/English/Spanish/Italian prefixes.
_LANG_PREFIX_RE = re.compile(
    r"^(?:carte\s+|kaart\s+|karte\s+|carta\s+)?"
    r"pok[eé]mon\s+(?:card\s+|kaart\s+|karte\s+|carte\s+|carta\s+)?",
    re.IGNORECASE,
)

# Collector number: e.g. "218/172", "006/197", "044/185"
_COLLECTOR_NUMBER_RE = re.compile(r"\b(\d{1,4})/(\d{2,4})\b")

# Subset collector number: e.g. "SV49/SV94" (Shiny Vault in Hidden Fates),
# "TG09/TG30" (Trainer Gallery in Silver Tempest), "GG12/GG70" (Crown Zenith).
# First group: card number with letter prefix + 2-3 digits.
# Second group: the total / set size (letter prefix optional).
# Requiring 2+ digits prevents false-positive matches on set codes like "SV1a".
_SUBSET_NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9])([A-Z]{1,3}\d{2,3}[a-z]?)\s*/\s*([A-Z]{0,3}\d{2,3}[a-z]?)(?![A-Za-z0-9])"
)

# Hash-style collector number: e.g. "#46" or "# 46".
# Used in WOTC-era listings such as "Kadabra #46 Pokemon Base Set 2".
_HASH_NUMBER_RE = re.compile(r"#\s*(\d{1,4})\b")

# Promo-style collector number: e.g. "SVP 214", "SVP214", "SWSHP 088", "XYP 145"
# Pattern: 2–6 uppercase letters (set code, typically ending in P for promos)
# followed by optional whitespace and 1–4 digits.
# Must be surrounded by word boundaries / non-alphanumeric chars to avoid false positives.
_PROMO_NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9])([A-Z]{2,6}P?)\s*(\d{1,4})(?![0-9A-Za-z/])"
)

# Known rarity/variant codes that appear *between* the card name and number.
# These are stripped so the card name is clean.  The pattern is applied
# iteratively (see _strip_rarity_suffixes) so multi-word suffixes like
# "Holo 1st Edition" are removed in two passes.
_RARITY_SUFFIX_RE = re.compile(
    r"[\s|]+\b(?:"
    r"SAR|CHR|CSR|SSR|UR|IR|HR|RR|AR|PROMO|SR|PR"
    r"|Full\s+Art"
    r"|Alt(?:ernate)?\s+Art"
    r"|Special\s+Illustration(?:\s+Rare)?"
    r"|Hyper\s+Rare"
    r"|Rainbow\s+Rare"
    r"|Ultra\s+Rare"
    r"|Secret\s+Rare"
    r"|Shiny(?:\s+Rare)?"
    r"|Rare\s+Holo"
    r"|Holo\s+Rare"
    r"|Reverse\s+Holo"
    r"|Holo"
    r"|Rare"
    r"|1st\s+Edition"
    r"|First\s+Edition"
    r")\b[\s|]*$",
    re.IGNORECASE,
)


def _strip_rarity_suffixes(text: str) -> str:
    """Apply ``_RARITY_SUFFIX_RE`` repeatedly until the name stabilises.

    A single pass is not enough for compound suffixes such as "Holo 1st Edition"
    (two distinct rarity tokens).  Iterating guarantees they are all stripped.
    """
    while True:
        stripped = _RARITY_SUFFIX_RE.sub("", text).strip()
        if stripped == text:
            return text
        text = stripped

# Matches a grade-certifier + grade-number token that acts as a "break point"
# when parsing noisy titles with no collector number.
# e.g. "PSA 9", "CGC 9.5", "BGS 10"
_GRADE_BREAK_RE = re.compile(
    r"\b(?:PSA|BGS|CGC|SGC|GMA)\s*\d+(?:\.\d+)?\b",
    re.IGNORECASE,
)

# Matches a standalone 4-digit calendar year used as a break point in noisy
# titles.  Requires that the year is NOT preceded or followed by another digit.
_YEAR_BREAK_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")

# Set code: a short alphanumeric token that appears right after the collector
# number (with an optional "–" or "-" separator).
# Pattern: starts uppercase, 2–6 chars total, last char may be lowercase/digit.
# e.g. S12a, SV1a, OBF, PAR, MEW, SWSH, BW, XY  — but NOT VSTAR, PROMO, etc.
_SET_CODE_RE = re.compile(
    r"^(?:[\s\-–—]+)?([A-Z][A-Z0-9]{0,3}[0-9a-z]?)\s+(\S.+)$"
)
_SET_CODE_ONLY_RE = re.compile(
    r"^(?:[\s\-–—]+)?([A-Z][A-Z0-9]{0,3}[0-9a-z]?)\s*$"
)

# Rarity/type tokens that must NOT be treated as a promo set code.
_NOT_PROMO_TOKENS: frozenset[str] = frozenset({
    "VMAX", "VSTAR", "VUNION", "EX", "GX", "TAG", "PROMO",
    "PSA", "BGS", "CGC", "NM", "LP", "MP", "HP", "MINT",
    "SAR", "CHR", "CSR", "SSR", "RR", "AR", "SR", "UR", "IR", "HR", "PR",
})

# ---------------------------------------------------------------------------
# Subset prefix → set inference
# ---------------------------------------------------------------------------

# When a subset collector number (e.g. "GG41/GG70") is found and no set info
# appears after it, we can infer the set from the prefix when it is unambiguous.
_SUBSET_PREFIX_TO_SET: dict[str, tuple[str, str]] = {
    # Crown Zenith Galarian Gallery cards (GG01–GG70) only appear in Crown Zenith.
    "GG": ("CRZ", "Crown Zenith"),
    # Trainer Gallery cards (TG01–TG30) appear in Lost Origin.
    "TG": ("LOR", "Lost Origin"),
}

# ---------------------------------------------------------------------------
# Known set names for title matching
# ---------------------------------------------------------------------------

# List of (lowercase_key, canonical_display_name) tuples.
# Sorted longest-first so longer names (e.g. "team rocket returns") are matched
# before shorter overlapping names (e.g. "team rocket").
# Used in _find_known_set_name() to identify the set name from a title fragment
# even when it is followed by extra noise words.
_KNOWN_SET_NAMES: list[tuple[str, str]] = sorted(
    [
        # Scarlet & Violet Era
        ("scarlet & violet", "Scarlet & Violet"),
        ("scarlet violet", "Scarlet & Violet"),
        ("paldea evolved", "Paldea Evolved"),
        ("obsidian flames", "Obsidian Flames"),
        ("paradox rift", "Paradox Rift"),
        ("paldean fates", "Paldean Fates"),
        ("temporal forces", "Temporal Forces"),
        ("twilight masquerade", "Twilight Masquerade"),
        ("shrouded fable", "Shrouded Fable"),
        ("stellar crown", "Stellar Crown"),
        ("surging sparks", "Surging Sparks"),
        ("prismatic evolutions", "Prismatic Evolutions"),
        ("journey together", "Journey Together"),
        ("destined rivals", "Destined Rivals"),
        ("151", "151"),
        # Sword & Shield Era
        ("sword & shield", "Sword & Shield"),
        ("sword shield", "Sword & Shield"),
        ("rebel clash", "Rebel Clash"),
        ("darkness ablaze", "Darkness Ablaze"),
        ("champion's path", "Champion's Path"),
        ("champions path", "Champion's Path"),
        ("vivid voltage", "Vivid Voltage"),
        ("shining fates", "Shining Fates"),
        ("battle styles", "Battle Styles"),
        ("chilling reign", "Chilling Reign"),
        ("evolving skies", "Evolving Skies"),
        ("celebrations", "Celebrations"),
        ("fusion strike", "Fusion Strike"),
        ("brilliant stars", "Brilliant Stars"),
        ("astral radiance", "Astral Radiance"),
        ("pokémon go", "Pokémon GO"),
        ("pokemon go", "Pokémon GO"),
        ("lost origin", "Lost Origin"),
        ("silver tempest", "Silver Tempest"),
        ("crown zenith", "Crown Zenith"),
        # Sun & Moon Era
        ("sun & moon", "Sun & Moon"),
        ("sun moon", "Sun & Moon"),
        ("guardians rising", "Guardians Rising"),
        ("burning shadows", "Burning Shadows"),
        ("shining legends", "Shining Legends"),
        ("crimson invasion", "Crimson Invasion"),
        ("ultra prism", "Ultra Prism"),
        ("forbidden light", "Forbidden Light"),
        ("celestial storm", "Celestial Storm"),
        ("dragon majesty", "Dragon Majesty"),
        ("lost thunder", "Lost Thunder"),
        ("team up", "Team Up"),
        ("detective pikachu", "Detective Pikachu"),
        ("unbroken bonds", "Unbroken Bonds"),
        ("unified minds", "Unified Minds"),
        ("hidden fates", "Hidden Fates"),
        ("cosmic eclipse", "Cosmic Eclipse"),
        # XY Era
        ("kalos starter set", "Kalos Starter Set"),
        ("flashfire", "Flashfire"),
        ("furious fists", "Furious Fists"),
        ("phantom forces", "Phantom Forces"),
        ("primal clash", "Primal Clash"),
        ("double crisis", "Double Crisis"),
        ("roaring skies", "Roaring Skies"),
        ("ancient origins", "Ancient Origins"),
        ("breakthrough", "Breakthrough"),
        ("breakpoint", "Breakpoint"),
        ("generations", "Generations"),
        ("fates collide", "Fates Collide"),
        ("steam siege", "Steam Siege"),
        ("evolutions", "Evolutions"),
        ("xy", "XY"),
        # Black & White Era
        ("black & white", "Black & White"),
        ("black white", "Black & White"),
        ("emerging powers", "Emerging Powers"),
        ("noble victories", "Noble Victories"),
        ("next destinies", "Next Destinies"),
        ("dark explorers", "Dark Explorers"),
        ("dragons exalted", "Dragons Exalted"),
        ("dragon vault", "Dragon Vault"),
        ("boundaries crossed", "Boundaries Crossed"),
        ("plasma storm", "Plasma Storm"),
        ("plasma freeze", "Plasma Freeze"),
        ("plasma blast", "Plasma Blast"),
        ("legendary treasures", "Legendary Treasures"),
        # Diamond & Pearl Era
        ("diamond & pearl", "Diamond & Pearl"),
        ("diamond pearl", "Diamond & Pearl"),
        ("mysterious treasures", "Mysterious Treasures"),
        ("secret wonders", "Secret Wonders"),
        ("great encounters", "Great Encounters"),
        ("majestic dawn", "Majestic Dawn"),
        ("legends awakened", "Legends Awakened"),
        ("stormfront", "Stormfront"),
        ("platinum", "Platinum"),
        ("rising rivals", "Rising Rivals"),
        ("supreme victors", "Supreme Victors"),
        ("heartgold & soulsilver", "HeartGold SoulSilver"),
        ("heartgold soulsilver", "HeartGold SoulSilver"),
        ("unleashed", "Unleashed"),
        ("undaunted", "Undaunted"),
        ("triumphant", "Triumphant"),
        ("call of legends", "Call of Legends"),
        # EX Era
        ("ruby & sapphire", "Ruby & Sapphire"),
        ("ruby sapphire", "Ruby & Sapphire"),
        ("sandstorm", "Sandstorm"),
        ("team magma vs team aqua", "Team Magma vs Team Aqua"),
        ("hidden legends", "Hidden Legends"),
        ("firered & leafgreen", "FireRed & LeafGreen"),
        ("firered leafgreen", "FireRed & LeafGreen"),
        ("team rocket returns", "Team Rocket Returns"),
        ("deoxys", "Deoxys"),
        ("emerald", "Emerald"),
        ("unseen forces", "Unseen Forces"),
        ("delta species", "Delta Species"),
        ("legend maker", "Legend Maker"),
        ("holon phantoms", "Holon Phantoms"),
        ("crystal guardians", "Crystal Guardians"),
        ("dragon frontiers", "Dragon Frontiers"),
        ("power keepers", "Power Keepers"),
        # EX Era – full names as they appear in Vinted titles (with "EX " prefix)
        ("ex ruby & sapphire", "Ruby & Sapphire"),
        ("ex ruby sapphire", "Ruby & Sapphire"),
        ("ex sandstorm", "Sandstorm"),
        ("ex dragon", "Dragon"),
        ("ex team magma vs team aqua", "Team Magma vs Team Aqua"),
        ("ex hidden legends", "Hidden Legends"),
        ("ex firered & leafgreen", "FireRed & LeafGreen"),
        ("ex firered leafgreen", "FireRed & LeafGreen"),
        ("ex team rocket returns", "Team Rocket Returns"),
        ("ex deoxys", "Deoxys"),
        ("ex emerald", "Emerald"),
        ("ex unseen forces", "Unseen Forces"),
        ("ex delta species", "Delta Species"),
        ("ex legend maker", "Legend Maker"),
        ("ex holon phantoms", "Holon Phantoms"),
        ("ex crystal guardians", "Crystal Guardians"),
        ("ex dragon frontiers", "Dragon Frontiers"),
        ("ex power keepers", "Power Keepers"),
        # Neo Era
        ("neo genesis", "Neo Genesis"),
        ("neo discovery", "Neo Discovery"),
        ("neo revelation", "Neo Revelation"),
        ("neo destiny", "Neo Destiny"),
        ("legendary collection", "Legendary Collection"),
        ("aquapolis", "Aquapolis"),
        ("skyridge", "Skyridge"),
        # Base Set Era
        ("base set 2", "Base Set 2"),
        ("base set", "Base Set"),
        ("jungle", "Jungle"),
        ("fossil", "Fossil"),
        ("team rocket", "Team Rocket"),
        ("gym heroes", "Gym Heroes"),
        ("gym challenge", "Gym Challenge"),
        # Japanese sets
        ("vstar universe", "VSTAR Universe"),
        ("star birth", "Star Birth"),
        ("vmax climax", "VMAX Climax"),
        ("incandescent arcana", "Incandescent Arcana"),
        # Mega Evolution / Japanese 2024-2025 sets
        ("mega evolution", "Mega Evolution"),
        ("phantasmal flames", "Phantasmal Flames"),
        ("ascended heroes", "Ascended Heroes"),
        ("perfect order", "Perfect Order"),
        ("chaos rising", "Chaos Rising"),
        ("pitch black", "Pitch Black"),
        ("black bolt", "Black Bolt"),
        ("white flare", "White Flare"),
        # Misc / promos
        ("southern islands", "Southern Islands"),
        ("wizards black star promos", "Wizards Black Star Promos"),
        ("nintendo black star promos", "Nintendo Black Star Promos"),
        ("dp black star promos", "DP Black Star Promos"),
        ("hgss black star promos", "HGSS Black Star Promos"),
        ("bw black star promos", "BW Black Star Promos"),
        ("xy black star promos", "XY Black Star Promos"),
        ("sm black star promos", "SM Black Star Promos"),
        ("swsh black star promos", "SWSH Black Star Promos"),
        ("sv black star promos", "SV Black Star Promos"),
    ],
    key=lambda x: len(x[0]),
    reverse=True,
)

# Reverse lookup: canonical set name (lowercase) → set code.
# Derived from SET_CODE_TO_SET_NAME so it stays in sync automatically.
# When multiple codes share the same display name the first one wins (rare).
_SET_NAME_TO_CODE: dict[str, str] = {
    name.lower(): code for code, name in reversed(list(SET_CODE_TO_SET_NAME.items()))
}

# ---------------------------------------------------------------------------
# After-number noise stripping
# ---------------------------------------------------------------------------

# Regex to strip a leading 2-letter ISO language code that sellers include
# after the collector number to indicate card language (e.g. "JP", "EN", "FR").
_LANG_CODE_PREFIX_RE = re.compile(
    r"^(JP|JA|EN|FR|DE|NL|ES|IT|KO|RU|PT)\b",
    re.IGNORECASE,
)

# Regex to strip a leading condition word (translated/foreign "condition").
_CONDITION_WORD_PREFIX_RE = re.compile(
    r"^(état|etat|zustand|condición|conditie|condition|staat)\b",
    re.IGNORECASE,
)

# Regex to strip a leading English condition full word/phrase.
# Multi-word phrases are listed before single-word ones so they match first.
_CONDITION_FULL_PREFIX_RE = re.compile(
    r"^(near\s+mint|light\s+played|excellent|played|mint|poor|good)\b",
    re.IGNORECASE,
)

# Regex to strip a leading condition abbreviation.
# Excludes EX (set code), PL (Platinum set code), HP (Holon Phantoms set code).
_CONDITION_ABBREV_PREFIX_RE = re.compile(
    r"^(NM|LP|GD|PO|VG)\b",
    re.IGNORECASE,
)

# Trailing game-context noise that is NOT part of a card name:
# e.g. "Probopass pokemon card" → strip "pokemon card" → "Probopass"
# e.g. "Pikachu TCG"           → strip "TCG"          → "Pikachu"
_GAME_SUFFIX_RE = re.compile(
    r"\s+(?:pok[eé]mon\s+)?(?:tcg\s+)?(?:card|kaart|karte|carte|carta)s?\s*$"
    r"|\s+(?:pok[eé]mon|tcg)\s*$",
    re.IGNORECASE,
)


def _find_known_set_name(text: str) -> str | None:
    """Return the canonical set name if *text* starts with a known set name.

    Scans ``_KNOWN_SET_NAMES`` (longest-first) and returns the canonical name
    if the text starts with that name and is followed by end-of-string or a
    non-alphanumeric character (word boundary).  Trailing noise words such as
    "Holo Vintage Originale" are therefore ignored.

    Returns ``None`` if no known set name is found.
    """
    normalised = " ".join(text.lower().split())
    for lower_key, canonical in _KNOWN_SET_NAMES:
        if normalised.startswith(lower_key):
            rest = normalised[len(lower_key):]
            if not rest or not rest[0].isalpha():
                return canonical
    return None


def _search_known_set_name(text: str) -> str | None:
    """Return the canonical set name if a known set name appears *anywhere* in *text*.

    Unlike :func:`_find_known_set_name` which only matches at the start of the
    text, this function scans the full string.  It is used as a fallback when
    the set name is buried after extraneous tokens — for example::

        "Trainer Gallery Oro Crown Zenith Galarian Gallery Ultra Rare"
        → "Crown Zenith"

    Returns the canonical name for the longest/first match, or ``None``.
    """
    normalised = " ".join(text.lower().split())
    for lower_key, canonical in _KNOWN_SET_NAMES:
        idx = normalised.find(lower_key)
        if idx == -1:
            continue
        # Ensure the match sits at a word boundary (preceded by start or space,
        # followed by end or non-alpha).
        if idx > 0 and normalised[idx - 1] != " ":
            continue
        end_idx = idx + len(lower_key)
        if end_idx < len(normalised) and normalised[end_idx].isalpha():
            continue
        return canonical
    return None


def _split_set_from_before(before: str) -> tuple[str, str]:
    """Split a set-name fragment that leaked into the *before* text.

    Some multilingual listing titles embed set info before the collector
    number, separated by " - ", e.g.::

        "Hypno Holo 1st Edition - Fossil WOTC Vintage (8/62)"
        → before="Hypno Holo 1st Edition - Fossil WOTC Vintage ("

    This function:
    1. Strips any trailing ``(`` / whitespace from *before*.
    2. When *before* contains " - " and the fragment after it starts with a
       known set name, splits there and returns *(card_part, set_fragment)*.
    3. Otherwise returns *(cleaned_before, "")*·

    Returns *(card_name_candidate, set_fragment)*; set_fragment is empty string
    when no set info was found in the before text.
    """
    # Remove trailing open-parenthesis characters left over from "(number)"
    # notation, e.g. "Hypno Holo - Fossil WOTC Vintage (" → strip "(".
    cleaned = re.sub(r"[\s(]+$", "", before).strip()

    dash_pos = cleaned.rfind(" - ")
    if dash_pos > 0:
        candidate_set = cleaned[dash_pos + 3:].strip()
        if _find_known_set_name(candidate_set):
            return cleaned[:dash_pos].strip(), candidate_set

    return cleaned, ""


def _split_before_at_known_set(text: str) -> tuple[str, str]:
    """Search *text* for a known set name anywhere (not just after ' - ').

    Useful when the set name appears inline before the collector number, e.g.::

        "Stoutland ir white flare 156/086"
        → card_part="Stoutland ir", set_fragment="white flare"

    Returns *(card_part, set_fragment)* where *set_fragment* is the
    canonical set name, or *(text, "")* when no known set is found.
    The search prefers the *rightmost* occurrence to keep as much of the
    card name as possible.
    """
    normalised = " ".join(text.lower().split())
    best_idx: int = -1
    best_canonical: str = ""
    for lower_key, canonical in _KNOWN_SET_NAMES:
        idx = normalised.find(lower_key)
        while idx != -1:
            # Ensure word boundary before.
            if idx > 0 and normalised[idx - 1] != " ":
                idx = normalised.find(lower_key, idx + 1)
                continue
            # Ensure word boundary after.
            end_idx = idx + len(lower_key)
            if end_idx < len(normalised) and normalised[end_idx].isalpha():
                idx = normalised.find(lower_key, idx + 1)
                continue
            # Prefer the leftmost match with the longest key (sorted longest-first);
            # when two keys start at the same position the longer one wins already.
            if best_idx == -1 or idx < best_idx:
                best_idx = idx
                best_canonical = canonical
            break
    if best_idx == -1:
        return text, ""
    # Map the index back to the original text using character count.
    card_part = text[:best_idx].rstrip()
    return card_part, best_canonical


def _strip_after_number_noise(text: str) -> str:
    """Strip language-code and condition tokens from text that follows a
    collector number in a Vinted (or similar) listing title.

    Sellers often append tokens like "JP • État NM" or "EN NM" after the card
    number to indicate the card language and condition.  These tokens must not
    be mistaken for a set name.

    Stripping rules (applied left-to-right after removing leading separators):
    1. A 2-letter ISO language code is stripped if found (JP, JA, EN, FR …).
    2. After a language code was stripped, any separator characters are removed.
    3. After a language code was stripped, a condition word is stripped if found
       (état, etat, zustand, conditie …).
    4. After a language code was stripped, an English condition full word or phrase
       is stripped if found (Mint, Near Mint, Excellent, Good, Light Played,
       Played, Poor).  These are *only* stripped after a language code; standalone
       stripping in other contexts is handled by ``_parse_set_info``.
    5. After a language code was stripped, a condition abbreviation is stripped
       if found (NM, LP, GD, PO, VG).  Same language-code prerequisite applies.
    6. Any remaining leading separators/whitespace are stripped.

    Returns the remaining text (may be empty string).
    """
    # Remove leading separators and whitespace.
    stripped = re.sub(r"^[\s\-–—•·|/\\]+", "", text)

    lang_found = False
    m = _LANG_CODE_PREFIX_RE.match(stripped)
    if m:
        stripped = stripped[m.end():].lstrip()
        lang_found = True

    if lang_found:
        # Strip any separator between lang code and condition.
        stripped = re.sub(r"^[\s\-–—•·|/\\]+", "", stripped)
        # Strip condition word (e.g. "état", "zustand").
        m = _CONDITION_WORD_PREFIX_RE.match(stripped)
        if m:
            stripped = stripped[m.end():].lstrip()
            stripped = re.sub(r"^[\s\-–—•·|/\\]+", "", stripped)
        # Strip English condition full word/phrase (e.g. "Near Mint", "Mint").
        m = _CONDITION_FULL_PREFIX_RE.match(stripped)
        if m:
            stripped = stripped[m.end():].lstrip()
        # Strip condition abbreviation (e.g. "NM", "LP") — only after lang code.
        m = _CONDITION_ABBREV_PREFIX_RE.match(stripped)
        if m:
            stripped = stripped[m.end():].lstrip()

    # Final cleanup of any leftover leading separators.
    stripped = re.sub(r"^[\s\-–—•·|/\\]+", "", stripped)
    return stripped


def _match_pokemon_name(text: str) -> tuple[str | None, bool]:
    """Try to parse *text* as ``[prefix] [pokemon_name] [suffix]``.

    Returns ``(full_card_name, is_matched)`` where:

    - *full_card_name* is the canonical card name (preserving prefix/suffix
      from *text* and the canonical base name from the known list), or the
      original *text* when no match is found.
    - *is_matched* is ``True`` when the base name was found in the known
      Pokémon names list.

    Examples::

        _match_pokemon_name("Charizard ex")       → ("Charizard ex", True)
        _match_pokemon_name("Alolan Ninetales GX") → ("Alolan Ninetales GX", True)
        _match_pokemon_name("Pikachu VMAX")        → ("Pikachu VMAX", True)
        _match_pokemon_name("Random Card EX")      → ("Random Card EX", False)
    """
    if not text:
        return None, False

    working = " ".join(text.split()).strip()

    # --- Strip trailing game-context noise before attempting name lookup ---
    # e.g. "Probopass pokemon card"  → "Probopass"
    # e.g. "Mewtwo ex pokemon kaart" → "Mewtwo ex"
    game_stripped = _GAME_SUFFIX_RE.sub("", working).strip()
    if game_stripped:
        working = game_stripped

    # --- Strip trailing card-name suffix (longest first) ---
    found_suffix: str | None = None
    for sfx in CARD_SUFFIXES:
        # Word-boundary-aware suffix match at end of string.
        pattern = r"(?i)(?:\s+)" + re.escape(sfx) + r"\s*$"
        m = re.search(pattern, working)
        if m:
            found_suffix = working[m.start() :].strip()  # preserve original casing
            working = working[: m.start()].strip()
            break

    # --- Strip leading card-name prefix (longest first) ---
    found_prefix: str | None = None
    for pfx in CARD_PREFIXES:
        pattern = r"^" + re.escape(pfx) + r"(?:\s+|$)"
        m = re.match(pattern, working, re.IGNORECASE)
        if m:
            found_prefix = working[: m.end()].strip()  # preserve original casing
            working = working[m.end() :].strip()
            break

    # --- Lookup remaining text in the known Pokémon names list ---
    canonical = _POKEMON_NAME_MAP.get(working.lower())
    if canonical is not None:
        parts: list[str] = []
        if found_prefix:
            parts.append(found_prefix)
        parts.append(canonical)
        if found_suffix:
            parts.append(found_suffix)
        return " ".join(parts), True

    # No match – return the original (cleaned) text as-is.
    original = " ".join(text.split()).strip()
    return original or None, False


def _verify_pokemon_in_text(text: str) -> bool:
    """Return True if at least one known Pokémon name appears anywhere in *text*.

    Scans all contiguous word sequences within *text* and checks each against
    the known Pokémon name map.  This allows the caller to confirm that a card
    name candidate like ``"Stargazer Pikachu & Friends"`` contains a valid
    Pokémon name (``"Pikachu"``) even though the full phrase is not itself a
    Pokémon name.
    """
    words = text.split()
    for i in range(len(words)):
        for j in range(i + 1, len(words) + 1):
            fragment = " ".join(words[i:j]).lower()
            if fragment in _POKEMON_NAME_MAP:
                return True
    return False


def _extract_promo_number(text: str) -> tuple[str | None, str | None, str | None]:
    """Try to extract a promo-style collector number from *text*.

    Returns *(card_name, set_code, bare_number)* or *(None, None, None)* when
    no promo number is found.

    Examples::

        _extract_promo_number("Pikachu (SVP 214)")  → ("Pikachu", "SVP", "214")
        _extract_promo_number("Pikachu SVP214")      → ("Pikachu", "SVP", "214")
    """
    for m in _PROMO_NUMBER_RE.finditer(text):
        token = m.group(1)
        number = m.group(2)
        # Skip tokens that are clearly not set codes.
        if token.upper() in _NOT_PROMO_TOKENS:
            continue
        # The token must look like a plausible set code:
        # pure uppercase letters, possibly ending with "P", length 2-6.
        if not re.match(r"^[A-Z]{2,6}$", token):
            continue
        # Everything before the match (minus surrounding punctuation) is the card name.
        before = text[: m.start()].strip(" \t()[],-–—")
        card_name = _RARITY_SUFFIX_RE.sub("", before).strip() or None
        return card_name, token, number
    return None, None, None


def extract_card_info(title: str) -> dict[str, str | None | bool]:
    """Extract structured card identity fields from a marketplace listing title.

    Handles common Vinted listing title formats across multiple languages::

        "Carte Pokemon Raikou V SAR 218/172 - S12a VSTAR Universe"
        → card_name="Raikou V", collector_number="218/172",
          set_code="S12a", set_name="VSTAR Universe", card_name_matched=True

        "Pokemon Charizard ex 006/197 Obsidian Flames"
        → card_name="Charizard ex", collector_number="006/197",
          set_code=None, set_name="Obsidian Flames", card_name_matched=True

        "Pikachu (SVP 214)"
        → card_name="Pikachu", collector_number="214",
          set_code="SVP", set_name=None, card_name_matched=True

        "Umbreon TG25/TG30 Silver Tempest"
        → card_name="Umbreon", collector_number="TG25/TG30",
          set_code=None, set_name="Silver Tempest", card_name_matched=True

    Returns a dict with keys ``card_name``, ``collector_number``, ``set_code``,
    ``set_name``, and ``card_name_matched`` (bool); string values are ``None``
    when not found.
    """
    result: dict[str, str | None | bool] = {
        "card_name": None,
        "collector_number": None,
        "set_code": None,
        "set_name": None,
        "card_name_matched": False,
    }

    # Strip language/game prefix.
    text = _LANG_PREFIX_RE.sub("", title).strip()

    # ── 1. Standard collector number: NNN/NNN ────────────────────────────
    num_match = _COLLECTOR_NUMBER_RE.search(text)
    if num_match:
        result["collector_number"] = num_match.group(0)
        before = text[: num_match.start()].strip()
        after = text[num_match.end() :].strip()

        before_clean, set_from_before = _split_set_from_before(before)

        # Strip any leading separator characters left by language-prefix removal
        # (e.g. "- Xatu" → "Xatu" when "Pokémon " was stripped but the dash
        # that followed it was left behind). Also strip trailing separators
        # (e.g. "Jirachi ex -" → "Jirachi ex" when a dash precedes the number).
        before_clean = re.sub(r"^[\s\-–—|:,]+", "", before_clean).rstrip(" \t-–—|:,")

        # Card name = text before number, minus any trailing rarity code.
        raw_name = _strip_rarity_suffixes(before_clean)
        card_name, matched = _match_pokemon_name(raw_name)
        result["card_name"] = card_name
        result["card_name_matched"] = matched

        # Set info = text after number (prefer after-number; fall back to
        # the set fragment extracted from the before text when present).
        after_clean = _strip_after_number_noise(after)
        # Strip close-parenthesis / bracket noise that carries no set info
        # (e.g. when the collector number is wrapped in "(NN/NN)").
        after_useful = re.sub(r"^[\s)\]]+$", "", after_clean).strip()
        set_code, set_name = _parse_set_info(after_useful or set_from_before or "")

        # When no set info could be determined from the after/before fragments,
        # scan the before text for an embedded known set name (e.g.
        # "Stoutland ir white flare 156/086").
        if set_code is None and set_name is None and not after_useful and not set_from_before:
            name_part, set_frag = _split_before_at_known_set(before_clean)
            if set_frag:
                raw_name2 = _strip_rarity_suffixes(re.sub(r"^[\s\-–—|:,]+", "", name_part))
                card_name2, matched2 = _match_pokemon_name(raw_name2)
                result["card_name"] = card_name2
                result["card_name_matched"] = matched2
                set_code, set_name = _parse_set_info(set_frag)

        # Last-resort fallback: when the card name still did not match and there
        # is no set info, check whether the last word of before_clean is a known
        # set code that got stuck in the name (e.g. "Audino JTG 124/159" where
        # "JTG" is a set code, not part of the Pokémon name).
        if not result.get("card_name_matched") and set_code is None and set_name is None:
            words = re.split(r"\s+", before_clean.strip())
            if len(words) >= 2 and words[-1].upper() in KNOWN_SET_CODES:
                trailing_code = words[-1]
                new_before = " ".join(words[:-1])
                raw_name3 = _strip_rarity_suffixes(re.sub(r"^[\s\-–—|:,]+", "", new_before))
                card_name3, matched3 = _match_pokemon_name(raw_name3)
                if matched3:
                    result["card_name"] = card_name3
                    result["card_name_matched"] = True
                    set_code = trailing_code

        # Fallback for the inverted format "<set_name> <number> <card_name> [extra]"
        # where the set name appears BEFORE the collector number and the card name
        # appears AFTER it.  Example: "Ascended heroes 44/217 sneasel ball&normal"
        # Conditions: card name unmatched, no set resolved, after text present, and
        # before text exactly matches a known set name.
        if (
            not result.get("card_name_matched")
            and set_code is None
            and set_name is None
            and after_useful
        ):
            candidate_set = _find_known_set_name(before_clean)
            if candidate_set:
                bc_norm = " ".join(before_clean.lower().split())
                cs_norm = " ".join(candidate_set.lower().split())
                if bc_norm == cs_norm:
                    # before_clean is exactly a known set name; try to extract a
                    # Pokémon name from the beginning of the after-number text.
                    after_words = after_useful.split()
                    for n in range(min(len(after_words), 4), 0, -1):
                        name_candidate = " ".join(after_words[:n])
                        name_result, matched_result = _match_pokemon_name(name_candidate)
                        if matched_result:
                            result["card_name"] = name_result
                            result["card_name_matched"] = True
                            set_name = candidate_set
                            set_code = _SET_NAME_TO_CODE.get(cs_norm)
                            break

        result["set_code"] = set_code
        result["set_name"] = set_name
        return result

    # ── 1.5. Hash-style collector number: "#46", "# 46" ─────────────────────
    hash_match = _HASH_NUMBER_RE.search(text)
    if hash_match:
        bare_num = hash_match.group(1)
        result["collector_number"] = bare_num
        before = text[: hash_match.start()].strip()
        after = text[hash_match.end() :].strip()

        before_clean, set_from_before = _split_set_from_before(before)
        before_clean = re.sub(r"^[\s\-–—|:,]+", "", before_clean).rstrip(" \t-–—|:,")

        raw_name = _strip_rarity_suffixes(before_clean)
        card_name, matched = _match_pokemon_name(raw_name)
        result["card_name"] = card_name
        result["card_name_matched"] = matched

        after_clean = _strip_after_number_noise(after)
        after_useful = re.sub(r"^[\s)\]]+$", "", after_clean).strip()
        set_code, set_name = _parse_set_info(after_useful or set_from_before or "")

        # Same fallback: scan before text for embedded set name.
        if set_code is None and set_name is None and not after_useful and not set_from_before:
            name_part, set_frag = _split_before_at_known_set(before_clean)
            if set_frag:
                raw_name2 = _strip_rarity_suffixes(re.sub(r"^[\s\-–—|:,]+", "", name_part))
                card_name2, matched2 = _match_pokemon_name(raw_name2)
                result["card_name"] = card_name2
                result["card_name_matched"] = matched2
                set_code, set_name = _parse_set_info(set_frag)

        # Last-resort: strip trailing set code from card name when unmatched.
        if not result.get("card_name_matched") and set_code is None and set_name is None:
            words = re.split(r"\s+", before_clean.strip())
            if len(words) >= 2 and words[-1].upper() in KNOWN_SET_CODES:
                trailing_code = words[-1]
                new_before = " ".join(words[:-1])
                raw_name3 = _strip_rarity_suffixes(re.sub(r"^[\s\-–—|:,]+", "", new_before))
                card_name3, matched3 = _match_pokemon_name(raw_name3)
                if matched3:
                    result["card_name"] = card_name3
                    result["card_name_matched"] = True
                    set_code = trailing_code

        # Fallback for the inverted format "<set_name> #<number> <card_name>".
        if (
            not result.get("card_name_matched")
            and set_code is None
            and set_name is None
            and after_useful
        ):
            candidate_set = _find_known_set_name(before_clean)
            if candidate_set:
                bc_norm = " ".join(before_clean.lower().split())
                cs_norm = " ".join(candidate_set.lower().split())
                if bc_norm == cs_norm:
                    after_words = after_useful.split()
                    for n in range(min(len(after_words), 4), 0, -1):
                        name_candidate = " ".join(after_words[:n])
                        name_result, matched_result = _match_pokemon_name(name_candidate)
                        if matched_result:
                            result["card_name"] = name_result
                            result["card_name_matched"] = True
                            set_name = candidate_set
                            set_code = _SET_NAME_TO_CODE.get(cs_norm)
                            break

        result["set_code"] = set_code
        result["set_name"] = set_name
        return result

    # ── 2. Subset slash notation: XX99/XX99 (e.g. SV49/SV94, TG09/TG30) ──
    subset_match = _SUBSET_NUMBER_RE.search(text)
    if subset_match:
        result["collector_number"] = subset_match.group(0)
        before = text[: subset_match.start()].strip()
        after = text[subset_match.end() :].strip()

        before_clean, set_from_before = _split_set_from_before(before)
        raw_name = _strip_rarity_suffixes(before_clean)
        card_name, matched = _match_pokemon_name(raw_name)
        result["card_name"] = card_name
        result["card_name_matched"] = matched

        after_clean = _strip_after_number_noise(after)
        after_useful = re.sub(r"^[\s)\]]+$", "", after_clean).strip()
        set_code, set_name = _parse_set_info(after_useful or set_from_before or "")

        # When no set info could be found from the text following the subset
        # number, try to infer the set from the subset prefix (e.g. "GG" → Crown
        # Zenith).  The prefix is the leading letter run of group(1).
        if set_code is None and set_name is None:
            prefix_m = re.match(r"[A-Z]+", subset_match.group(1))
            if prefix_m:
                inferred = _SUBSET_PREFIX_TO_SET.get(prefix_m.group(0).upper())
                if inferred:
                    set_code, set_name = inferred

        result["set_code"] = set_code
        result["set_name"] = set_name
        return result

    # ── 3. Promo-style collector number (e.g. "SVP 214", "SVP214") ────────
    promo_name, promo_set_code, promo_number = _extract_promo_number(text)
    if promo_set_code and promo_number:
        card_name, matched = _match_pokemon_name(promo_name or "")
        result["card_name"] = card_name
        result["card_name_matched"] = matched
        result["set_code"] = promo_set_code
        result["collector_number"] = promo_number
        return result

    # ── 4. No collector number – best-effort card name + set info ────────
    raw_name = _strip_rarity_suffixes(text)

    # Try to split the title at the first "break point" — a grade certifier
    # (e.g. "CGC 9") or a standalone calendar year (e.g. "2012") — so that
    # the card name and any set info can be extracted from the noise-free
    # portions of the title.
    bp_grade = _GRADE_BREAK_RE.search(raw_name)
    bp_year = _YEAR_BREAK_RE.search(raw_name)
    bp_candidates = [m for m in (bp_grade, bp_year) if m is not None]

    if bp_candidates:
        break_match = min(bp_candidates, key=lambda m: m.start())
        candidate = raw_name[: break_match.start()].strip()
        remainder = raw_name[break_match.end() :].strip()

        if candidate and _verify_pokemon_in_text(candidate):
            result["card_name"] = candidate
            result["card_name_matched"] = True
            # Strip any leading year from the remainder, then try set info.
            if remainder:
                remainder_clean = re.sub(r"\s+", " ", _YEAR_BREAK_RE.sub("", remainder, count=1)).strip()
                if remainder_clean:
                    set_code, set_name = _parse_set_info(remainder_clean)
                    result["set_code"] = set_code
                    result["set_name"] = set_name
            return result

    # Fallback: no break point found, or no Pokémon name in the candidate.
    card_name, matched = _match_pokemon_name(raw_name)
    result["card_name"] = card_name
    result["card_name_matched"] = matched

    # When no collector number was found and the card name wasn't matched,
    # try to split the text at a known set name so that e.g.
    # "Charizard ex Obsidian Flames" → card="Charizard ex", set="Obsidian Flames".
    if not matched:
        name_part, set_frag = _split_before_at_known_set(raw_name)
        if set_frag and name_part:
            card_name2, matched2 = _match_pokemon_name(name_part.strip())
            if matched2:
                result["card_name"] = card_name2
                result["card_name_matched"] = True
                set_code, set_name = _parse_set_info(set_frag)
                result["set_code"] = set_code
                result["set_name"] = set_name

    # Last-resort: if the title ends with a bare integer (e.g. "Great Tusk EX 246"),
    # try treating it as the collector number and re-match the card name from the
    # remainder. Skips numbers that look like calendar years (1900–2099).
    if not result.get("card_name_matched") and result.get("collector_number") is None:
        bare_tail = re.search(r"\s+(\d{1,4})\s*$", raw_name)
        if bare_tail:
            num_candidate = bare_tail.group(1)
            if not re.match(r"^(?:19|20)\d{2}$", num_candidate):
                name_candidate = raw_name[: bare_tail.start()].strip()
                card_name3, matched3 = _match_pokemon_name(name_candidate)
                if matched3:
                    result["card_name"] = card_name3
                    result["card_name_matched"] = True
                    result["collector_number"] = num_candidate

    return result


def _parse_set_info(text: str) -> tuple[str | None, str | None]:
    """Return *(set_code, set_name)* from the text following a collector number.

    Validates any candidate set code against the known set-code list.  Unknown
    codes are not stored as the set code; the full text is treated as the set
    name instead, so the listing goes to the review queue rather than receiving
    a fabricated set code.
    """
    text = text.strip()
    if not text:
        return None, None

    # Check full text against known set names FIRST so that multi-word names
    # that start with a token that is also a set code (e.g. "EX Crystal Guardians"
    # where "EX" is both a known set code and part of the EX-era set name) are
    # resolved correctly. Only use _find_known_set_name (prefix match) here,
    # not _search_known_set_name, so we don't swallow "OBF Obsidian Flames"
    # (where "obsidian flames" is found as a substring but "OBF" is the set code).
    quick_known = _find_known_set_name(text)
    if quick_known:
        return None, quick_known

    # If the text is entirely composed of rarity/variant tokens (e.g.
    # "Rare Holo", "Holo", "Ultra Rare"), it is not a set name — discard it.
    # We check by stripping all rarity suffixes and seeing if nothing remains.
    rarity_stripped = _strip_rarity_suffixes(" " + text).strip()
    if not rarity_stripped:
        return None, None

    # If the text starts with an English condition full word/phrase (e.g.
    # "Mint Obsidian Flames", "Near Mint Obsidian Flames"), strip the condition
    # token and treat the remainder as the set name.
    cm = _CONDITION_FULL_PREFIX_RE.match(text)
    if cm:
        remainder = text[cm.end():].strip()
        known = (_find_known_set_name(remainder) or _search_known_set_name(remainder)) if remainder else None
        return None, known or None

    # Try "set_code<space>set_name" pattern.
    m = _SET_CODE_RE.match(text)
    if m:
        candidate = m.group(1)
        if candidate.upper() in KNOWN_SET_CODES:
            set_name = m.group(2).strip() or None
            # If no name text follows the code, look it up from the mapping.
            if set_name is None:
                set_name = SET_CODE_TO_SET_NAME.get(candidate.upper())
            return candidate, set_name
        # If the leading token is a condition abbreviation (e.g. "NM"), strip it
        # and treat the remainder as the set name.
        if _CONDITION_ABBREV_PREFIX_RE.match(candidate):
            remainder = m.group(2).strip()
            known = _find_known_set_name(remainder) or _search_known_set_name(remainder)
            return None, known or None
        # Not a known set code – treat the whole text as the set name, but only
        # when it matches a known set.
        raw = re.sub(r"^[\s\-–—]+", "", text).strip()
        known = _find_known_set_name(raw) or _search_known_set_name(raw)
        return None, known or None

    # Try set code only (no set name after it).
    m = _SET_CODE_ONLY_RE.match(text)
    if m:
        stripped = text.lstrip(" \t-–—").strip()
        if stripped.upper() in KNOWN_SET_CODES:
            # Look up the set name from the mapping so the name field is always
            # populated when a known set code is recognised.
            set_name = SET_CODE_TO_SET_NAME.get(stripped.upper())
            return stripped or None, set_name
        # A standalone condition abbreviation yields no set info.
        if _CONDITION_ABBREV_PREFIX_RE.match(stripped):
            return None, None
        # Not a known set code – only return it when it's a known set name.
        known = _find_known_set_name(stripped) or _search_known_set_name(stripped)
        return None, known or None

    # No set code – strip any leading separator and treat remainder as set name,
    # but only when it is a recognised set name.
    raw = re.sub(r"^[\s\-–—]+", "", text).strip()
    known = _find_known_set_name(raw) or _search_known_set_name(raw)
    return None, known or None


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

class CardAnalyzer:
    """Analyses a listing for card-related properties and populates fields.

    Call :meth:`analyze` to populate ``listing.is_bulk_lot``,
    ``listing.estimated_card_count``, ``listing.price_per_card``,
    ``listing.confidence``, and ``listing.valuation_explanation`` in-place.

    This class is stateless; create a single instance and reuse it.
    """

    def analyze(self, listing: "Listing") -> None:
        """Populate card-analysis fields on *listing* in-place.

        This method is safe to call on any listing; it does nothing harmful
        if the listing is not card-related.
        """
        combined_text = _combined_text(listing)

        # Extract structured card identity from the listing title.
        card_info = extract_card_info(listing.title)
        listing.extracted_card_name = card_info["card_name"]
        listing.extracted_collector_number = card_info["collector_number"]
        listing.extracted_set_code = card_info["set_code"]
        listing.extracted_set_name = card_info["set_name"]
        if listing.extracted_card_name:
            logger.debug(
                "Listing %s card info: name=%r num=%r set_code=%r set_name=%r",
                listing.listing_id,
                listing.extracted_card_name,
                listing.extracted_collector_number,
                listing.extracted_set_code,
                listing.extracted_set_name,
            )

        # Determine whether the listing is bulk.
        is_bulk = self._detect_bulk_lot(combined_text)
        listing.is_bulk_lot = is_bulk

        if is_bulk:
            count, count_source = self._estimate_card_count(combined_text)
            listing.estimated_card_count = count
            if count is not None and count > 0:
                listing.price_per_card = round(listing.price / count, 4)
            else:
                listing.price_per_card = None

            listing.confidence, listing.valuation_explanation = (
                self._bulk_confidence(count, count_source)
            )
            logger.debug(
                "Listing %s bulk lot: count=%s ppc=%.4f conf=%s",
                listing.listing_id,
                count,
                listing.price_per_card or 0.0,
                listing.confidence,
            )
        else:
            # Individual card – confidence depends on whether a specific card
            # can be identified. Market valuation is handled externally by
            # price_lookup; here we only set a preliminary confidence.
            listing.estimated_card_count = None
            listing.price_per_card = None
            listing.confidence, listing.valuation_explanation = (
                self._individual_confidence(combined_text, listing.title)
            )
            logger.debug(
                "Listing %s individual card: conf=%s",
                listing.listing_id,
                listing.confidence,
            )

    # ------------------------------------------------------------------
    # Bulk lot detection
    # ------------------------------------------------------------------

    def _detect_bulk_lot(self, text: str) -> bool:
        """Return True when the text indicates a bulk lot."""
        text_lower = text.lower()
        for kw in _BULK_KEYWORDS:
            if kw in text_lower:
                return True
        # Also treat anything with a large explicit card count as a bulk lot.
        count, _ = self._estimate_card_count(text)
        if count is not None and count >= 20:
            return True
        return False

    # ------------------------------------------------------------------
    # Card count estimation
    # ------------------------------------------------------------------

    def _estimate_card_count(
        self, text: str
    ) -> tuple[int | None, str]:
        """Return ``(estimated_count, source_description)``.

        Source description is one of:
        ``"explicit"``, ``"weight"``, or ``"unknown"``.
        """
        # 1. Explicit count patterns.
        count = self._parse_explicit_count(text)
        if count is not None:
            return count, "explicit"

        # 2. Weight-based estimation.
        weight_g = self._parse_weight_grams(text)
        if weight_g is not None:
            estimated = int(weight_g / _GRAMS_PER_CARD)
            return estimated, "weight"

        return None, "unknown"

    def _parse_explicit_count(self, text: str) -> int | None:
        """Extract the largest explicit card count from *text*."""
        counts: list[int] = []
        for pattern in _COUNT_PATTERNS:
            for m in pattern.finditer(text):
                try:
                    counts.append(int(m.group(1)))
                except (ValueError, IndexError):
                    pass
        return max(counts) if counts else None

    def _parse_weight_grams(self, text: str) -> float | None:
        """Extract a weight in grams from *text*, converting kg if necessary."""
        for pattern in _WEIGHT_PATTERNS:
            m = pattern.search(text)
            if m:
                raw = m.group(1).replace(",", ".")
                try:
                    value = float(raw)
                    # Distinguish grams vs kilograms by pattern index.
                    if "kg" in m.group(0).lower():
                        return value * 1000
                    return value
                except ValueError:
                    pass
        return None

    # ------------------------------------------------------------------
    # Confidence assignment
    # ------------------------------------------------------------------

    def _bulk_confidence(
        self, count: int | None, count_source: str
    ) -> tuple[str, str]:
        """Return (confidence_label, explanation) for a bulk listing."""
        if count is None:
            return (
                "Low",
                "Bulk lot detected but card count could not be determined. "
                "Price per card is unavailable.",
            )
        if count_source == "explicit":
            return (
                "High",
                f"Card count of {count} was explicitly stated in the listing. "
                f"Price per card calculated from listing price ÷ card count.",
            )
        if count_source == "weight":
            return (
                "Medium",
                f"Card count estimated from weight at {_GRAMS_PER_CARD} g/card "
                f"→ ~{count} cards. Price per card is an approximation.",
            )
        return (
            "Low",
            f"Card count of ~{count} was estimated heuristically. "
            "Price per card is approximate.",
        )

    def _individual_confidence(
        self, combined_text: str, title: str
    ) -> tuple[str, str]:
        """Return (confidence_label, explanation) for an individual card.

        The final confidence will be raised to High or kept at Medium/Low once
        live market data is (or isn't) found by the caller.  We only set an
        initial level here based on whether a specific card can be identified.
        """
        text_lower = combined_text.lower()

        # Check whether any strong card keywords are present.
        is_card = any(kw in text_lower for kw in _CARD_KEYWORDS)
        if not is_card:
            return (
                "Low",
                "Listing does not appear to contain trading cards based on "
                "title and description text.",
            )

        # Check whether a specific named card can be identified in the title.
        # Heuristic: title contains a recognisable card name indicator.
        specific_indicators = (
            "psa",
            "bgs",
            "cgc",
            "graded",
            "1st edition",
            "first edition",
            "holo",
            "ex ",
            " ex ",
            "/",   # set notation like "001/165"
        )
        title_lower = title.lower()
        has_specific = any(ind in title_lower for ind in specific_indicators)
        if has_specific:
            return (
                "Medium",
                "Specific card indicators found in title. "
                "Confidence will rise to High if a live market price is matched.",
            )

        return (
            "Medium",
            "Trading card keywords found in the listing. "
            "Confidence will rise to High if a live market price is matched.",
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _combined_text(listing: "Listing") -> str:
    """Return title + description as a single lower-cased string."""
    parts = [listing.title or ""]
    if listing.description:
        parts.append(listing.description)
    return " ".join(parts)
