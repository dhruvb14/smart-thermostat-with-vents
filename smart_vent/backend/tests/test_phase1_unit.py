"""
Tests for Issue #123 Phase 1 — unit detection and persistence.

Covers:
- HAClient.get_temperature_unit()
- Scheduler temperature-unit getter/ack methods
- Scheduler._startup_resolve_unit() and _check_unit_change()
- GET /api/settings
- POST /api/settings/ack-unit-change
- POST /api/restart
- TEMPERATURE_UNIT env-var override in scheduler.start()
"""

from __future__ import annotations

import asyncio
import os
import signal
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from backend.ha_client import HAClient
from backend.main import build_app
from backend.scheduler import Scheduler

from .integration.fake_ha import FakeHomeAssistant


# ---------------------------------------------------------------------------
# HAClient.get_temperature_unit()
# ---------------------------------------------------------------------------


class TestGetTemperatureUnit:
    async def test_imperial_returns_F(self):
        client = HAClient("ws://ha.local", "tok")

        mock_resp = AsyncMock()
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(return_value={"unit_system": "imperial"})

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        client._session = mock_session

        result = await client.get_temperature_unit()
        assert result == "F"

    async def test_metric_returns_C(self):
        client = HAClient("ws://ha.local", "tok")

        mock_resp = AsyncMock()
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(return_value={"unit_system": "metric"})

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        client._session = mock_session

        result = await client.get_temperature_unit()
        assert result == "C"

    async def test_unknown_system_defaults_to_F(self):
        client = HAClient("ws://ha.local", "tok")

        mock_resp = AsyncMock()
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(return_value={"unit_system": "custom_system"})

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        client._session = mock_session

        result = await client.get_temperature_unit()
        assert result == "F"

    async def test_wss_url_converted_to_https(self):
        client = HAClient("wss://ha.example.com", "tok")

        captured_urls = []

        mock_resp = AsyncMock()
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(return_value={"unit_system": "imperial"})

        def capture_get(url, **kwargs):
            captured_urls.append(url)
            return mock_resp

        mock_session = MagicMock()
        mock_session.get = MagicMock(side_effect=capture_get)
        client._session = mock_session

        await client.get_temperature_unit()
        assert captured_urls[0].startswith("https://")


# ---------------------------------------------------------------------------
# Scheduler — unit getter / ack methods
# ---------------------------------------------------------------------------


@pytest.fixture
async def running_scheduler(tmp_path):
    """A fully-started Scheduler backed by a temp DB with a FakeHomeAssistant."""
    fake_ha = FakeHomeAssistant()
    db_path = str(tmp_path / "test.db")
    sched = Scheduler(ha=fake_ha, db_path=db_path)
    await sched.start()
    yield sched
    await sched.stop()


class TestSchedulerUnitMethods:
    async def test_get_temperature_unit_returns_F_by_default(self, running_scheduler):
        assert running_scheduler.get_temperature_unit() == "F"

    async def test_get_unit_change_ack_required_false_by_default(self, running_scheduler):
        result = await running_scheduler.get_unit_change_ack_required()
        assert result is False

    async def test_ack_unit_change_clears_flag(self, running_scheduler):
        sched = running_scheduler
        # Manually set the flag then clear it via ack
        from backend import db
        await db.set_system_setting(sched._db_conn, "unit_change_ack_required", "1")
        assert await sched.get_unit_change_ack_required() is True
        await sched.ack_unit_change()
        assert await sched.get_unit_change_ack_required() is False

    async def test_env_var_override_sets_C(self, tmp_path):
        fake_ha = FakeHomeAssistant()
        db_path = str(tmp_path / "test_c.db")
        sched = Scheduler(ha=fake_ha, db_path=db_path)
        with patch.dict(os.environ, {"TEMPERATURE_UNIT": "C"}):
            await sched.start()
        try:
            assert sched.get_temperature_unit() == "C"
            assert sched._unit_override == "C"
        finally:
            await sched.stop()

    async def test_env_var_override_F(self, tmp_path):
        fake_ha = FakeHomeAssistant()
        db_path = str(tmp_path / "test_f.db")
        sched = Scheduler(ha=fake_ha, db_path=db_path)
        with patch.dict(os.environ, {"TEMPERATURE_UNIT": "F"}):
            await sched.start()
        try:
            assert sched.get_temperature_unit() == "F"
        finally:
            await sched.stop()


# ---------------------------------------------------------------------------
# Scheduler._startup_resolve_unit()
# ---------------------------------------------------------------------------


class TestStartupResolveUnit:
    async def test_resolves_and_persists_unit_when_ha_connected(self, tmp_path):
        fake_ha = FakeHomeAssistant()
        db_path = str(tmp_path / "test.db")
        sched = Scheduler(ha=fake_ha, db_path=db_path)
        await sched.start()
        try:
            # Wait for the background resolve task to complete
            await asyncio.sleep(0.05)
            assert sched.get_temperature_unit() == "F"
        finally:
            await sched.stop()

    async def test_does_not_override_env_var_override(self, tmp_path):
        fake_ha = FakeHomeAssistant()
        db_path = str(tmp_path / "test.db")
        sched = Scheduler(ha=fake_ha, db_path=db_path)
        with patch.dict(os.environ, {"TEMPERATURE_UNIT": "C"}):
            await sched.start()
        try:
            await asyncio.sleep(0.05)
            assert sched.get_temperature_unit() == "C"
        finally:
            await sched.stop()

    async def test_ha_failure_does_not_crash(self, tmp_path):
        fake_ha = FakeHomeAssistant()
        fake_ha.get_temperature_unit = AsyncMock(side_effect=RuntimeError("HA down"))
        db_path = str(tmp_path / "test.db")
        sched = Scheduler(ha=fake_ha, db_path=db_path)
        await sched.start()
        try:
            await asyncio.sleep(0.05)
            # Should not raise; unit stays at default
            assert sched.get_temperature_unit() in ("F", "C")
        finally:
            await sched.stop()


# ---------------------------------------------------------------------------
# Scheduler._check_unit_change()
# ---------------------------------------------------------------------------


class TestCheckUnitChange:
    async def test_change_detected_sets_ack_flag(self, tmp_path):
        fake_ha = FakeHomeAssistant()
        db_path = str(tmp_path / "test.db")
        sched = Scheduler(ha=fake_ha, db_path=db_path)
        await sched.start()
        try:
            await asyncio.sleep(0.05)  # let startup resolve ("F") complete
            # Now simulate HA changing to metric
            fake_ha.get_temperature_unit = AsyncMock(return_value="C")
            await sched._check_unit_change()
            assert await sched.get_unit_change_ack_required() is True
        finally:
            await sched.stop()

    async def test_no_change_does_not_set_flag(self, tmp_path):
        fake_ha = FakeHomeAssistant()
        fake_ha.get_temperature_unit = AsyncMock(return_value="F")
        db_path = str(tmp_path / "test.db")
        sched = Scheduler(ha=fake_ha, db_path=db_path)
        await sched.start()
        try:
            await asyncio.sleep(0.05)
            await sched._check_unit_change()
            assert await sched.get_unit_change_ack_required() is False
        finally:
            await sched.stop()

    async def test_env_override_skips_check(self, tmp_path):
        fake_ha = FakeHomeAssistant()
        call_count = 0

        async def counting_get_unit():
            nonlocal call_count
            call_count += 1
            return "C"

        fake_ha.get_temperature_unit = counting_get_unit
        db_path = str(tmp_path / "test.db")
        sched = Scheduler(ha=fake_ha, db_path=db_path)
        with patch.dict(os.environ, {"TEMPERATURE_UNIT": "F"}):
            await sched.start()
        try:
            call_count = 0  # reset after startup
            await sched._check_unit_change()
            assert call_count == 0  # override locks — HA not queried
        finally:
            await sched.stop()

    async def test_ha_failure_during_check_is_swallowed(self, tmp_path):
        fake_ha = FakeHomeAssistant()
        fake_ha.get_temperature_unit = AsyncMock(side_effect=RuntimeError("conn lost"))
        db_path = str(tmp_path / "test.db")
        sched = Scheduler(ha=fake_ha, db_path=db_path)
        await sched.start()
        try:
            await sched._check_unit_change()  # must not raise
        finally:
            await sched.stop()


# ---------------------------------------------------------------------------
# Routes — GET /api/settings, POST /api/settings/ack-unit-change, POST /api/restart
# ---------------------------------------------------------------------------


@pytest.fixture
async def phase1_client(tmp_path):
    fake_ha = FakeHomeAssistant()
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        app = build_app(fake_ha, db_path, frontend_dist=None, start_ha=False)
        server = TestServer(app)
        async with TestClient(server) as c:
            await c.start_server()
            yield c
    finally:
        for suffix in ("", "-wal", "-shm"):
            p = db_path + suffix
            if os.path.exists(p):
                try:
                    os.unlink(p)
                except OSError:
                    pass


class TestSettingsEndpoints:
    async def test_get_settings_returns_temperature_unit(self, phase1_client):
        resp = await phase1_client.get("/api/settings")
        assert resp.status == 200
        data = await resp.json()
        assert "temperature_unit" in data
        assert data["temperature_unit"] in ("F", "C")
        assert "unit_change_ack_required" in data
        assert isinstance(data["unit_change_ack_required"], bool)

    async def test_get_settings_ack_required_false_initially(self, phase1_client):
        resp = await phase1_client.get("/api/settings")
        data = await resp.json()
        assert data["unit_change_ack_required"] is False

    async def test_ack_unit_change_clears_flag(self, phase1_client):
        # Manually set the flag via the scheduler
        scheduler = phase1_client.app["scheduler"]
        from backend import db
        await db.set_system_setting(scheduler._db_conn, "unit_change_ack_required", "1")

        resp = await phase1_client.post("/api/settings/ack-unit-change")
        assert resp.status == 200
        data = await resp.json()
        assert data["unit_change_ack_required"] is False

        # Confirm DB was cleared
        resp2 = await phase1_client.get("/api/settings")
        data2 = await resp2.json()
        assert data2["unit_change_ack_required"] is False

    async def test_restart_returns_restarting(self, phase1_client):
        with patch("backend.api.routes.os.kill") as mock_kill:
            resp = await phase1_client.post("/api/restart")
            assert resp.status == 200
            data = await resp.json()
            assert data["restarting"] is True
            # Give the delayed task a moment to schedule
            await asyncio.sleep(0.5)
            mock_kill.assert_called_once_with(os.getpid(), signal.SIGTERM)
