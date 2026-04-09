"""
aiohttp REST API routes.

All handlers are thin: validate input, call db helpers, return JSON.
The scheduler instance is attached to app['scheduler'].
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, time
from typing import Any

from aiohttp import web

from ..models import (
    Room,
    RoomOverride,
    RoomPresenceSensor,
    RoomSensor,
    RoomVent,
    Schedule,
    ThermostatConfig,
)
from .. import db

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
    return json_response({
        **room.__dict__,
        "sensors": [s.__dict__ for s in sensors],
        "vents": [v.__dict__ for v in vents],
        "presence_sensors": [p.__dict__ for p in presence],
        "schedules": [_schedule_to_dict(s) for s in schedules],
    })


@routes.put("/api/rooms/{room_id}")
async def update_room(request: web.Request) -> web.Response:
    conn = await get_conn(request)
    room = await db.get_room(conn, request.match_info["room_id"])
    if not room:
        return error("Room not found", 404)
    body = await request.json()
    for field in ("name", "thermostat_entity_id", "include_thermostat_sensor",
                  "system_wide_temp", "presence_holdover_hours", "notes"):
        if field in body:
            setattr(room, field, body[field])
    await db.upsert_room(conn, room)
    await refresh(request)
    return json_response(room.__dict__)


@routes.delete("/api/rooms/{room_id}")
async def delete_room(request: web.Request) -> web.Response:
    conn = await get_conn(request)
    room = await db.get_room(conn, request.match_info["room_id"])
    if not room:
        return error("Room not found", 404)
    await db.delete_room(conn, room.id)
    await refresh(request)
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
    conn = await get_conn(request)
    v = RoomVent.create(room_id=request.match_info["room_id"], entity_id=body["entity_id"])
    await db.add_room_vent(conn, v)
    return json_response(v.__dict__, status=201)


@routes.delete("/api/rooms/{room_id}/vents/{entity_id:.*}")
async def remove_vent(request: web.Request) -> web.Response:
    conn = await get_conn(request)
    await db.remove_room_vent(
        conn, request.match_info["room_id"], request.match_info["entity_id"]
    )
    return json_response({"deleted": True})


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


@routes.put("/api/thermostats/{entity_id:.*}")
async def upsert_thermostat(request: web.Request) -> web.Response:
    entity_id = request.match_info["entity_id"]
    conn = await get_conn(request)
    tc = await db.get_thermostat_config(conn, entity_id)
    body = await request.json()
    for field in (
        "min_setpoint", "max_setpoint", "deadband", "max_vent_closed_min",
        "min_open_vents", "overshoot_delta", "cycle_timeout_hours",
    ):
        if field in body:
            setattr(tc, field, body[field])
    await db.upsert_thermostat_config(conn, tc)
    return json_response(tc.__dict__)


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
    return json_response({
        "room_id": override.room_id,
        "target_temp": override.target_temp,
        "expires_at": override.expires_at.isoformat(),
    })


@routes.delete("/api/rooms/{room_id}/override")
async def clear_override(request: web.Request) -> web.Response:
    conn = await get_conn(request)
    await db.clear_room_override(conn, request.match_info["room_id"])
    return json_response({"cleared": True})


# ---------------------------------------------------------------------------
# System status + HA entity proxy
# ---------------------------------------------------------------------------

@routes.get("/api/status")
async def status(request: web.Request) -> web.Response:
    zones = request.app["scheduler"].get_all_zone_statuses()
    return json_response(zones)


@routes.get("/api/ha/entities")
async def ha_entities(request: web.Request) -> web.Response:
    domain = request.rel_url.query.get("domain")
    ha = request.app["ha"]
    if domain:
        entities = await ha.get_entities_by_domain(domain)
    else:
        entities = list(ha._state_cache.values())
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
    logs = await db.get_cycle_logs(conn, limit=limit)
    return json_response([
        {
            "id": l.id,
            "thermostat_entity_id": l.thermostat_entity_id,
            "started_at": l.started_at.isoformat(),
            "ended_at": l.ended_at.isoformat() if l.ended_at else None,
            "mode": l.mode,
            "rooms": json.loads(l.rooms_json),
        }
        for l in logs
    ])
