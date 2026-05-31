"""
services/cardmarket_resolver.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Cardmarket Resolution Service.

Resolves a ``CardFingerprint`` into a specific Cardmarket product URL using:

1. **Learning database lookup** (highest priority)
   – Fuzzy-matches the new listing title / fingerprint against previously
     validated mappings using ``rapidfuzz``.
   – Reuses the stored URL when confidence is above the configured threshold.

2. **Direct URL construction** from the fingerprint
   – Uses the set-code → slug mapping from ``scraper.cardmarket`` to build the
     product URL programmatically.
   – Marks the result as ``auto`` with ``confidence < 1.0``.

3. **Review queue fallback**
   – When neither strategy produces a URL, ``resolve()`` returns ``None``
     and the caller is expected to send the listing to the review queue.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from rapidfuzz import fuzz, process

from config.settings import settings
from scraper.cardmarket import (
    _LANGUAGE_TO_CM_CODE,
    build_cardmarket_url,
    normalize_cardmarket_url,
    register_variant_hint,
)
from services.card_identifier import CardFingerprint
from utils.logger import get_logger

if TYPE_CHECKING:
    from database.db import Database

logger = get_logger(__name__)

# Minimum fuzzy-match score (0–100) to consider a stored mapping a match.
_FUZZY_THRESHOLD = 80.0
# Minimum confidence to skip the review queue.
_CONFIDENCE_THRESHOLD = 0.70


@dataclass
class ResolvedUrl:
    """Result of a Cardmarket URL resolution attempt."""

    url: str                          # Always normalised with sellerCountry=23&language=1
    source: str                       # 'database' | 'constructed' | 'manual'
    confidence: float                 # 0.0 – 1.0
    mapping_id: int | None = None     # DB row ID when source='database'
    product_name: str | None = None   # From stored mapping, if available
    needs_validation: bool = False    # True when URL was constructed (not from DB) and should be HEAD-checked


class CardmarketResolver:
    """Resolves Vinted card fingerprints into Cardmarket product URLs."""

    def __init__(self, db: "Database") -> None:
        self._db = db
        # In-memory cache of all mappings; refreshed on startup and after writes.
        self._mappings: list[dict[str, Any]] = []
        # In-memory cache of slug prefix rules keyed by set_code (upper-case).
        self._prefix_rules: dict[str, dict[str, Any]] = {}
        # In-memory cache of slug overrides keyed by fingerprint hash.
        self._slug_overrides: dict[str, dict[str, Any]] = {}

    async def load(self) -> None:
        """Load all card mappings, prefix rules, and slug overrides from the database."""
        self._mappings = await self._db.get_all_mappings()
        await self._load_prefix_rules()
        await self._load_slug_overrides()
        logger.info(
            "CardmarketResolver: loaded %d mappings, %d prefix rules, %d slug overrides",
            len(self._mappings), len(self._prefix_rules), len(self._slug_overrides),
        )

    async def _load_prefix_rules(self) -> None:
        """Reload the in-memory prefix-rule cache from the database.

        SQLite returns rows ordered by ``confidence DESC, uses_successful DESC``, so the
        first row per set_code is the best-scoring one.  We keep only the first entry
        per set_code to avoid lower-scored duplicates overwriting a better rule.
        """
        rules = await self._db.get_all_slug_prefix_rules()
        new_rules: dict[str, dict[str, Any]] = {}
        for r in rules:
            sc = r["set_code"].upper()
            if sc not in new_rules:  # Keep the first (highest-scored) entry only.
                new_rules[sc] = r
        self._prefix_rules = new_rules

    async def _load_slug_overrides(self) -> None:
        """Load all slug overrides from the database into memory."""
        rows = await self._db.get_all_slug_overrides() if hasattr(self._db, "get_all_slug_overrides") else []
        self._slug_overrides = {r["fingerprint"]: r for r in rows}

    async def reload(self) -> None:
        """Reload mappings (call after adding a new mapping)."""
        await self.load()

    async def store_slug_override(
        self,
        fingerprint: CardFingerprint,
        preferred_slug: str,
        cardmarket_url: str,
    ) -> None:
        """Persist a slug override learned from a correction and update the in-memory cache.

        Also registers a variant hint so build_cardmarket_url() will produce the
        correct slug for future listings with the same card/set/number combination.
        """
        fp_hash = fingerprint.fingerprint_hash()
        if not fp_hash:
            return
        await self._db.add_slug_override(
            fingerprint=fp_hash,
            preferred_slug=preferred_slug,
            cardmarket_url=cardmarket_url,
        )
        # Update in-memory cache immediately.
        self._slug_overrides[fp_hash] = {
            "fingerprint": fp_hash,
            "preferred_slug": preferred_slug,
            "cardmarket_url": cardmarket_url,
        }
        # Also register a variant hint for build_cardmarket_url() so the same
        # card gets the right slug even when constructing from first principles.
        if fingerprint.card_name and fingerprint.set_code and fingerprint.collector_number:
            register_variant_hint(
                fingerprint.card_name,
                fingerprint.set_code,
                fingerprint.collector_number,
                preferred_slug,
            )
        logger.info(
            "CardmarketResolver: stored slug override for fingerprint %s → %s",
            fp_hash, cardmarket_url,
        )

    async def resolve(
        self,
        fingerprint: CardFingerprint,
        raw_title: str,
    ) -> ResolvedUrl | None:
        """Try to resolve *fingerprint* to a Cardmarket URL.

        Returns a ``ResolvedUrl`` when resolution succeeds, or ``None`` when
        the listing should go to the review queue.

        Resolution order:
        1. Exact title match in DB
        2. Fuzzy title match in DB
        3. Fingerprint match in DB (card_name + set_code + collector_number)
        4. Direct URL construction from fingerprint
        """
        # ── 1 & 2: DB lookup ─────────────────────────────────────────────
        db_result = self._lookup_in_db(fingerprint, raw_title)
        if db_result:
            return db_result

        # ── 3: Direct URL construction ────────────────────────────────────
        constructed = self._construct_url(fingerprint)
        if constructed:
            return constructed

        # ── 4: No resolution found → review queue ────────────────────────
        logger.debug(
            "CardmarketResolver: no resolution for '%s'", raw_title[:60]
        )
        return None

    # ------------------------------------------------------------------
    # Learning database lookup
    # ------------------------------------------------------------------

    def _lookup_in_db(
        self,
        fingerprint: CardFingerprint,
        raw_title: str,
    ) -> ResolvedUrl | None:
        """Search in-memory mappings for the best match."""
        if not self._mappings:
            return None

        title_lower = raw_title.lower().strip()

        best_score = 0.0
        best_mapping: dict[str, Any] | None = None

        for mapping in self._mappings:
            score = self._score_mapping(mapping, fingerprint, title_lower)
            if score > best_score:
                best_score = score
                best_mapping = mapping

        if best_mapping is None or best_score < _FUZZY_THRESHOLD:
            return None

        confidence = best_score / 100.0
        cm_url = normalize_cardmarket_url(best_mapping["cardmarket_url"])
        logger.info(
            "CardmarketResolver: DB match (score=%.1f) for '%s' → %s",
            best_score, raw_title[:60], cm_url,
        )
        return ResolvedUrl(
            url=cm_url,
            source="database",
            confidence=confidence,
            mapping_id=best_mapping.get("id"),
            product_name=best_mapping.get("cardmarket_product_name"),
        )

    def _score_mapping(
        self,
        mapping: dict[str, Any],
        fingerprint: CardFingerprint,
        title_lower: str,
    ) -> float:
        """Return a composite similarity score (0–100) for a DB mapping vs the new listing."""
        scores: list[float] = []

        # Title similarity
        stored_title = (mapping.get("vinted_title") or "").lower().strip()
        if stored_title:
            scores.append(fuzz.token_sort_ratio(title_lower, stored_title))

        # Fingerprint hash match (exact = 100)
        stored_fp = mapping.get("fingerprint") or ""
        new_fp = fingerprint.fingerprint_hash()
        if stored_fp and new_fp and stored_fp == new_fp:
            return 100.0  # Exact fingerprint match

        # Card name match
        stored_name = (mapping.get("card_name") or "").lower()
        new_name = (fingerprint.card_name or "").lower()
        if stored_name and new_name:
            scores.append(fuzz.ratio(stored_name, new_name) * 0.8)

        # Collector number match (exact = 100 weight)
        stored_num = (mapping.get("collector_number") or "").strip()
        new_num = (fingerprint.collector_number or "").strip()
        if stored_num and new_num:
            if stored_num == new_num:
                scores.append(100.0)
            else:
                scores.append(0.0)

        # Set code match (exact = 100 weight)
        stored_set = (mapping.get("set_code") or "").upper()
        new_set = (fingerprint.set_code or "").upper()
        if stored_set and new_set:
            if stored_set == new_set:
                scores.append(100.0)
            else:
                scores.append(0.0)

        # Token overlap
        stored_tokens_raw = mapping.get("tokens") or "[]"
        try:
            stored_tokens: list[str] = json.loads(stored_tokens_raw)
        except (ValueError, TypeError):
            stored_tokens = []
        new_tokens = fingerprint.token_set()
        if stored_tokens and new_tokens:
            overlap = len(set(stored_tokens) & set(new_tokens))
            total = max(len(stored_tokens), len(new_tokens))
            scores.append((overlap / total) * 100 if total > 0 else 0.0)

        if not scores:
            return 0.0

        # Weighted mean with higher weight for the first score (title)
        if len(scores) == 1:
            return scores[0]
        weights = [1.5] + [1.0] * (len(scores) - 1)
        total_weight = sum(weights)
        return sum(s * w for s, w in zip(scores, weights)) / total_weight

    # ------------------------------------------------------------------
    # Direct URL construction
    # ------------------------------------------------------------------

    def _construct_url(self, fingerprint: CardFingerprint) -> ResolvedUrl | None:
        """Try to build a Cardmarket URL from the fingerprint.

        Resolution order within construction:
        1. Slug override from the learning database (highest confidence).
        2. Direct URL build using set code or derived set code.

        Returns a ``ResolvedUrl`` with ``needs_validation=True`` for
        constructed URLs (not from DB overrides) so the caller can optionally
        perform a HEAD-request check.  Returns ``None`` when insufficient data
        is available.
        """
        card_name = fingerprint.card_name
        set_code = fingerprint.set_code
        collector_number = fingerprint.collector_number

        if not card_name:
            return None

        # ── 0: Slug override from correction learning ─────────────────────
        fp_hash = fingerprint.fingerprint_hash()
        if fp_hash and fp_hash in self._slug_overrides:
            override = self._slug_overrides[fp_hash]
            url = override["cardmarket_url"]
            logger.info(
                "CardmarketResolver: slug override match for fingerprint %s → %s",
                fp_hash, url,
            )
            return ResolvedUrl(
                url=url,
                source="constructed",
                confidence=0.95,
                product_name=override.get("preferred_slug"),
                needs_validation=False,
            )

        # Determine language code override (non-English, non-Dutch cards).
        lang_code: str | None = None
        if fingerprint.language:
            lc = _LANGUAGE_TO_CM_CODE.get(fingerprint.language)
            # Only override when it differs from the defaults (1=English, 11=Dutch).
            if lc and lc not in ("1", "11"):
                lang_code = lc

        # Determine minCondition filter (omit for Poor=7 or unknown).
        min_condition: int | None = None
        if fingerprint.condition_code is not None and 1 <= fingerprint.condition_code <= 6:
            min_condition = fingerprint.condition_code

        # Helper: look up a learned prefix for a given set_code.
        def _get_prefix(sc: str) -> str | None:
            rule = self._prefix_rules.get(sc.upper())
            if rule:
                confidence = float(rule.get("confidence", 0.0) or 0.0)
                min_conf = float(getattr(settings, "slug_confidence_threshold", 0.6))
                if confidence < min_conf:
                    logger.info(
                        "CardmarketResolver: skipping low-confidence prefix rule for %s "
                        "(confidence=%.2f < %.2f)",
                        sc,
                        confidence,
                        min_conf,
                    )
                    return None
                return rule.get("prefix")
            return None

        def _build(sc: str, prefix: str | None) -> str | None:
            """Build and normalise the Cardmarket URL for this card."""
            url = build_cardmarket_url(
                card_name,
                sc,
                collector_number or "",
                promo=fingerprint.is_promo,
                number_prefix=prefix,
            )
            if url is None:
                return None
            return normalize_cardmarket_url(
                url,
                language=lang_code,
                is_reverse_holo=bool(fingerprint.is_reverse_holo),
                min_condition=min_condition,
            )

        # ── 1: Try with explicit set code ─────────────────────────────────
        if set_code:
            prefix = _get_prefix(set_code)
            url = _build(set_code, prefix)
            if url:
                conf = 0.75 if prefix else 0.60
                logger.info(
                    "CardmarketResolver: constructed URL for '%s %s'%s: %s",
                    card_name, set_code,
                    f" (prefix={prefix!r})" if prefix else "",
                    url,
                )
                return ResolvedUrl(
                    url=url,
                    source="constructed",
                    confidence=conf,
                    product_name=None,
                    needs_validation=True,
                )

        # ── 2: Derive set code from set name ──────────────────────────────
        if fingerprint.set_name:
            derived_code = set_name_to_code(fingerprint.set_name)
            if derived_code:
                prefix = _get_prefix(derived_code)
                url = _build(derived_code, prefix)
                if url:
                    conf = 0.65 if prefix else 0.50
                    logger.info(
                        "CardmarketResolver: constructed URL via set-name '%s'%s: %s",
                        fingerprint.set_name,
                        f" (prefix={prefix!r})" if prefix else "",
                        url,
                    )
                    return ResolvedUrl(
                        url=url,
                        source="constructed",
                        confidence=conf,
                        product_name=None,
                        needs_validation=True,
                    )

        return None

    # ------------------------------------------------------------------
    # Persist a new validated mapping
    # ------------------------------------------------------------------

    async def store_mapping(
        self,
        fingerprint: CardFingerprint,
        raw_title: str,
        cardmarket_url: str,
        *,
        product_name: str | None = None,
        product_id: str | None = None,
        validated_by: str = "auto",
        confidence: float = 1.0,
        listing_url: str | None = None,
        listing_description: str | None = None,
        seller_name: str | None = None,
        price: float | None = None,
    ) -> int:
        """Store a validated Cardmarket mapping in the database.

        Also refreshes the in-memory mapping cache.
        """
        normalised_url = normalize_cardmarket_url(cardmarket_url)
        mapping_id = await self._db.add_mapping(
            vinted_title=raw_title,
            vinted_url=listing_url,
            vinted_description=listing_description,
            seller_name=seller_name,
            price=price,
            card_name=fingerprint.card_name,
            set_name=fingerprint.set_name,
            set_code=fingerprint.set_code,
            collector_number=fingerprint.collector_number,
            rarity=fingerprint.rarity,
            language=fingerprint.language,
            fingerprint=fingerprint.fingerprint_hash(),
            cardmarket_url=normalised_url,
            cardmarket_product_id=product_id,
            cardmarket_product_name=product_name,
            tokens=fingerprint.token_set(),
            confidence=confidence,
            validated_by=validated_by,
        )
        await self.reload()
        logger.info(
            "CardmarketResolver: stored mapping #%d for '%s' → %s",
            mapping_id, raw_title[:60], normalised_url,
        )
        return mapping_id

    async def store_prefix_rule(
        self,
        *,
        set_code: str,
        prefix: str,
        set_name: str | None = None,
    ) -> int:
        """Persist a learned slug-prefix rule and refresh the in-memory cache.

        Returns the rule row ID.
        """
        rule_id = await self._db.upsert_slug_prefix_rule(
            set_code=set_code,
            prefix=prefix,
            set_name=set_name,
        )
        await self._load_prefix_rules()
        logger.info(
            "CardmarketResolver: stored prefix rule – set_code=%r prefix=%r (id=%d)",
            set_code, prefix, rule_id,
        )
        return rule_id

    async def record_prefix_rule_use(
        self, set_code: str, *, success: bool
    ) -> None:
        """Record whether the prefix rule for *set_code* produced a valid URL."""
        rule = self._prefix_rules.get(set_code.upper())
        if rule:
            await self._db.record_slug_prefix_rule_use(rule["id"], success=success)
            await self._load_prefix_rules()


# ---------------------------------------------------------------------------
# Set-name → set-code helper
# ---------------------------------------------------------------------------

# Reverse mapping: Cardmarket set-name keywords → known set codes
_SET_NAME_TO_CODE: dict[str, str] = {
    "base set": "BS",
    "jungle": "JU",
    "fossil": "FO",
    "team rocket": "TR",
    "neo genesis": "N1",
    "neo discovery": "N2",
    "neo revelation": "N3",
    "neo destiny": "N4",
    "scarlet violet": "SVI",
    "scarlet & violet": "SVI",
    "151": "MEW",
    "paldea evolved": "PAL",
    "obsidian flames": "OBF",
    "paradox rift": "PAR",
    "paldean fates": "PAF",
    "temporal forces": "TEF",
    "twilight masquerade": "TWM",
    "shrouded fable": "SFA",
    "stellar crown": "SCR",
    "surging sparks": "SSP",
    "prismatic evolutions": "PRE",
    "journey together": "JTG",
    "vstar universe": "S12a",
    "crown zenith": "CRZ",
    "silver tempest": "SWSH12",
    "lost origin": "SWSH11",
    "astral radiance": "SWSH10",
    "brilliant stars": "SWSH9",
    "fusion strike": "SWSH8",
    "evolving skies": "SWSH7",
    "chilling reign": "SWSH6",
    "battle styles": "SWSH5",
    "vivid voltage": "SWSH4",
    "darkness ablaze": "SWSH3",
    "rebel clash": "SWSH2",
    "sword & shield": "SWSH1",
    "sword and shield": "SWSH1",
    "shining fates": "SWSH45",
    "hidden fates": "HIF",
    "cosmic eclipse": "CEC",
    "unified minds": "SM11",
    "unbroken bonds": "SM10",
    "ultra prism": "SM5",
    "burning shadows": "SM3",
    "guardians rising": "SM2",
    "sun & moon": "SM1",
    "sun and moon": "SM1",
    "evolutions": "EVO",
    "xy": "XY1",
    "pokemon go": "PGO",
    # French Scarlet & Violet aliases
    "ecarlate et violet": "SVI",
    "écarlate et violet": "SVI",
    "failles paradoxes": "PAR",
    "destinées de paldea": "PAF",
    "forces temporelles": "TEF",
    "mascarade crépusculaire": "TWM",
    # HeartGold & SoulSilver
    "heartgold soulsilver": "HGSS",
    "heart gold soul silver": "HGSS",
    # Platinum era
    "platinum": "PLA",
    "supreme victors": "STS",
    "stormfront": "SF",
    # McDonald's promos
    "mcdonald": "MCD",
    "mcdonalds": "MCD",
    # Jungle / Fossil / Base aliases already present above as BS/JU/FO
    "jungle": "JU",
    "fossil": "FO",
    # GO
    "pokemon go": "GO",
    # Black & White era
    "black white": "BLW",
    "black & white": "BLW",
    "emerging powers": "EPO",
    "noble victories": "NVI",
    "next destinies": "NXD",
    "dark explorers": "DEX",
    "dragons exalted": "DRX",
    "boundaries crossed": "BCR",
    "plasma storm": "PLS",
    "plasma freeze": "PLF",
    "plasma blast": "PLB",
    "legendary treasures": "LTR",
    # Mega Evolution / Japanese Era 2024–2025
    "mega evolution": "MEG",
    "phantasmal flames": "PFL",
    "ascended heroes": "ASC",
    "perfect order": "POR",
    "chaos rising": "CRI",
    "pitch black": "PBL",
}


def set_name_to_code(set_name: str) -> str | None:
    """Try to derive a set code from a set name."""
    name_lower = set_name.lower().strip()
    for pattern, code in _SET_NAME_TO_CODE.items():
        if pattern in name_lower or name_lower in pattern:
            return code
    return None
