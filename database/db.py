"""
database/db.py
~~~~~~~~~~~~~~
Async SQLite helper using aiosqlite.

All public methods are coroutines so they can be awaited inside the async
bot event-loop without blocking.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"
_DEFAULT_DB_PATH = Path("data") / "deals.db"


class Database:
    """Thin async wrapper around an aiosqlite connection."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self._path = Path(db_path) if db_path else _DEFAULT_DB_PATH
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Open the database and run the schema migrations."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        # Enable WAL mode for better concurrent read performance.
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.execute("PRAGMA foreign_keys=ON;")
        await self._apply_schema()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def _apply_schema(self) -> None:
        schema_sql = _SCHEMA_PATH.read_text(encoding="utf-8")
        async with self._lock:
            await self._conn.executescript(schema_sql)  # type: ignore[union-attr]
            await self._conn.commit()  # type: ignore[union-attr]

    # ------------------------------------------------------------------
    # Listings
    # ------------------------------------------------------------------

    async def is_seen(self, listing_id: str) -> bool:
        """Return True if the listing has already been recorded."""
        async with self._conn.execute(  # type: ignore[union-attr]
            "SELECT 1 FROM seen_listings WHERE id = ?", (listing_id,)
        ) as cur:
            return await cur.fetchone() is not None

    async def mark_seen(
        self,
        *,
        listing_id: str,
        title: str,
        url: str,
        price: float,
        seller: str | None,
        currency: str = "EUR",
        score: int = 0,
        posted_to_discord: bool = False,
        terms: list[str] | None = None,
    ) -> None:
        """Insert a new listing into the seen table."""
        now = datetime.now(timezone.utc).isoformat()
        async with self._lock:
            await self._conn.execute(  # type: ignore[union-attr]
                """
                INSERT OR IGNORE INTO seen_listings
                    (id, title, url, price, seller, currency, posted_at, score, posted_to_discord)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    listing_id,
                    title,
                    url,
                    price,
                    seller,
                    currency,
                    now,
                    score,
                    int(posted_to_discord),
                ),
            )
            if terms:
                await self._conn.executemany(  # type: ignore[union-attr]
                    "INSERT OR IGNORE INTO listing_terms (listing_id, term) VALUES (?, ?)",
                    [(listing_id, t) for t in terms],
                )
            await self._conn.commit()  # type: ignore[union-attr]

    async def mark_posted(self, listing_id: str) -> None:
        """Flag a listing as having been posted to Discord."""
        async with self._lock:
            await self._conn.execute(  # type: ignore[union-attr]
                "UPDATE seen_listings SET posted_to_discord = 1 WHERE id = ?",
                (listing_id,),
            )
            await self._conn.commit()  # type: ignore[union-attr]

    async def get_recent_listings(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the most recently seen listings."""
        async with self._conn.execute(  # type: ignore[union-attr]
            "SELECT * FROM seen_listings ORDER BY posted_at DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
            return [dict(row) for row in rows]

    async def prune_old(self, days: int = 30) -> int:
        """Remove listings older than *days* days.  Returns deleted row count."""
        async with self._lock:
            cur = await self._conn.execute(  # type: ignore[union-attr]
                """
                DELETE FROM seen_listings
                WHERE posted_at < datetime('now', ? || ' days')
                """,
                (f"-{days}",),
            )
            await self._conn.commit()  # type: ignore[union-attr]
            return cur.rowcount

    # ------------------------------------------------------------------
    # Filter overrides (set via slash commands)
    # ------------------------------------------------------------------

    async def set_filter(self, key: str, value: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        async with self._lock:
            await self._conn.execute(  # type: ignore[union-attr]
                """
                INSERT INTO filter_overrides (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, value, now),
            )
            await self._conn.commit()  # type: ignore[union-attr]

    async def get_filter(self, key: str) -> str | None:
        async with self._conn.execute(  # type: ignore[union-attr]
            "SELECT value FROM filter_overrides WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
            return row["value"] if row else None

    async def get_all_filters(self) -> dict[str, str]:
        async with self._conn.execute(  # type: ignore[union-attr]
            "SELECT key, value FROM filter_overrides"
        ) as cur:
            rows = await cur.fetchall()
            return {row["key"]: row["value"] for row in rows}

    async def delete_filter(self, key: str) -> bool:
        async with self._lock:
            cur = await self._conn.execute(  # type: ignore[union-attr]
                "DELETE FROM filter_overrides WHERE key = ?", (key,)
            )
            await self._conn.commit()  # type: ignore[union-attr]
            return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Unidentified listings (community review queue)
    # ------------------------------------------------------------------

    async def add_unidentified_listing(
        self,
        *,
        listing_id: str,
        title: str,
        url: str,
        price: float,
        currency: str = "EUR",
        description: str | None = None,
        images: list[str] | None = None,
        confidence: str = "Low",
        failure_reason: str | None = None,
        ocr_text: str | None = None,
        review_message_id: str | None = None,
    ) -> None:
        """Insert an unidentified listing into the review queue."""
        import json

        now = datetime.now(timezone.utc).isoformat()
        images_json = json.dumps(images or [])
        async with self._lock:
            await self._conn.execute(  # type: ignore[union-attr]
                """
                INSERT OR IGNORE INTO unidentified_listings
                    (id, title, url, price, currency, description, images,
                     confidence, failure_reason, ocr_text, review_message_id,
                     status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    listing_id,
                    title,
                    url,
                    price,
                    currency,
                    description,
                    images_json,
                    confidence,
                    failure_reason,
                    ocr_text,
                    review_message_id,
                    now,
                    now,
                ),
            )
            await self._conn.commit()  # type: ignore[union-attr]

    async def set_review_message_id(self, listing_id: str, message_id: str) -> None:
        """Store the Discord review message ID for an unidentified listing."""
        now = datetime.now(timezone.utc).isoformat()
        async with self._lock:
            await self._conn.execute(  # type: ignore[union-attr]
                "UPDATE unidentified_listings SET review_message_id = ?, updated_at = ? WHERE id = ?",
                (message_id, now, listing_id),
            )
            await self._conn.commit()  # type: ignore[union-attr]

    async def get_unidentified_listing(self, listing_id: str) -> dict[str, Any] | None:
        """Return an unidentified listing by its listing ID."""
        async with self._conn.execute(  # type: ignore[union-attr]
            "SELECT * FROM unidentified_listings WHERE id = ?", (listing_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get_unidentified_by_message_id(
        self, message_id: str
    ) -> dict[str, Any] | None:
        """Return the unidentified listing associated with a Discord message ID."""
        async with self._conn.execute(  # type: ignore[union-attr]
            "SELECT * FROM unidentified_listings WHERE review_message_id = ?",
            (message_id,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def update_unidentified_status(self, listing_id: str, status: str) -> None:
        """Update the review status of an unidentified listing."""
        now = datetime.now(timezone.utc).isoformat()
        async with self._lock:
            await self._conn.execute(  # type: ignore[union-attr]
                "UPDATE unidentified_listings SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, listing_id),
            )
            await self._conn.commit()  # type: ignore[union-attr]

    # ------------------------------------------------------------------
    # Reference submissions
    # ------------------------------------------------------------------

    async def add_reference_submission(
        self,
        *,
        listing_id: str,
        submitted_by: str,
        reference_url: str,
        platform: str | None = None,
        market_value: float | None = None,
    ) -> int:
        """Insert a new reference URL submission.  Returns the row ID."""
        now = datetime.now(timezone.utc).isoformat()
        async with self._lock:
            cur = await self._conn.execute(  # type: ignore[union-attr]
                """
                INSERT INTO reference_submissions
                    (listing_id, submitted_by, reference_url, platform,
                     market_value, validated, created_at)
                VALUES (?, ?, ?, ?, ?, 0, ?)
                """,
                (listing_id, submitted_by, reference_url, platform, market_value, now),
            )
            await self._conn.commit()  # type: ignore[union-attr]
            return cur.lastrowid  # type: ignore[return-value]

    async def set_confirm_message_id(self, ref_id: int, message_id: str) -> None:
        """Store the Discord confirmation message ID for a reference submission."""
        async with self._lock:
            await self._conn.execute(  # type: ignore[union-attr]
                "UPDATE reference_submissions SET confirm_message_id = ? WHERE id = ?",
                (message_id, ref_id),
            )
            await self._conn.commit()  # type: ignore[union-attr]

    async def get_reference_by_confirm_message(
        self, message_id: str
    ) -> dict[str, Any] | None:
        """Return the reference submission linked to a confirmation message."""
        async with self._conn.execute(  # type: ignore[union-attr]
            "SELECT * FROM reference_submissions WHERE confirm_message_id = ?",
            (message_id,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def update_reference_validated(self, ref_id: int, validated: int) -> None:
        """Set validated flag: 1 = approved, -1 = rejected, 0 = pending."""
        async with self._lock:
            await self._conn.execute(  # type: ignore[union-attr]
                "UPDATE reference_submissions SET validated = ? WHERE id = ?",
                (validated, ref_id),
            )
            await self._conn.commit()  # type: ignore[union-attr]

    async def update_reference_market_value(
        self, ref_id: int, market_value: float
    ) -> None:
        """Store the market value extracted for a reference submission."""
        async with self._lock:
            await self._conn.execute(  # type: ignore[union-attr]
                "UPDATE reference_submissions SET market_value = ? WHERE id = ?",
                (market_value, ref_id),
            )
            await self._conn.commit()  # type: ignore[union-attr]

    # ------------------------------------------------------------------
    # Identification memory (learning system)
    # ------------------------------------------------------------------

    async def add_to_memory(
        self,
        *,
        title_pattern: str,
        card_name: str,
        card_set: str | None = None,
        card_number: str | None = None,
        language: str | None = None,
        reference_url: str | None = None,
        market_value: float | None = None,
        source_listing_id: str | None = None,
        approved_by: str,
    ) -> None:
        """Store a human-approved identification in memory for future matching."""
        now = datetime.now(timezone.utc).isoformat()
        async with self._lock:
            await self._conn.execute(  # type: ignore[union-attr]
                """
                INSERT INTO identification_memory
                    (title_pattern, card_name, card_set, card_number, language,
                     reference_url, market_value, source_listing_id, approved_by,
                     created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    title_pattern.lower().strip(),
                    card_name,
                    card_set,
                    card_number,
                    language,
                    reference_url,
                    market_value,
                    source_listing_id,
                    approved_by,
                    now,
                ),
            )
            await self._conn.commit()  # type: ignore[union-attr]

    async def find_in_memory(self, title: str) -> list[dict[str, Any]]:
        """Return memory entries whose title_pattern appears in *title* or vice versa."""
        title_lower = title.lower().strip()
        async with self._conn.execute(  # type: ignore[union-attr]
            "SELECT * FROM identification_memory ORDER BY created_at DESC"
        ) as cur:
            rows = await cur.fetchall()

        matches: list[dict[str, Any]] = []
        for row in rows:
            pattern = (row["title_pattern"] or "").lower().strip()
            if not pattern:
                continue
            # Match if the stored pattern is a substring of the new title or vice versa.
            if pattern in title_lower or title_lower in pattern:
                matches.append(dict(row))
        return matches
