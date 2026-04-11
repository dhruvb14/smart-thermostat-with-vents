"""
HVAC Cycle Engine — one instance per thermostat zone.

State machine:
  IDLE → RUNNING → (all rooms at target) → TERMINATING → IDLE
  RUNNING → ABORTED (thermostat unavailable, mode change, timeout)
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from enum import Enum
from typing import Callable, Coroutine, Optional

import aiosqlite

from ..ha_client import HAClient
from ..models import (
    CycleLog,
    Room,
    RoomCycleState,
    RoomLiveState,
    RoomVent,
    ThermostatConfig,
    ZoneStatus,
)
from ..event_logger import EventLogger
from .. import db
from .room_manager import ActiveRoom, get_active_rooms, expire_holdovers
from .vent_controller import VentController

log = logging.getLogger(__name__)

# Callback type for broadcasting state changes to WebSocket clients
BroadcastFn = Callable[[str, dict], Coroutine]


class CycleState(Enum):
    IDLE = "idle"
    RUNNING = "running"
    TERMINATING = "terminating"


class CycleEngine:
    """
    Manages the HVAC cycle for one thermostat zone.
    Call `tick()` every 60s and whenever a relevant state change arrives.
    """

    def __init__(
        self,
        thermostat_entity_id: str,
        ha: HAClient,
        vent_ctrl: VentController,
        broadcast: Optional[BroadcastFn] = None,
        event_logger: Optional[EventLogger] = None,
        get_enabled: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.thermostat_entity_id = thermostat_entity_id
        self._ha = ha
        self._vent = vent_ctrl
        self._broadcast = broadcast
        self._logger = event_logger
        self._get_enabled = get_enabled

        self._state = CycleState.IDLE
        self._cycle_log: Optional[CycleLog] = None
        self._active_rooms: dict[str, ActiveRoom] = {}  # room_id → ActiveRoom
        self._room_cycle_states: dict[str, RoomCycleState] = {}  # room_id → state
        self._room_vents: dict[str, list[RoomVent]] = {}  # room_id → vents
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    @property
    def cycle_state(self) -> CycleState:
        return self._state

    @property
    def current_cycle_id(self) -> Optional[str]:
        return self._cycle_log.id if self._cycle_log else None

    async def tick(self, conn: aiosqlite.Connection) -> None:
        """Main entry point — called by scheduler every 60s or on state change."""
        async with self._lock:
            await self._do_tick(conn)

    async def handle_presence(
        self, conn: aiosqlite.Connection, room: Room
    ) -> None:
        """Called externally when a presence sensor fires for a room in this zone."""
        async with self._lock:
            await self._on_presence(conn, room)

    def get_zone_status(self) -> ZoneStatus:
        """Return a snapshot of the current zone status (no DB call needed)."""
        thermo_state = self._ha.get_state(self.thermostat_entity_id)
        if thermo_state:
            hvac_action = thermo_state.get("attributes", {}).get("hvac_action", "unknown")
            hvac_mode = thermo_state.get("state", "unknown")
            current_temp = thermo_state.get("attributes", {}).get("current_temperature")
            setpoint = thermo_state.get("attributes", {}).get("temperature")
        else:
            hvac_action = hvac_mode = "unknown"
            current_temp = setpoint = None

        room_states: list[RoomLiveState] = []
        for room_id, ar in self._active_rooms.items():
            vents = self._room_vents.get(room_id, [])
            room_states.append(
                RoomLiveState(
                    room_id=room_id,
                    avg_temp=self._get_avg_temp(ar.room),
                    sensor_count=len(ar.room.include_thermostat_sensor and [1] or []),
                    available_sensor_count=0,
                    vent_states=self._vent.get_vent_states(vents),
                    presence_active=ar.source == "presence",
                    holdover_expires_at=None,
                )
            )

        return ZoneStatus(
            thermostat_entity_id=self.thermostat_entity_id,
            hvac_mode=hvac_mode,
            hvac_action=hvac_action,
            current_temp=current_temp,
            setpoint=setpoint,
            cycle_id=self.current_cycle_id,
            cycle_started_at=self._cycle_log.started_at if self._cycle_log else None,
            rooms=room_states,
        )

    # ------------------------------------------------------------------
    # Tick logic
    # ------------------------------------------------------------------

    async def _do_tick(self, conn: aiosqlite.Connection) -> None:
        # Expire holdovers and overrides first
        expired = await expire_holdovers(conn)
        await db.clear_expired_overrides(conn)

        # Determine which rooms should be active now
        new_active = await get_active_rooms(conn, self.thermostat_entity_id)
        new_active_map = {ar.room.id: ar for ar in new_active}

        if not new_active_map:
            if self._state != CycleState.IDLE:
                await self._abort_cycle(conn, reason="no active rooms")
            return

        # Check thermostat availability (safety check always runs, even when disabled)
        thermo_state = self._ha.get_state(self.thermostat_entity_id)
        if thermo_state is None or thermo_state.get("state") == "unavailable":
            log.error("Thermostat %s unavailable — aborting", self.thermostat_entity_id)
            if self._logger:
                await self._logger.log(
                    "error", "engine",
                    f"Thermostat {self.thermostat_entity_id} unavailable — aborting cycle",
                    {"thermostat": self.thermostat_entity_id},
                )
            await self._abort_cycle(conn, reason="thermostat unavailable", safe_close=True)
            return

        # System disabled guard — skip all HA-mutating work
        if self._get_enabled is not None and not self._get_enabled():
            log.debug("System disabled — skipping tick for %s", self.thermostat_entity_id)
            return

        # Detect HVAC mode
        hvac_mode = self._read_hvac_mode()
        if hvac_mode == "unknown":
            log.warning("Thermostat %s mode unknown — skipping tick", self.thermostat_entity_id)
            return
        if hvac_mode == "off" and self._state == CycleState.IDLE:
            # No active cycle yet — rooms are defined but HVAC is off; wait
            return

        # If rooms changed, update and recompute setpoint
        rooms_changed = set(new_active_map) != set(self._active_rooms)
        if rooms_changed or self._state == CycleState.IDLE:
            await self._start_or_update_cycle(conn, new_active_map, hvac_mode)

        # Monitor rooms
        await self._monitor_rooms(conn, hvac_mode)

        # Check cycle timeout
        if self._cycle_log and self._state == CycleState.RUNNING:
            tc = await db.get_thermostat_config(conn, self.thermostat_entity_id)
            elapsed = datetime.utcnow() - self._cycle_log.started_at
            if elapsed > timedelta(hours=tc.cycle_timeout_hours):
                log.warning(
                    "Cycle %s timed out after %.1fh — terminating",
                    self._cycle_log.id, elapsed.total_seconds() / 3600,
                )
                if self._logger:
                    await self._logger.log(
                        "warning", "engine",
                        f"Cycle timed out after {elapsed.total_seconds()/3600:.1f}h for {self.thermostat_entity_id}",
                        {"thermostat": self.thermostat_entity_id, "cycle_id": self._cycle_log.id},
                    )
                await self._terminate_cycle(conn)

        await self._maybe_broadcast()

    async def _start_or_update_cycle(
        self,
        conn: aiosqlite.Connection,
        new_active_map: dict[str, ActiveRoom],
        hvac_mode: str,
    ) -> None:
        tc = await db.get_thermostat_config(conn, self.thermostat_entity_id)

        # Load vents for all active rooms
        self._room_vents = {}
        for room_id in new_active_map:
            self._room_vents[room_id] = await db.get_room_vents(conn, room_id)

        # Newly added rooms mid-cycle
        added = set(new_active_map) - set(self._active_rooms)
        removed = set(self._active_rooms) - set(new_active_map)

        self._active_rooms = new_active_map

        if self._state == CycleState.IDLE:
            # Start a fresh cycle
            rooms_snapshot = {
                ar.room.id: {"name": ar.room.name, "target": ar.target_temp, "source": ar.source}
                for ar in new_active_map.values()
            }
            self._cycle_log = CycleLog.create(
                thermostat_entity_id=self.thermostat_entity_id,
                mode=hvac_mode,
                rooms_json=json.dumps(rooms_snapshot),
            )
            await db.insert_cycle_log(conn, self._cycle_log)
            self._room_cycle_states = {}
            for ar in new_active_map.values():
                rcs = RoomCycleState(
                    cycle_id=self._cycle_log.id,
                    room_id=ar.room.id,
                    target_temp=ar.target_temp,
                )
                self._room_cycle_states[ar.room.id] = rcs
                await db.upsert_room_cycle_state(conn, rcs)
            self._state = CycleState.RUNNING
            room_names = [ar.room.name for ar in new_active_map.values()]
            log.info(
                "Cycle started for %s — mode=%s rooms=%s",
                self.thermostat_entity_id, hvac_mode, room_names,
            )
            if self._logger:
                await self._logger.log(
                    "info", "engine",
                    f"Cycle started for {self.thermostat_entity_id} — mode={hvac_mode}, rooms={room_names}",
                    {"thermostat": self.thermostat_entity_id, "mode": hvac_mode,
                     "cycle_id": self._cycle_log.id, "rooms": room_names},
                )
        else:
            # Update existing cycle (rooms changed mid-cycle)
            for room_id in added:
                ar = new_active_map[room_id]
                rcs = RoomCycleState(
                    cycle_id=self._cycle_log.id,
                    room_id=room_id,
                    target_temp=ar.target_temp,
                )
                self._room_cycle_states[room_id] = rcs
                await db.upsert_room_cycle_state(conn, rcs)
                # Open vents for newly added room
                vents = self._room_vents.get(room_id, [])
                await self._vent.open_room_vents(vents)
                log.info("Room %s added to running cycle", ar.room.name)
                if self._logger:
                    await self._logger.log(
                        "info", "engine",
                        f"Room {ar.room.name} added to running cycle (source: {ar.source})",
                        {"room_id": room_id, "room_name": ar.room.name, "source": ar.source,
                         "target_temp": ar.target_temp},
                    )

            for room_id in removed:
                # Close vents for removed rooms
                vents = self._room_vents.get(room_id, [])
                for v in vents:
                    try:
                        await self._ha.close_cover(v.entity_id)
                    except Exception as exc:
                        log.error("Error closing vent %s: %s", v.entity_id, exc)
                self._room_cycle_states.pop(room_id, None)
                log.info("Room %s removed from cycle (became idle)", room_id)
                if self._logger:
                    await self._logger.log(
                        "info", "engine",
                        f"Room {room_id} removed from cycle (became idle)",
                        {"room_id": room_id},
                    )

        # Open all active room vents
        for room_id, ar in self._active_rooms.items():
            rcs = self._room_cycle_states.get(room_id)
            if rcs and rcs.vent_closed_at is None:
                vents = self._room_vents.get(room_id, [])
                await self._vent.open_room_vents(vents)

        # Set thermostat setpoint
        await self._set_thermostat_setpoint(tc, hvac_mode)

    async def _monitor_rooms(self, conn: aiosqlite.Connection, hvac_mode: str) -> None:
        tc = await db.get_thermostat_config(conn, self.thermostat_entity_id)
        all_zone_vents = [v for vl in self._room_vents.values() for v in vl]

        # Safety: check max vent closed duration
        await self._vent.check_max_closed_duration(
            conn,
            self._room_vents,
            self._room_cycle_states,
            tc,
        )

        all_at_target = True
        for room_id, ar in self._active_rooms.items():
            rcs = self._room_cycle_states.get(room_id)
            if rcs is None:
                all_at_target = False
                continue

            avg = self._get_avg_temp(ar.room)
            if avg is None:
                # Room has no sensors (ventless/sensor-only room with no readings).
                # Skip it for target-check purposes — it cannot block cycle termination.
                log.warning("Room %s has no available sensors — skipping target check", ar.room.name)
                continue

            # Apply per-room offset: positive offset compensates for post-closure
            # drift (e.g. a room that overcools by 3°F gets offset=+3 so its vent
            # closes 3°F before the actual target, and it drifts to target).
            effective_avg = avg + ar.room.temp_offset
            at_target = _is_at_target(effective_avg, rcs.target_temp, hvac_mode, tc.deadband)

            if at_target and rcs.vent_closed_at is None:
                # Try to close the vent
                vents = self._room_vents.get(room_id, [])
                closed = await self._vent.close_room_vents(
                    vents, all_zone_vents, tc, self._room_cycle_states
                )
                if closed:
                    rcs.vent_closed_at = datetime.utcnow()
                    if rcs.reached_at is None:
                        rcs.reached_at = datetime.utcnow()
                    await db.upsert_room_cycle_state(conn, rcs)
                    offset_note = f", offset={ar.room.temp_offset:+.1f}°F" if ar.room.temp_offset != 0 else ""
                    log.info(
                        "Room %s hit target %.1f°F (avg=%.1f, effective=%.1f%s) — vent closed",
                        ar.room.name, rcs.target_temp, avg, effective_avg, offset_note,
                    )
                    if self._logger:
                        await self._logger.log(
                            "info", "engine",
                            f"Room {ar.room.name} reached target {rcs.target_temp}°F "
                            f"(avg={avg:.1f}°F, effective={effective_avg:.1f}°F{offset_note}) — vent closed",
                            {"room_id": room_id, "room_name": ar.room.name,
                             "target_temp": rcs.target_temp, "avg_temp": avg,
                             "effective_avg": effective_avg, "temp_offset": ar.room.temp_offset},
                        )
                else:
                    all_at_target = False  # deferred
            elif not at_target:
                all_at_target = False

        if all_at_target and self._active_rooms:
            await self._terminate_cycle(conn)

    async def _terminate_cycle(self, conn: aiosqlite.Connection) -> None:
        log.info("All rooms at target for %s — terminating cycle", self.thermostat_entity_id)
        self._state = CycleState.TERMINATING

        # Set thermostat setpoint to its own current ambient → HVAC shuts off
        thermo_state = self._ha.get_state(self.thermostat_entity_id)
        if thermo_state:
            ambient = thermo_state.get("attributes", {}).get("current_temperature")
            if ambient is not None:
                tc = await db.get_thermostat_config(conn, self.thermostat_entity_id)
                clamped = max(tc.min_setpoint, min(tc.max_setpoint, float(ambient)))
                try:
                    await self._ha.set_thermostat_temperature(
                        self.thermostat_entity_id, clamped
                    )
                    if self._logger:
                        await self._logger.log(
                            "info", "engine",
                            f"Cycle terminated for {self.thermostat_entity_id} — setpoint reset to ambient {clamped}°F",
                            {"thermostat": self.thermostat_entity_id, "setpoint": clamped,
                             "cycle_id": self._cycle_log.id if self._cycle_log else None},
                        )
                except Exception as exc:
                    log.error("Failed to set termination setpoint: %s", exc)

        if self._cycle_log:
            await db.close_cycle_log(conn, self._cycle_log.id, datetime.utcnow())

        self._state = CycleState.IDLE
        self._cycle_log = None
        self._active_rooms = {}
        self._room_cycle_states = {}
        self._room_vents = {}

    async def _abort_cycle(
        self,
        conn: aiosqlite.Connection,
        reason: str,
        safe_close: bool = False,
    ) -> None:
        log.warning("Aborting cycle for %s — %s", self.thermostat_entity_id, reason)
        if self._logger and self._state != CycleState.IDLE:
            await self._logger.log(
                "warning", "engine",
                f"Cycle aborted for {self.thermostat_entity_id} — {reason}",
                {"thermostat": self.thermostat_entity_id, "reason": reason,
                 "cycle_id": self._cycle_log.id if self._cycle_log else None},
            )
        if safe_close:
            all_vents = [v for vl in self._room_vents.values() for v in vl]
            await self._vent.close_all_zone_vents(all_vents)
        if self._cycle_log:
            await db.close_cycle_log(conn, self._cycle_log.id, datetime.utcnow())
        self._state = CycleState.IDLE
        self._cycle_log = None
        self._active_rooms = {}
        self._room_cycle_states = {}
        self._room_vents = {}

    # ------------------------------------------------------------------
    # Presence (called externally, then tick is triggered)
    # ------------------------------------------------------------------

    async def _on_presence(self, conn: aiosqlite.Connection, room: Room) -> None:
        from .room_manager import handle_presence_event
        newly_activated = await handle_presence_event(conn, room)
        if newly_activated and self._state == CycleState.RUNNING:
            # Room was just activated — add it mid-cycle on next tick
            # (tick() will pick it up via get_active_rooms)
            log.info(
                "Presence detected in %s — will be added to active cycle on next tick",
                room.name,
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _read_hvac_mode(self) -> str:
        """Return 'heating'|'cooling'|'off'|'unknown'."""
        state = self._ha.get_state(self.thermostat_entity_id)
        if state is None:
            return "unknown"
        # hvac_action is more reliable than hvac_mode
        action = state.get("attributes", {}).get("hvac_action", "")
        if action in ("heating", "cooling"):
            return action
        # Fall back to hvac_mode
        mode = state.get("state", "off")
        if mode in ("heat", "heat_cool"):
            return "heating"
        if mode in ("cool",):
            return "cooling"
        return "off"

    def _get_avg_temp(self, room: Room) -> Optional[float]:
        readings: list[float] = []

        # Re-query the cache for all sensors belonging to this room
        sensor_ids = self._sensor_ids_for_room.get(room.id, [])
        for eid in sensor_ids:
            val = self._ha.get_numeric_state(eid)
            if val is not None:
                readings.append(val)

        if room.include_thermostat_sensor:
            thermo = self._ha.get_state(room.thermostat_entity_id)
            if thermo:
                t = thermo.get("attributes", {}).get("current_temperature")
                if t is not None:
                    try:
                        readings.append(float(t))
                    except (ValueError, TypeError):
                        pass

        if not readings:
            return None
        return sum(readings) / len(readings)

    async def _set_thermostat_setpoint(
        self, tc: ThermostatConfig, hvac_mode: str
    ) -> None:
        targets = [ar.target_temp for ar in self._active_rooms.values()]
        if not targets:
            return
        if hvac_mode == "cooling":
            setpoint = min(targets) - tc.overshoot_delta
        else:
            setpoint = max(targets) + tc.overshoot_delta
        clamped = max(tc.min_setpoint, min(tc.max_setpoint, setpoint))
        try:
            await self._ha.set_thermostat_temperature(self.thermostat_entity_id, clamped)
            if self._logger:
                await self._logger.log(
                    "info", "engine",
                    f"Setpoint for {self.thermostat_entity_id} set to {clamped}°F (mode={hvac_mode})",
                    {"thermostat": self.thermostat_entity_id, "setpoint": clamped,
                     "hvac_mode": hvac_mode, "targets": targets},
                )
        except Exception as exc:
            log.error("Failed to set thermostat setpoint: %s", exc)

    async def _maybe_broadcast(self) -> None:
        if self._broadcast:
            try:
                status = self.get_zone_status()
                await self._broadcast(
                    "zone_status",
                    {
                        "thermostat_entity_id": status.thermostat_entity_id,
                        "hvac_mode": status.hvac_mode,
                        "hvac_action": status.hvac_action,
                        "current_temp": status.current_temp,
                        "setpoint": status.setpoint,
                        "cycle_id": status.cycle_id,
                        "cycle_state": self._state.value,
                    },
                )
            except Exception as exc:
                log.debug("Broadcast error: %s", exc)

    # We need to track sensor IDs per room across the cycle
    # Add a dict for this
    @property
    def _sensor_ids_for_room(self) -> dict[str, list[str]]:
        if not hasattr(self, "_sensor_map"):
            self._sensor_map: dict[str, list[str]] = {}
        return self._sensor_map

    async def load_room_sensors(
        self, conn: aiosqlite.Connection, room_ids: list[str]
    ) -> None:
        """Load and cache sensor entity IDs for a set of rooms."""
        if not hasattr(self, "_sensor_map"):
            self._sensor_map: dict[str, list[str]] = {}
        for room_id in room_ids:
            sensors = await db.get_room_sensors(conn, room_id)
            self._sensor_map[room_id] = [s.entity_id for s in sensors]


def _is_at_target(
    avg_temp: float, target_temp: float, hvac_mode: str, deadband: float
) -> bool:
    if hvac_mode == "cooling":
        return avg_temp <= target_temp + deadband
    else:  # heating
        return avg_temp >= target_temp - deadband
