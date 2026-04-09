"""
Central scheduler.

- Runs a 60-second tick for every thermostat zone's CycleEngine
- Listens to HA state changes and triggers targeted ticks
- Manages the mapping of thermostats → CycleEngine instances
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
from . import db

log = logging.getLogger(__name__)

BroadcastFn = Callable[[str, dict], Coroutine]


class Scheduler:
    def __init__(
        self,
        ha: HAClient,
        db_path: str,
        broadcast: Optional[BroadcastFn] = None,
    ) -> None:
        self._ha = ha
        self._db_path = db_path
        self._broadcast = broadcast
        self._vent_ctrl = VentController(ha)
        self._engines: dict[str, CycleEngine] = {}  # thermostat_entity_id → engine
        self._apscheduler = AsyncIOScheduler()
        self._db_conn: Optional[aiosqlite.Connection] = None

    async def start(self) -> None:
        self._db_conn = await aiosqlite.connect(self._db_path)
        self._db_conn.row_factory = aiosqlite.Row
        await db.init_db(self._db_conn)
        await self._sync_engines()

        # Subscribe to ALL state changes to detect presence + HVAC mode changes
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
        log.info("Scheduler started")

    async def stop(self) -> None:
        self._apscheduler.shutdown(wait=False)
        if self._db_conn:
            await self._db_conn.close()
        log.info("Scheduler stopped")

    async def get_db(self) -> aiosqlite.Connection:
        """Return the shared DB connection (for use by API routes)."""
        return self._db_conn

    async def refresh_engines(self) -> None:
        """Re-sync engines after room/thermostat config changes."""
        await self._sync_engines()

    # ------------------------------------------------------------------
    # Engine management
    # ------------------------------------------------------------------

    async def _sync_engines(self) -> None:
        """Create/remove CycleEngine instances to match configured rooms."""
        rooms = await db.get_all_rooms(self._db_conn)
        thermostat_ids = {r.thermostat_entity_id for r in rooms}

        # Add new engines
        for tid in thermostat_ids:
            if tid not in self._engines:
                engine = CycleEngine(
                    thermostat_entity_id=tid,
                    ha=self._ha,
                    vent_ctrl=self._vent_ctrl,
                    broadcast=self._broadcast,
                )
                self._engines[tid] = engine
                log.info("CycleEngine created for thermostat %s", tid)

        # Remove stale engines (thermostat no longer has any rooms)
        for tid in list(self._engines):
            if tid not in thermostat_ids:
                del self._engines[tid]
                log.info("CycleEngine removed for thermostat %s", tid)

    # ------------------------------------------------------------------
    # Tick
    # ------------------------------------------------------------------

    async def _tick_all(self) -> None:
        await self._sync_engines()
        tasks = [
            self._tick_engine(tid, engine)
            for tid, engine in self._engines.items()
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _tick_engine(self, tid: str, engine: CycleEngine) -> None:
        # Load sensor IDs for all rooms tied to this thermostat
        rooms = await db.get_rooms_for_thermostat(self._db_conn, tid)
        await engine.load_room_sensors(self._db_conn, [r.id for r in rooms])
        try:
            await engine.tick(self._db_conn)
        except Exception as exc:
            log.exception("Tick error for thermostat %s: %s", tid, exc)

    # ------------------------------------------------------------------
    # State change handler
    # ------------------------------------------------------------------

    async def _on_state_change(self, entity_id: str, new_state: dict) -> None:
        """Dispatch HA state changes to relevant engines."""
        # Check presence sensors
        if entity_id.startswith("binary_sensor.") and new_state.get("state") == "on":
            await self._handle_presence_event(entity_id)

        # If thermostat mode changed mid-cycle, trigger a tick
        if entity_id.startswith("climate.") and entity_id in self._engines:
            await self._tick_engine(entity_id, self._engines[entity_id])

    async def _handle_presence_event(self, presence_entity_id: str) -> None:
        """Look up which room(s) this sensor belongs to and notify their engines."""
        rooms = await db.get_all_rooms(self._db_conn)
        for room in rooms:
            presence_sensors = await db.get_room_presence_sensors(self._db_conn, room.id)
            for ps in presence_sensors:
                if ps.entity_id == presence_entity_id:
                    engine = self._engines.get(room.thermostat_entity_id)
                    if engine:
                        await engine.handle_presence(self._db_conn, room)
                        # Immediately trigger a tick so the room is added to the cycle
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
