"""
aiohttp REST API routes.

All handlers are thin: validate input, call db helpers, return JSON.
The scheduler instance is attached to app['scheduler'].
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import sqlite3
import tempfile
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import aiohttp
from aiohttp import web

from .. import db, tz
from ..engine import room_manager
from ..models import (
    VALID_CONTROL_METHODS,
    Room,
    RoomOverride,
    RoomPresenceSensor,
    RoomSensor,
    RoomVent,
    Schedule,
)
from ..units import delta_to_f as _delta_to_f
from ..units import from_f as _from_f
from ..units import from_f_delta as _from_f_delta
from ..units import to_f as _to_f
from . import schemas
from .openapi import docs, query_params, request_schema, response_schema

log = logging.getLogger(__name__)

routes = web.RouteTableDef()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def json_response(data: Any, status: int = 200) -> web.Response:
    return web.Response(
        text=json.dumps(data, default=str),
        content_type="application/json",
        status=status,
    )


def error(msg: str, status: int = 400) -> web.Response:
    return json_response({"error": msg}, status=status)


async def get_conn(request: web.Request):
    return await request.app["scheduler"].get_db()


async def refresh(request: web.Request) -> None:
    await request.app["scheduler"].refresh_engines()


async def emit(
    request: web.Request,
    level: str,
    category: str,
    message: str,
    details: dict | None = None,
) -> None:
    """Emit a log event via the EventLogger if available."""
    logger = request.app.get("event_logger")
    if logger:
        await logger.log(level, category, message, details)


def _temp_range_error(field: str, low_f: float, high_f: float, unit: str) -> web.Response:
    """Generate a unit-aware temperature range error response."""
    low = _from_f(low_f, unit)
    high = _from_f(high_f, unit)
    return error(f"{field} must be between {low} and {high}°{unit}")


def _validate_deadband_override(val, unit: str) -> tuple[web.Response | None, float | None]:
    """Validate a present per-room ``deadband_override`` value (Issue #277).

    ``val`` is the raw body value, already known to be present in the request.
    ``None`` clears the override so the room inherits the thermostat's deadband.
    A number is a delta (no -32 offset) converted to °F via ``_delta_to_f`` and
    bounded to a sane 0–10 °F band.

    Returns ``(error_response | None, value_f | None)``. On the error path the
    second element is meaningless; callers must check the response first.
    """
    if val is None:
        return None, None
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return error("deadband_override must be a number or null"), None
    val_f = _delta_to_f(val, unit)
    if not (0 <= val_f <= 10):
        return error("deadband_override must be between 0 and 10°F (or equivalent)"), None
    return None, val_f


# Eco Mode numeric fields (Issue #404): kind (absolute vs delta) + inclusive
# °F bounds applied AFTER unit conversion. The four outdoor threshold /
# full-drift values are absolutes and share a deliberately wide outdoor band
# (heating full-drift defaults to 0 °F / −18 °C; cooling full-drift to 100 °F).
# The two max-drift values and the hysteresis band are deltas. The effective
# target is also hard-clamped to the thermostat's setpoint envelope in the
# engine, so these bounds only reject nonsensical config, not unsafe drift.
_ECO_ABS_LO, _ECO_ABS_HI = -40.0, 130.0
_ECO_FIELD_SPECS: dict[str, tuple[str, float, float]] = {
    "eco_cooling_outdoor_threshold": ("absolute", _ECO_ABS_LO, _ECO_ABS_HI),
    "eco_cooling_full_drift_temp": ("absolute", _ECO_ABS_LO, _ECO_ABS_HI),
    "eco_cooling_max_drift": ("delta", 0.0, 20.0),
    "eco_heating_outdoor_threshold": ("absolute", _ECO_ABS_LO, _ECO_ABS_HI),
    "eco_heating_full_drift_temp": ("absolute", _ECO_ABS_LO, _ECO_ABS_HI),
    "eco_heating_max_drift": ("delta", 0.0, 20.0),
    "eco_hysteresis_band": ("delta", 0.0, 10.0),
}


def _validate_eco_temp(
    field: str, val, unit: str, allow_none: bool
) -> tuple[web.Response | None, float | None]:
    """Validate + convert one Eco Mode numeric field (Issue #404).

    ``val`` is the raw body value, known to be present. On the room write path
    ``allow_none=True`` lets ``None`` clear the override (inherit the
    thermostat); on the thermostat path ``allow_none=False`` rejects ``None``
    since the global fields are non-null. Absolute fields convert via ``_to_f``,
    deltas via ``_delta_to_f``; the °F result is bounded per ``_ECO_FIELD_SPECS``.

    Returns ``(error_response | None, value_f | None)``; check the response
    first — the value is meaningless on the error path.
    """
    if val is None:
        if allow_none:
            return None, None
        return error(f"{field} must be a number"), None
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return error(f"{field} must be a number{' or null' if allow_none else ''}"), None
    kind, lo_f, hi_f = _ECO_FIELD_SPECS[field]
    val_f = _to_f(val, unit) if kind == "absolute" else _delta_to_f(val, unit)
    if not (lo_f <= val_f <= hi_f):
        return error(f"{field} must be between {lo_f:g} and {hi_f:g}°F (or equivalent)"), None
    return None, val_f


def _validate_room_eco(body: dict, unit: str) -> tuple[web.Response | None, dict]:
    """Validate + convert the per-room Eco Mode override fields in *body*.

    Only keys present in *body* are touched, so the same helper serves create
    and update. Every field is nullable — ``None`` clears the override so the
    room inherits the thermostat value (field-level null-inheritance).
    ``eco_mode_enabled`` is a tri-state boolean (None/True/False). Numeric fields
    convert to °F and are bounded like their thermostat counterparts.

    Returns ``(error_response | None, updates)`` where ``updates`` maps each
    present field to its stored value (°F, or ``None``/bool). Check the response
    first.
    """
    updates: dict = {}
    if "eco_mode_enabled" in body:
        val = body["eco_mode_enabled"]
        updates["eco_mode_enabled"] = None if val is None else bool(val)
    for field in _ECO_FIELD_SPECS:
        if field in body:
            err, val_f = _validate_eco_temp(field, body[field], unit, allow_none=True)
            if err is not None:
                return err, {}
            updates[field] = val_f
    return None, updates


async def _eco_enable_blocked(conn, enabling: bool) -> web.Response | None:
    """Reject enabling Eco Mode without an outside-temperature sensor (Issue
    #404). Eco Mode is entirely outdoor-temperature-driven, so it must not be
    turned on until the house-wide outside-temp entity is configured. Returns an
    error response when *enabling* is true and no sensor is set, else ``None``.
    """
    if not enabling:
        return None
    entity_id = await db.get_system_setting(conn, "outside_temperature_entity_id", "")
    if not entity_id:
        return error(
            "Eco Mode needs an outside-temperature sensor. Configure the "
            "outside-temperature entity on the Thermostats page first — if you "
            "have no physical outdoor thermometer in Home Assistant, add a free "
            "weather integration such as PirateWeather and point it at that."
        )
    return None


# Safety-critical thermostat-config numeric fields (Issue #295). Each maps to a
# (minimum, inclusive) lower bound; the engine does arithmetic on these every
# tick (timedeltas, deadband comparisons), so a non-numeric or negative value
# stored verbatim raises TypeError / inverts comparisons and can silently kill
# the zone. ``cycle_timeout_hours`` must be strictly positive — a zero hard
# timeout would instantly time out every cycle. Bounds mirror the Thermostats
# page input ``min`` attributes.
_THERMO_NUMERIC_BOUNDS: dict[str, tuple[float, bool]] = {
    "deadband": (0.0, True),  # >= 0  (negative inverts the at-target comparison)
    "overshoot_delta": (0.0, True),
    "max_vent_closed_min": (0.0, True),
    "cycle_timeout_hours": (0.0, False),  # > 0
    "reconciliation_interval_min": (0.0, True),
    "min_cycle_runtime_min": (0.0, True),
    "min_cycle_offtime_min": (0.0, True),
}


def _validate_thermostat_numeric(field: str, val: object) -> web.Response | None:
    """Reject a non-numeric or out-of-range thermostat-config numeric field.

    Returns an error response, or ``None`` when the value is acceptable.
    """
    lo, inclusive = _THERMO_NUMERIC_BOUNDS[field]
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return error(f"{field} must be a number")
    if inclusive and val < lo:
        return error(f"{field} must be >= {lo:g}")
    if not inclusive and val <= lo:
        return error(f"{field} must be > {lo:g}")
    return None


def _validate_ambient_suppression(
    body: dict, unit: str, tc_deadband_f: float, feature_enabled: bool
) -> tuple[web.Response | None, dict]:
    """Validate the ambient-suppression (pre-cool/pre-heat) fields in *body*.

    Only keys actually present in *body* are validated, so the same helper
    serves both create (full body) and update (partial body). Temperature
    fields are deltas and are converted to °F via ``_delta_to_f``.

    ``feature_enabled`` is the effective enabled state for the room after this
    write. The "widened deadband must be ≥ the thermostat deadband" rule is only
    enforced when the feature is enabled: when it is off the value is unused
    (and the engine clamps with ``max()`` regardless), so a default widened
    deadband must never block an unrelated room save on a wide-deadband
    thermostat.

    Returns ``(error_response | None, converted_updates)``. On the first
    validation failure the error response is returned and the updates dict is
    empty; otherwise the updates dict holds storage-ready (°F where relevant)
    values keyed by Room attribute name. See Issue #248.
    """
    updates: dict = {}

    if "ambient_suppression_enabled" in body:
        val = body["ambient_suppression_enabled"]
        if not isinstance(val, bool):
            return error("ambient_suppression_enabled must be a boolean"), {}
        updates["ambient_suppression_enabled"] = val

    if "ambient_suppression_mode" in body:
        val = body["ambient_suppression_mode"]
        if val not in ("any_presence", "off_schedule_only"):
            return (
                error("ambient_suppression_mode must be 'any_presence' or 'off_schedule_only'"),
                {},
            )
        updates["ambient_suppression_mode"] = val

    if "ambient_suppression_min_differential" in body:
        val = body["ambient_suppression_min_differential"]
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            return error("ambient_suppression_min_differential must be numeric"), {}
        val_f = _delta_to_f(val, unit)
        if val_f < 0:
            return error("ambient_suppression_min_differential must be 0 or greater"), {}
        updates["ambient_suppression_min_differential"] = val_f

    if "ambient_suppression_deadband" in body:
        val = body["ambient_suppression_deadband"]
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            return error("ambient_suppression_deadband must be numeric"), {}
        val_f = _delta_to_f(val, unit)
        if feature_enabled and val_f < tc_deadband_f:
            min_display = _from_f_delta(tc_deadband_f, unit)
            return (
                error(
                    "ambient_suppression_deadband must be at least the thermostat's "
                    f"deadband ({min_display}°{unit})"
                ),
                {},
            )
        updates["ambient_suppression_deadband"] = val_f

    if "ambient_suppression_off_schedule_window_min" in body:
        val = body["ambient_suppression_off_schedule_window_min"]
        if isinstance(val, bool) or not isinstance(val, int) or val < 0:
            return (
                error("ambient_suppression_off_schedule_window_min must be a non-negative integer"),
                {},
            )
        updates["ambient_suppression_off_schedule_window_min"] = val

    return None, updates


# ---------------------------------------------------------------------------
# Temperature field registry
#
# Single Python-side source of truth for every body key that any write
# endpoint converts via _to_f / _delta_to_f. Mirrored in
# e2e/tests/temperature-fields.ts; the two are compared by
# backend/tests/test_temperature_field_parity.py — drift fails CI.
#
# Adding a temperature field to a write boundary requires:
#   1. Adding the key + kind here.
#   2. Adding the matching entry to temperature-fields.ts (with `ui` and
#      `endpoints` metadata).
#   3. If `ui: true` in the TS manifest, adding a `// @covers: <field>`
#      tag to a round-trip test in e2e/tests/temperature-units.spec.ts.
#
# Kinds:
#   "absolute"          — _to_f, value must be present (NOT NULL in DB).
#   "absolute_nullable" — _to_f, null clears / disables the value.
#   "delta"             — _delta_to_f (no -32 offset). Treated as 0 if absent.
#   "delta_nullable"    — _delta_to_f (no -32 offset), null clears the value.
# ---------------------------------------------------------------------------

TEMPERATURE_FIELDS: dict[str, str] = {
    # Thermostat config
    "default_temp": "absolute_nullable",
    "min_setpoint": "absolute",
    "max_setpoint": "absolute",
    "deadband": "delta",
    "overshoot_delta": "delta",
    "cooling_lockout_below_f": "absolute_nullable",
    # Room
    "system_wide_temp": "absolute_nullable",
    "temp_offset": "delta",
    # Per-room deadband override (Issue #277). Nullable delta: null clears the
    # override, restoring inheritance of the thermostat's deadband.
    "deadband_override": "delta_nullable",
    # Room — ambient-aware presence suppression / pre-cool (Issue #248)
    "ambient_suppression_min_differential": "delta",
    "ambient_suppression_deadband": "delta",
    # Eco Mode (Issue #404) — on BOTH the thermostat (non-null) and room
    # (nullable, null = inherit) write boundaries under the same field name.
    # The registry is keyed by field name, so each appears once with the
    # nullable kind (accurate for the room path; the conversion — _to_f for
    # thresholds/full-drift, _delta_to_f for max-drift/hysteresis — is identical
    # for the non-null thermostat path). eco_mode_enabled is a boolean, not a
    # temperature field, so it is not registered here.
    "eco_cooling_outdoor_threshold": "absolute_nullable",
    "eco_cooling_full_drift_temp": "absolute_nullable",
    "eco_cooling_max_drift": "delta_nullable",
    "eco_heating_outdoor_threshold": "absolute_nullable",
    "eco_heating_full_drift_temp": "absolute_nullable",
    "eco_heating_max_drift": "delta_nullable",
    "eco_hysteresis_band": "delta_nullable",
    # Schedules / overrides
    "target_temp": "absolute",
}


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@docs(tags=["system"], summary="Health check")
@response_schema(schemas.SuccessSchema)
@routes.get("/api/healthz")
async def healthz(request: web.Request) -> web.Response:
    return json_response({"ok": True})


# ---------------------------------------------------------------------------
# Rooms
# ---------------------------------------------------------------------------


@docs(tags=["rooms"], summary="List all rooms")
@response_schema(schemas.RoomSchema(many=True))
@routes.get("/api/rooms")
async def list_rooms(request: web.Request) -> web.Response:
    conn = await get_conn(request)
    rooms = await db.get_all_rooms(conn)
    return json_response([r.__dict__ for r in rooms])


@docs(tags=["rooms"], summary="Create a new room")
@request_schema(schemas.RoomSchema)
@response_schema(schemas.RoomSchema, code=201)
@routes.post("/api/rooms")
async def create_room(request: web.Request) -> web.Response:
    body = await request.json()
    if not body.get("name") or not body.get("thermostat_entity_id"):
        return error("name and thermostat_entity_id required")
    unit = request.app["scheduler"].get_temperature_unit()
    sys_temp = body.get("system_wide_temp")

    # Security: input validation
    holdover = body.get("presence_holdover_hours", 2.0)
    if not isinstance(holdover, (int, float)) or not (0 <= holdover <= 8760):
        return error("presence_holdover_hours must be between 0 and 8760")

    if sys_temp is not None:
        if not isinstance(sys_temp, (int, float)):
            return error("system_wide_temp must be numeric")
        sys_temp_f = _to_f(sys_temp, unit)
        if not (40 <= sys_temp_f <= 90):
            return _temp_range_error("system_wide_temp", 40, 90, unit)

    temp_offset_in = body.get("temp_offset", 0.0)
    if not isinstance(temp_offset_in, (int, float)):
        return error("temp_offset must be numeric")

    temp_offset_f = _delta_to_f(temp_offset_in, unit)
    if not (-20 <= temp_offset_f <= 20):
        return error("temp_offset must be between -20 and 20°F (or equivalent)")

    # Per-room deadband override (Issue #277). Absent → inherit (None).
    deadband_override_f: float | None = None
    if "deadband_override" in body:
        err, deadband_override_f = _validate_deadband_override(body["deadband_override"], unit)
        if err is not None:
            return err

    conn = await get_conn(request)
    # Ambient suppression (Issue #248): widened deadband is validated against the
    # room's thermostat deadband. get_thermostat_config returns a default config
    # (deadband 0.5) when the thermostat has no row yet.
    tc = await db.get_thermostat_config(conn, body["thermostat_entity_id"])
    ambient_enabled = bool(body.get("ambient_suppression_enabled", False))
    err, ambient_updates = _validate_ambient_suppression(body, unit, tc.deadband, ambient_enabled)
    if err is not None:
        return err

    # Eco Mode per-room overrides (Issue #404). All nullable (None = inherit).
    # Forcing Eco on for a room requires an outside-temperature sensor.
    blocked = await _eco_enable_blocked(conn, body.get("eco_mode_enabled") is True)
    if blocked is not None:
        return blocked
    err, eco_updates = _validate_room_eco(body, unit)
    if err is not None:
        return err

    room = Room.create(
        name=body["name"],
        thermostat_entity_id=body["thermostat_entity_id"],
        include_thermostat_sensor=body.get("include_thermostat_sensor", False),
        system_wide_temp=_to_f(sys_temp, unit) if sys_temp is not None else None,
        presence_holdover_hours=holdover,
        notes=body.get("notes", ""),
        temp_offset=temp_offset_f,
        deadband_override=deadband_override_f,
        **ambient_updates,
        **eco_updates,
    )
    await db.upsert_room(conn, room)
    await refresh(request)
    await emit(request, "info", "api", f"Room created: {room.name}", {"room_id": room.id})
    return json_response(room.__dict__, status=201)


@docs(tags=["rooms"], summary="Get room details")
@response_schema(schemas.RoomResponseSchema)
@routes.get("/api/rooms/{room_id}")
async def get_room(request: web.Request) -> web.Response:
    conn = await get_conn(request)
    room = await db.get_room(conn, request.match_info["room_id"])
    if not room:
        return error("Room not found", 404)
    sensors = await db.get_room_sensors(conn, room.id)
    vents = await db.get_room_vents(conn, room.id)
    presence = await db.get_room_presence_sensors(conn, room.id)
    schedules = await db.get_schedules_for_room(conn, room.id)
    return json_response(
        {
            **room.__dict__,
            "sensors": [s.__dict__ for s in sensors],
            "vents": [v.__dict__ for v in vents],
            "presence_sensors": [p.__dict__ for p in presence],
            "schedules": [_schedule_to_dict(s) for s in schedules],
        }
    )


@docs(tags=["rooms"], summary="Update room details")
@request_schema(schemas.RoomSchema)
@response_schema(schemas.RoomSchema)
@routes.put("/api/rooms/{room_id}")
async def update_room(request: web.Request) -> web.Response:
    conn = await get_conn(request)
    room = await db.get_room(conn, request.match_info["room_id"])
    if not room:
        return error("Room not found", 404)
    body = await request.json()
    unit = request.app["scheduler"].get_temperature_unit()

    # Security: input validation
    if "presence_holdover_hours" in body:
        val = body["presence_holdover_hours"]
        if not isinstance(val, (int, float)) or not (0 <= val <= 8760):
            return error("presence_holdover_hours must be between 0 and 8760")
    if "system_wide_temp" in body:
        val = body["system_wide_temp"]
        if val is not None:
            if not isinstance(val, (int, float)):
                return error("system_wide_temp must be numeric")
            val_f = _to_f(val, unit)
            if not (40 <= val_f <= 90):
                return _temp_range_error("system_wide_temp", 40, 90, unit)
    if "temp_offset" in body:
        val = body["temp_offset"]
        if not isinstance(val, (int, float)):
            return error("temp_offset must be numeric")
        val_f = _delta_to_f(val, unit)
        if not (-20 <= val_f <= 20):
            return error("temp_offset must be between -20 and 20°F (or equivalent)")
    # Per-room deadband override (Issue #277). Validated and converted up front;
    # applied below only when the key is present (null clears the override).
    deadband_override_present = "deadband_override" in body
    deadband_override_f: float | None = None
    if deadband_override_present:
        err, deadband_override_f = _validate_deadband_override(body["deadband_override"], unit)
        if err is not None:
            return err
    # Ambient suppression (Issue #248): validate against the *effective* thermostat
    # (a request can switch thermostat_entity_id in the same PUT).
    effective_thermostat = body.get("thermostat_entity_id", room.thermostat_entity_id)
    tc = await db.get_thermostat_config(conn, effective_thermostat)
    ambient_enabled = bool(
        body.get("ambient_suppression_enabled", room.ambient_suppression_enabled)
    )
    err, ambient_updates = _validate_ambient_suppression(body, unit, tc.deadband, ambient_enabled)
    if err is not None:
        return err
    # Eco Mode per-room overrides (Issue #404). All nullable (None = inherit).
    # Forcing Eco on for a room requires an outside-temperature sensor.
    blocked = await _eco_enable_blocked(conn, body.get("eco_mode_enabled") is True)
    if blocked is not None:
        return blocked
    err, eco_updates = _validate_room_eco(body, unit)
    if err is not None:
        return err
    for field in (
        "name",
        "thermostat_entity_id",
        "include_thermostat_sensor",
        "presence_holdover_hours",
        "notes",
    ):
        if field in body:
            setattr(room, field, body[field])
    if "system_wide_temp" in body:
        val = body["system_wide_temp"]
        room.system_wide_temp = _to_f(val, unit) if val is not None else None
    if "temp_offset" in body:
        room.temp_offset = _delta_to_f(body["temp_offset"], unit)
    if deadband_override_present:
        room.deadband_override = deadband_override_f
    for attr, value in ambient_updates.items():
        setattr(room, attr, value)
    for attr, value in eco_updates.items():
        setattr(room, attr, value)
    await db.upsert_room(conn, room)
    await refresh(request)
    await emit(request, "info", "api", f"Room updated: {room.name}", {"room_id": room.id})
    return json_response(room.__dict__)


@docs(tags=["rooms"], summary="Delete a room")
@response_schema(schemas.DeletedSchema)
@routes.delete("/api/rooms/{room_id}")
async def delete_room(request: web.Request) -> web.Response:
    conn = await get_conn(request)
    room = await db.get_room(conn, request.match_info["room_id"])
    if not room:
        return error("Room not found", 404)
    await db.delete_room(conn, room.id)
    await refresh(request)
    await emit(request, "info", "api", f"Room deleted: {room.name}", {"room_id": room.id})
    return json_response({"deleted": room.id})


# ---------------------------------------------------------------------------
# Room sensors
# ---------------------------------------------------------------------------


@docs(tags=["sensors"], summary="List sensors in a room")
@response_schema(schemas.RoomSensorSchema(many=True))
@routes.get("/api/rooms/{room_id}/sensors")
async def list_sensors(request: web.Request) -> web.Response:
    conn = await get_conn(request)
    sensors = await db.get_room_sensors(conn, request.match_info["room_id"])
    return json_response([s.__dict__ for s in sensors])


@docs(tags=["sensors"], summary="Add a sensor to a room")
@request_schema(schemas.RoomSensorSchema)
@response_schema(schemas.RoomSensorSchema, code=201)
@routes.post("/api/rooms/{room_id}/sensors")
async def add_sensor(request: web.Request) -> web.Response:
    body = await request.json()
    if not body.get("entity_id"):
        return error("entity_id required")
    conn = await get_conn(request)
    s = RoomSensor.create(room_id=request.match_info["room_id"], entity_id=body["entity_id"])
    await db.add_room_sensor(conn, s)
    await emit(
        request,
        "info",
        "api",
        f"Sensor added to room {request.match_info['room_id']}: {body['entity_id']}",
        {"room_id": request.match_info["room_id"], "entity_id": body["entity_id"]},
    )
    return json_response(s.__dict__, status=201)


@docs(tags=["sensors"], summary="Remove a sensor from a room")
@response_schema(schemas.DeletedTrueSchema)
@routes.delete("/api/rooms/{room_id}/sensors/{entity_id:.*}")
async def remove_sensor(request: web.Request) -> web.Response:
    conn = await get_conn(request)
    await db.remove_room_sensor(
        conn, request.match_info["room_id"], request.match_info["entity_id"]
    )
    return json_response({"deleted": True})


# ---------------------------------------------------------------------------
# Room vents
# ---------------------------------------------------------------------------


@docs(tags=["vents"], summary="List vents in a room")
@response_schema(schemas.RoomVentSchema(many=True))
@routes.get("/api/rooms/{room_id}/vents")
async def list_vents(request: web.Request) -> web.Response:
    conn = await get_conn(request)
    vents = await db.get_room_vents(conn, request.match_info["room_id"])
    return json_response([v.__dict__ for v in vents])


@docs(tags=["vents"], summary="Add a vent to a room")
@request_schema(schemas.RoomVentSchema)
@response_schema(schemas.RoomVentSchema, code=201)
@routes.post("/api/rooms/{room_id}/vents")
async def add_vent(request: web.Request) -> web.Response:
    body = await request.json()
    if not body.get("entity_id"):
        return error("entity_id required")
    method = body.get("control_method", "open_close")
    if method not in VALID_CONTROL_METHODS:
        return error(f"invalid control_method: {method}")
    conn = await get_conn(request)
    v = RoomVent.create(
        room_id=request.match_info["room_id"],
        entity_id=body["entity_id"],
        control_method=method,
    )
    await db.add_room_vent(conn, v)
    await emit(
        request,
        "info",
        "api",
        f"Vent added to room {request.match_info['room_id']}: {body['entity_id']}",
        {
            "room_id": request.match_info["room_id"],
            "entity_id": body["entity_id"],
            "control_method": method,
        },
    )
    return json_response(v.__dict__, status=201)


@docs(tags=["vents"], summary="Update vent control method")
@request_schema(schemas.RoomVentUpdateSchema)
@response_schema(schemas.RoomVentUpdateResponseSchema)
@routes.patch("/api/rooms/{room_id}/vents/{entity_id:.*}")
async def update_vent(request: web.Request) -> web.Response:
    body = await request.json()
    method = body.get("control_method")
    if method is None:
        return error("control_method required")
    if method not in VALID_CONTROL_METHODS:
        return error(f"invalid control_method: {method}")
    conn = await get_conn(request)
    room_id = request.match_info["room_id"]
    entity_id = request.match_info["entity_id"]
    await db.update_room_vent_control_method(conn, room_id, entity_id, method)
    await emit(
        request,
        "info",
        "api",
        f"Vent {entity_id} control method updated to {method}",
        {"room_id": room_id, "entity_id": entity_id, "control_method": method},
    )
    return json_response({"updated": True, "control_method": method})


@docs(tags=["vents"], summary="Remove a vent from a room")
@response_schema(schemas.DeletedTrueSchema)
@routes.delete("/api/rooms/{room_id}/vents/{entity_id:.*}")
async def remove_vent(request: web.Request) -> web.Response:
    conn = await get_conn(request)
    await db.remove_room_vent(conn, request.match_info["room_id"], request.match_info["entity_id"])
    return json_response({"deleted": True})


@docs(tags=["vents"], summary="Test vent operation")
@request_schema(schemas.VentTestSchema)
@response_schema(schemas.SuccessSchema)
@routes.post("/api/vents/test")
async def test_vent(request: web.Request) -> web.Response:
    """Invoke the chosen open/close action against an entity immediately.

    Accepts draft form state (entity_id + control_method + direction) so the
    user can iterate on the method choice before saving. Surfaces the raw HA
    error message on failure so misconfigured integrations are diagnosable
    inside the app.
    """
    body = await request.json()
    entity_id = body.get("entity_id")
    method = body.get("control_method")
    direction = body.get("direction")
    if not entity_id:
        return error("entity_id required")
    if method not in VALID_CONTROL_METHODS:
        return error(f"invalid control_method: {method}")
    if direction not in ("open", "close"):
        return error("direction must be 'open' or 'close'")

    ha = request.app["ha"]
    try:
        if direction == "open":
            if method == "set_position":
                await ha.set_cover_position(entity_id, 100)
            elif method == "set_tilt_position":
                await ha.set_cover_tilt_position(entity_id, 100)
            elif method == "toggle":
                await ha.toggle_cover(entity_id)
            else:
                await ha.open_cover(entity_id)
        else:
            if method == "set_position":
                await ha.set_cover_position(entity_id, 0)
            elif method == "set_tilt_position":
                await ha.set_cover_tilt_position(entity_id, 0)
            elif method == "toggle":
                await ha.toggle_cover(entity_id)
            else:
                await ha.close_cover(entity_id)
    except Exception:
        log.exception("Vent test %s failed for %s (%s)", direction, entity_id, method)
        await emit(
            request,
            "warning",
            "api",
            f"Vent test {direction} failed for {entity_id} ({method})",
            {
                "entity_id": entity_id,
                "control_method": method,
                "direction": direction,
            },
        )
        return error("Vent test failed", status=400)

    await emit(
        request,
        "info",
        "api",
        f"Vent test {direction} succeeded for {entity_id} ({method})",
        {"entity_id": entity_id, "control_method": method, "direction": direction},
    )
    return json_response({"ok": True})


# ---------------------------------------------------------------------------
# Room presence sensors
# ---------------------------------------------------------------------------


@docs(tags=["presence"], summary="List presence sensors in a room")
@response_schema(schemas.RoomPresenceSensorSchema(many=True))
@routes.get("/api/rooms/{room_id}/presence")
async def list_presence(request: web.Request) -> web.Response:
    conn = await get_conn(request)
    ps = await db.get_room_presence_sensors(conn, request.match_info["room_id"])
    return json_response([p.__dict__ for p in ps])


@docs(tags=["presence"], summary="Add a presence sensor to a room")
@request_schema(schemas.RoomPresenceSensorSchema)
@response_schema(schemas.RoomPresenceSensorSchema, code=201)
@routes.post("/api/rooms/{room_id}/presence")
async def add_presence(request: web.Request) -> web.Response:
    body = await request.json()
    if not body.get("entity_id"):
        return error("entity_id required")
    conn = await get_conn(request)
    p = RoomPresenceSensor.create(
        room_id=request.match_info["room_id"], entity_id=body["entity_id"]
    )
    await db.add_room_presence_sensor(conn, p)
    await emit(
        request,
        "info",
        "api",
        f"Presence sensor added to room {request.match_info['room_id']}: {body['entity_id']}",
        {"room_id": request.match_info["room_id"], "entity_id": body["entity_id"]},
    )
    return json_response(p.__dict__, status=201)


@docs(tags=["presence"], summary="Clear the active presence holdover for a room")
@response_schema(schemas.DeletedTrueSchema)
@routes.delete("/api/rooms/{room_id}/presence/holdover")
async def clear_presence_holdover(request: web.Request) -> web.Response:
    conn = await get_conn(request)
    room_id = request.match_info["room_id"]
    await db.delete_holdover_state(conn, room_id)
    await emit(
        request,
        "info",
        "api",
        f"Presence holdover cleared for room {room_id}",
        {"room_id": room_id},
    )
    return json_response({"ok": True})


@docs(tags=["presence"], summary="Remove a presence sensor from a room")
@response_schema(schemas.DeletedTrueSchema)
@routes.delete("/api/rooms/{room_id}/presence/{entity_id:.*}")
async def remove_presence(request: web.Request) -> web.Response:
    conn = await get_conn(request)
    await db.remove_room_presence_sensor(
        conn, request.match_info["room_id"], request.match_info["entity_id"]
    )
    return json_response({"deleted": True})


# ---------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------


def _schedule_to_dict(s: Schedule) -> dict:
    return {
        "id": s.id,
        "room_id": s.room_id,
        "days_of_week": s.days_of_week,
        "start_time": s.start_time.isoformat(),
        "end_time": s.end_time.isoformat(),
        "target_temp": s.target_temp,
        "enabled": s.enabled,
        # Naive LOCAL wall-clock ISO (or null = never expire). Matches the
        # frontend <input type="datetime-local"> contract (Issue #359).
        "expires_at": s.expires_at.isoformat() if s.expires_at else None,
    }


def _parse_expires_at(raw: object) -> datetime | None:
    """Parse a request `expires_at` value into a naive LOCAL datetime.

    Accepts None/"" (→ None = never expire), a naive local ISO string (as sent
    by `<input type="datetime-local">`), or an aware ISO string (converted to
    local naive). Raises ValueError/TypeError on malformed input.
    """
    if raw in (None, ""):
        return None
    if not isinstance(raw, str):
        raise TypeError("expires_at must be a string or null")
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is not None:
        dt = tz.to_local_naive(dt)
    return dt


def _schedule_overlap_label(e: Schedule) -> str:
    """Human-readable description of an existing block for overlap errors."""
    days_str = ", ".join(room_manager.DAYS_SHORT[d] for d in sorted(e.days_of_week))
    return f"{days_str} {e.start_time.strftime('%H:%M')}–{e.end_time.strftime('%H:%M')}"


@docs(tags=["schedules"], summary="List schedules for a room")
@response_schema(schemas.ScheduleSchema(many=True))
@routes.get("/api/rooms/{room_id}/schedules")
async def list_schedules(request: web.Request) -> web.Response:
    conn = await get_conn(request)
    schedules = await db.get_schedules_for_room(conn, request.match_info["room_id"])
    return json_response([_schedule_to_dict(s) for s in schedules])


@docs(tags=["schedules"], summary="Create a new schedule")
@request_schema(schemas.ScheduleSchema)
@response_schema(schemas.ScheduleSchema, code=201)
@routes.post("/api/rooms/{room_id}/schedules")
async def create_schedule(request: web.Request) -> web.Response:
    body = await request.json()
    required = ("days_of_week", "start_time", "end_time", "target_temp")
    if not all(k in body for k in required):
        return error(f"Required fields: {required}")
    unit = request.app["scheduler"].get_temperature_unit()
    try:
        target_temp_f = _to_f(float(body["target_temp"]), unit)
        if not (40 <= target_temp_f <= 90):
            return _temp_range_error("target_temp", 40, 90, unit)

        enabled = bool(body.get("enabled", True))
        expires_at = _parse_expires_at(body.get("expires_at"))
        s = Schedule.create(
            room_id=request.match_info["room_id"],
            days_of_week=body["days_of_week"],
            start_time=time.fromisoformat(body["start_time"]),
            end_time=time.fromisoformat(body["end_time"]),
            target_temp=target_temp_f,
            enabled=enabled,
            expires_at=expires_at,
        )
    except (ValueError, TypeError):
        return error("Invalid schedule payload")

    # An enabled block cannot be born already expired (Issue #359).
    if (
        s.enabled
        and s.expires_at is not None
        and s.expires_at <= tz.now_local().replace(tzinfo=None)
    ):
        return error("expires_at must be in the future")

    conn = await get_conn(request)
    # Overlap only matters for enabled blocks, and only against other enabled
    # blocks — a disabled (parked) block does not reserve its time slot (#359).
    if s.enabled:
        existing = await db.get_schedules_for_room(conn, request.match_info["room_id"])
        for e in existing:
            if e.enabled and room_manager.schedules_overlap(s, e):
                return error(f"Overlaps with existing block on {_schedule_overlap_label(e)}")
    await db.upsert_schedule(conn, s)
    return json_response(_schedule_to_dict(s), status=201)


@docs(tags=["schedules"], summary="Update a schedule")
@request_schema(schemas.ScheduleSchema)
@response_schema(schemas.ScheduleSchema)
@routes.put("/api/rooms/{room_id}/schedules/{schedule_id}")
async def update_schedule(request: web.Request) -> web.Response:
    conn = await get_conn(request)
    schedules = await db.get_schedules_for_room(conn, request.match_info["room_id"])
    sid = request.match_info["schedule_id"]
    schedule = next((s for s in schedules if s.id == sid), None)
    if not schedule:
        return error("Schedule not found", 404)
    body = await request.json()
    unit = request.app["scheduler"].get_temperature_unit()
    if "days_of_week" in body:
        schedule.days_of_week = body["days_of_week"]
    if "start_time" in body:
        schedule.start_time = time.fromisoformat(body["start_time"])
    if "end_time" in body:
        schedule.end_time = time.fromisoformat(body["end_time"])
    if "target_temp" in body:
        try:
            target_temp_f = _to_f(float(body["target_temp"]), unit)
        except (ValueError, TypeError):
            return error("target_temp must be numeric")
        if not (40 <= target_temp_f <= 90):
            return _temp_range_error("target_temp", 40, 90, unit)
        schedule.target_temp = target_temp_f
    if "expires_at" in body:
        try:
            schedule.expires_at = _parse_expires_at(body["expires_at"])
        except (ValueError, TypeError):
            return error("expires_at must be a valid datetime or null")
    if "enabled" in body:
        schedule.enabled = bool(body["enabled"])

    # Block a past expiry only when the request is actually (re)enabling the
    # schedule or setting/changing the expiry — otherwise the next sweep would
    # immediately undo it. Editing an unrelated field (e.g. target_temp) on a
    # schedule that is enabled-but-past-expiry during the sweep's
    # finish-the-current-block deferral window must NOT be rejected (Issue #359).
    asserting_enabled = "enabled" in body and bool(body["enabled"])
    setting_expiry = "expires_at" in body
    if (
        (asserting_enabled or setting_expiry)
        and schedule.enabled
        and schedule.expires_at is not None
        and schedule.expires_at <= tz.now_local().replace(tzinfo=None)
    ):
        return error(
            "expires_at is in the past; clear it or pick a later time to keep this schedule enabled"
        )

    # Overlap is only enforced for enabled blocks against other enabled blocks.
    # Disabling never conflicts; enabling re-checks because the slot may have
    # been reused while this block was parked (Issue #359).
    if schedule.enabled:
        for e in schedules:
            if e.id == schedule.id or not e.enabled:
                continue
            if room_manager.schedules_overlap(schedule, e):
                return error(f"Overlaps with existing block on {_schedule_overlap_label(e)}")
    await db.upsert_schedule(conn, schedule)
    return json_response(_schedule_to_dict(schedule))


@docs(tags=["schedules"], summary="Delete a schedule")
@response_schema(schemas.DeletedTrueSchema)
@routes.delete("/api/rooms/{room_id}/schedules/{schedule_id}")
async def delete_schedule(request: web.Request) -> web.Response:
    conn = await get_conn(request)
    await db.delete_schedule(conn, request.match_info["schedule_id"])
    return json_response({"deleted": True})


@docs(tags=["schedules"], summary="Copy a schedule to one or more other rooms")
@request_schema(schemas.ScheduleCopyRequestSchema)
@response_schema(schemas.ScheduleCopyResultSchema(many=True))
@routes.post("/api/rooms/{room_id}/schedules/{schedule_id}/copy")
async def copy_schedule(request: web.Request) -> web.Response:
    """Replicate a block into other rooms (Issue #359).

    The copy carries days/times/target only — it is created enabled and
    never-expiring (the source's expiry is room/guest-specific). If a copy
    would overlap an existing *enabled* block in a target room it is still
    created, but disabled, and reported as ``created_disabled_conflict`` so a
    human can resolve the conflict and re-enable it.
    """
    conn = await get_conn(request)
    src_room_id = request.match_info["room_id"]
    sid = request.match_info["schedule_id"]
    src_schedules = await db.get_schedules_for_room(conn, src_room_id)
    source = next((s for s in src_schedules if s.id == sid), None)
    if not source:
        return error("Schedule not found", 404)

    body = await request.json()
    targets = body.get("target_room_ids")
    if not isinstance(targets, list) or not targets:
        return error("target_room_ids must be a non-empty list")
    # Validate every target up front so a bad id creates nothing.
    for target_room_id in targets:
        if not isinstance(target_room_id, str):
            return error("target_room_ids must be a list of room id strings")
        if target_room_id == src_room_id:
            return error("Cannot copy a schedule onto its own room")
        if await db.get_room(conn, target_room_id) is None:
            return error(f"Room not found: {target_room_id}", 404)

    results: list[dict] = []
    for target_room_id in targets:
        copy = Schedule.create(
            room_id=target_room_id,
            days_of_week=source.days_of_week,
            start_time=source.start_time,
            end_time=source.end_time,
            target_temp=source.target_temp,
        )
        existing = await db.get_schedules_for_room(conn, target_room_id)
        conflict = next(
            (e for e in existing if e.enabled and room_manager.schedules_overlap(copy, e)),
            None,
        )
        if conflict is not None:
            copy.enabled = False
            status = "created_disabled_conflict"
            conflict_with: str | None = _schedule_overlap_label(conflict)
        else:
            status = "created"
            conflict_with = None
        await db.upsert_schedule(conn, copy)
        results.append(
            {
                "room_id": target_room_id,
                "schedule_id": copy.id,
                "status": status,
                "conflict_with": conflict_with,
            }
        )
    return json_response(results)


# ---------------------------------------------------------------------------
# Thermostat configs
# ---------------------------------------------------------------------------


@docs(tags=["thermostats"], summary="List all thermostat configurations")
@response_schema(schemas.ThermostatConfigSchema(many=True))
@routes.get("/api/thermostats")
async def list_thermostats(request: web.Request) -> web.Response:
    conn = await get_conn(request)
    configs = await db.get_all_thermostat_configs(conn)
    return json_response([tc.__dict__ for tc in configs])


@docs(tags=["thermostats"], summary="Create a new thermostat configuration")
@request_schema(schemas.ThermostatConfigSchema)
@response_schema(schemas.ThermostatConfigSchema, code=201)
@routes.post("/api/thermostats")
async def create_thermostat(request: web.Request) -> web.Response:
    body = await request.json()
    if not body.get("thermostat_entity_id"):
        return error("thermostat_entity_id required")

    # Airflow-floor (#213): total_vents_count is mandatory at registration.
    # Existing thermostats predate this requirement and surface a banner asking
    # the user to fill it in; new ones must supply it up-front.
    if body.get("total_vents_count") is None:
        return error(
            "total_vents_count required — count every register on this thermostat, "
            "smart vents AND passive ones, not only smart vents"
        )

    conn = await get_conn(request)
    unit = request.app["scheduler"].get_temperature_unit()
    # Eco Mode requires an outside-temperature sensor (Issue #404).
    blocked = await _eco_enable_blocked(conn, bool(body.get("eco_mode_enabled")))
    if blocked is not None:
        return blocked
    # Load defaults then apply body fields
    tc = await db.get_thermostat_config(conn, body["thermostat_entity_id"])

    # Security: input validation
    min_val = body.get("min_setpoint")
    max_val = body.get("max_setpoint")
    default_temp = body.get("default_temp")
    if (
        (min_val is not None and not isinstance(min_val, (int, float)))
        or (max_val is not None and not isinstance(max_val, (int, float)))
        or (default_temp is not None and not isinstance(default_temp, (int, float)))
    ):
        return error("Temperatures must be numeric")

    if default_temp is not None:
        default_temp_f = _to_f(default_temp, unit)
        if not (40 <= default_temp_f <= 90):
            return _temp_range_error("default_temp", 40, 90, unit)

    # Use existing (F) values as default if not in body
    min_f = _to_f(min_val, unit) if min_val is not None else tc.min_setpoint
    max_f = _to_f(max_val, unit) if max_val is not None else tc.max_setpoint

    if not (40 <= min_f <= 100) or not (40 <= max_f <= 100):
        return _temp_range_error("Setpoints", 40, 100, unit)
    if min_f >= max_f:
        return error("min_setpoint must be less than max_setpoint")
    for field in (
        "name",
        "default_temp",
        "min_setpoint",
        "max_setpoint",
        "deadband",
        "max_vent_closed_min",
        "overshoot_delta",
        "cycle_timeout_hours",
        "reconciliation_interval_min",
        "vacation_hvac_mode",
        "min_cycle_runtime_min",
        "min_cycle_offtime_min",
        "cooling_lockout_below_f",
        "total_vents_count",
        "has_bypass_damper",
        "min_open_vents_fraction",
        "overflow_during_min_runtime",
        "unavailable_abort_after_min",
        "eco_mode_enabled",
        "eco_cooling_outdoor_threshold",
        "eco_cooling_full_drift_temp",
        "eco_cooling_max_drift",
        "eco_heating_outdoor_threshold",
        "eco_heating_full_drift_temp",
        "eco_heating_max_drift",
        "eco_hysteresis_band",
    ):
        if field in body:
            if field in ("min_setpoint", "max_setpoint"):
                setattr(tc, field, _to_f(body[field], unit))
            elif field == "default_temp":
                # Nullable absolute temperature — null clears the per-thermostat
                # presence-activation default (rooms fall back to the system value).
                val = body[field]
                setattr(tc, field, _to_f(val, unit) if val is not None else None)
            elif field in ("deadband", "overshoot_delta"):
                err = _validate_thermostat_numeric(field, body[field])
                if err is not None:
                    return err
                setattr(tc, field, _delta_to_f(body[field], unit))
            elif field == "vacation_hvac_mode":
                if body[field] not in ("range", "single"):
                    return error("vacation_hvac_mode must be 'range' or 'single'")
                setattr(tc, field, body[field])
            elif field == "cooling_lockout_below_f":
                # Nullable absolute temperature — null disables the lockout.
                val = body[field]
                setattr(tc, field, _to_f(val, unit) if val is not None else None)
            elif field == "total_vents_count":
                # Airflow-floor (#213): total registers on this thermostat
                # (smart + passive). Null clears the value, returning the
                # thermostat to the transitional "≥1 open" default.
                val = body[field]
                if val is None:
                    setattr(tc, field, None)
                else:
                    if not isinstance(val, int) or val < 1:
                        return error("total_vents_count must be a positive integer")
                    setattr(tc, field, val)
            elif field == "has_bypass_damper":
                setattr(tc, field, bool(body[field]))
            elif field == "min_open_vents_fraction":
                val = body[field]
                if not isinstance(val, (int, float)) or not (0 < val <= 1):
                    return error("min_open_vents_fraction must be > 0 and ≤ 1")
                setattr(tc, field, float(val))
            elif field == "overflow_during_min_runtime":
                setattr(tc, field, bool(body[field]))
            elif field == "unavailable_abort_after_min":
                # Minutes of sustained thermostat unavailability before a
                # running cycle is aborted (#267). 0 = never abort.
                val = body[field]
                if not isinstance(val, int) or isinstance(val, bool) or val < 0:
                    return error("unavailable_abort_after_min must be a non-negative integer")
                setattr(tc, field, val)
            elif field == "eco_mode_enabled":
                # Eco Mode master toggle for this thermostat (Issue #404).
                setattr(tc, field, bool(body[field]))
            elif field in _ECO_FIELD_SPECS:
                # Eco Mode numeric config (Issue #404). Non-null on the
                # thermostat; converted + range-checked after the unit boundary.
                err, val_f = _validate_eco_temp(field, body[field], unit, allow_none=False)
                if err is not None:
                    return err
                setattr(tc, field, val_f)
            elif field in _THERMO_NUMERIC_BOUNDS:
                # cycle_timeout_hours / max_vent_closed_min / *_min fields — the
                # engine does timedelta/interval arithmetic on these, so reject
                # non-numeric and out-of-range values rather than store them and
                # crash a later tick (Issue #295).
                err = _validate_thermostat_numeric(field, body[field])
                if err is not None:
                    return err
                setattr(tc, field, body[field])
            else:
                setattr(tc, field, body[field])
    await db.upsert_thermostat_config(conn, tc)
    await refresh(request)
    await emit(
        request,
        "info",
        "api",
        f"Thermostat registered: {tc.name or tc.thermostat_entity_id}",
        {"entity_id": tc.thermostat_entity_id},
    )
    return json_response(tc.__dict__, status=201)


@docs(tags=["thermostats"], summary="Update a thermostat configuration")
@request_schema(schemas.ThermostatConfigSchema)
@response_schema(schemas.ThermostatConfigSchema)
@routes.put("/api/thermostats/{entity_id:.*}")
async def upsert_thermostat(request: web.Request) -> web.Response:
    entity_id = request.match_info["entity_id"]
    conn = await get_conn(request)
    unit = request.app["scheduler"].get_temperature_unit()
    tc = await db.get_thermostat_config(conn, entity_id)
    body = await request.json()
    # Eco Mode requires an outside-temperature sensor (Issue #404).
    blocked = await _eco_enable_blocked(conn, bool(body.get("eco_mode_enabled")))
    if blocked is not None:
        return blocked

    # Security: input validation
    min_val = body.get("min_setpoint")
    max_val = body.get("max_setpoint")
    default_temp = body.get("default_temp")
    if (
        (min_val is not None and not isinstance(min_val, (int, float)))
        or (max_val is not None and not isinstance(max_val, (int, float)))
        or (default_temp is not None and not isinstance(default_temp, (int, float)))
    ):
        return error("Temperatures must be numeric")

    if default_temp is not None:
        default_temp_f = _to_f(default_temp, unit)
        if not (40 <= default_temp_f <= 90):
            return _temp_range_error("default_temp", 40, 90, unit)

    # Use existing (F) values as default if not in body
    min_f = _to_f(min_val, unit) if min_val is not None else tc.min_setpoint
    max_f = _to_f(max_val, unit) if max_val is not None else tc.max_setpoint

    if not (40 <= min_f <= 100) or not (40 <= max_f <= 100):
        return _temp_range_error("Setpoints", 40, 100, unit)
    if min_f >= max_f:
        return error("min_setpoint must be less than max_setpoint")
    for field in (
        "name",
        "default_temp",
        "min_setpoint",
        "max_setpoint",
        "deadband",
        "max_vent_closed_min",
        "overshoot_delta",
        "cycle_timeout_hours",
        "reconciliation_interval_min",
        "vacation_hvac_mode",
        "min_cycle_runtime_min",
        "min_cycle_offtime_min",
        "cooling_lockout_below_f",
        "total_vents_count",
        "has_bypass_damper",
        "min_open_vents_fraction",
        "overflow_during_min_runtime",
        "unavailable_abort_after_min",
        "eco_mode_enabled",
        "eco_cooling_outdoor_threshold",
        "eco_cooling_full_drift_temp",
        "eco_cooling_max_drift",
        "eco_heating_outdoor_threshold",
        "eco_heating_full_drift_temp",
        "eco_heating_max_drift",
        "eco_hysteresis_band",
    ):
        if field in body:
            if field in ("min_setpoint", "max_setpoint"):
                setattr(tc, field, _to_f(body[field], unit))
            elif field == "default_temp":
                # Nullable absolute temperature — null clears the per-thermostat
                # presence-activation default (rooms fall back to the system value).
                val = body[field]
                setattr(tc, field, _to_f(val, unit) if val is not None else None)
            elif field in ("deadband", "overshoot_delta"):
                err = _validate_thermostat_numeric(field, body[field])
                if err is not None:
                    return err
                setattr(tc, field, _delta_to_f(body[field], unit))
            elif field == "vacation_hvac_mode":
                if body[field] not in ("range", "single"):
                    return error("vacation_hvac_mode must be 'range' or 'single'")
                setattr(tc, field, body[field])
            elif field == "cooling_lockout_below_f":
                # Nullable absolute temperature — null disables the lockout.
                val = body[field]
                setattr(tc, field, _to_f(val, unit) if val is not None else None)
            elif field == "total_vents_count":
                # Airflow-floor (#213): total registers on this thermostat
                # (smart + passive). Null clears the value, returning the
                # thermostat to the transitional "≥1 open" default.
                val = body[field]
                if val is None:
                    setattr(tc, field, None)
                else:
                    if not isinstance(val, int) or val < 1:
                        return error("total_vents_count must be a positive integer")
                    setattr(tc, field, val)
            elif field == "has_bypass_damper":
                setattr(tc, field, bool(body[field]))
            elif field == "min_open_vents_fraction":
                val = body[field]
                if not isinstance(val, (int, float)) or not (0 < val <= 1):
                    return error("min_open_vents_fraction must be > 0 and ≤ 1")
                setattr(tc, field, float(val))
            elif field == "overflow_during_min_runtime":
                setattr(tc, field, bool(body[field]))
            elif field == "unavailable_abort_after_min":
                # Minutes of sustained thermostat unavailability before a
                # running cycle is aborted (#267). 0 = never abort.
                val = body[field]
                if not isinstance(val, int) or isinstance(val, bool) or val < 0:
                    return error("unavailable_abort_after_min must be a non-negative integer")
                setattr(tc, field, val)
            elif field == "eco_mode_enabled":
                # Eco Mode master toggle for this thermostat (Issue #404).
                setattr(tc, field, bool(body[field]))
            elif field in _ECO_FIELD_SPECS:
                # Eco Mode numeric config (Issue #404). Non-null on the
                # thermostat; converted + range-checked after the unit boundary.
                err, val_f = _validate_eco_temp(field, body[field], unit, allow_none=False)
                if err is not None:
                    return err
                setattr(tc, field, val_f)
            elif field in _THERMO_NUMERIC_BOUNDS:
                # cycle_timeout_hours / max_vent_closed_min / *_min fields — the
                # engine does timedelta/interval arithmetic on these, so reject
                # non-numeric and out-of-range values rather than store them and
                # crash a later tick (Issue #295).
                err = _validate_thermostat_numeric(field, body[field])
                if err is not None:
                    return err
                setattr(tc, field, body[field])
            else:
                setattr(tc, field, body[field])
    await db.upsert_thermostat_config(conn, tc)
    await emit(
        request,
        "info",
        "api",
        f"Thermostat updated: {tc.name or tc.thermostat_entity_id}",
        {"entity_id": tc.thermostat_entity_id},
    )
    return json_response(tc.__dict__)


@docs(tags=["thermostats"], summary="Delete a thermostat configuration")
@response_schema(schemas.DeletedSchema)
@routes.delete("/api/thermostats/{entity_id:.*}")
async def delete_thermostat(request: web.Request) -> web.Response:
    entity_id = request.match_info["entity_id"]
    conn = await get_conn(request)
    await db.delete_thermostat_config(conn, entity_id)
    await refresh(request)
    await emit(
        request,
        "info",
        "api",
        f"Thermostat removed: {entity_id}",
        {"entity_id": entity_id},
    )
    return json_response({"deleted": entity_id})


# ---------------------------------------------------------------------------
# Overrides
# ---------------------------------------------------------------------------


@docs(tags=["overrides"], summary="Set a room temperature override")
@request_schema(schemas.RoomOverrideRequestSchema)
@response_schema(schemas.RoomOverrideSchema)
@routes.post("/api/rooms/{room_id}/override")
async def set_override(request: web.Request) -> web.Response:
    body = await request.json()
    if "target_temp" not in body:
        return error("target_temp required")

    # Security: input validation
    unit = request.app["scheduler"].get_temperature_unit()
    try:
        target_temp_f = _to_f(float(body["target_temp"]), unit)
    except (ValueError, TypeError):
        return error("target_temp must be numeric")
    if not (40 <= target_temp_f <= 90):
        return _temp_range_error("target_temp", 40, 90, unit)

    try:
        duration_hours = float(body.get("duration_hours", 2.0))
    except (ValueError, TypeError):
        return error("duration_hours must be numeric")
    if not (0 <= duration_hours <= 8760):
        return error("duration_hours must be between 0 and 8760")

    override = RoomOverride(
        room_id=request.match_info["room_id"],
        target_temp=target_temp_f,
        expires_at=datetime.now(UTC) + timedelta(hours=duration_hours),
    )
    conn = await get_conn(request)
    await db.set_room_override(conn, override)
    return json_response(
        {
            "room_id": override.room_id,
            "target_temp": override.target_temp,
            "expires_at": override.expires_at.replace(tzinfo=None).isoformat(),
        }
    )


@docs(tags=["overrides"], summary="Clear a room temperature override")
@response_schema(schemas.ClearedSchema)
@routes.delete("/api/rooms/{room_id}/override")
async def clear_override(request: web.Request) -> web.Response:
    conn = await get_conn(request)
    await db.clear_room_override(conn, request.match_info["room_id"])
    return json_response({"cleared": True})


# ---------------------------------------------------------------------------
# Room active-status (for UI cards)
# ---------------------------------------------------------------------------


@docs(tags=["rooms"], summary="Get detailed active status for multiple rooms")
@response_schema(schemas.RoomActiveStatusResponseSchema)
@routes.post("/api/rooms/active-status")
async def rooms_active_status(request: web.Request) -> web.Response:
    """
    Return detailed active status for a list of room IDs.
    Body: { "room_ids": ["...", ...] }
    Response: { "<room_id>": { source, target_temp, ends_in_seconds, ... }, ... }
    """
    body = await request.json()
    room_ids: list[str] = body.get("room_ids", [])
    conn = await get_conn(request)
    now = datetime.now(UTC)

    result = {}
    for room_id in room_ids:
        room = await db.get_room(conn, room_id)
        if not room:
            continue
        schedules = await db.get_schedules_for_room(conn, room_id)
        status = await room_manager.get_room_active_status(conn, room, schedules, now)
        result[room_id] = status

    return json_response(result)


# ---------------------------------------------------------------------------
# System status + HA entity proxy
# ---------------------------------------------------------------------------


@docs(tags=["system"], summary="Get current zone statuses")
@response_schema(schemas.ZoneStatusSchema(many=True))
@routes.get("/api/status")
async def status(request: web.Request) -> web.Response:
    zones = request.app["scheduler"].get_all_zone_statuses()
    return json_response(zones)


@docs(tags=["ha"], summary="Get current states of HA entities")
@response_schema(schemas.EntityStateResponseSchema)
@routes.post("/api/ha/states")
async def ha_states(request: web.Request) -> web.Response:
    """Return live state for a list of entity IDs from the HA state cache."""
    body = await request.json()
    entity_ids: list[str] = body.get("entity_ids", [])
    ha = request.app["ha"]
    result: dict[str, Any] = {}
    for eid in entity_ids:
        state = ha.get_state(eid)
        if state is None:
            result[eid] = None
            continue
        raw = state.get("state")
        attrs = state.get("attributes", {})
        unit = attrs.get("unit_of_measurement", "")
        # Normalise HA entity states: °C values from HA are converted to °F here,
        # independent of the active display unit (frontend handles display conversion).
        numeric = None
        try:
            val = float(raw)
            if unit == "°C":
                val = _to_f(val, "C")  # shared converter (Issue #251)
                unit = "°F"
            numeric = round(val, 1)
        except (ValueError, TypeError):
            pass
        result[eid] = {
            "state": raw,
            "numeric": numeric,
            "unit": unit,
            "attributes": attrs,
        }
    return json_response(result)


@docs(tags=["ha"], summary="List HA entities, optionally filtered by domain")
@query_params(
    [
        {
            "name": "domain",
            "schema": {"type": "string"},
            "description": "Comma-separated HA domains to filter by (e.g. 'sensor,weather').",
        },
        {
            "name": "has_attribute",
            "schema": {"type": "string"},
            "description": "Only entities exposing this state attribute (e.g. 'hvac_action').",
        },
        {
            "name": "exclude_icon",
            "schema": {"type": "string"},
            "description": "Exclude entities whose icon matches (e.g. 'mdi:door-open').",
        },
    ]
)
@response_schema(schemas.HAEntitySchema(many=True))
@routes.get("/api/ha/entities")
async def ha_entities(request: web.Request) -> web.Response:
    domain = request.rel_url.query.get("domain")
    has_attribute = request.rel_url.query.get("has_attribute")  # e.g. "hvac_action"
    exclude_icon = request.rel_url.query.get("exclude_icon")  # e.g. "mdi:door-open"
    ha = request.app["ha"]
    if domain:
        # Accept comma-separated domains (e.g. "sensor,weather") so the
        # outside-temperature picker can query both in one round-trip (#85 3c).
        domains = [d.strip() for d in domain.split(",") if d.strip()]
        entities: list[dict] = []
        seen: set[str] = set()
        for d in domains:
            for e in await ha.get_entities_by_domain(d):
                eid = e["entity_id"]
                if eid not in seen:
                    seen.add(eid)
                    entities.append(e)
    else:
        entities = list(ha._state_cache.values())
    # Optional attribute-presence filter (keeps only entities that have the attribute)
    if has_attribute:
        entities = [e for e in entities if has_attribute in e.get("attributes", {})]
    # Optional icon exclusion filter
    if exclude_icon:
        entities = [e for e in entities if e.get("attributes", {}).get("icon") != exclude_icon]
    result = [
        {
            "entity_id": e["entity_id"],
            "state": e.get("state"),
            "friendly_name": e.get("attributes", {}).get("friendly_name", e["entity_id"]),
        }
        for e in entities
    ]
    result.sort(key=lambda x: x["entity_id"])
    return json_response(result)


def _cycle_log_to_dict(log_entry, had_overflow: bool = False, had_eco: bool = False) -> dict:
    try:
        rooms = json.loads(log_entry.rooms_json) if log_entry.rooms_json else {}
    except (ValueError, TypeError):
        rooms = {}
    try:
        vents_at_start = json.loads(log_entry.vents_at_start) if log_entry.vents_at_start else None
    except (ValueError, TypeError):
        vents_at_start = None
    try:
        vents_at_end = json.loads(log_entry.vents_at_end) if log_entry.vents_at_end else None
    except (ValueError, TypeError):
        vents_at_end = None
    return {
        "id": log_entry.id,
        "thermostat_entity_id": log_entry.thermostat_entity_id,
        "started_at": log_entry.started_at.replace(tzinfo=None).isoformat(),
        "ended_at": log_entry.ended_at.replace(tzinfo=None).isoformat()
        if log_entry.ended_at
        else None,
        "mode": log_entry.mode,
        "rooms": rooms,
        "ended_reason": log_entry.ended_reason,
        "thermostat_temp_at_start": log_entry.thermostat_temp_at_start,
        "thermostat_temp_at_end": log_entry.thermostat_temp_at_end,
        "setpoint_at_start": log_entry.setpoint_at_start,
        "setpoint_at_end": log_entry.setpoint_at_end,
        "vents_at_start": vents_at_start,
        "vents_at_end": vents_at_end,
        # True when this cycle redirected surplus air into non-active rooms
        # during its minimum-runtime hold (Issue #254). Drives the Logs badge.
        "had_overflow": had_overflow,
        # True when Eco Mode relaxed at least one room's target this cycle
        # (Issue #404). Drives the Logs "Eco Mode" pill.
        "eco_active": had_eco,
    }


def _int_param(raw: str | None, *, default: int, minimum: int, maximum: int | None = None) -> int:
    """Parse an optional integer query param, clamping to [minimum, maximum].

    A missing or non-integer value falls back to *default* instead of 500-ing."""
    try:
        value = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


# Query params for the cycle-log list (Issue #403). Paging is limit+offset;
# date-range is start/end (with since/until kept as backward-compatible
# aliases). Declaring them surfaces the knobs over OpenAPI and MCP.
_LOGS_QUERY_PARAMS: list[dict[str, Any]] = [
    {
        "name": "limit",
        "schema": {"type": "integer", "minimum": 1, "maximum": 1000},
        "description": "Max cycles to return (default 50, capped at 1000).",
    },
    {
        "name": "offset",
        "schema": {"type": "integer", "minimum": 0},
        "description": "Number of cycles to skip, for paging through history (default 0).",
    },
    {
        "name": "start",
        "schema": {"type": "string"},
        "description": (
            "Only cycles started on/after this ISO date/datetime. Alias: `since`. "
            "Clamped forward to the cycle-log retention window."
        ),
    },
    {
        "name": "end",
        "schema": {"type": "string"},
        "description": "Only cycles started on/before this ISO date/datetime. Alias: `until`.",
    },
    {
        "name": "since",
        "schema": {"type": "string"},
        "description": "Backward-compatible alias of `start`.",
    },
    {
        "name": "until",
        "schema": {"type": "string"},
        "description": "Backward-compatible alias of `end`.",
    },
]


@docs(tags=["logs"], summary="Get cycle logs")
@query_params(_LOGS_QUERY_PARAMS)
@response_schema(schemas.CycleLogResponseSchema(many=True))
@routes.get("/api/logs")
async def get_logs(request: web.Request) -> web.Response:
    conn = await get_conn(request)
    q = request.rel_url.query
    limit = _int_param(q.get("limit"), default=50, minimum=1, maximum=1000)
    offset = _int_param(q.get("offset"), default=0, minimum=0)
    # `start`/`end` are the date-oriented aliases (mirroring the metrics API);
    # `since`/`until` stay supported for backward compatibility (Issue #403).
    since = q.get("since") or q.get("start") or None
    until = q.get("until") or q.get("end") or None
    # Never page before the retention horizon — that data has been purged, so a
    # wider `since` would just misleadingly return the same in-window slice.
    floor = await _retention_floor(request)
    if since is not None and since < floor:
        since = floor
    logs = await db.get_cycle_logs(conn, limit=limit, offset=offset, since=since, until=until)
    log_ids = [log_entry.id for log_entry in logs]
    overflow_ids = await db.get_cycle_ids_with_overflow(conn, log_ids)
    eco_ids = await db.get_cycle_ids_with_eco(conn, log_ids)
    return json_response(
        [
            _cycle_log_to_dict(log_entry, log_entry.id in overflow_ids, log_entry.id in eco_ids)
            for log_entry in logs
        ]
    )


@docs(tags=["logs"], summary="Get detailed cycle log by ID")
@response_schema(schemas.CycleDetailResponseSchema)
@routes.get("/api/logs/{cycle_id}/detail")
async def get_log_detail(request: web.Request) -> web.Response:
    """Return enriched per-cycle diagnostics: rooms, vent events, setpoint history."""
    conn = await get_conn(request)
    cycle_id = request.match_info["cycle_id"]
    cycle = await db.get_cycle_log(conn, cycle_id)
    if cycle is None:
        return json_response({"error": "not_found"}, status=404)

    rooms_meta: dict = {}
    try:
        rooms_meta = json.loads(cycle.rooms_json) if cycle.rooms_json else {}
    except (ValueError, TypeError):
        rooms_meta = {}

    room_states = await db.get_room_cycle_states(conn, cycle_id)
    had_overflow = any(rcs.role == "overflow" for rcs in room_states)
    had_eco = any(rcs.eco_active for rcs in room_states)
    rooms_payload = []
    for rcs in room_states:
        meta = rooms_meta.get(rcs.room_id, {}) or {}
        try:
            trigger = json.loads(rcs.trigger_detail) if rcs.trigger_detail else None
        except (ValueError, TypeError):
            trigger = None
        # Overflow rooms (Issue #254) are not in the cycle's rooms_json
        # snapshot, so fall back to the live room name for them.
        name = meta.get("name")
        if name is None:
            room = await db.get_room(conn, rcs.room_id)
            name = room.name if room else None
        rooms_payload.append(
            {
                "room_id": rcs.room_id,
                "name": name,
                "source": meta.get("source"),
                "target_temp": rcs.target_temp,
                "reached_at": rcs.reached_at.replace(tzinfo=None).isoformat()
                if rcs.reached_at
                else None,
                "vent_closed_at": rcs.vent_closed_at.replace(tzinfo=None).isoformat()
                if rcs.vent_closed_at
                else None,
                "temp_at_start": rcs.temp_at_start,
                "temp_at_end": rcs.temp_at_end,
                "trigger_detail": trigger,
                "joined_at": rcs.joined_at.replace(tzinfo=None).isoformat()
                if rcs.joined_at
                else None,
                "role": rcs.role,
                # Eco Mode measurability (Issue #404): pre-relaxation vs relaxed
                # target and whether Eco actually moved it this cycle.
                "requested_target": rcs.requested_target,
                "effective_target": rcs.effective_target,
                "eco_active": rcs.eco_active,
            }
        )

    vent_events = await db.get_cycle_vent_events(conn, cycle_id)
    vent_events_payload = [
        {
            "id": ev.id,
            "timestamp": ev.timestamp.replace(tzinfo=None).isoformat(),
            "entity_id": ev.entity_id,
            "room_id": ev.room_id,
            "action": ev.action,
            "reason": ev.reason,
        }
        for ev in vent_events
    ]

    setpoint_history = await db.get_cycle_setpoint_history(conn, cycle_id)
    setpoint_history_payload = [
        {
            "id": sp.id,
            "timestamp": sp.timestamp.replace(tzinfo=None).isoformat(),
            "setpoint": sp.setpoint,
            "reason": sp.reason,
        }
        for sp in setpoint_history
    ]

    return json_response(
        {
            "cycle": _cycle_log_to_dict(cycle, had_overflow, had_eco),
            "rooms": rooms_payload,
            "vent_events": vent_events_payload,
            "setpoint_history": setpoint_history_payload,
        }
    )


@docs(tags=["logs"], summary="Get temperature samples for a cycle")
@query_params(
    [
        {
            "name": "room_id",
            "schema": {"type": "string"},
            "description": "Filter samples to a single room.",
        }
    ]
)
@response_schema(schemas.CycleTempSampleSchema(many=True))
@routes.get("/api/logs/{cycle_id}/temp-samples")
async def get_log_temp_samples(request: web.Request) -> web.Response:
    """Return periodic temperature samples for a cycle, optionally filtered by room."""
    conn = await get_conn(request)
    cycle_id = request.match_info["cycle_id"]
    room_id = request.rel_url.query.get("room_id") or None
    samples = await db.get_cycle_temp_samples(conn, cycle_id, room_id=room_id)
    return json_response(
        [
            {
                "id": s.id,
                "cycle_id": s.cycle_id,
                "room_id": s.room_id,
                "timestamp": s.timestamp.replace(tzinfo=None).isoformat(),
                "room_temp": s.room_temp,
                "thermostat_temp": s.thermostat_temp,
                "setpoint": s.setpoint,
            }
            for s in samples
        ]
    )


@docs(tags=["logs"], summary="Get event logs")
@query_params(
    [
        {
            "name": "limit",
            "schema": {"type": "integer", "minimum": 1},
            "description": "Max events to return (default 100).",
        },
        {
            "name": "offset",
            "schema": {"type": "integer", "minimum": 0},
            "description": "Number of events to skip, for paging (default 0).",
        },
        {
            "name": "category",
            "schema": {"type": "string"},
            "description": "Filter to a single event category (e.g. 'dev').",
        },
        {
            "name": "since",
            "schema": {"type": "string"},
            "description": "Only events on/after this ISO date/datetime.",
        },
        {
            "name": "until",
            "schema": {"type": "string"},
            "description": "Only events on/before this ISO date/datetime.",
        },
        {
            "name": "level",
            "schema": {"type": "string"},
            "description": "Comma-separated levels to include (e.g. 'warning,error').",
        },
    ]
)
@response_schema(schemas.EventLogEntrySchema(many=True))
@routes.get("/api/logs/events")
async def get_event_logs(request: web.Request) -> web.Response:
    conn = await get_conn(request)
    limit = int(request.rel_url.query.get("limit", 100))
    offset = int(request.rel_url.query.get("offset", 0))
    category = request.rel_url.query.get("category") or None
    since = request.rel_url.query.get("since") or None
    until = request.rel_url.query.get("until") or None
    level_param = request.rel_url.query.get("level") or None
    levels = [lv.strip() for lv in level_param.split(",") if lv.strip()] if level_param else None
    logs = await db.get_event_logs(
        conn,
        limit=limit,
        offset=offset,
        category=category,
        since=since,
        until=until,
        levels=levels,
    )
    return json_response(logs)


@docs(tags=["logs"], summary="Clear all event logs")
@response_schema(schemas.ClearedSchema)
@routes.delete("/api/logs/events")
async def clear_event_logs(request: web.Request) -> web.Response:
    conn = await get_conn(request)
    await db.clear_event_logs(conn)
    await emit(request, "info", "system", "Event logs cleared by user")
    return json_response({"cleared": True})


@docs(tags=["settings"], summary="Get outside temperature entity setting")
@response_schema(schemas.OutsideTempEntitySettingSchema)
@routes.get("/api/settings/outside-temp-entity")
async def get_outside_temp_entity(request: web.Request) -> web.Response:
    """Return the configured outside-temperature HA entity_id and its current value (Issue #85 Phase 1b)."""
    conn = await get_conn(request)
    entity_id = await db.get_system_setting(conn, "outside_temperature_entity_id", "")
    current_value: float | None = None
    if entity_id:
        ha = request.app["ha"]
        try:
            current_value = ha.get_numeric_state(entity_id)
        except Exception:
            current_value = None
    return json_response({"entity_id": entity_id or None, "current_value": current_value})


@docs(tags=["settings"], summary="Set outside temperature entity setting")
@request_schema(schemas.OutsideTempEntitySettingSchema)
@response_schema(schemas.OutsideTempEntitySettingSchema)
@routes.put("/api/settings/outside-temp-entity")
async def set_outside_temp_entity(request: web.Request) -> web.Response:
    """Set the outside-temperature HA entity_id (Issue #85 Phase 1b).

    Validates that the entity exists in HA and exposes a numeric state via
    HAClient.get_numeric_state(); rejects with 400 otherwise. Pass
    entity_id=null (or empty string) to clear the setting.
    """
    conn = await get_conn(request)
    body = await request.json()
    if "entity_id" not in body:
        return error("entity_id field required")
    raw = body["entity_id"]
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        await db.set_system_setting(conn, "outside_temperature_entity_id", "")
        return json_response({"entity_id": None, "current_value": None})
    if not isinstance(raw, str):
        return error("entity_id must be a string or null")
    entity_id = raw.strip()
    ha = request.app["ha"]
    if ha.get_state(entity_id) is None:
        return error(f"Entity {entity_id!r} not found in Home Assistant")
    value = ha.get_numeric_state(entity_id)
    if value is None:
        return error(
            f"Entity {entity_id!r} does not return a numeric state (cannot be used as outside temperature)"
        )
    await db.set_system_setting(conn, "outside_temperature_entity_id", entity_id)
    return json_response({"entity_id": entity_id, "current_value": value})


@docs(tags=["settings"], summary="Get sensor-staleness threshold (Issue #211)")
@response_schema(schemas.SensorStalenessSettingSchema)
@routes.get("/api/settings/sensor-staleness")
async def get_sensor_staleness(request: web.Request) -> web.Response:
    """Return the configured sensor-staleness threshold in minutes (Issue #211)."""
    conn = await get_conn(request)
    from backend.engine.cycle_engine import SENSOR_STALE_AFTER_MIN

    raw = await db.get_system_setting(conn, "sensor_stale_after_min", str(SENSOR_STALE_AFTER_MIN))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = SENSOR_STALE_AFTER_MIN
    return json_response({"stale_after_min": value})


@docs(tags=["settings"], summary="Set sensor-staleness threshold (Issue #211)")
@request_schema(schemas.SensorStalenessSettingSchema)
@response_schema(schemas.SensorStalenessSettingSchema)
@routes.put("/api/settings/sensor-staleness")
async def set_sensor_staleness(request: web.Request) -> web.Response:
    """Set the sensor-staleness threshold. Range: 1 minute — 24 hours."""
    body = await request.json()
    val = body.get("stale_after_min")
    if not isinstance(val, (int, float)):
        return error("stale_after_min must be a number (minutes)")
    if not (1 <= val <= 24 * 60):
        return error("stale_after_min must be between 1 and 1440 minutes")
    conn = await get_conn(request)
    await db.set_system_setting(conn, "sensor_stale_after_min", str(float(val)))
    return json_response({"stale_after_min": float(val)})


@docs(tags=["diagnostics"], summary="Per-room sensor freshness summary (Issue #211)")
@response_schema(schemas.SensorHealthSchema)
@routes.get("/api/sensor-health")
async def get_sensor_health(request: web.Request) -> web.Response:
    """Per-room sensor freshness — drives the Dashboard banner and Room badges
    (Issue #211).

    Returns every configured room temperature sensor that has not reported
    within the active staleness threshold, with its age. A Home-Assistant entity
    not in the cache at all is reported with ``age_seconds=null`` so the UI can
    distinguish "stale" from "never seen". Rooms with no stale sensors are
    omitted from the response.
    """
    from backend.engine.cycle_engine import SENSOR_STALE_AFTER_MIN

    conn = await get_conn(request)
    raw = await db.get_system_setting(conn, "sensor_stale_after_min", str(SENSOR_STALE_AFTER_MIN))
    try:
        threshold_min = float(raw)
    except (TypeError, ValueError):
        threshold_min = SENSOR_STALE_AFTER_MIN

    ha = request.app["ha"]
    rooms = await db.get_all_rooms(conn)
    stale_rooms: list[dict] = []
    for room in rooms:
        sensors = await db.get_room_sensors(conn, room.id)
        stale_sensors: list[dict] = []
        for s in sensors:
            age_s = ha.get_state_age_seconds(s.entity_id)
            if age_s is None:
                # Never seen in the cache — the room never had a fresh reading.
                stale_sensors.append(
                    {
                        "entity_id": s.entity_id,
                        "age_seconds": None,
                        "reason": "not_in_cache",
                    }
                )
            elif age_s > threshold_min * 60:
                stale_sensors.append(
                    {
                        "entity_id": s.entity_id,
                        "age_seconds": age_s,
                        "reason": "stale",
                    }
                )
        if stale_sensors:
            stale_rooms.append(
                {
                    "room_id": room.id,
                    "room_name": room.name,
                    "thermostat_entity_id": room.thermostat_entity_id,
                    "stale_sensors": stale_sensors,
                }
            )
    return json_response(
        {
            "stale_after_min": threshold_min,
            "rooms": stale_rooms,
        }
    )


@docs(tags=["diagnostics"], summary="Thermostat availability summary (Issue #267)")
@response_schema(schemas.ThermostatHealthSchema)
@routes.get("/api/thermostat-health")
async def get_thermostat_health(request: web.Request) -> web.Response:
    """Registered thermostats whose climate entity is currently unavailable —
    drives the Dashboard banner (Issue #267), mirroring /api/sensor-health.

    A thermostat not in the HA state cache at all is reported with
    ``reason="not_in_cache"`` so the UI can distinguish "went unavailable" from
    "never seen". ``unavailable_seconds`` comes from the engine's per-tick
    availability tracking and may be null before the first tick of an outage.
    Healthy thermostats are omitted from the response.
    """
    conn = await get_conn(request)
    ha = request.app["ha"]
    scheduler = request.app["scheduler"]
    now = datetime.now(UTC)

    unavailable: list[dict] = []
    for tc in await db.get_all_thermostat_configs(conn):
        state = ha.get_state(tc.thermostat_entity_id)
        if state is not None and state.get("state") != "unavailable":
            continue
        engine = scheduler.get_engine(tc.thermostat_entity_id)
        since = engine.unavailable_since if engine else None
        unavailable.append(
            {
                "thermostat_entity_id": tc.thermostat_entity_id,
                "name": tc.name,
                "reason": "not_in_cache" if state is None else "unavailable",
                "unavailable_seconds": (now - since).total_seconds() if since else None,
                "abort_after_min": tc.unavailable_abort_after_min,
                "cycle_running": bool(engine and engine.cycle_state.value == "running"),
            }
        )
    return json_response({"thermostats": unavailable})


@docs(tags=["settings"], summary="Get log retention settings")
@response_schema(schemas.LogRetentionSettingsSchema)
@routes.get("/api/settings/log-retention")
async def get_log_retention(request: web.Request) -> web.Response:
    conn = await get_conn(request)
    event_days = int(await db.get_system_setting(conn, "event_log_retention_days", "7"))
    cycle_days = int(await db.get_system_setting(conn, "cycle_log_retention_days", "30"))
    return json_response(
        {
            "event_log_retention_days": event_days,
            "cycle_log_retention_days": cycle_days,
        }
    )


@docs(tags=["settings"], summary="Update log retention settings")
@request_schema(schemas.LogRetentionSettingsSchema)
@response_schema(schemas.LogRetentionSettingsSchema)
@routes.post("/api/settings/log-retention")
async def set_log_retention(request: web.Request) -> web.Response:
    conn = await get_conn(request)
    body = await request.json()
    if "event_log_retention_days" in body:
        days = max(1, int(body["event_log_retention_days"]))
        await db.set_system_setting(conn, "event_log_retention_days", str(days))
    if "cycle_log_retention_days" in body:
        days = max(1, int(body["cycle_log_retention_days"]))
        await db.set_system_setting(conn, "cycle_log_retention_days", str(days))
    event_days = int(await db.get_system_setting(conn, "event_log_retention_days", "7"))
    cycle_days = int(await db.get_system_setting(conn, "cycle_log_retention_days", "30"))
    return json_response(
        {
            "event_log_retention_days": event_days,
            "cycle_log_retention_days": cycle_days,
        }
    )


# ---------------------------------------------------------------------------
# Settings — aggregate + temperature-unit endpoints (Issue #123 Phase 1)
# ---------------------------------------------------------------------------


@docs(tags=["settings"], summary="Get all application settings")
@response_schema(schemas.AppSettingsSchema)
@routes.get("/api/settings")
async def get_settings(request: web.Request) -> web.Response:
    """Return all persisted application settings."""
    conn = await get_conn(request)
    temperature_unit = await db.get_system_setting(conn, "temperature_unit", "F")
    unit_change_ack_required = (
        await db.get_system_setting(conn, "unit_change_ack_required", "0") == "1"
    )
    scheduler = request.app["scheduler"]
    return json_response(
        {
            "temperature_unit": temperature_unit,
            "unit_change_ack_required": unit_change_ack_required,
            "vacation_mode": {
                "enabled": scheduler.get_vacation_mode(),
                "return_at": (
                    scheduler.get_vacation_return_at().isoformat()
                    if scheduler.get_vacation_return_at()
                    else None
                ),
            },
        }
    )


@docs(tags=["settings"], summary="Acknowledge temperature unit change")
@response_schema(schemas.UnitChangeAckResponseSchema)
@routes.post("/api/settings/ack-unit-change")
async def ack_unit_change(request: web.Request) -> web.Response:
    """Dismiss the unit-change banner by clearing the ack flag."""
    await request.app["scheduler"].ack_unit_change()
    return json_response({"unit_change_ack_required": False})


# ---------------------------------------------------------------------------
# Vacation mode
# ---------------------------------------------------------------------------


@docs(tags=["settings"], summary="Get vacation mode state")
@response_schema(schemas.VacationModeSchema)
@routes.get("/api/settings/vacation-mode")
async def get_vacation_mode(request: web.Request) -> web.Response:
    """Return current vacation mode state."""
    scheduler = request.app["scheduler"]
    return_at = scheduler.get_vacation_return_at()
    return json_response(
        {
            "enabled": scheduler.get_vacation_mode(),
            "return_at": return_at.isoformat() if return_at else None,
        }
    )


@docs(tags=["settings"], summary="Enable vacation mode")
@response_schema(schemas.VacationModeSchema, code=200)
@routes.post("/api/settings/vacation-mode")
async def enable_vacation_mode(request: web.Request) -> web.Response:
    """Enable vacation mode. Body: {return_at: ISO-8601 UTC string}."""
    body = await request.json()
    return_at_str = body.get("return_at")
    if not return_at_str:
        return error("return_at (ISO-8601 UTC datetime) is required")
    try:
        return_at = datetime.fromisoformat(return_at_str)
        if return_at.tzinfo is None:
            return_at = return_at.replace(tzinfo=UTC)
    except ValueError:
        return error("return_at must be a valid ISO-8601 datetime")
    if return_at <= datetime.now(UTC):
        return error("return_at must be in the future")
    await request.app["scheduler"].set_vacation_mode(True, return_at)
    await emit(
        request, "info", "api", "Vacation mode enabled", {"return_at": return_at.isoformat()}
    )
    return json_response({"enabled": True, "return_at": return_at.isoformat()})


@docs(tags=["settings"], summary="Disable vacation mode")
@response_schema(schemas.VacationModeSchema)
@routes.delete("/api/settings/vacation-mode")
async def disable_vacation_mode(request: web.Request) -> web.Response:
    """Disable vacation mode immediately and resume normal scheduling."""
    await request.app["scheduler"].set_vacation_mode(False)
    await emit(request, "info", "api", "Vacation mode disabled", {})
    return json_response({"enabled": False, "return_at": None})


@docs(tags=["thermostats"], summary="Test vacation range mode on thermostat")
@response_schema(schemas.VacationTestSchema)
@routes.post("/api/thermostats/{entity_id:.+}/test-vacation")
async def test_vacation_mode(request: web.Request) -> web.Response:
    """Temporarily send the vacation range command to a thermostat so the user
    can verify it responded correctly in HA. Caller is responsible for
    reverting (the engine will correct state on the next tick if vacation mode
    is not active, or keep the range if vacation mode is enabled)."""
    entity_id = request.match_info["entity_id"]
    conn = await get_conn(request)
    tc = await db.get_thermostat_config(conn, entity_id)
    ha = request.app["ha"]
    try:
        await ha.set_thermostat_temperature_range(entity_id, tc.min_setpoint, tc.max_setpoint)
    except Exception:
        log.exception("Failed to send thermostat range command for entity_id=%s", entity_id)
        return error("Failed to send range command", status=502)
    # Return current HA state so the UI can surface it to the user.
    state = ha.get_state(entity_id)
    return json_response(
        {
            "ok": True,
            "min_setpoint": tc.min_setpoint,
            "max_setpoint": tc.max_setpoint,
            "thermostat_state": state,
        }
    )


@docs(tags=["thermostats"], summary="Revert thermostat from vacation test mode back to off")
@response_schema(schemas.SuccessSchema)
@routes.delete("/api/thermostats/{entity_id:.+}/test-vacation")
async def revert_vacation_test(request: web.Request) -> web.Response:
    """Immediately revert a thermostat from heat_cool/auto mode back to off.
    Use after the Test auto mode button to undo the test without waiting for
    the engine's next tick."""
    entity_id = request.match_info["entity_id"]
    ha = request.app["ha"]
    try:
        await ha.set_thermostat_hvac_mode(entity_id, "off")
    except Exception:
        log.exception("Failed to revert thermostat mode for entity_id=%s", entity_id)
        return error("Failed to revert thermostat mode", status=502)
    return json_response({"ok": True})


@docs(tags=["system"], summary="Restart the application")
@response_schema(schemas.RestartResponseSchema)
@routes.post("/api/restart")
async def restart_app(request: web.Request) -> web.Response:
    """Gracefully restart the Plenum process (HA supervisor will restart the add-on)."""

    async def _do_restart() -> None:
        await asyncio.sleep(0.3)
        os.kill(os.getpid(), signal.SIGTERM)

    asyncio.create_task(_do_restart())
    return json_response({"restarting": True})


# ---------------------------------------------------------------------------
# Metrics rollup manual trigger (Issue #85 Phase 1d/1e)
# ---------------------------------------------------------------------------


@docs(tags=["metrics"], summary="Trigger daily metrics rollup")
@response_schema(schemas.SuccessSchema)
@routes.post("/api/metrics/rollup/daily")
async def trigger_daily_rollup(request: web.Request) -> web.Response:
    """Manually re-run the daily metrics rollup. Optional body: {days_back: int}."""
    body: dict = {}
    if request.body_exists:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            body = {}
    days_back = max(0, int(body.get("days_back", 1))) if "days_back" in body else 1
    n = await request.app["scheduler"].run_daily_metrics_rollup(days_back=days_back)
    return json_response({"rows_written": n, "days_back": days_back})


@docs(tags=["metrics"], summary="Trigger monthly metrics rollup")
@response_schema(schemas.SuccessSchema)
@routes.post("/api/metrics/rollup/monthly")
async def trigger_monthly_rollup(request: web.Request) -> web.Response:
    """Manually re-run the monthly metrics rollup. Optional body: {months_back: int}."""
    body: dict = {}
    if request.body_exists:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            body = {}
    months_back = max(0, int(body.get("months_back", 1))) if "months_back" in body else 1
    n = await request.app["scheduler"].run_monthly_metrics_rollup(months_back=months_back)
    return json_response({"rows_written": n, "months_back": months_back})


# ---------------------------------------------------------------------------
# Metrics read API (Issue #85 Phase 2)
# ---------------------------------------------------------------------------


def _retention_days(raw: str | None, default: int = 30) -> int:
    """Coerce the stored ``cycle_log_retention_days`` value to a positive int."""
    try:
        return max(1, int(raw))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


async def _retention_floor(request: web.Request) -> str:
    """Earliest local date whose cycle-log data is still retained (YYYY-MM-DD).

    Cycle logs older than ``cycle_log_retention_days`` have been purged, so any
    date-range query is clamped to start no earlier than this floor (Issue #403)
    — requesting a wider window would just return an emptier range and mislead.
    """
    conn = await get_conn(request)
    days = _retention_days(await db.get_system_setting(conn, "cycle_log_retention_days", "30"))
    return (tz.today_local() - timedelta(days=days)).isoformat()


async def _parse_date_range(request: web.Request, default_days: int = 7) -> tuple[str, str]:
    """Parse `start`/`end`/`days` query params (YYYY-MM-DD local) into an
    effective, retention-bounded ``[start, end]`` range (Issue #403).

    - `end` defaults to today.
    - When `start` is omitted, the window is the last `days` days (or, absent
      `days`, the last `default_days` days) ending at `end`.
    - The resolved `start` is clamped forward to the retention floor so callers
      cannot request data that has already been purged.

    Omitting every param preserves the historic behaviour: the last
    `default_days` days inclusive of today."""
    today = tz.today_local()
    start = request.rel_url.query.get("start") or None
    end = request.rel_url.query.get("end") or None
    if not end:
        end = today.isoformat()
    if start is None:
        n = default_days
        days_param = request.rel_url.query.get("days")
        if days_param is not None:
            try:
                n = max(1, int(days_param))
            except ValueError:
                n = default_days
        try:
            anchor = date.fromisoformat(end)
        except ValueError:
            anchor = today
        start = (anchor - timedelta(days=n - 1)).isoformat()
    floor = await _retention_floor(request)
    if start < floor:
        start = floor
    return start, end


# Date-range query params shared by every metrics endpoint that calls
# ``_parse_date_range`` (Issue #403). Declaring them here surfaces the knobs in
# the OpenAPI spec and the generated MCP tool schemas; the handlers still read
# them from ``request.rel_url.query``.
_DATE_RANGE_QUERY_PARAMS: list[dict[str, Any]] = [
    {
        "name": "start",
        "schema": {"type": "string", "format": "date"},
        "description": (
            "Inclusive start date (YYYY-MM-DD, local). Clamped forward to the "
            "cycle-log retention window. Defaults to a fixed window before `end`."
        ),
    },
    {
        "name": "end",
        "schema": {"type": "string", "format": "date"},
        "description": "Inclusive end date (YYYY-MM-DD, local). Defaults to today.",
    },
    {
        "name": "days",
        "schema": {"type": "integer", "minimum": 1},
        "description": (
            "Shorthand for the last N days ending at `end`. Ignored when `start` is provided."
        ),
    },
]


@docs(tags=["metrics"], summary="Get thermostat metrics summary")
@query_params(_DATE_RANGE_QUERY_PARAMS)
@response_schema(schemas.MetricsSummarySchema)
@routes.get("/api/metrics/thermostats/{entity_id:.*}/summary")
async def metrics_thermostat_summary(request: web.Request) -> web.Response:
    """2a — per-thermostat heating/cooling hours, cycles, duty cycle,
    completion + source breakdown for the date range."""
    conn = await get_conn(request)
    entity_id = request.match_info["entity_id"]
    start, end = await _parse_date_range(request)
    summary = await db.compute_thermostat_summary(conn, entity_id, start, end)
    return json_response(summary)


@docs(tags=["metrics"], summary="Get home metrics summary")
@query_params(_DATE_RANGE_QUERY_PARAMS)
@response_schema(schemas.MetricsSummarySchema)
@routes.get("/api/metrics/thermostats/summary")
async def metrics_home_summary(request: web.Request) -> web.Response:
    """2b — same shape as 2a, aggregated across all thermostats (home view)."""
    conn = await get_conn(request)
    start, end = await _parse_date_range(request)
    summary = await db.compute_thermostat_summary(conn, None, start, end)
    return json_response(summary)


@docs(tags=["metrics"], summary="Get thermostat metrics timeseries")
@query_params(
    [
        *_DATE_RANGE_QUERY_PARAMS,
        {
            "name": "metric",
            "schema": {"type": "string"},
            "description": "Which series to compute (e.g. hours, cycles, degree_minutes, time_to_target).",
        },
        {
            "name": "granularity",
            "schema": {"type": "string", "enum": ["day", "week", "month"]},
            "description": "Bucket size for the series (default day).",
        },
    ]
)
@response_schema(schemas.MetricsTimeseriesSchema)
@routes.get("/api/metrics/thermostats/{entity_id:.*}/timeseries")
async def metrics_thermostat_timeseries(request: web.Request) -> web.Response:
    """2c — generic per-chart data feed, switching on metric + granularity."""
    conn = await get_conn(request)
    entity_id = request.match_info["entity_id"]
    metric = request.rel_url.query.get("metric", "hours")
    granularity = request.rel_url.query.get("granularity", "day")
    start, end = await _parse_date_range(request, default_days=30 if granularity == "day" else 365)
    try:
        series = await db.compute_thermostat_timeseries(
            conn, entity_id, metric, granularity, start, end
        )
    except ValueError:
        return error("Invalid thermostat query parameters")
    return json_response(
        {
            "thermostat_entity_id": entity_id,
            "metric": metric,
            "granularity": granularity,
            "start": start,
            "end": end,
            "series": series,
        }
    )


@docs(tags=["metrics"], summary="Get room participation metrics")
@query_params(_DATE_RANGE_QUERY_PARAMS)
@response_schema(schemas.RoomMetricsResponseSchema)
@routes.get("/api/metrics/thermostats/{entity_id:.*}/rooms")
async def metrics_thermostat_rooms(request: web.Request) -> web.Response:
    """2d — per-room participation rate, heating/cooling time, time-to-target."""
    conn = await get_conn(request)
    entity_id = request.match_info["entity_id"]
    start, end = await _parse_date_range(request)
    rooms = await db.compute_room_metrics(conn, entity_id, start, end)
    return json_response(
        {
            "thermostat_entity_id": entity_id,
            "start": start,
            "end": end,
            "rooms": rooms,
        }
    )


@docs(tags=["metrics"], summary="Get cycles vs outside temperature data")
@query_params(_DATE_RANGE_QUERY_PARAMS)
@response_schema(schemas.CyclesVsOutsideTempResponseSchema)
@routes.get("/api/metrics/thermostats/{entity_id:.*}/cycles-vs-outside-temp")
async def metrics_cycles_vs_outside_temp(request: web.Request) -> web.Response:
    """2e — scatter data: each completed cycle as (outside_temp, duration)."""
    conn = await get_conn(request)
    entity_id = request.match_info["entity_id"]
    start, end = await _parse_date_range(request)
    points = await db.compute_cycles_vs_outside_temp(conn, entity_id, start, end)
    return json_response(
        {
            "thermostat_entity_id": entity_id,
            "start": start,
            "end": end,
            "points": points,
        }
    )


@docs(tags=["metrics"], summary="Get Eco Mode impact for a thermostat")
@query_params(_DATE_RANGE_QUERY_PARAMS)
@response_schema(schemas.EcoImpactResponseSchema)
@routes.get("/api/metrics/thermostats/{entity_id:.*}/eco-impact")
async def metrics_thermostat_eco_impact(request: web.Request) -> web.Response:
    """Eco Mode impact (Issue #404): cycles/runtime split by whether Eco relaxed
    a target, average drift applied, and a per-room breakdown, over a date
    range. Exposed as an MCP tool for post-rollout trend analysis."""
    conn = await get_conn(request)
    entity_id = request.match_info["entity_id"]
    start, end = await _parse_date_range(request)
    return json_response(await db.compute_eco_impact(conn, entity_id, start, end))


@docs(tags=["metrics"], summary="Get home-wide Eco Mode impact")
@query_params(_DATE_RANGE_QUERY_PARAMS)
@response_schema(schemas.EcoImpactResponseSchema)
@routes.get("/api/metrics/thermostats/eco-impact")
async def metrics_home_eco_impact(request: web.Request) -> web.Response:
    """Same shape as the per-thermostat Eco impact, aggregated across every
    thermostat (home view). Issue #404."""
    conn = await get_conn(request)
    start, end = await _parse_date_range(request)
    return json_response(await db.compute_eco_impact(conn, None, start, end))


@docs(tags=["metrics"], summary="Get overshoot histogram data")
@query_params(
    [
        *_DATE_RANGE_QUERY_PARAMS,
        {
            "name": "bin_size",
            "schema": {"type": "number"},
            "description": "Histogram bin width in degrees (default 1).",
        },
        {
            "name": "max_bins",
            "schema": {"type": "integer", "minimum": 1},
            "description": "Number of histogram bins (default 6).",
        },
    ]
)
@response_schema(schemas.OvershootHistogramSchema)
@routes.get("/api/metrics/thermostats/{entity_id:.*}/overshoot-histogram")
async def metrics_overshoot_histogram(request: web.Request) -> web.Response:
    """Phase 4l — histogram of how far past target each room participation
    actually went, computed from cycle_temp_samples."""
    conn = await get_conn(request)
    entity_id = request.match_info["entity_id"]
    start, end = await _parse_date_range(request)
    bin_size = float(request.rel_url.query.get("bin_size", "1"))
    max_bins = int(request.rel_url.query.get("max_bins", "6"))
    data = await db.compute_overshoot_histogram(
        conn, entity_id, start, end, bin_size=bin_size, max_bins=max_bins
    )
    return json_response(data)


@docs(tags=["metrics"], summary="Get HVAC hour heatmap")
@query_params(_DATE_RANGE_QUERY_PARAMS)
@response_schema(schemas.HourHeatmapSchema)
@routes.get("/api/metrics/thermostats/{entity_id:.*}/hour-heatmap")
async def metrics_thermostat_hour_heatmap(request: web.Request) -> web.Response:
    """2f — 7×24 grid of HVAC seconds (Mon..Sun × hour)."""
    conn = await get_conn(request)
    entity_id = request.match_info["entity_id"]
    start, end = await _parse_date_range(request)
    grid = await db.compute_hour_heatmap(conn, entity_id, start, end)
    return json_response(grid)


@docs(tags=["metrics"], summary="Get vent event timeline")
@query_params(_DATE_RANGE_QUERY_PARAMS)
@response_schema(schemas.VentTimelineResponseSchema)
@routes.get("/api/metrics/thermostats/{entity_id:.*}/vent-timeline")
async def metrics_thermostat_vent_timeline(request: web.Request) -> web.Response:
    """2g — cycle-boundary vent events for the range. UI must show the
    "boundary-only, not every vent movement" disclosure."""
    conn = await get_conn(request)
    entity_id = request.match_info["entity_id"]
    start, end = await _parse_date_range(request)
    events = await db.get_vent_events_in_range(conn, entity_id, start, end)
    return json_response(
        {
            "thermostat_entity_id": entity_id,
            "start": start,
            "end": end,
            "note": (
                "Cycle-boundary events only (opened_at_start, closed_reached_target, "
                "force_reopened_max_closed, reopened_min_runtime_hold, "
                "closed_overflow_hold, opened_overflow_hold). Mid-cycle vent "
                "movements are not currently tracked."
            ),
            "events": events,
        }
    )


@docs(tags=["metrics"], summary="Get live metrics for HA sensors")
@response_schema(schemas.MetricsLiveSchema)
@routes.get("/api/metrics/thermostats/{entity_id:.*}/live")
async def metrics_thermostat_live(request: web.Request) -> web.Response:
    """2h — today's running totals + current cycle info + current outside
    temperature, intended for HA sensor consumption."""
    conn = await get_conn(request)
    entity_id = request.match_info["entity_id"]

    today = tz.today_local().isoformat()
    summary = await db.compute_thermostat_summary(conn, entity_id, today, today)

    # Currently-running cycle (if any) for this thermostat.
    open_logs = await db.get_open_cycle_logs(conn, entity_id)
    current = None
    if open_logs:
        cl = open_logs[0]
        current = {
            "cycle_id": cl.id,
            "mode": cl.mode,
            "started_at": cl.started_at.replace(tzinfo=None).isoformat(),
            "thermostat_temp_at_start": cl.thermostat_temp_at_start,
            "setpoint_at_start": cl.setpoint_at_start,
            "outside_temp_at_start": cl.outside_temp_at_start,
        }

    # Current outside-temp reading via HAClient.get_numeric_state (°C → °F).
    outside_entity = await db.get_system_setting(conn, "outside_temperature_entity_id", "")
    current_outside_temp = None
    if outside_entity:
        try:
            current_outside_temp = request.app["ha"].get_numeric_state(outside_entity)
        except Exception:
            current_outside_temp = None

    return json_response(
        {
            "thermostat_entity_id": entity_id,
            "as_of": datetime.now(UTC).replace(tzinfo=None).isoformat(),
            "today": summary,
            "current_cycle": current,
            "outside_temp_entity_id": outside_entity or None,
            "current_outside_temp": current_outside_temp,
        }
    )


@docs(tags=["metrics"], summary="Export metrics as CSV")
@query_params(
    [
        *_DATE_RANGE_QUERY_PARAMS,
        {
            "name": "scope",
            "schema": {"type": "string", "enum": ["home", "thermostat"]},
            "description": "home (default) covers all thermostats; thermostat requires entity_id.",
        },
        {
            "name": "entity_id",
            "schema": {"type": "string"},
            "description": "Thermostat entity id, required when scope=thermostat.",
        },
    ]
)
@routes.get("/api/metrics/export.csv")
async def metrics_export_csv(request: web.Request) -> web.Response:
    """2i — CSV export of completed cycles in the range. scope=thermostat
    requires entity_id; scope=home (default) covers all thermostats."""
    import csv
    import io

    conn = await get_conn(request)
    unit = request.app["scheduler"].get_temperature_unit()
    unit_label = "°C" if unit == "C" else "°F"
    scope = request.rel_url.query.get("scope", "home")
    start, end = await _parse_date_range(request, default_days=30)
    if scope not in ("home", "thermostat"):
        return error("scope must be 'home' or 'thermostat'")
    thermostat_id = request.rel_url.query.get("entity_id")
    if scope == "thermostat" and not thermostat_id:
        return error("entity_id query param required when scope=thermostat")
    where, params = db.cycle_log_range_filter(
        thermostat_id if scope == "thermostat" else None, start, end
    )
    sql = f"""
        SELECT id, thermostat_entity_id, mode, started_at, ended_at, ended_reason,
               thermostat_temp_at_start, thermostat_temp_at_end,
               setpoint_at_start, setpoint_at_end,
               outside_temp_at_start, outside_temp_at_end
        FROM cycle_logs
        WHERE {where}
        ORDER BY started_at ASC
    """
    async with conn.execute(sql, params) as cur:
        rows = await cur.fetchall()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "cycle_id",
            "thermostat_entity_id",
            "mode",
            "started_at",
            "ended_at",
            "duration_seconds",
            "ended_reason",
            f"thermostat_temp_at_start ({unit_label})",
            f"thermostat_temp_at_end ({unit_label})",
            f"setpoint_at_start ({unit_label})",
            f"setpoint_at_end ({unit_label})",
            f"outside_temp_at_start ({unit_label})",
            f"outside_temp_at_end ({unit_label})",
        ]
    )
    for r in rows:
        duration: float | str
        try:
            duration = (
                datetime.fromisoformat(r["ended_at"]) - datetime.fromisoformat(r["started_at"])
            ).total_seconds()
        except (ValueError, TypeError):
            duration = ""
        writer.writerow(
            [
                r["id"],
                r["thermostat_entity_id"],
                r["mode"],
                r["started_at"],
                r["ended_at"],
                int(duration) if isinstance(duration, float) else duration,
                r["ended_reason"] or "",
                _from_f(r["thermostat_temp_at_start"], unit),
                _from_f(r["thermostat_temp_at_end"], unit),
                _from_f(r["setpoint_at_start"], unit),
                _from_f(r["setpoint_at_end"], unit),
                _from_f(r["outside_temp_at_start"], unit),
                _from_f(r["outside_temp_at_end"], unit),
            ]
        )
    return web.Response(
        body=buf.getvalue().encode("utf-8"),
        content_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="metrics_{start}_{end}.csv"'},
    )


# ---------------------------------------------------------------------------
# System enable / disable + developer mode
# ---------------------------------------------------------------------------


@docs(tags=["system"], summary="Get system enabled and dev mode status")
@response_schema(schemas.SystemStatusSchema)
@routes.get("/api/system/status")
async def system_status(request: web.Request) -> web.Response:
    scheduler = request.app["scheduler"]
    return json_response(
        {
            "enabled": scheduler.get_system_enabled(),
            "dev_mode": scheduler.get_dev_mode(),
            "mcp_enabled": scheduler.get_mcp_enabled(),
        }
    )


@docs(tags=["system"], summary="Enable or disable the system")
@request_schema(schemas.SystemStatusSchema)
@response_schema(schemas.SystemStatusSchema)
@routes.post("/api/system/enabled")
async def set_system_enabled(request: web.Request) -> web.Response:
    body = await request.json()
    if "enabled" not in body:
        return error("enabled field required")
    enabled = bool(body["enabled"])
    await request.app["scheduler"].set_system_enabled(enabled)
    state_str = "enabled" if enabled else "disabled"
    await emit(request, "info", "system", f"System {state_str} via API", {"enabled": enabled})
    return json_response({"enabled": enabled})


@docs(tags=["system"], summary="Enable or disable the HTTP MCP server")
@request_schema(schemas.SystemStatusSchema)
@response_schema(schemas.SystemStatusSchema)
@routes.post("/api/system/mcp")
async def set_mcp_enabled(request: web.Request) -> web.Response:
    body = await request.json()
    if "mcp_enabled" not in body:
        return error("mcp_enabled field required")
    enabled = bool(body["mcp_enabled"])
    await request.app["scheduler"].set_mcp_enabled(enabled)
    state_str = "enabled" if enabled else "disabled"
    await emit(
        request, "info", "system", f"MCP server {state_str} via API", {"mcp_enabled": enabled}
    )
    return json_response({"mcp_enabled": enabled})


@docs(tags=["system"], summary="Get dev mode status")
@response_schema(schemas.SystemStatusSchema)
@routes.get("/api/system/dev-mode")
async def get_dev_mode(request: web.Request) -> web.Response:
    return json_response({"dev_mode": request.app["scheduler"].get_dev_mode()})


@docs(tags=["system"], summary="Enable or disable dev mode")
@request_schema(schemas.SystemStatusSchema)
@response_schema(schemas.SystemStatusSchema)
@routes.post("/api/system/dev-mode")
async def set_dev_mode(request: web.Request) -> web.Response:
    body = await request.json()
    if "dev_mode" not in body:
        return error("dev_mode field required")
    enabled = bool(body["dev_mode"])
    await request.app["scheduler"].set_dev_mode(enabled)
    return json_response({"dev_mode": enabled})


# ---------------------------------------------------------------------------
# Backup / Restore
# ---------------------------------------------------------------------------


@docs(tags=["system"], summary="Download a database backup")
@routes.get("/api/backup")
async def backup_db(request: web.Request) -> web.Response:
    db_path: str = request.app["db_path"]
    if not os.path.exists(db_path):
        return error("Database file not found", 404)

    # Use sqlite3.backup() to produce a clean consistent snapshot that
    # includes any unflushed WAL-mode writes — serving the raw db file
    # directly can miss data still in the -wal file.
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
        tmp_path = tmp.name
    try:
        src = sqlite3.connect(db_path)
        dst = sqlite3.connect(tmp_path)
        src.backup(dst)
        src.close()
        dst.close()
        # Read the snapshot into the response body so the temp file can be
        # deleted immediately. Serving it via FileResponse would stream the file
        # after this handler returns, leaving the snapshot on disk forever — a
        # disk leak that accumulated a full DB copy per download (CWE-459 /
        # Issue #298).
        with open(tmp_path, "rb") as f:
            data = f.read()
        headers = {
            "Content-Disposition": 'attachment; filename="app.db"',
            "Content-Type": "application/octet-stream",
        }
        return web.Response(body=data, headers=headers)
    except Exception:
        log.exception("Backup failed")
        return error("Backup failed", 500)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


@docs(tags=["system"], summary="Restore a database from backup")
@response_schema(
    schemas.SuccessSchema
)  # Returns {"restored": True} but schemas.SuccessSchema (ok: True) is close enough
@routes.post("/api/restore")
async def restore_db(request: web.Request) -> web.Response:
    db_path: str = request.app["db_path"]

    reader = await request.multipart()
    field = await reader.next()
    if not isinstance(field, aiohttp.BodyPartReader) or field.name != "file":
        return error("Multipart field 'file' required")

    # Write upload to a temp file first so we can validate before overwriting
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
        tmp_path = tmp.name
        while True:
            chunk = await field.read_chunk(65536)
            if not chunk:
                break
            tmp.write(chunk)

    try:
        # Validate SQLite magic bytes
        file_size = os.path.getsize(tmp_path)
        with open(tmp_path, "rb") as f:
            magic = f.read(16)
        log.info("Restore: uploaded file size=%d magic=%r", file_size, magic[:16])
        if not magic.startswith(b"SQLite format 3\x00"):
            os.unlink(tmp_path)
            return error("Uploaded file is not a valid SQLite database")

        # Swap file then reload DB connection — APScheduler keeps running
        scheduler = request.app["scheduler"]
        log.info("Restore: moving %s → %s", tmp_path, db_path)
        shutil.move(tmp_path, db_path)
        log.info("Restore: file moved, reloading DB")
        await scheduler.reload_db()
        log.info("Restore: complete")

        await emit(request, "info", "system", "Database restored via UI upload")
        return json_response({"restored": True})
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        log.exception("Restore failed")
        return error("Restore failed", 500)
