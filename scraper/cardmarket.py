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
import time
import traceback
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote_plus, urlparse, urlunparse, urlencode, parse_qs

import aiohttp
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
# As of 2025 Cardmarket uses <dl class="labeled row mx-auto g-0"> (Bootstrap grid);
# legacy pages used <dl class="info-list-container">.
_PRICE_CONTAINER_SELS = [
    "dl.labeled",
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
    # ---------------------------------------------------------------------------
    # Promos
    # ---------------------------------------------------------------------------
    "WP": "Wizards-Black-Star-Promos",
    "NP": "Nintendo-Black-Star-Promos",
    "DPPR": "DP-Black-Star-Promos",
    "HGSS": "HGSS-Black-Star-Promos",
    "BWP": "BW-Black-Star-Promos",
    "BW": "BW-Black-Star-Promos",
    "XYP": "XY-Black-Star-Promos",
    "XYPR": "XY-Black-Star-Promos",
    "SMP": "SM-Black-Star-Promos",
    "SM": "SM-Black-Star-Promos",
    "SWSHP": "SWSH-Black-Star-Promos",
    "SWSH": "SWSH-Black-Star-Promos",
    "SVP": "SV-Black-Star-Promos",
    "MEP": "MEP-Black-Star-Promos",
    # ---------------------------------------------------------------------------
    # Misc / special sets
    # ---------------------------------------------------------------------------
    "SI": "Southern-Islands",
    "RM": "Pokemon-Rumble",
    "SVE": "SV-Energies",
    # ---------------------------------------------------------------------------
    # Mega Evolution Era (Japanese / special XY-era sets)
    # ---------------------------------------------------------------------------
    "MEG": "Mega-Evolution",
    "PFL": "Phantasmal-Flames",
    "ASC": "Ascended-Heroes",
    "POR": "Perfect-Order",
    "CRI": "Chaos-Rising",
    "PBL": "Pitch-Black",
    # ---------------------------------------------------------------------------
    # Scarlet & Violet Era
    # ---------------------------------------------------------------------------
    "SVI": "Scarlet-Violet",
    "SV1": "Scarlet-Violet",           # numeric alias
    "PAL": "Paldea-Evolved",
    "SV2": "Paldea-Evolved",           # numeric alias
    "OBF": "Obsidian-Flames",
    "SV3": "Obsidian-Flames",          # numeric alias
    "MEW": "151",
    "SV3PT5": "151",                   # numeric alias
    "PAR": "Paradox-Rift",
    "SV4": "Paradox-Rift",             # numeric alias
    "PAF": "Paldean-Fates",
    "SV4PT5": "Paldean-Fates",         # numeric alias
    "TEF": "Temporal-Forces",
    "SV5": "Temporal-Forces",          # numeric alias
    "TWM": "Twilight-Masquerade",
    "SV6": "Twilight-Masquerade",      # numeric alias
    "SFA": "Shrouded-Fable",
    "SV6PT5": "Shrouded-Fable",        # numeric alias
    "SCR": "Stellar-Crown",
    "SV7": "Stellar-Crown",            # numeric alias
    "SSP": "Surging-Sparks",
    "SV8": "Surging-Sparks",           # numeric alias
    "PRE": "Prismatic-Evolutions",
    "SV8PT5": "Prismatic-Evolutions",  # numeric alias
    "JTG": "Journey-Together",
    "SV9": "Journey-Together",         # numeric alias
    "DRI": "Destined-Rivals",
    "SV10": "Destined-Rivals",         # numeric alias
    "BLK": "Black-Bolt",
    "WHT": "White-Flare",
    # ---------------------------------------------------------------------------
    # Sword & Shield Era
    # ---------------------------------------------------------------------------
    "SSH": "Sword-Shield",
    "SWSH1": "Sword-Shield",           # numeric alias
    "RCL": "Rebel-Clash",
    "SWSH2": "Rebel-Clash",            # numeric alias
    "DAA": "Darkness-Ablaze",
    "SWSH3": "Darkness-Ablaze",        # numeric alias
    "CPA": "Champions-Path",
    "VIV": "Vivid-Voltage",
    "SWSH4": "Vivid-Voltage",          # numeric alias
    "SHF": "Shining-Fates",
    "SWSH45": "Shining-Fates",         # numeric alias
    "BST": "Battle-Styles",
    "SWSH5": "Battle-Styles",          # numeric alias
    "CRE": "Chilling-Reign",
    "SWSH6": "Chilling-Reign",         # numeric alias
    "EVS": "Evolving-Skies",
    "SWSH7": "Evolving-Skies",         # numeric alias
    "CEL": "Celebrations",
    "CEL25": "Celebrations",           # numeric alias
    "FST": "Fusion-Strike",
    "SWSH8": "Fusion-Strike",          # numeric alias
    "BRS": "Brilliant-Stars",
    "SWSH9": "Brilliant-Stars",        # numeric alias
    "ASR": "Astral-Radiance",
    "SWSH10": "Astral-Radiance",       # numeric alias
    "PGO": "Pokemon-GO",
    "GO": "Pokemon-GO",                # alias
    "LOR": "Lost-Origin",
    "SWSH11": "Lost-Origin",           # numeric alias
    "SIT": "Silver-Tempest",
    "SWSH12": "Silver-Tempest",        # numeric alias
    "CRZ": "Crown-Zenith",
    # ---------------------------------------------------------------------------
    # Sun & Moon Era
    # ---------------------------------------------------------------------------
    "SUM": "Sun-Moon",
    "SM1": "Sun-Moon",                 # numeric alias
    "GRI": "Guardians-Rising",
    "SM2": "Guardians-Rising",         # numeric alias
    "BUS": "Burning-Shadows",
    "SM3": "Burning-Shadows",          # numeric alias
    "SLG": "Shining-Legends",
    "CIN": "Crimson-Invasion",
    "SM4": "Crimson-Invasion",         # numeric alias
    "UPR": "Ultra-Prism",
    "SM5": "Ultra-Prism",              # numeric alias
    "FLI": "Forbidden-Light",
    "SM6": "Forbidden-Light",          # numeric alias
    "CES": "Celestial-Storm",
    "SM7": "Celestial-Storm",          # numeric alias
    "DRM": "Dragon-Majesty",
    "LOT": "Lost-Thunder",
    "SM8": "Lost-Thunder",             # numeric alias
    "TEU": "Team-Up",
    "SM9": "Team-Up",                  # numeric alias
    "DET": "Detective-Pikachu",
    "UNB": "Unbroken-Bonds",
    "SM10": "Unbroken-Bonds",          # numeric alias
    "UNM": "Unified-Minds",
    "SM11": "Unified-Minds",           # numeric alias
    "HIF": "Hidden-Fates",
    "CEC": "Cosmic-Eclipse",
    "SM12": "Cosmic-Eclipse",          # numeric alias
    # ---------------------------------------------------------------------------
    # XY Era
    # ---------------------------------------------------------------------------
    "XY": "XY",
    "XY1": "XY",                       # numeric alias
    "KSS": "Kalos-Starter-Set",
    "FLF": "Flashfire",
    "FFI": "Furious-Fists",
    "PHF": "Phantom-Forces",
    "PRC": "Primal-Clash",
    "DCR": "Double-Crisis",
    "ROS": "Roaring-Skies",
    "AOR": "Ancient-Origins",
    "BKT": "Breakthrough",
    "BKP": "Breakpoint",
    "GEN": "Generations",
    "FCO": "Fates-Collide",
    "STS": "Steam-Siege",
    "STE": "Steam-Siege",              # alternate alias
    "EVO": "Evolutions",
    # ---------------------------------------------------------------------------
    # Black & White Era
    # ---------------------------------------------------------------------------
    "BLW": "Black-White",
    "EPO": "Emerging-Powers",
    "NVI": "Noble-Victories",
    "NXD": "Next-Destinies",
    "DEX": "Dark-Explorers",
    "DEX2": "Dark-Explorers",          # alternate alias
    "DRX": "Dragons-Exalted",
    "DRV": "Dragon-Vault",
    "BCR": "Boundaries-Crossed",
    "PLS": "Plasma-Storm",
    "PLF": "Plasma-Freeze",
    "PLB": "Plasma-Blast",
    "LTR": "Legendary-Treasures",
    # ---------------------------------------------------------------------------
    # Diamond & Pearl Era
    # ---------------------------------------------------------------------------
    "DP": "Diamond-Pearl",
    "MT": "Mysterious-Treasures",
    "SW": "Secret-Wonders",
    "GE": "Great-Encounters",
    "MD": "Majestic-Dawn",
    "LA": "Legends-Awakened",
    "SF": "Stormfront",
    "PL": "Platinum",
    "PLA": "Platinum",                 # alternate alias
    "PLAT": "Platinum",                # alternate alias
    "RR": "Rising-Rivals",
    "SV": "Supreme-Victors",
    "AR": "Arceus",
    "HS": "HeartGold-SoulSilver",
    "UL": "Unleashed",
    "UD": "Undaunted",
    "TM": "Triumphant",
    "CL": "Call-of-Legends",
    # ---------------------------------------------------------------------------
    # EX Era
    # ---------------------------------------------------------------------------
    "RS": "Ruby-Sapphire",
    "SS": "Sandstorm",
    "DR": "Dragon",
    "MA": "Team-Magma-vs-Team-Aqua",
    "HL": "Hidden-Legends",
    "RG": "FireRed-LeafGreen",
    "FR": "FireRed-LeafGreen",         # alternate alias
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
    # ---------------------------------------------------------------------------
    # Neo Era
    # ---------------------------------------------------------------------------
    "N1": "Neo-Genesis",
    "N2": "Neo-Discovery",
    "N3": "Neo-Revelation",
    "N4": "Neo-Destiny",
    "LC": "Legendary-Collection",
    "EX": "Expedition-Base-Set",
    "AQ": "Aquapolis",
    "SK": "Skyridge",
    # ---------------------------------------------------------------------------
    # Base Set Era
    # ---------------------------------------------------------------------------
    "BS": "Base-Set",
    "JU": "Jungle",
    "FO": "Fossil",
    "B2": "Base-Set-2",
    "TR": "Team-Rocket",
    "G1": "Gym-Heroes",
    "G2": "Gym-Challenge",
    # ---------------------------------------------------------------------------
    # Japanese sets (partial)
    # ---------------------------------------------------------------------------
    "S12a": "VSTAR-Universe",
    "sv2a": "151",                     # Japanese 151 / Pokémon Card 151
    "S9": "Star-Birth",
    "S8b": "VMAX-Climax",
    "S8a": "Incandescent-Arcana",
    # ---------------------------------------------------------------------------
    # French Scarlet & Violet aliases
    # ---------------------------------------------------------------------------
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
    # ---------------------------------------------------------------------------
    # McDonald's Collections (year-based codes used on Cardmarket)
    # ---------------------------------------------------------------------------
    "MCD": "McDonalds-Collection",
    "MCDO": "McDonalds-Collection",
    "MCDP": "McDonalds-Collection",
    "M19": "McDonalds-Collection-2019",
    "M20": "McDonalds-Collection-2020",
    "M21": "McDonalds-Collection-2021",
    "M22": "McDonalds-Collection-2022",
    "M23": "McDonalds-Collection-2023",
    "M24": "McDonalds-Collection-2024",
    "M25": "McDonalds-Collection-2025",
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
    min_condition: int | None = None,
) -> str:
    """Return *url* with the standard Cardmarket filter params appended.

    Adds ``sellerCountry=23`` (Netherlands) and ``language=1`` (English) if
    they are not already present in the query string.  Non-Cardmarket URLs are
    returned unchanged.

    *language* – when provided as a Cardmarket language code string (e.g.
    ``"7"`` for Japanese), overrides the default ``language=1`` param.

    *is_reverse_holo* – when ``True``, appends ``isReverseHolo=Y``.

    *min_condition* – when provided as an integer (1–6), appends
    ``minCondition=<value>`` to filter results by card condition.  Cardmarket
    condition codes: 1=Mint, 2=Near Mint, 3=Excellent, 4=Good, 5=Light Played,
    6=Played.  Pass ``None`` or ``7`` (Poor) to omit the filter, which returns
    listings of all conditions.
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
    # Append minCondition when a condition stricter than Poor is specified.
    if min_condition is not None and 1 <= min_condition <= 6:
        params["minCondition"] = [str(min_condition)]
    new_query = urlencode({k: v[0] for k, v in params.items()})
    return urlunparse(parsed._replace(query=new_query))


async def validate_cardmarket_url(url: str) -> bool:
    """Perform a lightweight HEAD request to check that *url* resolves on Cardmarket.

    Returns ``True`` if the URL returns a 2xx/3xx status, or ``False`` when a
    404 is received.  A 403 is treated as valid because Cardmarket frequently
    blocks automated HEAD requests with 403 even for existing product pages.
    Network errors are treated as *not* resolved (returns ``False``) and are
    logged at WARNING level rather than raising.

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
                if resp.status == 403:
                    # Cardmarket blocks automated HEAD requests with 403 even for
                    # valid product pages; treat as URL exists.
                    logger.debug(
                        "validate_cardmarket_url: 403 (anti-bot block) for %s — treating as valid",
                        url,
                    )
                    return True
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
    price_data: dict[str, str] = {}

    # ── Strategy 1: rendered info-list-container ──────────────────────────
    dl = _find_price_dl(soup)
    if dl:
        dt_elements = dl.select("dt")
        dd_elements = dl.select("dd")

        for i in range(min(len(dt_elements), len(dd_elements))):
            key = dt_elements[i].get_text(" ", strip=True)
            value = _extract_price_from_dd(dd_elements[i])
            price_data[key] = value
            logger.debug("Cardmarket dt/dd: %r → %r", key, value)

        # Lowest / From price
        # Old labels (pre-2025): "From" / "De" / "Ab" / "Vanaf" / "Fra"
        # New labels (2025+):    "Lowest" / "Lowest price" and translations
        #   NL: "Laagste" / "Laagste prijs"
        #   DE: "Niedrigster" / "Niedrigster Preis"
        #   FR: "Le plus bas" / "Prix le plus bas"
        _LOWEST_LABELS = frozenset((
            "from", "de", "ab", "vanaf", "fra", "od", "da", "vanuit",
            "lowest", "lowest price",
            "laagste", "laagste prijs",
            "niedrigster", "niedrigster preis",
            "le plus bas", "prix le plus bas",
        ))
        for key, value in price_data.items():
            k_lower = key.lower()
            if k_lower in _LOWEST_LABELS or k_lower.startswith("lowest"):
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

    # dl was found but none of its dt/dd pairs matched known price labels.
    if dl:
        price_data_repr = {k: v for k, v in price_data.items()}
        logger.warning(
            "Cardmarket: price dl found but no prices extracted. "
            "dl classes=%r  dt/dd pairs: %s",
            " ".join(dl.get("class") or []),
            price_data_repr,
        )

    logger.warning("Cardmarket: no prices from dl strategy – trying JSON fallback")

    # ── Strategy 2: embedded JSON in <script> tags ────────────────────────
    prices = _extract_json_prices(soup)
    if not prices:
        logger.warning("Cardmarket: no pricing data found in page HTML or embedded JSON")
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
# Diagnostic logging helpers
# ---------------------------------------------------------------------------

# Known patterns in page titles/body that indicate the bot is being blocked.
_BLOCK_PATTERNS = (
    "just a moment",
    "checking your browser",
    "access denied",
    "403 forbidden",
    "cloudflare",
    "ddos-guard",
    "please wait",
    "enable javascript",
    "captcha",
    "security check",
    "ray id",
    "cf-browser-verification",
)


def _log_parse_diagnostics(html: str, url: str, page_title: str | None = None) -> None:
    """Log detailed diagnostics when price parsing fails.

    Inspects *html* and emits WARNING-level logs covering:
    - The page title and any detected block/bot-protection signals.
    - The actual URL (useful when Cardmarket redirected the request).
    - All ``<dl>`` elements found on the page and their class attributes.
    - Every ``<dt>``/``<dd>`` pair discovered in the document.
    - The first 1 500 characters of visible body text.

    Also saves the full raw HTML to a temporary file under ``/tmp/`` so it can
    be inspected manually or sent back for analysis.
    """
    soup = BeautifulSoup(html, "html.parser")

    # ── 1. Page title ─────────────────────────────────────────────────────
    title_el = soup.find("title")
    title_text = (page_title or (title_el.get_text(" ", strip=True) if title_el else "")) or "(no title)"
    logger.warning("Cardmarket [diag] page title: %r  |  url: %s", title_text, url)

    # ── 2. Bot/block detection ────────────────────────────────────────────
    title_lower = title_text.lower()
    body_sample = soup.get_text(" ", strip=True)[:3000].lower()
    detected_signals = [p for p in _BLOCK_PATTERNS if p in title_lower or p in body_sample]
    if detected_signals:
        logger.warning(
            "Cardmarket [diag] bot-block signals detected: %s", detected_signals
        )
    else:
        logger.warning("Cardmarket [diag] no bot-block signals detected in page content")

    # ── 3. All <dl> elements and their classes ────────────────────────────
    all_dls = soup.find_all("dl")
    if all_dls:
        for i, dl in enumerate(all_dls):
            classes = " ".join(dl.get("class") or [])
            logger.warning(
                "Cardmarket [diag] dl[%d] classes=%r  (dt count=%d, dd count=%d)",
                i, classes, len(dl.select("dt")), len(dl.select("dd")),
            )
    else:
        logger.warning("Cardmarket [diag] no <dl> elements found on page")

    # ── 4. All dt/dd pairs in the document ───────────────────────────────
    dt_els = soup.find_all("dt")
    dd_els = soup.find_all("dd")
    if dt_els:
        logger.warning(
            "Cardmarket [diag] %d <dt> elements found across whole page:", len(dt_els)
        )
        for i, (dt, dd) in enumerate(zip(dt_els, dd_els)):
            dt_text = dt.get_text(" ", strip=True)
            dd_text = dd.get_text(" ", strip=True)
            logger.warning("  dt[%d]: %r  →  dd: %r", i, dt_text, dd_text)
        if len(dt_els) > len(dd_els):
            for i, dt in enumerate(dt_els[len(dd_els):], start=len(dd_els)):
                logger.warning("  dt[%d]: %r  →  dd: (no matching dd)", i, dt.get_text(" ", strip=True))
    else:
        logger.warning("Cardmarket [diag] no <dt> elements found on page")

    # ── 5. First 1 500 chars of body text ─────────────────────────────────
    visible_text = soup.get_text(" ", strip=True)
    snippet = visible_text[:1500]
    logger.warning("Cardmarket [diag] body text (first 1500 chars):\n%s", snippet)

    # ── 6. Save raw HTML to a temp file ──────────────────────────────────
    try:
        ts = int(time.time())
        fname = f"/tmp/cardmarket_debug_{ts}.html"
        with open(fname, "w", encoding="utf-8") as fh:
            fh.write(html)
        logger.warning(
            "Cardmarket [diag] full page HTML saved to: %s  (size=%d bytes)",
            fname, len(html),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Cardmarket [diag] could not save debug HTML: %s", exc)


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
        # Persisted storage state (cookies + localStorage) from a successful
        # scrape.  Re-used when creating new contexts so that Cloudflare's
        # cf_clearance cookie survives across requests.
        self._saved_storage_state: dict | None = None

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
        try:
            prices_dict = await self._fetch_product_page(page, normalised_url)
            # Persist cookies/storage so Cloudflare recognises us on the next request.
            try:
                self._saved_storage_state = await context.storage_state()
            except Exception:  # noqa: BLE001
                pass
        except Exception as exc:  # noqa: BLE001
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
            try:
                prices_dict2 = await self._fetch_product_page(page2, url_no_country)
                try:
                    self._saved_storage_state = await context2.storage_state()
                except Exception:  # noqa: BLE001
                    pass
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

        Tries FlareSolverr first; falls back to Playwright when unavailable.

        Returns the price (float > 0) when a match is found, or ``None`` when
        no matching listing is available (caller should fall back to the
        standard From price).
        """
        normalised_url = normalize_cardmarket_url(url)
        logger.info(
            "Cardmarket: searching for PSA %d MT listing at %s", psa_grade, normalised_url
        )

        # ── FlareSolverr path ─────────────────────────────────────────────
        fs_html = await self._fetch_via_flaresolverr(normalised_url)
        if fs_html:
            price = _parse_psa_listing_price(fs_html, psa_grade)
            if price:
                logger.info(
                    "Cardmarket [FlareSolverr]: PSA %d MT listing found at €%.2f for %s",
                    psa_grade, price, normalised_url,
                )
            else:
                logger.info(
                    "Cardmarket [FlareSolverr]: no PSA %d MT listing found at %s",
                    psa_grade, normalised_url,
                )
            return price

        # ── Playwright fallback ───────────────────────────────────────────
        context = await self._new_context()
        page = await context.new_page()
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

        Tries FlareSolverr for the search step and the subsequent product page
        fetch when available.  Falls back to Playwright when FlareSolverr is
        not reachable or does not return usable results.

        Returns a non-empty ``dict`` on success, ``None`` on failure.
        """
        search_url = _CM_SEARCH_URL.format(query=quote_plus(query))
        logger.info("Cardmarket: searching '%s'", query)

        # ── FlareSolverr path ─────────────────────────────────────────────
        fs_search_html = await self._fetch_via_flaresolverr(search_url)
        if fs_search_html:
            product_url = _extract_first_product_url_from_html(fs_search_html)
            if product_url:
                logger.debug(
                    "Cardmarket [FlareSolverr]: found product URL %s for '%s'",
                    product_url, query,
                )
                # _fetch_product_page tries FlareSolverr first; only uses the
                # Playwright page when FlareSolverr is unavailable or fails.
                fs_ctx = await self._new_context()
                fs_page = await fs_ctx.new_page()
                try:
                    prices_dict = await self._fetch_product_page(fs_page, product_url)
                finally:
                    try:
                        await fs_ctx.close()
                    except Exception:  # noqa: BLE001
                        pass
                if prices_dict:
                    logger.debug(
                        "Cardmarket [FlareSolverr]: '%s' → %s", query, prices_dict
                    )
                    return prices_dict
                logger.warning(
                    "Cardmarket [FlareSolverr]: no prices from product page for '%s' – "
                    "falling back to Playwright",
                    query,
                )
            else:
                logger.warning(
                    "Cardmarket [FlareSolverr]: no product link in search results for "
                    "'%s' – falling back to Playwright",
                    query,
                )

        # ── Playwright fallback ───────────────────────────────────────────
        context = await self._new_context()
        page = await context.new_page()
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

        Tries FlareSolverr first (if configured) to bypass Cloudflare without
        launching a full Playwright browser session.  If FlareSolverr returns
        usable HTML the result is parsed immediately and the Playwright path is
        skipped entirely.

        When FlareSolverr is unavailable or returns no prices, falls back to
        the existing Playwright flow which pre-warms the browser context by
        visiting the Cardmarket homepage first so that Cloudflare session
        cookies are established before hitting the product page URL.

        When *url* contains ``isReverseHolo=Y`` the method additionally waits
        for the individual article/offer rows to be rendered and records the
        price from the first row as ``first_listing_price`` in the returned
        dict.  This is needed because the summary "From" price in the
        ``info-list-container`` includes non-reverse-holo listings, whereas the
        article rows are already filtered to reverse-holo-only.
        """
        # ── FlareSolverr path ─────────────────────────────────────────────
        fs_html = await self._fetch_via_flaresolverr(url)
        if fs_html:
            prices_dict = _parse_product_page(fs_html)
            if prices_dict:
                logger.info(
                    "Cardmarket [FlareSolverr]: prices parsed for %s", url
                )
                if _is_reverse_holo_url(url):
                    first_listing = _parse_first_listing_price(fs_html)
                    if first_listing is not None:
                        prices_dict["first_listing_price"] = first_listing
                return prices_dict
            logger.warning(
                "Cardmarket [FlareSolverr]: HTML received but no prices parsed for %s "
                "– falling back to Playwright",
                url,
            )
            _log_parse_diagnostics(fs_html, url)

        # ── Playwright fallback ───────────────────────────────────────────
        # Pre-warm: visit the Cardmarket category page first so that Cloudflare
        # sets the cf_clearance session cookie before we request the product page.
        try:
            await page.goto(
                "https://www.cardmarket.com/en/Pokemon/Products/Singles",
                wait_until="domcontentloaded",
                timeout=20_000,
            )
            await self._accept_cookies(page)
            # Give Cloudflare time to set session cookies after the first page.
            await asyncio.sleep(random.uniform(3.0, 5.0))
            # Brief scroll to simulate human behaviour before navigating on.
            await page.evaluate(
                "window.scrollTo({top: Math.floor(Math.random()*300+100), behavior:'smooth'})"
            )
            await asyncio.sleep(random.uniform(1.0, 2.0))
        except Exception:  # noqa: BLE001
            pass  # Pre-warm is best-effort; continue regardless

        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        await self._accept_cookies(page)

        # Detect Cloudflare challenge page ("Just a moment…") and wait for it
        # to auto-resolve.  Cloudflare's passive JS challenge typically resolves
        # within a few seconds when the browser executes the challenge script.
        for _attempt in range(4):
            title = await page.title()
            if "just a moment" not in title.lower() and "checking your browser" not in title.lower():
                break
            logger.warning(
                "Cardmarket: Cloudflare challenge detected (%r) – waiting for resolution … (attempt %d/4)",
                title, _attempt + 1,
            )
            # Simulate reading / scrolling while waiting for the JS challenge.
            await asyncio.sleep(random.uniform(2.0, 3.0))
            try:
                await page.evaluate(
                    "window.scrollTo({top: Math.floor(Math.random()*200+50), behavior:'smooth'})"
                )
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(random.uniform(6.0, 10.0))
        else:
            logger.warning(
                "Cardmarket: Cloudflare challenge did not resolve after retries for %s", url
            )

        # Log page title and actual URL after potential redirect.
        _page_title = await page.title()
        _actual_url = page.url
        logger.info(
            "Cardmarket: page loaded – title=%r  actual_url=%s", _page_title, _actual_url
        )

        # Wait for the price container to be rendered (dynamic content).
        found = False
        for sel in _PRICE_CONTAINER_SELS:
            try:
                await page.wait_for_selector(sel, timeout=8_000, state="visible")
                found = True
                logger.info("Cardmarket: price container found via selector %r", sel)
                break
            except Exception:  # noqa: BLE001
                continue

        if not found:
            logger.warning(
                "Cardmarket: price container not visible within timeout for %s "
                "(tried selectors: %s)",
                url, _PRICE_CONTAINER_SELS,
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
            logger.warning(
                "Cardmarket: _parse_product_page returned no prices for %s – running diagnostics",
                url,
            )
            _log_parse_diagnostics(html, _actual_url, _page_title)
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

    # ------------------------------------------------------------------
    # FlareSolverr integration
    # ------------------------------------------------------------------

    async def _fetch_via_flaresolverr(self, url: str) -> str | None:
        """Fetch *url* via FlareSolverr and return the rendered HTML.

        Sends a ``request.get`` command to the FlareSolverr API, which uses a
        real headless browser to bypass Cloudflare and similar bot-protection
        systems.  Returns the rendered HTML string on success, or ``None`` when
        FlareSolverr is unavailable or the request fails.

        The FlareSolverr URL is read from ``settings.flaresolverr_url``
        (``FLARESOLVERR_URL`` env var or config default ``http://localhost:8191``).
        """
        from config.settings import settings  # noqa: PLC0415 (lazy to avoid circular import)
        flaresolverr_url = settings.flaresolverr_url
        if not flaresolverr_url:
            return None

        api_url = f"{flaresolverr_url.rstrip('/')}/v1"
        payload = {
            "cmd": "request.get",
            "url": url,
            "maxTimeout": 60000,
        }

        try:
            timeout = aiohttp.ClientTimeout(total=70)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(api_url, json=payload) as resp:
                    if resp.status != 200:
                        logger.warning(
                            "Cardmarket [FlareSolverr]: HTTP %d from API for %s",
                            resp.status, url,
                        )
                        return None
                    data = await resp.json(content_type=None)
                    if data.get("status") == "ok":
                        html = data.get("solution", {}).get("response", "")
                        if html:
                            logger.info(
                                "Cardmarket [FlareSolverr]: successfully fetched %s "
                                "(solution status %s)",
                                url, data.get("status"),
                            )
                            # Persist cookies returned by FlareSolverr so that
                            # subsequent Playwright contexts can reuse them.
                            solution_cookies = data.get("solution", {}).get("cookies", [])
                            if solution_cookies and not self._saved_storage_state:
                                # Convert FlareSolverr cookie dicts to Playwright format.
                                pw_cookies: list[dict] = []
                                for c in solution_cookies:
                                    pw_cookies.append({
                                        "name": c.get("name", ""),
                                        "value": c.get("value", ""),
                                        "domain": c.get("domain", ".cardmarket.com"),
                                        "path": c.get("path", "/"),
                                        "secure": bool(c.get("secure", False)),
                                        "httpOnly": bool(c.get("httpOnly", False)),
                                    })
                                self._saved_storage_state = {"cookies": pw_cookies}
                            return html
                        logger.warning(
                            "Cardmarket [FlareSolverr]: empty response body for %s", url
                        )
                    else:
                        logger.warning(
                            "Cardmarket [FlareSolverr]: non-ok status for %s – %s",
                            url, data.get("message", data.get("status")),
                        )
        except asyncio.TimeoutError:
            logger.warning("Cardmarket [FlareSolverr]: timeout fetching %s", url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Cardmarket [FlareSolverr]: request failed for %s – %s", url, exc)
        return None

    async def _new_context(self) -> BrowserContext:
        widths = [1280, 1366, 1440, 1920]
        w = random.choice(widths)
        ctx = await self._browser.new_context(
            viewport={"width": w, "height": int(w * 0.5625)},
            locale="nl-NL",
            timezone_id="Europe/Amsterdam",
            java_script_enabled=True,
            extra_http_headers={
                "Accept-Language": "nl-NL,nl;q=0.9,en-GB;q=0.8,en;q=0.7",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
                "Upgrade-Insecure-Requests": "1",
            },
        )
        # Restore persisted cookies/storage so Cloudflare recognises the browser
        # across requests and does not re-challenge on every new context.
        if self._saved_storage_state:
            try:
                cookies = self._saved_storage_state.get("cookies", [])
                if cookies:
                    await ctx.add_cookies(cookies)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Cardmarket: could not restore saved cookies: %s", exc)
        return ctx


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


# ---------------------------------------------------------------------------
# URL variant helpers
# ---------------------------------------------------------------------------

# Matches a slug that already has a version prefix, e.g. "Salazzle-V1-ASC224".
# Groups: card (everything before the version), ver (digit(s)), suffix (code+number).
_SLUG_VERSION_RE = re.compile(
    r'^(?P<card>.+)-V(?P<ver>\d+)-(?P<suffix>[A-Z]{0,6}\d{2,4})$',
)

# Matches the number-suffix part of a product slug, e.g. "ASC224" or "044".
_SLUG_NUM_SUFFIX_RE = re.compile(r'^[A-Z]{0,6}\d{2,4}$')

# Number of numbered variants (V1…V_N) to probe beside the base slug.
_VARIANT_COUNT = 2


def generate_variant_urls(url: str) -> list[str]:
    """Return alternative URL variants for a Cardmarket product page.

    Cardmarket can list multiple print versions of the same card under
    different product slugs, e.g. ``Salazzle-ASC224``, ``Salazzle-V1-ASC224``,
    ``Salazzle-V2-ASC224``.  Given any one of these URLs this function returns
    the *other* variants so callers can probe all of them.

    Returns an empty list when the URL does not match the expected pattern.

    Examples::

        generate_variant_urls(".../Salazzle-ASC224?...")
        → [".../Salazzle-V1-ASC224?...", ".../Salazzle-V2-ASC224?..."]

        generate_variant_urls(".../Salazzle-V1-ASC224?...")
        → [".../Salazzle-ASC224?...", ".../Salazzle-V2-ASC224?..."]
    """
    parsed = urlparse(url)
    path_parts = [p for p in parsed.path.split("/") if p]
    if not path_parts:
        return []

    product_slug = path_parts[-1]
    parent_path = "/" + "/".join(path_parts[:-1])

    # Check whether the slug already contains a version prefix (e.g. "-V1-").
    m = _SLUG_VERSION_RE.match(product_slug)
    if m:
        card_part = m.group("card")
        num_suffix = m.group("suffix")
    else:
        # Scan right-to-left for the first hyphen-delimited segment that looks
        # like a set-code+number suffix (e.g. "ASC224" or "044").
        parts = product_slug.split("-")
        suffix_idx: int | None = None
        for i in range(len(parts) - 1, 0, -1):
            if _SLUG_NUM_SUFFIX_RE.match(parts[i]):
                suffix_idx = i
                break
        if suffix_idx is None:
            return []
        card_part = "-".join(parts[:suffix_idx])
        num_suffix = parts[suffix_idx]

    # Build all candidate slugs: base + V1…V{_VARIANT_COUNT}.
    all_slugs = [f"{card_part}-{num_suffix}"]
    for v in range(1, _VARIANT_COUNT + 1):
        all_slugs.append(f"{card_part}-V{v}-{num_suffix}")

    # Return every variant *except* the one that matches the input URL.
    result: list[str] = []
    for slug in all_slugs:
        if slug.lower() == product_slug.lower():
            continue
        new_path = f"{parent_path}/{slug}"
        result.append(urlunparse(parsed._replace(path=new_path)))

    return result


def _extract_first_product_url_from_html(html: str) -> str | None:
    """Extract the first Cardmarket product URL from a search-results HTML string.

    Mirrors the logic of ``CardmarketScraper._find_first_product_url`` but
    operates on raw HTML rather than a live Playwright page, so it can be used
    with HTML returned by FlareSolverr.
    """
    soup = BeautifulSoup(html, "html.parser")
    for link in soup.select(_PRODUCT_LINK_SEL):
        href = link.get("href", "")
        if not href:
            continue
        if href.count("/") >= _MIN_PRODUCT_PATH_DEPTH:
            full_url = href if href.startswith("http") else _CM_BASE + href
            return full_url
    return None



    """Return *url* with the sellerCountry parameter removed."""
    parsed = urlparse(url)
    params = parse_qs(parsed.query, keep_blank_values=True)
    params.pop("sellerCountry", None)
    new_query = urlencode({k: v[0] for k, v in params.items()})
    return urlunparse(parsed._replace(query=new_query))
