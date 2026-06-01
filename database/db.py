"""
database/db.py
~~~~~~~~~~~~~~
Async SQLite database for the deal-monitor bot.

Tables:
  seen_listings       – track processed Vinted listing IDs (deduplication)
  card_mappings       – learning database: Vinted title/fingerprint → Cardmarket URL
  review_queue        – listings pending manual Discord review
  error_log           – structured Cardmarket scraping / processing errors
  correction_log      – URL corrections supplied by users via Discord reply
  slug_prefix_rules   – learned set-prefix patterns (e.g. Team Rocket → TR prefix)
  filter_settings     – runtime-adjustable bot settings
  catalog_id_slugs    – idProduct/idExpansion → URL slug mappings (from unidentified channel)
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

_DEFAULT_FILTER_SETTINGS: dict[str, str] = {
    "pending_ttl_days": "3",
    "slug_confidence_threshold": "0.6",
    "min_price_eur": "0.50",
    "max_price_eur": "500.00",
}

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
    status                  TEXT    DEFAULT 'pending',  -- pending | resolved | expired | skipped
    created_at              TEXT,
    resolved_at             TEXT,
    resolved_cardmarket_url TEXT,
    resolved_by             TEXT
);

CREATE INDEX IF NOT EXISTS idx_review_queue_status ON review_queue(status);
CREATE INDEX IF NOT EXISTS idx_review_queue_discord_msg ON review_queue(discord_message_id);

CREATE TABLE IF NOT EXISTS error_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id          TEXT,
    listing_title       TEXT,
    listing_url         TEXT,
    listing_price       REAL,
    listing_currency    TEXT    DEFAULT 'EUR',
    listing_seller_name TEXT,
    cardmarket_url      TEXT,
    failure_step        TEXT,
    http_status         INTEGER,
    error_message       TEXT,
    stack_trace         TEXT,
    discord_message_id  TEXT,
    created_at          TEXT
);

CREATE INDEX IF NOT EXISTS idx_error_log_discord_msg ON error_log(discord_message_id);

CREATE TABLE IF NOT EXISTS correction_log (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    -- Original error details
    listing_id                  TEXT,
    listing_title               TEXT,
    listing_url                 TEXT,
    generated_cardmarket_url    TEXT,
    failed_slug                 TEXT,
    -- Corrected details
    correct_cardmarket_url      TEXT    NOT NULL,
    correct_slug                TEXT,
    product_name                TEXT,
    -- Card identity fields at the time of correction
    set_name                    TEXT,
    set_code                    TEXT,
    collector_number            TEXT,
    -- Pattern analysis
    original_identifier         TEXT,   -- e.g. "Dark-Raichu-83"
    corrected_identifier        TEXT,   -- e.g. "Dark-Raichu-TR83"
    learned_prefix              TEXT,   -- e.g. "TR" (NULL if no pattern found)
    -- Meta
    corrected_by                TEXT,
    date_learned                TEXT
);

CREATE TABLE IF NOT EXISTS slug_prefix_rules (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    set_name        TEXT,
    set_code        TEXT    NOT NULL,
    prefix          TEXT    NOT NULL,
    uses_successful INTEGER DEFAULT 0,
    uses_failed     INTEGER DEFAULT 0,
    confidence      REAL    DEFAULT 1.0,
    date_learned    TEXT,
    date_last_used  TEXT,
    UNIQUE(set_code, prefix)
);

CREATE INDEX IF NOT EXISTS idx_slug_prefix_rules_set_code ON slug_prefix_rules(set_code);

CREATE TABLE IF NOT EXISTS slug_overrides (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint     TEXT    NOT NULL UNIQUE,
    preferred_slug  TEXT    NOT NULL,
    cardmarket_url  TEXT    NOT NULL,
    date_learned    TEXT
);

CREATE INDEX IF NOT EXISTS idx_slug_overrides_fingerprint ON slug_overrides(fingerprint);

CREATE TABLE IF NOT EXISTS catalog_id_slugs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    id_product      INTEGER UNIQUE,
    product_slug    TEXT,
    id_expansion    INTEGER UNIQUE,
    set_slug        TEXT,
    cardmarket_url  TEXT,
    date_learned    TEXT
);

CREATE INDEX IF NOT EXISTS idx_catalog_id_slugs_id_product ON catalog_id_slugs(id_product);
CREATE INDEX IF NOT EXISTS idx_catalog_id_slugs_id_expansion ON catalog_id_slugs(id_expansion);

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
        # Migrate existing error_log table: add discord_message_id if missing.
        await self._migrate()
        await self.ensure_default_filters()
        logger.info("Database opened: %s", self._path)

    async def _migrate(self) -> None:
        """Apply any incremental schema migrations."""
        try:
            await self._conn.execute(  # type: ignore[union-attr]
                "ALTER TABLE error_log ADD COLUMN discord_message_id TEXT"
            )
            await self._conn.execute(  # type: ignore[union-attr]
                "CREATE INDEX IF NOT EXISTS idx_error_log_discord_msg "
                "ON error_log(discord_message_id)"
            )
            await self._conn.commit()  # type: ignore[union-attr]
            logger.debug("Database: migrated error_log – added discord_message_id column")
        except Exception:  # noqa: BLE001
            # Column already exists – this is expected for databases created
            # after the migration was included in the schema.
            pass
        for col, definition in (
            ("listing_price", "REAL"),
            ("listing_currency", "TEXT DEFAULT 'EUR'"),
            ("listing_seller_name", "TEXT"),
        ):
            try:
                await self._conn.execute(  # type: ignore[union-attr]
                    f"ALTER TABLE error_log ADD COLUMN {col} {definition}"
                )
                await self._conn.commit()  # type: ignore[union-attr]
                logger.debug("Database: migrated error_log – added %s column", col)
            except Exception:  # noqa: BLE001
                pass

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
        status: str = "pending",
    ) -> int:
        """Insert a listing into the review queue.  Returns the row ID.

        *status* defaults to ``'pending'``; pass ``'skipped'`` for pre-filtered
        non-card listings that should be observable but not actively reviewed.
        """
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
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    listing_id, title, url, price, currency, seller_name,
                    seller_id, description, images_json, fingerprint,
                    failure_reason, attempts_json, status, now,
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
        listing_price: float | None = None,
        listing_currency: str | None = None,
        listing_seller_name: str | None = None,
        cardmarket_url: str | None = None,
        failure_step: str | None = None,
        http_status: int | None = None,
        error_message: str | None = None,
        stack_trace: str | None = None,
    ) -> int | None:
        """Persist a structured processing error.  Returns the new row ID.

        De-duplication: if an entry already exists for the same
        ``(listing_id, failure_step)`` pair, the existing row ID is returned
        and no new row is inserted.  This prevents repeated runs from
        generating duplicate error entries for the same listing.
        """
        now = datetime.now(timezone.utc).isoformat()
        async with self._lock:
            # Check for an existing entry with the same listing_id + failure_step.
            if listing_id and failure_step:
                async with self._conn.execute(  # type: ignore[union-attr]
                    "SELECT id FROM error_log WHERE listing_id = ? AND failure_step = ? LIMIT 1",
                    (listing_id, failure_step),
                ) as dedup_cur:
                    existing = await dedup_cur.fetchone()
                if existing:
                    logger.debug(
                        "Database: skipping duplicate error_log for listing_id=%r step=%r",
                        listing_id, failure_step,
                    )
                    return existing["id"]

            cur = await self._conn.execute(  # type: ignore[union-attr]
                """
                INSERT INTO error_log
                    (listing_id, listing_title, listing_url,
                     listing_price, listing_currency, listing_seller_name,
                     cardmarket_url,
                     failure_step, http_status, error_message, stack_trace,
                     created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    listing_id, listing_title, listing_url,
                    listing_price, listing_currency or "EUR", listing_seller_name,
                    cardmarket_url,
                    failure_step, http_status, error_message, stack_trace, now,
                ),
            )
            await self._conn.commit()  # type: ignore[union-attr]
            return cur.lastrowid  # type: ignore[return-value]

    async def update_error_message_id(self, error_log_id: int, discord_message_id: str) -> None:
        """Store the Discord message ID for an error_log entry."""
        async with self._lock:
            await self._conn.execute(  # type: ignore[union-attr]
                "UPDATE error_log SET discord_message_id = ? WHERE id = ?",
                (discord_message_id, error_log_id),
            )
            await self._conn.commit()  # type: ignore[union-attr]

    async def get_error_by_message_id(self, discord_message_id: str) -> dict[str, Any] | None:
        """Return an error_log entry by its Discord message ID."""
        async with self._conn.execute(  # type: ignore[union-attr]
            "SELECT * FROM error_log WHERE discord_message_id = ? ORDER BY id DESC LIMIT 1",
            (discord_message_id,),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # Correction log
    # ------------------------------------------------------------------

    async def add_correction(
        self,
        *,
        listing_id: str | None = None,
        listing_title: str | None = None,
        listing_url: str | None = None,
        generated_cardmarket_url: str | None = None,
        failed_slug: str | None = None,
        correct_cardmarket_url: str,
        correct_slug: str | None = None,
        product_name: str | None = None,
        set_name: str | None = None,
        set_code: str | None = None,
        collector_number: str | None = None,
        original_identifier: str | None = None,
        corrected_identifier: str | None = None,
        learned_prefix: str | None = None,
        corrected_by: str | None = None,
    ) -> int:
        """Insert a user-supplied URL correction.  Returns the new row ID."""
        now = datetime.now(timezone.utc).isoformat()
        async with self._lock:
            cur = await self._conn.execute(  # type: ignore[union-attr]
                """
                INSERT INTO correction_log
                    (listing_id, listing_title, listing_url,
                     generated_cardmarket_url, failed_slug,
                     correct_cardmarket_url, correct_slug, product_name,
                     set_name, set_code, collector_number,
                     original_identifier, corrected_identifier, learned_prefix,
                     corrected_by, date_learned)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    listing_id, listing_title, listing_url,
                    generated_cardmarket_url, failed_slug,
                    correct_cardmarket_url, correct_slug, product_name,
                    set_name, set_code, collector_number,
                    original_identifier, corrected_identifier, learned_prefix,
                    corrected_by, now,
                ),
            )
            await self._conn.commit()  # type: ignore[union-attr]
            return cur.lastrowid  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Slug prefix rules (pattern learning)
    # ------------------------------------------------------------------

    async def upsert_slug_prefix_rule(
        self,
        *,
        set_code: str,
        prefix: str,
        set_name: str | None = None,
    ) -> int:
        """Insert or update a slug prefix rule.  Returns the rule row ID."""
        now = datetime.now(timezone.utc).isoformat()
        async with self._lock:
            # Try insert first.
            try:
                cur = await self._conn.execute(  # type: ignore[union-attr]
                    """
                    INSERT INTO slug_prefix_rules
                        (set_code, prefix, set_name, uses_successful, uses_failed,
                         confidence, date_learned, date_last_used)
                    VALUES (?, ?, ?, 1, 0, 1.0, ?, ?)
                    """,
                    (set_code, prefix, set_name, now, now),
                )
                await self._conn.commit()  # type: ignore[union-attr]
                return cur.lastrowid  # type: ignore[return-value]
            except Exception:  # noqa: BLE001
                # Row already exists – update success count and set name if provided.
                async with self._conn.execute(  # type: ignore[union-attr]
                    "SELECT id FROM slug_prefix_rules WHERE set_code = ? AND prefix = ?",
                    (set_code, prefix),
                ) as cur2:
                    row = await cur2.fetchone()
                if row:
                    await self._conn.execute(  # type: ignore[union-attr]
                        """
                        UPDATE slug_prefix_rules
                        SET uses_successful = uses_successful + 1,
                            date_last_used = ?,
                            set_name = COALESCE(?, set_name)
                        WHERE set_code = ? AND prefix = ?
                        """,
                        (now, set_name, set_code, prefix),
                    )
                    await self._conn.commit()  # type: ignore[union-attr]
                    return row["id"]
                return 0

    async def get_slug_prefix_rule(self, set_code: str) -> dict[str, Any] | None:
        """Return the highest-confidence prefix rule for *set_code*, or None."""
        async with self._conn.execute(  # type: ignore[union-attr]
            """
            SELECT * FROM slug_prefix_rules
            WHERE set_code = ?
            ORDER BY confidence DESC, uses_successful DESC
            LIMIT 1
            """,
            (set_code,),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def get_all_slug_prefix_rules(self) -> list[dict[str, Any]]:
        """Return all slug prefix rules ordered by confidence."""
        async with self._conn.execute(  # type: ignore[union-attr]
            "SELECT * FROM slug_prefix_rules ORDER BY confidence DESC, uses_successful DESC"
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def record_slug_prefix_rule_use(
        self, rule_id: int, *, success: bool
    ) -> None:
        """Increment use counters and recalculate confidence for a prefix rule."""
        now = datetime.now(timezone.utc).isoformat()
        async with self._lock:
            if success:
                await self._conn.execute(  # type: ignore[union-attr]
                    """
                    UPDATE slug_prefix_rules
                    SET uses_successful = uses_successful + 1,
                        date_last_used = ?,
                        confidence = CAST(uses_successful + 1 AS REAL)
                                     / (uses_successful + 1 + uses_failed)
                    WHERE id = ?
                    """,
                    (now, rule_id),
                )
            else:
                await self._conn.execute(  # type: ignore[union-attr]
                    """
                    UPDATE slug_prefix_rules
                    SET uses_failed = uses_failed + 1,
                        date_last_used = ?,
                        confidence = CAST(uses_successful AS REAL)
                                     / (uses_successful + uses_failed + 1)
                    WHERE id = ?
                    """,
                    (now, rule_id),
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

    async def ensure_default_filters(self) -> None:
        """Populate default runtime filter settings when missing."""
        async with self._lock:
            for key, value in _DEFAULT_FILTER_SETTINGS.items():
                await self._conn.execute(  # type: ignore[union-attr]
                    """
                    INSERT INTO filter_settings (key, value)
                    VALUES (?, ?)
                    ON CONFLICT(key) DO NOTHING
                    """,
                    (key, value),
                )
            await self._conn.commit()  # type: ignore[union-attr]

    # ------------------------------------------------------------------
    # Slug overrides (per-fingerprint preferred URL learned from corrections)
    # ------------------------------------------------------------------

    async def add_slug_override(
        self,
        *,
        fingerprint: str,
        preferred_slug: str,
        cardmarket_url: str,
    ) -> int:
        """Upsert a slug override for the given card fingerprint.

        When a user correction supplies a URL for a card whose auto-generated
        URL was wrong, the corrected URL is stored here so future resolutions
        return it directly without re-running slug construction.

        Returns the row ID.
        """
        now = datetime.now(timezone.utc).isoformat()
        async with self._lock:
            try:
                cur = await self._conn.execute(  # type: ignore[union-attr]
                    """
                    INSERT INTO slug_overrides
                        (fingerprint, preferred_slug, cardmarket_url, date_learned)
                    VALUES (?, ?, ?, ?)
                    """,
                    (fingerprint, preferred_slug, cardmarket_url, now),
                )
                await self._conn.commit()  # type: ignore[union-attr]
                return cur.lastrowid  # type: ignore[return-value]
            except Exception:  # noqa: BLE001
                # Row already exists – update in place.
                await self._conn.execute(  # type: ignore[union-attr]
                    """
                    UPDATE slug_overrides
                    SET preferred_slug = ?, cardmarket_url = ?, date_learned = ?
                    WHERE fingerprint = ?
                    """,
                    (preferred_slug, cardmarket_url, now, fingerprint),
                )
                await self._conn.commit()  # type: ignore[union-attr]
                async with self._conn.execute(  # type: ignore[union-attr]
                    "SELECT id FROM slug_overrides WHERE fingerprint = ?",
                    (fingerprint,),
                ) as cur2:
                    row = await cur2.fetchone()
                return row["id"] if row else 0

    async def get_slug_override(self, fingerprint: str) -> dict[str, Any] | None:
        """Return the slug override for the given fingerprint, or None."""
        async with self._conn.execute(  # type: ignore[union-attr]
            "SELECT * FROM slug_overrides WHERE fingerprint = ?",
            (fingerprint,),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def get_all_slug_overrides(self) -> list[dict[str, Any]]:
        """Return all slug overrides for in-memory caching."""
        async with self._conn.execute(  # type: ignore[union-attr]
            "SELECT * FROM slug_overrides ORDER BY id"
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Catalog ID slug mappings (idProduct / idExpansion → URL slugs)
    # ------------------------------------------------------------------

    async def store_catalog_id_slugs(
        self,
        *,
        id_product: int | None,
        product_slug: str | None,
        id_expansion: int | None,
        set_slug: str | None,
        cardmarket_url: str | None = None,
    ) -> None:
        """Persist idProduct → product_slug and idExpansion → set_slug mappings.

        Each ID is stored in its own row so that a product mapping and an
        expansion mapping can be updated independently.  Existing rows are
        updated when the slug changes.
        """
        now = datetime.now(timezone.utc).isoformat()
        async with self._lock:
            if id_product is not None and product_slug:
                await self._conn.execute(  # type: ignore[union-attr]
                    """
                    INSERT INTO catalog_id_slugs (id_product, product_slug, cardmarket_url, date_learned)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(id_product) DO UPDATE SET
                        product_slug   = excluded.product_slug,
                        cardmarket_url = excluded.cardmarket_url,
                        date_learned   = excluded.date_learned
                    """,
                    (id_product, product_slug, cardmarket_url, now),
                )
            if id_expansion is not None and set_slug:
                await self._conn.execute(  # type: ignore[union-attr]
                    """
                    INSERT INTO catalog_id_slugs (id_expansion, set_slug, cardmarket_url, date_learned)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(id_expansion) DO UPDATE SET
                        set_slug       = excluded.set_slug,
                        cardmarket_url = excluded.cardmarket_url,
                        date_learned   = excluded.date_learned
                    """,
                    (id_expansion, set_slug, cardmarket_url, now),
                )
            await self._conn.commit()  # type: ignore[union-attr]

    async def get_catalog_product_slug(self, id_product: int) -> str | None:
        """Return the stored product slug for *id_product*, or ``None``."""
        async with self._conn.execute(  # type: ignore[union-attr]
            "SELECT product_slug FROM catalog_id_slugs WHERE id_product = ?",
            (id_product,),
        ) as cur:
            row = await cur.fetchone()
        return row["product_slug"] if row else None

    async def get_catalog_expansion_slug(self, id_expansion: int) -> str | None:
        """Return the stored set slug for *id_expansion*, or ``None``."""
        async with self._conn.execute(  # type: ignore[union-attr]
            "SELECT set_slug FROM catalog_id_slugs WHERE id_expansion = ?",
            (id_expansion,),
        ) as cur:
            row = await cur.fetchone()
        return row["set_slug"] if row else None

    async def get_all_catalog_id_slugs(self) -> list[dict[str, Any]]:
        """Return all rows from catalog_id_slugs ordered by id."""
        async with self._conn.execute(  # type: ignore[union-attr]
            "SELECT * FROM catalog_id_slugs ORDER BY id"
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def find_catalog_id_by_set_slug(self, set_slug: str) -> dict[str, Any] | None:
        """Return the catalog_id_slugs row whose set_slug matches *set_slug*, or ``None``."""
        async with self._conn.execute(  # type: ignore[union-attr]
            "SELECT * FROM catalog_id_slugs WHERE set_slug = ?",
            (set_slug,),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def delete_catalog_id_slug(self, row_id: int) -> bool:
        """Delete a catalog_id_slugs row by its primary key.  Returns True if a row was deleted."""
        async with self._lock:
            cur = await self._conn.execute(  # type: ignore[union-attr]
                "DELETE FROM catalog_id_slugs WHERE id = ?", (row_id,)
            )
            await self._conn.commit()  # type: ignore[union-attr]
            return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Review queue maintenance
    # ------------------------------------------------------------------

    async def expire_old_review_items(self, days: int) -> int:
        """Mark pending review queue items older than *days* days as 'expired'.

        Returns the number of rows updated.
        """
        async with self._lock:
            cur = await self._conn.execute(  # type: ignore[union-attr]
                """
                UPDATE review_queue
                SET status = 'expired'
                WHERE status = 'pending'
                  AND created_at < datetime('now', ? || ' days')
                """,
                (f"-{days}",),
            )
            await self._conn.commit()  # type: ignore[union-attr]
            return cur.rowcount

    # ------------------------------------------------------------------
    # Error log aggregation
    # ------------------------------------------------------------------

    async def get_error_summary(self) -> list[dict[str, Any]]:
        """Return aggregate failure counts grouped by step and error message.

        Returns rows ordered by count descending, limited to 50.
        Each row has keys: ``failure_step``, ``error_message``, ``count``.
        """
        async with self._conn.execute(  # type: ignore[union-attr]
            """
            SELECT failure_step,
                   error_message,
                   COUNT(*) AS count
            FROM error_log
            GROUP BY failure_step, error_message
            ORDER BY count DESC
            LIMIT 50
            """
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]
