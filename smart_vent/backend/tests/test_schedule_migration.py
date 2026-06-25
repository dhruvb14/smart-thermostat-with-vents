"""Migration regression test (Issue #359).

Proves that upgrading a pre-#359 database (a `schedules` table without the
`enabled` / `expires_at` columns) backfills existing rows to the pre-#359
behaviour: enabled and never-expiring.
"""

from __future__ import annotations

import json

import aiosqlite
import pytest

from backend import db


@pytest.mark.asyncio
async def test_pre359_rows_default_enabled_and_never_expire(tmp_path) -> None:
    path = str(tmp_path / "old.db")
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    try:
        # Recreate the pre-#359 schedules table (no enabled / expires_at) and a
        # legacy row, then let init_db run the real ALTER TABLE migrations.
        await conn.execute(
            """
            CREATE TABLE schedules (
                id TEXT PRIMARY KEY,
                room_id TEXT NOT NULL,
                days_of_week TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                target_temp REAL NOT NULL
            )
            """
        )
        await conn.execute(
            "INSERT INTO schedules(id,room_id,days_of_week,start_time,end_time,target_temp) "
            "VALUES (?,?,?,?,?,?)",
            ("s1", "r1", json.dumps([0, 1, 2]), "22:00:00", "07:00:00", 68.0),
        )
        await conn.commit()

        await db.init_db(conn)  # runs the additive migrations

        scheds = await db.get_schedules_for_room(conn, "r1")
        assert len(scheds) == 1
        assert scheds[0].enabled is True  # backfilled to enabled
        assert scheds[0].expires_at is None  # backfilled to never-expire
    finally:
        await conn.close()
