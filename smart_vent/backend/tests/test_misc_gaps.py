"""Coverage for the last uncovered corners of the small backend modules.

Each test here targets a specific defensive branch or process-entry line that no
other suite reaches: the certifi-less SSL fallback and the unit-probe rescue in
``ha_client``, the ``__main__`` bootstrap and the MQTT half of ``main()``'s
shutdown, the ``description`` field of an OpenAPI operation, ``parse_expires_at``
rejections, and the issuer-less IdP metadata guard.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import runpy
import ssl
import sys
import types
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

from backend import ha_client as ha_client_mod
from backend import oidc, schedule_rules
from backend.api import openapi
from backend.ha_client import HAClient

from .integration.fake_ha import FakeHomeAssistant

# --------------------------------------------------------------------------- #
# ha_client — module-level certifi fallback
# --------------------------------------------------------------------------- #


def _load_ha_client_module(*, with_certifi: bool):
    """Execute ``backend/ha_client.py`` again under a private module name.

    A private name (rather than ``importlib.reload``) keeps the real
    ``backend.ha_client`` — and every ``HAClient`` identity bound to it across
    the suite — untouched while still executing the module's own source lines.
    """
    name = "backend._ha_client_probe"
    spec = importlib.util.spec_from_file_location(name, ha_client_mod.__file__)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    saved = sys.modules.get("certifi", ...)
    if with_certifi:
        sys.modules.pop("certifi", None)
    else:
        # A None entry makes ``import certifi`` raise ImportError without
        # touching the real package.
        sys.modules["certifi"] = None  # type: ignore[assignment]
    try:
        spec.loader.exec_module(module)
    finally:
        if saved is ...:
            sys.modules.pop("certifi", None)
        else:
            sys.modules["certifi"] = saved
    return module


def test_missing_certifi_falls_back_to_the_system_trust_store() -> None:
    """No certifi wheel in the image must not break TLS wiring — the module
    keeps importing and hands aiohttp ``None`` so it uses the system default."""
    module = _load_ha_client_module(with_certifi=False)
    assert module._SSL_CONTEXT is None


def test_certifi_present_builds_a_real_ssl_context() -> None:
    """The other half of the same try/except, so the fallback above is proven
    to be a fallback and not the only reachable outcome."""
    module = _load_ha_client_module(with_certifi=True)
    assert isinstance(module._SSL_CONTEXT, ssl.SSLContext)


# --------------------------------------------------------------------------- #
# ha_client._connect — the temperature-unit probe is best-effort
# --------------------------------------------------------------------------- #


def _connectable_client() -> tuple[HAClient, list[bool]]:
    client = HAClient("ws://ha.local", "tok")
    connected_during_read: list[bool] = []

    client._handshake = AsyncMock()  # type: ignore[method-assign]
    client._subscribe_state_changed = AsyncMock()  # type: ignore[method-assign]
    client.fetch_states = AsyncMock(return_value=[])  # type: ignore[method-assign]

    async def capturing_read_loop() -> None:
        connected_during_read.append(client._connected.is_set())

    client._read_loop = capturing_read_loop  # type: ignore[method-assign]

    mock_ws = AsyncMock()
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_ws)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)
    mock_session = MagicMock()
    mock_session.ws_connect = MagicMock(return_value=mock_ctx)
    client._session = mock_session
    return client, connected_during_read


async def test_connect_survives_a_failing_temperature_unit_probe() -> None:
    """An HA that refuses ``/api/config`` (old core, proxy, transient 500) must
    still yield a usable, subscribed connection — the probe is an optimisation,
    not a precondition. Issue #280."""
    client, connected_during_read = _connectable_client()
    client.get_temperature_unit = AsyncMock(side_effect=RuntimeError("500 from /api/config"))  # type: ignore[method-assign]

    await client._connect()  # must not raise

    client._subscribe_state_changed.assert_awaited_once()  # type: ignore[attr-defined]
    assert connected_during_read == [True]
    # The probe failed, so the cached unit stays at its safe default.
    assert client.ha_temp_unit == "F"


async def test_connect_probe_failure_is_logged_at_debug(caplog) -> None:
    client, _ = _connectable_client()
    client.get_temperature_unit = AsyncMock(side_effect=RuntimeError("boom-probe"))  # type: ignore[method-assign]
    with caplog.at_level("DEBUG", logger="backend.ha_client"):
        await client._connect()
    assert any("boom-probe" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------- #
# main() — the MQTT bridge is stopped on the way out
# --------------------------------------------------------------------------- #


async def test_main_stops_the_mqtt_bridge_on_shutdown(monkeypatch, tmp_path) -> None:
    """``main()``'s finally must tear the MQTT bridge down as well as MCP —
    otherwise its loopback ClientSession and broker task outlive the process's
    own HTTP runner. Issue #519."""
    import backend.main as main_mod

    monkeypatch.setattr(main_mod, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(main_mod, "DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setattr(main_mod, "PORT", 0)
    monkeypatch.setattr(main_mod, "build_ha_client", lambda: FakeHomeAssistant())

    mqtt_sentinel = ("bridge", "task", "session")
    start_mqtt = AsyncMock(return_value=mqtt_sentinel)
    stop_mqtt = AsyncMock()
    monkeypatch.setattr(main_mod, "_start_mqtt_bridge", start_mqtt)
    monkeypatch.setattr(main_mod, "_stop_mqtt_bridge", stop_mqtt)
    monkeypatch.setattr(main_mod, "_start_mcp_server", AsyncMock(return_value=None))

    task = asyncio.get_running_loop().create_task(main_mod.main())
    for _ in range(200):
        if start_mqtt.await_count:
            break
        await asyncio.sleep(0.05)
    assert start_mqtt.await_count == 1

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    stop_mqtt.assert_awaited_once_with(*mqtt_sentinel)


# --------------------------------------------------------------------------- #
# main.py __main__ bootstrap
# --------------------------------------------------------------------------- #


def _run_main_module_as_script() -> None:
    runpy.run_module("backend.main", run_name="__main__")


def test_entrypoint_prefers_uvloop(monkeypatch) -> None:
    """The add-on's process entry runs the app on uvloop when it is installed."""
    ran: list[str] = []

    def fake_run(coro):
        coro.close()  # never actually start the server
        ran.append("uvloop")

    fake_uvloop = types.ModuleType("uvloop")
    fake_uvloop.run = fake_run  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "uvloop", fake_uvloop)
    monkeypatch.setattr(asyncio, "run", lambda coro: pytest.fail("asyncio.run must not be used"))

    _run_main_module_as_script()
    assert ran == ["uvloop"]


def test_entrypoint_falls_back_to_asyncio_run_without_uvloop(monkeypatch) -> None:
    """uvloop is an optional speedup, not a dependency: a wheel-less arch must
    still boot on the stdlib event loop."""
    ran: list[str] = []

    def fake_run(coro):
        coro.close()
        ran.append("asyncio")

    # A None entry makes ``import uvloop`` raise ImportError.
    monkeypatch.setitem(sys.modules, "uvloop", None)
    monkeypatch.setattr(asyncio, "run", fake_run)

    _run_main_module_as_script()
    assert ran == ["asyncio"]


# --------------------------------------------------------------------------- #
# openapi — operation description
# --------------------------------------------------------------------------- #


def test_operation_description_reaches_the_spec() -> None:
    """``@docs(description=...)`` is the long-form prose Swagger UI renders under
    the summary; it must survive into the operation object, not just the
    handler's ``__apispec__``."""
    from marshmallow import Schema, fields

    class RespSchema(Schema):
        ok = fields.Bool()

    app = web.Application()

    @openapi.docs(
        tags=["rooms"],
        summary="Get a room",
        description="Returns one room, including its vents.",
    )
    @openapi.response_schema(RespSchema)
    async def get_room(_request):
        return web.Response()

    app.router.add_get("/api/rooms/{room_id}", get_room)
    spec = openapi.build_spec(app, title="Plenum API", version="v1")

    op = spec["paths"]["/api/rooms/{room_id}"]["get"]
    assert op["description"] == "Returns one room, including its vents."
    assert op["summary"] == "Get a room"


def test_operation_without_a_description_omits_the_key() -> None:
    """A handler that documents no description must not emit an empty one —
    Swagger UI renders an empty description block for ``""``."""
    op = openapi._build_operation({"summary": "Terse", "description": "", "responses": {}})
    assert "description" not in op


# --------------------------------------------------------------------------- #
# schedule_rules.parse_expires_at
# --------------------------------------------------------------------------- #


class TestParseExpiresAt:
    @pytest.mark.parametrize("raw", [None, ""])
    def test_absent_means_never_expires(self, raw: object) -> None:
        assert schedule_rules.parse_expires_at(raw) is None

    def test_naive_local_string_is_kept_naive(self) -> None:
        """What ``<input type="datetime-local">`` sends. Keeping it naive-local
        is load-bearing: it shares a frame with start_time/end_time."""
        assert schedule_rules.parse_expires_at("2025-06-04T22:30") == datetime(2025, 6, 4, 22, 30)

    def test_aware_string_is_converted_to_local_naive(self) -> None:
        parsed = schedule_rules.parse_expires_at("2025-06-04T22:30:00+00:00")
        assert parsed is not None
        assert parsed.tzinfo is None

    @pytest.mark.parametrize("raw", [0, 1, 1.5, True, [], {}, ["2025-06-04T22:30"]])
    def test_non_string_is_rejected_as_a_type_error(self, raw: object) -> None:
        """A JSON number or list must not be coerced — silently accepting one
        would store a nonsense expiry that the sweep then acts on."""
        with pytest.raises(TypeError, match="expires_at must be a string or null"):
            schedule_rules.parse_expires_at(raw)

    def test_unparseable_string_is_rejected_as_a_value_error(self) -> None:
        with pytest.raises(ValueError):
            schedule_rules.parse_expires_at("next tuesday")


# --------------------------------------------------------------------------- #
# oidc — metadata without an issuer
# --------------------------------------------------------------------------- #


async def test_validate_id_token_rejects_metadata_without_an_issuer() -> None:
    """Without an issuer there is nothing to bind the token to, so validation
    must refuse rather than fall through to an unchecked ``iss``."""
    provider = oidc.OIDCProvider(
        oidc.OIDCConfig(
            configuration_url="https://idp.example/.well-known/openid-configuration",
            client_id="cid",
            client_secret="sec",
            external_url="https://plenum.example",
        )
    )
    provider._metadata = {"jwks_uri": "https://idp.example/jwks"}  # no issuer
    with pytest.raises(oidc.OIDCError, match="no issuer"):
        await provider.validate_id_token("not.even.reached", "N")
