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

if TYPE_CHECKING:
    from scraper.base import Listing

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Average weight of a single trading card in grams.
_GRAMS_PER_CARD: float = 1.8

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
