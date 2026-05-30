"""
database/db.py
~~~~~~~~~~~~~~
Async SQLite database for the deal-monitor bot.

Tables:
  seen_listings       – track processed Vinted listing IDs (deduplication)
  card_mappings       – learning database: Vinted title/fingerprint → Cardmarket URL
  review_queue        – listings pending manual Discord review
  error_log           – structured Cardmarket scraping / processing errors
  filter_settings     – runtime-adjustable bot settings
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from utils.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS seen_listings (
    listing_id   TEXT PRIMARY KEY,
    title        TEXT,
    url          TEXT,
    price        REAL,
    currency     TEXT,
    seller_name  TEXT,
    fingerprint  TEXT,
    processed_at TEXT
);

CREATE TABLE IF NOT EXISTS card_mappings (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    -- Original Vinted data
    vinted_title             TEXT    NOT NULL,
    vinted_url               TEXT,
    vinted_description       TEXT,
    seller_name              TEXT,
    category                 TEXT,
    price                    REAL,
    -- Extracted card data
    card_name                TEXT,
    set_name                 TEXT,
    set_code                 TEXT,
    collector_number         TEXT,
    rarity                   TEXT,
    language                 TEXT,
    edition                  TEXT,
    fingerprint              TEXT,
    -- Cardmarket data
    cardmarket_url           TEXT    NOT NULL,
    cardmarket_product_id    TEXT,
    cardmarket_product_name  TEXT,
    -- Matching metadata
    tokens                   TEXT,       -- JSON list of keywords
    confidence               REAL    DEFAULT 1.0,
    validated_by             TEXT,       -- 'auto' | 'user:username'
    date_added               TEXT,
    date_updated             TEXT
);

CREATE INDEX IF NOT EXISTS idx_card_mappings_title ON card_mappings(vinted_title);
CREATE INDEX IF NOT EXISTS idx_card_mappings_fingerprint ON card_mappings(fingerprint);
CREATE INDEX IF NOT EXISTS idx_card_mappings_card_name ON card_mappings(card_name);
CREATE INDEX IF NOT EXISTS idx_card_mappings_set_code ON card_mappings(set_code);
CREATE INDEX IF NOT EXISTS idx_card_mappings_collector_number ON card_mappings(collector_number);

CREATE TABLE IF NOT EXISTS review_queue (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id              TEXT    NOT NULL UNIQUE,
    title                   TEXT    NOT NULL,
    url                     TEXT    NOT NULL,
    price                   REAL,
    currency                TEXT,
    seller_name             TEXT,
    seller_id               TEXT,
    description             TEXT,
    images                  TEXT,   -- JSON
    fingerprint             TEXT,
    failure_reason          TEXT,
    matching_attempts       TEXT,   -- JSON
    discord_message_id      TEXT,
    discord_channel_id      TEXT,
    status                  TEXT    DEFAULT 'pending',  -- pending | resolved | expired
    created_at              TEXT,
    resolved_at             TEXT,
    resolved_cardmarket_url TEXT,
    resolved_by             TEXT
);

CREATE INDEX IF NOT EXISTS idx_review_queue_status ON review_queue(status);
CREATE INDEX IF NOT EXISTS idx_review_queue_discord_msg ON review_queue(discord_message_id);

CREATE TABLE IF NOT EXISTS error_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id     TEXT,
    listing_title  TEXT,
    listing_url    TEXT,
    cardmarket_url TEXT,
    failure_step   TEXT,
    http_status    INTEGER,
    error_message  TEXT,
    stack_trace    TEXT,
    created_at     TEXT
);

CREATE TABLE IF NOT EXISTS filter_settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class Database:
    """Async SQLite database wrapper."""

    def __init__(self, db_path: Path | str = "data/deals.db") -> None:
        self._path = Path(db_path)
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Open the database and apply the schema."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self._path))
        self._conn.row_factory = aiosqlite.Row
        # Apply schema (idempotent – uses CREATE TABLE IF NOT EXISTS).
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()
        logger.info("Database opened: %s", self._path)

    async def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # Seen listings (deduplication)
    # ------------------------------------------------------------------

    async def is_seen(self, listing_id: str) -> bool:
        """Return True if this listing ID has already been processed."""
        async with self._conn.execute(  # type: ignore[union-attr]
            "SELECT 1 FROM seen_listings WHERE listing_id = ?", (listing_id,)
        ) as cur:
            return await cur.fetchone() is not None

    async def mark_seen(
        self,
        *,
        listing_id: str,
        title: str,
        url: str,
        price: float,
        currency: str,
        seller_name: str | None,
        fingerprint: str | None,
    ) -> None:
        """Record that a listing has been processed."""
        now = datetime.now(timezone.utc).isoformat()
        async with self._lock:
            await self._conn.execute(  # type: ignore[union-attr]
                """
                INSERT OR IGNORE INTO seen_listings
                    (listing_id, title, url, price, currency, seller_name,
                     fingerprint, processed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (listing_id, title, url, price, currency, seller_name,
                 fingerprint, now),
            )
            await self._conn.commit()  # type: ignore[union-attr]

    # ------------------------------------------------------------------
    # Card mappings (learning database)
    # ------------------------------------------------------------------

    async def get_all_mappings(self) -> list[dict[str, Any]]:
        """Return all card mapping records."""
        async with self._conn.execute(  # type: ignore[union-attr]
            "SELECT * FROM card_mappings ORDER BY confidence DESC, date_added DESC"
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def add_mapping(
        self,
        *,
        vinted_title: str,
        vinted_url: str | None = None,
        vinted_description: str | None = None,
        seller_name: str | None = None,
        category: str | None = None,
        price: float | None = None,
        card_name: str | None = None,
        set_name: str | None = None,
        set_code: str | None = None,
        collector_number: str | None = None,
        rarity: str | None = None,
        language: str | None = None,
        edition: str | None = None,
        fingerprint: str | None = None,
        cardmarket_url: str,
        cardmarket_product_id: str | None = None,
        cardmarket_product_name: str | None = None,
        tokens: list[str] | None = None,
        confidence: float = 1.0,
        validated_by: str = "auto",
    ) -> int:
        """Insert a new card mapping.  Returns the new row ID."""
        now = datetime.now(timezone.utc).isoformat()
        tokens_json = json.dumps(tokens or [])
        async with self._lock:
            cur = await self._conn.execute(  # type: ignore[union-attr]
                """
                INSERT INTO card_mappings
                    (vinted_title, vinted_url, vinted_description, seller_name,
                     category, price, card_name, set_name, set_code,
                     collector_number, rarity, language, edition, fingerprint,
                     cardmarket_url, cardmarket_product_id, cardmarket_product_name,
                     tokens, confidence, validated_by, date_added, date_updated)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    vinted_title, vinted_url, vinted_description, seller_name,
                    category, price, card_name, set_name, set_code,
                    collector_number, rarity, language, edition, fingerprint,
                    cardmarket_url, cardmarket_product_id, cardmarket_product_name,
                    tokens_json, confidence, validated_by, now, now,
                ),
            )
            await self._conn.commit()  # type: ignore[union-attr]
            return cur.lastrowid  # type: ignore[return-value]

    async def delete_mapping(self, mapping_id: int) -> bool:
        """Delete a mapping by ID.  Returns True if a row was deleted."""
        async with self._lock:
            cur = await self._conn.execute(  # type: ignore[union-attr]
                "DELETE FROM card_mappings WHERE id = ?", (mapping_id,)
            )
            await self._conn.commit()  # type: ignore[union-attr]
            return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Review queue
    # ------------------------------------------------------------------

    async def add_review_item(
        self,
        *,
        listing_id: str,
        title: str,
        url: str,
        price: float | None = None,
        currency: str | None = None,
        seller_name: str | None = None,
        seller_id: str | None = None,
        description: str | None = None,
        images: list[str] | None = None,
        fingerprint: str | None = None,
        failure_reason: str | None = None,
        matching_attempts: list[dict] | None = None,
    ) -> int:
        """Insert a listing into the review queue.  Returns the row ID."""
        now = datetime.now(timezone.utc).isoformat()
        images_json = json.dumps(images or [])
        attempts_json = json.dumps(matching_attempts or [])
        async with self._lock:
            cur = await self._conn.execute(  # type: ignore[union-attr]
                """
                INSERT OR IGNORE INTO review_queue
                    (listing_id, title, url, price, currency, seller_name,
                     seller_id, description, images, fingerprint,
                     failure_reason, matching_attempts, status, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'pending',?)
                """,
                (
                    listing_id, title, url, price, currency, seller_name,
                    seller_id, description, images_json, fingerprint,
                    failure_reason, attempts_json, now,
                ),
            )
            await self._conn.commit()  # type: ignore[union-attr]
            return cur.lastrowid  # type: ignore[return-value]

    async def set_review_discord_message(
        self, listing_id: str, message_id: str, channel_id: str
    ) -> None:
        """Store the Discord message ID for a review queue item."""
        async with self._lock:
            await self._conn.execute(  # type: ignore[union-attr]
                """UPDATE review_queue
                   SET discord_message_id = ?, discord_channel_id = ?
                   WHERE listing_id = ?""",
                (message_id, channel_id, listing_id),
            )
            await self._conn.commit()  # type: ignore[union-attr]

    async def get_review_item_by_message(
        self, message_id: str
    ) -> dict[str, Any] | None:
        """Return a review queue item by its Discord message ID."""
        async with self._conn.execute(  # type: ignore[union-attr]
            "SELECT * FROM review_queue WHERE discord_message_id = ?",
            (message_id,),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def resolve_review_item(
        self,
        listing_id: str,
        *,
        cardmarket_url: str,
        resolved_by: str,
    ) -> None:
        """Mark a review queue item as resolved."""
        now = datetime.now(timezone.utc).isoformat()
        async with self._lock:
            await self._conn.execute(  # type: ignore[union-attr]
                """UPDATE review_queue
                   SET status = 'resolved', resolved_at = ?,
                       resolved_cardmarket_url = ?, resolved_by = ?
                   WHERE listing_id = ?""",
                (now, cardmarket_url, resolved_by, listing_id),
            )
            await self._conn.commit()  # type: ignore[union-attr]

    async def get_pending_review_items(self) -> list[dict[str, Any]]:
        """Return all pending review queue items."""
        async with self._conn.execute(  # type: ignore[union-attr]
            "SELECT * FROM review_queue WHERE status = 'pending' ORDER BY created_at DESC"
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Error log
    # ------------------------------------------------------------------

    async def log_error(
        self,
        *,
        listing_id: str | None = None,
        listing_title: str | None = None,
        listing_url: str | None = None,
        cardmarket_url: str | None = None,
        failure_step: str | None = None,
        http_status: int | None = None,
        error_message: str | None = None,
        stack_trace: str | None = None,
    ) -> None:
        """Persist a structured processing error."""
        now = datetime.now(timezone.utc).isoformat()
        async with self._lock:
            await self._conn.execute(  # type: ignore[union-attr]
                """
                INSERT INTO error_log
                    (listing_id, listing_title, listing_url, cardmarket_url,
                     failure_step, http_status, error_message, stack_trace,
                     created_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    listing_id, listing_title, listing_url, cardmarket_url,
                    failure_step, http_status, error_message, stack_trace, now,
                ),
            )
            await self._conn.commit()  # type: ignore[union-attr]

    # ------------------------------------------------------------------
    # Filter settings (runtime config overrides)
    # ------------------------------------------------------------------

    async def get_all_filters(self) -> dict[str, str]:
        """Return all stored filter overrides as a key→value dict."""
        async with self._conn.execute(  # type: ignore[union-attr]
            "SELECT key, value FROM filter_settings"
        ) as cur:
            rows = await cur.fetchall()
        return {r["key"]: r["value"] for r in rows}

    async def set_filter(self, key: str, value: str) -> None:
        """Upsert a filter override."""
        async with self._lock:
            await self._conn.execute(  # type: ignore[union-attr]
                "INSERT OR REPLACE INTO filter_settings (key, value) VALUES (?, ?)",
                (key, value),
            )
            await self._conn.commit()  # type: ignore[union-attr]

    async def delete_filter(self, key: str) -> None:
        """Remove a filter override."""
        async with self._lock:
            await self._conn.execute(  # type: ignore[union-attr]
                "DELETE FROM filter_settings WHERE key = ?", (key,)
            )
            await self._conn.commit()  # type: ignore[union-attr]
