"""End-to-end MCP conformance suite (Issue #543).

This is the regression baseline for the mcp Python SDK v1 → v2 migration. It
drives **real MCP tool calls over the wire** and then proves the resulting state
three ways — MCP read-back, REST cross-check, and the actual Home Assistant
entity — so a migration that silently stops applying a mutation cannot pass.

Two targets, one test body
--------------------------
``PLENUM_CONFORMANCE_TARGET`` selects where the suite points:

* **in-process** (default) — boots the real aiohttp app against
  :class:`FakeHomeAssistant` plus a real uvicorn MCP server on a loopback port.
  Runs in ``lint.yml`` on every PR, no Docker needed.
* **container** — points at an already-running ``docker-compose.test.yml`` stack
  via ``PLENUM_MCP_URL`` / ``PLENUM_REST_URL`` / ``PLENUM_HA_URL`` /
  ``PLENUM_HA_TOKEN``, so the assertions run against a real Home Assistant and
  the real published image.

Both targets execute the *same* assertions; only the fixtures differ. The wire
driver (:mod:`.mcp_wire`) imports nothing from ``mcp``, so this file is
unchanged by the SDK migration — any behaviour difference it reports comes from
the server, which is the entire point of a baseline.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import uuid
from collections.abc import AsyncIterator
from typing import Any

import aiohttp
import pytest
import pytest_asyncio

from .mcp_wire import (
    PROTOCOL_REVISIONS,
    RawMcpClient,
    tool_failed,
    tool_result_json,
    tool_result_text,
)

# --------------------------------------------------------------------------
# Target selection
# --------------------------------------------------------------------------

_CONTAINER = os.environ.get("PLENUM_CONFORMANCE_TARGET", "in-process").lower() == "container"


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no")


def _stateless_params() -> list[bool]:
    """Which transport modes this run exercises.

    In-process we boot both a stateless and a stateful server and run everything
    twice. Against a container the mode is baked into the running stack by
    ``PLENUM_MCP_STATELESS``, so we honour that single value and let CI run the
    job once per mode.
    """
    if _CONTAINER:
        return [_env_flag("PLENUM_MCP_STATELESS", True)]
    return [True, False]


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port: int = sock.getsockname()[1]
    sock.close()
    return port


class ConformanceTarget:
    """What a conformance test is allowed to touch, abstracted over both modes."""

    def __init__(
        self,
        *,
        mcp_url: str,
        rest_url: str,
        http: aiohttp.ClientSession,
        stateless: bool,
        rest_headers: dict[str, str] | None = None,
    ) -> None:
        self.mcp_url = mcp_url
        self.rest_url = rest_url.rstrip("/")
        self.http = http
        self.stateless = stateless
        self.rest_headers = rest_headers or {}

    # -- REST cross-check -------------------------------------------------

    async def rest(
        self, method: str, path: str, *, json_body: Any = None, scope: str | None = None
    ) -> tuple[int, Any]:
        headers = dict(self.rest_headers)
        if scope:
            headers["X-Plenum-Scope"] = scope
        async with self.http.request(
            method, f"{self.rest_url}{path}", json=json_body, headers=headers
        ) as resp:
            text = await resp.text()
            try:
                return resp.status, json.loads(text) if text else None
            except json.JSONDecodeError:
                return resp.status, text

    # -- Home Assistant verification --------------------------------------

    async def ha_entity_state(self, entity_id: str) -> dict[str, Any] | None:
        raise NotImplementedError

    async def reset_ha_calls(self) -> None:
        """No-op where service calls are not observable (container mode)."""

    async def ha_service_calls(self) -> list[dict[str, Any]] | None:
        """Recorded HA service calls, or None when not observable."""
        return None

    async def temperature_unit(self) -> str:
        status, body = await self.rest("GET", "/api/settings")
        assert status == 200, f"GET /api/settings returned {status}"
        unit: str = body.get("temperature_unit", "F")
        return unit

    def client(self, *, bearer: str | None = None) -> RawMcpClient:
        return RawMcpClient(self.http, self.mcp_url, bearer=bearer)


class InProcessTarget(ConformanceTarget):
    def __init__(self, *, fake_ha: Any, app: Any, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.fake_ha = fake_ha
        self.app = app

    async def ha_entity_state(self, entity_id: str) -> dict[str, Any] | None:
        state: dict[str, Any] | None = self.fake_ha.get_state(entity_id)
        return state

    async def reset_ha_calls(self) -> None:
        self.fake_ha.reset_calls()

    async def ha_service_calls(self) -> list[dict[str, Any]] | None:
        return [
            {"domain": c.domain, "service": c.service, "data": c.data} for c in self.fake_ha.calls
        ]

    async def temperature_unit(self) -> str:
        # The scheduler holds the unit that ``_to_f`` actually consults, which is
        # the one that matters for the conversion contract.
        unit: str = self.app["scheduler"].get_temperature_unit()
        return unit


class ContainerTarget(ConformanceTarget):
    def __init__(self, *, ha_url: str | None, ha_token: str | None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.ha_url = (ha_url or "").rstrip("/")
        self.ha_token = ha_token

    async def ha_entity_state(self, entity_id: str) -> dict[str, Any] | None:
        # Deliberately NOT tolerant of a missing HA: a conformance run that
        # silently skipped its Home-Assistant assertions would pass vacuously,
        # which is the failure mode this whole suite exists to prevent.
        assert self.ha_url and self.ha_token, (
            "container conformance requires PLENUM_HA_URL and PLENUM_HA_TOKEN so "
            "Home Assistant assertions actually run"
        )
        async with self.http.get(
            f"{self.ha_url}/api/states/{entity_id}",
            headers={"Authorization": f"Bearer {self.ha_token}"},
        ) as resp:
            if resp.status == 404:
                return None
            assert resp.status == 200, f"HA /api/states/{entity_id} returned {resp.status}"
            body = await resp.json()
        return {
            "entity_id": body.get("entity_id"),
            "state": body.get("state"),
            "attributes": body.get("attributes", {}),
        }

    async def ha_call_service(self, domain: str, service: str, entity_id: str) -> None:
        """Drive a Home Assistant service directly, to set up known-state preconditions."""
        assert self.ha_url and self.ha_token, "container conformance requires HA credentials"
        async with self.http.post(
            f"{self.ha_url}/api/services/{domain}/{service}",
            json={"entity_id": entity_id},
            headers={"Authorization": f"Bearer {self.ha_token}"},
        ) as resp:
            assert resp.status == 200, (
                f"HA {domain}.{service} on {entity_id} returned {resp.status}"
            )


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest_asyncio.fixture(params=_stateless_params(), ids=lambda s: "stateless" if s else "stateful")
async def target(request: pytest.FixtureRequest) -> AsyncIterator[ConformanceTarget]:
    stateless: bool = request.param
    async with aiohttp.ClientSession() as http:
        if _CONTAINER:
            mcp_url = os.environ.get("PLENUM_MCP_URL", "http://127.0.0.1:9099/mcp")
            rest_url = os.environ.get("PLENUM_REST_URL", "http://127.0.0.1:8099")
            yield ContainerTarget(
                mcp_url=mcp_url,
                rest_url=rest_url,
                http=http,
                stateless=stateless,
                ha_url=os.environ.get("PLENUM_HA_URL"),
                ha_token=os.environ.get("PLENUM_HA_TOKEN"),
            )
            return

        async for tgt in _in_process_target(http, stateless):
            yield tgt


async def _in_process_target(
    http: aiohttp.ClientSession, stateless: bool
) -> AsyncIterator[ConformanceTarget]:
    """Boot the real app + a real MCP server on loopback."""
    import contextlib
    import tempfile

    import uvicorn
    from aiohttp.test_utils import TestClient, TestServer

    from backend.main import build_app
    from backend.mcp_http import MCP_PATH, build_asgi_app, build_mcp_server
    from backend.mcp_openapi import build_tool_specs

    from .fake_ha import FakeHomeAssistant

    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    fake_ha = FakeHomeAssistant()
    fake_ha.seed_state(
        "climate.conformance_tstat", "heat", {"current_temperature": 68.0, "temperature": 70.0}
    )
    fake_ha.seed_state("cover.conformance_vent", "open", {"current_position": 100})

    app = build_app(fake_ha, db_path, frontend_dist=None, start_ha=False)  # type: ignore[arg-type]
    rest_client = TestClient(TestServer(app))
    await rest_client.start_server()
    rest_url = f"http://127.0.0.1:{rest_client.port}"

    port = _free_port()
    specs = build_tool_specs(app)
    server = build_mcp_server(specs, http, rest_url, app["internal_token"])
    asgi = build_asgi_app(server, lambda: True, stateless=stateless)

    config = uvicorn.Config(asgi, host="127.0.0.1", port=port, log_level="error", lifespan="on")
    uv = uvicorn.Server(config)
    uv.install_signal_handlers = lambda: None  # type: ignore[attr-defined]
    task = asyncio.create_task(uv.serve())
    try:
        while not uv.started:  # noqa: ASYNC110
            await asyncio.sleep(0.05)
        yield InProcessTarget(
            fake_ha=fake_ha,
            app=app,
            mcp_url=f"http://127.0.0.1:{port}{MCP_PATH}",
            rest_url=rest_url,
            http=http,
            stateless=stateless,
            rest_headers={"X-Plenum-Internal": app["internal_token"]},
        )
    finally:
        uv.should_exit = True
        await task
        await rest_client.close()
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(OSError):
                os.unlink(db_path + suffix)


@pytest_asyncio.fixture
async def mcp(target: ConformanceTarget) -> AsyncIterator[RawMcpClient]:
    """An initialized client on the newest revision both sides support."""
    client = target.client()
    await client.initialize(PROTOCOL_REVISIONS[-1])
    yield client


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


# The thermostat rooms are attached to. In-process we seed our own; against the
# container we use one the e2e HA fixture actually defines, because the create
# handler stores whatever entity id it is given and later reads would diverge
# from reality if it named a non-existent entity.
_ROOM_THERMOSTAT = (
    os.environ.get("PLENUM_CONFORMANCE_THERMOSTAT", "climate.downstairs_thermostat")
    if _CONTAINER
    else "climate.conformance_tstat"
)


async def _create_room(
    mcp: RawMcpClient, name: str, **extra: Any
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Create a room over MCP. Returns (raw reply, parsed room)."""
    args = {
        "name": name,
        "thermostat_entity_id": extra.pop("thermostat_entity_id", _ROOM_THERMOSTAT),
        **extra,
    }
    reply = await mcp.call_tool_raw("post_rooms", args)
    assert not tool_failed(reply), f"post_rooms failed: {reply}"
    return reply, tool_result_json(reply)


async def _delete_room(mcp: RawMcpClient, room_id: str) -> None:
    await mcp.call_tool_raw("delete_rooms_room_id", {"room_id": room_id})


# --------------------------------------------------------------------------
# Protocol-level conformance
# --------------------------------------------------------------------------


@pytest.mark.parametrize("revision", PROTOCOL_REVISIONS)
async def test_handshake_negotiates_each_supported_revision(
    target: ConformanceTarget, revision: str
) -> None:
    """Every revision the server advertises must still be usable.

    This is the backward-compatibility guarantee: real clients in the wild
    (Claude Desktop and friends) negotiate older revisions, and mcp v2's
    headline feature is serving both eras from one server. If the migration
    silently drops a revision, this fails.
    """
    client = target.client()
    result = await client.initialize(revision)

    assert result.get("protocolVersion") == revision, (
        f"asked for {revision}, server negotiated {result.get('protocolVersion')}"
    )
    assert result.get("serverInfo", {}).get("name") == "plenum"
    assert "tools" in result.get("capabilities", {})

    # The session must actually be usable on that revision, not merely handshake.
    tools = await client.list_tools()
    assert len(tools) > 50, f"only {len(tools)} tools on revision {revision}"


async def test_session_id_matches_transport_mode(target: ConformanceTarget) -> None:
    """A stateful server issues an Mcp-Session-Id; a stateless one never does.

    Guards the transport-mode switch itself: if PLENUM_MCP_STATELESS stopped
    being honoured, both modes would look identical and the stateful axis of the
    CI matrix would be silently testing nothing.
    """
    client = target.client()
    await client.initialize(PROTOCOL_REVISIONS[-1])

    if target.stateless:
        assert client.session_id is None, (
            f"stateless server unexpectedly issued a session id: {client.session_id}"
        )
    else:
        assert client.session_id, "stateful server issued no Mcp-Session-Id"
        # And the id must actually work for a follow-up request.
        tools = await client.list_tools()
        assert len(tools) > 50


async def test_tool_surface_is_generated_from_the_rest_api(mcp: RawMcpClient) -> None:
    """The generated tool surface keeps its wire shape.

    ``inputSchema`` is camelCase **on the wire** in both SDK versions even though
    v2 renames the Python attribute to ``input_schema``. A migration that leaks
    the Python name onto the wire would break every MCP client, and this is the
    assertion that catches it.
    """
    tools = await mcp.list_tools()
    by_name = {t["name"]: t for t in tools}

    for expected in ("get_healthz", "get_rooms", "post_rooms", "put_rooms_room_id"):
        assert expected in by_name, f"{expected} missing from the tool surface"

    sample = by_name["post_rooms"]
    assert "inputSchema" in sample, f"tool exposed no camelCase inputSchema: {sorted(sample)}"
    assert sample["inputSchema"]["type"] == "object"
    assert "name" in sample["inputSchema"]["properties"]
    assert sample.get("description")


# --------------------------------------------------------------------------
# State mutation — the actual baseline
# --------------------------------------------------------------------------


async def test_create_room_is_visible_via_mcp_and_rest(
    mcp: RawMcpClient, target: ConformanceTarget
) -> None:
    """Create state over MCP, then prove it landed via MCP read-back AND REST."""
    name = _unique("conf-room")
    _, room = await _create_room(mcp, name)
    room_id = room["id"]
    try:
        assert room["name"] == name

        # (1) MCP read-back — the single-room tool.
        reply = await mcp.call_tool_raw("get_rooms_room_id", {"room_id": room_id})
        assert not tool_failed(reply), reply
        assert tool_result_json(reply)["name"] == name

        # (2) MCP read-back — the collection tool.
        listing = tool_result_json(await mcp.call_tool_raw("get_rooms", {}))
        rooms = listing if isinstance(listing, list) else listing.get("rooms", [])
        assert any(r["id"] == room_id for r in rooms), "room absent from get_rooms"

        # (3) REST cross-check — proves MCP wrote through to the same store the
        #     UI reads, not to some MCP-local shadow state.
        status, body = await target.rest("GET", f"/api/rooms/{room_id}")
        assert status == 200
        assert body["name"] == name
    finally:
        await _delete_room(mcp, room_id)


async def test_update_room_over_mcp_persists(mcp: RawMcpClient, target: ConformanceTarget) -> None:
    """A PUT-shaped tool with both a path param and a body must marshal correctly.

    Path-parameter substitution and body splitting live in ``ToolSpec.build_request``;
    an argument-marshalling regression during the migration shows up here first.
    """
    name = _unique("conf-update")
    _, room = await _create_room(mcp, name)
    room_id = room["id"]
    try:
        new_notes = "updated-by-conformance"
        reply = await mcp.call_tool_raw(
            "put_rooms_room_id",
            {
                "room_id": room_id,
                "name": name,
                "thermostat_entity_id": room["thermostat_entity_id"],
                "notes": new_notes,
            },
        )
        assert not tool_failed(reply), f"put_rooms_room_id failed: {reply}"

        status, body = await target.rest("GET", f"/api/rooms/{room_id}")
        assert status == 200
        assert body["notes"] == new_notes
    finally:
        await _delete_room(mcp, room_id)


async def test_delete_room_over_mcp_removes_it(
    mcp: RawMcpClient, target: ConformanceTarget
) -> None:
    """DELETE carries no body — a verb whose request shaping differs from writes."""
    _, room = await _create_room(mcp, _unique("conf-del"))
    room_id = room["id"]

    reply = await mcp.call_tool_raw("delete_rooms_room_id", {"room_id": room_id})
    assert not tool_failed(reply), f"delete failed: {reply}"

    status, _ = await target.rest("GET", f"/api/rooms/{room_id}")
    assert status == 404, f"room still readable after MCP delete (status {status})"


async def test_temperature_write_converts_exactly_once(
    mcp: RawMcpClient, target: ConformanceTarget
) -> None:
    """The #231 double-conversion guard, over MCP.

    MCP must not convert temperatures — payloads arrive in the display unit and
    the REST write boundary's ``_to_f`` converts once. If a migration ever added
    a conversion in the bridge, the stored value would be off by the conversion
    factor and this fails. Deliberately unit-agnostic so it is a real assertion
    whether the stack runs in °F or °C.
    """
    unit = await target.temperature_unit()
    display_value = 20.0 if unit == "C" else 68.0
    expected_f = round(display_value * 9 / 5 + 32, 2) if unit == "C" else display_value

    name = _unique("conf-temp")
    _, room = await _create_room(mcp, name, system_wide_temp=display_value)
    room_id = room["id"]
    try:
        assert room["system_wide_temp"] == pytest.approx(expected_f), (
            f"unit={unit} sent {display_value} → stored {room['system_wide_temp']}, "
            f"expected {expected_f} °F"
        )
        # And the same value must come back over both read paths.
        status, body = await target.rest("GET", f"/api/rooms/{room_id}")
        assert status == 200
        assert body["system_wide_temp"] == pytest.approx(expected_f)

        readback = tool_result_json(
            await mcp.call_tool_raw("get_rooms_room_id", {"room_id": room_id})
        )
        assert readback["system_wide_temp"] == pytest.approx(expected_f)
    finally:
        await _delete_room(mcp, room_id)


@pytest.mark.skipif(_CONTAINER, reason="unit is fixed by the running stack's TEMPERATURE_UNIT")
async def test_celsius_write_converts_exactly_once(
    mcp: RawMcpClient, target: ConformanceTarget
) -> None:
    """Force Celsius and re-run the conversion assertion.

    The unit-agnostic test above only exercises the °C path when the whole stack
    happens to run in °C. In-process we can flip the scheduler, so the °C branch
    of ``_to_f`` is covered on every PR rather than only in the °C CI leg.
    """
    scheduler = target.app["scheduler"]  # type: ignore[attr-defined]
    previous = scheduler.get_temperature_unit()
    scheduler._active_unit = "C"
    try:
        name = _unique("conf-celsius")
        _, room = await _create_room(mcp, name, system_wide_temp=20.0)
        room_id = room["id"]
        try:
            # 20 °C == 68 °F. A double conversion would land at 154.4 °F.
            assert room["system_wide_temp"] == pytest.approx(68.0), (
                f"20 °C stored as {room['system_wide_temp']} °F — expected 68.0 "
                "(154.4 would mean a double conversion, i.e. #231 has returned)"
            )
        finally:
            await _delete_room(mcp, room_id)
    finally:
        scheduler._active_unit = previous


async def test_vent_command_reaches_home_assistant(
    mcp: RawMcpClient, target: ConformanceTarget
) -> None:
    """An MCP call must actually command the physical HA entity.

    This is the assertion a DB-only check cannot make: it proves the call
    travelled MCP → loopback REST → HA client → Home Assistant.

    What counts as "the entity moved" differs by target, because the two Home
    Assistants differ:

    * in-process — :class:`FakeHomeAssistant` models position, so we assert the
      cover's own ``current_position`` reaches 0.
    * container — the e2e fixture's vents are *template* covers whose
      ``position_template`` is a **constant** (``{{ 75 }}`` and friends), so
      their reported position never changes no matter what we command. Asserting
      on it would pass vacuously forever. The fixture instead routes every cover
      service onto a backing ``input_boolean``, so that boolean flipping is the
      real, observable proof the service call reached HA.
    """
    if isinstance(target, InProcessTarget):
        entity_id = "cover.conformance_vent"
        await target.reset_ha_calls()

        reply = await mcp.call_tool_raw(
            "post_vents_test",
            {"entity_id": entity_id, "control_method": "set_position", "direction": "close"},
        )
        assert not tool_failed(reply), f"post_vents_test failed: {reply}"

        calls = await target.ha_service_calls()
        assert calls is not None
        assert any(c["service"] == "set_cover_position" for c in calls), (
            f"no set_cover_position service call recorded, got: {calls}"
        )

        state = await target.ha_entity_state(entity_id)
        assert state is not None
        assert state["attributes"]["current_position"] == 0, (
            f"{entity_id} did not move after an MCP command; state={state}"
        )
        return

    # -- container ---------------------------------------------------------
    entity_id = os.environ.get("PLENUM_CONFORMANCE_VENT", "cover.living_room_vent")
    effect_entity = os.environ.get(
        "PLENUM_CONFORMANCE_VENT_EFFECT", "input_boolean.dummy_downstairs"
    )
    assert isinstance(target, ContainerTarget)

    # Drive the backing boolean to a known state first, so a stale "on" from an
    # earlier test cannot make this pass without the MCP call doing anything.
    await target.ha_call_service("input_boolean", "turn_off", effect_entity)
    before = await target.ha_entity_state(effect_entity)
    assert before is not None and before["state"] == "off", (
        f"could not reset {effect_entity} before the command; state={before}"
    )

    reply = await mcp.call_tool_raw(
        "post_vents_test",
        {"entity_id": entity_id, "control_method": "set_position", "direction": "open"},
    )
    assert not tool_failed(reply), f"post_vents_test failed: {reply}"

    for _ in range(20):
        after = await target.ha_entity_state(effect_entity)
        if after is not None and after["state"] == "on":
            break
        await asyncio.sleep(0.25)
    else:
        after = await target.ha_entity_state(effect_entity)
        pytest.fail(
            f"MCP commanded {entity_id} but its backing {effect_entity} never turned on; "
            f"state={after}"
        )


# --------------------------------------------------------------------------
# Error semantics
# --------------------------------------------------------------------------


async def test_unknown_tool_is_reported_as_a_failure(mcp: RawMcpClient) -> None:
    """An unknown tool must fail — by either mechanism, but it must fail."""
    reply = await mcp.call_tool_raw("does_not_exist_at_all", {})
    assert tool_failed(reply), f"unknown tool did not fail: {reply}"


async def test_invalid_arguments_fail_without_leaking_internals(mcp: RawMcpClient) -> None:
    """A rejected call returns a safe message — never a traceback (CWE-209).

    The repo's hard rule (security alert #4) is that raw exception detail never
    reaches a caller. mcp v2 stops auto-wrapping handler exceptions into
    error-flagged results and lets them surface as JSON-RPC errors instead, so
    this assertion is exactly what keeps that change from turning into an
    information-disclosure regression.
    """
    reply = await mcp.call_tool_raw("post_rooms", {"name": ""})
    assert tool_failed(reply), f"invalid create unexpectedly succeeded: {reply}"

    blob = json.dumps(reply)
    for leak in ("Traceback", 'File "/', "backend/api/routes.py", "aiohttp.client_exceptions"):
        assert leak not in blob, f"error payload leaked internals ({leak!r}): {blob[:400]}"

    # It should still be *useful* — the handler's own safe message survives.
    text = tool_result_text(reply) or json.dumps(reply.get("error", {}))
    assert "required" in text.lower(), f"unhelpful error payload: {text[:200]}"


# --------------------------------------------------------------------------
# A real SDK client still works
# --------------------------------------------------------------------------


async def test_a_real_sdk_client_negotiates_the_modern_protocol(target: ConformanceTarget) -> None:
    """Proves the MODERN protocol era, and the real SDK client path.

    Together with ``test_handshake_negotiates_each_supported_revision`` (which
    covers the four legacy revisions over the raw wire) this is the other half of
    v2's dual-era guarantee: the same server answers both.

    The modern era is driven through the SDK client rather than the raw driver on
    purpose. 2026-07-28 is a different wire contract — an era header on the
    handshake, a ``_meta`` envelope on every request's params, and an
    ``mcp-method`` header mirroring the body — so hand-rolling it would mean
    reimplementing the SDK client inside the test suite. This is also the ONLY
    test in the file that touches SDK types, and therefore the only one the
    v1 → v2 migration had to rewrite.
    """
    from mcp import Client
    from mcp.types import LATEST_PROTOCOL_VERSION

    from .mcp_wire import V2_PROTOCOL_REVISION

    # Guard the assumption the rest of this test rests on.
    assert LATEST_PROTOCOL_VERSION == V2_PROTOCOL_REVISION, (
        f"SDK latest is {LATEST_PROTOCOL_VERSION}, expected {V2_PROTOCOL_REVISION}"
    )

    async with Client(target.mcp_url) as session:
        # The client negotiates LATEST by default, so this asserts the server
        # actually speaks the new era rather than silently downgrading — the
        # exact failure the raw-wire handshake test caught during the migration.
        assert session.protocol_version == V2_PROTOCOL_REVISION, (
            f"server negotiated {session.protocol_version}, expected "
            f"{V2_PROTOCOL_REVISION} — the modern transport is not wired up"
        )

        tools = await session.list_tools()
        assert "get_healthz" in {t.name for t in tools.tools}

        result = await session.call_tool("get_healthz", {})
        # getattr rather than an isinstance narrow: the content-block union is
        # wide, and this one test is the only place SDK types leak into the suite.
        payload = getattr(result.content[0], "text", "")
        assert json.loads(payload) == {"ok": True}


# --------------------------------------------------------------------------
# Auth + scope (Issue #373 dual-layer, re-proven over the wire)
# --------------------------------------------------------------------------
#
# Run in-process only. The container legs deliberately run REQUIRE_AUTH=false
# (matching every other compose leg — see docker-compose.test.yml), so the auth
# axis is proven here, where a full auth-enabled stack can be stood up cheaply
# and deterministically on every PR.


class _AuthStack:
    """An auth-enabled app + MCP server, plus one minted token per scope."""

    def __init__(self, mcp_url: str, http: aiohttp.ClientSession, tokens: dict[str, str]) -> None:
        self.mcp_url = mcp_url
        self.http = http
        self.tokens = tokens

    def client(self, scope: str | None) -> RawMcpClient:
        bearer = self.tokens[scope] if scope else None
        return RawMcpClient(self.http, self.mcp_url, bearer=bearer)


@pytest_asyncio.fixture
async def auth_stack() -> AsyncIterator[_AuthStack]:
    import contextlib
    import tempfile

    import uvicorn
    from aiohttp.test_utils import TestClient, TestServer

    from backend import session as _session
    from backend.main import build_app, validate_mcp_bearer
    from backend.mcp_http import MCP_PATH, build_mcp_asgi_app

    from .fake_ha import FakeHomeAssistant

    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    fake_ha = FakeHomeAssistant()
    app = build_app(fake_ha, db_path, frontend_dist=None, start_ha=False)  # type: ignore[arg-type]
    app["require_auth"] = True
    rest_client = TestClient(TestServer(app))
    await rest_client.start_server()

    # A logged-in web admin (session cookie == full access) mints the tokens,
    # exactly as a human operator would from the Settings page.
    admin = {_session.COOKIE_NAME: _session.issue_token(app["session_secret"], "admin")}
    tokens: dict[str, str] = {}
    for scope in ("read", "write", "destructive"):
        resp = await rest_client.post(
            "/api/mcp/tokens", json={"label": f"conf-{scope}", "scope": scope}, cookies=admin
        )
        assert resp.status in (200, 201), f"minting a {scope} token returned {resp.status}"
        tokens[scope] = (await resp.json())["token"]

    port = _free_port()
    scheduler = app["scheduler"]
    async with aiohttp.ClientSession() as http:
        asgi = build_mcp_asgi_app(
            app,
            http,
            f"http://127.0.0.1:{rest_client.port}",
            is_enabled=lambda: True,
            internal_token=app["internal_token"],
            require_auth=lambda: True,
            validate_bearer=lambda t: validate_mcp_bearer(scheduler, t),
        )
        config = uvicorn.Config(asgi, host="127.0.0.1", port=port, log_level="error", lifespan="on")
        uv = uvicorn.Server(config)
        uv.install_signal_handlers = lambda: None  # type: ignore[attr-defined]
        task = asyncio.create_task(uv.serve())
        try:
            while not uv.started:  # noqa: ASYNC110
                await asyncio.sleep(0.05)
            yield _AuthStack(f"http://127.0.0.1:{port}{MCP_PATH}", http, tokens)
        finally:
            uv.should_exit = True
            await task
            await rest_client.close()
            for suffix in ("", "-wal", "-shm"):
                with contextlib.suppress(OSError):
                    os.unlink(db_path + suffix)


@pytest.mark.skipif(_CONTAINER, reason="container legs run REQUIRE_AUTH=false")
@pytest.mark.parametrize("bearer", [None, "not-a-real-token"])
async def test_mcp_rejects_missing_or_bogus_bearer(
    auth_stack: _AuthStack, bearer: str | None
) -> None:
    """With auth on, the MCP port is closed to anyone without a valid token."""
    client = RawMcpClient(auth_stack.http, auth_stack.mcp_url, bearer=bearer)
    await client.initialize(PROTOCOL_REVISIONS[-1])
    assert client.init_status == 401, (
        f"expected 401 for bearer={bearer!r}, got {client.init_status}"
    )


@pytest.mark.skipif(_CONTAINER, reason="container legs run REQUIRE_AUTH=false")
async def test_read_scoped_token_can_read_but_not_write(auth_stack: _AuthStack) -> None:
    """Least privilege holds over MCP: read tokens cannot mutate.

    The check that counts is at the REST boundary (scopes.py), reached through
    dispatch_tool's X-Plenum-Scope header — so this proves the scope survives
    the whole MCP → loopback chain, not just the ASGI gate.
    """
    client = auth_stack.client("read")
    await client.initialize(PROTOCOL_REVISIONS[-1])
    assert client.init_status == 200

    allowed = await client.call_tool_raw("get_rooms", {})
    assert not tool_failed(allowed), f"read token denied a read tool: {allowed}"

    denied = await client.call_tool_raw(
        "post_rooms", {"name": _unique("nope"), "thermostat_entity_id": "climate.nope"}
    )
    assert tool_failed(denied), "read-scoped token was allowed to create a room"
    assert "403" in json.dumps(denied), f"expected a 403 at the REST boundary: {denied}"


@pytest.mark.skipif(_CONTAINER, reason="container legs run REQUIRE_AUTH=false")
async def test_write_scoped_token_can_write_but_not_mint_tokens(auth_stack: _AuthStack) -> None:
    """A write token mutates rooms but cannot escalate by minting credentials.

    ``/api/mcp/tokens`` is destructive-classified precisely so a write-scoped
    token cannot bootstrap itself a stronger one.
    """
    client = auth_stack.client("write")
    await client.initialize(PROTOCOL_REVISIONS[-1])

    created = await client.call_tool_raw(
        "post_rooms",
        {"name": _unique("conf-write"), "thermostat_entity_id": _ROOM_THERMOSTAT},
    )
    assert not tool_failed(created), f"write token could not create a room: {created}"
    room_id = tool_result_json(created)["id"]

    escalation = await client.call_tool_raw(
        "post_mcp_tokens", {"label": "escalate", "scope": "destructive"}
    )
    assert tool_failed(escalation), "write-scoped token was allowed to mint an MCP token"

    await client.call_tool_raw("delete_rooms_room_id", {"room_id": room_id})
