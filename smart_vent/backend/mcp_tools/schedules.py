"""MCP tools: schedule management."""

from __future__ import annotations

import json
from datetime import time

import aiosqlite
from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent

from .. import db, schedule_rules
from ..models import Schedule
from ..units import delta_to_f, to_f
from ._units import active_unit, echo_abs, echo_delta


def _validate_target_temp(value: float, unit: str) -> tuple[str | None, float]:
    """Convert and bound an MCP-supplied target temperature (Issue #522).

    Mirrors the REST boundary's 40-90 °F check. Without it an MCP client can
    persist a target the UI would refuse and the safety envelope never intended.
    """
    value_f = to_f(value, unit)
    if not (40 <= value_f <= 90):
        return "target_temp must be between 40 and 90°F (or equivalent)", value_f
    return None, value_f


def _validate_deadband_override(value: float, unit: str) -> tuple[str | None, float]:
    """Convert and bound an MCP-supplied schedule deadband (Issue #517).

    Mirrors ``routes._validate_deadband_override``: a DELTA (no -32 offset)
    converted via ``delta_to_f`` and bounded to 0–10 °F. The REST boundary
    validates; the MCP boundary must too, or a client can persist a band the UI
    would have rejected. Returns ``(error_message | None, value_f)``.
    """
    value_f = delta_to_f(value, unit)
    if not (0 <= value_f <= 10):
        return "deadband_override must be between 0 and 10°F (or equivalent)", value_f
    return None, value_f


def register(server: FastMCP, conn: aiosqlite.Connection) -> None:

    @server.tool()
    async def list_schedules(room_id: str) -> list[TextContent]:
        """List all schedule blocks for a room."""
        schedules = await db.get_schedules_for_room(conn, room_id)
        data = [
            {
                "id": s.id,
                "room_id": s.room_id,
                "days_of_week": s.days_of_week,
                "start_time": s.start_time.isoformat(),
                "end_time": s.end_time.isoformat(),
                "target_temp": s.target_temp,
                "enabled": s.enabled,
                "expires_at": s.expires_at.isoformat() if s.expires_at else None,
                "deadband_override": s.deadband_override,
                # Optional label, or null = unnamed (Issue #520). `id` stays the
                # only way to address the block.
                "name": s.name,
            }
            for s in schedules
        ]
        return [TextContent(type="text", text=json.dumps(data, indent=2))]

    @server.tool()
    async def create_schedule(
        room_id: str,
        days_of_week: list[int],
        start_time: str,
        end_time: str,
        target_temp: float,
        deadband_override: float | None = None,
        enabled: bool = True,
        expires_at: str | None = None,
        name: str | None = None,
    ) -> list[TextContent]:
        """
        Create a schedule block for a room.
        days_of_week: list of ints 0–6 (0=Monday, 6=Sunday)
        start_time / end_time: 'HH:MM' format
        target_temp: target temperature in the configured display unit (°C/°F),
          stored as °F (same convention as the UI and the REST API).
        enabled: pass false to create the block PARKED — stored but inert. A
          parked block does not reserve its time slot, so a room can hold two
          blocks for the same window and flip between them.
        expires_at: optional local wall-clock ISO timestamp ('2026-08-01T09:00').
          The block disables itself then; it is never deleted. Omit for never.
        deadband_override: optional tolerance band for THIS block, as a DELTA in
          the configured display unit (0–10°F or equivalent). While the block is
          running, the room may drift this far from target before calling for
          heating or cooling — a wider band saves runtime in a room nobody is
          using. Omit or pass null to inherit the room's deadband override, then
          the thermostat's deadband.
        name: optional display name for the block ('Weekday night setback'), at
          most 64 characters. A label, not an identifier — it is not unique, and
          the schedule's id remains the only way to address it. Omit for an
          unnamed block, which is displayed by its id.
        """
        unit = await active_unit(conn)
        try:
            clean_name = schedule_rules.normalize_name(name)
        except (TypeError, ValueError) as exc:
            return [TextContent(type="text", text=str(exc))]
        err, target_f = _validate_target_temp(target_temp, unit)
        if err:
            return [TextContent(type="text", text=err)]
        deadband_f: float | None = None
        if deadband_override is not None:
            err, deadband_f = _validate_deadband_override(deadband_override, unit)
            if err:
                return [TextContent(type="text", text=err)]
        try:
            expires = schedule_rules.parse_expires_at(expires_at)
        except (ValueError, TypeError):
            return [
                TextContent(
                    type="text",
                    text="expires_at must be a local ISO timestamp like "
                    "'2026-08-01T09:00', or null",
                )
            ]
        s = Schedule.create(
            room_id=room_id,
            days_of_week=days_of_week,
            start_time=time.fromisoformat(start_time),
            end_time=time.fromisoformat(end_time),
            target_temp=target_f,
            enabled=enabled,
            expires_at=expires,
            deadband_override=deadband_f,
            name=clean_name,
        )
        if schedule_rules.expiry_in_past(s):
            return [
                TextContent(
                    type="text",
                    text="expires_at is already past — the next sweep would disable this "
                    "block immediately. Pick a future time or create it with enabled=false.",
                )
            ]
        conflict = schedule_rules.find_conflict(s, await db.get_schedules_for_room(conn, room_id))
        if conflict is not None:
            return [
                TextContent(
                    type="text",
                    text=f"Overlaps with existing block on "
                    f"{schedule_rules.describe_block(conflict)}. Park one of them "
                    f"(enabled=false) or change the window.",
                )
            ]
        await db.upsert_schedule(conn, s)
        band = "" if deadband_f is None else f", drift ±{echo_delta(deadband_f, unit)}"
        state = "" if enabled else " [parked]"
        expiry = "" if expires is None else f", expires {expires.isoformat(timespec='minutes')}"
        # Echo the name next to the id rather than instead of it — the id is
        # what a follow-up update/delete call needs (Issue #520).
        label = "" if clean_name is None else f' "{clean_name}"'
        return [
            TextContent(
                type="text",
                text=f"Created schedule {s.id}{label} for room {room_id}: "
                f"days={days_of_week} {start_time}–{end_time} @ {echo_abs(target_f, unit)}"
                f"{band}{expiry}{state}",
            )
        ]

    @server.tool()
    async def update_schedule(
        schedule_id: str,
        room_id: str,
        days_of_week: list[int] | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        target_temp: float | None = None,
        deadband_override: float | None = None,
        clear_deadband_override: bool = False,
        enabled: bool | None = None,
        expires_at: str | None = None,
        clear_expires_at: bool = False,
        name: str | None = None,
        clear_name: bool = False,
    ) -> list[TextContent]:
        """Update an existing schedule block. room_id is required to locate the schedule.

        Every field is optional; omitting one leaves it as it is.

        target_temp is given in the configured display unit (°C/°F), stored as °F.

        deadband_override sets this block's drift tolerance, as a DELTA in the
        configured display unit (0–10°F or equivalent). It REPLACES the room's
        deadband while the block runs — it is not added to it.

        enabled=false PARKS the block: stored but inert, and it stops reserving
        its time slot, so another block can cover the same window. enabled=true
        re-arms it, and is refused if that would overlap a live block.

        expires_at is a local wall-clock ISO timestamp ('2026-08-01T09:00') at
        which the block disables itself. Because omitted and null are
        indistinguishable in this signature, use clear_expires_at=true to make
        the block never expire, clear_deadband_override=true to drop the band
        back to inheriting the room's, then the thermostat's, and
        clear_name=true to return the block to unnamed (displayed by its id).

        name renames the block (at most 64 characters). It is a label, not an
        identifier — renaming never changes the id this call takes.
        """
        schedules = await db.get_schedules_for_room(conn, room_id)
        s = next((x for x in schedules if x.id == schedule_id), None)
        if not s:
            return [TextContent(type="text", text=f"Schedule {schedule_id} not found")]
        if deadband_override is not None and clear_deadband_override:
            return [
                TextContent(
                    type="text",
                    text="Pass either deadband_override or clear_deadband_override, not both",
                )
            ]
        if expires_at is not None and clear_expires_at:
            return [
                TextContent(
                    type="text",
                    text="Pass either expires_at or clear_expires_at, not both",
                )
            ]
        if name is not None and clear_name:
            return [
                TextContent(
                    type="text",
                    text="Pass either name or clear_name, not both",
                )
            ]
        if days_of_week is not None:
            s.days_of_week = days_of_week
        if start_time is not None:
            s.start_time = time.fromisoformat(start_time)
        if end_time is not None:
            s.end_time = time.fromisoformat(end_time)
        if target_temp is not None:
            err, target_f = _validate_target_temp(target_temp, await active_unit(conn))
            if err:
                return [TextContent(type="text", text=err)]
            s.target_temp = target_f
        if enabled is not None:
            s.enabled = enabled
        if clear_expires_at:
            s.expires_at = None
        elif expires_at is not None:
            try:
                s.expires_at = schedule_rules.parse_expires_at(expires_at)
            except (ValueError, TypeError):
                return [
                    TextContent(
                        type="text",
                        text="expires_at must be a local ISO timestamp like "
                        "'2026-08-01T09:00', or use clear_expires_at",
                    )
                ]
        if clear_name:
            s.name = None
        elif name is not None:
            try:
                s.name = schedule_rules.normalize_name(name)
            except (TypeError, ValueError) as exc:
                return [TextContent(type="text", text=str(exc))]
        if clear_deadband_override:
            s.deadband_override = None
        elif deadband_override is not None:
            err, deadband_f = _validate_deadband_override(
                deadband_override, await active_unit(conn)
            )
            if err:
                return [TextContent(type="text", text=err)]
            s.deadband_override = deadband_f
        # Guards run AFTER the mutations, on the block as it would be stored —
        # the same order the REST handler uses, so a request that changes the
        # window and re-arms the block is judged on its final shape rather than
        # its old one.
        if (enabled is True or expires_at is not None or clear_expires_at) and (
            schedule_rules.expiry_in_past(s)
        ):
            return [
                TextContent(
                    type="text",
                    text="expires_at is in the past — clear it or pick a later time to keep "
                    "this block enabled.",
                )
            ]
        conflict = schedule_rules.find_conflict(s, schedules, exclude_id=s.id)
        if conflict is not None:
            return [
                TextContent(
                    type="text",
                    text=f"Overlaps with existing block on "
                    f"{schedule_rules.describe_block(conflict)}. Park one of them "
                    f"(enabled=false) or change the window.",
                )
            ]
        await db.upsert_schedule(conn, s)
        return [TextContent(type="text", text=f"Updated schedule {schedule_id}")]

    @server.tool()
    async def delete_schedule(schedule_id: str) -> list[TextContent]:
        """Delete a schedule block by ID."""
        await db.delete_schedule(conn, schedule_id)
        return [TextContent(type="text", text=f"Deleted schedule {schedule_id}")]
