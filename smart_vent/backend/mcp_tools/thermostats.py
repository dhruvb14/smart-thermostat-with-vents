"""MCP tools: thermostat configuration."""

from __future__ import annotations

import json

import aiosqlite
from mcp.server.mcpserver import MCPServer
from mcp.types import TextContent

from .. import db
from ..units import delta_to_f, to_f
from ._units import active_unit, echo_abs, echo_delta


def _validate_total_vents_count(value: int) -> str | None:
    """Bound an MCP-supplied ``total_vents_count`` (Issue #579).

    Mirrors the REST boundary's check in ``routes.py``: the airflow floor is
    ``ceil(total_vents_count * min_open_vents_fraction)``, so a zero or
    negative total would compute a floor of 0 and silently disable dead-head
    protection.
    """
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        return "total_vents_count must be a positive integer"
    return None


def _validate_min_open_vents_fraction(value: float) -> str | None:
    """Bound an MCP-supplied ``min_open_vents_fraction`` (Issue #579).

    Mirrors the REST boundary: > 0 and ≤ 1. A fraction of 0 would let the
    engine close every vent (dead-heading the air handler — Issue #210); one
    above 1 would demand more open vents than exist and wedge the cycle.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not (0 < value <= 1):
        return "min_open_vents_fraction must be > 0 and ≤ 1"
    return None


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
        total_vents_count: int | None = None,
        clear_total_vents_count: bool = False,
        min_open_vents_fraction: float | None = None,
        has_bypass_damper: bool | None = None,
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
        total_vents_count: total registers on this thermostat, smart + passive. Sets the
          denominator of the airflow floor; unset until you provide it, in which case the
          engine falls back to "keep ≥1 vent open".
        clear_total_vents_count: unset total_vents_count (back to the ≥1-vent fallback).
          The signature can't tell "omitted" from "null", so clearing needs its own flag.
        min_open_vents_fraction: share of total_vents_count that must stay open
          (> 0 and ≤ 1; default ≈ 1/3)
        has_bypass_damper: True if a bypass damper relieves duct static pressure — the
          airflow floor is then not enforced
        overshoot_delta: how far past target to set thermostat to drive the HVAC
          (delta, display unit; default 2°F)
        cycle_timeout_hours: abort a cycle after N hours (default 3)
        """
        if clear_total_vents_count and total_vents_count is not None:
            return [
                TextContent(
                    type="text",
                    text="Pass either total_vents_count or clear_total_vents_count, not both.",
                )
            ]
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
        if clear_total_vents_count:
            tc.total_vents_count = None
        elif total_vents_count is not None:
            err = _validate_total_vents_count(total_vents_count)
            if err:
                return [TextContent(type="text", text=err)]
            tc.total_vents_count = total_vents_count
        if min_open_vents_fraction is not None:
            err = _validate_min_open_vents_fraction(min_open_vents_fraction)
            if err:
                return [TextContent(type="text", text=err)]
            tc.min_open_vents_fraction = float(min_open_vents_fraction)
        if has_bypass_damper is not None:
            tc.has_bypass_damper = bool(has_bypass_damper)
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
