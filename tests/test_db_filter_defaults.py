from __future__ import annotations

from pathlib import Path

import pytest

from database.db import Database


@pytest.mark.anyio
async def test_default_filter_settings_are_seeded(tmp_path: Path) -> None:
    db = Database(db_path=tmp_path / "test.db")
    await db.connect()
    try:
        filters = await db.get_all_filters()
        assert filters["pending_ttl_days"] == "3"
        assert filters["slug_confidence_threshold"] == "0.6"
        assert filters["min_price_eur"] == "0.50"
        assert filters["max_price_eur"] == "500.00"
    finally:
        await db.close()
