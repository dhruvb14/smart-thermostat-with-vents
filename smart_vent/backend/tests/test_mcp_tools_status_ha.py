"""Coverage for the read-only MCP status tools and the HA entity-discovery tool.

``backend/mcp_tools/status.py`` and ``backend/mcp_tools/ha_entities.py`` were
registered and schema-checked by ``test_mcp_server.py`` but never actually
invoked, so every statement in their bodies was unexecuted. These tests call
each tool through the real ``MCPServer`` (same path an MCP client takes) and
assert the payload it produces.

``list_ha_entities`` is the only MCP tool that talks to Home Assistant over
HTTP. Nothing here makes a real request: ``aiohttp`` is replaced in the module
namespace by a recording double, so the URL, headers and TLS context the tool
builds are asserted directly.
"""

from __future__ import annotations

import builtins
import importlib
import json
import types
from datetime import UTC, datetime, time

import aiosqlite
import pytest

from backend import db
from backend.mcp_server import build_server
from backend.mcp_tools import ha_entities
from backend.models import CycleLog, PresenceHoldoverState, Room, RoomOverride, Schedule


def _text(result) -> str:
    """First text block of a tool result (mcp v2 returns a CallToolResult)."""
    return str(result.content[0].text)


async def _conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await db.init_db(conn)
    return conn


# ---------------------------------------------------------------------------
# status.py — get_system_status
# ---------------------------------------------------------------------------


class TestGetSystemStatus:
    async def test_empty_db_returns_empty_array(self):
        conn = await _conn()
        try:
            server = build_server(conn)
            result = await server.call_tool("get_system_status", {})
            assert json.loads(_text(result)) == []
        finally:
            await conn.close()

    async def test_room_without_override_or_holdover_reports_nulls(self):
        conn = await _conn()
        try:
            room = Room.create(
                name="Upstairs Office",
                thermostat_entity_id="climate.upstairs",
                system_wide_temp=71.5,
                presence_holdover_hours=3.0,
            )
            await db.upsert_room(conn, room)

            server = build_server(conn)
            data = json.loads(_text(await server.call_tool("get_system_status", {})))

            assert len(data) == 1
            entry = data[0]
            assert entry["room_id"] == room.id
            assert entry["name"] == "Upstairs Office"
            assert entry["thermostat_entity_id"] == "climate.upstairs"
            assert entry["system_wide_temp"] == 71.5
            assert entry["presence_holdover_hours"] == 3.0
            assert entry["active_override"] is None
            assert entry["presence_holdover"] is None
            assert entry["schedule_count"] == 0

        finally:
            await conn.close()

    async def test_room_with_override_holdover_and_schedules(self):
        conn = await _conn()
        try:
            room = Room.create(name="Den", thermostat_entity_id="climate.main")
            await db.upsert_room(conn, room)

            expires = datetime(2025, 6, 4, 18, 0, 0, tzinfo=UTC)
            detected = datetime(2025, 6, 4, 16, 0, 0, tzinfo=UTC)
            await db.set_room_override(
                conn,
                RoomOverride(
                    room_id=room.id,
                    target_temp=68.0,
                    expires_at=expires,
                    respect_eco=True,
                ),
            )
            await db.upsert_holdover_state(
                conn,
                PresenceHoldoverState(
                    room_id=room.id,
                    last_detected_at=detected,
                    expires_at=expires,
                ),
            )
            for start, end in ((time(8, 0), time(12, 0)), (time(20, 0), time(22, 0))):
                await db.upsert_schedule(
                    conn,
                    Schedule.create(
                        room_id=room.id,
                        days_of_week=[0, 1, 2, 3, 4],
                        start_time=start,
                        end_time=end,
                        target_temp=70.0,
                    ),
                )

            server = build_server(conn)
            entry = json.loads(_text(await server.call_tool("get_system_status", {})))[0]

            # Stored naive-UTC and read back as aware UTC, so the tool's
            # isoformat() carries the +00:00 offset. Exact, clock-independent.
            assert entry["active_override"] == {
                "target_temp": 68.0,
                "expires_at": "2025-06-04T18:00:00+00:00",
                "respect_eco": True,
            }
            assert entry["presence_holdover"] == {
                "last_detected_at": "2025-06-04T16:00:00+00:00",
                "expires_at": "2025-06-04T18:00:00+00:00",
            }
            assert entry["schedule_count"] == 2
        finally:
            await conn.close()

    async def test_reports_every_room_independently(self):
        """The per-room lookups must be keyed on that room, not leak across."""
        conn = await _conn()
        try:
            held = Room.create(name="Room A", thermostat_entity_id="climate.a")
            plain = Room.create(name="Room B", thermostat_entity_id="climate.b")
            await db.upsert_room(conn, held)
            await db.upsert_room(conn, plain)
            await db.set_room_override(
                conn,
                RoomOverride(
                    room_id=held.id,
                    target_temp=66.0,
                    expires_at=datetime(2025, 6, 4, 18, 0, 0, tzinfo=UTC),
                ),
            )

            server = build_server(conn)
            data = json.loads(_text(await server.call_tool("get_system_status", {})))
            by_id = {e["room_id"]: e for e in data}

            assert len(by_id) == 2
            assert by_id[held.id]["active_override"]["target_temp"] == 66.0
            # respect_eco defaults False — the flag round-trips as a bool, not 0.
            assert by_id[held.id]["active_override"]["respect_eco"] is False
            assert by_id[plain.id]["active_override"] is None
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# status.py — get_cycle_logs
# ---------------------------------------------------------------------------


def _cycle(
    cycle_id: str,
    started: datetime,
    ended: datetime | None,
    rooms: list[dict],
    mode: str = "cooling",
) -> CycleLog:
    return CycleLog(
        id=cycle_id,
        thermostat_entity_id="climate.main",
        started_at=started,
        ended_at=ended,
        mode=mode,
        rooms_json=json.dumps(rooms),
    )


class TestGetCycleLogs:
    async def test_empty_db_returns_empty_array(self):
        conn = await _conn()
        try:
            server = build_server(conn)
            assert json.loads(_text(await server.call_tool("get_cycle_logs", {}))) == []
        finally:
            await conn.close()

    async def test_closed_and_open_cycles_render_ended_at(self):
        """An in-flight cycle has no ``ended_at``; it must serialise as null
        rather than blowing up on ``.isoformat()``."""
        conn = await _conn()
        try:
            await db.insert_cycle_log(
                conn,
                _cycle(
                    "cyc-closed",
                    datetime(2025, 6, 1, 10, 0, 0, tzinfo=UTC),
                    datetime(2025, 6, 1, 10, 30, 0, tzinfo=UTC),
                    [{"room_id": "r1", "target_temp": 70.0}],
                    mode="heating",
                ),
            )
            await db.insert_cycle_log(
                conn,
                _cycle(
                    "cyc-open",
                    datetime(2025, 6, 1, 11, 0, 0, tzinfo=UTC),
                    None,
                    [],
                ),
            )

            server = build_server(conn)
            data = json.loads(_text(await server.call_tool("get_cycle_logs", {})))

            # get_cycle_logs orders by started_at DESC — newest first.
            assert [d["id"] for d in data] == ["cyc-open", "cyc-closed"]
            assert data[0]["ended_at"] is None
            assert data[0]["mode"] == "cooling"
            assert data[0]["rooms"] == []
            assert data[1]["ended_at"] == "2025-06-01T10:30:00+00:00"
            assert data[1]["started_at"] == "2025-06-01T10:00:00+00:00"
            assert data[1]["mode"] == "heating"
            assert data[1]["thermostat_entity_id"] == "climate.main"
            # rooms_json is re-inflated, not passed through as a JSON string.
            assert data[1]["rooms"] == [{"room_id": "r1", "target_temp": 70.0}]
        finally:
            await conn.close()

    async def test_limit_argument_is_forwarded(self):
        conn = await _conn()
        try:
            for i in range(5):
                await db.insert_cycle_log(
                    conn,
                    _cycle(
                        f"cyc-{i}",
                        datetime(2025, 6, 1, 10 + i, 0, 0, tzinfo=UTC),
                        None,
                        [],
                    ),
                )

            server = build_server(conn)
            data = json.loads(_text(await server.call_tool("get_cycle_logs", {"limit": 2})))
            assert [d["id"] for d in data] == ["cyc-4", "cyc-3"]

            # Default limit is 10, so all five come back when it is omitted.
            all_data = json.loads(_text(await server.call_tool("get_cycle_logs", {})))
            assert len(all_data) == 5
        finally:
            await conn.close()

    async def test_an_undecodable_snapshot_costs_only_its_own_rooms_field(self):
        """#604: ``rooms_json`` was decoded with a bare ``json.loads``, so one
        row the add-on could not have written — a hand-edited backup restored
        through /api/restore, or a truncated on-disk value — failed the whole
        tool call and lost every *other* cycle in the listing too. The bad row
        must degrade to an empty ``rooms`` map and the listing must survive."""
        conn = await _conn()
        try:
            await db.insert_cycle_log(
                conn,
                _cycle(
                    "cyc-good",
                    datetime(2025, 6, 1, 10, 0, 0, tzinfo=UTC),
                    None,
                    [{"room_id": "r1"}],
                ),
            )
            await conn.execute(
                "INSERT INTO cycle_logs (id, thermostat_entity_id, started_at, mode, rooms_json)"
                " VALUES (?,?,?,?,?)",
                (
                    "cyc-bad",
                    "climate.main",
                    "2025-06-01T11:00:00+00:00",
                    "cooling",
                    "{not json at all",
                ),
            )
            await conn.commit()

            server = build_server(conn)
            data = json.loads(_text(await server.call_tool("get_cycle_logs", {})))

            assert [d["id"] for d in data] == ["cyc-bad", "cyc-good"]
            assert data[0]["rooms"] == {}
            # The readable neighbour is untouched.
            assert data[1]["rooms"] == [{"room_id": "r1"}]
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# ha_entities.py — HTTP doubles (no network)
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload, raise_exc: Exception | None = None):
        self._payload = payload
        self._raise = raise_exc

    def raise_for_status(self) -> None:
        if self._raise is not None:
            raise self._raise

    async def json(self):
        return self._payload


class _FakeRequestCtx:
    def __init__(self, response: _FakeResponse):
        self._response = response

    async def __aenter__(self) -> _FakeResponse:
        return self._response

    async def __aexit__(self, *exc_info) -> bool:
        return False


class _FakeSession:
    def __init__(self, calls: list[dict], response: _FakeResponse, connector):
        self._calls = calls
        self._response = response
        self.connector = connector
        self.closed = False

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc_info) -> bool:
        self.closed = True
        return False

    def get(self, url, headers=None):
        self._calls.append({"url": url, "headers": headers})
        return _FakeRequestCtx(self._response)


class _Recorder:
    """Stand-in for the ``aiohttp`` module inside ha_entities."""

    def __init__(self, response: _FakeResponse):
        self.response = response
        self.requests: list[dict] = []
        self.connector_kwargs: list[dict] = []
        self.sessions: list[_FakeSession] = []

    # aiohttp.TCPConnector(ssl=...)
    def TCPConnector(self, **kwargs):  # noqa: N802 - mirrors the aiohttp name
        self.connector_kwargs.append(kwargs)
        return types.SimpleNamespace(**kwargs)

    # aiohttp.ClientSession(connector=...)
    def ClientSession(self, connector=None):  # noqa: N802 - mirrors the aiohttp name
        session = _FakeSession(self.requests, self.response, connector)
        self.sessions.append(session)
        return session


@pytest.fixture
def ha_http(monkeypatch):
    """Install a recording ``aiohttp`` double and hand back a factory."""

    def install(payload=None, raise_exc: Exception | None = None) -> _Recorder:
        recorder = _Recorder(_FakeResponse(payload, raise_exc))
        monkeypatch.setattr(ha_entities, "aiohttp", recorder)
        return recorder

    return install


_STATES = [
    {"entity_id": "sensor.zeta_temp", "state": "71.0", "attributes": {"friendly_name": "Zeta"}},
    {"entity_id": "sensor.alpha_temp", "state": "68.0", "attributes": {"friendly_name": "Alpha"}},
    # No friendly_name → the entity_id is the fallback label.
    {"entity_id": "sensor.mid_temp", "state": "70.0", "attributes": {}},
    # No attributes key at all → same fallback, via the .get default.
    {"entity_id": "sensor.bare_temp", "state": "69.0"},
    {"entity_id": "climate.main", "state": "cool", "attributes": {"friendly_name": "Main"}},
    # Prefix match must be on "<domain>." — "sensorfoo" is a different domain.
    {"entity_id": "sensorfoo.decoy", "state": "on", "attributes": {}},
]


class TestListHaEntities:
    async def test_filters_sorts_and_labels(self, ha_http):
        recorder = ha_http(payload=_STATES)
        conn = await _conn()
        try:
            server = build_server(conn)
            text = _text(await server.call_tool("list_ha_entities", {"domain": "sensor"}))

            header, _, body = text.partition("\n")
            assert header == "Found 4 entities in domain 'sensor':"
            entities = json.loads(body)
            assert [e["entity_id"] for e in entities] == [
                "sensor.alpha_temp",
                "sensor.bare_temp",
                "sensor.mid_temp",
                "sensor.zeta_temp",
            ]
            assert entities[0] == {
                "entity_id": "sensor.alpha_temp",
                "state": "68.0",
                "friendly_name": "Alpha",
            }
            # Missing / absent attributes fall back to the entity_id.
            by_id = {e["entity_id"]: e for e in entities}
            assert by_id["sensor.mid_temp"]["friendly_name"] == "sensor.mid_temp"
            assert by_id["sensor.bare_temp"]["friendly_name"] == "sensor.bare_temp"
            assert recorder.sessions and recorder.sessions[0].closed is True
        finally:
            await conn.close()

    async def test_other_domain_and_no_matches(self, ha_http):
        ha_http(payload=_STATES)
        conn = await _conn()
        try:
            server = build_server(conn)

            climate = _text(await server.call_tool("list_ha_entities", {"domain": "climate"}))
            assert climate.startswith("Found 1 entities in domain 'climate':")
            assert json.loads(climate.partition("\n")[2])[0]["entity_id"] == "climate.main"

            empty = _text(await server.call_tool("list_ha_entities", {"domain": "cover"}))
            assert empty.startswith("Found 0 entities in domain 'cover':")
            assert json.loads(empty.partition("\n")[2]) == []
        finally:
            await conn.close()

    async def test_uses_ha_url_and_bearer_token_from_env(self, ha_http, monkeypatch):
        recorder = ha_http(payload=[])
        monkeypatch.setenv("HA_URL", "http://ha.local:8123/")
        monkeypatch.setenv("HA_TOKEN", "tok-123")
        conn = await _conn()
        try:
            server = build_server(conn)
            await server.call_tool("list_ha_entities", {"domain": "sensor"})

            assert recorder.requests == [
                {
                    # The trailing slash is stripped — no "//api/states".
                    "url": "http://ha.local:8123/api/states",
                    "headers": {"Authorization": "Bearer tok-123"},
                }
            ]
        finally:
            await conn.close()

    async def test_defaults_when_env_is_unset(self, ha_http, monkeypatch):
        recorder = ha_http(payload=[])
        monkeypatch.delenv("HA_URL", raising=False)
        monkeypatch.delenv("HA_TOKEN", raising=False)
        conn = await _conn()
        try:
            server = build_server(conn)
            await server.call_tool("list_ha_entities", {"domain": "sensor"})

            assert recorder.requests[0]["url"] == "http://homeassistant.local:8123/api/states"
            assert recorder.requests[0]["headers"] == {"Authorization": "Bearer "}
        finally:
            await conn.close()

    async def test_http_error_is_reported_not_raised(self, ha_http):
        ha_http(payload=None, raise_exc=RuntimeError("401, message='Unauthorized'"))
        conn = await _conn()
        try:
            server = build_server(conn)
            text = _text(await server.call_tool("list_ha_entities", {"domain": "sensor"}))
            assert text == "Error fetching HA entities: 401, message='Unauthorized'"
        finally:
            await conn.close()

    async def test_connection_failure_is_reported_not_raised(self, ha_http, monkeypatch):
        """A failure building the session (HA unreachable) is caught too."""
        recorder = ha_http(payload=[])

        def boom(**kwargs):
            raise OSError("Cannot connect to host homeassistant.local:8123")

        monkeypatch.setattr(recorder, "TCPConnector", boom)
        conn = await _conn()
        try:
            server = build_server(conn)
            text = _text(await server.call_tool("list_ha_entities", {"domain": "sensor"}))
            assert text.startswith("Error fetching HA entities: Cannot connect to host")
            assert recorder.requests == []
        finally:
            await conn.close()

    async def test_connector_uses_the_certifi_ssl_context(self, ha_http):
        recorder = ha_http(payload=[])
        conn = await _conn()
        try:
            server = build_server(conn)
            await server.call_tool("list_ha_entities", {"domain": "sensor"})
            assert recorder.connector_kwargs == [{"ssl": ha_entities._SSL_CONTEXT}]
            assert ha_entities._SSL_CONTEXT is not None
        finally:
            await conn.close()

    async def test_connector_falls_back_to_true_without_an_ssl_context(self, ha_http, monkeypatch):
        """``ssl=None`` is rejected by aiohttp, so the module passes ``True``
        (aiohttp's default verification) when certifi never loaded."""
        recorder = ha_http(payload=[])
        monkeypatch.setattr(ha_entities, "_SSL_CONTEXT", None)
        conn = await _conn()
        try:
            server = build_server(conn)
            await server.call_tool("list_ha_entities", {"domain": "sensor"})
            assert recorder.connector_kwargs == [{"ssl": True}]
        finally:
            await conn.close()


class TestCertifiImportFallback:
    def test_missing_certifi_leaves_the_ssl_context_none(self, monkeypatch):
        """Reload the module with ``import certifi`` failing, exercising the
        ``except ImportError`` arm, then restore the real module state."""
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "certifi":
                raise ImportError("No module named 'certifi'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        try:
            reloaded = importlib.reload(ha_entities)
            assert reloaded._SSL_CONTEXT is None
        finally:
            monkeypatch.undo()
            importlib.reload(ha_entities)

        # Restored: with certifi present the module builds a real context.
        assert ha_entities._SSL_CONTEXT is not None
