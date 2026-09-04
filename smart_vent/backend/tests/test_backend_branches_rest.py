"""Untaken halves of guards outside the cycle engine / db / scheduler.

Branch coverage (``branch = true``) exposed a set of partial branches whose
False (or loop-exhaustion) arm no other test reached: defensive re-reads in the
room status endpoint, the "nothing to clean up" arms of the HA WebSocket
teardown, the optional-body arms of a few REST endpoints, and the
"control declares no bounds" shape of an MQTT Discovery number.

Every test here asserts the *behaviour* of the untaken arm, not merely that the
line ran.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import sys
import tempfile
from datetime import UTC, datetime, time, timedelta

import aiosqlite
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from backend import db
from backend.api import openapi as openapi_mod
from backend.api import routes as routes_mod
from backend.api.ws_handler import WSManager
from backend.engine import room_manager
from backend.engine.room_manager import ActiveRoom, get_room_active_status
from backend.engine.vent_controller import VentController
from backend.main import build_app, security_headers_middleware
from backend.models import (
    CycleLog,
    PresenceHoldoverState,
    Room,
    RoomCycleState,
    RoomVent,
    Schedule,
    ThermostatConfig,
)
from backend.mqtt import discovery
from backend.mqtt.registry import KIND_NUMBER, Control

from .integration.fake_ha import FakeHomeAssistant

THERMO_ID = "climate.branch_probe"


# ---------------------------------------------------------------------------
# Local app / client fixtures (this file lives outside tests/integration/, so
# it cannot use that package's conftest).
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_ha() -> FakeHomeAssistant:
    return FakeHomeAssistant()


@pytest.fixture
def db_file():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        yield path
    finally:
        for suffix in ("", "-wal", "-shm"):
            p = path + suffix
            if os.path.exists(p):
                with contextlib.suppress(OSError):
                    os.unlink(p)


@pytest.fixture
async def client(fake_ha: FakeHomeAssistant, db_file: str):
    app = build_app(fake_ha, db_file, frontend_dist=None, start_ha=False)  # type: ignore[arg-type]
    async with TestClient(TestServer(app)) as c:
        await c.start_server()
        yield c


async def _memory_db() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await db.init_db(conn)
    return conn


# ---------------------------------------------------------------------------
# backend/api/openapi.py
# ---------------------------------------------------------------------------


class TestOpenApiDecorators:
    def test_docs_without_a_summary_leaves_the_default_in_place(self) -> None:
        """``docs(summary="")`` must not stamp an empty summary over the
        default — the extras still land (openapi.py 64->66)."""

        async def handler(_request: web.Request) -> web.Response:  # pragma: no cover - never run
            return web.Response()

        openapi_mod.docs(tags=["probe"], deprecated=True)(handler)

        meta = handler.__apispec__  # type: ignore[attr-defined]
        assert meta["summary"] == ""
        assert meta["tags"] == ["probe"]
        assert meta["deprecated"] is True

    async def test_swagger_path_without_trailing_slash_registers_no_redirect(self) -> None:
        """When the docs path has no trailing slash there is no bare form to
        redirect from, so the redirect route is skipped and the static assets
        still mount (openapi.py 262->270)."""
        app = web.Application()
        openapi_mod.setup_openapi(
            app,
            url="/api/docs/openapi.json",
            swagger_path="/api/docs",
            static_path="/api/docs/static",
        )

        async with TestClient(TestServer(app)) as c:
            await c.start_server()
            resp = await c.get("/api/docs")
            assert resp.status == 200
            # Served directly, not via a 302 to a trailing-slash form.
            assert resp.history == ()
            assert resp.content_type == "text/html"
            assert (await c.get("/api/docs/openapi.json")).status == 200


# ---------------------------------------------------------------------------
# backend/api/ws_handler.py
# ---------------------------------------------------------------------------


class TestWsManagerReadLoop:
    async def test_non_error_frame_is_ignored_and_the_socket_stays_open(self) -> None:
        """Only a WS ERROR frame breaks the read loop; a TEXT frame is dropped
        and the client keeps receiving broadcasts (ws_handler.py 30->29)."""
        mgr = WSManager()
        finished = asyncio.Event()

        async def handler(request: web.Request) -> web.WebSocketResponse:
            try:
                return await mgr.handle(request)
            finally:
                finished.set()

        app = web.Application()
        app.router.add_get("/ws", handler)

        async with TestClient(TestServer(app)) as c:
            await c.start_server()
            ws = await c.ws_connect("/ws")
            await ws.send_str("clients never send commands")
            await mgr.broadcast("zone_status", {"n": 1})
            payload = await asyncio.wait_for(ws.receive_json(), timeout=5)
            assert payload == {"type": "zone_status", "data": {"n": 1}}
            await ws.close()
            # The handler only returns once the loop ended, which is after the
            # TEXT frame was consumed without breaking.
            await asyncio.wait_for(finished.wait(), timeout=5)


# ---------------------------------------------------------------------------
# backend/engine/room_manager.py
# ---------------------------------------------------------------------------


def _schedule(**kw) -> Schedule:
    defaults: dict = {
        "id": "s1",
        "room_id": "r1",
        "days_of_week": [0, 1, 2, 3, 4, 5, 6],
        "start_time": time(8, 0),
        "end_time": time(17, 0),
        "target_temp": 70.0,
    }
    defaults.update(kw)
    return Schedule(**defaults)


class TestNextScheduleStart:
    def test_a_schedule_with_no_days_never_produces_a_candidate(self) -> None:
        """The 8-day lookahead runs to exhaustion (no ``break``) when a block
        matches no weekday, and the caller reports "no next schedule"
        (room_manager.py 319->312)."""
        now = datetime(2025, 6, 4, 10, 0, tzinfo=UTC)  # Wed
        never = _schedule(id="never", days_of_week=[])

        assert room_manager._next_schedule_start([never], now) is None

        # A real block alongside it still resolves, proving the exhausted inner
        # loop only skipped its own schedule.
        real = _schedule(id="real", days_of_week=[2], start_time=time(20, 0))
        found = room_manager._next_schedule_start([never, real], now)
        assert found is not None
        assert found[2] == "Wed 8:00 PM"


class TestRoomActiveStatusDefensiveReads:
    """``get_room_active_status`` re-reads the row that ``_resolve_room``
    matched on. If it has since gone (a hold deleted, a block edited, a
    holdover cleared between the two reads) the endpoint must still answer —
    with ``ends_in_seconds`` simply unknown — rather than raise."""

    async def _room(self, conn: aiosqlite.Connection) -> Room:
        room = Room(id="r1", name="Probe", thermostat_entity_id=THERMO_ID)
        await db.upsert_room(conn, room)
        return room

    def _force_source(self, monkeypatch: pytest.MonkeyPatch, room: Room, source: str) -> None:
        async def _resolved(_conn, _room, _schedules, _now):
            return ActiveRoom(room=room, target_temp=71.0, source=source)

        monkeypatch.setattr(room_manager, "_resolve_room", _resolved)

    async def test_override_source_with_no_override_row(self, monkeypatch) -> None:
        """room_manager.py 390->405."""
        conn = await _memory_db()
        try:
            room = await self._room(conn)
            self._force_source(monkeypatch, room, "override")
            assert await db.get_room_override(conn, room.id) is None

            status = await get_room_active_status(conn, room, [], now=datetime.now(UTC))
            assert status["source"] == "override"
            assert status["ends_in_seconds"] is None
            assert status["override_respect_eco"] is None
        finally:
            await conn.close()

    async def test_schedule_source_with_no_matching_block(self, monkeypatch) -> None:
        """room_manager.py 396->405. The next-schedule lookahead must not be
        excluded by a stale ``current_schedule_id``."""
        conn = await _memory_db()
        try:
            room = await self._room(conn)
            self._force_source(monkeypatch, room, "schedule")
            now = datetime(2025, 6, 4, 10, 0, tzinfo=UTC)  # Wed 10:00
            upcoming = _schedule(id="later", days_of_week=[2], start_time=time(20, 0))

            status = await get_room_active_status(conn, room, [upcoming], now=now)
            assert status["source"] == "schedule"
            assert status["ends_in_seconds"] is None
            # Nothing was excluded, so the 20:00 block is still announced.
            assert status["next_schedule_label"] == "Wed 8:00 PM"
        finally:
            await conn.close()

    async def test_presence_source_with_no_holdover_row(self, monkeypatch) -> None:
        """room_manager.py 401->405."""
        conn = await _memory_db()
        try:
            room = await self._room(conn)
            self._force_source(monkeypatch, room, "presence")
            assert await db.get_holdover_state(conn, room.id) is None

            status = await get_room_active_status(conn, room, [], now=datetime.now(UTC))
            assert status["source"] == "presence"
            assert status["ends_in_seconds"] is None
            assert status["presence_holdover_active"] is False
        finally:
            await conn.close()

    async def test_holdover_row_still_reports_a_countdown(self) -> None:
        """Control for the three tests above: with the row present the same
        code path fills ``ends_in_seconds`` in."""
        conn = await _memory_db()
        try:
            room = Room(
                id="r1", name="Probe", thermostat_entity_id=THERMO_ID, system_wide_temp=71.0
            )
            await db.upsert_room(conn, room)
            now = datetime.now(UTC)
            await db.upsert_holdover_state(
                conn,
                PresenceHoldoverState(
                    room_id=room.id,
                    last_detected_at=now,
                    expires_at=now + timedelta(hours=1),
                ),
            )
            status = await get_room_active_status(conn, room, [], now=now)
            assert status["source"] == "presence"
            assert status["ends_in_seconds"] == pytest.approx(3600, abs=2)
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# backend/engine/vent_controller.py
# ---------------------------------------------------------------------------


def _vent(room_id: str, entity_id: str) -> RoomVent:
    return RoomVent.create(room_id=room_id, entity_id=entity_id)


class _FakeHa:
    def __init__(self, states: dict[str, str]) -> None:
        self._states = states
        self.opened: list[str] = []
        self.closed: list[str] = []

    def get_state(self, entity_id: str):
        if entity_id in self._states:
            return {"state": self._states[entity_id]}
        return None

    async def open_cover(self, entity_id: str) -> None:
        self.opened.append(entity_id)
        self._states[entity_id] = "open"

    async def close_cover(self, entity_id: str) -> None:
        self.closed.append(entity_id)
        self._states[entity_id] = "closed"


class TestVentControllerGuards:
    async def test_close_accepts_an_explicit_now(self) -> None:
        """``now`` is normally defaulted; callers inside a tick pass the tick's
        instant instead (vent_controller.py 153->156)."""
        ha = _FakeHa({"cover.a": "open", "cover.b": "open"})
        ctrl = VentController(ha)  # type: ignore[arg-type]
        tc = ThermostatConfig(thermostat_entity_id=THERMO_ID, has_bypass_damper=True)
        vents = [_vent("r1", "cover.a")]

        pinned = datetime(2025, 6, 4, 10, 0, tzinfo=UTC)
        closed = await ctrl.close_room_vents(
            vents,
            vents + [_vent("r2", "cover.b")],
            tc,
            {},
            now=pinned,
        )
        assert closed is True
        assert ha.closed == ["cover.a"]

    async def test_expired_room_with_no_vents_is_skipped_not_reopened(self) -> None:
        """A stale cycle-state row whose room has since lost its vents must be
        passed over — and must not stop the next room from being reopened
        (vent_controller.py 228->223)."""
        ha = _FakeHa({"cover.real": "closed"})
        ctrl = VentController(ha)  # type: ignore[arg-type]
        tc = ThermostatConfig(thermostat_entity_id=THERMO_ID, max_vent_closed_min=30)

        closed_at = datetime(2025, 6, 4, 10, 0, tzinfo=UTC)
        now = closed_at + timedelta(minutes=45)
        ghost = RoomCycleState(
            cycle_id="c1", room_id="ghost", target_temp=70.0, vent_closed_at=closed_at
        )
        real = RoomCycleState(
            cycle_id="c1", room_id="real", target_temp=70.0, vent_closed_at=closed_at
        )

        conn = await _memory_db()
        try:
            await db.upsert_room(conn, Room(id="real", name="Real", thermostat_entity_id=THERMO_ID))
            cycle = CycleLog.create(thermostat_entity_id=THERMO_ID, mode="cooling", rooms_json="{}")
            cycle.id = "c1"
            await db.insert_cycle_log(conn, cycle)
            await db.upsert_room_cycle_state(conn, real)

            reopened = await ctrl.check_max_closed_duration(
                conn,
                {"real": [_vent("real", "cover.real")]},  # "ghost" has no vents
                {"ghost": ghost, "real": real},
                tc,
                now=now,
            )
            assert reopened == ["real"]
            assert ha.opened == ["cover.real"]
            # The vent-less room's timer is left alone rather than cleared.
            assert ghost.vent_closed_at == closed_at
            assert real.vent_closed_at is None
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# backend/mqtt/discovery.py
# ---------------------------------------------------------------------------


class TestDiscoveryNumberWithoutBounds:
    def test_a_bare_number_control_advertises_no_min_max_step_or_unit(self) -> None:
        """A number control that declares no bounds, no step and no unit must
        produce a payload with none of those keys — and no temperature
        conversion happens in the bridge either way, per #519/#231
        (discovery.py 200->206, 206->212, 212->216, 223->258)."""
        control = Control(
            key="bare_number",
            entity="number",
            name="Bare Number",
            kind=KIND_NUMBER,
            field="bare_number",
        )
        assert (control.min, control.max, control.step, control.temp, control.unit) == (
            None,
            None,
            None,
            None,
            None,
        )

        info = discovery.device_block("plenum", "room", "room-guid-1", "Plenum Probe")
        entities = discovery.build_entities(
            control,
            prefix="plenum",
            discovery_prefix="homeassistant",
            device="room",
            ident="room-guid-1",
            topic_ident="room-guid-1",
            device_info=info,
            unit="C",
        )
        assert len(entities) == 1
        payload = entities[0].payload
        for absent in ("min", "max", "step", "unit_of_measurement", "device_class"):
            assert absent not in payload, f"{absent} must be omitted for an unbounded control"
        assert payload["mode"] == "box"
        assert entities[0].topic.startswith("homeassistant/number/")


# ---------------------------------------------------------------------------
# backend/ha_client.py
# ---------------------------------------------------------------------------


def _ha_client():
    from backend.ha_client import HAClient

    return HAClient("http://ha.invalid:8123", "token")


class TestHaClientTeardownAndDispatch:
    async def test_reconnect_loop_exits_when_stopped_during_the_backoff(self, monkeypatch) -> None:
        """``stop()`` landing while the loop is sleeping out its backoff must
        end ``start()`` at the ``while`` header, not one iteration later
        (ha_client.py 109->exit)."""
        import backend.ha_client as ha_mod

        client = _ha_client()
        attempts = 0

        async def _connect() -> None:
            nonlocal attempts
            attempts += 1

        slept: list[float] = []
        real_sleep = asyncio.sleep

        async def _sleep(delay, *a, **kw):
            slept.append(delay)
            client._running = False  # the stop() lands mid-backoff
            return await real_sleep(0)

        monkeypatch.setattr(client, "_connect", _connect)
        monkeypatch.setattr(ha_mod, "_MIN_RECONNECT_DELAY_S", 0)
        monkeypatch.setattr(ha_mod.asyncio, "sleep", _sleep)

        await asyncio.wait_for(client.start(), timeout=5)

        assert attempts == 1, "the loop must not start a second connection"
        assert slept == [0]
        assert client._session is not None
        await client._session.close()

    async def test_teardown_leaves_already_resolved_futures_alone(self) -> None:
        """The disconnect sweep only fails futures still in flight; one that
        already carries a result keeps it (ha_client.py 518->517)."""

        class _BoomSession:
            def ws_connect(self, *_a, **_kw):
                raise ConnectionResetError("peer went away")

        client = _ha_client()
        client._session = _BoomSession()

        loop = asyncio.get_running_loop()
        settled: asyncio.Future = loop.create_future()
        settled.set_result({"already": "answered"})
        in_flight: asyncio.Future = loop.create_future()
        client._pending = {1: settled, 2: in_flight}

        with pytest.raises(ConnectionResetError):
            await client._connect()

        assert settled.result() == {"already": "answered"}
        with pytest.raises(RuntimeError, match="HA WebSocket disconnected"):
            in_flight.result()
        assert client._pending == {}
        assert client._ws is None

    async def test_non_state_changed_events_are_ignored(self) -> None:
        """Only ``state_changed`` events touch the cache or fire listeners
        (ha_client.py 579->exit)."""
        client = _ha_client()
        seen: list[tuple[str, dict]] = []

        async def _cb(entity_id: str, state: dict) -> None:
            seen.append((entity_id, state))

        client._wildcard_listeners.append(_cb)

        await client._dispatch(
            {
                "type": "event",
                "event": {
                    "event_type": "call_service",
                    "data": {"entity_id": "climate.x", "new_state": {"state": "heat"}},
                },
            }
        )

        assert client._state_cache == {}
        assert seen == []
        assert client._dispatch_tasks == set()

    async def test_send_failure_does_not_cancel_an_already_resolved_future(self) -> None:
        """A send that fails *after* the reply landed must re-raise without
        cancelling the future that already holds the result
        (ha_client.py 605->607)."""
        client = _ha_client()

        class _RacyWs:
            async def send_json(self, payload: dict) -> None:
                # The reply is dispatched before the write is reported failed.
                client._pending[payload["id"]].set_result({"raced": True})
                raise ConnectionResetError("write failed after the reply landed")

        client._ws = _RacyWs()
        client._connected.set()

        with pytest.raises(ConnectionResetError):
            await client._send({"type": "ping"})

        assert client._pending == {}


# ---------------------------------------------------------------------------
# backend/main.py
# ---------------------------------------------------------------------------


class TestSecurityHeadersMiddleware:
    async def test_a_prepared_http_exception_is_re_raised_untouched(self) -> None:
        """Headers cannot be written onto a response that already started
        streaming, so the middleware re-raises without touching it
        (main.py 109->111)."""
        request = make_mocked_request("GET", "/already-streaming")
        ex = web.HTTPNotFound()
        await ex.prepare(request)
        assert ex.prepared

        async def handler(_request: web.Request) -> web.StreamResponse:
            raise ex

        with pytest.raises(web.HTTPNotFound) as caught:
            await security_headers_middleware(request, handler)

        assert caught.value is ex
        assert "X-Content-Type-Options" not in caught.value.headers


class TestOptionalSubsystemStartupFailures:
    async def test_mqtt_bridge_failure_before_the_session_exists(
        self, monkeypatch, tmp_path
    ) -> None:
        """When the failure precedes ``ClientSession()`` there is nothing to
        close — the helper must still swallow it (main.py 550->552)."""
        import backend.main as main

        monkeypatch.setenv("MQTT_HOST", "broker.invalid")

        def _boom(*_a, **_kw):
            raise RuntimeError("no event loop for you")

        monkeypatch.setattr(main.aiohttp, "ClientSession", _boom)
        app = build_app(
            FakeHomeAssistant(),  # type: ignore[arg-type]
            str(tmp_path / "mqtt.db"),
            frontend_dist=None,
            start_ha=False,
        )
        assert await main._start_mqtt_bridge(app) is None
        assert app["mqtt"].get("bridge") is None

    async def test_mcp_server_failure_before_the_session_exists(
        self, monkeypatch, tmp_path
    ) -> None:
        """``import uvicorn`` is the first statement in the try block; when it
        fails there is no session to close (main.py 623->625)."""
        import backend.main as main

        monkeypatch.setitem(sys.modules, "uvicorn", None)
        app = build_app(
            FakeHomeAssistant(),  # type: ignore[arg-type]
            str(tmp_path / "mcp.db"),
            frontend_dist=None,
            start_ha=False,
        )
        assert await main._start_mcp_server(app) is None


# ---------------------------------------------------------------------------
# backend/api/routes.py
# ---------------------------------------------------------------------------


class TestEmitWithoutAnEventLogger:
    async def test_emit_is_a_no_op_when_the_app_has_no_event_logger(self) -> None:
        """``emit`` is called from handlers that also run before startup wires
        the logger in (routes.py 85->exit)."""
        app = web.Application()
        request = make_mocked_request("GET", "/", app=app)
        assert "event_logger" not in app
        # Must not raise.
        await routes_mod.emit(request, "info", "system", "no logger attached")


class TestHaEntitiesDedupe:
    async def test_repeated_domains_yield_each_entity_once(self, client, fake_ha) -> None:
        """The comma-separated domain filter dedupes across passes, so an
        entity already collected is skipped (routes.py 2211->2209)."""
        fake_ha.seed_state("climate.one", "cool")
        fake_ha.seed_state("climate.two", "heat")

        resp = await client.get("/api/ha/entities?domain=climate,climate")
        assert resp.status == 200
        ids = [e["entity_id"] for e in await resp.json()]
        assert ids == ["climate.one", "climate.two"]


class TestLogRetentionPartialUpdates:
    async def test_only_the_cycle_field_is_updated(self, client) -> None:
        """routes.py 2774->2777."""
        before = await (await client.get("/api/settings/log-retention")).json()
        resp = await client.post(
            "/api/settings/log-retention", json={"cycle_log_retention_days": 45}
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["cycle_log_retention_days"] == 45
        assert body["event_log_retention_days"] == before["event_log_retention_days"]

    async def test_only_the_event_field_is_updated(self, client) -> None:
        """routes.py 2777->2780."""
        before = await (await client.get("/api/settings/log-retention")).json()
        resp = await client.post(
            "/api/settings/log-retention", json={"event_log_retention_days": 3}
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["event_log_retention_days"] == 3
        assert body["cycle_log_retention_days"] == before["cycle_log_retention_days"]


class TestOptionalRequestBodies:
    async def test_monthly_rollup_accepts_no_body_at_all(self, client) -> None:
        """The UI button posts nothing; the handler must default
        ``months_back`` (routes.py 3062->3067)."""
        resp = await client.post("/api/metrics/rollup/monthly")
        assert resp.status == 200
        assert (await resp.json())["months_back"] == 1

    async def test_demo_seed_accepts_no_body_at_all(self, client) -> None:
        """routes.py 3694->3699."""
        assert (await client.post("/api/system/dev-mode", json={"dev_mode": True})).status == 200
        resp = await client.post("/api/dev/seed-demo-metrics")
        assert resp.status == 200, await resp.text()
        assert (await resp.json())["start_date"] == "2025-06-01"


class TestRestoreFailureAfterTheTempFileMoved:
    async def test_no_temp_file_left_to_clean_up(self, client, monkeypatch) -> None:
        """When the swap succeeded and the *reload* is what failed, the temp
        file is already gone — the cleanup guard skips it and the handler still
        returns a generic message (routes.py 3805->3807, CWE-209)."""
        db_bytes = await (await client.get("/api/backup")).read()
        assert db_bytes[:16] == b"SQLite format 3\x00"

        scheduler = client.app["scheduler"]

        async def _boom() -> None:
            raise RuntimeError("connection to /secret/path/app.db is poisoned")

        monkeypatch.setattr(scheduler, "reload_db", _boom)

        resp = await client.post("/api/restore", data={"file": io.BytesIO(db_bytes)})
        assert resp.status == 500
        text = await resp.text()
        assert (await resp.json())["error"] == "Restore failed"
        assert "secret" not in text
        assert "poisoned" not in text
        assert "RuntimeError" not in text
