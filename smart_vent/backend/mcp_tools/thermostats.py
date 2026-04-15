"""MCP tools: thermostat configuration."""

from __future__ import annotations

import json

import aiosqlite
from mcp.server import Server
from mcp.types import TextContent

from .. import db


def register(server: Server, conn: aiosqlite.Connection) -> None:

    @server.tool()
    async def list_thermostat_configs() -> list[TextContent]:
        """List all thermostat safety configurations."""
        configs = await db.get_all_thermostat_configs(conn)
        return [
            TextContent(
                type="text", text=json.dumps([c.__dict__ for c in configs], indent=2)
            )
        ]

    @server.tool()
    async def set_thermostat_config(
        thermostat_entity_id: str,
        min_setpoint: float | None = None,
        max_setpoint: float | None = None,
        deadband: float | None = None,
        max_vent_closed_min: int | None = None,
        min_open_vents: int | None = None,
        overshoot_delta: float | None = None,
        cycle_timeout_hours: float | None = None,
    ) -> list[TextContent]:
        """
        Configure safety limits for a thermostat zone.

        min_setpoint / max_setpoint: absolute setpoint bounds (°F)
        deadband: ±°F tolerance to consider a room 'at target' (0 = exact match)
        max_vent_closed_min: reopen vents after N minutes closed (0 = disabled, for bypass dampers)
        min_open_vents: always keep at least N vents open (0 = allow all closed)
        overshoot_delta: how far past target to set thermostat to drive the HVAC (default 2°F)
        cycle_timeout_hours: abort a cycle after N hours (default 3)
        """
        tc = await db.get_thermostat_config(conn, thermostat_entity_id)
        if min_setpoint is not None:
            tc.min_setpoint = min_setpoint
        if max_setpoint is not None:
            tc.max_setpoint = max_setpoint
        if deadband is not None:
            tc.deadband = deadband
        if max_vent_closed_min is not None:
            tc.max_vent_closed_min = max_vent_closed_min
        if min_open_vents is not None:
            tc.min_open_vents = min_open_vents
        if overshoot_delta is not None:
            tc.overshoot_delta = overshoot_delta
        if cycle_timeout_hours is not None:
            tc.cycle_timeout_hours = cycle_timeout_hours
        await db.upsert_thermostat_config(conn, tc)
        return [
            TextContent(
                type="text",
                text=f"Updated config for {thermostat_entity_id}: {tc.__dict__}",
            )
        ]
