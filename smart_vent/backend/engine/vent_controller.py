"""
Vent controller: open/close Flair cover entities with safety enforcement.

Safety rules enforced here:
- Airflow floor (#213): keep at least ``required_open_vents()`` smart vents open
  so total duct airflow does not drop below a fraction of total registers.
- max_vent_closed_min: opt-in safety valve — reopen a vent closed too long (0 = disabled, the default)
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, timedelta

import aiosqlite

from .. import db
from ..event_logger import EventLogger
from ..ha_client import HAClient
from ..models import RoomCycleState, RoomVent, ThermostatConfig


def required_open_vents(tc: ThermostatConfig, total_smart_vents: int) -> int:
    """Minimum *smart* vents that must stay open under the airflow floor (#213).

    Translates the per-thermostat config into a hard count the caller can
    compare against ``open - would_close``:

    * ``has_bypass_damper=True`` → ``0`` (a bypass damper relieves duct static
      pressure mechanically; the airflow floor is not enforced).
    * ``total_vents_count`` set → fraction-of-total minus the passive registers
      that are always open: ``max(0, ceil(total * fraction) - (total - smart))``.
      With e.g. 12 total / 4 smart / fraction 1/3, ``ceil(4) - 8 = -4`` →
      clamped to 0, so all four smart vents may close.
    * ``total_vents_count`` unset → ``1`` (transitional default, matches the
      pre-#213 ``min_open_vents`` default of 1 so existing thermostats do not
      regress through the upgrade window; the Thermostats-page banner asks the
      user to fill in the new fields).
    """
    if tc.has_bypass_damper:
        return 0
    if tc.total_vents_count is None:
        return 1
    required_total_open = math.ceil(tc.total_vents_count * tc.min_open_vents_fraction)
    always_open_passive = max(0, tc.total_vents_count - total_smart_vents)
    return max(0, required_total_open - always_open_passive)


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
                f"Failed to {direction} vent {vent.entity_id} using {vent.control_method}: {exc}",
                {
                    "entity_id": vent.entity_id,
                    "control_method": vent.control_method,
                    "direction": direction,
                    "error": str(exc),
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
            # Method-aware skip (#425): a tilt/position vent whose HA `state`
            # reads "open" may be tilt-closed or parked partial — only skip
            # when it is genuinely at full open.
            if self._is_fully_open(vent):
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

        required = required_open_vents(tc, len(all_zone_vents))
        currently_open = self._count_open_vents(all_zone_vents)
        would_close = len([v for v in vents if self._is_open(v)])
        if currently_open - would_close < required:
            log.warning(
                "Deferring vent close — would drop to %d open (required=%d)",
                currently_open - would_close,
                required,
            )
            if self._logger:
                vent_ids = [v.entity_id for v in vents]
                await self._logger.log(
                    "warning",
                    "engine",
                    f"Vent close deferred — would drop to {currently_open - would_close} open "
                    f"(airflow floor requires {required}): {vent_ids}",
                    {
                        "entity_ids": vent_ids,
                        "currently_open": currently_open,
                        "would_close": would_close,
                        "required_open_vents": required,
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

    async def force_close_vents(self, vents: list[RoomVent]) -> None:
        """Close vents without consulting the airflow floor — but never
        blindly (#424): a vent that is already closed is skipped, because for
        ``control_method="toggle"`` an unguarded close INVERTS it open, and
        for every other method the skip saves a redundant HA call. Errors are
        logged per vent and never raised — vent failures must not abort a
        tick or a cycle transition.
        """
        for vent in vents:
            if not self._is_open(vent):
                continue
            try:
                await self._invoke_close(vent)
            except Exception as exc:
                await self._log_vent_error(vent, "close", exc)

    async def close_all_zone_vents(self, all_zone_vents: list[RoomVent]) -> None:
        """Emergency: close every vent in a zone (thermostat unavailable etc.)."""
        for vent in all_zone_vents:
            # Guard on physical state (#424): toggling an already-closed
            # `toggle` vent would OPEN it — on the one path whose entire
            # purpose is "make everything closed".
            if not self._is_open(vent):
                continue
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

    @staticmethod
    def _position_attr_for(control_method: str | None) -> str | None:
        """The HA attribute that reflects airflow for position-driven methods."""
        if control_method == "set_tilt_position":
            return "current_tilt_position"
        if control_method == "set_position":
            return "current_position"
        return None

    def _is_open(self, vent: RoomVent | str) -> bool:
        """Whether a vent is passing air, judged per control method (#425).

        HA derives a cover's ``state`` from ``current_position``; for
        tilt-commanded vents it generally does NOT track
        ``current_tilt_position``, so ``state`` alone misreports tilt vents
        (and a ``set_position`` vent parked partially open reports plain
        "open"). Position/tilt methods therefore read their own attribute —
        any value > 0 counts as passing air — falling back to ``state`` when
        the attribute is absent. A bare entity_id (control method unknown)
        keeps the legacy state-only check.
        """
        if isinstance(vent, str):
            entity_id, method = vent, None
        else:
            entity_id, method = vent.entity_id, vent.control_method
        state = self._ha.get_state(entity_id)
        if state is None:
            return False
        attr = self._position_attr_for(method)
        if attr is not None:
            pos = (state.get("attributes") or {}).get(attr)
            if pos is not None:
                try:
                    return float(pos) > 0
                except (TypeError, ValueError):
                    pass
        return state.get("state") == "open"

    def _is_fully_open(self, vent: RoomVent) -> bool:
        """Whether a vent is at (or effectively at) its full-open position.

        Used by ``open_room_vents`` to decide the "already open — skip" case:
        a position/tilt vent parked at 50% reports ``state="open"`` but must
        still be driven to 100 at cycle start (#425), so for those methods the
        skip requires the position attribute to read ≥ 99.
        """
        state = self._ha.get_state(vent.entity_id)
        if state is None:
            return False
        attr = self._position_attr_for(vent.control_method)
        if attr is not None:
            pos = (state.get("attributes") or {}).get(attr)
            if pos is not None:
                try:
                    return float(pos) >= 99
                except (TypeError, ValueError):
                    pass
        return state.get("state") == "open"

    def _count_open_vents(self, vents: list[RoomVent]) -> int:
        return sum(1 for v in vents if self._is_open(v))

    def get_vent_states(self, vents: list[RoomVent]) -> dict[str, str]:
        """Return {entity_id: 'open'|'closed'|'unknown'} for each vent.

        Follows the same control-method-aware airflow judgement as
        ``_is_open`` (#425) so the UI shows what the vent is physically doing
        rather than HA's position-derived ``state`` for tilt vents.
        """
        result = {}
        for v in vents:
            state = self._ha.get_state(v.entity_id)
            if state is None:
                result[v.entity_id] = "unknown"
            elif state.get("state") not in ("open", "closed"):
                # unavailable/unknown etc. — pass through unmodified.
                result[v.entity_id] = state.get("state", "unknown")
            else:
                result[v.entity_id] = "open" if self._is_open(v) else "closed"
        return result
