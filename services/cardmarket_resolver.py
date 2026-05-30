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

import httpx
from rapidfuzz import fuzz, process

from scraper.cardmarket import build_cardmarket_url, normalize_cardmarket_url
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
    set_code: str | None = None
    prefix_rule_id: int | None = None


class CardmarketResolver:
    """Resolves Vinted card fingerprints into Cardmarket product URLs."""

    def __init__(self, db: "Database") -> None:
        self._db = db
        # In-memory cache of all mappings; refreshed on startup and after writes.
        self._mappings: list[dict[str, Any]] = []
        # In-memory cache of slug prefix rules keyed by set_code (upper-case).
        self._prefix_rules: dict[str, list[dict[str, Any]]] = {}

    async def load(self) -> None:
        """Load all card mappings and prefix rules from the database into memory."""
        self._mappings = await self._db.get_all_mappings()
        await self._load_prefix_rules()
        logger.info(
            "CardmarketResolver: loaded %d mappings, %d prefix rules",
            len(self._mappings), len(self._prefix_rules),
        )

    async def _load_prefix_rules(self) -> None:
        """Reload the in-memory prefix-rule cache from the database."""
        rules = await self._db.get_all_slug_prefix_rules()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for rule in rules:
            if rule.get("confidence", 0.0) < 0.5:
                continue
            sc = (rule.get("set_code") or "").upper()
            if not sc:
                continue
            grouped.setdefault(sc, []).append(rule)
        for sc, rule_list in grouped.items():
            rule_list.sort(
                key=lambda r: (float(r.get("uses_successful") or 0), float(r.get("confidence") or 0)),
                reverse=True,
            )
        self._prefix_rules = grouped

    async def reload(self) -> None:
        """Reload mappings (call after adding a new mapping)."""
        await self.load()

    # ------------------------------------------------------------------
    # Primary resolution
    # ------------------------------------------------------------------

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

        # ── 3: Direct URL construction + URL existence validation ─────────
        candidates = self._construct_url_candidates(fingerprint)
        for candidate in candidates:
            if await self._url_resolves(candidate.url):
                if candidate.prefix_rule_id and candidate.set_code:
                    await self._db.record_slug_prefix_rule_use(
                        candidate.prefix_rule_id, success=True
                    )
                    await self._load_prefix_rules()
                return candidate
            if candidate.prefix_rule_id and candidate.set_code:
                await self._db.record_slug_prefix_rule_use(
                    candidate.prefix_rule_id, success=False
                )
                await self._load_prefix_rules()

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

    def _construct_url_candidates(self, fingerprint: CardFingerprint) -> list[ResolvedUrl]:
        """Build and rank possible Cardmarket URLs from the fingerprint.

        Candidate URLs are ordered by learned-rule confidence and success count.
        """
        card_name = fingerprint.card_name
        set_code = fingerprint.set_code
        collector_number = fingerprint.collector_number

        if not card_name:
            return []

        candidate_set_codes: list[str] = []
        if set_code:
            candidate_set_codes.append(set_code)
        if fingerprint.set_name:
            derived_code = set_name_to_code(fingerprint.set_name)
            if derived_code and derived_code not in candidate_set_codes:
                candidate_set_codes.append(derived_code)

        if not candidate_set_codes:
            return []

        candidates: list[ResolvedUrl] = []
        seen_urls: set[str] = set()
        for idx, candidate_code in enumerate(candidate_set_codes):
            rules = self._prefix_rules.get(candidate_code.upper(), [])
            ordered_prefixes: list[tuple[str | None, int | None, float]] = [
                (r.get("prefix"), r.get("id"), 0.75 + min(float(r.get("uses_successful") or 0) * 0.01, 0.15))
                for r in rules
                if r.get("prefix")
            ]
            ordered_prefixes.append((None, None, 0.60 if idx == 0 else 0.50))
            for prefix, rule_id, confidence in ordered_prefixes:
                url = build_cardmarket_url(
                    card_name,
                    candidate_code,
                    collector_number or "",
                    promo=fingerprint.is_promo,
                    number_prefix=prefix,
                    language=fingerprint.language,
                    reverse_holo=fingerprint.is_reverse_holo,
                )
                if not url:
                    continue
                url = normalize_cardmarket_url(
                    url,
                    language=fingerprint.language,
                    reverse_holo=fingerprint.is_reverse_holo,
                )
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                candidates.append(
                    ResolvedUrl(
                        url=url,
                        source="constructed",
                        confidence=confidence,
                        product_name=None,
                        set_code=candidate_code,
                        prefix_rule_id=rule_id,
                    )
                )

        for candidate in candidates:
            logger.info("CardmarketResolver: constructed URL candidate: %s", candidate.url)
        return candidates

    async def _url_resolves(self, url: str) -> bool:
        """Return True when a Cardmarket product URL resolves successfully."""
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as client:
                head_resp = await client.head(url)
                if head_resp.status_code < 400:
                    return True
                if head_resp.status_code == 404:
                    return False
                get_resp = await client.get(url)
                return get_resp.status_code < 400
        except Exception:  # noqa: BLE001
            return False

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
    "evolutions a paldee": "EV",
    "fable nebuleuse": "EV35",
    "faille paradoxe": "EV4",
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
    "heartgold soulsilver": "HGSS",
    "stormfront": "SF",
    "supreme victors": "SV",
    "platinum": "PL",
    "mcdonald": "MCD",
    "adv promos": "ADVP",
}


def set_name_to_code(set_name: str) -> str | None:
    """Try to derive a set code from a set name."""
    name_lower = set_name.lower().strip()
    for pattern, code in _SET_NAME_TO_CODE.items():
        if pattern in name_lower or name_lower in pattern:
            return code
    return None
