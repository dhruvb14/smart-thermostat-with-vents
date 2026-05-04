"""
Vent controller: open/close Flair cover entities with safety enforcement.

Safety rules enforced here:
- min_open_vents: never drop total open vent count below this threshold
- max_vent_closed_min: opt-in safety valve — reopen a vent closed too long (0 = disabled, the default)
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import aiosqlite

from .. import db
from ..event_logger import EventLogger
from ..ha_client import HAClient
from ..models import RoomCycleState, RoomVent, ThermostatConfig

log = logging.getLogger(__name__)


class VentController:
    def __init__(self, ha: HAClient, event_logger: EventLogger | None = None) -> None:
        self._ha = ha
        self._logger = event_logger

    # ------------------------------------------------------------------
    # Dispatch: per-vent control method → HA service call
    # ------------------------------------------------------------------

    async def _invoke_open(self, vent: RoomVent) -> None:
        """Issue the configured 'open' action for one vent. Raises if HA fails."""
        method = vent.control_method
        if method == "set_position":
            await self._ha.set_cover_position(vent.entity_id, 100)
        elif method == "set_tilt_position":
            await self._ha.set_cover_tilt_position(vent.entity_id, 100)
        elif method == "toggle":
            await self._ha.toggle_cover(vent.entity_id)
        else:  # "open_close" (default)
            await self._ha.open_cover(vent.entity_id)

    async def _invoke_close(self, vent: RoomVent) -> None:
        """Issue the configured 'close' action for one vent. Raises if HA fails."""
        method = vent.control_method
        if method == "set_position":
            await self._ha.set_cover_position(vent.entity_id, 0)
        elif method == "set_tilt_position":
            await self._ha.set_cover_tilt_position(vent.entity_id, 0)
        elif method == "toggle":
            await self._ha.toggle_cover(vent.entity_id)
        else:  # "open_close"
            await self._ha.close_cover(vent.entity_id)

    async def _log_vent_error(self, vent: RoomVent, direction: str, exc: Exception) -> None:
        """Surface a vent service-call failure to the UI Live Feed.

        Vent failures never abort a cycle — they're logged here and the engine
        moves on to the next vent. Misconfigured control_method on a single
        vent must not stall the zone.
        """
        log.error(
            "Vent %s %s failed (method=%s): %s",
            vent.entity_id,
            direction,
            vent.control_method,
            exc,
        )
        if self._logger:
            await self._logger.log(
                "error",
                "engine",
                f"Failed to {direction} vent {vent.entity_id} using {vent.control_method}",
                {
                    "entity_id": vent.entity_id,
                    "control_method": vent.control_method,
                    "direction": direction,
                },
            )

    # ------------------------------------------------------------------
    # Open / close with safety guards
    # ------------------------------------------------------------------

    async def open_room_vents(self, vents: list[RoomVent]) -> None:
        for vent in vents:
            state = self._ha.get_state(vent.entity_id)
            if state is None:
                log.error("Vent entity %s not found in HA", vent.entity_id)
                continue
            if state.get("state") == "open":
                continue
            try:
                await self._invoke_open(vent)
            except Exception as exc:
                await self._log_vent_error(vent, "open", exc)
                continue
            if self._logger:
                await self._logger.log(
                    "info",
                    "engine",
                    f"Opened vent {vent.entity_id}",
                    {"entity_id": vent.entity_id, "control_method": vent.control_method},
                )

    async def close_room_vents(
        self,
        vents: list[RoomVent],
        all_zone_vents: list[RoomVent],
        tc: ThermostatConfig,
        cycle_states: dict[str, RoomCycleState],
        now: datetime | None = None,
    ) -> bool:
        """
        Attempt to close all vents for a room.
        Returns True if vents were closed, False if deferred due to min_open_vents.
        """
        if now is None:
            now = datetime.now(UTC)

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
                        "warning",
                        "engine",
                        f"Vent close deferred — would drop to {currently_open - would_close} open "
                        f"(min_open_vents={tc.min_open_vents}): {vent_ids}",
                        {
                            "entity_ids": vent_ids,
                            "currently_open": currently_open,
                            "would_close": would_close,
                            "min_open_vents": tc.min_open_vents,
                        },
                    )
                return False

        for vent in vents:
            if not self._is_open(vent.entity_id):
                continue
            try:
                await self._invoke_close(vent)
            except Exception as exc:
                await self._log_vent_error(vent, "close", exc)
                continue
            if self._logger:
                await self._logger.log(
                    "info",
                    "engine",
                    f"Closed vent {vent.entity_id} (room at target)",
                    {"entity_id": vent.entity_id, "control_method": vent.control_method},
                )

        return True

    async def check_max_closed_duration(
        self,
        conn: aiosqlite.Connection,
        room_vents: dict[str, list[RoomVent]],  # room_id → vents
        cycle_states: dict[str, RoomCycleState],
        tc: ThermostatConfig,
        now: datetime | None = None,
    ) -> list[str]:
        """
        Reopen vents that have been closed longer than max_vent_closed_min.
        Returns list of room_ids that had vents reopened.
        """
        if tc.max_vent_closed_min == 0:
            return []
        if now is None:
            now = datetime.now(UTC)

        reopened_rooms: list[str] = []
        limit = timedelta(minutes=tc.max_vent_closed_min)

        for room_id, states in cycle_states.items():
            if states.vent_closed_at is None:
                continue
            if now - states.vent_closed_at >= limit:
                vents = room_vents.get(room_id, [])
                if vents:
                    duration_min = (now - states.vent_closed_at).total_seconds() / 60
                    vent_ids = [v.entity_id for v in vents]
                    log.warning(
                        "Room %s vents %s closed %.1f min (max=%d) — reopening for safety",
                        room_id,
                        vent_ids,
                        duration_min,
                        tc.max_vent_closed_min,
                    )
                    if self._logger:
                        await self._logger.log(
                            "warning",
                            "engine",
                            f"Force-reopening vents for room {room_id} — "
                            f"closed {duration_min:.1f}min (max={tc.max_vent_closed_min}min): {vent_ids}",
                            {
                                "room_id": room_id,
                                "entity_ids": vent_ids,
                                "duration_closed_min": round(duration_min, 1),
                                "max_vent_closed_min": tc.max_vent_closed_min,
                            },
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
                await self._invoke_close(vent)
                if self._logger:
                    await self._logger.log(
                        "warning",
                        "engine",
                        f"Emergency closed vent {vent.entity_id}",
                        {"entity_id": vent.entity_id, "control_method": vent.control_method},
                    )
            except Exception as exc:
                await self._log_vent_error(vent, "close", exc)

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
