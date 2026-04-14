"""
Vent controller: open/close Flair cover entities with safety enforcement.

Safety rules enforced here:
- min_open_vents: never drop total open vent count below this threshold
- max_vent_closed_min: reopen a vent that has been closed too long (0 = disabled)
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

import aiosqlite

from ..ha_client import HAClient
from ..models import RoomCycleState, ThermostatConfig, RoomVent
from ..event_logger import EventLogger
from .. import db

log = logging.getLogger(__name__)


class VentController:
    def __init__(self, ha: HAClient, event_logger: Optional[EventLogger] = None) -> None:
        self._ha = ha
        self._logger = event_logger

    # ------------------------------------------------------------------
    # Open / close with safety guards
    # ------------------------------------------------------------------

    async def open_room_vents(self, vents: list[RoomVent]) -> None:
        for vent in vents:
            state = self._ha.get_state(vent.entity_id)
            if state is None:
                log.error("Vent entity %s not found in HA", vent.entity_id)
                continue
            if state.get("state") != "open":
                await self._ha.open_cover(vent.entity_id)
                if self._logger:
                    await self._logger.log(
                        "info", "engine",
                        f"Opened vent {vent.entity_id}",
                        {"entity_id": vent.entity_id},
                    )

    async def close_room_vents(
        self,
        vents: list[RoomVent],
        all_zone_vents: list[RoomVent],
        tc: ThermostatConfig,
        cycle_states: dict[str, RoomCycleState],
        now: Optional[datetime] = None,
    ) -> bool:
        """
        Attempt to close all vents for a room.
        Returns True if vents were closed, False if deferred due to min_open_vents.
        """
        if now is None:
            now = datetime.utcnow()

        if tc.min_open_vents > 0:
            currently_open = self._count_open_vents(all_zone_vents)
            would_close = len([v for v in vents if self._is_open(v.entity_id)])
            if currently_open - would_close < tc.min_open_vents:
                log.warning(
                    "Deferring vent close — would drop to %d open (min=%d)",
                    currently_open - would_close,
                    tc.min_open_vents,
                )
                if self._logger:
                    vent_ids = [v.entity_id for v in vents]
                    await self._logger.log(
                        "warning", "engine",
                        f"Vent close deferred — would drop to {currently_open - would_close} open "
                        f"(min_open_vents={tc.min_open_vents}): {vent_ids}",
                        {"entity_ids": vent_ids, "currently_open": currently_open,
                         "would_close": would_close, "min_open_vents": tc.min_open_vents},
                    )
                return False

        for vent in vents:
            if self._is_open(vent.entity_id):
                await self._ha.close_cover(vent.entity_id)
                if self._logger:
                    await self._logger.log(
                        "info", "engine",
                        f"Closed vent {vent.entity_id} (room at target)",
                        {"entity_id": vent.entity_id},
                    )

        return True

    async def check_max_closed_duration(
        self,
        conn: aiosqlite.Connection,
        room_vents: dict[str, list[RoomVent]],  # room_id → vents
        cycle_states: dict[str, RoomCycleState],
        tc: ThermostatConfig,
        now: Optional[datetime] = None,
    ) -> list[str]:
        """
        Reopen vents that have been closed longer than max_vent_closed_min.
        Returns list of room_ids that had vents reopened.
        """
        if tc.max_vent_closed_min == 0:
            return []
        if now is None:
            now = datetime.utcnow()

        reopened_rooms: list[str] = []
        limit = timedelta(minutes=tc.max_vent_closed_min)

        for room_id, states in cycle_states.items():
            if states.vent_closed_at is None:
                continue
            if now - states.vent_closed_at >= limit:
                vents = room_vents.get(room_id, [])
                if vents:
                    log.warning(
                        "Room %s vents closed > %d min — reopening for safety",
                        room_id, tc.max_vent_closed_min,
                    )
                    if self._logger:
                        await self._logger.log(
                            "warning", "engine",
                            f"Force-reopening vents for room {room_id} — closed > {tc.max_vent_closed_min}min",
                            {"room_id": room_id, "max_vent_closed_min": tc.max_vent_closed_min},
                        )
                    await self.open_room_vents(vents)
                    # Reset vent_closed_at so the timer restarts
                    states.vent_closed_at = None
                    await db.upsert_room_cycle_state(conn, states)
                    reopened_rooms.append(room_id)
        return reopened_rooms

    async def close_all_zone_vents(self, all_zone_vents: list[RoomVent]) -> None:
        """Emergency: close every vent in a zone (thermostat unavailable etc.)."""
        for vent in all_zone_vents:
            try:
                await self._ha.close_cover(vent.entity_id)
                if self._logger:
                    await self._logger.log(
                        "warning", "engine",
                        f"Emergency closed vent {vent.entity_id}",
                        {"entity_id": vent.entity_id},
                    )
            except Exception as exc:
                log.error("Failed to close vent %s: %s", vent.entity_id, exc)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _is_open(self, entity_id: str) -> bool:
        state = self._ha.get_state(entity_id)
        if state is None:
            return False
        return state.get("state") == "open"

    def _count_open_vents(self, vents: list[RoomVent]) -> int:
        return sum(1 for v in vents if self._is_open(v.entity_id))

    def get_vent_states(self, vents: list[RoomVent]) -> dict[str, str]:
        """Return {entity_id: 'open'|'closed'|'unknown'} for each vent."""
        result = {}
        for v in vents:
            state = self._ha.get_state(v.entity_id)
            if state is None:
                result[v.entity_id] = "unknown"
            else:
                result[v.entity_id] = state.get("state", "unknown")
        return result
