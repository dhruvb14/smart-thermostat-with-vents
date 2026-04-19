"""Smoke tests that verify the integration fixtures wire up correctly."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_app_boots(client) -> None:
    resp = await client.get("/api/rooms")
    assert resp.status == 200
    data = await resp.json()
    assert data == []


@pytest.mark.asyncio
async def test_fake_ha_is_wired_into_app(client, fake_ha) -> None:
    # The app should be holding our fake, not a real HAClient
    assert client.app["ha"] is fake_ha


@pytest.mark.asyncio
async def test_tick_fixture_runs(tick) -> None:
    # No rooms → tick is a no-op but should not raise
    await tick()
