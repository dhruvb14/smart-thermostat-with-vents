"""MCP tools: thermostat configuration."""

from __future__ import annotations

import json

import aiosqlite
from mcp.server.mcpserver import MCPServer
from mcp.types import TextContent

from .. import db
from ..units import delta_to_f, to_f
from ._units import active_unit, echo_abs, echo_delta


def register(server: MCPServer, conn: aiosqlite.Connection) -> None:

    @server.tool()
    async def list_thermostat_configs() -> list[TextContent]:
        """List all thermostat safety configurations."""
        configs = await db.get_all_thermostat_configs(conn)
        return [TextContent(type="text", text=json.dumps([c.__dict__ for c in configs], indent=2))]

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

        Temperatures are given in the configured display unit (°C/°F) and stored
        as °F, matching the UI and the REST API.

        min_setpoint / max_setpoint: absolute setpoint bounds (display unit)
        deadband: ± tolerance (delta, display unit) to consider a room 'at target'
        max_vent_closed_min: reopen vents after N minutes closed (0 = disabled, for bypass dampers)
        min_open_vents: always keep at least N vents open (0 = allow all closed)
        overshoot_delta: how far past target to set thermostat to drive the HVAC
          (delta, display unit; default 2°F)
        cycle_timeout_hours: abort a cycle after N hours (default 3)
        """
        unit = await active_unit(conn)
        tc = await db.get_thermostat_config(conn, thermostat_entity_id)
        changed: list[str] = []
        if min_setpoint is not None:
            tc.min_setpoint = to_f(min_setpoint, unit)
            changed.append(f"min_setpoint={echo_abs(tc.min_setpoint, unit)}")
        if max_setpoint is not None:
            tc.max_setpoint = to_f(max_setpoint, unit)
            changed.append(f"max_setpoint={echo_abs(tc.max_setpoint, unit)}")
        if deadband is not None:
            tc.deadband = delta_to_f(deadband, unit)
            changed.append(f"deadband={echo_delta(tc.deadband, unit)}")
        if max_vent_closed_min is not None:
            tc.max_vent_closed_min = max_vent_closed_min
        if min_open_vents is not None:
            tc.min_open_vents = min_open_vents
        if overshoot_delta is not None:
            tc.overshoot_delta = delta_to_f(overshoot_delta, unit)
            changed.append(f"overshoot_delta={echo_delta(tc.overshoot_delta, unit)}")
        if cycle_timeout_hours is not None:
            tc.cycle_timeout_hours = cycle_timeout_hours
        await db.upsert_thermostat_config(conn, tc)
        temp_note = f" ({', '.join(changed)})" if changed else ""
        return [
            TextContent(
                type="text",
                text=f"Updated config for {thermostat_entity_id}{temp_note}: {tc.__dict__}",
            )
        ]
