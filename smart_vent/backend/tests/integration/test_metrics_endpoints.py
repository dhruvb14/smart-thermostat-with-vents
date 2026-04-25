"""
Integration tests for Issue #85 Phase 1 HTTP endpoints:
  - GET/PUT /api/settings/outside-temp-entity (Phase 1b)
  - POST /api/metrics/rollup/daily          (Phase 1d manual trigger)
  - POST /api/metrics/rollup/monthly        (Phase 1e manual trigger)
"""

from __future__ import annotations

import pytest


class TestOutsideTempEntityEndpoint:
    @pytest.mark.asyncio
    async def test_get_returns_none_when_unset(self, client):
        resp = await client.get("/api/settings/outside-temp-entity")
        assert resp.status == 200
        body = await resp.json()
        assert body == {"entity_id": None, "current_value": None}

    @pytest.mark.asyncio
    async def test_put_rejects_unknown_entity(self, client):
        resp = await client.put(
            "/api/settings/outside-temp-entity",
            json={"entity_id": "sensor.nonexistent"},
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_put_rejects_non_numeric_entity(self, client, fake_ha):
        fake_ha.seed_state("sensor.weather_state", "sunny", {})
        resp = await client.put(
            "/api/settings/outside-temp-entity",
            json={"entity_id": "sensor.weather_state"},
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_put_accepts_celsius_entity_and_returns_fahrenheit(self, client, fake_ha):
        # HAClient.get_numeric_state normalises °C → °F: 20°C → 68°F.
        fake_ha.seed_state("sensor.outdoor_c", "20", {"unit_of_measurement": "°C"})
        resp = await client.put(
            "/api/settings/outside-temp-entity",
            json={"entity_id": "sensor.outdoor_c"},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["entity_id"] == "sensor.outdoor_c"
        assert body["current_value"] == pytest.approx(68.0)

        body2 = await (await client.get("/api/settings/outside-temp-entity")).json()
        assert body2["entity_id"] == "sensor.outdoor_c"
        assert body2["current_value"] == pytest.approx(68.0)

    @pytest.mark.asyncio
    async def test_put_clears_with_null(self, client, fake_ha):
        fake_ha.seed_state("sensor.outdoor_f", "55", {"unit_of_measurement": "°F"})
        await client.put(
            "/api/settings/outside-temp-entity",
            json={"entity_id": "sensor.outdoor_f"},
        )
        resp = await client.put(
            "/api/settings/outside-temp-entity",
            json={"entity_id": None},
        )
        assert resp.status == 200
        assert (await resp.json()) == {"entity_id": None, "current_value": None}

    @pytest.mark.asyncio
    async def test_put_clears_with_empty_string(self, client, fake_ha):
        fake_ha.seed_state("sensor.outdoor_f", "55", {"unit_of_measurement": "°F"})
        await client.put(
            "/api/settings/outside-temp-entity",
            json={"entity_id": "sensor.outdoor_f"},
        )
        resp = await client.put(
            "/api/settings/outside-temp-entity",
            json={"entity_id": ""},
        )
        assert resp.status == 200
        assert (await resp.json()) == {"entity_id": None, "current_value": None}

    @pytest.mark.asyncio
    async def test_put_requires_entity_id_field(self, client):
        resp = await client.put("/api/settings/outside-temp-entity", json={})
        assert resp.status == 400


class TestHaEntitiesMultiDomain:
    """Issue #85 Phase 3c — `/api/ha/entities?domain=sensor,weather` so the
    outside-temp picker can fetch both domains in one round-trip."""

    @pytest.mark.asyncio
    async def test_comma_separated_domains(self, client, fake_ha):
        fake_ha.seed_state("sensor.outdoor_temp", "65", {"unit_of_measurement": "°F"})
        fake_ha.seed_state("weather.home", "sunny", {"temperature": 68, "temperature_unit": "°F"})
        fake_ha.seed_state("climate.thermo_a", "cool", {})

        resp = await client.get("/api/ha/entities?domain=sensor,weather")
        assert resp.status == 200
        body = await resp.json()
        ids = {e["entity_id"] for e in body}
        assert "sensor.outdoor_temp" in ids
        assert "weather.home" in ids
        assert "climate.thermo_a" not in ids

    @pytest.mark.asyncio
    async def test_single_domain_unchanged(self, client, fake_ha):
        fake_ha.seed_state("sensor.outdoor_temp", "65", {"unit_of_measurement": "°F"})
        fake_ha.seed_state("weather.home", "sunny", {})
        resp = await client.get("/api/ha/entities?domain=sensor")
        body = await resp.json()
        ids = {e["entity_id"] for e in body}
        assert "sensor.outdoor_temp" in ids
        assert "weather.home" not in ids


class TestRollupTriggerEndpoints:
    @pytest.mark.asyncio
    async def test_daily_trigger_succeeds_with_no_cycles(self, client):
        resp = await client.post("/api/metrics/rollup/daily", json={"days_back": 0})
        assert resp.status == 200
        body = await resp.json()
        assert body["rows_written"] == 0
        assert body["days_back"] == 0

    @pytest.mark.asyncio
    async def test_daily_trigger_works_without_body(self, client):
        resp = await client.post("/api/metrics/rollup/daily")
        assert resp.status == 200
        body = await resp.json()
        # default days_back=1
        assert body["days_back"] == 1
        assert body["rows_written"] == 0

    @pytest.mark.asyncio
    async def test_monthly_trigger_succeeds_with_no_cycles(self, client):
        resp = await client.post("/api/metrics/rollup/monthly", json={"months_back": 0})
        assert resp.status == 200
        body = await resp.json()
        assert body["rows_written"] == 0
        assert body["months_back"] == 0
