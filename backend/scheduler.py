"""
Central scheduler.

- Runs a 60-second tick for every thermostat zone's CycleEngine
- Listens to HA state changes and triggers targeted ticks
- Manages the mapping of thermostats → CycleEngine instances
- Owns the system_enabled flag (persisted to DB)
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Callable, Coroutine, Optional

import aiosqlite
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .ha_client import HAClient
from .engine.cycle_engine import CycleEngine
from .engine.vent_controller import VentController
from .event_logger import EventLogger
from . import db

log = logging.getLogger(__name__)

BroadcastFn = Callable[[str, dict], Coroutine]


class Scheduler:
    def __init__(
        self,
        ha: HAClient,
        db_path: str,
        broadcast: Optional[BroadcastFn] = None,
        event_logger: Optional[EventLogger] = None,
    ) -> None:
        self._ha = ha
        self._db_path = db_path
        self._broadcast = broadcast
        self._event_logger = event_logger
        self._vent_ctrl: Optional[VentController] = None
        self._engines: dict[str, CycleEngine] = {}
        self._apscheduler = AsyncIOScheduler()
        self._db_conn: Optional[aiosqlite.Connection] = None
        self._system_enabled: bool = True

    # ------------------------------------------------------------------
    # Startup / shutdown
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._db_conn = await aiosqlite.connect(self._db_path)
        self._db_conn.row_factory = aiosqlite.Row
        await db.init_db(self._db_conn)

        # Load persisted system_enabled flag
        val = await db.get_system_setting(self._db_conn, "system_enabled", "1")
        self._system_enabled = val == "1"

        # Wire logger's DB connection
        if self._event_logger:
            self._event_logger.set_conn(self._db_conn)
            await self._event_logger.log(
                "info", "system",
                f"Scheduler started (system {'enabled' if self._system_enabled else 'disabled'})",
            )

        self._vent_ctrl = VentController(self._ha, event_logger=self._event_logger)

        await self._sync_engines()

        # Subscribe to ALL HA state changes
        self._ha.subscribe_all(self._on_state_change)

        self._apscheduler.add_job(
            self._tick_all,
            "interval",
            seconds=60,
            id="main_tick",
            max_instances=1,
            coalesce=True,
        )
        self._apscheduler.start()
        log.info("Scheduler started (system_enabled=%s)", self._system_enabled)

    async def stop(self) -> None:
        self._apscheduler.shutdown(wait=False)
        if self._db_conn:
            await self._db_conn.close()
        log.info("Scheduler stopped")

    async def get_db(self) -> aiosqlite.Connection:
        return self._db_conn

    # ------------------------------------------------------------------
    # System enable / disable
    # ------------------------------------------------------------------

    def get_system_enabled(self) -> bool:
        return self._system_enabled

    async def set_system_enabled(self, enabled: bool) -> None:
        self._system_enabled = enabled
        await db.set_system_setting(self._db_conn, "system_enabled", "1" if enabled else "0")
        log.info("System %s", "enabled" if enabled else "disabled")
        if self._broadcast:
            await self._broadcast("system_enabled_changed", {"enabled": enabled})

    # ------------------------------------------------------------------
    # Engine management
    # ------------------------------------------------------------------

    async def refresh_engines(self) -> None:
        await self._sync_engines()

    async def _sync_engines(self) -> None:
        rooms = await db.get_all_rooms(self._db_conn)
        thermostat_ids = {r.thermostat_entity_id for r in rooms}

        for tid in thermostat_ids:
            if tid not in self._engines:
                engine = CycleEngine(
                    thermostat_entity_id=tid,
                    ha=self._ha,
                    vent_ctrl=self._vent_ctrl,
                    broadcast=self._broadcast,
                    event_logger=self._event_logger,
                    get_enabled=self.get_system_enabled,
                )
                self._engines[tid] = engine
                log.info("CycleEngine created for %s", tid)

        for tid in list(self._engines):
            if tid not in thermostat_ids:
                del self._engines[tid]
                log.info("CycleEngine removed for %s", tid)

    # ------------------------------------------------------------------
    # Tick
    # ------------------------------------------------------------------

    async def _tick_all(self) -> None:
        await self._sync_engines()
        tasks = [self._tick_engine(tid, eng) for tid, eng in self._engines.items()]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _tick_engine(self, tid: str, engine: CycleEngine) -> None:
        rooms = await db.get_rooms_for_thermostat(self._db_conn, tid)
        await engine.load_room_sensors(self._db_conn, [r.id for r in rooms])
        try:
            await engine.tick(self._db_conn)
        except Exception as exc:
            log.exception("Tick error for %s: %s", tid, exc)

    # ------------------------------------------------------------------
    # HA state change dispatch
    # ------------------------------------------------------------------

    async def _on_state_change(self, entity_id: str, new_state: dict) -> None:
        if entity_id.startswith("binary_sensor.") and new_state.get("state") == "on":
            await self._handle_presence_event(entity_id)

        if entity_id.startswith("climate.") and entity_id in self._engines:
            await self._tick_engine(entity_id, self._engines[entity_id])

    async def _handle_presence_event(self, presence_entity_id: str) -> None:
        rooms = await db.get_all_rooms(self._db_conn)
        for room in rooms:
            presence_sensors = await db.get_room_presence_sensors(self._db_conn, room.id)
            for ps in presence_sensors:
                if ps.entity_id == presence_entity_id:
                    if self._event_logger:
                        await self._event_logger.log(
                            "info", "presence",
                            f"Presence detected in {room.name} via {presence_entity_id}",
                            {"room_id": room.id, "sensor": presence_entity_id},
                        )
                    engine = self._engines.get(room.thermostat_entity_id)
                    if engine:
                        await engine.handle_presence(self._db_conn, room)
                        await self._tick_engine(room.thermostat_entity_id, engine)

    # ------------------------------------------------------------------
    # Status helpers for API
    # ------------------------------------------------------------------

    def get_all_zone_statuses(self) -> list[dict]:
        return [
            {
                "thermostat_entity_id": tid,
                "cycle_state": engine.cycle_state.value,
                **self._zone_status_dict(engine),
            }
            for tid, engine in self._engines.items()
        ]

    def _zone_status_dict(self, engine: CycleEngine) -> dict:
        s = engine.get_zone_status()
        return {
            "hvac_mode": s.hvac_mode,
            "hvac_action": s.hvac_action,
            "current_temp": s.current_temp,
            "setpoint": s.setpoint,
            "cycle_id": s.cycle_id,
            "cycle_started_at": s.cycle_started_at.isoformat() if s.cycle_started_at else None,
            "rooms": [
                {
                    "room_id": r.room_id,
                    "avg_temp": r.avg_temp,
                    "vent_states": r.vent_states,
                    "presence_active": r.presence_active,
                }
                for r in s.rooms
            ],
        }
