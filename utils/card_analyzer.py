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
    _POKEMON_NAME_MAP,
)

if TYPE_CHECKING:
    from scraper.base import Listing

logger = get_logger(__name__)

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
    "classeur",
    "binder",
    "etb",
    "elite trainer box",
    "booster box",
    "figure",
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
)

def is_non_card_item(title: str, description: str | None = None) -> bool:
    """Return True when *title* (or *description*) clearly indicates a non-TCG item.

    Checks against :data:`NON_CARD_KEYWORDS`.  ``toploader`` and ``sleeve`` are
    deliberately excluded so that card+accessory combo listings are not filtered.
    """
    combined = (title + " " + (description or "")).lower()
    return any(kw in combined for kw in NON_CARD_KEYWORDS)


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

# Promo-style collector number: e.g. "SVP 214", "SVP214", "SWSHP 088", "XYP 145"
# Pattern: 2–6 uppercase letters (set code, typically ending in P for promos)
# followed by optional whitespace and 1–4 digits.
# Must be surrounded by word boundaries / non-alphanumeric chars to avoid false positives.
_PROMO_NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9])([A-Z]{2,6}P?)\s*(\d{1,4})(?![0-9A-Za-z/])"
)

# Known rarity/variant codes that appear *between* the card name and number.
# These are stripped so the card name is clean.
_RARITY_SUFFIX_RE = re.compile(
    r"\s+\b(?:SAR|CHR|CSR|SSR|UR|IR|HR|RR|AR|PROMO|SR|PR)\b\s*$",
    re.IGNORECASE,
)

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

        # Card name = text before number, minus any trailing rarity code.
        raw_name = _RARITY_SUFFIX_RE.sub("", before).strip()
        card_name, matched = _match_pokemon_name(raw_name)
        result["card_name"] = card_name
        result["card_name_matched"] = matched

        # Set info = text after number.
        set_code, set_name = _parse_set_info(after)
        result["set_code"] = set_code
        result["set_name"] = set_name
        return result

    # ── 2. Subset slash notation: XX99/XX99 (e.g. SV49/SV94, TG09/TG30) ──
    subset_match = _SUBSET_NUMBER_RE.search(text)
    if subset_match:
        result["collector_number"] = subset_match.group(0)
        before = text[: subset_match.start()].strip()
        after = text[subset_match.end() :].strip()

        raw_name = _RARITY_SUFFIX_RE.sub("", before).strip()
        card_name, matched = _match_pokemon_name(raw_name)
        result["card_name"] = card_name
        result["card_name_matched"] = matched

        set_code, set_name = _parse_set_info(after)
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

    # ── 4. No collector number – best-effort card name only ───────────────
    raw_name = _RARITY_SUFFIX_RE.sub("", text).strip()
    card_name, matched = _match_pokemon_name(raw_name)
    result["card_name"] = card_name
    result["card_name_matched"] = matched
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

    # Try "set_code<space>set_name" pattern.
    m = _SET_CODE_RE.match(text)
    if m:
        candidate = m.group(1)
        if candidate.upper() in KNOWN_SET_CODES:
            return candidate, m.group(2).strip() or None
        # Not a known set code – treat the whole text as the set name.
        set_name = re.sub(r"^[\s\-–—]+", "", text).strip()
        return None, set_name or None

    # Try set code only (no set name after it).
    m = _SET_CODE_ONLY_RE.match(text)
    if m:
        stripped = text.lstrip(" \t-–—").strip()
        if stripped.upper() in KNOWN_SET_CODES:
            return stripped or None, None
        # Not a known set code – treat it as a set name.
        return None, stripped or None

    # No set code – strip any leading separator and treat remainder as set name.
    set_name = re.sub(r"^[\s\-–—]+", "", text).strip()
    return None, set_name or None


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
