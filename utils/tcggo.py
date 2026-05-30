"""
utils/tcggo.py
~~~~~~~~~~~~~~
TCGGO API client for structured Cardmarket pricing lookups via RapidAPI.

Replaces direct Cardmarket HTML scraping with an API-based approach that
returns structured pricing data (trend, market, low, average, suggested)
without requiring a browser or DOM parsing.

Configuration (environment variables):
    RAPIDAPI_KEY      – RapidAPI subscription key (X-RapidAPI-Key header).
    RAPIDAPI_HOST     – RapidAPI host for TCGGO (X-RapidAPI-Host header).
    TCGGO_API_URL     – Base URL for the TCGGO API endpoint.

The client is stateless – create one ``TcggoClient`` per session and reuse it.
"""

from __future__ import annotations

import asyncio
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from utils.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class TcggoCardResult:
    """All pricing and metadata fields returned from a TCGGO API response.

    Every pricing field is optional because the API may not return all
    values for every card.  Callers must check for ``None`` before using
    a value in calculations.
    """

    # Card identity
    card_name: str = ""
    set_name: str = ""
    set_code: str = ""
    collector_number: str = ""
    language: str = ""
    rarity: str = ""
    tcggo_id: str = ""
    cardmarket_url: str = ""

    # Cardmarket prices (preferred hierarchy: trend → market → avg → low)
    price_trend: float | None = None       # CM Trend price
    market_price: float | None = None      # CM Market price
    avg_price: float | None = None         # CM Average price (often 30-day)
    avg_30_days: float | None = None       # CM 30-day average
    avg_7_days: float | None = None        # CM 7-day average
    avg_1_day: float | None = None         # CM 1-day average
    low_price: float | None = None         # CM From / lowest price
    suggested_price: float | None = None   # Suggested sell price (if provided)

    # Alternative marketplace prices (non-CM)
    alt_prices: dict[str, float] = field(default_factory=dict)

    # Match quality
    confidence: str = "Low"               # "Low" | "Medium" | "High"
    confidence_score: int = 0             # 0–100 internal score before bucketing

    # ---------------------------------------------------------------------------
    # Derived helpers
    # ---------------------------------------------------------------------------

    def cardmarket_values(self) -> list[float]:
        """Return all non-None Cardmarket price values in hierarchy order."""
        candidates = [
            self.price_trend,
            self.market_price,
            self.avg_30_days,
            self.avg_price,
            self.avg_7_days,
            self.avg_1_day,
            self.low_price,
            self.suggested_price,
        ]
        return [v for v in candidates if v is not None and v > 0]

    def best_market_value(self) -> float | None:
        """Return the best single market value using the preference hierarchy.

        Priority:
        1. Cardmarket Trend Price
        2. Cardmarket Market Price
        3. Cardmarket Average / 30-day Average
        4. Other Cardmarket-derived values
        5. Alternative marketplace values
        """
        for v in (
            self.price_trend,
            self.market_price,
            self.avg_30_days,
            self.avg_price,
            self.avg_7_days,
            self.avg_1_day,
            self.low_price,
            self.suggested_price,
        ):
            if v is not None and v > 0:
                return v
        if self.alt_prices:
            return next(iter(self.alt_prices.values()))
        return None

    def cm_low(self) -> float | None:
        """Lowest of all Cardmarket price values."""
        vals = self.cardmarket_values()
        return min(vals) if vals else None

    def cm_high(self) -> float | None:
        """Highest of all Cardmarket price values."""
        vals = self.cardmarket_values()
        return max(vals) if vals else None

    def cm_average(self) -> float | None:
        """Mean of all Cardmarket price values."""
        vals = self.cardmarket_values()
        return round(sum(vals) / len(vals), 2) if vals else None


# ---------------------------------------------------------------------------
# Confidence scoring
# ---------------------------------------------------------------------------

def _compute_confidence(
    result_data: dict[str, Any],
    query_context: dict[str, str | None],
) -> tuple[str, int]:
    """Return *(confidence_label, score)* comparing API result to query context.

    Scoring breakdown (total up to 100):
    - Card name match       : 0–40 pts
    - Set name / code match : 0–25 pts
    - Collector number match: 0–20 pts
    - Language match        : 0–10 pts
    - Has pricing data      :  5 pts

    Buckets:
    - High   : score >= 65
    - Medium : score >= 35
    - Low    : score <  35
    """
    score = 0

    # --- Card name ---
    query_name = (query_context.get("card_name") or "").lower().strip()
    result_name = (result_data.get("card_name") or result_data.get("name") or "").lower().strip()
    if query_name and result_name:
        if query_name == result_name:
            score += 40
        elif query_name in result_name or result_name in query_name:
            score += 25
        else:
            # Token overlap (e.g. "Charizard ex" vs "Charizard ex V1")
            q_tokens = set(query_name.split())
            r_tokens = set(result_name.split())
            overlap = q_tokens & r_tokens
            if overlap and len(overlap) >= min(2, len(q_tokens)):
                score += 15

    # --- Set name / code ---
    query_set = (query_context.get("set_name") or "").lower().strip()
    query_set_code = (query_context.get("set_code") or "").lower().strip()
    result_set = (result_data.get("set_name") or result_data.get("expansion") or "").lower().strip()
    result_set_code = (result_data.get("set_code") or "").lower().strip()
    if (query_set and result_set and query_set in result_set) or (
        query_set_code and result_set_code and query_set_code == result_set_code
    ):
        score += 25
    elif query_set and result_set:
        # partial token match
        qs_tokens = set(query_set.split())
        rs_tokens = set(result_set.split())
        if qs_tokens & rs_tokens:
            score += 10

    # --- Collector number ---
    query_num = (query_context.get("collector_number") or "").strip()
    result_num = (
        result_data.get("collector_number")
        or result_data.get("number")
        or result_data.get("card_number")
        or ""
    ).strip()
    if query_num and result_num:
        # Normalise: "006/165" vs "6/165" should match
        def _norm_num(n: str) -> str:
            parts = n.split("/")
            return "/".join(str(int(p)) if p.isdigit() else p for p in parts)
        if _norm_num(query_num) == _norm_num(result_num):
            score += 20
        elif query_num.lstrip("0") == result_num.lstrip("0"):
            score += 15

    # --- Language ---
    query_lang = (query_context.get("language") or "").lower().strip()
    result_lang = (result_data.get("language") or "").lower().strip()
    if query_lang and result_lang and query_lang == result_lang:
        score += 10

    # --- Has pricing data ---
    has_price = any(
        result_data.get(k) for k in (
            "price_trend", "trend_price", "market_price", "low_price",
            "avg_price", "avg30", "avg_30_days",
        )
    )
    if has_price:
        score += 5

    # Bucket
    if score >= 65:
        label = "High"
    elif score >= 35:
        label = "Medium"
    else:
        label = "Low"

    return label, score


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def _extract_slug_from_cardmarket_url(url: str) -> str | None:
    """Extract the product slug from a Cardmarket product URL.

    Examples::

        https://www.cardmarket.com/en/Pokemon/Products/Singles/Base-Set/Charizard
        → "Singles/Base-Set/Charizard"

        https://www.cardmarket.com/en/Pokemon/Products/Singles/Scarlet-Violet/Charizard-ex
        → "Singles/Scarlet-Violet/Charizard-ex"
    """
    parsed = urllib.parse.urlparse(url)
    netloc = parsed.netloc.lower()
    if netloc != "cardmarket.com" and not netloc.endswith(".cardmarket.com"):
        return None

    # Path pattern: /en/Pokemon/Products/<slug...>
    path = parsed.path
    match = re.search(r"/en/[^/]+/Products/(.+?)(?:\?|$)", path)
    if match:
        return match.group(1).rstrip("/")
    return None


def _cardmarket_url_to_search_terms(url: str) -> dict[str, str]:
    """Derive search context from a Cardmarket product URL.

    Returns a dict with keys that may include: ``card_name``, ``set_name``.
    """
    slug = _extract_slug_from_cardmarket_url(url)
    if not slug:
        return {}

    parts = [p for p in slug.split("/") if p.lower() not in ("singles", "products")]
    context: dict[str, str] = {}
    if len(parts) >= 2:
        # Last part is typically the card name (with hyphens instead of spaces)
        context["card_name"] = parts[-1].replace("-", " ")
        # Second-to-last is the set name
        context["set_name"] = parts[-2].replace("-", " ")
    elif len(parts) == 1:
        context["card_name"] = parts[0].replace("-", " ")
    return context


# ---------------------------------------------------------------------------
# API response parsing
# ---------------------------------------------------------------------------

_PRICE_FIELD_MAP: list[tuple[tuple[str, ...], str]] = [
    # Cardmarket trend price (highest priority)
    (("priceTrend", "price_trend", "trendPrice", "trend_price", "cmTrend"), "price_trend"),
    # Cardmarket market price
    (("marketPrice", "market_price", "cmMarket", "cm_market"), "market_price"),
    # 30-day average
    (("avg30", "avg_30", "avg30Days", "avg_30_days", "averagePrice30", "cmAvg30"), "avg_30_days"),
    # 7-day average
    (("avg7", "avg_7", "avg7Days", "avg_7_days", "averagePrice7", "cmAvg7"), "avg_7_days"),
    # 1-day average
    (("avg1", "avg_1", "avg1Day", "avg_1_day", "averagePrice1", "cmAvg1"), "avg_1_day"),
    # Generic / unspecified average
    (("avgPrice", "avg_price", "averagePrice", "avg", "cmAvg"), "avg_price"),
    # Low / from price
    (("lowPrice", "low_price", "fromPrice", "from_price", "minPrice", "cmLow"), "low_price"),
    # Suggested price
    (("suggestedPrice", "suggested_price", "suggestPrice"), "suggested_price"),
]

_IDENTITY_FIELD_MAP: dict[str, str] = {
    "name": "card_name",
    "cardName": "card_name",
    "card_name": "card_name",
    "setName": "set_name",
    "set_name": "set_name",
    "expansion": "set_name",
    "expansionName": "set_name",
    "setCode": "set_code",
    "set_code": "set_code",
    "expansionCode": "set_code",
    "number": "collector_number",
    "cardNumber": "collector_number",
    "collector_number": "collector_number",
    "collectorNumber": "collector_number",
    "language": "language",
    "lang": "language",
    "id": "tcggo_id",
    "cardId": "tcggo_id",
    "card_id": "tcggo_id",
    "cardmarketUrl": "cardmarket_url",
    "cardmarket_url": "cardmarket_url",
    "cmUrl": "cardmarket_url",
}

# Keys that are NOT Cardmarket prices (do not put in alt_prices)
_CM_PRICE_KEYS = {canonical for _, canonical in _PRICE_FIELD_MAP}


def _parse_card_data(raw: dict[str, Any], query_context: dict[str, str | None]) -> TcggoCardResult:
    """Build a ``TcggoCardResult`` from a raw API response object."""
    result = TcggoCardResult()

    # ── Identity fields ───────────────────────────────────────────────────────
    for api_key, attr in _IDENTITY_FIELD_MAP.items():
        val = raw.get(api_key)
        if val is not None and str(val).strip():
            setattr(result, attr, str(val).strip())

    # ── Price fields ──────────────────────────────────────────────────────────
    for api_keys, attr in _PRICE_FIELD_MAP:
        for k in api_keys:
            val = raw.get(k)
            if val is None:
                continue
            try:
                fval = float(val)
                if fval > 0:
                    setattr(result, attr, fval)
                    break
            except (ValueError, TypeError):
                pass

    # ── Nested price objects (e.g. {"prices": {"trend": 4.20, ...}}) ─────────
    nested = raw.get("prices") or raw.get("cardmarketPrices") or raw.get("cm_prices") or {}
    if isinstance(nested, dict):
        for api_keys, attr in _PRICE_FIELD_MAP:
            if getattr(result, attr) is not None:
                continue
            for k in api_keys:
                val = nested.get(k)
                if val is None:
                    continue
                try:
                    fval = float(val)
                    if fval > 0:
                        setattr(result, attr, fval)
                        break
                except (ValueError, TypeError):
                    pass

    # ── Alternative marketplace prices ───────────────────────────────────────
    alt_raw = raw.get("altPrices") or raw.get("alt_prices") or raw.get("otherPrices") or {}
    if isinstance(alt_raw, dict):
        for k, v in alt_raw.items():
            try:
                result.alt_prices[k] = float(v)
            except (ValueError, TypeError):
                pass

    # ── Confidence ───────────────────────────────────────────────────────────
    result.confidence, result.confidence_score = _compute_confidence(
        {
            "card_name": result.card_name,
            "set_name": result.set_name,
            "set_code": result.set_code,
            "collector_number": result.collector_number,
            "language": result.language,
            # Include raw price keys for the "has pricing data" check
            "price_trend": result.price_trend,
            "market_price": result.market_price,
            "low_price": result.low_price,
        },
        query_context,
    )

    return result


# ---------------------------------------------------------------------------
# Main client
# ---------------------------------------------------------------------------


class TcggoClient:
    """Async TCGGO API client authenticated through RapidAPI.

    All methods require an external ``aiohttp.ClientSession`` to be supplied
    so that the caller controls connection pooling and lifecycle.

    Usage::

        async with aiohttp.ClientSession() as session:
            client = TcggoClient.from_settings()
            result = await client.search_card(session, card_name="Charizard ex")
    """

    def __init__(
        self,
        rapidapi_key: str,
        rapidapi_host: str,
        api_url: str,
    ) -> None:
        if not rapidapi_key:
            raise ValueError("RAPIDAPI_KEY is required for TcggoClient")
        if not rapidapi_host:
            raise ValueError("RAPIDAPI_HOST is required for TcggoClient")
        if not api_url:
            raise ValueError("TCGGO_API_URL is required for TcggoClient")

        self._key = rapidapi_key
        self._host = rapidapi_host
        self._base_url = api_url.rstrip("/")

    @classmethod
    def from_settings(cls) -> "TcggoClient":
        """Construct from ``config.settings`` (reads env vars automatically)."""
        from config.settings import settings  # local import to avoid circular dep

        return cls(
            rapidapi_key=settings.rapidapi_key or "",
            rapidapi_host=settings.rapidapi_host or "",
            api_url=settings.tcggo_api_url or "",
        )

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-RapidAPI-Key": self._key,
            "X-RapidAPI-Host": self._host,
        }

    def is_configured(self) -> bool:
        """Return True when all required credentials are non-empty."""
        return bool(self._key and self._host and self._base_url)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def search_card(
        self,
        session: aiohttp.ClientSession,
        *,
        card_name: str | None = None,
        set_name: str | None = None,
        set_code: str | None = None,
        collector_number: str | None = None,
        ocr_text: str | None = None,
        listing_title: str | None = None,
        language: str | None = None,
    ) -> TcggoCardResult | None:
        """Search TCGGO for a card using any combination of identifiers.

        Tries an exact search first using the most specific identifiers, then
        falls back to a fuzzy title-based search when no exact match is found.

        Returns the best-matching ``TcggoCardResult`` or ``None`` when no
        match passes the minimum confidence threshold (score > 0).
        """
        if not self.is_configured():
            logger.debug("TcggoClient: not configured – skipping lookup")
            return None

        query_context: dict[str, str | None] = {
            "card_name": card_name,
            "set_name": set_name,
            "set_code": set_code,
            "collector_number": collector_number,
            "language": language,
        }

        # Build the primary query string from the richest available data.
        primary_query = self._build_query(
            card_name=card_name,
            set_name=set_name,
            set_code=set_code,
            collector_number=collector_number,
        )

        # Attempt exact / structured search first.
        result = await self._search_exact(session, primary_query, query_context)
        if result and result.confidence != "Low":
            return result

        # Fallback: title or OCR text search.
        for fallback_query in filter(None, [listing_title, ocr_text]):
            result = await self._search_fuzzy(session, fallback_query, query_context)
            if result and result.confidence != "Low":
                return result

        # Return whatever we found (even Low confidence) so callers can decide.
        return result

    async def lookup_by_url(
        self,
        session: aiohttp.ClientSession,
        cardmarket_url: str,
    ) -> TcggoCardResult | None:
        """Resolve a Cardmarket product URL to a TCGGO result.

        Extracts product identifiers from the URL and then searches TCGGO.
        The URL is treated as an identification source, not a pricing source;
        no scraping of the Cardmarket page is performed.
        """
        context = _cardmarket_url_to_search_terms(cardmarket_url)
        if not context:
            logger.warning(
                "TcggoClient: could not extract search terms from URL: %s",
                cardmarket_url,
            )
            return None

        slug = _extract_slug_from_cardmarket_url(cardmarket_url)
        query_context: dict[str, str | None] = {
            "card_name": context.get("card_name"),
            "set_name": context.get("set_name"),
            "set_code": None,
            "collector_number": None,
            "language": None,
        }

        # Try to look up by URL slug first (most accurate).
        result = await self._lookup_slug(session, slug, query_context)
        if result and result.confidence != "Low":
            return result

        # Fall back to name+set search.
        query = self._build_query(
            card_name=context.get("card_name"),
            set_name=context.get("set_name"),
        )
        return await self._search_exact(session, query, query_context)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_with_retry(
        self,
        session: aiohttp.ClientSession,
        endpoint: str,
        params: dict[str, str],
        max_retries: int = 3,
    ) -> tuple[int, Any]:
        """Perform a GET request, retrying on HTTP 429 and 5xx responses.

        On HTTP 429 the ``Retry-After`` response header is respected when
        present; otherwise a 60-second back-off is used.  5xx errors use an
        exponential back-off starting at 1 second (capped at 30 seconds).

        Returns a ``(status_code, response_data)`` tuple.  When all retries
        are exhausted ``(last_status, None)`` is returned.
        """
        delay = 1.0
        last_status = -1
        for attempt in range(max_retries + 1):
            should_retry = False
            next_delay = delay
            try:
                async with session.get(
                    endpoint,
                    params=params,
                    headers=self._headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    last_status = resp.status
                    if resp.status == 429:
                        retry_after = resp.headers.get("Retry-After", "")
                        try:
                            next_delay = float(retry_after)
                        except (ValueError, TypeError):
                            next_delay = 60.0
                        logger.warning(
                            "TCGGO rate limited (HTTP 429); retrying in %.0fs"
                            " (attempt %d/%d)",
                            next_delay, attempt + 1, max_retries,
                        )
                        should_retry = True
                    elif resp.status >= 500:
                        logger.warning(
                            "TCGGO API returned HTTP %d; retrying in %.0fs"
                            " (attempt %d/%d)",
                            resp.status, next_delay, attempt + 1, max_retries,
                        )
                        should_retry = True
                    else:
                        data = await resp.json(content_type=None)
                        return resp.status, data
            except Exception as exc:  # noqa: BLE001
                logger.warning("TCGGO request to %s failed: %s", endpoint, exc)
                if attempt >= max_retries:
                    raise
                should_retry = True

            if not should_retry or attempt >= max_retries:
                break
            await asyncio.sleep(next_delay)
            delay = min(delay * 2, 30.0)

        return last_status, None

    def _build_query(
        self,
        card_name: str | None = None,
        set_name: str | None = None,
        set_code: str | None = None,
        collector_number: str | None = None,
    ) -> str:
        """Compose a query string from the available identifiers."""
        parts = []
        if card_name:
            parts.append(card_name.strip())
        if set_name:
            parts.append(set_name.strip())
        if set_code:
            parts.append(set_code.strip())
        if collector_number:
            parts.append(collector_number.strip())
        return " ".join(parts)

    async def _search_exact(
        self,
        session: aiohttp.ClientSession,
        query: str,
        query_context: dict[str, str | None],
    ) -> TcggoCardResult | None:
        """GET a card search request to the cardmarket-api-tcg endpoint."""
        if not query.strip():
            return None

        endpoint = f"{self._base_url}/pokemon/cards/search"
        params = {"search": query, "sort": "relevance"}

        try:
            status, data = await self._get_with_retry(session, endpoint, params)
        except Exception as exc:  # noqa: BLE001
            logger.warning("TCGGO search failed for '%s': %s", query, exc)
            return None

        if status == 404:
            logger.debug("TCGGO: no result for query '%s'", query)
            return None
        if status != 200:
            logger.warning(
                "TCGGO API returned HTTP %d for query '%s'", status, query
            )
            return None

        return self._pick_best(data, query_context)

    async def _search_fuzzy(
        self,
        session: aiohttp.ClientSession,
        query: str,
        query_context: dict[str, str | None],
    ) -> TcggoCardResult | None:
        """Fuzzy / title-based search using the cardmarket-api-tcg endpoint."""
        if not query.strip():
            return None

        endpoint = f"{self._base_url}/pokemon/cards/search"
        params = {"search": query, "sort": "relevance"}

        try:
            status, data = await self._get_with_retry(session, endpoint, params)
        except Exception as exc:  # noqa: BLE001
            logger.warning("TCGGO fuzzy search failed for '%s': %s", query, exc)
            return None

        if status not in (200, 404):
            logger.warning(
                "TCGGO fuzzy search returned HTTP %d for '%s'", status, query
            )
        if status != 200:
            return None

        return self._pick_best(data, query_context)

    async def _lookup_slug(
        self,
        session: aiohttp.ClientSession,
        slug: str | None,
        query_context: dict[str, str | None],
    ) -> TcggoCardResult | None:
        """Attempt a direct TCGGO lookup by Cardmarket product slug."""
        if not slug:
            return None

        endpoint = f"{self._base_url}/cardmarket"
        params = {"slug": slug, "game": "pokemon"}

        try:
            status, data = await self._get_with_retry(session, endpoint, params)
        except Exception as exc:  # noqa: BLE001
            logger.warning("TCGGO slug lookup failed for '%s': %s", slug, exc)
            return None

        if status != 200:
            logger.debug(
                "TCGGO slug lookup HTTP %d for slug '%s'", status, slug
            )
            return None

        # Slug lookup returns a single card object (not a list).
        if isinstance(data, dict):
            return _parse_card_data(data, query_context)
        return self._pick_best(data, query_context)

    def _pick_best(
        self,
        data: Any,
        query_context: dict[str, str | None],
    ) -> TcggoCardResult | None:
        """Select the best match from a list or single-object API response."""
        if not data:
            return None

        items: list[dict[str, Any]]
        if isinstance(data, list):
            items = [d for d in data if isinstance(d, dict)]
        elif isinstance(data, dict):
            # Some APIs wrap in {"data": [...]} or {"results": [...]}
            inner = data.get("data") or data.get("results") or data.get("cards")
            if isinstance(inner, list):
                items = [d for d in inner if isinstance(d, dict)]
            else:
                items = [data]
        else:
            return None

        if not items:
            return None

        candidates: list[TcggoCardResult] = [
            _parse_card_data(item, query_context) for item in items
        ]

        # Sort by confidence score descending; prefer items with pricing data.
        candidates.sort(
            key=lambda r: (r.confidence_score, r.best_market_value() is not None),
            reverse=True,
        )

        best = candidates[0]
        logger.debug(
            "TCGGO best match: '%s' set='%s' confidence=%s score=%d price=%s",
            best.card_name,
            best.set_name,
            best.confidence,
            best.confidence_score,
            best.best_market_value(),
        )
        return best
