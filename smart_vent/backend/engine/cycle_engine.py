"""
HVAC Cycle Engine — one instance per thermostat zone.

State machine:
  IDLE → RUNNING → (all rooms at target) → TERMINATING → IDLE
  RUNNING → ABORTED (system disabled, vacation mode, no active rooms,
                     sustained thermostat unavailability)
  RUNNING → TERMINATED (cycle timeout)
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

import aiosqlite

from .. import db, eco
from ..event_logger import EventLogger
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
from ..units import to_f
from .room_manager import (
    ActiveRoom,
    OverflowCandidate,
    _effective_deadband,
    _find_matching_schedule,
    _seconds_since_schedule_end,
    expire_holdovers,
    get_active_rooms,
)
from .vent_controller import VentController, required_open_vents

log = logging.getLogger(__name__)

# Callback type for broadcasting state changes to WebSocket clients
BroadcastFn = Callable[[str, dict], Coroutine]

# Sensor-staleness guard (Issue #211). Battery-powered Zigbee/Z-Wave temperature
# sensors that drop off the mesh keep their last numeric state in HA. Readings
# older than this threshold (minutes) are excluded from room-temperature
# averages so the engine never drives control decisions off stale data.
SENSOR_STALE_AFTER_MIN: float = 30.0

# Setpoint drift tolerance (°F). Two setpoints within this band are treated as
# equal, so the engine neither re-commands an idle setpoint that already equals
# ambient (Issue #296) nor flags reconcile drift for HA float-rounding noise.
_SETPOINT_DRIFT_TOLERANCE_F: float = 0.1

# Thermostat-unavailability tolerance (Issue #267): the abort threshold is the
# per-thermostat ``unavailable_abort_after_min`` config field (default 5 min,
# 0 = never abort), surfaced on the Thermostats page. See the availability
# guard at the top of ``_do_tick``.

# Ambient plausibility guard (Issue #636). A climate integration that has just
# reconnected after an outage can report a stale/zeroed ``current_temperature``
# — 0°C (32°F) is the classic null-glitch value — for exactly one tick before
# it repopulates its real attributes. That reading is a valid, fresh,
# non-stale float: #211 sensor staleness only watches ROOM sensors, and #267's
# unavailable-abort only watches the entity's ``state``, not a fresh-looking
# attribute value, so nothing upstream of `_read_validated_ambient_f` can tell
# a fault from a real reading. Two independent bands, both internal safety
# constants rather than user knobs (a new knob would owe a UI control per
# CLAUDE.md, which this narrow guard does not need):
#   - Absolute plausibility: no occupied indoor space reports outside this
#     range, so a thermostat that does is reporting a fault, not a
#     temperature.
#   - Rate of change: building thermal mass makes more than a few °F of real
#     drift per minute physically impossible, so this also catches a glitch
#     that happens to land inside the absolute band.
AMBIENT_PLAUSIBLE_MIN_F: float = 35.0
AMBIENT_PLAUSIBLE_MAX_F: float = 110.0
AMBIENT_MAX_RATE_F_PER_MIN: float = 3.0

# Floor under the elapsed-time denominator of the rate-of-change check, purely
# to keep two readings that land in the same instant (or, in theory, out of
# monotonic order) from dividing by ~zero and rejecting on ANY nonzero delta.
# Deliberately tiny (one second): it must never mask a genuinely fast swing at
# the real 60s tick cadence, only guard the arithmetic.
_AMBIENT_RATE_MIN_ELAPSED_MIN: float = 1.0 / 60.0


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
        broadcast: BroadcastFn | None = None,
        event_logger: EventLogger | None = None,
        get_enabled: Callable[[], bool] | None = None,
        get_vacation_mode: Callable[[], bool] | None = None,
        get_eco_suspended: Callable[[], bool] | None = None,
    ) -> None:
        self.thermostat_entity_id = thermostat_entity_id
        self._ha = ha
        self._vent = vent_ctrl
        self._broadcast = broadcast
        self._logger = event_logger
        self._get_enabled = get_enabled
        self._get_vacation_mode = get_vacation_mode
        self._get_eco_suspended = get_eco_suspended

        self._state = CycleState.IDLE
        self._cycle_log: CycleLog | None = None
        self._cycle_mode: str | None = None  # mode locked at cycle start; used for monitoring
        self._cycle_ha_mode: str | None = None  # 'heat' or 'cool' — explicit HA mode sent
        self._active_rooms: dict[str, ActiveRoom] = {}  # room_id → ActiveRoom
        self._room_cycle_states: dict[str, RoomCycleState] = {}  # room_id → state
        self._room_vents: dict[str, list[RoomVent]] = {}  # room_id → vents
        # Eco Mode hysteresis state (Issue #404): (room_id, mode) → whether Eco
        # is currently engaged (past its threshold). Kept in memory across cycle
        # boundaries so relaxation begins at the threshold but only stops once
        # outside falls to threshold − band. Keyed by mode as well as room so a
        # cooling engagement can never seed a later heating evaluation (their
        # thresholds are unrelated). A restart resets it (re-evaluated on the
        # next boundary); it never affects behaviour when Eco is off.
        self._eco_engaged: dict[tuple[str, str], bool] = {}
        # Eco Suspend snapshot (Issue #500). Eco Suspend is next-cycle-only: a
        # cycle finishes under the Eco state it started with, so the live
        # suspension flag is sampled by _apply_eco only while IDLE (i.e. when
        # deciding/starting the next cycle) and the sampled value is carried
        # here for the cycle's whole lifetime. Mid-cycle joins and in-place
        # trigger updates read the snapshot, never the live flag.
        self._cycle_eco_suspended: bool = False
        self._lock = asyncio.Lock()

        # Last setpoint value successfully sent to HA; used by reconciliation to
        # detect external changes to the thermostat setpoint.
        self._last_setpoint_sent: float | None = None
        # Timestamp of the last reconciliation run; None = never reconciled.
        self._last_reconciled_at: datetime | None = None
        # One-shot grace flag (Issue #637 follow-up): set when `_do_tick`'s
        # heat_cool guard (~line 474) reverts a live heat_cool to `off`.
        # Consumed by the very next idle-arm mode-hygiene evaluation in
        # `_reconcile_state`, which otherwise sees that just-commanded `off`
        # and mistakes Plenum's own remedial action for external
        # interference, reverting it right back to heat/cool. Cleared the
        # first time the idle arm actually runs WHILE IDLE, whether or not
        # it found a mismatch — the grace is "give our own last action one
        # look", not an ongoing suppression. That first look only happens on
        # a tick where `_maybe_reconcile` reaches `_reconcile_state` at all,
        # which needs `reconciliation_interval_min > 0` — at the default of
        # 0 (reconciliation disabled) this flag is armed but never consumed.
        # Harmless: it costs at most one skipped correction on whatever
        # later tick reconciliation gets turned on, never an extra command.
        self._heat_cool_revert_pending: bool = False
        self._sensor_map: dict[str, list[str]] = {}

        # Short-cycle protection (Issue #208): wall-clock time the most recent
        # cycle ended (terminated or aborted). Used to enforce the compressor
        # off-time lockout before a new cycle may start. Rehydrated from the
        # newest closed cycle log on restore (#432) — an add-on restart takes
        # seconds, not the multi-minute gap this field's in-memory-only
        # design originally assumed, so a reboot must not silently disable
        # compressor protection.
        self._last_cycle_ended_at: datetime | None = None

        # Wall-clock time the VACATION HOLD last stopped a compressor run
        # (Issue #628). `_last_cycle_ended_at` above is written only when a
        # CYCLE ends, and the hold never creates a cycle — so across a
        # multi-day trip the off-time lockout expired once (after the
        # activation abort stamped it) and never re-armed, leaving the hold
        # free to stop and restart the compressor on its own bound for days
        # with nobody home. Deliberately a SEPARATE field rather than
        # overloading `_last_cycle_ended_at`: that one is rehydrated from the
        # newest closed cycle log on restore (#432), so it has to keep meaning
        # "the end of a row in cycle_log" or the next reader to rely on that
        # correspondence is misled. `_compressor_off_since` unifies the two
        # for the guard, which cares about the equipment rather than about
        # which part of the engine stopped it.
        self._hold_compressor_off_at: datetime | None = None

        # When the thermostat entity became unavailable (Issue #267); None
        # while it is reachable. Cleared the moment a tick sees it available
        # again. Drives both the cycle-abort threshold and the UI banner
        # (/api/thermostat-health).
        self._unavailable_since: datetime | None = None

        # True while the tick in progress is running under vacation mode with
        # per-room safety cycles enabled (#626). Set once per tick in
        # `_do_tick`; read by `_start_or_update_cycle` so a fresh vacation
        # safety cycle does NOT close the idle rooms' vents — with nobody home
        # every room is a welcome destination for the surplus air, and the
        # per-tick `_apply_vacation_vent_policy` closes each one only as it
        # approaches its own opposite safety bound.
        self._vacation_cycle: bool = False

        # Sensor-staleness episodes already announced via the event log
        # (Issue #211). Tracked per-engine so we warn once per stale episode
        # rather than every 60-second tick.
        self._stale_warned: set[str] = set()

        # The vacation hold's last-announced posture (Issue #627), or None when
        # no trip is in progress. The hold re-evaluates every 60 s for as long
        # as the trip lasts, so its event-log lines are keyed on this posture
        # and written once per TRANSITION — the same rate-limiting discipline
        # as #211 (staleness) and #270 (thermostat outage). Cleared the moment
        # vacation ends so the next trip announces its opening state afresh.
        self._vacation_hold_posture: str | None = None

        # Ambient plausibility guard (Issue #636). The last ambient reading
        # `_read_validated_ambient_f` accepted, and when — the baseline the
        # rate-of-change check compares a new reading against. None until the
        # first reading is accepted, so a thermostat that has never reported
        # (or has only ever reported implausible values) gets its first
        # plausible-looking reading accepted outright, which then becomes the
        # baseline.
        self._last_valid_ambient_f: float | None = None
        self._last_valid_ambient_at: datetime | None = None
        # Human-readable reason the MOST RECENT ambient reading was rejected,
        # or None when the last read was accepted (or simply absent). Lets
        # `_enforce_safety_setpoint`, which has no announce path of its own,
        # report a rejection once per episode without re-deriving the message
        # `_read_validated_ambient_f` already built.
        self._ambient_reject_detail: str | None = None
        # Whether the CURRENT ambient-rejection episode has already been
        # announced via the event log — the same once-per-episode discipline
        # as #211 (staleness), #270 (outage) and #627 (vacation hold posture),
        # so a flapping integration cannot flood the feed. Cleared the moment
        # a reading is accepted again.
        self._ambient_reject_warned: bool = False
        # Per-TICK memoization of the validated ambient reading. The guard
        # above is stateful — its rate-of-change check compares against real
        # WALL-CLOCK elapsed time since the last accepted reading — but
        # `_get_avg_temp` (the `include_thermostat_sensor` room-temperature
        # proxy) is called from 15+ places within a single tick's execution,
        # all effectively simultaneous. Re-running the raw check on every one
        # of those calls would compare each call's reading against a baseline
        # captured microseconds earlier and manufacture a rejection out of
        # ANY nonzero delta — a bug this guard would be introducing, not
        # fixing. `_validated_ambient_for_tick` runs the real check at most
        # ONCE per tick and every caller within that tick reads the same
        # cached answer. `_tick_ambient_computed` is a separate bool (not
        # `_tick_validated_ambient_f is not None`) because a rejected/missing
        # reading is itself a valid, memoizable outcome for the tick — the
        # cache must distinguish "not yet computed" from "computed and the
        # answer was None". Reset at the top of every `_do_tick`.
        self._tick_ambient_computed: bool = False
        self._tick_validated_ambient_f: float | None = None

        # Rooms currently held active by per-room safety protection (Issue #367),
        # so the activation warning is emitted once per breach episode rather
        # than every tick the room stays over/under the envelope. Rooms that
        # recover (or gain real demand) are dropped so a future breach warns
        # again. See ``_add_safety_rooms``.
        self._safety_warned_room_ids: set[str] = set()

        # Rooms whose safety protection is being SUSTAINED past the bound that
        # triggered it, mapped to the direction that triggered it ("cooling" /
        # "heating") — Issue #633. `_add_safety_rooms` re-evaluates every room
        # from scratch on each 60 s tick; without this latch it tested the bare
        # `max_setpoint` / `min_setpoint` every time, so a room dropped out of
        # the active set the moment it crossed back inside the envelope by
        # 0.1°F — long before reaching the deadband-inset target the safety
        # cycle was commanding. During vacation that empties `new_active_map`
        # and the no-demand branch aborts the cycle, so a 78.0°F ceiling
        # produced one-minute cycles that stopped at 77.9°F while still
        # targeting 76.5°F, repeating all night behind the off-time lockout.
        # This is the same edge short-cycling #628 removed from the vacation
        # HOLD; #367 gave safety rooms a hysteretic TARGET but left the
        # re-arm test on the bare bound, so the cycle could never reach it.
        # The direction is stored, not just the room id: a cooling-latched
        # room must not inherit the heating branch's loosened bound.
        self._safety_sustain: dict[str, str] = {}
        # Active staleness threshold (minutes). Refreshed at the start of each
        # tick from the ``sensor_stale_after_min`` system setting so changes
        # from the Settings page take effect on the next tick.
        self._stale_after_min: float = SENSOR_STALE_AFTER_MIN

        # Overflow-conditioning room set (Issue #237): non-active rooms whose
        # vents we opened during the current minimum-runtime hold so the
        # surplus conditioned air can be absorbed somewhere other than the
        # already-satisfied cycle rooms. Recomputed every tick during the hold;
        # cleared at cycle termination.
        self._overflow_room_ids: set[str] = set()

        # Overflow-room cycle data points (Issue #254): the persisted
        # RoomCycleState (role='overflow') for each non-active room this cycle
        # has redirected surplus air into, keyed by room_id. Captures
        # temp_at_start (first overflow-open) and temp_at_end (final close or
        # cycle end). Kept separate from ``_room_cycle_states`` so overflow
        # rooms never leak into the active-room paths.
        self._overflow_room_states: dict[str, RoomCycleState] = {}

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    @property
    def cycle_state(self) -> CycleState:
        return self._state

    @property
    def current_cycle_id(self) -> str | None:
        return self._cycle_log.id if self._cycle_log else None

    @property
    def unavailable_since(self) -> datetime | None:
        """When the thermostat entity became unavailable; None while reachable.

        Read by ``/api/thermostat-health`` to drive the UI banner (Issue #267).
        """
        return self._unavailable_since

    async def tick(self, conn: aiosqlite.Connection) -> None:
        """Main entry point — called by scheduler every 60s or on state change."""
        async with self._lock:
            await self._do_tick(conn)

    async def handle_presence(self, conn: aiosqlite.Connection, room: Room) -> None:
        """Called externally when a presence sensor fires for a room in this zone."""
        async with self._lock:
            await self._on_presence(conn, room)

    async def force_abort(self, conn: aiosqlite.Connection, reason: str) -> None:
        """Abort the current cycle regardless of the enabled flag.

        Called by the scheduler on system/dev mode toggles to guarantee a clean
        slate — the normal abort path inside `_do_tick` only fires when
        `_get_enabled()` is False, so a transition like system-on or
        dev-off-with-system-still-on wouldn't otherwise terminate an in-flight
        cycle.
        """
        async with self._lock:
            if self._state != CycleState.IDLE:
                await self._abort_cycle(conn, reason=reason)

    def get_zone_status(self) -> ZoneStatus:
        """Return a snapshot of the current zone status (no DB call needed)."""
        thermo_state = self._ha.get_state(self.thermostat_entity_id)
        if thermo_state:
            hvac_action = thermo_state.get("attributes", {}).get("hvac_action", "unknown")
            hvac_mode = thermo_state.get("state", "unknown")
            current_temp = _climate_temp_to_f(
                thermo_state.get("attributes", {}).get("current_temperature"),
                self._ha.ha_temp_unit,
            )
            setpoint = _climate_temp_to_f(
                thermo_state.get("attributes", {}).get("temperature"),
                self._ha.ha_temp_unit,
            )
        else:
            hvac_action = hvac_mode = "unknown"
            current_temp = setpoint = None

        room_states: list[RoomLiveState] = []
        for room_id, ar in self._active_rooms.items():
            vents = self._room_vents.get(room_id, [])
            total_sensors, available_sensors = self._sensor_counts(ar.room)
            room_states.append(
                RoomLiveState(
                    room_id=room_id,
                    avg_temp=self._get_avg_temp(ar.room),
                    sensor_count=total_sensors,
                    available_sensor_count=available_sensors,
                    vent_states=self._vent.get_vent_states(vents),
                    presence_active=ar.source == "presence",
                    holdover_expires_at=None,
                    target_temp=ar.target_temp,
                    requested_target=(
                        ar.requested_target if ar.requested_target is not None else ar.target_temp
                    ),
                    eco_active=ar.eco_active,
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
        # Ambient plausibility guard (#636): invalidate last tick's memoized
        # validated-ambient answer so this tick's first reader recomputes a
        # fresh one. Must happen before anything below can call
        # `_validated_ambient_for_tick` (directly, or indirectly through
        # `_get_avg_temp`).
        self._tick_ambient_computed = False
        self._tick_validated_ambient_f = None

        # Expire holdovers and overrides first
        await expire_holdovers(conn)
        expired_holds = await db.clear_expired_overrides(conn)
        # Hold expiry is a user-visible control handoff (#576): the room falls
        # back to schedule/presence on this very tick, so say so in the feed.
        # clear_expired_overrides only reports rows THIS caller deleted, so
        # concurrently ticking engines never double-log. The rows are global
        # (not scoped to this zone) — whichever engine sweeps first reports.
        if expired_holds and self._logger:
            for hold in expired_holds:
                room = await db.get_room(conn, hold.room_id)
                room_name = room.name if room else hold.room_id
                await self._logger.log(
                    "info",
                    "engine",
                    f"Temperature hold expired for {room_name} — "
                    "resuming schedule/presence control",
                    {"room_id": hold.room_id, "target_temp": hold.target_temp},
                )

        # Refresh the sensor-staleness threshold so Settings-page changes take
        # effect on the next tick. Stored as a string in system_settings.
        raw = await db.get_system_setting(
            conn, "sensor_stale_after_min", str(SENSOR_STALE_AFTER_MIN)
        )
        try:
            self._stale_after_min = float(raw)
        except (TypeError, ValueError):
            self._stale_after_min = SENSOR_STALE_AFTER_MIN

        # Thermostat availability check — always runs, even when system is
        # disabled or vacation mode is active. Transient outages are tolerated;
        # a sustained outage with a cycle in flight aborts it (Issue #267),
        # because every per-tick safety monitor (cycle timeout, the
        # max_vent_closed_min watchdog, reconciliation) lives below this guard
        # and would otherwise stay suspended while the physical HVAC keeps
        # running at the last commanded setpoint with vents closed.
        thermo_state = self._ha.get_state(self.thermostat_entity_id)
        if thermo_state is None or thermo_state.get("state") == "unavailable":
            now = datetime.now(UTC)
            first_tick_of_outage = self._unavailable_since is None
            if self._unavailable_since is None:
                self._unavailable_since = now
            outage_min = (now - self._unavailable_since).total_seconds() / 60
            log.warning(
                "Thermostat %s unavailable — skipping tick (%.1f min so far)",
                self.thermostat_entity_id,
                outage_min,
            )
            # Event-log warning once per outage episode (Issue #270), not on
            # every 60-second tick — mirrors the #211 sensor-staleness
            # rate-limiting so a multi-hour outage doesn't bury the feed.
            if first_tick_of_outage and self._logger:
                await self._logger.log(
                    "warning",
                    "engine",
                    f"Thermostat {self.thermostat_entity_id} is unavailable in Home Assistant "
                    "— the engine cannot supervise this zone until it reports again.",
                    {"thermostat": self.thermostat_entity_id},
                )
            if self._state != CycleState.IDLE:
                tc = await db.get_thermostat_config(conn, self.thermostat_entity_id)
                if (
                    tc.unavailable_abort_after_min > 0
                    and outage_min >= tc.unavailable_abort_after_min
                ):
                    await self._abort_cycle(conn, reason="thermostat unavailable")
            return
        # Available again — if we had been in an outage, announce recovery once
        # (symmetric with the once-per-episode warning above).
        if self._unavailable_since is not None:
            log.info("Thermostat %s is reporting again", self.thermostat_entity_id)
            if self._logger:
                await self._logger.log(
                    "info",
                    "engine",
                    f"Thermostat {self.thermostat_entity_id} is reporting again — "
                    "normal supervision resumed.",
                    {"thermostat": self.thermostat_entity_id},
                )
            self._unavailable_since = None

        # System disabled guard — if a cycle is running, abort it immediately.
        # _abort_cycle handles all logging; no pre-call log needed here.
        if self._get_enabled is not None and not self._get_enabled():
            if self._state != CycleState.IDLE:
                await self._abort_cycle(conn, reason="system disabled")
            else:
                log.debug("System disabled — skipping tick for %s", self.thermostat_entity_id)
            # Release the safety hysteresis (#633). Disabling is an operator
            # action of unbounded duration, and `_add_safety_rooms` — the only
            # thing that rebuilds this latch — does not run below. A latch left
            # standing would re-arm protection on the loosened inset bound on
            # the first tick after re-enable, conditioning a room that is back
            # inside the envelope. A brief thermostat outage deliberately does
            # NOT clear it: that return is transient, and dropping the latch
            # there would hand the flap back on every reconnect.
            self._safety_sustain = {}
            return

        # Vacation mode guard (Issue #626). Vacation is a DEMAND FILTER, not an
        # early return: schedules, presence and temporary holds stay paused, but
        # a room that breaches the comfort envelope still gets a real cycle via
        # `_add_safety_rooms` below. That is the #367/#368 incident one state
        # over — there the zone baked at 81°F against a 77°F ceiling *after* a
        # vacation hold expired; the same room, breaching the same bound one
        # minute earlier, was invisible to the engine.
        #
        # `_apply_vacation_hold` remains the strategy for every tick with no
        # breach, and the ONLY strategy when safety cycles are off or the
        # thermostat holds a heat_cool range — see `_vacation_safety_enabled`.
        in_vacation = self._get_vacation_mode is not None and self._get_vacation_mode()
        if not in_vacation:
            # The hold's announcements are scoped to one trip (#627): end the
            # episode here so a later vacation re-announces what it is doing
            # instead of inheriting the posture the last one left behind.
            self._vacation_hold_posture = None
        else:
            tc_vac = await db.get_thermostat_config(conn, self.thermostat_entity_id)
            if not self._vacation_safety_enabled(tc_vac):
                self._vacation_cycle = False
                if self._state != CycleState.IDLE:
                    await self._abort_cycle(conn, reason="vacation mode")
                # Same reasoning as the disabled guard above (#633): safety
                # cycles are off, the hold owns the zone, and nothing rebuilds
                # the latch while that is true.
                self._safety_sustain = {}
                await self._apply_vacation_hold(conn, thermo_state)
                return
        self._vacation_cycle = in_vacation

        # heat_cool / auto mode must ONLY be active during vacation RANGE mode.
        # If the thermostat is in heat_cool outside of vacation (e.g. left behind
        # by the "Test auto mode" button), revert it to "off" immediately so the
        # next tick can start a normal single-direction cycle if needed. Reaching
        # here during vacation means safety cycles are on, which implies
        # `vacation_hvac_mode == "single"` — so a lingering heat_cool is equally
        # wrong there and gets the same treatment (#626).
        if thermo_state.get("state") == "heat_cool":
            why = "vacation single-setpoint mode" if in_vacation else "vacation mode not active"
            log.info(
                "Thermostat %s in heat_cool (%s) — reverting to off",
                self.thermostat_entity_id,
                why,
            )
            if self._logger:
                await self._logger.log(
                    "info",
                    "engine",
                    f"Thermostat {self.thermostat_entity_id} reverted from heat_cool to off"
                    f" ({why})",
                    {"thermostat": self.thermostat_entity_id},
                )
            await self._ha.set_thermostat_hvac_mode(self.thermostat_entity_id, "off")
            # Arm the one-tick grace window (Issue #637 follow-up) so the
            # idle-arm mode-hygiene check does not mistake this very "off" —
            # Plenum's own remedial action, not external interference — for
            # a mismatch on the next reconcile pass and revert it right back.
            self._heat_cool_revert_pending = True
            return

        # Determine which rooms should be active now. In vacation mode the
        # schedule/presence/override sources stay paused (#626) — a safety
        # breach below is the only thing that may create demand.
        new_active = [] if in_vacation else await get_active_rooms(conn, self.thermostat_entity_id)
        new_active_map = {ar.room.id: ar for ar in new_active}

        # Per-room safety protection (Issue #367): pull in any zone room whose
        # own temperature has breached the configured envelope — even with no
        # presence, schedule, or override — so it is conditioned by a cycle
        # instead of left to bake while other rooms run. Runs before the
        # no-active-rooms gate so a breaching room with no other demand still
        # triggers a protection cycle. The thermostat and system-disabled guards
        # above have already returned; the vacation guard has NOT (#626) — during
        # vacation this is the sole source of demand, `new_active_map` having
        # been seeded empty just above.
        await self._add_safety_rooms(conn, new_active_map)

        # Surface room-sensor staleness before any decisions are made off the
        # (now stale-filtered) data. Once-per-episode rate-limited internally.
        await self._emit_sensor_freshness_warnings(new_active_map)

        if not new_active_map:
            if in_vacation:
                # Minimum runtime still applies to a vacation safety cycle
                # (#633). This branch is the one exit from a running cycle that
                # never consulted `min_cycle_runtime_min`: the safety room is
                # the cycle's sole demand, so its release empties the map and
                # the abort below fired at whatever age the cycle had reached.
                # With the sustain latch in place the room no longer leaves
                # early, but a room that genuinely arrives inside the runtime
                # window would still pulse the compressor — hold the cycle open
                # instead, exactly as the non-vacation satisfied-rooms path does.
                if self._state != CycleState.IDLE and not self._cycle_runtime_satisfied(tc_vac):
                    await self._enter_min_runtime_hold(conn)
                    await self._maybe_broadcast()
                    return
                # Vacation, nothing breaching (#626): hand back to the hold —
                # which for a single-setpoint thermostat IS the thermostat-ambient
                # backstop, so `_enforce_safety_setpoint` must NOT also run or the
                # two would issue competing setpoint commands on the same tick.
                if self._state != CycleState.IDLE:
                    await self._abort_cycle(conn, reason="vacation mode - envelope restored")
                await self._apply_vacation_hold(conn, thermo_state)
                return
            if self._state != CycleState.IDLE:
                await self._abort_cycle(conn, reason="no active rooms")
            # IDLE reconciliation: ensure all zone vents are open even when no
            # rooms are scheduled. Only runs when system is enabled.
            if self._get_enabled is None or self._get_enabled():
                # Safety backstop (Issue #367): with no room demand, none of the
                # active-room logic below — where the max/min hard cap lives —
                # runs, so enforce the envelope directly against thermostat
                # ambient before the idle reconcile re-opens the zone vents.
                # The return value feeds `_maybe_reconcile` so the idle-mode
                # correction defers when the backstop just commanded this
                # tick — the pre-existing mechanism always wins.
                backstop_fired = await self._enforce_safety_setpoint(conn, thermo_state)
                await self._maybe_reconcile(
                    conn, in_vacation=in_vacation, backstop_fired=backstop_fired
                )
            return

        # Detect HVAC mode
        hvac_mode = self._read_hvac_mode()
        if hvac_mode == "unknown":
            log.warning("Thermostat %s mode unknown — skipping tick", self.thermostat_entity_id)
            return

        # Fetch thermostat config (needed for mode inference and room filtering).
        tc = await db.get_thermostat_config(conn, self.thermostat_entity_id)

        # Outside temperature for ambient presence-suppression / pre-cool
        # (Issue #248). Read once per tick; None when no sensor is configured or
        # it is unreadable, in which case the feature stays inert (fail-safe).
        outside_temp_f = await self._read_outside_temp(conn)
        # Per-room off_schedule_only window flags (only queried for rooms using
        # that mode). Threaded into both inference and filtering.
        off_schedule_ok = await self._compute_off_schedule_flags(conn, new_active_map)

        # Determine the effective mode for this tick:
        # - IDLE: infer from room temps (majority vote)
        # - RUNNING: use the mode locked at cycle start
        if self._state == CycleState.IDLE:
            inferred = await self._infer_mode_from_room_temps(
                new_active_map,
                tc.deadband,
                thermo_state,
                outside_temp=outside_temp_f,
                tc=tc,
                off_schedule_ok=off_schedule_ok,
            )
            if inferred == "off":
                # All rooms within deadband — park the setpoint so the HVAC
                # goes idle, then skip starting a new cycle.
                await self._reset_setpoint_to_ambient(thermo_state, tc)
                await self._maybe_reconcile(conn, in_vacation=in_vacation)
                await self._maybe_broadcast()
                return

            # Outdoor-temperature cooling lockout (Issue #209). Refuse to start
            # a cooling cycle when it is too cold outside — running an AC
            # compressor at low outdoor ambient risks liquid slugging and
            # evaporator coil icing. Opt-in: needs both a configured threshold
            # and a readable outdoor-temperature sensor. (Heat pumps are not
            # supported, so there is no heating lockout.)
            if inferred == "cooling":
                lockout_state, outside_temp = await self._cooling_lockout_state(conn, tc)
                if lockout_state == "sensor_unavailable":
                    log.warning(
                        "Cooling lockout configured for %s but the outdoor "
                        "temperature sensor is unset or unreadable — allowing "
                        "cooling (fail-open)",
                        self.thermostat_entity_id,
                    )
                    if self._logger:
                        await self._logger.log(
                            "warning",
                            "engine",
                            f"Cooling lockout is configured for {self.thermostat_entity_id} "
                            "but the outdoor-temperature sensor is unset or unreadable — "
                            "allowing the cooling cycle (fail-open). Set the outdoor sensor "
                            "on the Thermostats page for the lockout to take effect.",
                            {"thermostat": self.thermostat_entity_id},
                        )
                elif lockout_state == "locked_out":
                    assert outside_temp is not None  # 'locked_out' implies a reading
                    threshold = tc.cooling_lockout_below_f
                    assert threshold is not None
                    log.warning(
                        "Cooling locked out for %s — outdoor %.1f°F is below "
                        "cooling_lockout_below_f=%.1f°F; suppressing cooling cycle",
                        self.thermostat_entity_id,
                        outside_temp,
                        threshold,
                    )
                    if self._logger:
                        await self._logger.log(
                            "warning",
                            "engine",
                            f"Cooling cycle suppressed for {self.thermostat_entity_id} — "
                            f"outdoor temperature {outside_temp:.1f}°F is below the cooling "
                            f"lockout ({threshold:.1f}°F). Running the AC compressor this "
                            "cold risks liquid slugging and coil icing.",
                            {
                                "thermostat": self.thermostat_entity_id,
                                "outside_temp": outside_temp,
                                "cooling_lockout_below_f": threshold,
                            },
                        )
                    await self._reset_setpoint_to_ambient(thermo_state, tc)
                    await self._maybe_reconcile(conn, in_vacation=in_vacation)
                    await self._maybe_broadcast()
                    return

            effective_mode = inferred
        else:
            effective_mode = self._cycle_mode or hvac_mode

        # Filter out rooms that need the opposite direction from the chosen
        # cycle mode.  Without this, a cooling cycle with a minority of rooms
        # needing heat would open those rooms' vents during cooling, driving
        # them further from target.  (Issue #48 Bug 3)
        new_active_map = await self._filter_rooms_for_mode(
            new_active_map,
            effective_mode,
            tc.deadband,
            thermo_state,
            outside_temp_f,
            tc,
            off_schedule_ok,
        )

        if not new_active_map:
            if self._state != CycleState.IDLE:
                await self._abort_cycle(conn, reason="no compatible rooms after filtering")
            # Safety backstop (Issue #367): every room was filtered out, so no
            # cycle will drive the thermostat this tick. The system-disabled
            # guard above has already returned, so the system is enabled —
            # enforce the envelope directly. During vacation the hold owns the
            # thermostat instead (#626); running both would issue competing
            # setpoint commands on the same tick.
            backstop_fired = False
            if in_vacation:
                await self._apply_vacation_hold(conn, thermo_state)
            else:
                backstop_fired = await self._enforce_safety_setpoint(conn, thermo_state)
            # Both the vacation hold and the backstop above may have just
            # commanded this thermostat — the idle-mode correction must defer
            # to whichever one owned this tick rather than fight it.
            await self._maybe_reconcile(
                conn, in_vacation=in_vacation, backstop_fired=backstop_fired
            )
            await self._maybe_broadcast()
            return

        # Compressor off-time lockout (Issue #208). When the engine is IDLE and
        # a cycle ended recently, defer starting a new one until the configured
        # off-time has elapsed. This only gates a fresh IDLE→RUNNING start; an
        # already-running cycle is never interrupted by the lockout.
        if self._state == CycleState.IDLE and self._in_offtime_lockout(tc):
            remaining = self._offtime_lockout_remaining(tc)
            log.warning(
                "Compressor off-time lockout active for %s — %.1f min remaining, "
                "deferring new cycle",
                self.thermostat_entity_id,
                remaining,
            )
            if self._logger:
                await self._logger.log(
                    "warning",
                    "engine",
                    f"Compressor off-time lockout for {self.thermostat_entity_id} — "
                    f"new cycle deferred {remaining:.1f} min "
                    f"(min_cycle_offtime_min={tc.min_cycle_offtime_min})",
                    {
                        "thermostat": self.thermostat_entity_id,
                        "min_cycle_offtime_min": tc.min_cycle_offtime_min,
                        "lockout_remaining_min": round(remaining, 1),
                    },
                )
            await self._maybe_reconcile(conn, in_vacation=in_vacation)
            await self._maybe_broadcast()
            return

        # Eco Mode demand gate (#410): before an IDLE→RUNNING start, relax the
        # surviving rooms' targets and require at least one of them to still
        # have demand AT ITS RELAXED target. The mode vote and the room filter
        # above deliberately run on raw targets (join semantics unchanged),
        # but the cycle runs to the relaxed target — without this gate, a room
        # in the band (requested+deadband, relaxed] started a cycle that was
        # instantly "at target": min-runtime pulse cycles with full vent churn
        # on exactly the days Eco is supposed to REDUCE runtime.
        # _apply_eco is idempotent within the tick (it skips rooms whose
        # requested_target is already set), so the later call inside
        # _start_or_update_cycle does not double-relax.
        if self._state == CycleState.IDLE:
            self._apply_eco(new_active_map, effective_mode, outside_temp_f, tc)
            if not self._demand_at_relaxed_targets(
                new_active_map, effective_mode, tc, outside_temp_f, off_schedule_ok
            ):
                log.info(
                    "Eco Mode: every room is inside its relaxed target for %s — no cycle",
                    self.thermostat_entity_id,
                )
                await self._reset_setpoint_to_ambient(thermo_state, tc)
                await self._maybe_reconcile(conn, in_vacation=in_vacation)
                await self._maybe_broadcast()
                return

        # A persisting room's trigger (source or target_temp) can change mid-
        # cycle — e.g. presence holdover giving way to a schedule block for the
        # same room (priority 2 > 3 in _resolve_room), or a schedule's target
        # being edited. The room-id set is unchanged, so `rooms_changed` stays
        # False. Rather than tearing the whole cycle down — which stops the
        # HVAC and, with #208's off-time lockout, can then lock the room out of
        # heat it obviously still needs — the change is applied IN PLACE:
        # _start_or_update_cycle updates the room's target/source and re-derives
        # the setpoint while the cycle keeps running. A genuine direction flip
        # never reaches this point: _filter_rooms_for_mode (above) has already
        # dropped any room that now needs the opposite of the locked cycle
        # mode, so every surviving trigger change is same-direction. (#215)
        # #408: compare like with like. ``new_active_map`` carries the RAW
        # requested target (rebuilt fresh each tick, pre-Eco), while the
        # stored active rooms carry the Eco-RELAXED effective target — a
        # raw-vs-effective comparison made trigger_changed fire on EVERY tick
        # of an eco-relaxed cycle, re-running _start_or_update_cycle (and
        # re-computing Eco with the live outdoor reading) 60 times an hour.
        # _requested_of() compares the user's actual ask on both sides.
        # #517: the per-schedule deadband is part of the ask too. The monitor
        # paths (_monitor_rooms, _rooms_drifted_past_deadband) read the band off
        # self._active_rooms, which is only reassigned by _start_or_update_cycle
        # — so without this comparison, crossing from one block to another with
        # the same source and target but a WIDER band (or editing a running
        # block) would leave the cycle monitoring on the stale band until it
        # ended. Both sides are raw schedule values (never Eco-adjusted), so
        # this compares like with like and cannot re-introduce the #408 churn.
        trigger_changed = self._state == CycleState.RUNNING and any(
            room_id in self._active_rooms
            and _trigger_differs(new_active_map[room_id], self._active_rooms[room_id])
            for room_id in new_active_map
        )

        # If rooms changed, a trigger changed, or we're starting fresh, update
        # the cycle and recompute the setpoint. For mid-cycle updates the
        # original cycle direction is preserved so a momentary
        # hvac_action="idle" (common on heat_cool thermostats) does not flip
        # the setpoint calculation to the wrong direction.
        rooms_changed = set(new_active_map) != set(self._active_rooms)
        if rooms_changed or trigger_changed or self._state == CycleState.IDLE:
            await self._start_or_update_cycle(conn, new_active_map, effective_mode, outside_temp_f)

        # Monitor rooms using the mode locked at cycle start.  Live hvac_action
        # oscillates between "cooling"/"heating" and "idle" between HVAC bursts;
        # re-reading it each tick causes _is_at_target() to use the wrong
        # comparison direction during the idle phase (see issue #26).
        #
        # Guard: if _cycle_mode is None during a running cycle (should not
        # happen, but possible after a restore edge case), skip monitoring
        # rather than falling back to hvac_mode which may be "off"/"unknown"
        # and would cause _is_at_target to use the wrong comparison
        # direction.  (Issue #48 Bug 6)
        monitor_mode = self._cycle_mode or hvac_mode
        if monitor_mode not in ("cooling", "heating"):
            log.warning(
                "Skipping room monitoring — no valid cycle mode "
                "(cycle_mode=%r, hvac_mode=%r) for %s",
                self._cycle_mode,
                hvac_mode,
                self.thermostat_entity_id,
            )
        else:
            await self._monitor_rooms(conn, monitor_mode)
            # Vacation "everyone shares the air" vent policy (#626). Runs after
            # the normal monitor so the active (breaching) rooms' own at-target
            # closes win, and re-asserted every tick so a restart or an external
            # vent change self-heals.
            if in_vacation and self._state == CycleState.RUNNING:
                await self._apply_vacation_vent_policy(conn, tc, monitor_mode)

        # Check cycle timeout
        if self._cycle_log and self._state == CycleState.RUNNING:
            tc = await db.get_thermostat_config(conn, self.thermostat_entity_id)
            elapsed = datetime.now(UTC) - self._cycle_log.started_at
            if elapsed > timedelta(hours=tc.cycle_timeout_hours):
                log.warning(
                    "Cycle %s timed out after %.1fh — terminating",
                    self._cycle_log.id,
                    elapsed.total_seconds() / 3600,
                )
                if self._logger:
                    await self._logger.log(
                        "warning",
                        "engine",
                        f"Cycle timed out after {elapsed.total_seconds() / 3600:.1f}h for {self.thermostat_entity_id}",
                        {
                            "thermostat": self.thermostat_entity_id,
                            "cycle_id": self._cycle_log.id,
                        },
                    )
                await self._terminate_cycle(conn, reason="timeout")

        await self._maybe_reconcile(conn, in_vacation=in_vacation)
        await self._maybe_broadcast()

    def _apply_eco(
        self,
        new_active_map: dict[str, ActiveRoom],
        hvac_mode: str,
        outside_temp_f: float | None,
        tc: ThermostatConfig,
    ) -> None:
        """Relax active-room targets per Eco Mode (Issue #404), in place.

        For each active room, resolve the effective Eco config (thermostat
        values with per-room field-level overrides) and relax the room's
        ``target_temp`` toward the configured drift based on how far past the
        threshold it is outside. ``requested_target`` preserves the original
        ask; ``eco_active`` flags whether the target actually moved. Hysteresis
        engaged-state is carried in ``self._eco_engaged`` across cycle
        boundaries. Pure computation (delegates to ``eco.relax_target``); all
        temperatures °F.

        Strict no-op when Eco is off, the outside temp is missing, or the mode
        is not heating/cooling: ``target_temp`` is left untouched, so every
        downstream line runs exactly as it did before this feature (the
        eco-off byte-identical invariant).
        """
        # Eco Suspend (Issue #500): while suspended, Eco is fully off for the
        # whole zone — including rooms whose tri-state override explicitly
        # opts them in. Next-cycle-only semantics: a RUNNING cycle uses the
        # snapshot taken when it started; an IDLE evaluation samples the live
        # flag and refreshes the snapshot the next fresh start will carry.
        # ``_eco_engaged`` is deliberately untouched — a suspension is a
        # pause, not a reset, so the hysteresis memory survives it (same
        # principle as #434's flaky-outdoor-tick rule).
        if self._state == CycleState.RUNNING:
            suspended = self._cycle_eco_suspended
        else:
            suspended = bool(self._get_eco_suspended is not None and self._get_eco_suspended())
            self._cycle_eco_suspended = suspended

        for ar in new_active_map.values():
            if ar.requested_target is not None:
                # Already relaxed this tick (#410 applies Eco before the
                # cycle-start decision; this method is also called from
                # _start_or_update_cycle) — never relax twice.
                continue
            if suspended:
                # Same strict no-op shape as the eco-off path: the target is
                # untouched, eco_active stays False, and every downstream line
                # (metrics included) behaves exactly as if Eco were disabled.
                ar.requested_target = ar.target_temp
                ar.eco_active = False
                continue
            if ar.source == "safety" or (ar.source == "override" and not ar.respect_eco):
                # Safety rooms (#409): their target is a protective bound —
                # max_setpoint − deadband, deliberately one deadband INSIDE the
                # envelope for hysteresis (#367). Relaxing it clamps to the
                # bound exactly, so the cycle ends ON the breach threshold and
                # the room re-breaches next tick — perpetual edge cycling on
                # the hottest days. Eco relaxes comfort asks, never protective
                # recovery targets.
                # Manual overrides (#419): "this room, this temperature, right
                # now" is the strongest user signal there is — the same
                # explicit-intent rule the pre-cool feature applies. Schedules
                # remain relaxable (that is Eco's whole value); per-room
                # eco_mode_enabled=Off is the standing opt-out.
                # A hold may opt IN to relaxation via respect_eco (#576) —
                # such a hold falls through to the normal relax path below,
                # still gated by the suspension branch above and the room's
                # eco tri-state inside resolve_params.
                ar.requested_target = ar.target_temp
                ar.eco_active = False
                continue
            params = eco.resolve_params(tc, ar.room)
            result = eco.relax_target(
                ar.target_temp,
                hvac_mode,
                outside_temp_f,
                params,
                tc.min_setpoint,
                tc.max_setpoint,
                self._eco_engaged.get((ar.room.id, hvac_mode), False),
            )
            if outside_temp_f is not None:
                # #434: relax_target returns engaged=False whenever the outdoor
                # reading is missing — writing that back would erase the
                # hysteresis memory on a single flaky tick, and a recovery
                # inside the band would then snap Eco off (the spurious burst
                # the band exists to prevent). A no-op tick leaves the
                # engagement untouched.
                self._eco_engaged[(ar.room.id, hvac_mode)] = result.engaged
            ar.requested_target = ar.target_temp
            ar.target_temp = result.effective_target
            ar.eco_active = result.eco_active
            if result.eco_active:
                log.info(
                    "Eco Mode relaxed %s: %.1f°F → %.1f°F (outside=%.1f°F, mode=%s)",
                    ar.room.name,
                    ar.requested_target,
                    ar.target_temp,
                    outside_temp_f,
                    hvac_mode,
                )

    def _demand_at_relaxed_targets(
        self,
        new_active_map: dict[str, ActiveRoom],
        hvac_mode: str,
        tc: ThermostatConfig,
        outside_temp: float | None,
        off_schedule_ok: dict[str, bool] | None,
    ) -> bool:
        """Whether any room still calls for *hvac_mode* at its RELAXED target
        (#410). Assumes ``_apply_eco`` has already run on the map.

        Uses the same per-room vote as mode inference (suppression and the
        pre-cool hard cap included) so the demand gate composes with every
        other guard. Rooms with no reading cannot vote; if NO room has a
        reading the gate defers to the raw-target decision already made
        (returns True) rather than blocking a cycle on missing data.
        """
        want = "cool" if hvac_mode == "cooling" else "heat"
        any_reading = False
        for ar in new_active_map.values():
            avg = self._get_avg_temp(ar.room)
            if avg is None:
                continue
            any_reading = True
            recently_off = bool(off_schedule_ok and off_schedule_ok.get(ar.room.id))
            vote, _suppressed = self._suppression_vote(
                ar.room,
                avg + ar.room.temp_offset,
                ar.target_temp,
                ar.source,
                _effective_deadband(ar.room, tc.deadband, ar.deadband_override),
                outside_temp,
                tc,
                recently_off,
            )
            if vote == want:
                return True
        return not any_reading

    @staticmethod
    def _rcs_eco_kwargs(ar: ActiveRoom) -> dict:
        """Eco measurability fields for a RoomCycleState built from *ar*
        (Issue #404). ``requested_target`` falls back to the target when Eco
        never ran, so the columns are always populated for active rooms."""
        requested = ar.requested_target if ar.requested_target is not None else ar.target_temp
        return {
            "requested_target": requested,
            "effective_target": ar.target_temp,
            "eco_active": ar.eco_active,
        }

    async def _start_or_update_cycle(
        self,
        conn: aiosqlite.Connection,
        new_active_map: dict[str, ActiveRoom],
        hvac_mode: str,
        outside_temp_f: float | None = None,
    ) -> None:
        tc = await db.get_thermostat_config(conn, self.thermostat_entity_id)

        # Eco Mode (Issue #404): relax each active room's target here, at the
        # cycle boundary, before it becomes a setpoint or a RoomCycleState. When
        # Eco is off this is a strict no-op — targets are untouched and every
        # line below runs exactly as before.
        self._apply_eco(new_active_map, hvac_mode, outside_temp_f, tc)

        # Load vents for all active rooms (new set) — snapshot BEFORE opening
        # so we can record vents_at_start for fresh cycles.
        new_room_vents: dict[str, list[RoomVent]] = {}
        for room_id in new_active_map:
            new_room_vents[room_id] = await db.get_room_vents(conn, room_id)

        # Newly added / removed rooms mid-cycle, plus persisting rooms whose
        # trigger (source or target_temp) changed since the last tick. The
        # `changed` set drives the #215 in-place update — it must be computed
        # before self._active_rooms is reassigned below.
        added = set(new_active_map) - set(self._active_rooms)
        removed = set(self._active_rooms) - set(new_active_map)
        # #408: judged on the REQUESTED target (both sides are post-_apply_eco
        # here, so requested_target is populated). Comparing effective targets
        # made every outdoor-reading drift register as a "trigger change" —
        # an in-place update, an event-log entry, and a setpoint-history row
        # per active room every few minutes for the whole cycle.
        changed = {
            rid
            for rid in set(new_active_map) & set(self._active_rooms)
            if _trigger_differs(new_active_map[rid], self._active_rooms[rid])
        }

        # Capture the prior room-vent map BEFORE overwriting it — the removed
        # loop below needs the vent list for rooms that are no longer in
        # ``new_active_map`` (otherwise ``self._room_vents.get(removed_id)``
        # returns [] and the close path is silently a no-op).
        prev_room_vents = self._room_vents
        self._active_rooms = new_active_map
        self._room_vents = new_room_vents

        # Rooms this call has fully joined to the cycle — row persisted, vents
        # opened, entry published into ``_room_cycle_states`` (#615). Both the
        # fresh-start loop and the mid-cycle ``added`` branch fill it, and the
        # two loops that run afterwards read it: the tail vent-open loop skips
        # these rooms (their vents were opened moments ago, inside the join),
        # and the fresh-start "opened_at_start" recorder skips the rooms that
        # are *not* in it, so a room whose join failed is not credited with an
        # opening that never happened.
        joined_room_ids: set[str] = set()

        is_fresh_start = self._state == CycleState.IDLE
        if is_fresh_start:
            # Lock in the cycle direction — used by _monitor_rooms for the entire
            # cycle lifetime to avoid mid-cycle mode misdetection (issue #26).
            self._cycle_mode = hvac_mode

            # Close any open cycle logs left over from a previous server run or
            # from an exception that occurred after a prior insert but before the
            # state transition completed. Without this, restarting while a cycle
            # is active produces a second open row, showing two "Active" entries
            # in the UI for the same thermostat.
            orphaned = await db.close_open_cycle_logs(conn, self.thermostat_entity_id)
            if orphaned > 0:
                log.warning(
                    "Closed %d orphaned open cycle log(s) for %s before starting new cycle",
                    orphaned,
                    self.thermostat_entity_id,
                )
                if self._logger:
                    await self._logger.log(
                        "warning",
                        "engine",
                        f"Closed {orphaned} orphaned cycle log(s) for {self.thermostat_entity_id} "
                        f"(stale from previous server run or failed state transition)",
                        {
                            "thermostat": self.thermostat_entity_id,
                            "orphaned_count": orphaned,
                        },
                    )

            # Close open cycles on OTHER thermostats that contain any of the
            # rooms we're about to include.  This prevents the same room from
            # appearing in two simultaneous cycles (e.g. after a room is
            # reassigned between thermostats).  (Issue #48 Bug 4)
            incoming_room_ids = list(new_active_map.keys())
            cross_closed = await db.close_open_cycles_for_rooms(
                conn,
                incoming_room_ids,
                exclude_thermostat=self.thermostat_entity_id,
            )
            if cross_closed > 0:
                log.warning(
                    "Closed %d open cycle(s) on other thermostats containing rooms %s",
                    cross_closed,
                    incoming_room_ids,
                )
                if self._logger:
                    await self._logger.log(
                        "warning",
                        "engine",
                        f"Closed {cross_closed} open cycle(s) on other thermostats "
                        f"that contained rooms being added to new cycle on "
                        f"{self.thermostat_entity_id}",
                        {
                            "thermostat": self.thermostat_entity_id,
                            "cross_closed_count": cross_closed,
                            "room_ids": incoming_room_ids,
                        },
                    )

            # Start a fresh cycle
            rooms_snapshot = {
                ar.room.id: {
                    "name": ar.room.name,
                    "target": ar.target_temp,
                    # #408: persist the pre-Eco ask too, so a restart can
                    # rebuild requested/effective instead of conflating them.
                    "requested_target": _requested_of(ar),
                    "source": ar.source,
                }
                for ar in new_active_map.values()
            }
            # Capture starting thermostat temp, setpoint, and vent states BEFORE
            # the engine opens any vents or writes a new setpoint.
            thermo_temp_start, thermo_setpoint_start = self._read_thermo_temp_and_setpoint()
            vents_at_start_json = self._snapshot_vent_states_json(
                [v for vl in self._room_vents.values() for v in vl]
            )
            outside_temp_start = await self._read_outside_temp(conn)

            self._cycle_log = CycleLog.create(
                thermostat_entity_id=self.thermostat_entity_id,
                mode=hvac_mode,
                rooms_json=json.dumps(rooms_snapshot),
            )
            self._cycle_log.thermostat_temp_at_start = thermo_temp_start
            self._cycle_log.setpoint_at_start = thermo_setpoint_start
            self._cycle_log.vents_at_start = vents_at_start_json
            self._cycle_log.outside_temp_at_start = outside_temp_start
            await db.insert_cycle_log(conn, self._cycle_log)
            # Transition to RUNNING immediately after the DB insert so that if
            # any subsequent await raises, the next tick takes the running-cycle
            # path rather than treating the engine as IDLE and inserting again.
            self._state = CycleState.RUNNING
            self._room_cycle_states = {}
            for ar in new_active_map.values():
                # Per-room unit of work: persist, vent, publish — and on
                # failure, leave the room out of both maps so this tick's
                # _monitor_rooms pass repairs it (#615). One room's locked-DB
                # write no longer abandons its siblings mid-loop.
                if await self._join_room_to_cycle(
                    conn, ar, cycle_id=self._cycle_log.id, joined_at=None
                ):
                    joined_room_ids.add(ar.room.id)
            # Record "opened_at_start" vent events for every vent in the fresh
            # cycle so the diagnostics view shows the initial open actions.
            now_ts = datetime.now(UTC)
            for room_id, vents in self._room_vents.items():
                if room_id not in joined_room_ids:
                    # This room's join failed above, so its vents were never
                    # opened — recording "opened_at_start" for them would put a
                    # lie in the diagnostics stream (#615). Nothing is lost from
                    # the timeline by skipping: the repair records the room's
                    # real opening as "opened_at_repair" when it heals it, at
                    # the moment it actually happens.
                    continue
                for v in vents:
                    try:
                        await db.insert_cycle_vent_event(
                            conn,
                            self._cycle_log.id,
                            now_ts,
                            v.entity_id,
                            room_id,
                            "opened_at_start",
                            None,
                        )
                    except Exception as exc:
                        log.debug("Failed to record opened_at_start event: %s", exc)
            room_names = [ar.room.name for ar in new_active_map.values()]
            log.info(
                "Cycle started for %s — mode=%s rooms=%s",
                self.thermostat_entity_id,
                hvac_mode,
                room_names,
            )
            if self._logger:
                await self._logger.log(
                    "info",
                    "engine",
                    f"Cycle started for {self.thermostat_entity_id} — mode={hvac_mode}, rooms={room_names}",
                    {
                        "thermostat": self.thermostat_entity_id,
                        "mode": hvac_mode,
                        "cycle_id": self._cycle_log.id,
                        "rooms": room_names,
                    },
                )
        else:
            # Update existing cycle (rooms changed mid-cycle)
            assert self._cycle_log is not None
            for room_id in added:
                ar = new_active_map[room_id]
                if not await self._join_room_to_cycle(
                    conn, ar, cycle_id=self._cycle_log.id, joined_at=datetime.now(UTC)
                ):
                    # The join failed as a unit — no row, no vent, no map
                    # entry. Skip the rest of this room's bookkeeping (the
                    # overflow eviction below is only correct once the upsert
                    # has actually flipped the row's role) and let
                    # _monitor_rooms repair the room (#615).
                    continue
                joined_room_ids.add(room_id)
                # If this room was being held open as overflow, it is now a full
                # active participant. Evict it from the overflow bookkeeping so
                # the overflow management / cycle-end finalize don't later
                # overwrite its active data point with overflow close state
                # (Issue #300). The DB row's role was just flipped to 'active' by
                # the upsert inside the join above.
                self._overflow_room_states.pop(room_id, None)
                self._overflow_room_ids.discard(room_id)
                log.info("Room %s added to running cycle", ar.room.name)
                if self._logger:
                    await self._logger.log(
                        "info",
                        "engine",
                        f"Room {ar.room.name} added to running cycle (source: {ar.source})",
                        {
                            "room_id": room_id,
                            "room_name": ar.room.name,
                            "source": ar.source,
                            "target_temp": ar.target_temp,
                        },
                    )

            for room_id in removed:
                # Close vents for removed rooms, respecting the airflow floor.
                # Previously closed directly via ha.close_cover(), bypassing the
                # VentController safety check (issue #26 Bug 3).  ``vents``
                # comes from the captured ``prev_room_vents`` because
                # ``self._room_vents`` has already been overwritten with the
                # new active set above (#210).
                vents = prev_room_vents.get(room_id, [])
                if vents:
                    # The airflow floor must be judged against EVERY smart vent
                    # on the zone — active, removed, and idle alike. A partial
                    # pool credits absent idle rooms' closed smart vents as
                    # always-open passive registers and deflates the floor (#421).
                    all_zone_vents_now = await self._get_all_zone_vents(conn)
                    required = required_open_vents(tc, len(all_zone_vents_now))
                    open_count = self._vent._count_open_vents(all_zone_vents_now)
                    would_close = sum(1 for v in vents if self._vent._is_open(v))
                    can_close = (open_count - would_close) >= required
                    if not can_close:
                        log.warning(
                            "Cannot close removed room %s vents — airflow floor requires %d open",
                            room_id,
                            required,
                        )
                        if self._logger:
                            await self._logger.log(
                                "warning",
                                "engine",
                                f"Removed room {room_id} vents kept open — closing them would "
                                f"drop the zone below the airflow floor (requires {required} "
                                "open). Add more smart vents or lower the minimum-open "
                                "fraction to allow closure.",
                                {
                                    "thermostat": self.thermostat_entity_id,
                                    "room_id": room_id,
                                    "required_open_vents": required,
                                    "open_count": open_count,
                                    "would_close": would_close,
                                },
                            )
                    if can_close:
                        await self._vent.force_close_vents(vents)
                self._room_cycle_states.pop(room_id, None)
                log.info("Room %s removed from cycle (became idle)", room_id)
                if self._logger:
                    await self._logger.log(
                        "info",
                        "engine",
                        f"Room {room_id} removed from cycle (became idle)",
                        {"room_id": room_id},
                    )

            # Persisting rooms whose trigger changed — apply the new source /
            # target in place so the cycle keeps running (#215). Updating the
            # RoomCycleState target_temp is what makes _monitor_rooms (which
            # checks against rcs.target_temp) honour the new target; the
            # setpoint is re-derived from self._active_rooms by the
            # _set_thermostat_setpoint call at the end of this method.
            for room_id in changed:
                ar = new_active_map[room_id]
                changed_rcs: RoomCycleState | None = self._room_cycle_states.get(room_id)
                if changed_rcs is not None:
                    changed_rcs.target_temp = ar.target_temp
                    # Eco Mode (Issue #404): re-record the relaxed target when a
                    # room's trigger changes in place mid-cycle.
                    eco_kwargs = self._rcs_eco_kwargs(ar)
                    changed_rcs.requested_target = eco_kwargs["requested_target"]
                    changed_rcs.effective_target = eco_kwargs["effective_target"]
                    changed_rcs.eco_active = eco_kwargs["eco_active"]
                    trigger_detail = await self._build_trigger_detail(conn, ar)
                    changed_rcs.trigger_detail = (
                        json.dumps(trigger_detail) if trigger_detail else None
                    )
                    await db.upsert_room_cycle_state(conn, changed_rcs)
                log.info(
                    "Room %s trigger updated in place — source=%s, target=%.1f°F",
                    ar.room.name,
                    ar.source,
                    ar.target_temp,
                )
                if self._logger:
                    await self._logger.log(
                        "info",
                        "engine",
                        f"Room {ar.room.name} updated in place — now source={ar.source}, "
                        f"target={ar.target_temp}°F (cycle continues without a restart)",
                        {
                            "room_id": room_id,
                            "room_name": ar.room.name,
                            "source": ar.source,
                            "target_temp": ar.target_temp,
                        },
                    )

            # Issue #423: new or raised demand invalidates the min-runtime
            # hold's premise ("every room is satisfied; the cycle is only
            # burning minimum runtime"). Without this, the hold exit
            # terminated the cycle against the new room's live demand and the
            # off-time lockout (#208) then blocked the restart it needed.
            # Rooms already at their (possibly new) target keep the hold.
            if (
                self._cycle_log is not None
                and self._cycle_log.in_min_runtime_hold
                and (added or changed)
            ):
                unsatisfied: list[str] = []
                for room_id in added | changed:
                    joined = new_active_map.get(room_id)
                    if joined is None:
                        continue
                    avg = self._get_avg_temp(joined.room)
                    if avg is None:
                        continue
                    if not _is_at_target(
                        avg + joined.room.temp_offset, joined.target_temp, hvac_mode
                    ):
                        unsatisfied.append(joined.room.name)
                if unsatisfied:
                    await self._release_min_runtime_hold(
                        conn,
                        reason="demand joined or changed mid-hold: " + ", ".join(unsatisfied),
                    )

            # Keep the cycle log's room snapshot current after any mid-cycle
            # change so /api/logs reflects reality rather than cycle-start
            # state. Merge into the existing snapshot rather than rebuilding it:
            # a room removed mid-cycle keeps its entry (its RoomCycleState row
            # still belongs to this cycle and the detail view looks up name /
            # source here), and a room added mid-cycle gains one.
            if added or removed or changed:
                try:
                    rooms_snapshot = json.loads(self._cycle_log.rooms_json or "{}")
                except (ValueError, TypeError):
                    rooms_snapshot = {}
                if not isinstance(rooms_snapshot, dict):
                    rooms_snapshot = {}
                for ar in new_active_map.values():
                    rooms_snapshot[ar.room.id] = {
                        "name": ar.room.name,
                        "target": ar.target_temp,
                        "requested_target": _requested_of(ar),
                        "source": ar.source,
                    }
                self._cycle_log.rooms_json = json.dumps(rooms_snapshot)
                await db.update_cycle_log_rooms(
                    conn, self._cycle_log.id, self._cycle_log.rooms_json
                )

        # Open all active room vents. Rooms joined by this same call already
        # had theirs opened inside the join (#615) — re-issuing here would
        # double every open_cover on a fresh start, because HA's cached cover
        # state has not caught up within a single tick and
        # ``open_room_vents``' fully-open skip therefore cannot dedupe them.
        for room_id in self._active_rooms:
            if room_id in joined_room_ids:
                continue
            active_rcs: RoomCycleState | None = self._room_cycle_states.get(room_id)
            if active_rcs and active_rcs.vent_closed_at is None:
                vents = self._room_vents.get(room_id, [])
                await self._vent.open_room_vents(vents)

        # Close vents for idle rooms on this zone (fresh cycle start only).
        #
        # _terminate_cycle re-opens ALL zone vents at the end of every cycle so
        # the system returns to a neutral idle state.  Without this step, those
        # vents stay open into the next cycle even when their rooms have no
        # active demand, diluting airflow and defeating zone-based vent control.
        # This is the mirror of the mid-cycle room-removal logic (see `removed`
        # loop above) but applied once at cycle start.  (Issue #67)
        # Vacation safety cycles skip it on purpose (#626): nobody is home, so
        # every room is a legitimate destination for the surplus air and the
        # zone runs wide open. `_apply_vacation_vent_policy` then closes each
        # idle room individually, but only once that room nears its OWN
        # opposite safety bound.
        if is_fresh_start and not self._vacation_cycle:
            # We captured is_fresh_start=True before mutating self._state so
            # this block only runs when starting a brand-new cycle, not when
            # rooms are added/removed mid-cycle.
            await self._close_idle_room_vents(conn, tc)

        # Set thermostat setpoint. Tag the setpoint-history reason so the cycle
        # detail view shows *why* it moved — in particular surfacing a #215
        # in-place trigger update, which otherwise leaves no mark on the cycle.
        if is_fresh_start:
            setpoint_reason = None
        elif changed:
            setpoint_reason = "trigger updated in place"
        elif added or removed:
            setpoint_reason = "rooms changed"
        else:
            setpoint_reason = None
        await self._set_thermostat_setpoint(
            tc, hvac_mode, conn=conn, setpoint_reason=setpoint_reason
        )

    async def _join_room_to_cycle(
        self,
        conn: aiosqlite.Connection,
        ar: ActiveRoom,
        *,
        cycle_id: str,
        joined_at: datetime | None,
    ) -> RoomCycleState | None:
        """Persist, vent, and only then publish one room's ``RoomCycleState`` (#615).

        Both cycle-join paths in ``_start_or_update_cycle`` go through here —
        the fresh-start loop (``joined_at=None``) and the mid-cycle ``added``
        branch (``joined_at=now``) — so they uphold the same invariant
        ``_repair_missing_room_state`` upholds for its own entries (#603): **an
        entry in ``_room_cycle_states`` means the room has a persisted row and
        its vents have been opened.**

        Why the ordering is load-bearing. ``_monitor_rooms`` offers the #427
        repair only to a room it finds *missing* from ``_room_cycle_states``,
        so an entry published before the write did not merely race the write —
        it made a half-joined room permanently invisible to the healer. And on
        a fresh start the vents used to be opened by a tail loop *after* the
        per-room loop, so a single ``OperationalError("database is locked")``
        (#286) aborted the method and left **every** room in the zone
        published-but-ventless: monitored, blocking termination, and driving
        the setpoint from behind a shut damper until ``cycle_timeout_hours``
        (3 h by default). Reordering the publish alone would not have fixed
        that — the room whose upsert *succeeded* legitimately belongs in the
        map and would still never have been vented. Opening the vents here,
        inside the per-room unit of work, is what makes the invariant true for
        the rooms that succeed as well as for the ones that fail.

        Returns the published state, or ``None`` when the join failed. On
        failure both maps are exactly as this call found them, so the caller
        skips the room's remaining bookkeeping and the next ``_monitor_rooms``
        pass repairs it — in the common transient-lock case that is the *same*
        tick, since ``_do_tick`` monitors immediately after
        ``_start_or_update_cycle``.

        The failure is logged to the module logger only, deliberately, matching
        ``_repair_missing_room_state``'s except arm. Three reasons: the repair
        announces itself loudly to the event log on the very next pass, which
        is the same tick whenever the lock was transient; an ``EventLogger``
        call here would be the one thing in this arm that can raise (its
        ``json.dumps(details)`` sits outside its own try), and an exception
        escaping *here* would abandon the sibling rooms — the exact failure
        #615 exists to remove; and in the only case the extra line would add
        signal (a lock that persists, so the repair fails too) the event
        logger's own DB write is failing and being swallowed, so it would not
        reach the Live Feed anyway.
        """
        try:
            trigger_detail = await self._build_trigger_detail(conn, ar)
            rcs = RoomCycleState(
                cycle_id=cycle_id,
                room_id=ar.room.id,
                target_temp=ar.target_temp,
                temp_at_start=self._get_avg_temp(ar.room),
                trigger_detail=json.dumps(trigger_detail) if trigger_detail else None,
                joined_at=joined_at,
                **self._rcs_eco_kwargs(ar),
            )
            await db.upsert_room_cycle_state(conn, rcs)
            # ``open_room_vents`` swallows per-vent HA errors internally, so
            # an unreachable cover cannot roll this join back: every failure
            # that realistically reaches the except arm is one of the DB calls
            # above it, with nothing yet written. And even if the vent step did
            # raise, the repair's upsert is idempotent, so the worst case is a
            # persisted row rewritten and the vents opened on the next pass —
            # never a room the engine believes it is serving but is not.
            await self._vent.open_room_vents(self._room_vents.get(ar.room.id, []))
            # Publish last: this entry is also _monitor_rooms' retry gate.
            self._room_cycle_states[ar.room.id] = rcs
            return rcs
        except Exception as exc:
            log.error(
                "Failed to join room %s to the cycle — leaving it out of the "
                "cycle-state map so the next monitor pass repairs it: %s",
                ar.room.name,
                exc,
            )
            return None

    async def _close_idle_room_vents(
        self,
        conn: aiosqlite.Connection,
        tc: ThermostatConfig,
    ) -> None:
        """Close vents for rooms on this zone that are NOT in ``self._active_rooms``.

        Used at fresh cycle start (see ``_start_or_update_cycle``) and on
        restart recovery (see ``restore_from_db``).  ``_terminate_cycle``
        re-opens every zone vent at cycle end, so any tick that resumes a
        running cycle (including DB restore after a reboot) must re-assert
        closure on idle-room vents.  Otherwise they stay open into the cycle
        and dilute airflow to the active rooms.  (Issue #67 / reboot regression)
        """
        all_zone_rooms = await db.get_rooms_for_thermostat(conn, self.thermostat_entity_id)
        active_ids = set(self._active_rooms.keys())
        # The floor must be judged against the WHOLE zone's smart vents, not
        # just the active rooms plus the one room being closed — a partial
        # pool counts the other idle rooms' closed smart vents as always-open
        # passive registers and deflates the floor (#421).
        all_zone_vents = await self._get_all_zone_vents(conn)
        for zone_room in all_zone_rooms:
            if zone_room.id in active_ids:
                continue
            if zone_room.id in self._overflow_room_ids:
                # Overflow rooms (#237) are deliberately open during the
                # min-runtime hold despite not being active — closing them
                # here (the restore path repopulates the overflow set before
                # calling this) silently defeated overflow conditioning (#422).
                continue
            idle_vents = await db.get_room_vents(conn, zone_room.id)
            if not idle_vents:
                continue
            required = required_open_vents(tc, len(all_zone_vents))
            open_count = self._vent._count_open_vents(all_zone_vents)
            would_close = sum(1 for v in idle_vents if self._vent._is_open(v))
            can_close = (open_count - would_close) >= required
            if not can_close:
                log.warning(
                    "Cannot close idle room %s vents — airflow floor requires %d open",
                    zone_room.name,
                    required,
                )
                if self._logger:
                    await self._logger.log(
                        "warning",
                        "engine",
                        f"Idle room {zone_room.name} vents kept open — closing them would drop "
                        f"the zone below the airflow floor (requires {required} open). Add "
                        f"more smart vents or lower the minimum-open fraction to allow closure.",
                        {
                            "thermostat": self.thermostat_entity_id,
                            "room": zone_room.name,
                            "required_open_vents": required,
                            "open_count": open_count,
                            "would_close": would_close,
                        },
                    )
            if can_close:
                await self._vent.force_close_vents(idle_vents)
                log.info(
                    "Closed idle room %s vents for %s",
                    zone_room.name,
                    self.thermostat_entity_id,
                )
                if self._logger:
                    await self._logger.log(
                        "info",
                        "engine",
                        f"Closed idle room {zone_room.name} vents for {self.thermostat_entity_id}",
                        {
                            "thermostat": self.thermostat_entity_id,
                            "room_id": zone_room.id,
                            "room_name": zone_room.name,
                            "cycle_id": self._cycle_log.id if self._cycle_log else None,
                        },
                    )

    async def _get_all_zone_vents(self, conn: aiosqlite.Connection) -> list[RoomVent]:
        """Every smart vent on this thermostat's zone — active AND idle rooms.

        The airflow floor (#213) credits ``total_vents_count − <smart vents in
        the list>`` as always-open passive (dumb) registers. Passing a partial
        list (e.g. only the active cycle's rooms) makes the closed smart vents
        of idle rooms count as passive/open, silently deflating the floor
        (#421). Every floor computation must see the whole zone.
        """
        vents: list[RoomVent] = []
        for room in await db.get_rooms_for_thermostat(conn, self.thermostat_entity_id):
            vents.extend(await db.get_room_vents(conn, room.id))
        return vents

    async def _monitor_rooms(self, conn: aiosqlite.Connection, hvac_mode: str) -> None:
        tc = await db.get_thermostat_config(conn, self.thermostat_entity_id)
        all_zone_vents = await self._get_all_zone_vents(conn)

        # Safety: check max vent closed duration
        force_reopened_rooms = await self._vent.check_max_closed_duration(
            conn,
            self._room_vents,
            self._room_cycle_states,
            tc,
        )
        # Mirror any force-reopens into the cycle diagnostics stream.
        if force_reopened_rooms and self._cycle_log:
            ts = datetime.now(UTC)
            for room_id in force_reopened_rooms:
                for v in self._room_vents.get(room_id, []):
                    try:
                        await db.insert_cycle_vent_event(
                            conn,
                            self._cycle_log.id,
                            ts,
                            v.entity_id,
                            room_id,
                            "force_reopened_max_closed",
                            f"exceeded max_vent_closed_min={tc.max_vent_closed_min}",
                        )
                    except Exception as exc:
                        log.debug("Failed to record force_reopened event: %s", exc)

        # Per-tick temperature sampling — lets the UI draw a chart per room of
        # the temperature trajectory alongside the thermostat reading.
        if self._cycle_log:
            sample_ts = datetime.now(UTC)
            thermo_cur, thermo_sp = self._read_thermo_temp_and_setpoint()
            for room_id, ar in self._active_rooms.items():
                try:
                    await db.insert_cycle_temp_sample(
                        conn,
                        self._cycle_log.id,
                        room_id,
                        sample_ts,
                        self._get_avg_temp(ar.room),
                        thermo_cur,
                        thermo_sp,
                    )
                except Exception as exc:
                    log.debug("Failed to record temp sample: %s", exc)

        # Minimum cycle runtime hold (Issue #208). If every room in the cycle
        # has reached target but the cycle has not yet run min_cycle_runtime_min,
        # hold it open rather than completing a too-short cycle: re-open ALL of
        # the cycle's vents so the air handler keeps a full duct path (no
        # high-static-pressure dead-heading through whichever room finished
        # last) and the unavoidable overshoot is distributed evenly across the
        # rooms that were part of the cycle. The cycle terminates normally once
        # the runtime clock is satisfied. With min_cycle_runtime_min = 0 the
        # guard is disabled and this never fires.
        if (
            self._active_rooms
            and not self._cycle_runtime_satisfied(tc)
            and self._all_active_rooms_satisfied(hvac_mode)
        ):
            await self._enter_min_runtime_hold(conn)
            await self._apply_overflow_during_hold(conn, hvac_mode, tc)
            return

        # Issue #237: once a cycle is in the minimum-runtime hold, the per-room
        # close-vent loop below must NOT run. The hold has just re-opened those
        # vents (and possibly opened overflow-destination vents in non-active
        # rooms); the close loop sees the satisfied active rooms as "at target,
        # vent_closed_at is None" and would close them again next tick,
        # producing open/close churn until the hold expires.
        if self._cycle_log is not None and self._cycle_log.in_min_runtime_hold:
            # Issue #423: a room can drift back OUT of its comfort band while
            # the hold burns runtime. Past its deadband the hold's premise
            # ("every room is satisfied") is false — release the hold and fall
            # through to normal monitoring so the cycle keeps running until
            # the room re-reaches target. Drift *within* the deadband keeps
            # the hold (hysteresis — otherwise the hold would flap).
            drifted = self._rooms_drifted_past_deadband(hvac_mode, tc)
            if drifted:
                await self._release_min_runtime_hold(
                    conn,
                    reason="drifted past deadband during hold: " + ", ".join(drifted),
                )
            elif self._cycle_runtime_satisfied(tc):
                # Hold satisfied — terminate. Within-deadband drift does not
                # block termination (the cycle has done its minimum runtime;
                # equilibrium will settle out idle).
                await self._terminate_cycle(conn)
                return
            else:
                await self._apply_overflow_during_hold(conn, hvac_mode, tc)
                return

        all_at_target = True
        # Track WHY termination is blocked: when the only blockers are
        # at-target rooms whose vent close was deferred by the airflow floor,
        # the cycle may still terminate (see the check after the loop).
        any_floor_deferred = False
        blocked_only_by_floor = True
        for room_id, ar in self._active_rooms.items():
            rcs = self._room_cycle_states.get(room_id)
            if rcs is None:
                # Self-repair (#427): a room can be in the active map with no
                # RoomCycleState when an exception hit _start_or_update_cycle
                # between committing the map and the per-room work (a locked
                # DB is the known case, #286). The retry never redoes it —
                # `added` is computed against the already-updated map — so
                # without repair the room is never conditioned or monitored
                # and the missing rcs blocks termination until the cycle
                # timeout. Recreate the state and open the room's vents.
                rcs = await self._repair_missing_room_state(conn, ar)
                if rcs is None:
                    all_at_target = False
                    blocked_only_by_floor = False
                    continue

            avg = self._get_avg_temp(ar.room)
            if avg is None:
                # Room has no sensors (ventless/sensor-only room with no readings).
                # Skip it for target-check purposes — it cannot block cycle termination.
                log.warning(
                    "Room %s has no available sensors — skipping target check",
                    ar.room.name,
                )
                continue

            # Apply per-room offset: positive offset compensates for post-closure
            # drift (e.g. a room that overcools by 3°F gets offset=+3 so its vent
            # closes 3°F before the actual target, and it drifts to target).
            effective_avg = avg + ar.room.temp_offset
            at_target = _is_at_target(effective_avg, rcs.target_temp, hvac_mode)

            if rcs.vent_closed_at is not None:
                # Room already served this cycle (its vent is closed). #86: a
                # served room's small re-crossing of the target is hysteresis
                # noise and must NOT block termination — otherwise the cycle
                # never ends. But while a SLOWER co-active room keeps the cycle
                # running, a served room can heat-/cool-soak far past its
                # target with its vent shut and nothing reopens it until the
                # cycle timeout. #423 already re-engages a drifted served room
                # — but only inside the minimum-runtime hold (which is reached
                # only once every room is satisfied). Generalize it here: once
                # a served room drifts more than a FULL deadband past its
                # (possibly eco-relaxed) target it has live demand again —
                # reopen its vent and resume serving it in this cycle. The
                # deadband is the hysteresis (it must climb a full band past
                # target to re-engage, then return to target to re-close), so
                # this cannot flap; within-band drift stays "served" per #86.
                # The pure-target close decision (#70) is untouched.
                band = _effective_deadband(ar.room, tc.deadband, ar.deadband_override)
                if _drifted_past_deadband(effective_avg, rcs.target_temp, hvac_mode, band):
                    await self._reopen_drifted_room(conn, room_id, ar, rcs, avg, band)
                    all_at_target = False
                    blocked_only_by_floor = False
                continue

            if at_target and rcs.vent_closed_at is None:
                # Try to close the vent.
                # Bug 6: if this is the last room whose vent still needs to close and
                # the airflow floor would block the close, bypass the constraint and
                # close anyway — leaving it open would deadlock the cycle forever.
                # "Last vent needing close" covers both single-room zones AND multi-room
                # zones where all other rooms have already had their vents closed.
                vents = self._room_vents.get(room_id, [])
                is_last_vent_to_close = (
                    sum(1 for r in self._room_cycle_states.values() if r.vent_closed_at is None)
                    == 1
                )
                required = required_open_vents(tc, len(all_zone_vents))
                if is_last_vent_to_close and required > 0:
                    open_count = self._vent._count_open_vents(all_zone_vents)
                    would_close = sum(1 for v in vents if self._vent._is_open(v))
                    if open_count - would_close < required:
                        log.warning(
                            "Room %s is last vent needing close — bypassing airflow floor to terminate",
                            ar.room.name,
                        )
                        if self._logger:
                            await self._logger.log(
                                "warning",
                                "engine",
                                f"Room {ar.room.name} is the last vent needing close — "
                                f"bypassing the airflow floor (required={required}) to terminate cycle",
                                {
                                    "room_id": room_id,
                                    "room_name": ar.room.name,
                                    "required_open_vents": required,
                                },
                            )
                        # Guarded + error-contained (#424): a raise here
                        # previously failed the whole tick.
                        await self._vent.force_close_vents(vents)
                        closed = True
                    else:
                        closed = await self._vent.close_room_vents(
                            vents, all_zone_vents, tc, self._room_cycle_states
                        )
                else:
                    closed = await self._vent.close_room_vents(
                        vents, all_zone_vents, tc, self._room_cycle_states
                    )
                if closed:
                    rcs.vent_closed_at = datetime.now(UTC)
                    if rcs.reached_at is None:
                        rcs.reached_at = datetime.now(UTC)
                    rcs.temp_at_end = avg
                    await db.upsert_room_cycle_state(conn, rcs)
                    if self._cycle_log:
                        ts = rcs.vent_closed_at
                        for v in vents:
                            try:
                                await db.insert_cycle_vent_event(
                                    conn,
                                    self._cycle_log.id,
                                    ts,
                                    v.entity_id,
                                    room_id,
                                    "closed_reached_target",
                                    f"avg={avg:.1f} target={rcs.target_temp}",
                                )
                            except Exception as exc:
                                log.debug("Failed to record closed_reached_target event: %s", exc)
                    offset_note = (
                        f", offset={ar.room.temp_offset:+.1f}°F" if ar.room.temp_offset != 0 else ""
                    )
                    log.info(
                        "Room %s hit target %.1f°F (avg=%.1f, effective=%.1f%s) — vent closed",
                        ar.room.name,
                        rcs.target_temp,
                        avg,
                        effective_avg,
                        offset_note,
                    )
                    if self._logger:
                        await self._logger.log(
                            "info",
                            "engine",
                            f"Room {ar.room.name} reached target {rcs.target_temp}°F "
                            f"(avg={avg:.1f}°F, effective={effective_avg:.1f}°F{offset_note}) — vent closed",
                            {
                                "room_id": room_id,
                                "room_name": ar.room.name,
                                "target_temp": rcs.target_temp,
                                "avg_temp": avg,
                                "effective_avg": effective_avg,
                                "temp_offset": ar.room.temp_offset,
                            },
                        )
                else:
                    all_at_target = False  # deferred by the airflow floor
                    any_floor_deferred = True
            elif not at_target and rcs.vent_closed_at is None:
                all_at_target = False
                blocked_only_by_floor = False

        if self._active_rooms and (all_at_target or (any_floor_deferred and blocked_only_by_floor)):
            # Either every room is served, or every room is AT TARGET and the
            # only thing standing between the cycle and termination is the
            # airflow floor refusing the final vent closes. With the floor
            # judged zone-wide (#421), a multi-room cycle can legitimately
            # reach that state (no room may close without dropping below the
            # floor) — previously it idled at temperature until the cycle
            # timeout while the HVAC kept running. Terminate directly: the
            # open vents already ARE the post-termination idle state
            # (_terminate_cycle re-opens the zone anyway), and stopping the
            # HVAC at target is strictly protective. The floor is never
            # violated — the deferred closes simply never happen.
            await self._terminate_cycle(conn)

    async def _reopen_drifted_room(
        self,
        conn: aiosqlite.Connection,
        room_id: str,
        ar: ActiveRoom,
        rcs: RoomCycleState,
        avg: float,
        band: float,
    ) -> None:
        """Reopen a served room's vent after it drifted past its deadband.

        Clears ``vent_closed_at`` so the room is served again in the current
        cycle; the per-room monitor then keeps the vent open until the room
        re-reaches target, at which point the normal close path re-closes it.
        Opening a vent only adds airflow, so it can never breach the airflow
        floor, and the setpoint already accounts for this room's target (it
        never left ``_active_rooms``). ``reached_at`` is left intact so the
        room's original time-to-target is preserved for metrics.
        """
        vents = self._room_vents.get(room_id, [])
        if vents:
            await self._vent.open_room_vents(vents)
        rcs.vent_closed_at = None
        rcs.temp_at_end = None
        await db.upsert_room_cycle_state(conn, rcs)
        drift = abs(avg + ar.room.temp_offset - rcs.target_temp)
        log.info(
            "Room %s drifted %.1f°F past target %.1f°F (deadband %.1f°F) — "
            "vent reopened to resume serving",
            ar.room.name,
            drift,
            rcs.target_temp,
            band,
        )
        if self._cycle_log:
            ts = datetime.now(UTC)
            for v in vents:
                try:
                    await db.insert_cycle_vent_event(
                        conn,
                        self._cycle_log.id,
                        ts,
                        v.entity_id,
                        room_id,
                        "reopened_drift",
                        f"avg={avg:.1f} target={rcs.target_temp} band={band}",
                    )
                except Exception as exc:
                    log.debug("Failed to record reopened_drift event: %s", exc)
        if self._logger:
            await self._logger.log(
                "info",
                "engine",
                f"Room {ar.room.name} drifted {drift:.1f}°F past its "
                f"{rcs.target_temp}°F target (deadband {band}°F) — vent reopened "
                "to resume serving",
                {
                    "room_id": room_id,
                    "room_name": ar.room.name,
                    "target_temp": rcs.target_temp,
                    "avg_temp": avg,
                    "deadband": band,
                },
            )

    async def _repair_missing_room_state(
        self, conn: aiosqlite.Connection, ar: ActiveRoom
    ) -> RoomCycleState | None:
        """Recreate a lost ``RoomCycleState`` for an active room (#427).

        Heals the zombie-room inconsistency regardless of which path produced
        it: the room gets its cycle state (so monitoring and termination work
        again), its vents are opened (they never were), and the repair is
        loudly logged. Returns None when the repair itself fails or no cycle
        log exists — the caller then falls back to the old conservative
        "block termination" behavior for one more tick, and retries the repair
        on the next one.

        The repair is all-or-nothing with respect to its own visibility
        (#603). The invariant **this method upholds for the entries it
        publishes**: an entry in ``_room_cycle_states`` means the room has a
        persisted row and its vents have been opened. That entry is also the
        caller's retry gate — ``_monitor_rooms`` only repairs a room it finds
        *missing* from the map — so publishing before the write meant a repair
        killed by the still-locked DB (#286, the very condition that produced
        the zombie 60 s earlier) left the entry behind and no later tick ever
        retried: the room stayed monitored, blocked termination and drove the
        setpoint from behind a shut damper for the rest of the cycle. That is
        the third-order case #427 itself created — the healer poisoning its
        own retry — so the maps are touched only once every side effect below
        has succeeded, and a failed repair leaves them exactly as it found
        them.

        That invariant is no longer local to this method (#615).
        ``_start_or_update_cycle`` used to publish into ``_room_cycle_states``
        before its own upsert on both join paths, which — because this
        repair's gate is ``rcs is None`` — made a half-joined room *invisible*
        to the healer rather than reachable by it, while the fresh start's
        vents were opened only by a tail loop after that per-room loop. Both
        paths now go through ``_join_room_to_cycle``, which persists, vents,
        and only then publishes, exactly as this method does.
        """
        if self._cycle_log is None:
            return None
        try:
            trigger_detail = await self._build_trigger_detail(conn, ar)
            rcs = RoomCycleState(
                cycle_id=self._cycle_log.id,
                room_id=ar.room.id,
                target_temp=ar.target_temp,
                temp_at_start=self._get_avg_temp(ar.room),
                trigger_detail=json.dumps(trigger_detail) if trigger_detail else None,
                joined_at=datetime.now(UTC),
                **self._rcs_eco_kwargs(ar),
            )
            # Read the vents BEFORE the first write (#603). Both DB calls are
            # live during the same lock window, and ordering the read first
            # means a failed vent lookup also leaves no persisted row for a
            # room the engine is not yet serving — the two poison sites roll
            # back identically.
            vents = self._room_vents.get(ar.room.id)
            if vents is None:
                vents = await db.get_room_vents(conn, ar.room.id)
            await db.upsert_room_cycle_state(conn, rcs)
            # ``open_room_vents`` swallows per-vent HA errors internally, so an
            # unreachable cover cannot spuriously roll the repair back. Every
            # failure that *can* reach the except arm — the vent read and the
            # upsert, both DB calls — happens above this line, so a rollback
            # always means nothing was persisted.
            await self._vent.open_room_vents(vents)
            # Record the opening in the cycle diagnostics stream. The
            # fresh-start recorder deliberately skips rooms whose join failed
            # (#615) — crediting them with an "opened_at_start" their vents
            # never received would put a lie in the vent timeline — so without
            # this line a repaired room's real opening would simply be *absent*
            # from `/api/logs/{cycle_id}/detail` and the vent-timeline chart for
            # the whole cycle. A silent gap traded for a lie is the same defect
            # class either way; recording it where it actually happens is the
            # only answer that is true. Best-effort and individually guarded,
            # exactly like every other vent-event recorder in this file:
            # diagnostics may never abort the repair they describe, and this
            # method's rollback contract (nothing published on failure) depends
            # on that.
            repaired_at = datetime.now(UTC)
            for v in vents:
                try:
                    await db.insert_cycle_vent_event(
                        conn,
                        self._cycle_log.id,
                        repaired_at,
                        v.entity_id,
                        ar.room.id,
                        "opened_at_repair",
                        "cycle-join interrupted mid-write; vents opened by the repair (#427)",
                    )
                except Exception as exc:
                    log.debug("Failed to record opened_at_repair event: %s", exc)
            log.warning(
                "Repaired missing cycle state for active room %s — its earlier "
                "cycle-join was interrupted mid-write (see #427)",
                ar.room.name,
            )
            if self._logger:
                await self._logger.log(
                    "warning",
                    "engine",
                    f"Repaired missing cycle state for room {ar.room.name} — its earlier "
                    "cycle-join was interrupted before the room was fully registered; "
                    "the room is now monitored and its vents opened.",
                    {
                        "room_id": ar.room.id,
                        "room_name": ar.room.name,
                        "cycle_id": self._cycle_log.id,
                    },
                )
            # Publish last — after every await in this block, announcement
            # included. ``EventLogger.log`` is documented never to raise, but
            # its ``json.dumps(details)`` sits outside its own try, so keeping
            # the two writes below the announcement is what makes the
            # except-arm comment unconditionally true rather than true only by
            # the logger's contract.
            self._room_vents[ar.room.id] = vents
            self._room_cycle_states[ar.room.id] = rcs
            return rcs
        except Exception as exc:
            # Nothing was published above, so both maps are exactly as this
            # call found them and the next tick's rcs-is-None gate retries the
            # repair instead of adopting a half-repaired room (#603).
            log.error("Failed to repair missing room state for %s: %s", ar.room.name, exc)
            return None

    async def _terminate_cycle(self, conn: aiosqlite.Connection, reason: str = "completed") -> None:
        log.info("All rooms at target for %s — terminating cycle", self.thermostat_entity_id)
        self._state = CycleState.TERMINATING

        # Capture end-of-cycle diagnostics before any state mutations or HA calls.
        thermo_temp_end, thermo_setpoint_end = self._read_thermo_temp_and_setpoint()
        vents_at_end_json = self._snapshot_vent_states_json(
            [v for vl in self._room_vents.values() for v in vl]
        )
        outside_temp_end = await self._read_outside_temp(conn)

        # Persist per-room temp_at_end for rooms that didn't hit target.
        if self._cycle_log:
            for room_id, ar in self._active_rooms.items():
                rcs = self._room_cycle_states.get(room_id)
                if rcs and rcs.temp_at_end is None:
                    rcs.temp_at_end = self._get_avg_temp(ar.room)
                    try:
                        await db.upsert_room_cycle_state(conn, rcs)
                    except Exception as exc:
                        log.debug("Failed to persist end-of-cycle room state: %s", exc)
            # Close out overflow-room data points still open at cycle end (#254).
            await self._finalize_overflow_rooms(conn)

        # Close the DB record FIRST so the cycle log is never left orphaned
        # with ended_at=NULL if subsequent vent/setpoint operations fail.
        if self._cycle_log:
            try:
                await db.close_cycle_log(
                    conn,
                    self._cycle_log.id,
                    datetime.now(UTC),
                    ended_reason=reason,
                    thermostat_temp_at_end=thermo_temp_end,
                    setpoint_at_end=thermo_setpoint_end,
                    vents_at_end=vents_at_end_json,
                    outside_temp_at_end=outside_temp_end,
                )
            except Exception as exc:
                log.error("Failed to close cycle log %s in DB: %s", self._cycle_log.id, exc)

        # Park the thermostat setpoint → HVAC shuts off AND stays off. The
        # setpoint is ambient nudged overshoot_delta to the idle side of the
        # cycle direction (see _parked_setpoint): parking exactly at ambient
        # left the thermostat armed to restart the HVAC the moment its own
        # room drifted, racing the engine's room-demand cycle start. Leave the
        # thermostat in the cycle's mode (heat/cool).
        thermo_state = self._ha.get_state(self.thermostat_entity_id)
        if thermo_state:
            ambient_f = _climate_temp_to_f(
                thermo_state.get("attributes", {}).get("current_temperature"),
                self._ha.ha_temp_unit,
            )
            if ambient_f is not None:
                try:
                    tc = await db.get_thermostat_config(conn, self.thermostat_entity_id)
                    parked = self._parked_setpoint(
                        ambient_f,
                        self._cycle_ha_mode or self._cycle_mode,
                        tc.overshoot_delta,
                    )
                    await self._ha.set_thermostat_temperature(
                        self.thermostat_entity_id,
                        parked,
                        hvac_mode=self._cycle_ha_mode,
                    )
                    self._last_setpoint_sent = parked
                    if self._logger:
                        await self._logger.log(
                            "info",
                            "engine",
                            f"Cycle terminated for {self.thermostat_entity_id} — "
                            f"setpoint parked at {parked}°F (ambient {ambient_f}°F "
                            f"offset {tc.overshoot_delta}°F to the idle side so the "
                            "thermostat cannot restart the HVAC before the engine reacts)",
                            {
                                "thermostat": self.thermostat_entity_id,
                                "setpoint": parked,
                                "ambient": ambient_f,
                                "overshoot_delta": tc.overshoot_delta,
                                "hvac_mode": self._cycle_ha_mode,
                                "cycle_id": self._cycle_log.id if self._cycle_log else None,
                            },
                        )
                except Exception as exc:
                    log.error("Failed to set termination setpoint: %s", exc)

        # Capture all zone vents before clearing state so we can re-open them.
        # self._room_vents only contains active-cycle rooms; idle rooms whose vents
        # were closed at cycle start (by _close_idle_room_vents) must also be
        # re-opened so the zone returns to a fully-open idle state (issue #244).
        all_zone_vents = [v for vl in self._room_vents.values() for v in vl]
        try:
            _active_ids = set(self._active_rooms.keys())
            for _zr in await db.get_rooms_for_thermostat(conn, self.thermostat_entity_id):
                if _zr.id not in _active_ids:
                    all_zone_vents.extend(await db.get_room_vents(conn, _zr.id))
        except Exception:
            log.exception(
                "Failed to fetch idle-room vents for %s during termination — "
                "idle vents may remain closed until the next reconcile pass",
                self.thermostat_entity_id,
            )

        self._state = CycleState.IDLE
        self._cycle_mode = None
        self._cycle_ha_mode = None
        self._cycle_log = None
        self._active_rooms = {}
        self._room_cycle_states = {}
        self._room_vents = {}
        # Clear the in-memory overflow set (Issue #237). All zone vents are
        # re-opened on termination so any per-room overflow state is moot.
        self._overflow_room_ids = set()
        # Start the compressor off-time lockout clock (Issue #208). Both normal
        # termination and abort stop the HVAC, so both arm the lockout.
        self._last_cycle_ended_at = datetime.now(UTC)

        if all_zone_vents:
            log.info(
                "Cycle complete — re-opening all zone vents for %s",
                self.thermostat_entity_id,
            )
            try:
                await self._vent.open_room_vents(all_zone_vents)
                if self._logger:
                    await self._logger.log(
                        "info",
                        "engine",
                        f"Cycle complete for {self.thermostat_entity_id} — all zone vents re-opened",
                        {"thermostat": self.thermostat_entity_id},
                    )
            except Exception as exc:
                log.error(
                    "Terminate: vent re-open failed for %s: %s",
                    self.thermostat_entity_id,
                    exc,
                )

    async def _abort_cycle(
        self,
        conn: aiosqlite.Connection,
        reason: str,
    ) -> None:
        log.warning("Aborting cycle for %s — %s", self.thermostat_entity_id, reason)
        if self._logger and self._state != CycleState.IDLE:
            await self._logger.log(
                "warning",
                "engine",
                f"Cycle aborted for {self.thermostat_entity_id} — {reason}",
                {
                    "thermostat": self.thermostat_entity_id,
                    "reason": reason,
                    "cycle_id": self._cycle_log.id if self._cycle_log else None,
                },
            )

        # Capture end-of-cycle diagnostics before any state mutations.
        thermo_temp_end, thermo_setpoint_end = self._read_thermo_temp_and_setpoint()
        vents_at_end_json = self._snapshot_vent_states_json(
            [v for vl in self._room_vents.values() for v in vl]
        )
        outside_temp_end = await self._read_outside_temp(conn)

        # Persist per-room temp_at_end for active rooms that never hit target.
        if self._cycle_log:
            for room_id, ar in self._active_rooms.items():
                rcs = self._room_cycle_states.get(room_id)
                if rcs and rcs.temp_at_end is None:
                    rcs.temp_at_end = self._get_avg_temp(ar.room)
                    try:
                        await db.upsert_room_cycle_state(conn, rcs)
                    except Exception as exc:
                        log.debug("Failed to persist abort room state: %s", exc)
            # Close out overflow-room data points still open at abort (#254).
            await self._finalize_overflow_rooms(conn)

        # Close the DB record FIRST so the cycle log is never left orphaned
        # with ended_at=NULL if subsequent vent/setpoint operations fail.
        if self._cycle_log:
            try:
                await db.close_cycle_log(
                    conn,
                    self._cycle_log.id,
                    datetime.now(UTC),
                    ended_reason=f"aborted: {reason}",
                    thermostat_temp_at_end=thermo_temp_end,
                    setpoint_at_end=thermo_setpoint_end,
                    vents_at_end=vents_at_end_json,
                    outside_temp_at_end=outside_temp_end,
                )
            except Exception as exc:
                log.error(
                    "Abort: failed to close cycle log %s in DB: %s",
                    self._cycle_log.id,
                    exc,
                )

        # Include idle-room vents (closed at cycle start) so the abort path
        # returns the zone to fully-open idle state (issue #244).
        all_vents = [v for vl in self._room_vents.values() for v in vl]
        try:
            _active_ids = set(self._active_rooms.keys())
            for _zr in await db.get_rooms_for_thermostat(conn, self.thermostat_entity_id):
                if _zr.id not in _active_ids:
                    all_vents.extend(await db.get_room_vents(conn, _zr.id))
        except Exception:
            log.exception(
                "Failed to fetch idle-room vents for %s during abort — "
                "idle vents may remain closed until the next reconcile pass",
                self.thermostat_entity_id,
            )
        try:
            if all_vents:
                await self._vent.open_room_vents(all_vents)
                if self._logger:
                    await self._logger.log(
                        "info",
                        "engine",
                        f"Cycle aborted for {self.thermostat_entity_id} ({reason}) — all zone vents re-opened",
                        {"thermostat": self.thermostat_entity_id, "reason": reason},
                    )
        except Exception as exc:
            log.error("Abort: vent operation failed for %s: %s", self.thermostat_entity_id, exc)

        # Park the thermostat setpoint so the HVAC goes idle and stays idle
        # (ambient nudged overshoot_delta to the idle side of the cycle
        # direction — see _parked_setpoint). _terminate_cycle() does this on
        # normal termination; mirroring it here ensures an aborted cycle never
        # leaves a stale active setpoint in place. On an unavailability abort
        # (#267) the ambient read comes back None and this block is skipped —
        # there is nothing to command.
        thermo_state = self._ha.get_state(self.thermostat_entity_id)
        if thermo_state:
            ambient_f = _climate_temp_to_f(
                thermo_state.get("attributes", {}).get("current_temperature"),
                self._ha.ha_temp_unit,
            )
            if ambient_f is not None:
                try:
                    tc = await db.get_thermostat_config(conn, self.thermostat_entity_id)
                    parked = self._parked_setpoint(
                        ambient_f,
                        self._cycle_ha_mode or self._cycle_mode,
                        tc.overshoot_delta,
                    )
                    await self._ha.set_thermostat_temperature(
                        self.thermostat_entity_id,
                        parked,
                        hvac_mode=self._cycle_ha_mode,
                    )
                    self._last_setpoint_sent = parked
                    if self._logger:
                        await self._logger.log(
                            "info",
                            "engine",
                            f"Cycle aborted for {self.thermostat_entity_id} — "
                            f"setpoint parked at {parked}°F (ambient {ambient_f}°F "
                            f"offset {tc.overshoot_delta}°F to the idle side)",
                            {
                                "thermostat": self.thermostat_entity_id,
                                "setpoint": parked,
                                "ambient": ambient_f,
                                "overshoot_delta": tc.overshoot_delta,
                                "hvac_mode": self._cycle_ha_mode,
                            },
                        )
                except Exception as exc:
                    log.error("Abort: failed to reset setpoint to ambient: %s: %r", exc, exc)

        self._state = CycleState.IDLE
        self._cycle_mode = None
        self._cycle_ha_mode = None
        self._cycle_log = None
        self._active_rooms = {}
        self._room_cycle_states = {}
        self._room_vents = {}
        # Clear the in-memory overflow set (Issue #237). All zone vents are
        # re-opened on termination so any per-room overflow state is moot.
        self._overflow_room_ids = set()
        # Start the compressor off-time lockout clock (Issue #208). Both normal
        # termination and abort stop the HVAC, so both arm the lockout.
        self._last_cycle_ended_at = datetime.now(UTC)

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
        """Return 'heating'|'cooling'|'off'|'unknown'.

        hvac_action is the authoritative source when the HVAC is actively
        running. When action is 'idle' (HVAC satisfied its setpoint), we fall
        back to hvac_mode — but only for unambiguous single-direction modes
        ('heat' / 'cool'). For 'heat_cool' (auto) mode the thermostat's
        direction can only be determined from the action attribute; mapping it
        to 'heating' was incorrect and caused _is_at_target() to use the wrong
        comparison direction during the idle phase of a cooling cycle.
        """
        state = self._ha.get_state(self.thermostat_entity_id)
        if state is None:
            return "unknown"
        # hvac_action is the most reliable signal
        action = str(state.get("attributes", {}).get("hvac_action", ""))
        if action in ("heating", "cooling"):
            return action
        # Fall back to hvac_mode only for unambiguous single-direction modes
        mode = state.get("state", "off")
        if mode == "heat":
            return "heating"
        if mode == "cool":
            return "cooling"
        # heat_cool (auto), off, unavailable, and anything else → "off"
        # Direction for heat_cool is determined by hvac_action only; when idle
        # it is unknown. _cycle_mode handles monitoring direction mid-cycle.
        return "off"

    def _ambient_suppression_eligible(
        self, room: Room, source: str, recently_off_schedule: bool
    ) -> bool:
        """Whether ambient presence-suppression (pre-cool/pre-heat) may engage.

        Covers the gates that do not depend on temperature (Issue #248):
        - the room opted in,
        - the demand is presence-driven (schedule/override are explicit user
          intent and are never suppressed),
        - the trigger-scope mode permits it right now.

        For ``off_schedule_only`` the room must also have *recently* come off a
        schedule — ``recently_off_schedule`` is precomputed by the caller from
        the room's schedules and its configured window (see
        ``_compute_off_schedule_flags``). ``any_presence`` ignores that flag.
        """
        if not room.ambient_suppression_enabled:
            return False
        if source != "presence":
            return False
        if room.ambient_suppression_mode == "off_schedule_only":
            return recently_off_schedule
        return True

    async def _compute_off_schedule_flags(
        self,
        conn: aiosqlite.Connection,
        active_rooms: dict[str, ActiveRoom],
        now: datetime | None = None,
    ) -> dict[str, bool]:
        """Precompute the ``off_schedule_only`` window flag per room (Issue #248).

        Only rooms that actually use the mode are queried. A room qualifies when
        a schedule block ended within the last
        ``ambient_suppression_off_schedule_window_min`` minutes. Returns a map of
        ``room_id -> bool``; absent rooms are treated as False by callers.
        """
        flags: dict[str, bool] = {}
        if now is None:
            now = datetime.now(UTC)
        for ar in active_rooms.values():
            room = ar.room
            if (
                room.ambient_suppression_enabled
                and ar.source == "presence"
                and room.ambient_suppression_mode == "off_schedule_only"
            ):
                schedules = await db.get_schedules_for_room(conn, room.id)
                gap = _seconds_since_schedule_end(schedules, now)
                flags[room.id] = (
                    gap is not None and gap <= room.ambient_suppression_off_schedule_window_min * 60
                )
        return flags

    def _suppression_vote(
        self,
        room: Room,
        effective: float,
        target: float,
        source: str,
        normal_deadband: float,
        outside_temp: float | None,
        tc: ThermostatConfig | None,
        recently_off_schedule: bool = False,
    ) -> tuple[str, bool]:
        """Return ``(vote, suppressed)`` for one room (Issue #248).

        ``vote`` is the room's HVAC demand — ``"cool"`` | ``"heat"`` | ``"off"``.
        ``suppressed`` is True when ambient presence-suppression is actively
        holding the room off: it would normally call for HVAC but is being
        allowed to coast toward target on outside air.

        With the feature inactive (disabled, non-presence source, or no outside
        reading) this collapses to the plain normal-deadband vote — no widened
        band and no hard cap — so rooms not using the feature are unaffected.

        Decision (only when the feature is active for this room):
        - **Minimum-differential gate** — only coast when the outside temp is at
          least ``min_differential`` °F past the target on the helpful side.
        - **Asymmetric widened deadband** — relax only the side being coasted
          from. The instant the room crosses the target, the coasting branch no
          longer matches and ``base`` (the normal deadband) governs the far
          side, so e.g. coasting up the room cools at ``target + normal_deadband``
          rather than ``target + widened_deadband``.
        - **Hard cap** — the thermostat min/max setpoint overrides suppression,
          forcing HVAC if the room drifts past the absolute comfort bounds.
        """
        if effective > target + normal_deadband:
            base = "cool"
        elif effective < target - normal_deadband:
            base = "heat"
        else:
            base = "off"

        vote = base
        suppressed = False

        if outside_temp is not None and self._ambient_suppression_eligible(
            room, source, recently_off_schedule
        ):
            differential = room.ambient_suppression_min_differential
            wide = max(room.ambient_suppression_deadband, normal_deadband)
            if effective < target and outside_temp >= target + differential:
                # Coast up: warm enough outside to drift up — relax heating only.
                vote = "heat" if effective < target - wide else "off"
                suppressed = vote != base
            elif effective > target and outside_temp <= target - differential:
                # Coast down: cool enough outside to drift down — relax cooling only.
                vote = "cool" if effective > target + wide else "off"
                suppressed = vote != base
            # Otherwise the differential gate is not met, or the room has crossed
            # to the far side of target — keep the normal-deadband ``base`` vote.

            # Hard cap (absolute comfort protection) — overrides suppression so a
            # coasting room never drifts past the thermostat's min/max setpoint.
            # Scoped to feature-active rooms: a room not using pre-cool/pre-heat
            # keeps its plain deadband vote with no new comfort floor.
            if tc is not None:
                if effective <= tc.min_setpoint:
                    vote, suppressed = "heat", False
                elif effective >= tc.max_setpoint:
                    vote, suppressed = "cool", False

        return vote, suppressed

    async def _infer_mode_from_room_temps(
        self,
        active_rooms: dict[str, ActiveRoom],
        deadband: float,
        thermo_state: dict | None = None,
        outside_temp: float | None = None,
        tc: ThermostatConfig | None = None,
        off_schedule_ok: dict[str, bool] | None = None,
    ) -> str:
        """Determine needed cycle direction from room temperatures vs targets.

        Returns 'cooling' | 'heating' | 'off'.  'off' means every room is
        within deadband — no cycle needed.  Mixed rooms: majority wins; ties
        go to cooling.

        Two additional guarantees beyond the basic vote:

        1. Sensor fallback — if a room's sensors have no readings (common right
           after startup before HA pushes state), the thermostat's own
           current_temperature is used as a proxy.  Without this, every room
           would be skipped and the vote returns 'off', causing the engine to
           reset the thermostat setpoint to ambient without changing its mode —
           leaving the HVAC in whatever direction it was running before.

        2. Ambient sanity check — after the room-sensor vote produces a mode,
           the result is cross-validated against the thermostat's current
           ambient.  If they directly contradict each other (e.g. vote says
           'heating' but thermostat ambient is already above every room target)
           the mode is flipped and a warning event log is written so operators
           can investigate the sensor offset or placement that caused the skew.
           This is the "ambient ± direction" check from issue #38.
        """
        # Read thermostat ambient once — used for both the fallback and sanity check.
        thermo_ambient: float | None = None
        if thermo_state:
            thermo_ambient = _climate_temp_to_f(
                thermo_state.get("attributes", {}).get("current_temperature"),
                self._ha.ha_temp_unit,
            )

        needs_cool = 0
        needs_heat = 0
        for ar in active_rooms.values():
            avg = self._get_avg_temp(ar.room)
            if avg is None:
                # Sensor readings not yet in the HA cache — use the thermostat
                # ambient as a proxy so this room still participates in the vote.
                avg = thermo_ambient
            if avg is None:
                continue  # no data at all; skip room
            effective = avg + ar.room.temp_offset
            recently_off = bool(off_schedule_ok and off_schedule_ok.get(ar.room.id))
            vote, _suppressed = self._suppression_vote(
                ar.room,
                effective,
                ar.target_temp,
                ar.source,
                _effective_deadband(ar.room, deadband, ar.deadband_override),
                outside_temp,
                tc,
                recently_off,
            )
            if vote == "cool":
                needs_cool += 1
            elif vote == "heat":
                needs_heat += 1

        if needs_cool == 0 and needs_heat == 0:
            return "off"
        if needs_cool > 0 and needs_heat == 0:
            inferred = "cooling"
        elif needs_heat > 0 and needs_cool == 0:
            inferred = "heating"
        else:
            # Mixed rooms — majority wins; ties go to cooling.
            inferred = "cooling" if needs_cool >= needs_heat else "heating"

        # Sanity check: cross-validate the room-sensor vote against the
        # thermostat's own ambient.  A contradiction (e.g. sensors vote
        # "heating" but the thermostat itself is already warmer than every room
        # target) means sensors are stale, offset-skewed, or in a microclimate.
        # Override the vote and warn so the operator can investigate.
        if thermo_ambient is not None and active_rooms:
            all_targets = [ar.target_temp for ar in active_rooms.values()]
            if inferred == "heating" and thermo_ambient > max(all_targets) + deadband:
                corrected = "cooling"
            elif inferred == "cooling" and thermo_ambient < min(all_targets) - deadband:
                corrected = "heating"
            else:
                corrected = None

            if corrected:
                log.warning(
                    "Mode contradiction for %s: room sensors voted %r but "
                    "thermostat ambient %.1f°F says %r — using %r. "
                    "Check room sensor temp_offset values or placement.",
                    self.thermostat_entity_id,
                    inferred,
                    thermo_ambient,
                    corrected,
                    corrected,
                )
                if self._logger:
                    await self._logger.log(
                        "warning",
                        "engine",
                        f"Mode contradiction for {self.thermostat_entity_id}: "
                        f"room sensors voted {inferred!r} but thermostat ambient "
                        f"{thermo_ambient:.1f}°F contradicts — overriding to {corrected!r}. "
                        f"Check room sensor temp_offset values or sensor placement.",
                        {
                            "thermostat": self.thermostat_entity_id,
                            "room_vote": inferred,
                            "corrected_mode": corrected,
                            "thermostat_ambient": thermo_ambient,
                            "targets": all_targets,
                        },
                    )
                inferred = corrected

        return inferred

    async def _filter_rooms_for_mode(
        self,
        active_rooms: dict[str, ActiveRoom],
        mode: str,
        deadband: float,
        thermo_state: dict | None,
        outside_temp: float | None = None,
        tc: ThermostatConfig | None = None,
        off_schedule_ok: dict[str, bool] | None = None,
    ) -> dict[str, ActiveRoom]:
        """Remove rooms that need the opposite direction from the cycle mode.

        Rooms within deadband or needing the same direction are kept.  Rooms
        with no sensor data are kept (benefit of the doubt).  (Issue #48 Bug 3)

        Rooms the ambient pre-cool/pre-heat feature is actively suppressing
        (Issue #248) are dropped here too: they carry no demand and must not be
        pulled into another room's cycle, so they coast with their vents at the
        resting (open) position like any idle room.
        """
        thermo_ambient: float | None = None
        if thermo_state:
            thermo_ambient = _climate_temp_to_f(
                thermo_state.get("attributes", {}).get("current_temperature"),
                self._ha.ha_temp_unit,
            )

        filtered: dict[str, ActiveRoom] = {}
        for room_id, ar in active_rooms.items():
            avg = self._get_avg_temp(ar.room)
            if avg is None:
                avg = thermo_ambient
            if avg is None:
                # No data at all — include room (benefit of the doubt)
                filtered[room_id] = ar
                continue
            effective = avg + ar.room.temp_offset
            recently_off = bool(off_schedule_ok and off_schedule_ok.get(ar.room.id))
            room_deadband = _effective_deadband(ar.room, deadband, ar.deadband_override)
            vote, suppressed = self._suppression_vote(
                ar.room,
                effective,
                ar.target_temp,
                ar.source,
                room_deadband,
                outside_temp,
                tc,
                recently_off,
            )
            if suppressed:
                # Ambient pre-cool/pre-heat is holding this room off — drop it so
                # it coasts toward target on outside air instead of riding the
                # cycle (Issue #248). When no cycle runs, its vents rest open;
                # when ANOTHER room drives a cycle, the coasting room is closed
                # like any idle room (#416 — deliberate: an open vent would let
                # the active cycle's supply air fight the coast).
                log.info(
                    "Excluding room %s from %s cycle — ambient pre-cool/pre-heat "
                    "is letting it coast (effective=%.1f, target=%.1f, outside=%s)",
                    ar.room.name,
                    mode,
                    effective,
                    ar.target_temp,
                    f"{outside_temp:.1f}" if outside_temp is not None else "n/a",
                )
                if self._logger:
                    await self._logger.log(
                        "info",
                        "engine",
                        f"Room {ar.room.name} left to coast — ambient pre-cool/pre-heat "
                        f"is skipping presence {mode} (effective={effective:.1f}°F, "
                        f"target={ar.target_temp}°F).",
                        {
                            "thermostat": self.thermostat_entity_id,
                            "room_id": room_id,
                            "room_name": ar.room.name,
                            "effective_temp": effective,
                            "target_temp": ar.target_temp,
                            "outside_temp": outside_temp,
                            "cycle_mode": mode,
                        },
                    )
                continue
            if mode == "cooling" and vote == "heat":
                # Room needs heating but cycle is cooling — exclude
                log.info(
                    "Excluding room %s from cooling cycle — needs heating "
                    "(effective=%.1f, target=%.1f, deadband=%.1f)",
                    ar.room.name,
                    effective,
                    ar.target_temp,
                    room_deadband,
                )
                if self._logger:
                    await self._logger.log(
                        "info",
                        "engine",
                        f"Excluding room {ar.room.name} from cooling cycle — room needs "
                        f"heating (effective={effective:.1f}°F, target={ar.target_temp}°F)",
                        {
                            "thermostat": self.thermostat_entity_id,
                            "room_id": room_id,
                            "room_name": ar.room.name,
                            "effective_temp": effective,
                            "target_temp": ar.target_temp,
                            "cycle_mode": mode,
                        },
                    )
                continue
            if mode == "heating" and vote == "cool":
                # Room needs cooling but cycle is heating — exclude
                log.info(
                    "Excluding room %s from heating cycle — needs cooling "
                    "(effective=%.1f, target=%.1f, deadband=%.1f)",
                    ar.room.name,
                    effective,
                    ar.target_temp,
                    room_deadband,
                )
                if self._logger:
                    await self._logger.log(
                        "info",
                        "engine",
                        f"Excluding room {ar.room.name} from heating cycle — room needs "
                        f"cooling (effective={effective:.1f}°F, target={ar.target_temp}°F)",
                        {
                            "thermostat": self.thermostat_entity_id,
                            "room_id": room_id,
                            "room_name": ar.room.name,
                            "effective_temp": effective,
                            "target_temp": ar.target_temp,
                            "cycle_mode": mode,
                        },
                    )
                continue
            filtered[room_id] = ar
        return filtered

    async def _read_outside_temp(self, conn: aiosqlite.Connection) -> float | None:
        """Read the configured outside-temperature HA entity, or None if unset/unreadable.

        The entity_id lives in `system_settings.outside_temperature_entity_id`
        (Issue #85 Phase 1b). HAClient.get_numeric_state handles °C → °F.
        """
        entity_id = await db.get_system_setting(conn, "outside_temperature_entity_id", "")
        if not entity_id:
            return None
        try:
            return self._ha.get_numeric_state(entity_id)
        except Exception:
            return None

    async def _cooling_lockout_state(
        self, conn: aiosqlite.Connection, tc: ThermostatConfig
    ) -> tuple[str, float | None]:
        """Evaluate the outdoor-temperature cooling lockout (Issue #209).

        Returns ``(state, outside_temp)`` where ``state`` is one of:

          - ``"disabled"``           — no ``cooling_lockout_below_f`` configured
          - ``"sensor_unavailable"`` — threshold set, but the outdoor sensor is
                                       unset or unreadable
          - ``"locked_out"``         — outdoor temperature is below the threshold
          - ``"allowed"``            — outdoor temperature is at/above the threshold

        Fail-open by design: an unconfigured or unreadable outdoor sensor never
        blocks cooling. The caller logs a warning for ``sensor_unavailable`` so
        the gap is visible rather than silent.
        """
        if tc.cooling_lockout_below_f is None:
            return ("disabled", None)
        outside = await self._read_outside_temp(conn)
        if outside is None:
            return ("sensor_unavailable", None)
        if outside < tc.cooling_lockout_below_f:
            return ("locked_out", outside)
        return ("allowed", outside)

    def _read_thermo_temp_and_setpoint(self) -> tuple[float | None, float | None]:
        """Read (current_temperature, temperature) from the thermostat state, in °F.

        Both attributes are reported in HA's configured system unit; they are
        normalised to the engine's internal °F via ``_climate_temp_to_f``
        (Issue #280). Missing/unparseable values come back as ``None``.
        """
        state = self._ha.get_state(self.thermostat_entity_id)
        if not state:
            return (None, None)
        attrs = state.get("attributes", {})
        unit = self._ha.ha_temp_unit
        return (
            _climate_temp_to_f(attrs.get("current_temperature"), unit),
            _climate_temp_to_f(attrs.get("temperature"), unit),
        )

    @staticmethod
    def _parked_setpoint(ambient_f: float, direction: str | None, overshoot_delta: float) -> float:
        """Where to park the thermostat setpoint when the engine stops driving it.

        Parking exactly at ambient leaves the thermostat armed: the moment its
        OWN room drifts past the native hysteresis, the HVAC restarts on the
        thermostat's judgement — racing the engine, which wants ROOM demand to
        start the next cycle. Nudge the parked setpoint by the user-configured
        ``overshoot_delta`` to the idle side of the direction — cooling parks
        ABOVE ambient, heating BELOW — so the HVAC cannot self-trigger until
        the zone has genuinely moved a full overshoot on its own, by which
        point the engine has already seen the room demand and started a proper
        cycle. This is the mirror of the ambient-anchored overshoot
        ``_set_thermostat_setpoint`` uses mid-cycle to guarantee the HVAC runs.
        An unknown/off direction parks at plain ambient (nothing to arm).

        Rounded to the closest whole degree (halves up): ambient readings are
        almost always fractional (e.g. 68.28°F), and most thermostats reject
        partial-degree setpoints — commanding 70.28°F parks the device at 70°F
        and the reconciler then re-asserts the unreachable value every pass.
        """
        if direction in ("cool", "cooling"):
            return eco.round_whole_f(ambient_f + overshoot_delta)
        if direction in ("heat", "heating"):
            return eco.round_whole_f(ambient_f - overshoot_delta)
        return eco.round_whole_f(ambient_f)

    async def _reset_setpoint_to_ambient(self, thermo_state: dict, tc: ThermostatConfig) -> None:
        """Park the thermostat setpoint for an idle zone — ambient nudged
        ``overshoot_delta`` to the idle side of the thermostat's current mode
        (see ``_parked_setpoint``) — skipping the ``climate.set_temperature``
        call when the setpoint is already parked within the drift tolerance.

        Without the skip a house sitting within deadband re-commands the same
        setpoint on every 60 s tick — continuous needless write traffic that can
        hit cloud-thermostat rate limits and churns the HA recorder (Issue #296).
        """
        attrs = thermo_state.get("attributes", {})
        unit = self._ha.ha_temp_unit
        ambient_f = _climate_temp_to_f(attrs.get("current_temperature"), unit)
        if ambient_f is None:
            return
        parked = self._parked_setpoint(ambient_f, thermo_state.get("state"), tc.overshoot_delta)
        current_sp_f = _climate_temp_to_f(attrs.get("temperature"), unit)
        if current_sp_f is not None and abs(current_sp_f - parked) <= _SETPOINT_DRIFT_TOLERANCE_F:
            # Already parked — nothing to send. Keep the tracked value in
            # sync so the reconciler treats the current setpoint as intended.
            self._last_setpoint_sent = parked
            return
        try:
            await self._ha.set_thermostat_temperature(self.thermostat_entity_id, parked)
            self._last_setpoint_sent = parked
        except Exception as exc:
            log.error("Failed to reset setpoint to ambient: %s: %r", exc, exc)

    def _snapshot_vent_states_json(self, vents: list[RoomVent]) -> str | None:
        if not vents:
            return None
        return json.dumps(self._vent.get_vent_states(vents))

    async def _build_trigger_detail(
        self, conn: aiosqlite.Connection, ar: ActiveRoom
    ) -> dict | None:
        """Build a per-room trigger detail dict for audit/UI display."""
        detail: dict = {"source": ar.source, "target": ar.target_temp}
        if ar.source == "override":
            try:
                override = await db.get_room_override(conn, ar.room.id)
            except Exception:
                override = None
            if override:
                detail["expires_at"] = override.expires_at.replace(tzinfo=None).isoformat()
                detail["respect_eco"] = override.respect_eco
        elif ar.source == "schedule":
            try:
                schedules = await db.get_schedules_for_room(conn, ar.room.id)
                from .room_manager import _find_matching_schedule

                match = _find_matching_schedule(schedules, datetime.now(UTC))
                if match:
                    detail["schedule_id"] = match.id
                    detail["start_time"] = match.start_time.isoformat()
                    detail["end_time"] = match.end_time.isoformat()
                    detail["days_of_week"] = match.days_of_week
            except Exception:
                pass
        elif ar.source == "presence":
            try:
                holdover = await db.get_holdover_state(conn, ar.room.id)
            except Exception:
                holdover = None
            if holdover:
                detail["holdover_expires_at"] = holdover.expires_at.replace(tzinfo=None).isoformat()
                detail["last_detected_at"] = holdover.last_detected_at.replace(
                    tzinfo=None
                ).isoformat()
            try:
                sensors = await db.get_room_presence_sensors(conn, ar.room.id)
                if sensors:
                    detail["sensor_entity_ids"] = [s.entity_id for s in sensors]
            except Exception:
                pass
        return detail

    async def _emit_sensor_freshness_warnings(self, active_rooms: dict[str, ActiveRoom]) -> None:
        """Surface room-sensor staleness in the event log (Issue #211).

        Walks each active room's sensors and compares their age against
        ``SENSOR_STALE_AFTER_MIN``. The first tick a sensor crosses into
        staleness writes a ``warning`` event naming the entity and its age;
        the engine then suppresses further warnings for that entity until it
        reports a fresh reading again, at which point a ``info`` recovery
        event is written. This keeps the event feed signal-heavy rather than
        spamming a stuck sensor every 60 seconds.
        """
        now_stale: set[str] = set()
        now_fresh: set[str] = set()
        for ar in active_rooms.values():
            sensor_ids = self._sensor_ids_for_room.get(ar.room.id, [])
            for eid in sensor_ids:
                age_s = self._ha.get_state_age_seconds(eid)
                if age_s is None:
                    continue  # entity not in cache at all — separate failure mode
                if age_s <= self._stale_after_min * 60:
                    now_fresh.add(eid)
                    continue
                now_stale.add(eid)
                if eid in self._stale_warned:
                    continue
                self._stale_warned.add(eid)
                age_min = age_s / 60
                log.warning(
                    "Sensor %s for room %r is stale (age %.0f min); excluding from room average",
                    eid,
                    ar.room.name,
                    age_min,
                )
                if self._logger:
                    await self._logger.log(
                        "warning",
                        "engine",
                        f"Temperature sensor {eid} for room '{ar.room.name}' has "
                        f"not reported in {age_min:.0f} minutes; excluded from the "
                        "room temperature average. Check the sensor's battery or "
                        "connection.",
                        {
                            "entity_id": eid,
                            "room": ar.room.name,
                            "age_seconds": age_s,
                        },
                    )

        # Recovery events only for sensors OBSERVED fresh this tick (#434).
        # The old `warned − now_stale` form emitted a bogus "reporting again"
        # for a still-dead sensor whose room merely deactivated (it was no
        # longer examined, so it never landed in now_stale) — misleading for
        # battery triage, and the same episode re-warned on reactivation.
        recovered = self._stale_warned & now_fresh
        for eid in recovered:
            self._stale_warned.discard(eid)
            log.info("Sensor %s is reporting again (fresh reading)", eid)
            if self._logger:
                await self._logger.log(
                    "info",
                    "engine",
                    f"Temperature sensor {eid} is reporting again (fresh reading received).",
                    {"entity_id": eid},
                )

    def _get_avg_temp(self, room: Room) -> float | None:
        readings: list[float] = []

        # Re-query the cache for all sensors belonging to this room. Readings
        # older than SENSOR_STALE_AFTER_MIN are excluded so a battery sensor
        # that has dropped off cannot silently drive control decisions
        # (Issue #211). Warning events are emitted from _do_tick on the
        # transition into staleness, not here, to keep this method sync.
        sensor_ids = self._sensor_ids_for_room.get(room.id, [])
        for eid in sensor_ids:
            val = self._ha.get_numeric_state(eid, max_age_min=self._stale_after_min)
            if val is not None:
                readings.append(val)

        if room.include_thermostat_sensor:
            # The thermostat probe reports in HA's system unit and is
            # normalised to °F so it is not mixed with already-°F sensor
            # readings (#280). Routed through the plausibility guard (Issue
            # #636), via the per-tick memoized wrapper rather than
            # `_read_validated_ambient_f`/`_climate_temp_to_f` directly:
            # `_get_avg_temp` is one of 15+ call sites that can run within a
            # single tick, so a reconnect glitch is rejected here exactly as
            # it is for the two no-demand supervision arms, and every room
            # (and every call within the tick) reads the SAME answer rather
            # than each re-running the stateful rate check against an
            # almost-zero elapsed baseline.
            thermo = self._ha.get_state(room.thermostat_entity_id)
            t_f = self._validated_ambient_for_tick(thermo)
            if t_f is not None:
                readings.append(t_f)

        if not readings:
            return None
        return sum(readings) / len(readings)

    def _sensor_counts(self, room: Room) -> tuple[int, int]:
        """Return (configured_sensor_count, available_sensor_count) for a room.

        Mirrors ``_get_avg_temp``'s sources: the room's configured temperature
        sensors plus the thermostat probe when ``include_thermostat_sensor`` is
        set. "Available" counts only sensors with a fresh reading (stale ones
        are excluded the same way ``_get_avg_temp`` excludes them), so the UI
        can show "2 of 3 sensors reporting" rather than a misleading 1/0.
        """
        sensor_ids = self._sensor_ids_for_room.get(room.id, [])
        total = len(sensor_ids)
        available = sum(
            1
            for eid in sensor_ids
            if self._ha.get_numeric_state(eid, max_age_min=self._stale_after_min) is not None
        )
        if room.include_thermostat_sensor:
            total += 1
            thermo = self._ha.get_state(room.thermostat_entity_id)
            if thermo:
                t = thermo.get("attributes", {}).get("current_temperature")
                if t is not None:
                    try:
                        float(t)
                        available += 1
                    except (ValueError, TypeError):
                        pass
        return total, available

    async def _set_thermostat_setpoint(
        self,
        tc: ThermostatConfig,
        hvac_mode: str,
        conn: aiosqlite.Connection | None = None,
        setpoint_reason: str | None = None,
    ) -> None:
        # target_temp values come from DB where the route layer stores them in °F
        # after converting from the active display unit.  No unit conversion here.
        targets = [ar.target_temp for ar in self._active_rooms.values()]
        if not targets:
            return
        if hvac_mode == "cooling":
            setpoint = min(targets) - tc.overshoot_delta
            ha_mode = "cool"
        elif hvac_mode == "heating":
            setpoint = max(targets) + tc.overshoot_delta
            ha_mode = "heat"
        else:
            # Unexpected mode (e.g. "off", "unknown") — refuse to set a setpoint
            # rather than silently defaulting to heat.  (Issue #48 Bug 2)
            log.error(
                "Refusing to set setpoint — unexpected hvac_mode %r for %s",
                hvac_mode,
                self.thermostat_entity_id,
            )
            if self._logger:
                await self._logger.log(
                    "error",
                    "engine",
                    f"Refusing to set setpoint — unexpected hvac_mode {hvac_mode!r} "
                    f"for {self.thermostat_entity_id}. Expected 'cooling' or 'heating'.",
                    {
                        "thermostat": self.thermostat_entity_id,
                        "hvac_mode": hvac_mode,
                    },
                )
            return

        # Anchor setpoint to thermostat ambient so the HVAC always has a reason
        # to run.  When room sensors diverge from the thermostat probe (e.g.
        # thermostat in hallway reads 71°F, bedroom sensors read 80°F), the
        # target-derived setpoint can land at or beyond ambient, causing the
        # HVAC to see "already satisfied" and never activate.  Clamping
        # guarantees the setpoint is strictly beyond the thermostat's own
        # reading by at least overshoot_delta.  (Issue #48 Bug 1)
        thermo_state = self._ha.get_state(self.thermostat_entity_id)
        if thermo_state:
            # Thermostat ambient is reported in HA's system unit; normalise to °F
            # before comparing against the °F-derived setpoint. (Issue #280)
            ambient = _climate_temp_to_f(
                thermo_state.get("attributes", {}).get("current_temperature"),
                self._ha.ha_temp_unit,
            )
            if ambient is not None:
                try:
                    if hvac_mode == "cooling":
                        ambient_bound = ambient - tc.overshoot_delta
                        if setpoint > ambient_bound:
                            log.info(
                                "Clamping cooling setpoint %.1f→%.1f (ambient=%.1f, delta=%.1f)",
                                setpoint,
                                ambient_bound,
                                ambient,
                                tc.overshoot_delta,
                            )
                            if self._logger:
                                await self._logger.log(
                                    "info",
                                    "engine",
                                    f"Clamped cooling setpoint {setpoint:.1f}→{ambient_bound:.1f}°F "
                                    f"(thermostat ambient={ambient:.1f}°F, overshoot_delta={tc.overshoot_delta})",
                                    {
                                        "thermostat": self.thermostat_entity_id,
                                        "original_setpoint": setpoint,
                                        "clamped_setpoint": ambient_bound,
                                        "ambient": ambient,
                                        "overshoot_delta": tc.overshoot_delta,
                                    },
                                )
                            setpoint = ambient_bound
                    else:
                        ambient_bound = ambient + tc.overshoot_delta
                        if setpoint < ambient_bound:
                            log.info(
                                "Clamping heating setpoint %.1f→%.1f (ambient=%.1f, delta=%.1f)",
                                setpoint,
                                ambient_bound,
                                ambient,
                                tc.overshoot_delta,
                            )
                            if self._logger:
                                await self._logger.log(
                                    "info",
                                    "engine",
                                    f"Clamped heating setpoint {setpoint:.1f}→{ambient_bound:.1f}°F "
                                    f"(thermostat ambient={ambient:.1f}°F, overshoot_delta={tc.overshoot_delta})",
                                    {
                                        "thermostat": self.thermostat_entity_id,
                                        "original_setpoint": setpoint,
                                        "clamped_setpoint": ambient_bound,
                                        "ambient": ambient,
                                        "overshoot_delta": tc.overshoot_delta,
                                    },
                                )
                            setpoint = ambient_bound
                except (ValueError, TypeError):
                    pass

        # Clamp to configured safety bounds. Ambient-overshoot above can push the
        # setpoint past min_setpoint (aggressive cooling) or max_setpoint
        # (aggressive heating) — correct it back into the user's envelope so the
        # HVAC is never commanded outside its configured safe range.
        if setpoint < tc.min_setpoint or setpoint > tc.max_setpoint:
            clamped = min(max(setpoint, tc.min_setpoint), tc.max_setpoint)
            log.info(
                "Clamping setpoint %.1f→%.1f to bounds [%.1f, %.1f]",
                setpoint,
                clamped,
                tc.min_setpoint,
                tc.max_setpoint,
            )
            if self._logger:
                await self._logger.log(
                    "info",
                    "engine",
                    f"Clamped setpoint {setpoint:.1f}→{clamped:.1f}°F to configured bounds "
                    f"[{tc.min_setpoint:.1f}, {tc.max_setpoint:.1f}]°F",
                    {
                        "thermostat": self.thermostat_entity_id,
                        "original_setpoint": setpoint,
                        "clamped_setpoint": clamped,
                        "min_setpoint": tc.min_setpoint,
                        "max_setpoint": tc.max_setpoint,
                    },
                )
            setpoint = clamped

        # Most thermostats reject (or silently floor) partial-degree setpoints,
        # which turns every fractional command into permanent reconcile "drift"
        # against the device. Command whole degrees only, rounded in the run
        # direction — cooling floors, heating ceils — so the command never sits
        # on the idle side of the (possibly fractional) driving target: with
        # overshoot_delta=0 a closest-whole round of a 72.57 °F cooling target
        # would command 73 °F and the HVAC would stop before the room ever
        # reached 72.57. Re-clamped so rounding can never leave the envelope.
        # This is the ONLY place cycle setpoints are rounded: eco-relaxed room
        # targets deliberately keep their fraction (eco.relax_target) because
        # they are the rooms' stop conditions, and fractional targets, overshoot
        # deltas, and the ambient-anchored clamp above all produce fractions.
        whole = (
            eco.floor_whole_f(setpoint) if hvac_mode == "cooling" else eco.ceil_whole_f(setpoint)
        )
        setpoint = min(max(whole, tc.min_setpoint), tc.max_setpoint)

        try:
            # Pass hvac_mode explicitly so heat_cool thermostats switch to the correct
            # single-direction mode. HA silently ignores temperature-only calls for
            # heat_cool mode (Bug 2).
            await self._ha.set_thermostat_temperature(
                self.thermostat_entity_id, setpoint, hvac_mode=ha_mode
            )
            # Track what we last commanded so reconciliation can detect external drift.
            self._last_setpoint_sent = setpoint
            self._cycle_ha_mode = ha_mode  # track the mode we locked the thermostat into
            # Record setpoint change in cycle history for diagnostics.
            if conn is not None and self._cycle_log:
                try:
                    await db.insert_cycle_setpoint_history(
                        conn,
                        self._cycle_log.id,
                        datetime.now(UTC),
                        setpoint,
                        setpoint_reason or f"mode={hvac_mode}",
                    )
                except Exception as exc:
                    log.debug("Failed to record setpoint history: %s", exc)
            if self._logger:
                await self._logger.log(
                    "info",
                    "engine",
                    f"Setpoint for {self.thermostat_entity_id} set to {setpoint:.1f}°F "
                    f"(mode={hvac_mode}, ha_mode={ha_mode})",
                    {
                        "thermostat": self.thermostat_entity_id,
                        "setpoint": setpoint,
                        "hvac_mode": hvac_mode,
                        "ha_mode": ha_mode,
                        "targets": targets,
                    },
                )
        except Exception as exc:
            log.error("Failed to set thermostat setpoint: %s", exc)

    async def _maybe_reconcile(
        self,
        conn: aiosqlite.Connection,
        *,
        in_vacation: bool = False,
        backstop_fired: bool = False,
    ) -> None:
        """Check whether it is time to reconcile and, if so, call _reconcile_state.

        ``in_vacation`` and ``backstop_fired`` are forwarded to
        ``_reconcile_state`` unchanged — see its docstring. Both default to
        False so direct calls in tests (which predate these parameters) keep
        their original behavior.
        """
        tc = await db.get_thermostat_config(conn, self.thermostat_entity_id)
        if tc.reconciliation_interval_min <= 0:
            return
        now = datetime.now(UTC)
        interval_secs = tc.reconciliation_interval_min * 60
        if (
            self._last_reconciled_at is None
            or (now - self._last_reconciled_at).total_seconds() >= interval_secs
        ):
            await self._reconcile_state(
                conn, tc, in_vacation=in_vacation, backstop_fired=backstop_fired
            )
            self._last_reconciled_at = now

    async def _reconcile_state(
        self,
        conn: aiosqlite.Connection,
        tc: ThermostatConfig,
        *,
        in_vacation: bool = False,
        backstop_fired: bool = False,
    ) -> None:
        """
        Verify actual vent and thermostat state matches engine intent; correct any drift.

        RUNNING: each active room's vents should be open when vent_closed_at is None,
                 closed when vent_closed_at is set. Thermostat setpoint is checked
                 against _last_setpoint_sent.
        IDLE:    all zone vents should be open — no active cycle means nothing should
                 be closed. Loads vents fresh from DB since _room_vents is cleared.

        ``in_vacation`` and ``backstop_fired`` describe what ELSE ran on this
        same tick, before this call: whether vacation mode is active, and
        whether `_enforce_safety_setpoint` (the #367 backstop) just commanded
        a corrective setpoint. Both mean some other mechanism already owns
        this tick's thermostat command, so the idle-mode-hygiene comparison
        below must defer entirely rather than compare against — and
        potentially "correct" — a value that mechanism just legitimately
        set. See the idle-mode-hygiene comment below for why (Issue #637
        follow-up: the original version fought both, plus the heat_cool
        guard's own remedy one tick later).

        All corrections are logged as 'warning' under category 'reconcile'.
        """
        # Always log that reconciliation ran — even when nothing needs correcting.
        # This gives operators a heartbeat to confirm the reconciler is active.
        thermo_state_now = self._ha.get_state(self.thermostat_entity_id) or {}
        ha_mode_now = thermo_state_now.get("state", "unknown")
        ha_setpoint_now = _climate_temp_to_f(
            thermo_state_now.get("attributes", {}).get("temperature"),
            self._ha.ha_temp_unit,
        )

        # Idle mode-hygiene check (Issue #637). While idle there is nothing in
        # memory to compare the live HA mode against — `_cycle_ha_mode` is
        # cleared by `_terminate_cycle`/`_abort_cycle`, and deliberately
        # nothing durable replaces it either: an in-memory "expected mode"
        # would treat every restart while parked-idle as "no recorded
        # expectation" and force a perfectly healthy thermostat to be
        # re-commanded. Instead recover the last legitimate conditioning
        # direction from the most recent CLOSED cycle already durable in
        # `cycle_logs`. Computed here, before the summary log below, so a
        # live mismatch can bump that line off INFO — this is exactly the
        # gap that let "engine=idle, ha_mode='heat', expected_mode=None" read
        # as routine for six hours: `expected_mode` (`_cycle_ha_mode`) is
        # always None while idle by design, so it never told the story.
        # RUNNING is excluded: the RUNNING arm below already re-asserts
        # `_cycle_ha_mode` against live drift, and this thermostat may
        # legitimately still be finishing the cycle that produced the very
        # cycle_logs row this check would otherwise compare against.
        #
        # Deference (Issue #637 follow-up — a real regression the first
        # version shipped with, caught in review): this comparison is NOT the
        # only thing that may command `hvac_mode`/setpoint on this same tick,
        # and when something else already has, THAT command must stand —
        # the pre-existing mechanism always wins, never the new one:
        #   - `backstop_fired` — `_enforce_safety_setpoint` (the #367 comfort-
        #     envelope backstop) just commanded a corrective mode+setpoint to
        #     hold a breached bound. Comparing against the recovered cycle
        #     history here would see that correction as "external
        #     interference" and revert it, undoing the very backstop #367/#368
        #     hardened against — unbounded ping-pong on real equipment.
        #   - `in_vacation` — `_apply_vacation_hold` owns this thermostat
        #     while vacation is active (its own comment at the call site
        #     already warns "running both would issue competing setpoint
        #     commands on the same tick"); this check must not be the second
        #     thing issuing one.
        #   - `_heat_cool_revert_pending` — the guard in `_do_tick` (still at
        #     ~line 474, unchanged, and structurally unreachable from here on
        #     the SAME tick since it returns first) reverted a live
        #     `heat_cool` to `off` last tick. That `off` is Plenum's own
        #     remedial action, not interference — comparing against it
        #     immediately would undo the very revert that just happened.
        #     Consumed (read-then-cleared) unconditionally on every idle-arm
        #     evaluation: the grace is "give our last action one look", not
        #     an ongoing suppression. (That look only happens on a tick that
        #     reaches this arm with reconciliation enabled — see the field's
        #     own docstring in __init__ for the default-off caveat.)
        idle_recovered_cycle: CycleLog | None = None
        idle_expected_ha_mode: str | None = None
        idle_mode_mismatch = False
        if self._state != CycleState.RUNNING:
            heat_cool_grace = self._heat_cool_revert_pending
            self._heat_cool_revert_pending = False
            defer_idle_mode_hygiene = in_vacation or backstop_fired or heat_cool_grace
            if not defer_idle_mode_hygiene:
                idle_recovered_cycle = await db.get_last_completed_cycle_for_thermostat(
                    conn, self.thermostat_entity_id
                )
                if idle_recovered_cycle is not None:
                    # CycleLog.mode ('heating'/'cooling') is exactly the
                    # `direction` `_parked_setpoint` already accepts; mirrors
                    # the mapping `restore_from_db` uses to rehydrate
                    # `_cycle_ha_mode`.
                    idle_expected_ha_mode = (
                        "cool" if idle_recovered_cycle.mode == "cooling" else "heat"
                    )
                    # Folds the heat_cool guard's own condition into this same
                    # inequality — heat_cool, the opposite direction, and a
                    # live `off` all fail to equal the recovered direction, so
                    # none needs its own separate check. A live `off` is
                    # deliberately NOT treated as an intentional state to
                    # preserve (see the correction below) — except for the
                    # one-tick grace above, which is about WHO caused it
                    # (Plenum itself, last tick), not about `off` being
                    # inherently safe to ignore.
                    if ha_mode_now != idle_expected_ha_mode:
                        idle_mode_mismatch = True

        summary_level = "warning" if idle_mode_mismatch else "info"
        summary_log_fn = log.warning if idle_mode_mismatch else log.info
        summary_log_fn(
            "Reconcile %s: engine_state=%s ha_mode=%s ha_setpoint=%s "
            "cycle_ha_mode=%s last_setpoint_sent=%s",
            self.thermostat_entity_id,
            self._state.value,
            ha_mode_now,
            ha_setpoint_now,
            self._cycle_ha_mode,
            self._last_setpoint_sent,
        )
        if self._logger:
            await self._logger.log(
                summary_level,
                "reconcile",
                f"Reconcile {self.thermostat_entity_id}: engine={self._state.value}, "
                f"ha_mode={ha_mode_now!r}, ha_setpoint={ha_setpoint_now}, "
                f"expected_mode={self._cycle_ha_mode!r}, "
                f"expected_setpoint={self._last_setpoint_sent}, "
                f"active_rooms={len(self._active_rooms)}, "
                f"cycle_id={self._cycle_log.id if self._cycle_log else 'none'}",
                {
                    "thermostat": self.thermostat_entity_id,
                    "engine_state": self._state.value,
                    "ha_mode": ha_mode_now,
                    "ha_setpoint": ha_setpoint_now,
                    "expected_mode": self._cycle_ha_mode,
                    "expected_setpoint": self._last_setpoint_sent,
                    "active_rooms": len(self._active_rooms),
                    "cycle_id": self._cycle_log.id if self._cycle_log else None,
                    "db_min_setpoint": tc.min_setpoint,
                    "db_max_setpoint": tc.max_setpoint,
                    "db_deadband": tc.deadband,
                    "db_total_vents_count": tc.total_vents_count,
                    "db_has_bypass_damper": tc.has_bypass_damper,
                    "db_min_open_vents_fraction": tc.min_open_vents_fraction,
                    "db_max_vent_closed_min": tc.max_vent_closed_min,
                    "db_reconciliation_interval_min": tc.reconciliation_interval_min,
                },
            )

        if self._state == CycleState.RUNNING:
            # Zone-wide vent pool so the airflow floor inside close_room_vents
            # is not deflated by idle rooms' closed smart vents (#421).
            all_zone_vents = await self._get_all_zone_vents(conn)
            for room_id in self._active_rooms:
                rcs = self._room_cycle_states.get(room_id)
                vents = self._room_vents.get(room_id, [])
                if not vents or rcs is None:
                    continue
                should_be_closed = rcs.vent_closed_at is not None
                for vent in vents:
                    actual_open = self._vent._is_open(vent)
                    if should_be_closed and actual_open:
                        # External actor opened a vent that the engine closed — re-close.
                        closed = await self._vent.close_room_vents(
                            [vent], all_zone_vents, tc, self._room_cycle_states
                        )
                        if closed:
                            log.warning(
                                "Reconcile: vent %s should be closed — re-closing (external change detected)",
                                vent.entity_id,
                            )
                            if self._logger:
                                await self._logger.log(
                                    "warning",
                                    "reconcile",
                                    f"Drift: vent {vent.entity_id} found open but should be closed — re-closed",
                                    {
                                        "entity_id": vent.entity_id,
                                        "room_id": room_id,
                                        "thermostat": self.thermostat_entity_id,
                                    },
                                )
                    elif not should_be_closed and not actual_open:
                        # External actor closed a vent that the engine opened — re-open.
                        await self._vent.open_room_vents([vent])
                        log.warning(
                            "Reconcile: vent %s should be open — re-opening (external change detected)",
                            vent.entity_id,
                        )
                        if self._logger:
                            await self._logger.log(
                                "warning",
                                "reconcile",
                                f"Drift: vent {vent.entity_id} found closed but should be open — re-opened",
                                {
                                    "entity_id": vent.entity_id,
                                    "room_id": room_id,
                                    "thermostat": self.thermostat_entity_id,
                                },
                            )

            # Idle-zone-room drift: vents for rooms NOT in the active cycle must
            # stay closed for the duration of the cycle (issue #82). If the
            # initial close silently failed, or HA reloaded and reopened them,
            # nothing in the active-room loop above would catch it.
            active_ids = set(self._active_rooms.keys())
            zone_rooms = await db.get_rooms_for_thermostat(conn, self.thermostat_entity_id)
            for zone_room in zone_rooms:
                if zone_room.id in active_ids:
                    continue
                if zone_room.id in self._overflow_room_ids:
                    # Overflow rooms (#237) are deliberately open during the
                    # min-runtime hold despite not being active — re-closing
                    # them as "drift" silently defeated overflow conditioning
                    # for the rest of the hold (#422).
                    continue
                idle_vents = await db.get_room_vents(conn, zone_room.id)
                for vent in idle_vents:
                    if not self._vent._is_open(vent):
                        continue
                    # all_zone_vents is already the full zone (#421) — the
                    # idle room's vents are in it; do not append them twice.
                    closed = await self._vent.close_room_vents(
                        [vent], all_zone_vents, tc, self._room_cycle_states
                    )
                    if closed:
                        log.warning(
                            "Reconcile: idle-room vent %s drifted open — re-closing",
                            vent.entity_id,
                        )
                        if self._logger:
                            await self._logger.log(
                                "warning",
                                "reconcile",
                                f"Drift: idle-room vent {vent.entity_id} ({zone_room.name}) "
                                f"found open during cycle — re-closed",
                                {
                                    "entity_id": vent.entity_id,
                                    "room_id": zone_room.id,
                                    "room_name": zone_room.name,
                                    "thermostat": self.thermostat_entity_id,
                                },
                            )

            # Check thermostat mode and setpoint drift.
            if self._last_setpoint_sent is not None or self._cycle_ha_mode is not None:
                thermo_state = self._ha.get_state(self.thermostat_entity_id)
                if thermo_state:
                    needs_reassert = False

                    # Re-assert mode if the thermostat was switched off or to heat_cool
                    # mid-cycle (Bug 4). The cycle-locked ha_mode must be the active mode.
                    if self._cycle_ha_mode is not None:
                        current_mode = thermo_state.get("state", "")
                        if current_mode != self._cycle_ha_mode:
                            log.warning(
                                "Reconcile: thermostat %s mode drifted %s→%s — re-asserting",
                                self.thermostat_entity_id,
                                current_mode,
                                self._cycle_ha_mode,
                            )
                            if self._logger:
                                await self._logger.log(
                                    "warning",
                                    "reconcile",
                                    f"Drift: thermostat {self.thermostat_entity_id} mode changed "
                                    f"from {current_mode!r} to {self._cycle_ha_mode!r} — re-asserting",
                                    {
                                        "entity_id": self.thermostat_entity_id,
                                        "expected_mode": self._cycle_ha_mode,
                                        "actual_mode": current_mode,
                                    },
                                )
                            needs_reassert = True

                    # Re-assert setpoint if it drifted externally. The HA
                    # setpoint is in the system unit; normalise to °F so drift is
                    # not falsely detected on metric installs. (Issue #280)
                    if self._last_setpoint_sent is not None:
                        current_sp = _climate_temp_to_f(
                            thermo_state.get("attributes", {}).get("temperature"),
                            self._ha.ha_temp_unit,
                        )
                        if current_sp is not None:
                            drift = abs(current_sp - self._last_setpoint_sent)
                            if drift > _SETPOINT_DRIFT_TOLERANCE_F:  # float rounding in HA
                                log.warning(
                                    "Reconcile: thermostat %s setpoint drifted %.1f→%.1f — re-asserting",
                                    self.thermostat_entity_id,
                                    current_sp,
                                    self._last_setpoint_sent,
                                )
                                if self._logger:
                                    await self._logger.log(
                                        "warning",
                                        "reconcile",
                                        f"Drift: thermostat {self.thermostat_entity_id} setpoint changed "
                                        f"from {self._last_setpoint_sent:.1f}°F to {float(current_sp):.1f}°F "
                                        f"— re-asserting",
                                        {
                                            "entity_id": self.thermostat_entity_id,
                                            "expected": self._last_setpoint_sent,
                                            "actual": float(current_sp),
                                        },
                                    )
                                needs_reassert = True

                    if needs_reassert and self._last_setpoint_sent is not None:
                        try:
                            await self._ha.set_thermostat_temperature(
                                self.thermostat_entity_id,
                                self._last_setpoint_sent,
                                hvac_mode=self._cycle_ha_mode,
                            )
                        except Exception as exc:
                            log.error("Reconcile: failed to re-assert mode+setpoint: %s", exc)

        else:
            # IDLE — all zone vents should be open; load fresh from DB.
            rooms = await db.get_rooms_for_thermostat(conn, self.thermostat_entity_id)
            for room in rooms:
                vents = await db.get_room_vents(conn, room.id)
                for vent in vents:
                    if not self._vent._is_open(vent):
                        await self._vent.open_room_vents([vent])
                        log.warning(
                            "Reconcile (idle): vent %s was closed externally while zone is idle — re-opening "
                            "(check for manual HA cover controls or automations acting on this entity)",
                            vent.entity_id,
                        )
                        if self._logger:
                            await self._logger.log(
                                "warning",
                                "reconcile",
                                f"Vent {vent.entity_id} found closed while zone is idle — re-opened. "
                                f"This vent was closed by something outside Plenum (manual HA cover control, "
                                f"an HA automation, or an HA restart that reset cover state). "
                                f"Plenum does not close vents when the zone is idle.",
                                {
                                    "entity_id": vent.entity_id,
                                    "room_id": room.id,
                                    "thermostat": self.thermostat_entity_id,
                                },
                            )

            # DB settings check (idle): warn if the HA thermostat setpoint is
            # outside the configured bounds — could indicate an external actor
            # set an unsafe temperature while the system was not running a cycle.
            if ha_setpoint_now is not None:
                try:
                    sp = float(ha_setpoint_now)
                    if sp < tc.min_setpoint or sp > tc.max_setpoint:
                        log.warning(
                            "Reconcile (idle): thermostat %s setpoint %.1f is outside "
                            "configured bounds [%.1f, %.1f]",
                            self.thermostat_entity_id,
                            sp,
                            tc.min_setpoint,
                            tc.max_setpoint,
                        )
                        if self._logger:
                            await self._logger.log(
                                "warning",
                                "reconcile",
                                f"DB settings drift (idle): thermostat {self.thermostat_entity_id} "
                                f"setpoint {sp:.1f}°F is outside configured bounds "
                                f"[{tc.min_setpoint:.1f}, {tc.max_setpoint:.1f}]°F — "
                                f"external actor may have changed it",
                                {
                                    "entity_id": self.thermostat_entity_id,
                                    "ha_setpoint": sp,
                                    "db_min_setpoint": tc.min_setpoint,
                                    "db_max_setpoint": tc.max_setpoint,
                                },
                            )
                except (ValueError, TypeError):
                    pass

            # Mode-hygiene correction (Issue #637): the live HA mode disagrees
            # with the direction recovered above. The system-disabled guard at
            # the top of `_do_tick` already returned before `_maybe_reconcile`
            # is ever reached, so by construction the system is enabled here —
            # Plenum is the sole intended controller of this thermostat, and a
            # live mode that disagrees with what it last parked for is
            # external interference, not something to respect. This matches
            # `_apply_vacation_hold`'s own standard on its single-setpoint
            # branch: it re-asserts `off` every tick without carving out a
            # live mode that might have been set on purpose, and a live `off`
            # here gets the same non-deference — it is not preserved either.
            # Correct by RECOMPUTING the park against a fresh ambient read
            # (`_parked_setpoint`'s job is to sit a safe margin from CURRENT
            # ambient, which has generally moved since the row was written),
            # not by replaying the row's `setpoint_at_end` — that value was
            # captured before parking even happened, so it was never the
            # parked value to begin with.
            if idle_mode_mismatch and idle_recovered_cycle and idle_expected_ha_mode:
                log.warning(
                    "Reconcile (idle): thermostat %s hvac_mode=%s disagrees with "
                    "the last completed cycle's direction (%s → expected %s) — "
                    "correcting",
                    self.thermostat_entity_id,
                    ha_mode_now,
                    idle_recovered_cycle.mode,
                    idle_expected_ha_mode,
                )
                if self._logger:
                    await self._logger.log(
                        "warning",
                        "reconcile",
                        f"Drift: thermostat {self.thermostat_entity_id} hvac_mode is "
                        f"{ha_mode_now!r} but the last completed cycle ({idle_recovered_cycle.id}) "
                        f"ran {idle_recovered_cycle.mode!r} (expected {idle_expected_ha_mode!r}) — "
                        "this looks like external interference, not an intentional change "
                        "(Plenum is the sole intended controller of this thermostat while "
                        "enabled) — re-asserting the recovered direction with a freshly "
                        "parked setpoint",
                        {
                            "entity_id": self.thermostat_entity_id,
                            "ha_mode": ha_mode_now,
                            "recovered_cycle_id": idle_recovered_cycle.id,
                            "recovered_cycle_mode": idle_recovered_cycle.mode,
                            "expected_ha_mode": idle_expected_ha_mode,
                        },
                    )
                ambient_f = _climate_temp_to_f(
                    thermo_state_now.get("attributes", {}).get("current_temperature"),
                    self._ha.ha_temp_unit,
                )
                if ambient_f is None:
                    log.warning(
                        "Reconcile (idle): cannot correct hvac_mode for %s — no readable "
                        "current_temperature to park against",
                        self.thermostat_entity_id,
                    )
                else:
                    parked = self._parked_setpoint(
                        ambient_f, idle_expected_ha_mode, tc.overshoot_delta
                    )
                    try:
                        await self._ha.set_thermostat_temperature(
                            self.thermostat_entity_id,
                            parked,
                            hvac_mode=idle_expected_ha_mode,
                        )
                        self._last_setpoint_sent = parked
                    except Exception as exc:
                        log.error(
                            "Reconcile (idle): failed to correct hvac_mode/setpoint for %s: %s",
                            self.thermostat_entity_id,
                            exc,
                        )

    async def restore_from_db(self, conn: aiosqlite.Connection) -> None:
        """
        Restore in-memory cycle state from DB at startup.

        If an open cycle log exists for this thermostat, the engine resumes
        it rather than starting fresh on the first tick. This preserves:
          - Which rooms had already hit their target (vent_closed_at)
          - The original cycle start timestamp (timeout clock continues)
          - Vent expectations so reconciliation can correct post-restart drift

        Multiple open logs (from the pre-fix duplicate-cycle bug) are handled
        by closing all but the most recent and restoring from the newest one.
        Rooms that no longer exist in DB, and rooms whose snapshot entry is
        malformed (#604), are skipped with a warning; the cycle still resumes
        with whatever rooms did restore.

        This runs inside aiohttp's ``on_startup``, before the HTTP server
        binds, so an escaping exception costs the whole process. The snapshot
        is therefore validated for shape and field types rather than trusted.
        That does not make the function total — ``db.get_open_cycle_logs``
        parses ``started_at`` and raises on a corrupt timestamp, for one — so
        the caller (``Scheduler._sync_engines_locked``) carries a structural
        net as well.
        """
        # Rehydrate the compressor off-time lockout clock (#432). Termination
        # and abort both persist ended_at, and an add-on restart takes seconds
        # — without this a reboot right after a cycle stopped allowed an
        # immediate compressor restart inside the protection window.
        try:
            last_end = await db.get_latest_cycle_end(conn, self.thermostat_entity_id)
            if last_end is not None:
                self._last_cycle_ended_at = last_end
        except Exception as exc:
            log.warning("Failed to rehydrate off-time lockout clock: %s", exc)

        open_logs = await db.get_open_cycle_logs(conn, self.thermostat_entity_id)
        if not open_logs:
            return

        # Close duplicates (all but the most recent); newest-first order from DB
        to_restore = open_logs[0]
        if len(open_logs) > 1:
            now = datetime.now(UTC)
            for stale in open_logs[1:]:
                await db.close_cycle_log(conn, stale.id, now)
            log.warning(
                "Closed %d duplicate open cycle log(s) for %s on startup",
                len(open_logs) - 1,
                self.thermostat_entity_id,
            )
            if self._logger:
                await self._logger.log(
                    "warning",
                    "engine",
                    f"Closed {len(open_logs) - 1} duplicate open cycle log(s) for "
                    f"{self.thermostat_entity_id} on startup",
                    {
                        "thermostat": self.thermostat_entity_id,
                        "duplicate_count": len(open_logs) - 1,
                    },
                )

        # Restore cycle log metadata
        self._cycle_log = to_restore
        self._cycle_mode = to_restore.mode  # 'heating' | 'cooling'
        self._cycle_ha_mode = "cool" if to_restore.mode == "cooling" else "heat"
        # Eco Suspend (#500): the at-start snapshot is not persisted, so a
        # restored cycle adopts the suspension state at restore time. Slightly
        # coarser than "state at start", but a restart takes seconds and the
        # alternative (defaulting to False) would resume relaxation mid-cycle
        # for a cycle that started suspended.
        self._cycle_eco_suspended = bool(
            self._get_eco_suspended is not None and self._get_eco_suspended()
        )

        # Restore per-room cycle states (which rooms hit target, when vents
        # closed). Overflow rooms (Issue #254) live in a separate dict so they
        # never leak into the active-room paths; a still-open overflow room
        # (no temp_at_end yet) is re-tracked so the cycle-end finalize can
        # close its data point.
        room_states = await db.get_room_cycle_states(conn, to_restore.id)
        self._room_cycle_states = {
            rcs.room_id: rcs for rcs in room_states if rcs.role != "overflow"
        }
        self._overflow_room_states = {
            rcs.room_id: rcs for rcs in room_states if rcs.role == "overflow"
        }
        self._overflow_room_ids = {
            rid for rid, rcs in self._overflow_room_states.items() if rcs.temp_at_end is None
        }

        # Restore active rooms and vents from the rooms_json snapshot + DB.
        #
        # #604: guarding only the decode is not enough. This runs inside
        # aiohttp's on_startup, so anything that escapes here takes the process
        # down before the HTTP server binds — the Supervisor restarts it, it
        # dies on the same row, and the operator gets a crash loop with no web
        # UI to recover from while the HVAC sits on the last setpoint the
        # engine wrote. The product cannot write a wrong-shaped snapshot itself
        # (both writers json.dumps a dict of dicts whose target is a non-
        # optional float), but a hand-edited backup uploaded to /api/restore,
        # or on-disk corruption that happens to leave well-formed JSON, can. So
        # the shape and the per-field types are validated too — the same
        # both-halves guard _start_or_update_cycle already applies to this
        # column. An entry that will not coerce is treated as an unrestorable
        # room and given the same disposition as a room that no longer exists
        # in the DB, so the cycle still resumes (with fewer rooms) and the
        # timeout monitor and reconciler keep supervising the running HVAC.
        try:
            rooms_snapshot: dict = json.loads(to_restore.rooms_json)
        except (ValueError, TypeError):
            rooms_snapshot = {}
        if not isinstance(rooms_snapshot, dict):
            rooms_snapshot = {}

        skipped = 0
        for room_id, snap in rooms_snapshot.items():
            if not isinstance(snap, dict):
                log.warning(
                    "Restore: room %s snapshot in cycle %s is malformed "
                    "(%s, not an object) — skipping",
                    room_id,
                    to_restore.id,
                    type(snap).__name__,
                )
                skipped += 1
                continue
            room = await db.get_room(conn, room_id)
            if room is None:
                log.warning(
                    "Restore: room %s from cycle %s no longer exists — skipping",
                    room_id,
                    to_restore.id,
                )
                skipped += 1
                continue
            source = snap.get("source", "schedule")
            requested = snap.get("requested_target")
            try:
                # Subscript, not .get(..., 0.0): a snapshot entry with no
                # "target" key at all is exactly as unreadable as one whose
                # target will not coerce, and the old default silently restored
                # it as an active room at 0.0 °F — a target no cooling cycle can
                # ever satisfy, so the room's vent would never close and the
                # cycle would run to the timeout monitor instead of to target.
                # The KeyError lands in the same except as the coercion errors.
                target = float(snap["target"])
                requested_target = None if requested is None else float(requested)
            except (KeyError, ValueError, TypeError):
                log.warning(
                    "Restore: room %s in cycle %s has a missing or non-numeric "
                    "target / requested_target (%r / %r) — skipping",
                    room_id,
                    to_restore.id,
                    snap.get("target"),
                    requested,
                )
                skipped += 1
                continue
            if not isinstance(source, str):
                # A non-string source matches neither "schedule" nor
                # "override", so the #517 deadband re-resolve and the #576
                # respect_eco re-read below would both be skipped for this
                # room — and the next in-place trigger update would serialise
                # the garbage straight back into rooms_json, persisting the
                # corruption this guard exists to contain. Unreadable field,
                # same disposition as an unreadable target.
                log.warning(
                    "Restore: room %s in cycle %s has a non-string source (%r) — skipping",
                    room_id,
                    to_restore.id,
                    source,
                )
                skipped += 1
                continue
            ar = ActiveRoom(room=room, target_temp=target, source=source)
            if requested_target is not None:
                ar.requested_target = requested_target
                ar.eco_active = requested_target != target
            if source == "schedule":
                # #517: the band is not in the snapshot — re-resolve it from the
                # room's blocks as they stand NOW, so an edit made while the
                # server was down is picked up. Without this the room would
                # monitor on the room/thermostat band until the next tick's
                # trigger_changed noticed and forced a spurious cycle update.
                match = _find_matching_schedule(
                    await db.get_schedules_for_room(conn, room_id), datetime.now(UTC)
                )
                if match is not None:
                    ar.deadband_override = match.deadband_override
            elif source == "override":
                # #576: respect_eco is not in the snapshot either — re-read it
                # from the live hold row (the #517 pattern above). Without this
                # a restored ActiveRoom defaults to False and the first tick's
                # _trigger_differs (which compares the flag) would fire a
                # spurious in-place update for every respect_eco=True hold.
                # A row that expired while the server was down leaves the
                # default in place; the next tick re-resolves the source anyway.
                live_hold = await db.get_room_override(conn, room_id)
                if live_hold is not None:
                    ar.respect_eco = live_hold.respect_eco
            self._active_rooms[room_id] = ar
            self._room_vents[room_id] = await db.get_room_vents(conn, room_id)

        # Sanity check: verify the restored mode still makes sense given the
        # thermostat's current ambient temperature.  A server restart can take
        # several minutes; if the HVAC ran while the server was down (or someone
        # changed the thermostat manually), the persisted direction may now be
        # wrong.  For example, a heating cycle persisted when the room was cold
        # might resume after the room has already reached — or exceeded — target.
        # In that case the reconciler would re-assert the wrong HA mode, setting
        # the thermostat to heat when the space actually needs cooling.
        # Fix: close the stale cycle and stay IDLE; the next tick infers fresh.
        thermo_state_now = self._ha.get_state(self.thermostat_entity_id)
        if thermo_state_now and self._active_rooms:
            # Thermostat ambient is in HA's system unit; normalise to °F to
            # compare against the °F room targets. (Issue #280)
            ambient_now = _climate_temp_to_f(
                thermo_state_now.get("attributes", {}).get("current_temperature"),
                self._ha.ha_temp_unit,
            )
            if ambient_now is not None:
                try:
                    all_targets = [ar.target_temp for ar in self._active_rooms.values()]
                    contradicts = (
                        to_restore.mode == "heating" and ambient_now > max(all_targets)
                    ) or (to_restore.mode == "cooling" and ambient_now < min(all_targets))
                    if contradicts:
                        await db.close_cycle_log(
                            conn,
                            to_restore.id,
                            datetime.now(UTC),
                            ended_reason="discarded_stale_on_restore",
                        )
                        log.warning(
                            "Restore: discarding stale %s cycle %s for %s — "
                            "thermostat ambient %.1f°F contradicts restored mode "
                            "(targets min=%.1f max=%.1f). Next tick starts fresh.",
                            to_restore.mode,
                            to_restore.id,
                            self.thermostat_entity_id,
                            ambient_now,
                            min(all_targets),
                            max(all_targets),
                        )
                        if self._logger:
                            await self._logger.log(
                                "warning",
                                "engine",
                                f"Restore: discarding stale {to_restore.mode} cycle for "
                                f"{self.thermostat_entity_id} — thermostat ambient "
                                f"{ambient_now:.1f}°F contradicts restored mode. "
                                f"Next tick will infer the correct direction from current temps.",
                                {
                                    "thermostat": self.thermostat_entity_id,
                                    "cycle_id": to_restore.id,
                                    "restored_mode": to_restore.mode,
                                    "thermostat_ambient": ambient_now,
                                    "targets": all_targets,
                                },
                            )
                        # Physical cleanup (#429): the thermostat is still
                        # commanded at the discarded cycle's overshoot setpoint,
                        # and with no open cycle log there is no timeout monitor
                        # or watchdog supervising it. Mirror the abort tail:
                        # park the setpoint on the idle side of the discarded
                        # direction, reopen the zone's vents, and arm the
                        # off-time lockout for the run that just ended.
                        try:
                            tc = await db.get_thermostat_config(conn, self.thermostat_entity_id)
                            ha_mode = "cool" if to_restore.mode == "cooling" else "heat"
                            parked = self._parked_setpoint(
                                ambient_now, to_restore.mode, tc.overshoot_delta
                            )
                            await self._ha.set_thermostat_temperature(
                                self.thermostat_entity_id, parked, hvac_mode=ha_mode
                            )
                            self._last_setpoint_sent = parked
                        except Exception as exc:
                            log.error("Restore discard: failed to park setpoint: %s", exc)
                        try:
                            all_zone_vents = await self._get_all_zone_vents(conn)
                            if all_zone_vents:
                                await self._vent.open_room_vents(all_zone_vents)
                        except Exception as exc:
                            log.error("Restore discard: failed to reopen zone vents: %s", exc)
                        self._last_cycle_ended_at = datetime.now(UTC)
                        # Reset to clean IDLE — _active_rooms etc. were set above
                        # but nothing should persist; the engine was never RUNNING.
                        self._cycle_log = None
                        self._cycle_mode = None
                        self._cycle_ha_mode = None
                        self._active_rooms = {}
                        self._room_cycle_states = {}
                        self._room_vents = {}
                        # Overflow bookkeeping was repopulated from DB just above;
                        # clear it too so a discarded cycle's overflow rooms don't
                        # leak into the next freshly-started cycle (Issue #300).
                        self._overflow_room_states = {}
                        self._overflow_room_ids = set()
                        return
                except (ValueError, TypeError):
                    pass

        # Transition to RUNNING
        self._state = CycleState.RUNNING

        # Force reconciliation on first tick — _last_setpoint_sent can't be
        # recovered from DB, so setpoint won't be checked, but mode drift will be.
        self._last_reconciled_at = None

        # Re-assert idle-room vent closure. _terminate_cycle re-opens every
        # zone vent at cycle end, so before the server went down the idle
        # rooms' vents were closed by the original _start_or_update_cycle
        # call. But nothing in the restore path re-runs that logic, so after
        # a reboot idle vents would stay open and dilute airflow until the
        # cycle naturally ends. Close them now so the restored cycle behaves
        # identically to the pre-restart one.
        tc = await db.get_thermostat_config(conn, self.thermostat_entity_id)
        await self._close_idle_room_vents(conn, tc)

        room_names = [ar.room.name for ar in self._active_rooms.values()]
        log.info(
            "Restored cycle %s for %s (mode=%s, rooms=%s%s)",
            to_restore.id,
            self.thermostat_entity_id,
            to_restore.mode,
            room_names,
            f", skipped {skipped} deleted/unrestorable rooms" if skipped else "",
        )
        if self._logger:
            await self._logger.log(
                "info",
                "engine",
                f"Restored cycle {to_restore.id} for {self.thermostat_entity_id} "
                f"from DB on startup (mode={to_restore.mode}, rooms={room_names})",
                {
                    "thermostat": self.thermostat_entity_id,
                    "cycle_id": to_restore.id,
                    "mode": to_restore.mode,
                    "rooms": room_names,
                    "rooms_skipped": skipped,
                },
            )

    async def _announce_vacation_hold(
        self, posture: str, level: str, message: str, details: dict
    ) -> None:
        """Write ONE event per vacation-hold posture change (Issue #627).

        The hold is the only thing driving the thermostat on a range-mode trip,
        or on any trip with ``vacation_safety_cycles`` off, and it used to
        command heat, cool and off without writing a single ``event_log`` row —
        so from the Logs page a hold that was working looked exactly like a
        hold that was dead. It also re-evaluates every 60 s for days, so a
        per-tick event would bury the Live Feed; #211 (sensor staleness) and
        #270 (thermostat outage) rate-limit to once per episode for the same
        reason, and this follows them.

        ``posture`` is the state key: the commanded mode plus the bound it was
        commanded to, so editing ``min_setpoint`` mid-trip re-announces the new
        bound while a stable hold stays silent. Failures carry their own
        ``-failed`` posture, so a thermostat that keeps rejecting the command
        is reported once rather than alternating success/failure every tick.
        The posture is updated whether or not a logger is attached, so the
        rate-limiting behaves identically in both cases.
        """
        if self._vacation_hold_posture == posture:
            return
        self._vacation_hold_posture = posture
        if self._logger:
            await self._logger.log(level, "engine", message, details)

    def _read_validated_ambient_f(self, thermo_state: dict) -> float | None:
        """Plausibility-checked thermostat ambient reading (Issue #636).

        The unmemoized core check. Nothing outside `_validated_ambient_for_tick`
        should call this directly — it is stateful (the rate-of-change check
        below compares against real elapsed wall-clock time since the last
        ACCEPTED reading), so calling it more than once within the same tick
        would compare near-simultaneous calls against each other and reject
        almost any change. `_validated_ambient_for_tick` runs this at most
        once per tick and memoizes the answer for every reader that tick:
        `_enforce_safety_setpoint` and `_apply_vacation_hold` (the two
        no-demand supervision arms that drive the thermostat directly off its
        own probe with no room in the loop) and `_get_avg_temp` (the
        ``include_thermostat_sensor`` room-temperature proxy, called from
        15+ places per tick). Without this guard, a reconnect glitch (the
        classic 0°C/32°F null value) would reach all three. Neither existing
        guard catches it: #211 sensor staleness only watches ROOM sensors,
        and #267's unavailable-abort only watches the entity's ``state`` — a
        glitched ``current_temperature`` is a valid, fresh, non-stale float
        that passes both.

        Two independent checks, either one rejects the reading:

        * Absolute band (`AMBIENT_PLAUSIBLE_MIN_F`..`AMBIENT_PLAUSIBLE_MAX_F`):
          no occupied indoor space reports outside this range, so a
          thermostat that does is reporting a fault, not a temperature.
        * Rate of change (`AMBIENT_MAX_RATE_F_PER_MIN`): building thermal mass
          makes more than a few °F per minute of real drift physically
          impossible, so this also catches a glitch that happens to land
          inside the absolute band — in either direction: a reading that
          jumps implausibly far below OR above the last accepted one is
          rejected the same way, so the mirror-image glitch (an implausibly
          HOT reading) gets the identical treatment.

        A rejected reading returns ``None`` — exactly the outcome of a
        genuinely missing/unparseable one — so `_apply_vacation_hold`'s
        existing #627 ``unreadable:no-ambient`` bail-out needs no new code
        path; it already treats "no ambient" as "command HVAC off, announce
        once." `_enforce_safety_setpoint` has no branch of its own for that,
        so it consults `_ambient_reject_detail` (set here) to announce the
        rejection itself, once per episode.

        Accepting a reading updates the baseline used for the NEXT rate
        check; rejecting one does not, so a run of glitched ticks is judged
        against the last known-good value, not against another glitch.
        """
        attrs = thermo_state.get("attributes", {})
        raw = _climate_temp_to_f(attrs.get("current_temperature"), self._ha.ha_temp_unit)
        if raw is None:
            # No reading at all is a different, pre-existing condition (the
            # callers already fail safe on it) — not an episode this guard
            # tracks, so end any rejection episode that happened to precede it.
            # Both fields must clear together: leaving `_ambient_reject_warned`
            # set here would silently swallow the announcement for a LATER,
            # genuinely new rejection episode (e.g. a fresh glitch value
            # arriving after a tick with no reading at all in between) — the
            # warned flag would still read True from the earlier episode even
            # though `_ambient_reject_detail` had already been cleared.
            self._ambient_reject_detail = None
            self._ambient_reject_warned = False
            return None

        if not (AMBIENT_PLAUSIBLE_MIN_F <= raw <= AMBIENT_PLAUSIBLE_MAX_F):
            self._ambient_reject_detail = (
                f"{raw:.1f}°F is outside the plausible indoor range "
                f"({AMBIENT_PLAUSIBLE_MIN_F:.0f}–{AMBIENT_PLAUSIBLE_MAX_F:.0f}°F)"
            )
            log.warning(
                "Rejecting implausible ambient reading for %s: %.1f°F is outside "
                "the plausible indoor range (%.0f–%.0f°F)",
                self.thermostat_entity_id,
                raw,
                AMBIENT_PLAUSIBLE_MIN_F,
                AMBIENT_PLAUSIBLE_MAX_F,
            )
            return None

        now = datetime.now(UTC)
        if self._last_valid_ambient_f is not None and self._last_valid_ambient_at is not None:
            elapsed_min = (now - self._last_valid_ambient_at).total_seconds() / 60.0
            elapsed_min = max(elapsed_min, _AMBIENT_RATE_MIN_ELAPSED_MIN)
            allowed = AMBIENT_MAX_RATE_F_PER_MIN * elapsed_min
            delta = abs(raw - self._last_valid_ambient_f)
            if delta > allowed:
                self._ambient_reject_detail = (
                    f"{raw:.1f}°F is a {delta:.1f}°F change from the last "
                    f"accepted reading ({self._last_valid_ambient_f:.1f}°F) in "
                    f"{elapsed_min:.1f} min — faster than "
                    f"{AMBIENT_MAX_RATE_F_PER_MIN:.1f}°F/min of real drift is possible"
                )
                log.warning(
                    "Rejecting implausible ambient reading for %s: %.1f°F is a %.1f°F "
                    "change from the last accepted %.1f°F in %.1f min",
                    self.thermostat_entity_id,
                    raw,
                    delta,
                    self._last_valid_ambient_f,
                    elapsed_min,
                )
                return None

        self._ambient_reject_detail = None
        self._ambient_reject_warned = False
        self._last_valid_ambient_f = raw
        self._last_valid_ambient_at = now
        return raw

    def _validated_ambient_for_tick(self, thermo_state: dict | None) -> float | None:
        """Memoized, per-tick wrapper around `_read_validated_ambient_f` (#636).

        Every consumer of the validated ambient reading — `_apply_vacation_hold`,
        `_enforce_safety_setpoint`, and `_get_avg_temp`'s
        `include_thermostat_sensor` proxy — calls THIS, never
        `_read_validated_ambient_f` directly, so the stateful rate-of-change
        check runs at most once per tick regardless of how many rooms or
        code paths ask this tick. See the `_tick_ambient_computed` field
        comment in `__init__` for why: `_get_avg_temp` alone can be called
        15+ times in one tick, and the rate check's window is real elapsed
        WALL-CLOCK time — comparing multiple effectively-simultaneous calls
        against each other would reject on almost any nonzero change.

        ``thermo_state`` may be ``None`` (`_get_avg_temp` calls
        ``self._ha.get_state(room.thermostat_entity_id)``, which returns
        ``None`` when the entity is not yet in the HA cache) — treated the
        same as an unreadable reading, matching the pre-existing
        `_get_avg_temp` behaviour of skipping the thermostat-probe
        contribution when ``thermo`` is falsy.
        """
        if self._tick_ambient_computed:
            return self._tick_validated_ambient_f
        self._tick_ambient_computed = True
        self._tick_validated_ambient_f = (
            self._read_validated_ambient_f(thermo_state) if thermo_state is not None else None
        )
        return self._tick_validated_ambient_f

    async def _apply_vacation_hold(
        self, conn: aiosqlite.Connection, thermo_state: dict | None
    ) -> None:
        """Apply the configured vacation hold strategy for this thermostat.

        Called on every tick while vacation mode is active (after any running
        cycle has been aborted). ``thermo_state`` is the already-fetched HA
        state dict (may be None / unavailable — we bail out in that case).

        Every outcome announces itself through ``_announce_vacation_hold``
        (Issue #627) — once per transition, never per tick.
        """
        if thermo_state is None or thermo_state.get("state") == "unavailable":
            await self._announce_vacation_hold(
                "unreadable:unavailable",
                "warning",
                f"Vacation hold for {self.thermostat_entity_id} is not running — the "
                "thermostat is unavailable in Home Assistant, so the engine cannot tell "
                "what it is doing and issues no commands. The hold resumes when the "
                "thermostat reports again.",
                {"thermostat": self.thermostat_entity_id, "reason": "unavailable"},
            )
            return

        tc = await db.get_thermostat_config(conn, self.thermostat_entity_id)

        if tc.vacation_hvac_mode == "range":
            # Set thermostat to heat_cool/auto with low=min_setpoint, high=max_setpoint.
            # The bounds are commanded BARE here — no deadband inset (#628).
            # That is deliberate, not the flapping defect the single-setpoint
            # branch had: in heat_cool the EQUIPMENT decides heat vs cool from
            # its own probe and applies its own internal hysteresis, so there
            # is no arrive-and-immediately-shut-off edge for us to cushion.
            # Insetting would just condition the house more tightly than the
            # user asked for.
            # ACCEPTED GAP (#426, re-examined under #628): unlike the
            # single-setpoint branch below, range mode does not defer for the
            # compressor off-time lockout, and cannot re-arm it either —
            # heat_cool range semantics don't map onto a single-direction
            # deferral, and the hold never commands this thermostat off, so
            # there is no compressor-stop instant for it to stamp. Activating
            # range-mode vacation mid-cooling-cycle can therefore still let the
            # thermostat restart the compressor early. Closing it would mean
            # taking the heat/cool decision back off the equipment, which is
            # the #26/#29 inversion bug; the honest fix is to use single
            # setpoint, which the Thermostats page and docs/vacation-mode.md
            # both recommend for exactly this reason.
            # Idempotence (#434/#296): skip the write when the thermostat is
            # already holding the range — re-commanding every 60 s tick for
            # days of vacation is thousands of redundant cloud-thermostat
            # writes. External drift still gets corrected (values differ).
            attrs = thermo_state.get("attributes", {})
            unit = self._ha.ha_temp_unit
            cur_low = _climate_temp_to_f(attrs.get("target_temp_low"), unit)
            cur_high = _climate_temp_to_f(attrs.get("target_temp_high"), unit)
            already_held = (
                thermo_state.get("state") == "heat_cool"
                and cur_low is not None
                and cur_high is not None
                and abs(cur_low - tc.min_setpoint) <= _SETPOINT_DRIFT_TOLERANCE_F
                and abs(cur_high - tc.max_setpoint) <= _SETPOINT_DRIFT_TOLERANCE_F
            )
            if already_held:
                return
            range_details = {
                "thermostat": self.thermostat_entity_id,
                "min_setpoint": tc.min_setpoint,
                "max_setpoint": tc.max_setpoint,
                "action": "range",
            }
            try:
                await self._ha.set_thermostat_temperature_range(
                    self.thermostat_entity_id, tc.min_setpoint, tc.max_setpoint
                )
            except Exception as exc:
                log.error(
                    "Vacation hold: failed to set range for %s: %s",
                    self.thermostat_entity_id,
                    exc,
                )
                await self._announce_vacation_hold(
                    f"range-failed:{tc.min_setpoint:.1f}:{tc.max_setpoint:.1f}",
                    "error",
                    f"Vacation hold for {self.thermostat_entity_id} could not set the "
                    f"heat_cool range {tc.min_setpoint:.1f}°F–{tc.max_setpoint:.1f}°F — "
                    "Home Assistant rejected the command. The hold retries every tick.",
                    range_details,
                )
                return
            # Announced only on the tick that actually writes: the idempotence
            # skip above returns before this on every subsequent tick, so a
            # multi-day range hold logs when it takes the thermostat and when
            # the bounds change, not 1440 times a day.
            await self._announce_vacation_hold(
                f"range:{tc.min_setpoint:.1f}:{tc.max_setpoint:.1f}",
                "info",
                f"Vacation hold for {self.thermostat_entity_id}: holding the heat_cool "
                f"range {tc.min_setpoint:.1f}°F–{tc.max_setpoint:.1f}°F. The equipment "
                "picks heat vs cool from its own probe, so no per-room safety cycles run.",
                range_details,
            )
            return

        # Single-setpoint mode: turn off unless a bound is breached.
        # current_temperature is reported in HA's system unit; normalise to °F so
        # it compares correctly against the °F min/max setpoints. (Issue #280)
        # Routed through the plausibility guard (Issue #636) rather than
        # `_climate_temp_to_f` directly, so a reconnect glitch is rejected and
        # falls into the `current_temp_f is None` branch just below — the
        # SAME "no ambient" bail-out a genuinely missing reading takes. Uses
        # the per-tick memoized wrapper, not `_read_validated_ambient_f`
        # directly, so this shares one answer with `_get_avg_temp` and
        # `_enforce_safety_setpoint` for the tick.
        current_temp_f = self._validated_ambient_for_tick(thermo_state)
        current_hvac_mode = thermo_state.get("state", "off")

        if current_temp_f is None:
            if current_hvac_mode != "off":
                try:
                    await self._ha.set_thermostat_hvac_mode(self.thermostat_entity_id, "off")
                except Exception as exc:
                    log.error(
                        "Vacation hold: failed to turn off %s: %s",
                        self.thermostat_entity_id,
                        exc,
                    )
                else:
                    # Losing the ambient reading still stops a running
                    # compressor, so the lockout re-arms here too (#628).
                    self._note_hold_stopped_compressor(current_hvac_mode)
            # The bail-out that most resembles a dead system (#627): the
            # thermostat is reachable but has no ambient the hold can trust,
            # so it has nothing to compare against the band and stops
            # deciding. Announce the condition itself — whether or not the
            # "off" command was needed or succeeded, the house is now
            # unsupervised.
            #
            # Issue #636: `_ambient_reject_detail` (set by the plausibility
            # guard) distinguishes a genuinely MISSING reading from one that
            # WAS reported but rejected as implausible — the generic "reports
            # no current temperature" text below is simply false for the
            # rejected case (a value like 32.0°F absolutely was reported), and
            # the issue's own item 4 asks that a rejection be announced "with
            # the rejected value". Surface it when present.
            if self._ambient_reject_detail is not None:
                message = (
                    f"Vacation hold for {self.thermostat_entity_id} rejected its "
                    f"ambient reading — {self._ambient_reject_detail}. The hold "
                    "cannot tell whether the house is inside the vacation band off "
                    "a reading it does not trust, so HVAC has been commanded off "
                    "until a plausible reading returns."
                )
                reason = "ambient_rejected"
            else:
                message = (
                    f"Vacation hold for {self.thermostat_entity_id} has no ambient "
                    "reading — the thermostat reports no current temperature, so the "
                    "hold cannot tell whether the house is inside the vacation band. "
                    "HVAC has been commanded off until a reading returns."
                )
                reason = "no_ambient"
            await self._announce_vacation_hold(
                "unreadable:no-ambient",
                "warning",
                message,
                {
                    "thermostat": self.thermostat_entity_id,
                    "reason": reason,
                    "detail": self._ambient_reject_detail,
                },
            )
            return

        current_sp_f = _climate_temp_to_f(
            thermo_state.get("attributes", {}).get("temperature"), self._ha.ha_temp_unit
        )

        # Hysteresis (#628). TRIGGER on the bare bound below, but RECOVER to
        # one deadband inside it — the same treatment #367 gave safety rooms,
        # with the identical clamp to the opposite bound that
        # `_add_safety_rooms` applies. Commanding the bound itself is what made
        # the hold flap: it heats to exactly `min_setpoint`, arrives, the
        # `< min_setpoint` test immediately goes false, the else-branch shuts
        # the HVAC off, the zone drifts back across the bound and it starts
        # again — edge short-cycling with no margin at all, on the one code
        # path that runs unattended for days.
        #
        # This is NOT #32's abandoned setpoint clamp: the envelope is not being
        # used to clip some target the engine computed elsewhere. The bound is
        # the trigger and the deadband-inset value is the hold's own target,
        # exactly as the issue distinguishes them.
        heat_target = min(tc.max_setpoint, tc.min_setpoint + tc.deadband)
        cool_target = max(tc.min_setpoint, tc.max_setpoint - tc.deadband)

        def _holding(mode: str, target: float) -> bool:
            # Idempotence (#434/#296): skip re-commanding a hold the
            # thermostat is already executing. Compares against the TARGET the
            # hold commands, not the bound that triggered it (#628).
            return (
                current_hvac_mode == mode
                and current_sp_f is not None
                and abs(current_sp_f - target) <= _SETPOINT_DRIFT_TOLERANCE_F
            )

        if current_temp_f < tc.min_setpoint:
            # Too cold — heat to one deadband INSIDE the minimum bound (#628).
            heat_details = {
                "thermostat": self.thermostat_entity_id,
                "current_temp": current_temp_f,
                "min_setpoint": tc.min_setpoint,
                "target": heat_target,
                "action": "heat",
            }
            if not _holding("heat", heat_target):
                try:
                    await self._ha.set_thermostat_temperature(
                        self.thermostat_entity_id, heat_target, hvac_mode="heat"
                    )
                except Exception as exc:
                    log.error(
                        "Vacation hold: failed to heat %s to %.1f°F: %s",
                        self.thermostat_entity_id,
                        heat_target,
                        exc,
                    )
                    await self._announce_vacation_hold(
                        f"heat-failed:{heat_target:.1f}",
                        "error",
                        f"Vacation hold for {self.thermostat_entity_id} could not command "
                        f"heat to {heat_target:.1f}°F with ambient at "
                        f"{current_temp_f:.1f}°F — Home Assistant rejected the command. "
                        "The hold retries every tick.",
                        heat_details,
                    )
                    return
                # A swing straight through the band can take the thermostat
                # from cool to heat without passing the else-branch, and that
                # still stopped the compressor (#628).
                self._note_hold_stopped_compressor(current_hvac_mode)
            # Reached on the commanding tick AND on a tick that found the
            # thermostat already holding (engine restart mid-trip, or the user
            # set it by hand) — the posture, not the write, is the news.
            await self._announce_vacation_hold(
                f"heat:{heat_target:.1f}",
                "info",
                f"Vacation hold for {self.thermostat_entity_id}: ambient "
                f"{current_temp_f:.1f}°F is below min_setpoint {tc.min_setpoint:.1f}°F — "
                f"holding heat at {heat_target:.1f}°F, one deadband inside the bound.",
                heat_details,
            )
        elif current_temp_f > tc.max_setpoint:
            # Too hot — cool to the maximum bound. Respect the compressor
            # off-time lockout (#208/#426): vacation activation aborts any
            # running cycle in the SAME tick, so without this gate the hold
            # could stop and restart the compressor within seconds. The hold
            # re-evaluates every tick, so cooling starts once the lockout
            # elapses. (Heating above is furnace-side and stays exempt.)
            cool_details = {
                "thermostat": self.thermostat_entity_id,
                "current_temp": current_temp_f,
                "max_setpoint": tc.max_setpoint,
                "target": cool_target,
                "action": "cool",
            }
            if not _holding("cool", cool_target):
                if self._in_offtime_lockout(tc):
                    remaining = self._offtime_lockout_remaining(tc)
                    log.warning(
                        "Vacation hold for %s deferred — compressor off-time lockout, "
                        "%.1f min remaining",
                        self.thermostat_entity_id,
                        remaining,
                    )
                    # Also once-per-transition (#627). This warning predates the
                    # rest and fired on every tick of the lockout; the lockout
                    # can run for tens of minutes, so it now announces the
                    # deferral once and the "holding cool" line below marks its
                    # end.
                    await self._announce_vacation_hold(
                        f"cool-deferred:{cool_target:.1f}",
                        "warning",
                        f"Vacation hold for {self.thermostat_entity_id} deferred — ambient "
                        f"{current_temp_f:.1f}°F is above max_setpoint "
                        f"{tc.max_setpoint:.1f}°F, but the compressor off-time lockout has "
                        f"{remaining:.1f} min remaining. Cooling will be commanded when it "
                        "elapses.",
                        {
                            "thermostat": self.thermostat_entity_id,
                            "current_temp": current_temp_f,
                            "max_setpoint": tc.max_setpoint,
                            "lockout_remaining_min": round(remaining, 1),
                        },
                    )
                    return
                try:
                    await self._ha.set_thermostat_temperature(
                        self.thermostat_entity_id, cool_target, hvac_mode="cool"
                    )
                except Exception as exc:
                    log.error(
                        "Vacation hold: failed to cool %s to %.1f°F: %s",
                        self.thermostat_entity_id,
                        cool_target,
                        exc,
                    )
                    await self._announce_vacation_hold(
                        f"cool-failed:{cool_target:.1f}",
                        "error",
                        f"Vacation hold for {self.thermostat_entity_id} could not command "
                        f"cooling to {cool_target:.1f}°F with ambient at "
                        f"{current_temp_f:.1f}°F — Home Assistant rejected the command. "
                        "The hold retries every tick.",
                        cool_details,
                    )
                    return
            await self._announce_vacation_hold(
                f"cool:{cool_target:.1f}",
                "info",
                f"Vacation hold for {self.thermostat_entity_id}: ambient "
                f"{current_temp_f:.1f}°F is above max_setpoint {tc.max_setpoint:.1f}°F — "
                f"holding cooling at {cool_target:.1f}°F, one deadband inside the bound.",
                cool_details,
            )
        else:
            # Temperature is within the safe band — ensure HVAC is off.
            off_details = {
                "thermostat": self.thermostat_entity_id,
                "current_temp": current_temp_f,
                "min_setpoint": tc.min_setpoint,
                "max_setpoint": tc.max_setpoint,
                "action": "off",
            }
            if current_hvac_mode != "off":
                try:
                    await self._ha.set_thermostat_hvac_mode(self.thermostat_entity_id, "off")
                except Exception as exc:
                    log.error(
                        "Vacation hold: failed to turn off %s: %s",
                        self.thermostat_entity_id,
                        exc,
                    )
                    await self._announce_vacation_hold(
                        f"off-failed:{tc.min_setpoint:.1f}:{tc.max_setpoint:.1f}",
                        "error",
                        f"Vacation hold for {self.thermostat_entity_id} could not turn the "
                        f"HVAC off with ambient {current_temp_f:.1f}°F back inside the "
                        f"vacation band {tc.min_setpoint:.1f}°F–{tc.max_setpoint:.1f}°F — "
                        "Home Assistant rejected the command. The hold retries every tick.",
                        off_details,
                    )
                    return
                # The hold just ended a compressor run, so the off-time lockout
                # must re-arm (#628) — otherwise it expires once, early in the
                # trip, and the next bound breach restarts the compressor with
                # no protection at all.
                self._note_hold_stopped_compressor(current_hvac_mode)
            # Announced on the transition into the band, and on the first tick
            # of a trip that starts inside it — so the Live Feed says the hold
            # is alive rather than showing nothing for a week.
            await self._announce_vacation_hold(
                f"off:{tc.min_setpoint:.1f}:{tc.max_setpoint:.1f}",
                "info",
                f"Vacation hold for {self.thermostat_entity_id}: ambient "
                f"{current_temp_f:.1f}°F is inside the vacation band "
                f"{tc.min_setpoint:.1f}°F–{tc.max_setpoint:.1f}°F — HVAC is off.",
                off_details,
            )

    def _vacation_safety_enabled(self, tc: ThermostatConfig) -> bool:
        """Whether this thermostat runs per-room safety cycles in vacation (#626).

        Two conditions, both required:

        * ``vacation_safety_cycles`` — the user-facing opt-out.
        * ``vacation_hvac_mode == "single"`` — a hard restriction, not a
          preference. A "range" thermostat holds ``heat_cool`` and the
          EQUIPMENT decides heat vs cool from its own built-in probe. The
          engine cannot lock a cycle direction it does not own, and #26/#29
          settled that deriving direction from the thermostat's live state
          inverts every vent decision at exactly the moment it matters. Rather
          than re-fight that, range mode keeps the pre-#626 hold and senses on
          the thermostat's internal sensor alone; the Thermostats page says so
          next to the selector.
        """
        return bool(tc.vacation_safety_cycles) and tc.vacation_hvac_mode == "single"

    async def _apply_vacation_vent_policy(
        self, conn: aiosqlite.Connection, tc: ThermostatConfig, hvac_mode: str
    ) -> None:
        """Share a vacation safety cycle's air with every non-breaching room (#626).

        With the house empty there is no comfort target to protect, so the zone
        runs wide open and each idle room keeps absorbing conditioned air until
        it nears its OWN opposite safety bound — cooling stops at
        ``min_setpoint + deadband``, heating at ``max_setpoint - deadband``.
        That band is the room's own ``deadband_override`` when set (#305).

        Deliberately NOT the #237 tier system: ``get_overflow_candidates`` ranks
        against a room's *comfort* setpoint (``system_wide_temp`` →
        ``default_temp``) and skips rooms that have neither, which during
        vacation is the wrong question and the wrong exclusion. It keeps its
        ``in_vacation`` short-circuit; nothing here touches it.

        Closes are ordered most-satisfied-first so that when the airflow floor
        (#210/#213) starts refusing them, the vents left open belong to the
        rooms with the most headroom — otherwise which room stayed open fell
        out of dict iteration order.
        """
        if hvac_mode not in ("cooling", "heating"):
            return

        zone_rooms = await db.get_rooms_for_thermostat(conn, self.thermostat_entity_id)
        active_ids = set(self._active_rooms.keys())
        all_zone_vents = await self._get_all_zone_vents(conn)
        required = required_open_vents(tc, len(all_zone_vents))

        keep_open: list[Room] = []
        # (past_by, room_id, room) — room_id breaks ties so the ordering is
        # total and the resulting vent commands are reproducible in tests.
        close_candidates: list[tuple[float, str, Room]] = []

        for room in zone_rooms:
            if room.id in active_ids:
                continue  # the breaching rooms; `_monitor_rooms` owns their vents
            avg = self._get_avg_temp(room)
            if avg is None:
                # No fresh reading — fail OPEN. An unreadable room cannot be
                # shown to be near its bound, and extra airflow is the safe
                # error here (#210 dead-heading is the hazard, not overshoot).
                keep_open.append(room)
                continue
            effective = avg + room.temp_offset
            band = _effective_deadband(room, tc.deadband)
            if hvac_mode == "cooling":
                threshold = tc.min_setpoint + band
                past_by = threshold - effective
            else:
                threshold = tc.max_setpoint - band
                past_by = effective - threshold
            if past_by >= 0:
                close_candidates.append((past_by, room.id, room))
            else:
                keep_open.append(room)

        for room in keep_open:
            vents = await db.get_room_vents(conn, room.id)
            if vents:
                await self._vent.open_room_vents(vents)

        close_candidates.sort(key=lambda c: (-c[0], c[1]))
        for past_by, _room_id, room in close_candidates:
            vents = await db.get_room_vents(conn, room.id)
            if not vents:
                continue
            would_close = sum(1 for v in vents if self._vent._is_open(v))
            if would_close == 0:
                continue  # already closed; no command, no log
            open_count = self._vent._count_open_vents(all_zone_vents)
            if (open_count - would_close) < required:
                # Every remaining candidate has less headroom than this one, so
                # none of them may close either — stop rather than keep probing.
                log.info(
                    "Vacation vent policy: keeping %s open — airflow floor requires %d open",
                    room.name,
                    required,
                )
                break
            await self._vent.force_close_vents(vents)
            log.info(
                "Vacation vent policy: closed %s (%.1f°F past its %s bound)",
                room.name,
                past_by,
                "minimum" if hvac_mode == "cooling" else "maximum",
            )
            if self._logger:
                bound_field = "min_setpoint" if hvac_mode == "cooling" else "max_setpoint"
                bound_val = tc.min_setpoint if hvac_mode == "cooling" else tc.max_setpoint
                await self._logger.log(
                    "info",
                    "engine",
                    f"Vacation: closed vents in '{room.name}' — it is within its deadband of "
                    f"the {bound_field} {bound_val:.1f}°F and would overshoot if it kept "
                    f"absorbing air from this safety cycle.",
                    {
                        "thermostat": self.thermostat_entity_id,
                        "room_id": room.id,
                        "room_name": room.name,
                        "bound": bound_field,
                        "bound_value": bound_val,
                        "past_by": round(past_by, 2),
                        "mode": hvac_mode,
                    },
                )

    # ------------------------------------------------------------------
    # Safety protection (Issue #367)
    # ------------------------------------------------------------------

    async def _add_safety_rooms(
        self, conn: aiosqlite.Connection, new_active_map: dict[str, ActiveRoom]
    ) -> None:
        """Activate any zone room that has breached the comfort envelope.

        The per-room ``max_setpoint`` / ``min_setpoint`` hard cap only protects
        rooms that already have demand (presence/schedule/override). A room with
        no demand source is never evaluated, so it can bake past the ceiling
        while a cycle runs for other rooms — e.g. a presence cycle holds the
        Bedroom at 70°F while the unoccupied Gym climbs to 78°F over a 77°F
        ceiling.

        This pulls such a room into the active set with ``source="safety"`` so
        the normal cycle machinery conditions it: it joins the running cycle
        when the direction matches, or starts a fresh one when the zone is idle.
        The target is one deadband inside the breached bound (clamped to the
        envelope) — cool to ``max_setpoint - deadband`` / heat to
        ``min_setpoint + deadband`` — so the room is brought safely back inside
        the envelope rather than fully conditioned like an occupied room.

        The hysteresis is two-sided (#633): protection ARMS on the bare bound
        but is SUSTAINED, via ``_safety_sustain``, until the room reaches that
        inset target. Testing the bare bound on every tick is what made the
        protection unable to finish its own job — see that field's comment.

        Mutates ``new_active_map`` in place. Rooms already active (real demand)
        are left untouched; rooms with no usable sensor reading are skipped (a
        sensorless zone is covered by the thermostat-ambient backstop instead).
        """
        tc = await db.get_thermostat_config(conn, self.thermostat_entity_id)
        zone_rooms = await db.get_rooms_for_thermostat(conn, self.thermostat_entity_id)
        breaching_now: set[str] = set()
        sustaining_now: dict[str, str] = {}
        for room in zone_rooms:
            if room.id in new_active_map:
                continue  # already conditioned via override/schedule/presence
            avg = self._get_avg_temp(room)
            if avg is None:
                continue  # no fresh sensor reading — nothing to act on per-room
            effective = avg + room.temp_offset
            # No schedule band here, deliberately (#517): this loop skipped
            # every room in `new_active_map` above, and a room with a matching
            # enabled block is active by construction — so a room reaching this
            # line cannot have one. Room→thermostat is the whole chain.
            deadband = _effective_deadband(room, tc.deadband)

            # TRIGGER on the bare bound, SUSTAIN until the inset target is
            # reached (#633) — the treatment #628 gave the vacation hold, which
            # this loop's docstring already claims ("a built-in hysteresis
            # margin that prevents edge short-cycling") but did not implement:
            # the target was inset, the re-arm test was not. A room that is
            # already under protection stays under it until it actually
            # arrives, instead of dropping out 0.1°F inside the bound and
            # leaving the cycle it was the sole demand for with nothing to do.
            cool_target = max(tc.min_setpoint, tc.max_setpoint - deadband)
            heat_target = min(tc.max_setpoint, tc.min_setpoint + deadband)
            sustaining = self._safety_sustain.get(room.id)
            cool_bound = cool_target if sustaining == "cooling" else tc.max_setpoint
            heat_bound = heat_target if sustaining == "heating" else tc.min_setpoint

            if effective > cool_bound:
                target = cool_target
                mode_word, bound_word, bound_val = "cooling", "maximum", tc.max_setpoint
                bound_field = "max_setpoint"
            elif effective < heat_bound:
                target = heat_target
                mode_word, bound_word, bound_val = "heating", "minimum", tc.min_setpoint
                bound_field = "min_setpoint"
            else:
                continue  # recovered to target (or never breached) — release

            sustaining_now[room.id] = mode_word
            breaching_now.add(room.id)
            new_active_map[room.id] = ActiveRoom(room=room, target_temp=target, source="safety")

            # Announce once per breach episode, not every tick the room stays
            # over/under the envelope (it may take many minutes to recover).
            if room.id not in self._safety_warned_room_ids:
                self._safety_warned_room_ids.add(room.id)
                log.warning(
                    "Safety protection: room %s at %.1f°F breached the %s setpoint "
                    "%.1f°F with no active demand — adding it to a %s cycle (target %.1f°F)",
                    room.name,
                    effective,
                    bound_word,
                    bound_val,
                    mode_word,
                    target,
                )
                if self._logger:
                    await self._logger.log(
                        "warning",
                        "engine",
                        f"Safety protection engaged for room '{room.name}': it reached "
                        f"{effective:.1f}°F, past the {bound_word} setpoint {bound_val:.1f}°F, "
                        f"with no schedule, presence, or override. Adding it to a {mode_word} "
                        f"cycle (target {target:.1f}°F) to bring it back inside the comfort "
                        f"envelope.",
                        {
                            "thermostat": self.thermostat_entity_id,
                            "room_id": room.id,
                            "room_name": room.name,
                            "room_temp": effective,
                            "bound": bound_field,
                            "bound_value": bound_val,
                            "target_temp": target,
                            "source": "safety",
                        },
                    )

        # Drop rooms that recovered (or gained real demand) so a future breach
        # warns again rather than staying silently suppressed. Rebuilding the
        # sustain latch from this tick's survivors does the same for the
        # hysteresis (#633): a room that reached its target, or that picked up
        # schedule/presence/override demand and was skipped above, is released
        # and must breach the bare bound again to re-arm.
        self._safety_warned_room_ids &= breaching_now
        self._safety_sustain = sustaining_now

    async def _enforce_safety_setpoint(
        self, conn: aiosqlite.Connection, thermo_state: dict | None
    ) -> bool:
        """Hard safety backstop for the idle, no-demand gap (Issue #367).

        The per-room ``max_setpoint`` / ``min_setpoint`` hard cap lives in
        ``_suppression_vote``, which only runs on the active-room code path.
        When ``get_active_rooms`` returns nothing — an empty house with no
        schedule, presence, or override demand — ``_do_tick`` takes a
        ``not new_active_map`` branch and returns before any cap is evaluated,
        leaving the thermostat ``off`` while the space drifts past the
        configured envelope. This was observed after a vacation hold ended: the
        upstairs zone climbed to 81°F against a 77°F ceiling with no cycle ever
        starting (the thermostat had been reverted ``heat_cool`` → ``off`` and
        nothing re-engaged it).

        This is the dedicated guard for that gap. It is intentionally
        self-contained — it starts no engine cycle and mutates no cycle state —
        and drives the thermostat straight to the breached bound, the same
        approach as the proven single-setpoint vacation hold. Because it is
        reached only on the no-active-rooms branches it can never preempt a
        normal per-room cycle (those run for any room with real demand), and
        inside the configured envelope it is a complete no-op.

        Returns True when a bound was breached (the thermostat was driven to
        it, is already being held there, or the command was deferred by the
        compressor off-time lockout — #426), False otherwise.
        """
        if thermo_state is None or thermo_state.get("state") == "unavailable":
            return False

        tc = await db.get_thermostat_config(conn, self.thermostat_entity_id)
        # Routed through the plausibility guard (Issue #636) rather than
        # `_climate_temp_to_f` directly, so a reconnect glitch (e.g. the
        # classic 0°C/32°F null value) cannot masquerade as a real breach —
        # unlike `_apply_vacation_hold`, this backstop has no existing
        # "no ambient" announce path of its own, so a rejection is reported
        # here, once per episode. Uses the per-tick memoized wrapper, not
        # `_read_validated_ambient_f` directly, so this shares one answer
        # with `_get_avg_temp` and `_apply_vacation_hold` for the tick.
        current_temp_f = self._validated_ambient_for_tick(thermo_state)
        if current_temp_f is None:
            if self._ambient_reject_detail is not None and not self._ambient_reject_warned:
                self._ambient_reject_warned = True
                log.warning(
                    "Safety backstop for %s: ambient reading rejected — %s",
                    self.thermostat_entity_id,
                    self._ambient_reject_detail,
                )
                if self._logger:
                    await self._logger.log(
                        "warning",
                        "engine",
                        f"Safety backstop for {self.thermostat_entity_id}: ambient "
                        f"reading rejected — {self._ambient_reject_detail}. Treating "
                        "this tick as having no ambient reading rather than trusting "
                        "the value; no setpoint is being commanded off it.",
                        {
                            "thermostat": self.thermostat_entity_id,
                            "reason": "ambient_rejected",
                            "detail": self._ambient_reject_detail,
                        },
                    )
            # No usable ambient reading — fail safe by doing nothing rather than
            # commanding the HVAC off a value we cannot trust.
            return False

        if current_temp_f > tc.max_setpoint:
            await self._command_safety_bound(
                thermo_state, "cool", tc.max_setpoint, current_temp_f, tc
            )
            return True
        if current_temp_f < tc.min_setpoint:
            await self._command_safety_bound(
                thermo_state, "heat", tc.min_setpoint, current_temp_f, tc
            )
            return True
        return False

    async def _command_safety_bound(
        self,
        thermo_state: dict,
        ha_mode: str,
        setpoint: float,
        current_temp_f: float,
        tc: ThermostatConfig,
    ) -> None:
        """Drive the thermostat to a safety bound, idempotently (Issue #367).

        Re-asserting the identical setpoint every 60 s tick while the HVAC works
        the space back inside the envelope is needless write traffic that can
        hit cloud-thermostat rate limits (Issue #296), so the write is skipped
        when the thermostat is already in the target mode at the target
        setpoint. ``ha_mode`` is ``"cool"`` (holding ``max_setpoint``) or
        ``"heat"`` (holding ``min_setpoint``). The bound itself is the target —
        the thermostat is asked to bring the space exactly to the cap, not past
        it, so the backstop holds the envelope edge rather than overshooting.
        """
        attrs = thermo_state.get("attributes", {})
        current_mode = thermo_state.get("state")
        current_sp_f = _climate_temp_to_f(attrs.get("temperature"), self._ha.ha_temp_unit)
        bound_label = "maximum" if ha_mode == "cool" else "minimum"
        already_held = (
            current_mode == ha_mode
            and current_sp_f is not None
            and abs(current_sp_f - setpoint) <= _SETPOINT_DRIFT_TOLERANCE_F
        )
        if already_held:
            log.debug(
                "Safety backstop: %s already holding %s bound %.1f°F (ambient %.1f°F) — "
                "skipping re-command",
                self.thermostat_entity_id,
                bound_label,
                setpoint,
                current_temp_f,
            )
            return

        # Compressor off-time lockout (#208/#426): the backstop must not
        # restart the compressor inside the pressure-equalization window a
        # just-ended cooling cycle armed. Defer — the breach is re-evaluated
        # every tick and the command fires the moment the lockout elapses.
        # Heating stays exempt (furnace-side; the lockout protects the
        # compressor).
        if ha_mode == "cool" and self._in_offtime_lockout(tc):
            remaining = self._offtime_lockout_remaining(tc)
            log.warning(
                "Safety backstop for %s deferred — compressor off-time lockout, %.1f min remaining",
                self.thermostat_entity_id,
                remaining,
            )
            if self._logger:
                await self._logger.log(
                    "warning",
                    "engine",
                    f"Safety backstop for {self.thermostat_entity_id} deferred — ambient "
                    f"{current_temp_f:.1f}°F breached the {bound_label} setpoint "
                    f"{setpoint:.1f}°F, but the compressor off-time lockout has "
                    f"{remaining:.1f} min remaining. Cooling will be commanded when it "
                    "elapses.",
                    {
                        "thermostat": self.thermostat_entity_id,
                        "current_temp": current_temp_f,
                        "setpoint": setpoint,
                        "lockout_remaining_min": round(remaining, 1),
                    },
                )
            return

        try:
            await self._ha.set_thermostat_temperature(
                self.thermostat_entity_id, setpoint, hvac_mode=ha_mode
            )
        except Exception as exc:
            log.error(
                "Safety backstop: failed to command %s for %s: %s",
                ha_mode,
                self.thermostat_entity_id,
                exc,
            )
            return

        log.warning(
            "Safety backstop engaged for %s — ambient %.1f°F breached the %s setpoint "
            "%.1f°F with no active rooms; commanding %s to %.1f°F",
            self.thermostat_entity_id,
            current_temp_f,
            bound_label,
            setpoint,
            ha_mode,
            setpoint,
        )
        if self._logger:
            await self._logger.log(
                "warning",
                "engine",
                f"Safety backstop engaged for {self.thermostat_entity_id}: ambient "
                f"{current_temp_f:.1f}°F breached the {bound_label} setpoint "
                f"{setpoint:.1f}°F while no rooms had active demand. Commanded the "
                f"thermostat to {ha_mode} to {setpoint:.1f}°F to hold the configured "
                f"comfort envelope. Add a schedule, presence sensor, or override for this "
                f"zone if you expect it to be conditioned before a bound is breached.",
                {
                    "thermostat": self.thermostat_entity_id,
                    "current_temp": current_temp_f,
                    "bound": "max_setpoint" if ha_mode == "cool" else "min_setpoint",
                    "setpoint": setpoint,
                    "ha_mode": ha_mode,
                },
            )

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

    @property
    def _sensor_ids_for_room(self) -> dict[str, list[str]]:
        return self._sensor_map

    async def load_room_sensors(self, conn: aiosqlite.Connection, room_ids: list[str]) -> None:
        """Load and cache sensor entity IDs for a set of rooms."""
        for room_id in room_ids:
            sensors = await db.get_room_sensors(conn, room_id)
            self._sensor_map[room_id] = [s.entity_id for s in sensors]

    # ------------------------------------------------------------------
    # Short-cycle protection (Issue #208)
    # ------------------------------------------------------------------

    def _compressor_off_since(self) -> datetime | None:
        """When the compressor most recently stopped, by ANY mechanism (#628).

        The later of a cycle ending (``_last_cycle_ended_at``) and the vacation
        hold commanding a cooling thermostat away from ``cool``
        (``_hold_compressor_off_at``). Short-cycle protection is a fact about
        the equipment: a compressor the hold stopped 30 seconds ago needs its
        pressures to equalise exactly as much as one a cycle stopped, so both
        stamps have to arm the same guard. Never cleared — a trip ending does
        not un-stop the compressor.
        """
        stamps = [
            stamp
            for stamp in (self._last_cycle_ended_at, self._hold_compressor_off_at)
            if stamp is not None
        ]
        return max(stamps) if stamps else None

    def _note_hold_stopped_compressor(self, previous_hvac_mode: str) -> None:
        """Re-arm the off-time lockout when the hold ends a compressor run (#628).

        Called after the vacation hold successfully commands a thermostat away
        from a mode that could have been running the compressor — ``off`` when
        the band is regained or the ambient reading is lost, ``heat`` on a
        swing straight through the band.

        Two modes count. ``cool`` is the obvious one. ``heat_cool`` counts
        because the EQUIPMENT owns the direction there and we cannot tell which
        way it was running; a single-setpoint hold meets a ``heat_cool``
        thermostat when the user switches ``vacation_hvac_mode`` from range to
        single mid-trip. ``heat`` does not count — the lockout protects the
        compressor and heat is furnace-side, so arming on a heating stop would
        defer a later cooling start for equipment that never ran.

        These are hvac_mode values, not hvac_action, so this over-arms slightly
        when the equipment was merely idle in that mode. That is the protective
        direction and the same assumption the cycle path makes.
        """
        if previous_hvac_mode in ("cool", "heat_cool"):
            self._hold_compressor_off_at = datetime.now(UTC)

    def _in_offtime_lockout(self, tc: ThermostatConfig, now: datetime | None = None) -> bool:
        """Return True if the compressor off-time lockout is still active.

        After the compressor stops, the equipment must stay off for at least
        ``min_cycle_offtime_min`` before a new cycle may start. Restarting a
        compressor before its internal pressures have equalised is a primary
        cause of motor and contactor failure.
        """
        off_since = self._compressor_off_since()
        if tc.min_cycle_offtime_min <= 0 or off_since is None:
            return False
        if now is None:
            now = datetime.now(UTC)
        return now - off_since < timedelta(minutes=tc.min_cycle_offtime_min)

    def _offtime_lockout_remaining(
        self, tc: ThermostatConfig, now: datetime | None = None
    ) -> float:
        """Return minutes left on the off-time lockout (0.0 if not locked out)."""
        if not self._in_offtime_lockout(tc, now):
            return 0.0
        if now is None:
            now = datetime.now(UTC)
        off_since = self._compressor_off_since()
        assert off_since is not None
        elapsed_min = (now - off_since).total_seconds() / 60
        return tc.min_cycle_offtime_min - elapsed_min

    def _cycle_runtime_satisfied(self, tc: ThermostatConfig, now: datetime | None = None) -> bool:
        """Return True if the current cycle has run at least ``min_cycle_runtime_min``.

        A cycle that completes seconds after it started has short-cycled the
        compressor. Normal completion is deferred until this returns True. With
        no cycle log (nothing running) or the guard disabled, returns True.
        """
        if tc.min_cycle_runtime_min <= 0 or self._cycle_log is None:
            return True
        if now is None:
            now = datetime.now(UTC)
        return now - self._cycle_log.started_at >= timedelta(minutes=tc.min_cycle_runtime_min)

    def _all_active_rooms_satisfied(self, hvac_mode: str) -> bool:
        """Return True if every active room has reached target this cycle.

        A room whose vent already closed counts as satisfied. A room with no
        sensor reading cannot block — it is treated as satisfied, mirroring
        ``_monitor_rooms``. Used to detect when a cycle would normally
        terminate, so the minimum-runtime hold can engage instead.
        """
        for room_id, ar in self._active_rooms.items():
            rcs = self._room_cycle_states.get(room_id)
            if rcs is None:
                return False
            if rcs.vent_closed_at is not None:
                continue
            avg = self._get_avg_temp(ar.room)
            if avg is None:
                continue
            if not _is_at_target(avg + ar.room.temp_offset, rcs.target_temp, hvac_mode):
                return False
        return True

    def _rooms_drifted_past_deadband(self, hvac_mode: str, tc: ThermostatConfig) -> list[str]:
        """Names of active rooms that drifted past target by MORE than their
        deadband (Issue #423).

        Used while the min-runtime hold is engaged: a room outside its comfort
        band again has live demand, so the hold's "everyone is satisfied"
        premise no longer holds. The deadband is the hysteresis — drift within
        it never releases the hold, so the hold cannot flap at the target
        boundary. Rooms without a reading cannot vote (mirrors
        ``_all_active_rooms_satisfied``). Uses the same ``_drifted_past_deadband``
        predicate as the served-room reopen check in ``_monitor_rooms``.
        """
        drifted: list[str] = []
        for room_id, ar in self._active_rooms.items():
            rcs = self._room_cycle_states.get(room_id)
            if rcs is None:
                continue
            avg = self._get_avg_temp(ar.room)
            if avg is None:
                continue
            effective = avg + ar.room.temp_offset
            band = _effective_deadband(ar.room, tc.deadband, ar.deadband_override)
            if _drifted_past_deadband(effective, rcs.target_temp, hvac_mode, band):
                drifted.append(ar.room.name)
        return drifted

    async def _release_min_runtime_hold(self, conn: aiosqlite.Connection, reason: str) -> None:
        """Take the cycle out of the minimum-runtime hold and resume normal
        monitoring (Issue #423).

        Called when the hold's premise ("every room is satisfied; the cycle is
        only burning minimum runtime") stops being true — a room joined the
        cycle mid-hold, an in-place trigger update raised a room's demand, or
        a held room drifted back past its deadband. Without this, the hold
        exit terminated the cycle against live demand and the off-time
        lockout (#208) then blocked the restart the room obviously needed.

        Overflow conditioning is defined as running *during the hold*
        (``overflow_during_min_runtime``), so the overflow rooms' vents are
        closed here — the resumed cycle should direct air at the rooms with
        live demand. If the cycle re-satisfies before its minimum runtime,
        ``_enter_min_runtime_hold`` simply re-engages (it is idempotent) and
        overflow resumes.
        """
        if self._cycle_log is None or not self._cycle_log.in_min_runtime_hold:
            return
        self._cycle_log.in_min_runtime_hold = False
        try:
            await db.set_cycle_log_min_runtime_hold(conn, self._cycle_log.id, False)
        except Exception as exc:
            log.warning("Failed to persist in_min_runtime_hold clear: %s", exc)
        await self._close_overflow_rooms(
            conn, set(self._overflow_room_ids), "min-runtime hold released"
        )
        self._overflow_room_ids = set()
        log.info(
            "Min-runtime hold released for %s — %s; cycle resumes normal monitoring",
            self.thermostat_entity_id,
            reason,
        )
        if self._logger:
            await self._logger.log(
                "info",
                "engine",
                f"Minimum-runtime hold released for {self.thermostat_entity_id} — {reason}. "
                "The cycle keeps running until every room is back at target.",
                {
                    "thermostat": self.thermostat_entity_id,
                    "cycle_id": self._cycle_log.id,
                    "reason": reason,
                },
            )

    async def _close_overflow_rooms(
        self, conn: aiosqlite.Connection, room_ids: set[str], reason: str
    ) -> None:
        """Close the vents of *room_ids* opened as overflow destinations and
        record the per-room disposition (Issues #237/#254).
        """
        for room_id in room_ids:
            vents = await db.get_room_vents(conn, room_id)
            if not vents:
                continue
            await self._vent.force_close_vents(vents)
            if self._cycle_log:
                ts = datetime.now(UTC)
                for v in vents:
                    try:
                        await db.insert_cycle_vent_event(
                            conn,
                            self._cycle_log.id,
                            ts,
                            v.entity_id,
                            room_id,
                            "closed_overflow_hold",
                            reason,
                        )
                    except Exception as exc:
                        log.debug("Failed to record closed_overflow_hold event: %s", exc)
                # Capture this room's end temperature at the moment its overflow
                # vent closes (Issue #254). "Final close wins": a later re-open
                # clears temp_at_end, so the value persisted here reflects the
                # most recent disposition.
                await self._record_overflow_close(conn, room_id, ts)

    async def _enter_min_runtime_hold(self, conn: aiosqlite.Connection) -> None:
        """Hold a satisfied-but-too-young cycle open without dead-heading.

        Every room in the cycle has reached target but the cycle has not yet
        run ``min_cycle_runtime_min``. Re-open the vents of any cycle rooms
        that closed earlier so the air handler keeps a full duct path and the
        leftover heat/cool is spread across every room that was part of the
        cycle — rather than dumped into whichever room finished last, which
        would dead-head the system through a single vent.

        Persists ``CycleLog.in_min_runtime_hold = True`` (Issue #237) so the
        per-room close-vent loop on later ticks skips the rooms whose vents
        the hold just opened — without the flag the close loop re-closes them
        on the next tick, producing open/close churn through the hold window.

        Idle and opposite-direction rooms are deliberately left closed by this
        method — the actual overflow-conditioning decision (which idle rooms
        can absorb the surplus air without crossing into an opposite cycle)
        runs through ``_apply_overflow_during_hold`` on every tick of the hold.
        """
        # Mark the cycle as held even if no vents need re-opening (the flag
        # still gates close-vent behaviour on later ticks). Idempotent — the
        # cycle stays in hold until termination.
        if self._cycle_log is not None and not self._cycle_log.in_min_runtime_hold:
            self._cycle_log.in_min_runtime_hold = True
            try:
                await db.set_cycle_log_min_runtime_hold(conn, self._cycle_log.id, True)
            except Exception as exc:
                log.warning("Failed to persist in_min_runtime_hold flag: %s", exc)

        reopened: list[str] = []
        for room_id in self._active_rooms:
            rcs = self._room_cycle_states.get(room_id)
            if rcs is None or rcs.vent_closed_at is None:
                continue
            vents = self._room_vents.get(room_id, [])
            await self._vent.open_room_vents(vents)
            rcs.vent_closed_at = None
            await db.upsert_room_cycle_state(conn, rcs)
            reopened.append(room_id)
            if self._cycle_log:
                ts = datetime.now(UTC)
                for v in vents:
                    try:
                        await db.insert_cycle_vent_event(
                            conn,
                            self._cycle_log.id,
                            ts,
                            v.entity_id,
                            room_id,
                            "reopened_min_runtime_hold",
                            "cycle satisfied — held open for minimum runtime",
                        )
                    except Exception as exc:
                        log.debug("Failed to record reopened_min_runtime_hold event: %s", exc)

        if reopened:
            room_names = [self._active_rooms[r].room.name for r in reopened]
            log.info(
                "Cycle for %s reached target before its minimum runtime — "
                "re-opened %d room(s) %s to hold the cycle open with full airflow",
                self.thermostat_entity_id,
                len(reopened),
                room_names,
            )
            if self._logger:
                await self._logger.log(
                    "info",
                    "engine",
                    f"Cycle for {self.thermostat_entity_id} reached target before its "
                    f"minimum runtime — re-opened {room_names} so the HVAC keeps running "
                    f"with full airflow until the minimum runtime is met",
                    {
                        "thermostat": self.thermostat_entity_id,
                        "reopened_rooms": room_names,
                    },
                )

    async def _apply_overflow_during_hold(
        self,
        conn: aiosqlite.Connection,
        hvac_mode: str,
        tc: ThermostatConfig,
    ) -> None:
        """Open non-active rooms' vents during the minimum-runtime hold to
        absorb surplus conditioned air without overshooting the active rooms
        (Issue #237).

        Runs on every tick of the hold so a candidate that drifts past its
        goal gets its vent closed and a different room takes its place. The
        candidate selection (tiered) is in ``room_manager.get_overflow_candidates``.

        No-op when the feature is disabled, vacation mode is active, or the
        candidate algorithm finds no suitable destination — in which case the
        previously-active cycle rooms continue to absorb the surplus on their
        own, which is the pre-#237 behaviour.
        """
        if not tc.overflow_during_min_runtime:
            return
        in_vacation = bool(self._get_vacation_mode and self._get_vacation_mode())
        if in_vacation:
            # Vacation hold is a separate state machine; do not change vents.
            return

        # "Active cycle target" = the most aggressive target across active
        # rooms (lowest for cooling, highest for heating). That's the
        # temperature the thermostat is currently driving toward and the
        # closest proxy we have for the direction the supply air will push a
        # candidate room.
        active_targets = [
            rcs.target_temp
            for rid, rcs in self._room_cycle_states.items()
            if rid in self._active_rooms
        ]
        if not active_targets:
            return
        active_cycle_target = min(active_targets) if hvac_mode == "cooling" else max(active_targets)

        try:
            from .room_manager import get_overflow_candidates
        except Exception as exc:
            log.debug("Failed to import get_overflow_candidates: %s", exc)
            return

        candidates = await get_overflow_candidates(
            conn,
            self.thermostat_entity_id,
            hvac_mode=hvac_mode,
            active_room_ids=set(self._active_rooms.keys()),
            active_cycle_target_f=active_cycle_target,
            deadband_f=tc.deadband,
            in_vacation=in_vacation,
            get_avg_temp=self._get_avg_temp,
        )
        desired_ids = {c.room.id for c in candidates}

        # Close any previously-opened overflow vents that are no longer
        # candidates (they crossed their setpoint, or another room won the
        # tier on this tick).
        to_close = self._overflow_room_ids - desired_ids
        await self._close_overflow_rooms(conn, to_close, "overflow room no longer a candidate")

        # Open vents for newly-selected overflow candidates. Judged against
        # PHYSICAL vent state, not just the remembered set (#422): if another
        # code path closed an overflow vent mid-hold, trusting the bookkeeping
        # left it closed for the rest of the hold with no repair.
        to_open: set[str] = set()
        for c in candidates:
            vents = await db.get_room_vents(conn, c.room.id)
            if not vents:
                continue
            if c.room.id in self._overflow_room_ids and all(self._vent._is_open(v) for v in vents):
                continue
            to_open.add(c.room.id)
            await self._vent.open_room_vents(vents)
            if self._cycle_log:
                ts = datetime.now(UTC)
                tier_label = f"tier{c.tier}"
                reason_extra = f", headroom={c.headroom:.1f}°F" if c.headroom is not None else ""
                for v in vents:
                    try:
                        await db.insert_cycle_vent_event(
                            conn,
                            self._cycle_log.id,
                            ts,
                            v.entity_id,
                            c.room.id,
                            "opened_overflow_hold",
                            f"{tier_label}: cur={c.current_temp:.1f}°F{reason_extra}",
                        )
                    except Exception as exc:
                        log.debug("Failed to record opened_overflow_hold event: %s", exc)
                # Record / refresh this room's overflow cycle data point so the
                # Logs page can show its start and end temps (Issue #254).
                await self._record_overflow_open(conn, c, ts)

        self._overflow_room_ids = desired_ids

        if to_open or to_close:
            tier_used = candidates[0].tier if candidates else None
            log.info(
                "Overflow hold for %s: tier=%s, open=%s, close=%s",
                self.thermostat_entity_id,
                tier_used,
                sorted(to_open),
                sorted(to_close),
            )
            if self._logger and candidates:
                opened_names = [c.room.name for c in candidates if c.room.id in to_open]
                if opened_names:
                    await self._logger.log(
                        "info",
                        "engine",
                        f"Overflow conditioning (tier {tier_used}) opened "
                        f"{opened_names} during minimum-runtime hold to absorb surplus "
                        f"{'cooling' if hvac_mode == 'cooling' else 'heating'}",
                        {
                            "thermostat": self.thermostat_entity_id,
                            "tier": tier_used,
                            "rooms": opened_names,
                            "active_cycle_target": active_cycle_target,
                        },
                    )

    async def _record_overflow_open(
        self, conn: aiosqlite.Connection, c: OverflowCandidate, ts: datetime
    ) -> None:
        """Persist an overflow room's cycle data point on its first open, or
        re-arm it on a subsequent re-open (Issue #254).

        On first open we create a ``room_cycle_states`` row tagged
        ``role='overflow'`` capturing ``temp_at_start`` (the room's temp when it
        was chosen). On a re-open we keep the original ``temp_at_start`` but
        clear ``temp_at_end`` / ``vent_closed_at`` so the room counts as open
        again — its end temp is re-captured on the next close or at cycle end.
        """
        if self._cycle_log is None:
            return
        existing = self._overflow_room_states.get(c.room.id)
        if existing is None:
            trigger = json.dumps(
                {
                    "overflow": True,
                    "tier": c.tier,
                    "headroom": c.headroom,
                    "effective_setpoint": c.effective_setpoint,
                }
            )
            rcs = RoomCycleState(
                cycle_id=self._cycle_log.id,
                room_id=c.room.id,
                # Overflow rooms have no cycle target of their own; record the
                # room's effective setpoint so the UI has a reference point.
                target_temp=c.effective_setpoint
                if c.effective_setpoint is not None
                else c.current_temp,
                temp_at_start=c.current_temp,
                trigger_detail=trigger,
                joined_at=ts,
                role="overflow",
            )
            self._overflow_room_states[c.room.id] = rcs
        else:
            # Re-opened: keep temp_at_start, drop any prior end so the room is
            # treated as open again.
            rcs = existing
            rcs.temp_at_end = None
            rcs.vent_closed_at = None
        try:
            await db.upsert_room_cycle_state(conn, rcs)
        except Exception as exc:
            log.debug("Failed to persist overflow room open state: %s", exc)

    async def _record_overflow_close(
        self, conn: aiosqlite.Connection, room_id: str, ts: datetime
    ) -> None:
        """Capture an overflow room's end temperature when its vent closes
        (Issue #254). No-op for rooms we never recorded as overflow."""
        if self._cycle_log is None:
            return
        rcs = self._overflow_room_states.get(room_id)
        if rcs is None:
            return
        room = await db.get_room(conn, room_id)
        rcs.temp_at_end = self._get_avg_temp(room) if room else None
        rcs.vent_closed_at = ts
        try:
            await db.upsert_room_cycle_state(conn, rcs)
        except Exception as exc:
            log.debug("Failed to persist overflow room close state: %s", exc)

    async def _finalize_overflow_rooms(self, conn: aiosqlite.Connection) -> None:
        """At cycle termination, fill ``temp_at_end`` for any overflow rooms
        still open (never swapped out) so their data point closes at cycle end
        (Issue #254). Then clear the in-memory overflow tracking."""
        if self._cycle_log is not None:
            ts = datetime.now(UTC)
            for room_id, rcs in self._overflow_room_states.items():
                if rcs.temp_at_end is not None:
                    continue
                room = await db.get_room(conn, room_id)
                rcs.temp_at_end = self._get_avg_temp(room) if room else None
                rcs.vent_closed_at = ts
                try:
                    await db.upsert_room_cycle_state(conn, rcs)
                except Exception as exc:
                    log.debug("Failed to finalize overflow room state: %s", exc)
        self._overflow_room_states = {}


def _climate_temp_to_f(value: Any, unit: str) -> float | None:
    """Normalise a climate-entity temperature reading to °F.

    Climate entities (``current_temperature``, ``temperature``) report in HA's
    configured system unit — ``HAClient.ha_temp_unit`` — unlike sensor entities,
    which the HA client already normalises via their per-entity
    ``unit_of_measurement``. The engine reasons entirely in °F, so every climate
    read must pass through here. Returns ``None`` for missing/unparseable
    values. For an °F install (``unit != "C"``) this is identity beyond a 2dp
    round, so imperial behaviour is unchanged. (Issue #280)
    """
    if value is None:
        return None
    try:
        return to_f(float(value), unit)
    except (ValueError, TypeError):
        return None


def _trigger_differs(new: ActiveRoom, old: ActiveRoom) -> bool:
    """Whether a room's trigger changed in a way the cycle must act on.

    ONE definition, deliberately, because two callers need it and they must
    never disagree:

    * ``_evaluate``'s ``trigger_changed`` decides whether
      ``_start_or_update_cycle`` runs at all.
    * ``_start_or_update_cycle``'s ``changed`` set decides whether the room's
      ``RoomCycleState`` is re-recorded.

    They were the same expression until #517 added the deadband to the first
    and not the second. A band-only change then updated ``self._active_rooms``
    (so the monitor paths saw the new band) while leaving ``RoomCycleState``
    untouched — and since ``_monitor_rooms`` judges rooms against
    ``rcs.target_temp``, an Eco re-relaxation in that same tick would move the
    written setpoint while the recorded target stayed behind.

    Compares REQUESTED (pre-Eco) targets on both sides, per #408: comparing
    Eco-effective targets makes every outdoor-reading drift look like a trigger
    change and re-runs the update every tick. The deadband values are raw
    schedule data and never Eco-adjusted, so they compare like with like.

    ``respect_eco`` is compared for the same reason the deadband is (#517):
    re-posting a hold with only the Eco opt-in flipped must re-run the in-place
    update so the running cycle's relaxation state follows the flag (#576).
    Both sides are raw hold data, never Eco-adjusted — restore_from_db re-reads
    the flag from the live row so a restart cannot fabricate a difference.
    """
    return (
        new.source != old.source
        or _requested_of(new) != _requested_of(old)
        or new.deadband_override != old.deadband_override
        or new.respect_eco != old.respect_eco
    )


def _requested_of(ar: ActiveRoom) -> float:
    """The room's REQUESTED (pre-Eco) target (#408).

    ``requested_target`` is populated once ``_apply_eco`` has run; a freshly
    built ActiveRoom (or one restored from an old snapshot) carries only
    ``target_temp``, which at that point IS the requested value.
    """
    return ar.requested_target if ar.requested_target is not None else ar.target_temp


def _is_at_target(avg_temp: float, target_temp: float, hvac_mode: str) -> bool:
    if hvac_mode == "cooling":
        return avg_temp <= target_temp
    if hvac_mode == "heating":
        return avg_temp >= target_temp
    # Unexpected mode — return False (not at target) so vents stay open
    # rather than closing prematurely.  (Issue #48 Bug 6)
    log.warning("_is_at_target called with unexpected mode %r — returning False", hvac_mode)
    return False


def _drifted_past_deadband(
    effective_avg: float, target_temp: float, hvac_mode: str, band: float
) -> bool:
    """True if a served room has drifted MORE than *band* past its target.

    This is renewed live demand — not the fraction-of-a-degree re-crossing of
    the target that #86 deliberately treats as "served" (which would otherwise
    block termination forever). The deadband is the hysteresis, so re-engaging
    on this predicate cannot flap at the target boundary. Shared by the
    min-runtime-hold release check (#423) and the served-room reopen check in
    ``_monitor_rooms`` so the two can never diverge. Unexpected modes never
    count as drifted.
    """
    if hvac_mode == "cooling":
        return effective_avg > target_temp + band
    if hvac_mode == "heating":
        return effective_avg < target_temp - band
    return False
