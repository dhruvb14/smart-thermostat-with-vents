"""Temporary temperature hold integration tests (Issue #576).

Drives the hold write boundary (POST/DELETE /api/rooms/{id}/override), the
new GET /api/overrides listing, and the engine-tick expiry sweep
(``db.clear_expired_overrides``) against the full aiohttp app:

  - the Eco opt-in flag (``respect_eco``) round-trips through the API and the
    ``room_overrides`` upsert (a missing ON CONFLICT column would silently
    resurrect the old flag), and defaults to False — #419's "holds are never
    Eco-relaxed" stays the out-of-the-box behaviour;
  - unknown rooms 404 on both POST and DELETE;
  - the #576 duration cap: (0, 8] hours, inclusive ceiling;
  - GET /api/overrides lists only live holds (expired rows are filtered even
    before any tick sweeps them) with a sane ``ends_in_seconds`` countdown;
  - the tick sweep deletes expired holds, keeps live ones, and event-logs the
    expiry; POST/DELETE emit their own event-log lines (DELETE only when a
    hold actually existed);
  - Celsius mode converts once at the write boundary and the listing returns
    raw °F (no double conversion on the read path — #231).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from backend import db as _db
from backend.models import RoomOverride

THERMO = "climate.test"


async def _create_room(client, name="Living Room", thermostat=THERMO):
    resp = await client.post(
        "/api/rooms",
        json={"name": name, "thermostat_entity_id": thermostat},
    )
    assert resp.status == 201
    return await resp.json()


def _seed_thermostat(fake_ha, entity_id=THERMO) -> None:
    """Seed the climate entity so a tick sees an available thermostat."""
    fake_ha.seed_state(entity_id, "off", {"current_temperature": 72.0, "hvac_action": "idle"})


async def _messages(client, category: str) -> list[str]:
    events = await (await client.get(f"/api/logs/events?category={category}&limit=100")).json()
    return [e["message"] for e in events]


# ---------------------------------------------------------------------------
# respect_eco round-trip
# ---------------------------------------------------------------------------


class TestHoldRespectEco:
    async def test_respect_eco_round_trips_and_upserts(self, client):
        room = await _create_room(client)
        resp = await client.post(
            f"/api/rooms/{room['id']}/override",
            json={"target_temp": 72.0, "duration_hours": 2.0, "respect_eco": True},
        )
        assert resp.status == 200
        assert (await resp.json())["respect_eco"] is True
        conn = client.app["scheduler"]._db_conn
        ov = await _db.get_room_override(conn, room["id"])
        assert ov is not None
        assert ov.respect_eco is True

        # Re-POST with the flag off: the upsert's ON CONFLICT column list must
        # include respect_eco, or the stale True would survive the update
        # (the classic upsert column-preservation trap).
        resp = await client.post(
            f"/api/rooms/{room['id']}/override",
            json={"target_temp": 75.0, "duration_hours": 1.0, "respect_eco": False},
        )
        assert resp.status == 200
        assert (await resp.json())["respect_eco"] is False
        ov = await _db.get_room_override(conn, room["id"])
        assert ov is not None
        assert ov.respect_eco is False
        assert ov.target_temp == 75.0  # the rest of the row updated in place too

    async def test_respect_eco_defaults_false(self, client):
        # #419: an explicit hold is the strongest user signal there is, so a
        # caller that never mentions Eco keeps the never-relaxed behaviour.
        room = await _create_room(client)
        resp = await client.post(
            f"/api/rooms/{room['id']}/override",
            json={"target_temp": 72.0, "duration_hours": 2.0},
        )
        assert resp.status == 200
        assert (await resp.json())["respect_eco"] is False
        conn = client.app["scheduler"]._db_conn
        ov = await _db.get_room_override(conn, room["id"])
        assert ov is not None
        assert ov.respect_eco is False


# ---------------------------------------------------------------------------
# Validation: unknown rooms and the duration cap
# ---------------------------------------------------------------------------


class TestHoldValidation:
    async def test_set_override_unknown_room_404(self, client):
        resp = await client.post(
            "/api/rooms/no-such-room/override",
            json={"target_temp": 72.0},
        )
        assert resp.status == 404
        assert (await resp.json())["error"] == "Room not found"

    async def test_clear_override_unknown_room_404(self, client):
        resp = await client.delete("/api/rooms/no-such-room/override")
        assert resp.status == 404
        assert (await resp.json())["error"] == "Room not found"

    async def test_duration_cap(self, client):
        room = await _create_room(client)
        url = f"/api/rooms/{room['id']}/override"
        # 8 h is the inclusive ceiling (#576) — accepted.
        resp = await client.post(url, json={"target_temp": 72.0, "duration_hours": 8.0})
        assert resp.status == 200
        # Everything past the ceiling, zero, and negatives are rejected with
        # the same message.
        for bad in (8.5, 9000, 0, -1):
            resp = await client.post(url, json={"target_temp": 72.0, "duration_hours": bad})
            assert resp.status == 400, f"duration_hours={bad} must be rejected"
            assert (await resp.json())[
                "error"
            ] == "duration_hours must be greater than 0 and at most 8"


# ---------------------------------------------------------------------------
# GET /api/overrides
# ---------------------------------------------------------------------------


class TestListOverrides:
    async def test_empty_initially(self, client):
        resp = await client.get("/api/overrides")
        assert resp.status == 200
        assert await resp.json() == []

    async def test_lists_live_holds_with_countdown(self, client):
        room_a = await _create_room(client, name="Bedroom")
        room_b = await _create_room(client, name="Kitchen")
        await client.post(
            f"/api/rooms/{room_a['id']}/override",
            json={"target_temp": 72.0, "duration_hours": 2.0, "respect_eco": True},
        )
        await client.post(
            f"/api/rooms/{room_b['id']}/override",
            json={"target_temp": 68.0, "duration_hours": 4.0},
        )
        listing = await (await client.get("/api/overrides")).json()
        by_room = {o["room_id"]: o for o in listing}
        assert set(by_room) == {room_a["id"], room_b["id"]}

        hold_a = by_room[room_a["id"]]
        assert hold_a["target_temp"] == 72.0
        assert hold_a["respect_eco"] is True
        assert 2 * 3600 - 30 <= hold_a["ends_in_seconds"] <= 2 * 3600
        # expires_at is a naive-UTC ISO string, matching the POST response.
        expires = datetime.fromisoformat(hold_a["expires_at"])
        assert expires.tzinfo is None

        hold_b = by_room[room_b["id"]]
        assert hold_b["target_temp"] == 68.0
        assert hold_b["respect_eco"] is False
        assert 4 * 3600 - 30 <= hold_b["ends_in_seconds"] <= 4 * 3600

    async def test_expired_rows_hidden_before_any_sweep(self, client):
        room = await _create_room(client)
        conn = client.app["scheduler"]._db_conn
        await _db.set_room_override(
            conn,
            RoomOverride(
                room_id=room["id"],
                target_temp=72.0,
                expires_at=datetime.now(UTC) - timedelta(minutes=1),
            ),
        )
        # No tick has run, so the expired row is still in the table…
        assert await _db.get_room_override(conn, room["id"]) is not None
        # …but the listing filters on expiry rather than trusting the sweep.
        assert await (await client.get("/api/overrides")).json() == []


# ---------------------------------------------------------------------------
# Expiry sweep + event-log lines
# ---------------------------------------------------------------------------


class TestHoldExpirySweep:
    async def test_tick_sweeps_expired_and_keeps_live(self, client, fake_ha, tick):
        _seed_thermostat(fake_ha)
        expired_room = await _create_room(client, name="Bedroom")
        live_room = await _create_room(client, name="Kitchen")
        conn = client.app["scheduler"]._db_conn
        now = datetime.now(UTC)
        await _db.set_room_override(
            conn,
            RoomOverride(
                room_id=expired_room["id"],
                target_temp=72.0,
                expires_at=now - timedelta(minutes=5),
            ),
        )
        await _db.set_room_override(
            conn,
            RoomOverride(
                room_id=live_room["id"],
                target_temp=68.0,
                expires_at=now + timedelta(hours=1),
            ),
        )

        await tick()

        assert await _db.get_room_override(conn, expired_room["id"]) is None
        live = await _db.get_room_override(conn, live_room["id"])
        assert live is not None
        assert live.target_temp == 68.0
        messages = await _messages(client, category="engine")
        assert any(
            m == "Temperature hold expired for Bedroom — resuming schedule/presence control"
            for m in messages
        ), f"missing expiry event, got: {messages}"
        # The live hold must not be reported as expired.
        assert not any(m.startswith("Temperature hold expired for Kitchen") for m in messages)

    async def test_post_and_delete_emit_api_events(self, client):
        room = await _create_room(client)
        url = f"/api/rooms/{room['id']}/override"
        resp = await client.post(url, json={"target_temp": 75.0, "duration_hours": 1.0})
        assert resp.status == 200
        messages = await _messages(client, category="api")
        assert any(m.startswith("Temperature hold set for room Living Room") for m in messages)

        resp = await client.delete(url)
        assert resp.status == 200
        messages = await _messages(client, category="api")
        assert any(
            m.startswith("Temperature hold cancelled for room Living Room") for m in messages
        )

    async def test_delete_without_hold_emits_no_cancelled_event(self, client):
        room = await _create_room(client)
        resp = await client.delete(f"/api/rooms/{room['id']}/override")
        # Idempotent: still cleared=true…
        assert resp.status == 200
        assert (await resp.json())["cleared"] is True
        # …but a no-op deletion is not worth an event-log line.
        messages = await _messages(client, category="api")
        assert not any(m.startswith("Temperature hold cancelled") for m in messages)


# ---------------------------------------------------------------------------
# Celsius mode
# ---------------------------------------------------------------------------


class TestHoldCelsius:
    async def test_celsius_hold_stores_and_lists_raw_f(self, client):
        room = await _create_room(client)
        client.app["scheduler"]._active_unit = "C"
        try:
            resp = await client.post(
                f"/api/rooms/{room['id']}/override",
                json={"target_temp": 22.0, "respect_eco": True},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["target_temp"] == 71.6  # 22°C → 71.6°F at the write boundary
            assert data["respect_eco"] is True
            # The listing returns raw °F — the frontend converts for display,
            # so a °C value here would be a #231-style double conversion.
            listing = await (await client.get("/api/overrides")).json()
            assert len(listing) == 1
            assert listing[0]["target_temp"] == 71.6
        finally:
            client.app["scheduler"]._active_unit = "F"
