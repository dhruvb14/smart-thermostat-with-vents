"""
aiohttp REST API routes.

All handlers are thin: validate input, call db helpers, return JSON.
The scheduler instance is attached to app['scheduler'].
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, time, timedelta
from typing import Any

from aiohttp import web

from .. import db
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


# ---------------------------------------------------------------------------
# Rooms
# ---------------------------------------------------------------------------


@routes.get("/api/rooms")
async def list_rooms(request: web.Request) -> web.Response:
    conn = await get_conn(request)
    rooms = await db.get_all_rooms(conn)
    return json_response([r.__dict__ for r in rooms])


@routes.post("/api/rooms")
async def create_room(request: web.Request) -> web.Response:
    body = await request.json()
    if not body.get("name") or not body.get("thermostat_entity_id"):
        return error("name and thermostat_entity_id required")
    room = Room.create(
        name=body["name"],
        thermostat_entity_id=body["thermostat_entity_id"],
        include_thermostat_sensor=body.get("include_thermostat_sensor", False),
        system_wide_temp=body.get("system_wide_temp"),
        presence_holdover_hours=body.get("presence_holdover_hours", 2.0),
        notes=body.get("notes", ""),
    )
    conn = await get_conn(request)
    await db.upsert_room(conn, room)
    await refresh(request)
    await emit(request, "info", "api", f"Room created: {room.name}", {"room_id": room.id})
    return json_response(room.__dict__, status=201)


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


@routes.put("/api/rooms/{room_id}")
async def update_room(request: web.Request) -> web.Response:
    conn = await get_conn(request)
    room = await db.get_room(conn, request.match_info["room_id"])
    if not room:
        return error("Room not found", 404)
    body = await request.json()
    for field in (
        "name",
        "thermostat_entity_id",
        "include_thermostat_sensor",
        "system_wide_temp",
        "presence_holdover_hours",
        "notes",
        "temp_offset",
    ):
        if field in body:
            setattr(room, field, body[field])
    await db.upsert_room(conn, room)
    await refresh(request)
    await emit(request, "info", "api", f"Room updated: {room.name}", {"room_id": room.id})
    return json_response(room.__dict__)


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


@routes.get("/api/rooms/{room_id}/sensors")
async def list_sensors(request: web.Request) -> web.Response:
    conn = await get_conn(request)
    sensors = await db.get_room_sensors(conn, request.match_info["room_id"])
    return json_response([s.__dict__ for s in sensors])


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


@routes.get("/api/rooms/{room_id}/vents")
async def list_vents(request: web.Request) -> web.Response:
    conn = await get_conn(request)
    vents = await db.get_room_vents(conn, request.match_info["room_id"])
    return json_response([v.__dict__ for v in vents])


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


@routes.delete("/api/rooms/{room_id}/vents/{entity_id:.*}")
async def remove_vent(request: web.Request) -> web.Response:
    conn = await get_conn(request)
    await db.remove_room_vent(conn, request.match_info["room_id"], request.match_info["entity_id"])
    return json_response({"deleted": True})


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
    except Exception as exc:
        await emit(
            request,
            "warning",
            "api",
            f"Vent test {direction} failed for {entity_id} ({method}): {exc}",
            {
                "entity_id": entity_id,
                "control_method": method,
                "direction": direction,
                "error": str(exc),
            },
        )
        return error(str(exc), status=400)

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


@routes.get("/api/rooms/{room_id}/presence")
async def list_presence(request: web.Request) -> web.Response:
    conn = await get_conn(request)
    ps = await db.get_room_presence_sensors(conn, request.match_info["room_id"])
    return json_response([p.__dict__ for p in ps])


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
    }


@routes.get("/api/rooms/{room_id}/schedules")
async def list_schedules(request: web.Request) -> web.Response:
    conn = await get_conn(request)
    schedules = await db.get_schedules_for_room(conn, request.match_info["room_id"])
    return json_response([_schedule_to_dict(s) for s in schedules])


@routes.post("/api/rooms/{room_id}/schedules")
async def create_schedule(request: web.Request) -> web.Response:
    body = await request.json()
    required = ("days_of_week", "start_time", "end_time", "target_temp")
    if not all(k in body for k in required):
        return error(f"Required fields: {required}")
    try:
        s = Schedule.create(
            room_id=request.match_info["room_id"],
            days_of_week=body["days_of_week"],
            start_time=time.fromisoformat(body["start_time"]),
            end_time=time.fromisoformat(body["end_time"]),
            target_temp=float(body["target_temp"]),
        )
    except (ValueError, TypeError) as exc:
        return error(str(exc))
    conn = await get_conn(request)
    # Check for overlapping schedules
    existing = await db.get_schedules_for_room(conn, request.match_info["room_id"])
    for e in existing:
        if room_manager.schedules_overlap(s, e):
            days_str = ", ".join(room_manager.DAYS_SHORT[d] for d in sorted(e.days_of_week))
            return error(
                f"Overlaps with existing block on {days_str} "
                f"{e.start_time.strftime('%H:%M')}–{e.end_time.strftime('%H:%M')}"
            )
    await db.upsert_schedule(conn, s)
    return json_response(_schedule_to_dict(s), status=201)


@routes.put("/api/rooms/{room_id}/schedules/{schedule_id}")
async def update_schedule(request: web.Request) -> web.Response:
    conn = await get_conn(request)
    schedules = await db.get_schedules_for_room(conn, request.match_info["room_id"])
    sid = request.match_info["schedule_id"]
    schedule = next((s for s in schedules if s.id == sid), None)
    if not schedule:
        return error("Schedule not found", 404)
    body = await request.json()
    if "days_of_week" in body:
        schedule.days_of_week = body["days_of_week"]
    if "start_time" in body:
        schedule.start_time = time.fromisoformat(body["start_time"])
    if "end_time" in body:
        schedule.end_time = time.fromisoformat(body["end_time"])
    if "target_temp" in body:
        schedule.target_temp = float(body["target_temp"])
    # Check for overlapping schedules (excluding self)
    for e in schedules:
        if e.id == schedule.id:
            continue
        if room_manager.schedules_overlap(schedule, e):
            days_str = ", ".join(room_manager.DAYS_SHORT[d] for d in sorted(e.days_of_week))
            return error(
                f"Overlaps with existing block on {days_str} "
                f"{e.start_time.strftime('%H:%M')}–{e.end_time.strftime('%H:%M')}"
            )
    await db.upsert_schedule(conn, schedule)
    return json_response(_schedule_to_dict(schedule))


@routes.delete("/api/rooms/{room_id}/schedules/{schedule_id}")
async def delete_schedule(request: web.Request) -> web.Response:
    conn = await get_conn(request)
    await db.delete_schedule(conn, request.match_info["schedule_id"])
    return json_response({"deleted": True})


# ---------------------------------------------------------------------------
# Thermostat configs
# ---------------------------------------------------------------------------


@routes.get("/api/thermostats")
async def list_thermostats(request: web.Request) -> web.Response:
    conn = await get_conn(request)
    configs = await db.get_all_thermostat_configs(conn)
    return json_response([tc.__dict__ for tc in configs])


@routes.post("/api/thermostats")
async def create_thermostat(request: web.Request) -> web.Response:
    body = await request.json()
    if not body.get("thermostat_entity_id"):
        return error("thermostat_entity_id required")
    conn = await get_conn(request)
    # Load defaults then apply body fields
    tc = await db.get_thermostat_config(conn, body["thermostat_entity_id"])
    for field in (
        "name",
        "default_temp",
        "min_setpoint",
        "max_setpoint",
        "deadband",
        "max_vent_closed_min",
        "min_open_vents",
        "overshoot_delta",
        "cycle_timeout_hours",
        "reconciliation_interval_min",
    ):
        if field in body:
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


@routes.put("/api/thermostats/{entity_id:.*}")
async def upsert_thermostat(request: web.Request) -> web.Response:
    entity_id = request.match_info["entity_id"]
    conn = await get_conn(request)
    tc = await db.get_thermostat_config(conn, entity_id)
    body = await request.json()
    for field in (
        "name",
        "default_temp",
        "min_setpoint",
        "max_setpoint",
        "deadband",
        "max_vent_closed_min",
        "min_open_vents",
        "overshoot_delta",
        "cycle_timeout_hours",
        "reconciliation_interval_min",
    ):
        if field in body:
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


@routes.post("/api/rooms/{room_id}/override")
async def set_override(request: web.Request) -> web.Response:
    body = await request.json()
    if "target_temp" not in body:
        return error("target_temp required")
    duration_hours = float(body.get("duration_hours", 2.0))
    override = RoomOverride(
        room_id=request.match_info["room_id"],
        target_temp=float(body["target_temp"]),
        expires_at=datetime.utcnow() + timedelta(hours=duration_hours),
    )
    conn = await get_conn(request)
    await db.set_room_override(conn, override)
    return json_response(
        {
            "room_id": override.room_id,
            "target_temp": override.target_temp,
            "expires_at": override.expires_at.isoformat(),
        }
    )


@routes.delete("/api/rooms/{room_id}/override")
async def clear_override(request: web.Request) -> web.Response:
    conn = await get_conn(request)
    await db.clear_room_override(conn, request.match_info["room_id"])
    return json_response({"cleared": True})


# ---------------------------------------------------------------------------
# Room active-status (for UI cards)
# ---------------------------------------------------------------------------


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
    now = datetime.now()

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


@routes.get("/api/status")
async def status(request: web.Request) -> web.Response:
    zones = request.app["scheduler"].get_all_zone_statuses()
    return json_response(zones)


@routes.post("/api/ha/states")
async def ha_states(request: web.Request) -> web.Response:
    """Return live state for a list of entity IDs from the HA state cache."""
    body = await request.json()
    entity_ids: list[str] = body.get("entity_ids", [])
    ha = request.app["ha"]
    result = {}
    for eid in entity_ids:
        state = ha.get_state(eid)
        if state is None:
            result[eid] = None
            continue
        raw = state.get("state")
        attrs = state.get("attributes", {})
        unit = attrs.get("unit_of_measurement", "")
        # Convert °C → °F for numeric states
        numeric = None
        try:
            val = float(raw)
            if unit == "°C":
                val = val * 9 / 5 + 32
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


@routes.get("/api/ha/entities")
async def ha_entities(request: web.Request) -> web.Response:
    domain = request.rel_url.query.get("domain")
    has_attribute = request.rel_url.query.get("has_attribute")  # e.g. "hvac_action"
    exclude_icon = request.rel_url.query.get("exclude_icon")  # e.g. "mdi:door-open"
    ha = request.app["ha"]
    if domain:
        entities = await ha.get_entities_by_domain(domain)
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


@routes.get("/api/logs")
async def get_logs(request: web.Request) -> web.Response:
    conn = await get_conn(request)
    limit = int(request.rel_url.query.get("limit", 50))
    offset = int(request.rel_url.query.get("offset", 0))
    since = request.rel_url.query.get("since") or None
    until = request.rel_url.query.get("until") or None
    logs = await db.get_cycle_logs(conn, limit=limit, offset=offset, since=since, until=until)
    return json_response(
        [
            {
                "id": log_entry.id,
                "thermostat_entity_id": log_entry.thermostat_entity_id,
                "started_at": log_entry.started_at.isoformat(),
                "ended_at": log_entry.ended_at.isoformat() if log_entry.ended_at else None,
                "mode": log_entry.mode,
                "rooms": json.loads(log_entry.rooms_json),
            }
            for log_entry in logs
        ]
    )


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


@routes.delete("/api/logs/events")
async def clear_event_logs(request: web.Request) -> web.Response:
    conn = await get_conn(request)
    await db.clear_event_logs(conn)
    await emit(request, "info", "system", "Event logs cleared by user")
    return json_response({"cleared": True})


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
# System enable / disable + developer mode
# ---------------------------------------------------------------------------


@routes.get("/api/system/status")
async def system_status(request: web.Request) -> web.Response:
    scheduler = request.app["scheduler"]
    return json_response(
        {
            "enabled": scheduler.get_system_enabled(),
            "dev_mode": scheduler.get_dev_mode(),
        }
    )


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


@routes.get("/api/system/dev-mode")
async def get_dev_mode(request: web.Request) -> web.Response:
    return json_response({"dev_mode": request.app["scheduler"].get_dev_mode()})


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
        headers = {
            "Content-Disposition": 'attachment; filename="flair.db"',
            "Content-Type": "application/octet-stream",
        }
        return web.FileResponse(tmp_path, headers=headers)
    except Exception as exc:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        return error(f"Backup failed: {exc}", 500)


@routes.post("/api/restore")
async def restore_db(request: web.Request) -> web.Response:
    db_path: str = request.app["db_path"]

    reader = await request.multipart()
    field = await reader.next()
    if field is None or field.name != "file":
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
    except Exception as exc:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        log.exception("Restore failed")
        return error(f"Restore failed: {exc}", 500)
