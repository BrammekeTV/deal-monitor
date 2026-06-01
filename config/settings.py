"""
config/settings.py
~~~~~~~~~~~~~~~~~~
Loads configuration from config/config.yaml and overrides with environment
variables where applicable.  All other modules should import `settings` from
here rather than reading config directly.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# Load .env file (silently ignored if it does not exist).
load_dotenv()

_CONFIG_PATH = Path(__file__).parent / "config.yaml"


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _deep_get(d: dict, *keys: str, default: Any = None) -> Any:
    """Safely traverse nested dict keys."""
    for key in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(key, default)  # type: ignore[assignment]
    return d


class Settings:
    """Central configuration object.

    Attributes are populated from config.yaml first, then overridden by
    environment variables so that Docker / CI environments stay clean.
    """

    def __init__(self) -> None:
        self._raw: dict[str, Any] = _load_yaml(_CONFIG_PATH)
        self._load()

    def _load(self) -> None:
        raw = self._raw

        # --- Secrets (always from env) ---
        self.discord_token: str = os.environ["DISCORD_BOT_TOKEN"]
        self.discord_channel_id: int = int(
            os.getenv("DISCORD_CHANNEL_ID")
            or _deep_get(raw, "discord", "channel_id", default=0)
        )
        self.discord_match_channel_id: int = int(
            os.getenv("DISCORD_MATCH_CHANNEL_ID")
            or _deep_get(raw, "discord", "match_channel_id", default=0)
        )
        self.discord_review_channel_id: int = int(
            os.getenv("DISCORD_REVIEW_CHANNEL_ID")
            or _deep_get(raw, "discord", "review_channel_id", default=0)
        )
        self.discord_unidentified_channel_id: int = int(
            os.getenv("DISCORD_UNIDENTIFIED_CHANNEL_ID")
            or _deep_get(raw, "discord", "unidentified_channel_id", default=0)
        )
        self.discord_log_channel_id: int = int(
            os.getenv("DISCORD_LOG_CHANNEL_ID")
            or _deep_get(raw, "discord", "log_channel_id", default=0)
        )
        self.discord_status_channel_id: int = int(
            os.getenv("DISCORD_STATUS_CHANNEL_ID")
            or _deep_get(raw, "discord", "status_channel_id", default=0)
        )
        self.discord_guild_id: int = int(
            os.getenv("DISCORD_GUILD_ID")
            or _deep_get(raw, "discord", "guild_id", default=0)
        )

        # --- Scraper ---
        s = raw.get("scraper", {})
        self.interval_min: int = int(s.get("interval_min", 60))
        self.interval_max: int = int(s.get("interval_max", 300))
        self.results_per_term: int = int(s.get("results_per_term", 30))
        self.review_queue_expiry_days: int = int(s.get("review_queue_expiry_days", 30))
        self.headless: bool = bool(s.get("headless", True))
        self.browser: str = s.get("browser", "chromium")
        self.page_delay_min: float = float(s.get("page_delay_min", 2.0))
        self.page_delay_max: float = float(s.get("page_delay_max", 5.0))
        self.max_retries: int = int(s.get("max_retries", 3))
        self.retry_delay: int = int(s.get("retry_delay", 10))
        self.countries: list[str] = [c.upper() for c in s.get("countries", [])]

        # --- Search terms ---
        self.search_terms: list[str] = raw.get("search_terms", ["Pokemon"])

        # --- Deal detection ---
        d = raw.get("deal", {})
        self.max_price: float = float(d.get("max_price", 500.0))
        self.blacklist_keywords: list[str] = [
            kw.lower() for kw in d.get("blacklist_keywords", [])
        ]
        self.min_seller_rating: float = float(d.get("min_seller_rating", 0.0))

        # --- Cardmarket ---
        cm = raw.get("cardmarket", {})
        self.cardmarket_enabled: bool = bool(cm.get("enabled", True))
        self.cardmarket_fuzzy_threshold: float = float(cm.get("fuzzy_threshold", 80.0))

        # --- Cardmarket Product Catalog ---
        # When enabled, prices are fetched from the Cardmarket S3 JSON files
        # instead of scraping individual product pages via Flaresolverr / Playwright.
        # Set to false to fall back to browser-based scraping for every listing.
        catalog = raw.get("catalog", {})
        self.catalog_enabled: bool = bool(
            os.getenv("CATALOG_ENABLED", str(catalog.get("enabled", True))).lower()
            not in ("0", "false", "no")
        )
        # How often to refresh the cached catalog files (hours).  Set to 0 to
        # never refresh after the initial download.
        self.catalog_refresh_hours: int = int(
            os.getenv("CATALOG_REFRESH_HOURS", str(catalog.get("refresh_hours", 24)))
        )
        # Directory where catalog JSON files are cached on disk.
        self.catalog_cache_dir: str = os.getenv(
            "CATALOG_CACHE_DIR",
            str(catalog.get("cache_dir", "data/catalog_cache")),
        )

        # --- FlareSolverr ---
        # Can be overridden via FLARESOLVERR_URL env var.
        # Default: localhost for local development; Docker Compose sets this
        # automatically to http://flaresolverr:8191 via the environment block.
        self.flaresolverr_url: str = os.getenv(
            "FLARESOLVERR_URL",
            _deep_get(raw, "flaresolverr", "url", default="http://localhost:8191"),
        )

        # --- Byparr ---
        # Byparr (https://github.com/ThePhaseless/Byparr) is used as a fallback
        # when FlareSolverr returns a 500 or fails.  Uses the same API as
        # FlareSolverr so it is a transparent drop-in.
        # Default: http://localhost:8192 (Byparr's default port).
        self.byparr_url: str = os.getenv(
            "BYPARR_URL",
            _deep_get(raw, "byparr", "url", default="http://localhost:8192"),
        )

        # When True, Playwright is used as a fallback when FlareSolverr is
        # configured but does not return usable results.  Defaults to False
        # because FlareSolverr is faster; set PLAYWRIGHT_FALLBACK=true or
        # scraper.playwright_fallback: true in config.yaml to opt in.
        _pw_fallback_env = os.getenv("PLAYWRIGHT_FALLBACK", "").lower()
        if _pw_fallback_env in ("1", "true", "yes"):
            self.playwright_fallback: bool = True
        elif _pw_fallback_env in ("0", "false", "no"):
            self.playwright_fallback = False
        else:
            self.playwright_fallback = bool(s.get("playwright_fallback", False))

        # --- Discord presentation ---
        disc = raw.get("discord", {})
        self.embed_colour: int = int(disc.get("embed_colour", 0x00FF7F))

    def reload(self) -> None:
        """Reload settings from disk (useful for hot-reloading config)."""
        self._raw = _load_yaml(_CONFIG_PATH)
        self._load()


# Singleton – import this everywhere.
settings = Settings()
