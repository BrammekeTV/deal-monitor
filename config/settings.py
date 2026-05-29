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
        self.discord_webhook_url: str | None = os.getenv("DISCORD_WEBHOOK_URL")

        # --- Scraper ---
        s = raw.get("scraper", {})
        self.interval_min: int = int(s.get("interval_min", 60))
        self.interval_max: int = int(s.get("interval_max", 300))
        self.results_per_term: int = int(s.get("results_per_term", 30))
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
        self.min_score: int = int(d.get("min_score", 30))
        self.discount_threshold_pct: float = float(d.get("discount_threshold_pct", 20))
        self.positive_keywords: list[str] = [
            kw.lower() for kw in d.get("positive_keywords", [])
        ]
        self.blacklist_keywords: list[str] = [
            kw.lower() for kw in d.get("blacklist_keywords", [])
        ]
        self.min_seller_rating: float = float(d.get("min_seller_rating", 0.0))
        self.bundle_keywords: list[str] = [
            kw.lower() for kw in d.get("bundle_keywords", [])
        ]

        # --- Market values (title substring → float EUR) ---
        self.market_values: dict[str, float] = {
            k.lower(): float(v) for k, v in raw.get("market_values", {}).items()
        }

        # --- Price lookup ---
        pl = raw.get("price_lookup", {})
        self.price_lookup_cache_ttl: int = int(pl.get("cache_ttl", 3600))

        ebay_cfg = pl.get("ebay", {})
        self.ebay_enabled: bool = bool(ebay_cfg.get("enabled", True))
        self.ebay_sample_size: int = int(ebay_cfg.get("sample_size", 10))
        self.ebay_site_id: int = int(ebay_cfg.get("site_id", 3))
        self.ebay_app_id: str | None = os.getenv("EBAY_APP_ID")

        cm_cfg = pl.get("cardmarket", {})
        self.cardmarket_enabled: bool = bool(cm_cfg.get("enabled", True))
        self.cardmarket_sample_size: int = int(cm_cfg.get("sample_size", 5))

        # --- Discord presentation ---
        disc = raw.get("discord", {})
        self.embed_colour: int = int(disc.get("embed_colour", 0x00FF7F))
        self.use_webhook: bool = bool(disc.get("use_webhook", False))

    def reload(self) -> None:
        """Reload settings from disk (useful for hot-reloading config)."""
        self._raw = _load_yaml(_CONFIG_PATH)
        self._load()


# Singleton – import this everywhere.
settings = Settings()
