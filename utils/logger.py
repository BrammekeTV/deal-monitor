"""
utils/logger.py
~~~~~~~~~~~~~~~
Centralised logging configuration.

Usage::

    from utils.logger import get_logger
    logger = get_logger(__name__)
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_LOG_DIR = Path("logs")
_LOG_FILE = _LOG_DIR / "deal_monitor.log"

_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DATE_FMT = "%Y-%m-%d %H:%M:%S"

_configured = False


def configure_logging(level: str = "INFO") -> None:
    """Call once at startup to set up file + console handlers."""
    global _configured  # noqa: PLW0603
    if _configured:
        return
    _configured = True

    _LOG_DIR.mkdir(exist_ok=True)

    numeric_level = getattr(logging, level.upper(), logging.INFO)

    formatter = logging.Formatter(_FMT, datefmt=_DATE_FMT)

    # Console handler (stdout so Docker log drivers capture it).
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(numeric_level)

    # Rotating file handler.
    from logging.handlers import RotatingFileHandler

    file_handler = RotatingFileHandler(
        _LOG_FILE,
        maxBytes=5 * 1024 * 1024,  # 5 MB
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(numeric_level)

    root = logging.getLogger()
    root.setLevel(numeric_level)
    root.addHandler(console_handler)
    root.addHandler(file_handler)

    # Silence noisy libraries at WARNING level.
    for noisy in ("asyncio", "discord.gateway", "discord.http", "urllib3", "aiohttp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Return a named logger (initialises root config on first call)."""
    configure_logging()
    return logging.getLogger(name)
