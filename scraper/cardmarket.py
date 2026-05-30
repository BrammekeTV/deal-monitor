"""
scraper/cardmarket.py
~~~~~~~~~~~~~~~~~~~~~
Playwright-based price scraper for Cardmarket (cardmarket.com).

Architecture (new):
  The primary entry point is ``CardmarketScraper.scrape_url()``, which
  accepts a resolved Cardmarket product URL and scrapes its pricing data
  directly.  No search step is performed – the caller must provide the
  exact product URL.

  The Dutch-seller filter (``sellerCountry=23``) and English-language filter
  (``language=1``) are always applied via ``normalize_cardmarket_url()``.

Legacy search-based flow (``CardmarketScraper.lookup()``) is retained for
backwards compatibility with tests that still exercise the search-results
approach.

HTML parsing helpers are kept public so they can be unit-tested in isolation
without a browser:
  - ``_clean_price_string``
  - ``_parse_price_to_float``
  - ``_parse_product_page``
  - ``_extract_card_metadata``
  - ``normalize_cardmarket_url``
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import traceback
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote_plus, urlparse, urlunparse, urlencode, parse_qs

from bs4 import BeautifulSoup
from playwright.async_api import Browser, BrowserContext, Page

from utils.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CM_BASE = "https://www.cardmarket.com"
_CM_SEARCH_URL = (
    _CM_BASE
    + "/en/Pokemon/Products/Search"
    + "?searchString={query}&sortBy=price_asc&perSite=20"
)

# Cookie / consent banner accept button selectors.
_COOKIE_SEL = (
    "button#cmVendorAcceptBtn,"
    "button[data-role='acceptAll'],"
    "button.btn-primary[class*='consent'],"
    "#onetrust-accept-btn-handler,"
    "button[id*='accept'],"
    "button[class*='accept']"
)

# Search-results page: any link pointing to a Pokemon product.
_PRODUCT_LINK_SEL = "a[href*='/en/Pokemon/Products/']"

# Price info container on product pages; we wait for this to be rendered.
# Multiple selectors are tried in order to handle HTML structure variations.
_PRICE_CONTAINER_SELS = [
    "dl.info-list-container",
    ".info-list-container dl",
    ".info-list-container",
    ".col-price dl",
    ".product-price-info dl",
]
_PRICE_CONTAINER_SEL = _PRICE_CONTAINER_SELS[0]  # Primary (for wait_for_selector)

# Minimum path-segment depth for a product page URL (not a category/search page).
# e.g. /en/Pokemon/Products/Singles/Base-Set/Charizard  → 6 segments
_MIN_PRODUCT_PATH_DEPTH = 6

# JavaScript snippet injected into every new page to hide automation signals.
_STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3]});
Object.defineProperty(navigator, 'languages', {get: () => ['nl-NL', 'nl', 'en-GB', 'en']});
window.chrome = {runtime: {}};
"""

# Default filter parameters added to product URLs.
# sellerCountry=23 → Netherlands; language=1 → English cards.
_CM_FILTER_PARAMS: dict[str, str] = {"sellerCountry": "23", "language": "1"}

# ---------------------------------------------------------------------------
# Set-code → Cardmarket URL slug mapping
# ---------------------------------------------------------------------------
# Maps the short set code (as seen in Vinted listing titles) to the slug used
# in Cardmarket product URLs under /en/Pokemon/Products/Singles/{slug}/...
# Covers Scarlet & Violet era, Sword & Shield era, and older series.
_SET_CODE_TO_SLUG: dict[str, str] = {
    # Scarlet & Violet promos / special sets
    "SVP": "SV-Black-Star-Promos",
    "SVE": "SV-Energies",
    # Scarlet & Violet main sets (official codes + numeric aliases)
    "SVI": "Scarlet-Violet",
    "SV1": "Scarlet-Violet",       # numeric alias
    "PAL": "Paldea-Evolved",
    "SV2": "Paldea-Evolved",       # numeric alias
    "OBF": "Obsidian-Flames",
    "SV3": "Obsidian-Flames",      # numeric alias
    "MEW": "151",
    "SV3PT5": "151",               # numeric alias
    "PAR": "Paradox-Rift",
    "SV4": "Paradox-Rift",         # numeric alias
    "PAF": "Paldean-Fates",
    "SV4PT5": "Paldean-Fates",     # numeric alias
    "TEF": "Temporal-Forces",
    "SV5": "Temporal-Forces",      # numeric alias
    "TWM": "Twilight-Masquerade",
    "SV6": "Twilight-Masquerade",  # numeric alias
    "SFA": "Shrouded-Fable",
    "SV6PT5": "Shrouded-Fable",    # numeric alias
    "SCR": "Stellar-Crown",
    "SV7": "Stellar-Crown",        # numeric alias
    "SSP": "Surging-Sparks",
    "SV8": "Surging-Sparks",       # numeric alias
    "PRE": "Prismatic-Evolutions",
    "SV8PT5": "Prismatic-Evolutions",  # numeric alias
    "JTG": "Journey-Together",
    "SV9": "Journey-Together",     # numeric alias
    # Sword & Shield promos / special sets
    "SWSHP": "SWSH-Black-Star-Promos",
    "CEL25": "Celebrations",
    "CPA": "Champions-Path",
    "SWSH45": "Shining-Fates",
    "CRZ": "Crown-Zenith",
    "PGO": "Pokemon-GO",
    # Sword & Shield main sets (SWSH1–SWSH12)
    "SWSH1": "Sword-Shield",
    "SWSH2": "Rebel-Clash",
    "SWSH3": "Darkness-Ablaze",
    "SWSH4": "Vivid-Voltage",
    "SWSH5": "Battle-Styles",
    "SWSH6": "Chilling-Reign",
    "SWSH7": "Evolving-Skies",
    "SWSH8": "Fusion-Strike",
    "SWSH9": "Brilliant-Stars",
    "SWSH10": "Astral-Radiance",
    "SWSH11": "Lost-Origin",
    "SWSH12": "Silver-Tempest",
    # Sun & Moon promos / special sets
    "SMP": "SM-Black-Star-Promos",
    "DRM": "Dragon-Majesty",
    "HIF": "Hidden-Fates",
    "CEC": "Cosmic-Eclipse",
    # Sun & Moon main sets
    "SM1": "Sun-Moon",
    "SM2": "Guardians-Rising",
    "SM3": "Burning-Shadows",
    "SM4": "Crimson-Invasion",
    "SM5": "Ultra-Prism",
    "SM6": "Forbidden-Light",
    "SM7": "Celestial-Storm",
    "SM8": "Lost-Thunder",
    "SM9": "Team-Up",
    "SM10": "Unbroken-Bonds",
    "SM11": "Unified-Minds",
    "SM12": "Cosmic-Eclipse",
    # XY promos / special sets
    "XYP": "XY-Black-Star-Promos",
    "GEN": "Generations",
    "EVO": "Evolutions",
    "FCO": "Fates-Collide",
    "STE": "Steam-Siege",
    "BKP": "Breakpoint",
    "BKT": "Breakthrough",
    "AOR": "Ancient-Origins",
    "ROS": "Roaring-Skies",
    "DCR": "Double-Crisis",
    "PRC": "Primal-Clash",
    "PHF": "Phantom-Forces",
    "FLF": "Flashfire",
    "FFI": "Furious-Fists",
    "XY1": "XY",
    # Black & White promos / special sets
    "BWP": "BW-Black-Star-Promos",
    "NXD": "Next-Destinies",
    "DEX": "Dark-Explorers",
    "DRX": "Dragons-Exalted",
    "BCR": "Boundaries-Crossed",
    "PLS": "Plasma-Storm",
    "PLF": "Plasma-Freeze",
    "PLB": "Plasma-Blast",
    "LTR": "Legendary-Treasures",
    "KSS": "Kalos-Starter-Set",
    # Black & White main sets
    "BLW": "Black-White",
    "EPO": "Emerging-Powers",
    "NVI": "Noble-Victories",
    "DEX2": "Dark-Explorers",
    # HeartGold / SoulSilver
    "HS": "HeartGold-SoulSilver",
    "UL": "Unleashed",
    "UD": "Undaunted",
    "TM": "Triumphant",
    "CL": "Call-of-Legends",
    # Platinum
    "PL": "Platinum",
    "RR": "Rising-Rivals",
    "SV": "Supreme-Victors",
    "AR": "Arceus",
    # Diamond & Pearl
    "DP": "Diamond-Pearl",
    "MT": "Mysterious-Treasures",
    "SW": "Secret-Wonders",
    "GE": "Great-Encounters",
    "MD": "Majestic-Dawn",
    "LA": "Legends-Awakened",
    "SF": "Stormfront",
    # EX era
    "RS": "Ruby-Sapphire",
    "SS": "Sandstorm",
    "DR": "Dragon",
    "MA": "Team-Magma-vs-Team-Aqua",
    "HL": "Hidden-Legends",
    "FR": "FireRed-LeafGreen",
    "TRR": "Team-Rocket-Returns",
    "DX": "Deoxys",
    "EM": "Emerald",
    "UF": "Unseen-Forces",
    "DS": "Delta-Species",
    "LM": "Legend-Maker",
    "HP": "Holon-Phantoms",
    "CG": "Crystal-Guardians",
    "DF": "Dragon-Frontiers",
    "PK": "Power-Keepers",
    # Classic (Base Set era)
    "BS": "Base-Set",
    "JU": "Jungle",
    "FO": "Fossil",
    "B2": "Base-Set-2",
    "TR": "Team-Rocket",
    "G1": "Gym-Heroes",
    "G2": "Gym-Challenge",
    "N1": "Neo-Genesis",
    "N2": "Neo-Discovery",
    "N3": "Neo-Revelation",
    "N4": "Neo-Destiny",
    "LC": "Legendary-Collection",
    "EX": "Expedition-Base-Set",
    "AQ": "Aquapolis",
    "SK": "Skyridge",
    # Japanese sets (partial)
    "S12a": "VSTAR-Universe",
    "sv2a": "151",               # Japanese 151 / Pokémon Card 151
    "S9": "Star-Birth",
    "S8b": "VMAX-Climax",
    "S8a": "Incandescent-Arcana",
    # French Scarlet & Violet aliases
    "EV1": "Scarlet-Violet",
    "EV2": "Paldea-Evolved",
    "EV3": "Obsidian-Flames",
    "EV3PT5": "151",
    "EV4": "Paradox-Rift",
    "EV4PT5": "Paldean-Fates",
    "EV5": "Temporal-Forces",
    "EV6": "Twilight-Masquerade",
    "EV6PT5": "Shrouded-Fable",
    "EV7": "Stellar-Crown",
    "EV8": "Surging-Sparks",
    "EV8PT5": "Prismatic-Evolutions",
    # HeartGold / SoulSilver aliases
    "HGSS": "HeartGold-SoulSilver",
    # Platinum aliases
    "PLA": "Platinum",
    "PLAT": "Platinum",
    "STS": "Supreme-Victors",
    # McDonald's promos
    "MCD": "McDonalds-Collection",
    "MCDO": "McDonalds-Collection",
    "MCDP": "McDonalds-Collection",
    # Pokémon GO alias
    "GO": "Pokemon-GO",
}


# ---------------------------------------------------------------------------
# Language → Cardmarket language-code mapping
# ---------------------------------------------------------------------------
# Cardmarket uses numeric language codes in the ``language`` query parameter.
# 1=English (default), 2=French, 3=German, 4=Spanish, 5=Italian, 7=Japanese,
# 8=Korean, 9=Russian, 10=Portuguese, 11=Dutch.
_LANGUAGE_TO_CM_CODE: dict[str, str] = {
    "English": "1",
    "Dutch": "11",
    "French": "2",
    "German": "3",
    "Spanish": "4",
    "Italian": "5",
    "Japanese": "7",
    "Korean": "8",
    "Russian": "9",
    "Portuguese": "10",
}

# ---------------------------------------------------------------------------
# Variant hints — per-card preferred product slugs
# ---------------------------------------------------------------------------
# When multiple Cardmarket product variants exist for the same collector
# number, this dict maps ``(card_name_slug, set_code_upper, bare_number)``
# to the preferred full product slug (e.g. "Bulbasaur-V2-MEW166").
# Populated at startup from corrections and by register_variant_hint().
_VARIANT_HINTS: dict[tuple[str, str, str], str] = {}


def register_variant_hint(
    card_name: str,
    set_code: str,
    collector_number: str,
    preferred_slug: str,
) -> None:
    """Register a preferred product slug for a specific card variant.

    Overwrites any existing hint for the same ``(card_name, set_code,
    collector_number)`` triple.  Callers should pass the bare collector number
    (digits only, no total, no prefix), e.g. ``"166"`` for ``"166/165"``.

    Example::

        register_variant_hint("Bulbasaur", "MEW", "166", "Bulbasaur-V2-MEW166")
    """
    card_slug = re.sub(r"[^A-Za-z0-9]+", "-", card_name).strip("-")
    bare_num = re.sub(r"[^0-9]", "", collector_number.split("/")[0])
    key = (card_slug, set_code.upper(), bare_num)
    _VARIANT_HINTS[key] = preferred_slug
    logger.debug(
        "Registered variant hint %r → %r", key, preferred_slug
    )


# ---------------------------------------------------------------------------
# Public URL helpers
# ---------------------------------------------------------------------------

def build_cardmarket_url(
    card_name: str,
    set_code: str,
    collector_number: str,
    *,
    promo: bool = False,
    number_prefix: str | None = None,
) -> str | None:
    """Build a Cardmarket product URL from card identity fields.

    Handles both standard collector numbers (``006/197``) and promo-style
    numbers (``SVP 214``, ``SVP214``).

    *number_prefix* – when provided (e.g. ``"TR"`` for Team Rocket), the
    prefix is prepended to the bare collector number in the product slug,
    overriding the default logic.  This is used to apply learned prefix
    patterns from the correction database.

    Returns a normalised Cardmarket URL with ``sellerCountry=23`` and
    ``language=1`` filter params, or ``None`` when the set code is unknown.

    Examples::

        build_cardmarket_url("Pikachu", "SVP", "214")
        → "https://www.cardmarket.com/en/Pokemon/Products/Singles/
           SV-Black-Star-Promos/Pikachu-SVP214?sellerCountry=23&language=1"

        build_cardmarket_url("Charizard ex", "OBF", "125/197")
        → "https://www.cardmarket.com/en/Pokemon/Products/Singles/
           Obsidian-Flames/Charizard-ex-125?sellerCountry=23&language=1"

        build_cardmarket_url("Dark Raichu", "TR", "83/82", number_prefix="TR")
        → "https://www.cardmarket.com/en/Pokemon/Products/Singles/
           Team-Rocket/Dark-Raichu-TR83?sellerCountry=23&language=1"
    """
    set_slug = _SET_CODE_TO_SLUG.get(set_code.upper() if set_code else "")
    if not set_slug:
        return None

    # Build the card slug: replace spaces/special chars with hyphens.
    card_slug = re.sub(r"[^A-Za-z0-9]+", "-", card_name).strip("-")

    # Build the collector-number suffix.
    # Promo format:  "SVP 214" or "214"  → suffix = "SVP214" (set_code + bare_number)
    # Standard format: "125/197"          → suffix = "125"    (just the card number)
    bare_num = re.sub(r"[^0-9]", "", collector_number.split("/")[0]) if collector_number else ""

    if number_prefix is not None:
        # Explicit prefix supplied by caller (learned from correction database).
        num_suffix = f"{number_prefix}{bare_num}" if bare_num else number_prefix
    elif promo or "/" not in (collector_number or ""):
        # Promo card: {card-slug}-{set_code}{number}
        num_suffix = f"{set_code.upper()}{bare_num}" if bare_num else set_code.upper()
    else:
        # Standard card: {card-slug}-{bare_number}
        num_suffix = bare_num

    product_slug = f"{card_slug}-{num_suffix}" if num_suffix else card_slug

    # Check variant hints: if a preferred slug is registered for this exact
    # card/set/number combination, use it instead of the auto-generated one.
    hint_key = (card_slug, set_code.upper(), bare_num)
    if hint_key in _VARIANT_HINTS:
        product_slug = _VARIANT_HINTS[hint_key]
        logger.debug(
            "build_cardmarket_url: applied variant hint %r → %r", hint_key, product_slug
        )

    url = f"{_CM_BASE}/en/Pokemon/Products/Singles/{set_slug}/{product_slug}"
    return normalize_cardmarket_url(url)


def normalize_cardmarket_url(
    url: str,
    *,
    language: str | None = None,
    is_reverse_holo: bool = False,
) -> str:
    """Return *url* with the standard Cardmarket filter params appended.

    Adds ``sellerCountry=23`` (Netherlands) and ``language=1`` (English) if
    they are not already present in the query string.  Non-Cardmarket URLs are
    returned unchanged.

    *language* – when provided as a Cardmarket language code string (e.g.
    ``"7"`` for Japanese), overrides the default ``language=1`` param.

    *is_reverse_holo* – when ``True``, appends ``isReverseHolo=Y``.
    """
    parsed = urlparse(url)
    netloc = parsed.netloc.lower()
    if netloc != "cardmarket.com" and not netloc.endswith(".cardmarket.com"):
        return url
    params = parse_qs(parsed.query, keep_blank_values=True)
    # Apply base filter params (sellerCountry + default language).
    for key, value in _CM_FILTER_PARAMS.items():
        if key not in params:
            params[key] = [value]
    # Override language when a specific code was requested.
    if language is not None:
        params["language"] = [language]
    # Append reverse-holo flag when applicable.
    if is_reverse_holo:
        params["isReverseHolo"] = ["Y"]
    new_query = urlencode({k: v[0] for k, v in params.items()})
    return urlunparse(parsed._replace(query=new_query))


async def validate_cardmarket_url(url: str) -> bool:
    """Perform a lightweight HEAD request to check that *url* resolves on Cardmarket.

    Returns ``True`` if the URL returns a 2xx/3xx status, or ``False`` when a
    4xx (typically 404) is received.  Network errors are treated as *not*
    resolved (returns ``False``) and are logged at WARNING level rather than
    raising.

    Uses ``aiohttp`` with a short timeout (10 s) so that a bad URL does not
    stall the processing pipeline for long.
    """
    import aiohttp  # noqa: PLC0415  (lazy import to avoid hard dep at module level)

    timeout = aiohttp.ClientTimeout(total=10)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; DealMonitor/1.0; "
            "+https://github.com/BrammekeTV/deal-monitor)"
        )
    }
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.head(url, allow_redirects=True) as resp:
                if resp.status == 404:
                    logger.warning("validate_cardmarket_url: 404 for %s", url)
                    return False
                if resp.status >= 400:
                    logger.warning(
                        "validate_cardmarket_url: unexpected status %d for %s",
                        resp.status, url,
                    )
                    return False
                return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("validate_cardmarket_url: request error for %s — %s", url, exc)
        return False
# ---------------------------------------------------------------------------

def _clean_price_string(price_string: str) -> str:
    """Fix mojibake encoding issues in price strings (â‚¬ → €, etc.)."""
    if not price_string:
        return ""
    return (
        price_string
        .replace("â\x82¬", "€")
        .replace("Â£", "£")
        .strip()
    )


def _parse_price_to_float(price_string: str) -> float:
    """Parse a European-format price string (``"13,98 €"``) into a float.

    Handles both comma-as-decimal (``13,98``) and dot-as-thousands-separator
    (``1.234,56``) formats.
    """
    if not price_string:
        return 0.0
    try:
        cleaned = "".join(c for c in price_string if c.isdigit() or c in ",.").strip()
        if not cleaned:
            return 0.0
        if "," in cleaned and "." in cleaned:
            # European with thousands separator: "1.234,56" → "1234.56"
            cleaned = cleaned.replace(".", "").replace(",", ".")
        elif "," in cleaned:
            # Decimal comma only: "13,98" → "13.98"
            cleaned = cleaned.replace(",", ".")
        return float(cleaned)
    except (ValueError, AttributeError):
        return 0.0


def _find_price_dl(soup: BeautifulSoup) -> Any | None:
    """Find the price info ``<dl>`` element using multiple selector strategies."""
    for sel in _PRICE_CONTAINER_SELS:
        el = soup.select_one(sel)
        if el is None:
            continue
        # If the matched element is already a <dl>, use it directly.
        if el.name == "dl":
            return el
        # Otherwise look for a <dl> child.
        dl = el.select_one("dl")
        if dl:
            return dl
    return None


def _extract_price_from_dd(dd_el: Any) -> str:
    """Extract the price text from a ``<dd>`` element.

    Cardmarket wraps prices in one or more ``<span>`` elements.  We try the
    innermost span first (to skip outer wrappers with icon elements), then
    fall back to the full ``<dd>`` text.
    """
    # Collect all spans.
    spans = dd_el.select("span")
    # Return the last span that looks like a price (contains digit + currency sign).
    for span in reversed(spans):
        text = span.get_text(" ", strip=True)
        # A price span has at least one digit.
        if any(c.isdigit() for c in text):
            return _clean_price_string(text)
    # Fall back to the full dd text.
    return _clean_price_string(dd_el.get_text(" ", strip=True))


def _parse_product_page(html_content: str) -> dict[str, Any]:
    """Extract price fields (and optional card metadata) from a Cardmarket product page.

    Tries two strategies in order:
    1. Parse the ``dl`` inside ``.info-list-container`` (rendered HTML).
    2. Search ``<script>`` tags for embedded JSON price data as a fallback.

    Returns a dict with keys: ``lowest_price``, ``price_trend``,
    ``avg_30_days``, ``avg_7_days``, ``avg_1_day``.  Additional metadata
    keys ``set_name``, ``card_number``, ``card_name`` are included when
    found.  Missing fields are omitted from the dict.

    A page is only considered successfully parsed when the returned dict is
    non-empty (i.e. at least one pricing metric was found).
    """
    soup = BeautifulSoup(html_content, "html.parser")
    prices: dict[str, Any] = {}

    # ── Strategy 1: rendered info-list-container ──────────────────────────
    dl = _find_price_dl(soup)
    if dl:
        dt_elements = dl.select("dt")
        dd_elements = dl.select("dd")

        price_data: dict[str, str] = {}
        for i in range(min(len(dt_elements), len(dd_elements))):
            key = dt_elements[i].get_text(" ", strip=True)
            value = _extract_price_from_dd(dd_elements[i])
            price_data[key] = value
            logger.debug("Cardmarket dt/dd: %r → %r", key, value)

        # Lowest / From price  (labels: "From" / "De" / "Ab" / "Vanaf" / "Fra")
        for key, value in price_data.items():
            if key.lower() in ("from", "de", "ab", "vanaf", "fra", "od", "da", "vanuit"):
                v = _parse_price_to_float(value)
                if v > 0:
                    prices["lowest_price"] = v
                break

        # Price trend  (labels containing "trend" / "tendance" / "prijstrend")
        for key, value in price_data.items():
            if any(t in key.lower() for t in ("trend", "tendance")):
                v = _parse_price_to_float(value)
                if v > 0:
                    prices["price_trend"] = v
                break

        # 30-day average
        for key, value in price_data.items():
            if "30" in key and any(t in key.lower() for t in ("day", "jour", "tage", "dag", "dias", "giorni")):
                v = _parse_price_to_float(value)
                if v > 0:
                    prices["avg_30_days"] = v
                break

        # 7-day average
        for key, value in price_data.items():
            if "7" in key and any(t in key.lower() for t in ("day", "jour", "tage", "dag", "dias", "giorni")):
                v = _parse_price_to_float(value)
                if v > 0:
                    prices["avg_7_days"] = v
                break

        # 1-day average
        for key, value in price_data.items():
            k_lower = key.lower()
            if (
                re.search(r"\b1\b", key)
                and any(t in k_lower for t in ("day", "jour", "tage", "dag", "dias", "giorni"))
                and "30" not in key
                and "7" not in key
            ):
                v = _parse_price_to_float(value)
                if v > 0:
                    prices["avg_1_day"] = v
                break

    if prices:
        _extract_card_metadata(soup, prices)
        return prices

    logger.debug("Cardmarket: .info-list-container empty or absent – trying JSON fallback")

    # ── Strategy 2: embedded JSON in <script> tags ────────────────────────
    prices = _extract_json_prices(soup)
    if not prices:
        logger.debug("Cardmarket: no pricing data found in page HTML or embedded JSON")
    else:
        _extract_card_metadata(soup, prices)
    return prices


def _extract_card_metadata(soup: BeautifulSoup, prices: dict[str, Any]) -> None:
    """Extract card set name, card number and card name from the page and add
    them to *prices* in-place.  Missing fields are silently skipped."""
    # ── Card name from <h1> ───────────────────────────────────────────────
    h1 = soup.select_one("h1")
    if h1:
        card_name = h1.get_text(" ", strip=True)
        if card_name:
            prices["card_name"] = card_name

    # ── Set name from breadcrumb ──────────────────────────────────────────
    breadcrumb_links = soup.select("ol.breadcrumb li a, .breadcrumb a")
    if len(breadcrumb_links) >= 2:
        set_link = breadcrumb_links[-2]
        set_name = set_link.get_text(" ", strip=True)
        if set_name and set_name.lower() not in ("pokemon", "pokémon", "singles", "products"):
            prices["set_name"] = set_name

    # ── Card number from page ─────────────────────────────────────────────
    full_text = soup.get_text(" ", strip=True)
    number_match = re.search(r"\b(\d{1,3}/\d{2,4})\b", full_text)
    if number_match:
        prices["card_number"] = number_match.group(1)


# ---------------------------------------------------------------------------
# PSA detection helpers  (public so they can be used by the monitor cog)
# ---------------------------------------------------------------------------

# Compiled once for performance.
_PSA_GRADE_RE = re.compile(r"\bPSA\s*(\d{1,2})\b", re.IGNORECASE)


def contains_psa(text: str) -> bool:
    """Return True if *text* contains the word "PSA" (case-insensitive)."""
    return bool(re.search(r"\bPSA\b", text, re.IGNORECASE))


def extract_psa_grade(text: str) -> int | None:
    """Extract the PSA grade number from *text* (e.g. "PSA 9" → 9, "PSA10" → 10).

    Returns the grade as an integer, or ``None`` when no grade can be parsed.
    Only the *first* PSA grade token found is returned.
    """
    match = _PSA_GRADE_RE.search(text)
    if match:
        return int(match.group(1))
    return None


# ---------------------------------------------------------------------------
# PSA listing-row helpers (used by _parse_psa_listing_price)
# ---------------------------------------------------------------------------

# Selectors tried (in order) to locate individual offer/article rows on a
# Cardmarket product page.
_ARTICLE_ROW_SELS = [
    ".article-row",
    "article.article-row",
    "[class*='article-row']",
]

# Selectors tried (in order) to extract the offer price from a row.
_ROW_PRICE_SELS = [
    ".price-container",
    ".col-offer-price",
    ".col-price",
]


def _row_has_mint_condition(row: Any) -> bool:
    """Return True when the listing row carries a Mint (MT) condition badge.

    Tries three strategies:
    1. A class containing ``condition-mt`` (e.g. ``badge-article-condition-mt``).
    2. A ``title`` / ``data-original-title`` attribute equal to "Mint" or "MT".
    3. A ``<span>`` / ``<a>`` / ``<abbr>`` whose *text* is exactly "MT".
    """
    for el in row.find_all(True):
        classes = " ".join(el.get("class") or []).lower()
        if "condition-mt" in classes:
            return True
        title = (el.get("title") or el.get("data-original-title") or "").strip().lower()
        if title in ("mint", "mt"):
            return True
    # Fallback: any badge-like inline element whose text is "MT".
    for el in row.find_all(["span", "a", "abbr"]):
        if el.get_text(strip=True).upper() == "MT":
            return True
    return False


def _extract_row_price(row: Any) -> float:
    """Return the first positive price found in a Cardmarket listing row.

    Uses *_ROW_PRICE_SELS* containers first, then falls back to scanning all
    ``<span>`` elements for anything that looks like a currency amount.
    """
    for sel in _ROW_PRICE_SELS:
        container = row.select_one(sel)
        if container:
            text = _extract_price_from_dd(container)
            if text:
                price = _parse_price_to_float(text)
                if price > 0:
                    return price
    # Fallback: scan all spans for a price-like value (digit + currency sign).
    for el in row.find_all("span"):
        text = el.get_text(" ", strip=True)
        if any(c.isdigit() for c in text) and any(s in text for s in ("€", "£", "$")):
            price = _parse_price_to_float(_clean_price_string(text))
            if price > 0:
                return price
    return 0.0


def _is_reverse_holo_url(url: str) -> bool:
    """Return True when *url* contains the ``isReverseHolo=Y`` query parameter."""
    parsed = urlparse(url)
    params = parse_qs(parsed.query, keep_blank_values=True)
    return params.get("isReverseHolo", [""])[0].upper() == "Y"


def _parse_first_listing_price(html: str) -> float | None:
    """Return the price from the very first article row on the page.

    Used for reverse holo pages where the "From" price in the
    ``info-list-container`` may include non-reverse-holo listings, but the
    individual article rows are filtered to reverse-holo-only when the page
    was loaded with ``isReverseHolo=Y``.

    Returns the price (float > 0) of the first article row, or ``None`` when
    no article rows are found.
    """
    soup = BeautifulSoup(html, "html.parser")

    rows: list[Any] = []
    for sel in _ARTICLE_ROW_SELS:
        rows = soup.select(sel)
        if rows:
            break

    if not rows:
        logger.debug("_parse_first_listing_price: no article rows found in page HTML")
        return None

    price = _extract_row_price(rows[0])
    if price > 0:
        logger.debug("_parse_first_listing_price: first listing at €%.2f", price)
        return price

    logger.debug("_parse_first_listing_price: no price found in first row")
    return None


def _parse_psa_listing_price(html: str, psa_grade: int) -> float | None:
    """Scan Cardmarket listing rows for an MT-condition offer matching *psa_grade*.

    For each article row on the product page the function checks:
    1. The row has an MT (Mint) condition badge.
    2. The row text contains a PSA grade that matches *psa_grade* exactly.

    When both conditions are met the price from that row is returned and the
    search stops.  Returns ``None`` when no match is found.
    """
    soup = BeautifulSoup(html, "html.parser")

    rows: list[Any] = []
    for sel in _ARTICLE_ROW_SELS:
        rows = soup.select(sel)
        if rows:
            break

    if not rows:
        logger.debug("_parse_psa_listing_price: no article rows found in page HTML")
        return None

    for row in rows:
        if not _row_has_mint_condition(row):
            continue

        row_text = row.get_text(" ", strip=True)
        grade = extract_psa_grade(row_text)
        if grade != psa_grade:
            continue

        price = _extract_row_price(row)
        if price > 0:
            logger.debug(
                "_parse_psa_listing_price: found PSA %d MT listing at €%.2f",
                psa_grade, price,
            )
            return price

    logger.debug(
        "_parse_psa_listing_price: no MT listing found for PSA %d", psa_grade
    )
    return None


def _extract_json_prices(soup: BeautifulSoup) -> dict[str, Any]:
    """Attempt to extract price metrics from embedded ``<script>`` JSON blobs."""
    prices: dict[str, Any] = {}

    _JSON_KEY_MAP: list[tuple[tuple[str, ...], str]] = [
        (("lowPrice", "lowestPrice", "fromPrice", "minPrice"), "lowest_price"),
        (("trendPrice", "priceTrend", "trend"), "price_trend"),
        (("avg30", "avg30Days", "averagePrice30"), "avg_30_days"),
        (("avg7", "avg7Days", "averagePrice7"), "avg_7_days"),
        (("avg1", "avg1Day", "averagePrice1"), "avg_1_day"),
    ]

    for script_tag in soup.find_all("script"):
        text = script_tag.string or ""
        if not text or len(text) < 20:
            continue

        for match in re.finditer(r"\{[^{}]{20,}\}", text):
            candidate = match.group(0)
            if not any(
                k in candidate
                for keys, _ in _JSON_KEY_MAP
                for k in keys
            ):
                continue
            try:
                obj = json.loads(candidate)
            except (ValueError, json.JSONDecodeError):
                continue

            for json_keys, canonical in _JSON_KEY_MAP:
                if canonical in prices:
                    continue
                for jk in json_keys:
                    raw = obj.get(jk)
                    if raw is None:
                        continue
                    try:
                        v = float(raw)
                    except (ValueError, TypeError):
                        v = _parse_price_to_float(str(raw))
                    if v > 0:
                        prices[canonical] = v
                        break

            if len(prices) >= 3:
                return prices

    return prices


# ---------------------------------------------------------------------------
# Scrape result dataclass
# ---------------------------------------------------------------------------

@dataclass
class CardmarketPriceData:
    """Scraped pricing data from a Cardmarket product page."""

    product_url: str
    product_name: str | None = None
    product_id: str | None = None

    # Core price metrics
    from_price: float | None = None        # "From" / lowest available price
    price_trend: float | None = None       # Reference price trend
    avg_30_days: float | None = None       # 30-day average
    avg_7_days: float | None = None        # 7-day average
    avg_1_day: float | None = None         # 1-day average

    # Dutch-seller availability
    dutch_sellers_available: bool = True   # False when sellerCountry=23 returns no results

    # PSA-specific listing price (populated when a matching MT + PSA-grade offer is found)
    psa_listing_price: float | None = None  # price from the first MT listing matching the PSA grade

    # Metadata
    set_name: str | None = None
    card_number: str | None = None

    def is_valid(self) -> bool:
        """True if the from_price was successfully scraped."""
        return self.from_price is not None and self.from_price > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "product_url": self.product_url,
            "product_name": self.product_name,
            "product_id": self.product_id,
            "from_price": self.from_price,
            "price_trend": self.price_trend,
            "avg_30_days": self.avg_30_days,
            "avg_7_days": self.avg_7_days,
            "avg_1_day": self.avg_1_day,
            "dutch_sellers_available": self.dutch_sellers_available,
            "set_name": self.set_name,
            "card_number": self.card_number,
        }


# ---------------------------------------------------------------------------
# Main scraper class
# ---------------------------------------------------------------------------

class CardmarketScraper:
    """Scrapes Cardmarket product pages for Pokémon prices.

    Reuses an existing Playwright ``Browser`` instance (managed externally).

    Primary entry point:
        ``scrape_url(url)`` – scrape a specific product page by its URL.

    Legacy entry point (search-based, kept for backwards compatibility):
        ``lookup(query)`` – search for a card and scrape the first result.
    """

    def __init__(self, browser: Browser) -> None:
        self._browser = browser

    # ------------------------------------------------------------------
    # Primary API: scrape by product URL
    # ------------------------------------------------------------------

    async def scrape_url(
        self,
        url: str,
        *,
        retry_without_country_filter: bool = True,
    ) -> CardmarketPriceData:
        """Scrape price data from a specific Cardmarket product URL.

        Always adds ``?sellerCountry=23&language=1`` before fetching.
        If the From price cannot be extracted (Dutch sellers unavailable),
        and ``retry_without_country_filter`` is True, retries without the
        seller-country filter and sets ``dutch_sellers_available=False``.

        Raises ``CardmarketScrapeError`` on unrecoverable failure.
        """
        normalised_url = normalize_cardmarket_url(url)
        product_id = _extract_product_id(normalised_url)

        logger.info("Cardmarket: scraping product page %s", normalised_url)

        context = await self._new_context()
        page = await context.new_page()
        await page.add_init_script(_STEALTH_SCRIPT)
        try:
            prices_dict = await self._fetch_product_page(page, normalised_url)
        except Exception as exc:  # noqa: BLE001
            await context.close()
            tb = traceback.format_exc()
            raise CardmarketScrapeError(
                url=normalised_url,
                step="page_fetch",
                message=str(exc),
                stack_trace=tb,
            ) from exc
        finally:
            try:
                await context.close()
            except Exception:  # noqa: BLE001
                pass

        if not prices_dict:
            raise CardmarketScrapeError(
                url=normalised_url,
                step="price_parse",
                message="No pricing data found on page (structure may have changed).",
            )

        dutch_available = True

        # For reverse holo URLs the "From" price in info-list-container can
        # include non-reverse-holo listings.  Use the price from the first
        # article row instead, which is guaranteed to be a reverse-holo offer
        # because the page was loaded with isReverseHolo=Y.
        if _is_reverse_holo_url(normalised_url):
            from_price = prices_dict.get("first_listing_price") or prices_dict.get("lowest_price")
            if from_price:
                logger.info(
                    "Cardmarket: reverse holo – using first listing price €%.2f for %s",
                    from_price, normalised_url,
                )
        else:
            from_price = prices_dict.get("lowest_price")

        # If no From price with Dutch filter, retry without country filter.
        if not from_price and retry_without_country_filter:
            logger.info(
                "Cardmarket: no From price with Dutch filter – retrying without country filter"
            )
            url_no_country = _remove_country_filter(normalised_url)
            context2 = await self._new_context()
            page2 = await context2.new_page()
            await page2.add_init_script(_STEALTH_SCRIPT)
            try:
                prices_dict2 = await self._fetch_product_page(page2, url_no_country)
            except Exception:  # noqa: BLE001
                prices_dict2 = None
            finally:
                try:
                    await context2.close()
                except Exception:  # noqa: BLE001
                    pass

            if prices_dict2 and prices_dict2.get("lowest_price"):
                dutch_available = False
                prices_dict = prices_dict2
                from_price = prices_dict2.get("lowest_price")
                logger.info(
                    "Cardmarket: prices found without Dutch filter (from_price=%.2f)",
                    from_price,
                )

        if not from_price:
            raise CardmarketScrapeError(
                url=normalised_url,
                step="from_price_missing",
                message="From Price element not found on product page.",
            )

        return CardmarketPriceData(
            product_url=normalised_url,
            product_name=prices_dict.get("card_name"),
            product_id=product_id,
            from_price=from_price,
            price_trend=prices_dict.get("price_trend"),
            avg_30_days=prices_dict.get("avg_30_days"),
            avg_7_days=prices_dict.get("avg_7_days"),
            avg_1_day=prices_dict.get("avg_1_day"),
            dutch_sellers_available=dutch_available,
            set_name=prices_dict.get("set_name"),
            card_number=prices_dict.get("card_number"),
        )

    # ------------------------------------------------------------------
    # PSA listing price  (searches individual seller offers)
    # ------------------------------------------------------------------

    async def scrape_psa_listing_price(
        self,
        url: str,
        psa_grade: int,
    ) -> float | None:
        """Fetch the Cardmarket product page and find the first MT listing with *psa_grade*.

        Scans the individual seller offer rows on the product page for a row
        that simultaneously has a Mint (MT) condition badge *and* a PSA grade
        in its description that matches *psa_grade* exactly.

        Returns the price (float > 0) when a match is found, or ``None`` when
        no matching listing is available (caller should fall back to the
        standard From price).
        """
        normalised_url = normalize_cardmarket_url(url)
        logger.info(
            "Cardmarket: searching for PSA %d MT listing at %s", psa_grade, normalised_url
        )

        context = await self._new_context()
        page = await context.new_page()
        await page.add_init_script(_STEALTH_SCRIPT)
        try:
            await page.goto(normalised_url, wait_until="load", timeout=30_000)
            await self._accept_cookies(page)

            # Wait for article/offer rows to be rendered.
            for sel in _ARTICLE_ROW_SELS:
                try:
                    await page.wait_for_selector(sel, timeout=8_000, state="visible")
                    break
                except Exception:  # noqa: BLE001
                    continue
            else:
                # Rows not found via selectors – give JS a moment and continue.
                await asyncio.sleep(random.uniform(1.5, 2.5))

            html = await page.content()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Cardmarket: failed to load page for PSA listing search: %s", exc
            )
            return None
        finally:
            try:
                await context.close()
            except Exception:  # noqa: BLE001
                pass

        price = _parse_psa_listing_price(html, psa_grade)
        if price:
            logger.info(
                "Cardmarket: PSA %d MT listing found at €%.2f for %s",
                psa_grade, price, normalised_url,
            )
        else:
            logger.info(
                "Cardmarket: no PSA %d MT listing found at %s",
                psa_grade, normalised_url,
            )
        return price

    # ------------------------------------------------------------------
    # Legacy search-based API (kept for backwards compatibility)
    # ------------------------------------------------------------------

    async def lookup(self, query: str, sample_size: int = 5) -> dict[str, Any] | None:
        """Return price data for *query* scraped from Cardmarket.

        Performs a two-step search: searches Cardmarket, finds the first
        product link, then scrapes the product page.

        Returns a non-empty ``dict`` on success, ``None`` on failure.
        """
        search_url = _CM_SEARCH_URL.format(query=quote_plus(query))
        logger.info("Cardmarket: searching '%s'", query)

        context = await self._new_context()
        page = await context.new_page()
        await page.add_init_script(_STEALTH_SCRIPT)
        try:
            await page.goto(search_url, wait_until="domcontentloaded", timeout=30_000)
            await self._accept_cookies(page)
            await asyncio.sleep(random.uniform(1.5, 3.0))

            product_url = await self._find_first_product_url(page)
            if not product_url:
                logger.warning("Cardmarket: no product link found for '%s'", query)
                return None

            logger.debug("Cardmarket: navigating to product page %s", product_url)
            prices_dict = await self._fetch_product_page(page, product_url)

            if not prices_dict:
                logger.debug("Cardmarket: no prices parsed for '%s'", query)
                return None

            logger.debug("Cardmarket: '%s' → %s", query, prices_dict)
            return prices_dict

        except Exception as exc:  # noqa: BLE001
            logger.warning("Cardmarket scrape error for '%s': %s", query, exc)
            return None
        finally:
            await context.close()

    async def lookup_url(self, url: str) -> dict[str, Any] | None:
        """Fetch price data directly from a Cardmarket product page URL.

        Returns the raw prices dict on success, or ``None`` on failure.
        """
        url = normalize_cardmarket_url(url)
        logger.info("Cardmarket: fetching prices from URL %s", url)
        context = await self._new_context()
        page = await context.new_page()
        await page.add_init_script(_STEALTH_SCRIPT)
        try:
            prices_dict = await self._fetch_product_page(page, url)
            await self._accept_cookies(page)

            if not prices_dict:
                logger.debug("Cardmarket lookup_url: no prices parsed from %s", url)
                return None

            logger.debug("Cardmarket lookup_url: %s → %s", url, prices_dict)
            return prices_dict

        except Exception as exc:  # noqa: BLE001
            logger.warning("Cardmarket lookup_url error for %s: %s", url, exc)
            return None
        finally:
            await context.close()

    # ------------------------------------------------------------------
    # Internal browser helpers
    # ------------------------------------------------------------------

    async def _fetch_product_page(
        self, page: Page, url: str
    ) -> dict[str, Any] | None:
        """Navigate to *url*, wait for dynamic content, and parse prices.

        When *url* contains ``isReverseHolo=Y`` the method additionally waits
        for the individual article/offer rows to be rendered and records the
        price from the first row as ``first_listing_price`` in the returned
        dict.  This is needed because the summary "From" price in the
        ``info-list-container`` includes non-reverse-holo listings, whereas the
        article rows are already filtered to reverse-holo-only.
        """
        await page.goto(url, wait_until="load", timeout=30_000)
        await self._accept_cookies(page)

        # Wait for the price container to be rendered (dynamic content).
        found = False
        for sel in _PRICE_CONTAINER_SELS:
            try:
                await page.wait_for_selector(sel, timeout=8_000, state="visible")
                found = True
                break
            except Exception:  # noqa: BLE001
                continue

        if not found:
            logger.debug(
                "Cardmarket: price container not visible within timeout for %s", url
            )
            await asyncio.sleep(random.uniform(2.0, 3.5))

        # For reverse holo pages, also wait for individual article rows so that
        # _parse_first_listing_price can read the actual RH offer price.
        if _is_reverse_holo_url(url):
            for sel in _ARTICLE_ROW_SELS:
                try:
                    await page.wait_for_selector(sel, timeout=8_000, state="visible")
                    break
                except Exception:  # noqa: BLE001
                    continue
            else:
                await asyncio.sleep(random.uniform(1.5, 2.5))

        html = await page.content()
        prices_dict = _parse_product_page(html)
        if not prices_dict:
            return None

        if _is_reverse_holo_url(url):
            first_listing = _parse_first_listing_price(html)
            if first_listing is not None:
                prices_dict["first_listing_price"] = first_listing

        return prices_dict

    async def _find_first_product_url(self, page: Page) -> str | None:
        """Return the href of the first product link on the search results page."""
        try:
            await page.wait_for_selector(_PRODUCT_LINK_SEL, timeout=12_000)
        except Exception:  # noqa: BLE001
            return None

        links = await page.query_selector_all(_PRODUCT_LINK_SEL)
        for link in links:
            href = await link.get_attribute("href")
            if not href:
                continue
            if href.count("/") >= _MIN_PRODUCT_PATH_DEPTH:
                full_url = href if href.startswith("http") else _CM_BASE + href
                return full_url
        return None

    async def _accept_cookies(self, page: Page) -> None:
        """Dismiss the Cardmarket cookie / consent banner if present."""
        try:
            btn = page.locator(_COOKIE_SEL)
            if await btn.count() > 0:
                await btn.first.click(timeout=3_000)
                logger.debug("Cardmarket: cookie banner accepted")
                await asyncio.sleep(0.8)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Cardmarket: cookie accept skipped: %s", exc)

    async def _new_context(self) -> BrowserContext:
        widths = [1280, 1366, 1440, 1920]
        w = random.choice(widths)
        chrome_versions = ["124.0.0.0", "125.0.0.0", "131.0.0.0", "132.0.0.0", "136.0.0.0"]
        chrome_ver = random.choice(chrome_versions)
        return await self._browser.new_context(
            viewport={"width": w, "height": int(w * 0.5625)},
            locale="nl-NL",
            timezone_id="Europe/Amsterdam",
            user_agent=(
                f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                f"AppleWebKit/537.36 (KHTML, like Gecko) "
                f"Chrome/{chrome_ver} Safari/537.36"
            ),
            java_script_enabled=True,
            extra_http_headers={
                "Accept-Language": "nl-NL,nl;q=0.9,en-GB;q=0.8,en;q=0.7",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-User": "?1",
                "Sec-Fetch-Dest": "document",
            },
        )


# ---------------------------------------------------------------------------
# Error class
# ---------------------------------------------------------------------------

class CardmarketScrapeError(Exception):
    """Raised when Cardmarket scraping fails with structured context."""

    def __init__(
        self,
        url: str,
        step: str,
        message: str,
        http_status: int | None = None,
        stack_trace: str | None = None,
    ) -> None:
        self.url = url
        self.step = step
        self.message = message
        self.http_status = http_status
        self.stack_trace = stack_trace
        super().__init__(f"Cardmarket scrape aborted [{step}]: {message}")


# ---------------------------------------------------------------------------
# URL utility helpers
# ---------------------------------------------------------------------------

def _extract_product_id(url: str) -> str | None:
    """Extract a product identifier from a Cardmarket product URL path."""
    parsed = urlparse(url)
    path_parts = [p for p in parsed.path.split("/") if p]
    # Product slug is the last path component.
    return path_parts[-1] if path_parts else None


def _remove_country_filter(url: str) -> str:
    """Return *url* with the sellerCountry parameter removed."""
    parsed = urlparse(url)
    params = parse_qs(parsed.query, keep_blank_values=True)
    params.pop("sellerCountry", None)
    new_query = urlencode({k: v[0] for k, v in params.items()})
    return urlunparse(parsed._replace(query=new_query))
