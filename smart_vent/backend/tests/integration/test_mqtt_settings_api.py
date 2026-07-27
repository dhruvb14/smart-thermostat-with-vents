"""The MQTT toggle + status endpoints behind the Settings panel (Issue #519)."""

from __future__ import annotations

from typing import Any

import pytest

from backend.mqtt.config import MqttConfig


def _config(**overrides) -> MqttConfig:
    base: dict[str, Any] = {
        "host": "broker.local",
        "port": 1883,
        "username": None,
        "password": "hunter2",
        "prefix": "plenum_beta",
        "discovery": True,
        "discovery_prefix": "homeassistant",
    }
    base.update(overrides)
    return MqttConfig(**base)


class TestToggle:
    @pytest.mark.asyncio
    async def test_defaults_off(self, client) -> None:
        """Opt-in, like the MCP server: the bridge exposes a write surface."""
        status = await (await client.get("/api/system/status")).json()
        assert status["mqtt_enabled"] is False

    @pytest.mark.asyncio
    async def test_round_trip(self, client) -> None:
        resp = await client.post("/api/system/mqtt", json={"mqtt_enabled": True})
        assert resp.status == 200
        assert (await resp.json())["mqtt_enabled"] is True

        status = await (await client.get("/api/system/status")).json()
        assert status["mqtt_enabled"] is True

        await client.post("/api/system/mqtt", json={"mqtt_enabled": False})
        status = await (await client.get("/api/system/status")).json()
        assert status["mqtt_enabled"] is False

    @pytest.mark.asyncio
    async def test_missing_field_is_rejected(self, client) -> None:
        resp = await client.post("/api/system/mqtt", json={})
        assert resp.status == 400
        assert "mqtt_enabled" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_toggle_is_persisted(self, client) -> None:
        from backend import db as _db

        await client.post("/api/system/mqtt", json={"mqtt_enabled": True})
        value = await _db.get_system_setting(client.app["scheduler"]._db_conn, "mqtt_enabled", "0")
        assert value == "1"

    @pytest.mark.asyncio
    async def test_toggle_is_logged(self, client) -> None:
        await client.post("/api/system/mqtt", json={"mqtt_enabled": True})
        events = await (await client.get("/api/logs/events")).json()
        assert any("MQTT bridge enabled" in e["message"] for e in events)


class TestStatus:
    @pytest.mark.asyncio
    async def test_reports_nothing_configured_by_default(self, client) -> None:
        body = await (await client.get("/api/settings/mqtt")).json()
        assert body["configured"] is False
        assert body["connected"] is False
        assert body["host"] is None

    @pytest.mark.asyncio
    async def test_reports_the_resolved_configuration(self, client) -> None:
        """The resolved topic prefix is otherwise invisible — it is derived from
        the add-on slug, so the panel is the only place a user can see it."""
        client.app["mqtt"]["config"] = _config()
        body = await (await client.get("/api/settings/mqtt")).json()
        assert body["configured"] is True
        assert body["host"] == "broker.local"
        assert body["port"] == 1883
        assert body["topic_prefix"] == "plenum_beta"
        assert body["discovery"] is True
        assert body["discovery_prefix"] == "homeassistant"

    @pytest.mark.asyncio
    async def test_never_returns_the_broker_password(self, client) -> None:
        """Credentials are configured out-of-band; the status endpoint must not
        become a way to read them back out."""
        client.app["mqtt"]["config"] = _config()
        text = await (await client.get("/api/settings/mqtt")).text()
        assert "hunter2" not in text

    @pytest.mark.asyncio
    async def test_surfaces_the_colliding_prefix_warning(self, client) -> None:
        client.app["mqtt"]["config"] = _config(prefix="plenum", prefix_is_fallback=True)
        body = await (await client.get("/api/settings/mqtt")).json()
        assert body["prefix_is_fallback"] is True

    @pytest.mark.asyncio
    async def test_reports_live_connection_state_and_last_error(self, client) -> None:
        class _Bridge:
            connected = True
            last_error = "OSError: broker unreachable"

        client.app["mqtt"]["config"] = _config()
        client.app["mqtt"]["bridge"] = _Bridge()
        body = await (await client.get("/api/settings/mqtt")).json()
        assert body["connected"] is True
        assert body["last_error"] == "OSError: broker unreachable"
