"""
Tests for the event_log periodic trim in backend/db.insert_event_log (Issue #299).

The trim must:
  * never delete rows while the table is below _EVENT_LOG_MAX, and
  * settle at exactly _EVENT_LOG_MAX rows in steady state (not _EVENT_LOG_MAX - 1).
"""

from __future__ import annotations

import aiosqlite

from backend import db


async def _fresh_db() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await db.init_db(conn)
    return conn


async def _count(conn: aiosqlite.Connection) -> int:
    async with conn.execute("SELECT COUNT(*) AS c FROM event_log") as cur:
        row = await cur.fetchone()
    return int(row["c"])


class TestEventLogTrim:
    async def test_below_cap_does_not_drop_rows(self, monkeypatch) -> None:
        # Trim fires on every insert; cap far above the number we insert, so the
        # table is always under the cap and nothing should ever be deleted.
        monkeypatch.setattr(db, "_TRIM_EVERY", 1)
        monkeypatch.setattr(db, "_EVENT_LOG_MAX", 1000)
        conn = await _fresh_db()
        try:
            for i in range(10):
                await db.insert_event_log(
                    conn, f"2026-01-01T00:00:{i:02d}", "info", "system", f"m{i}", None
                )
            assert await _count(conn) == 10
        finally:
            await conn.close()

    async def test_steady_state_keeps_exactly_cap_rows(self, monkeypatch) -> None:
        # With the cap reached, the table should settle at exactly the cap,
        # not cap - 1 (the off-by-one from `id <=`).
        monkeypatch.setattr(db, "_TRIM_EVERY", 1)
        monkeypatch.setattr(db, "_EVENT_LOG_MAX", 5)
        conn = await _fresh_db()
        try:
            for i in range(20):
                await db.insert_event_log(
                    conn, "2026-01-01T00:00:00", "info", "system", f"m{i}", None
                )
            assert await _count(conn) == 5
        finally:
            await conn.close()
