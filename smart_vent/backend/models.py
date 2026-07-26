"""
Dataclasses for all domain entities.

All temperatures are stored internally in °F. Conversion from HA (which may
report °C) happens at ingestion in HAClient.get_numeric_state(). The active
display unit ('F' or 'C') is persisted in the system_settings table under the
key 'temperature_unit' and exposed via GET /api/settings. See Issue #123.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from typing import Literal

ControlMethod = Literal["open_close", "set_position", "set_tilt_position", "toggle"]
VALID_CONTROL_METHODS: tuple[ControlMethod, ...] = (
    "open_close",
    "set_position",
    "set_tilt_position",
    "toggle",
)


def new_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Configuration entities
# ---------------------------------------------------------------------------


@dataclass
class Room:
    id: str
    name: str
    thermostat_entity_id: str
    include_thermostat_sensor: bool = False
    system_wide_temp: float | None = None
    presence_holdover_hours: float = 2.0
    notes: str = ""
    temp_offset: float = 0.0  # °F added to measured avg before comparing to target
    # Per-room deadband override (Issue #277). When set, this replaces the
    # thermostat's ``deadband`` for THIS room's start-cycle / join-cycle vote
    # only — the ±°F tolerance band around target within which the room is
    # considered "at target" and calls for no HVAC. ``None`` (the default)
    # means inherit the thermostat's deadband, so existing rooms are
    # unaffected. This is distinct from ``ambient_suppression_deadband`` below,
    # which is the *widened* coasting band used only by the pre-cool/pre-heat
    # feature.
    deadband_override: float | None = None
    # Ambient-aware presence suppression / pre-cool / pre-heat (Issue #248).
    # Per-room opt-in; only active when an outside temperature sensor is
    # configured. When the outside temp is at least
    # ``ambient_suppression_min_differential`` °F past the presence target on the
    # helpful side, the room is allowed to drift to target on its own instead of
    # calling for HVAC, riding a widened deadband on the coasting side only.
    ambient_suppression_enabled: bool = False
    # "any_presence" | "off_schedule_only"
    ambient_suppression_mode: str = "any_presence"
    # °F the outside temp must be past the target before coasting (>= 0).
    ambient_suppression_min_differential: float = 5.0
    # °F widened deadband applied while coasting (>= thermostat deadband).
    ambient_suppression_deadband: float = 2.0
    # Minutes after a schedule block ends that off_schedule_only mode applies.
    ambient_suppression_off_schedule_window_min: int = 60
    # Eco Mode per-room overrides (Issue #404). Every field is nullable with
    # **field-level null-inheritance**: ``None`` (the default) inherits the
    # thermostat's value for that field; a non-``None`` value overrides just
    # that one field, so a room can, e.g., relax more aggressively than its
    # thermostat while inheriting every other Eco setting. ``eco_mode_enabled``
    # is a tri-state override: ``None`` inherits the thermostat toggle, ``True``
    # opts this room in even if the thermostat has Eco off, ``False`` opts it
    # out even if the thermostat has Eco on. See ``eco.py`` / docs/eco-mode.md.
    eco_mode_enabled: bool | None = None
    eco_cooling_outdoor_threshold: float | None = None  # °F absolute (outdoor)
    eco_cooling_full_drift_temp: float | None = None  # °F absolute (outdoor)
    eco_cooling_max_drift: float | None = None  # °F delta
    eco_heating_outdoor_threshold: float | None = None  # °F absolute (outdoor)
    eco_heating_full_drift_temp: float | None = None  # °F absolute (outdoor)
    eco_heating_max_drift: float | None = None  # °F delta
    eco_hysteresis_band: float | None = None  # °F delta

    @classmethod
    def create(cls, name: str, thermostat_entity_id: str, **kwargs) -> Room:
        return cls(id=new_id(), name=name, thermostat_entity_id=thermostat_entity_id, **kwargs)


@dataclass
class RoomSensor:
    id: str
    room_id: str
    entity_id: str  # sensor.*

    @classmethod
    def create(cls, room_id: str, entity_id: str) -> RoomSensor:
        return cls(id=new_id(), room_id=room_id, entity_id=entity_id)


@dataclass
class RoomVent:
    id: str
    room_id: str
    entity_id: str  # cover.*
    control_method: ControlMethod = "open_close"

    @classmethod
    def create(
        cls,
        room_id: str,
        entity_id: str,
        control_method: ControlMethod = "open_close",
    ) -> RoomVent:
        return cls(
            id=new_id(),
            room_id=room_id,
            entity_id=entity_id,
            control_method=control_method,
        )


@dataclass
class RoomPresenceSensor:
    id: str
    room_id: str
    entity_id: str  # binary_sensor.*

    @classmethod
    def create(cls, room_id: str, entity_id: str) -> RoomPresenceSensor:
        return cls(id=new_id(), room_id=room_id, entity_id=entity_id)


@dataclass
class Schedule:
    id: str
    room_id: str
    days_of_week: list[int]  # 0=Monday … 6=Sunday
    start_time: time  # local time
    end_time: time
    target_temp: float  # °F
    # Lifecycle (Issue #359). `enabled=False` parks a block so the engine
    # ignores it without deleting it. `expires_at` is a *local wall-clock*
    # datetime (None = never expire); a background sweep flips `enabled` to
    # False once it passes. Schedules are only ever deleted by a human.
    enabled: bool = True
    expires_at: datetime | None = None
    # Per-schedule deadband override (Issue #517). ``None`` (the default)
    # inherits the room's ``deadband_override``, falling back to the
    # thermostat's ``deadband`` — so existing blocks are unaffected. A value
    # replaces both, but ONLY while this block is the active source for the
    # room (``ActiveRoom.source == "schedule"``); an override, presence
    # holdover, or safety activation falls back to the room/thermostat chain.
    # Widening the band lets a room drift further before it calls for HVAC,
    # which is the point: a night block on a room nobody is using can coast
    # instead of running the compressor. Resolved by
    # ``room_manager._effective_deadband``.
    deadband_override: float | None = None
    # Optional display name (Issue #520). ``None`` — the default, and what every
    # pre-#520 block reads back as — means "unnamed": callers that need a label
    # fall back to ``id``, so nothing changes for anyone who never sets one.
    # Purely a label: the engine never reads it, and it is not an identifier
    # (nothing enforces uniqueness — ``id`` remains the only way to address a
    # block). It exists because a GUID makes a poor human-facing name in HA's
    # MQTT discovery (#519), and because the Schedules page had no way to say
    # what a block is *for*.
    name: str | None = None

    @classmethod
    def create(
        cls,
        room_id: str,
        days_of_week: list[int],
        start_time: time,
        end_time: time,
        target_temp: float,
        enabled: bool = True,
        expires_at: datetime | None = None,
        deadband_override: float | None = None,
        name: str | None = None,
    ) -> Schedule:
        return cls(
            id=new_id(),
            room_id=room_id,
            days_of_week=days_of_week,
            start_time=start_time,
            end_time=end_time,
            target_temp=target_temp,
            enabled=enabled,
            expires_at=expires_at,
            deadband_override=deadband_override,
            name=name,
        )

    @property
    def display_name(self) -> str:
        """What to call this block in a UI or an HA entity name (Issue #520).

        The name when it has one, else the ``id`` — the fallback #519's MQTT
        discovery needs, kept here so every caller falls back identically.
        """
        return self.name or self.id


@dataclass
class ThermostatConfig:
    thermostat_entity_id: str  # PK — HA climate entity
    name: str = ""  # friendly display name, e.g. "Upstairs HVAC"
    default_temp: float | None = None  # thermostat-level fallback for presence activation
    min_setpoint: float = 60.0
    max_setpoint: float = 85.0
    deadband: float = 0.5  # ±°F
    max_vent_closed_min: int = (
        0  # minutes before a closed vent is force-reopened; 0 = no limit (feature off by default)
    )
    # Airflow-floor / dead-head protection (Issue #213). Replaces the prior
    # count-based ``min_open_vents`` setting with a fraction-of-total-vents
    # calculation that knows about passive (non-smart) registers and an
    # optional bypass damper. Computed by ``_required_open_vents`` in the
    # engine — see docs/safety.md.
    # total_vents_count: total registers on this thermostat (smart + passive).
    #   None until the user fills it in; while None the engine falls back to
    #   the prior "≥1 vent open" default so existing thermostats keep working
    #   through the upgrade window. The Thermostats-page banner nudges users
    #   to set it.
    total_vents_count: int | None = None
    # has_bypass_damper: when True, the airflow floor is not enforced — the
    #   bypass damper relieves duct static pressure mechanically.
    has_bypass_damper: bool = False
    # min_open_vents_fraction: share of total_vents_count that must stay open.
    #   Default ≈ 1/3.
    min_open_vents_fraction: float = 0.333
    overshoot_delta: float = 2.0
    cycle_timeout_hours: float = 3.0
    # How often (minutes) the engine re-checks actual vent/thermostat state against its
    # intended state and corrects external changes (e.g. Flair app, manual HA overrides).
    # 0 = disabled. Should not exceed cycle_timeout_hours * 60.
    reconciliation_interval_min: int = 0
    # How to hold the thermostat during vacation mode:
    # "range"   → set heat_cool/auto mode with low=min_setpoint, high=max_setpoint
    # "single"  → turn off; re-engage heat/cool when a bound is breached
    vacation_hvac_mode: str = "single"
    # Short-cycle protection (Issue #208). Rapid stop/start of a compressor is a
    # primary equipment-failure mode.
    # min_cycle_runtime_min: once a cycle starts, defer its normal completion
    #   until it has run at least this long — prevents stopping the compressor
    #   moments after it started.
    # min_cycle_offtime_min: after a cycle ends, refuse to start a new one until
    #   this much time has elapsed — the classic compressor anti-short-cycle
    #   off-timer.
    # 0 disables either guard.
    min_cycle_runtime_min: int = 0
    min_cycle_offtime_min: int = 0
    # Outdoor-temperature cooling lockout (Issue #209). When set, the engine
    # refuses to start a cooling cycle while the configured outdoor-temperature
    # sensor reads below this value (°F). Running a standard AC compressor in
    # cold outdoor conditions risks liquid slugging and evaporator icing.
    # None = disabled. NOTE: heat pumps are not supported, so there is no
    # corresponding heating lockout.
    cooling_lockout_below_f: float | None = None
    # Overflow conditioning during minimum-runtime hold (Issue #237). When True
    # and a cycle is held open past its goal to satisfy min_cycle_runtime_min,
    # also open vents in non-active rooms that can absorb the surplus air
    # without crossing into their opposite-direction trigger. Disabled in
    # vacation mode regardless of this setting. See docs/overflow-conditioning.md.
    overflow_during_min_runtime: bool = True
    # Thermostat-unavailability abort (Issue #267). While the climate entity is
    # unavailable the engine skips its tick, which suspends every per-tick
    # safety monitor (cycle timeout, max_vent_closed_min watchdog,
    # reconciliation) — meanwhile the physical HVAC may keep running at the
    # last commanded setpoint with vents closed. Once the entity has been
    # unavailable this many minutes, a running cycle is aborted and all zone
    # vents re-opened (cover entities are independent of the climate entity).
    # Transient outages shorter than this are tolerated and the cycle resumes
    # untouched. 0 = never abort (not recommended).
    unavailable_abort_after_min: int = 5
    # Eco Mode — outdoor-temperature-compensated setpoint drift (Issue #404).
    # Defaults OFF; when off the engine follows the exact pre-Eco code path.
    # These are the global per-thermostat values; rooms inherit them field by
    # field (see Room.eco_* above). Defaults are the round-in-Fahrenheit set
    # from ``eco.ECO_DEFAULTS_F``; a °C-mode install seeds the round-in-Celsius
    # equivalents (see ``db._migrate_eco_defaults``). All values are °F.
    eco_mode_enabled: bool = False
    eco_cooling_outdoor_threshold: float = 86.0  # °F absolute (outdoor)
    eco_cooling_full_drift_temp: float = 100.0  # °F absolute (outdoor)
    eco_cooling_max_drift: float = 4.0  # °F delta
    eco_heating_outdoor_threshold: float = 40.0  # °F absolute (outdoor)
    eco_heating_full_drift_temp: float = 0.0  # °F absolute (outdoor)
    eco_heating_max_drift: float = 4.0  # °F delta
    eco_hysteresis_band: float = 2.0  # °F delta
    # Eco Suspend (Issue #500) — READ-ONLY in API responses. Not a
    # thermostat_configs column: the state lives in the eco_suspensions table
    # (its own expiry-bearing row) so the config upsert can never clobber it
    # with a stale form snapshot. The API layer populates this from the
    # scheduler when serializing. ISO-8601 UTC string; None = not suspended.
    # Ignored on POST/PUT — use the dedicated /eco-suspend endpoints.
    eco_suspend_until: str | None = None


@dataclass
class RoomOverride:
    room_id: str  # PK
    target_temp: float
    expires_at: datetime


# ---------------------------------------------------------------------------
# Runtime / persisted state
# ---------------------------------------------------------------------------


@dataclass
class PresenceHoldoverState:
    room_id: str  # PK
    last_detected_at: datetime
    expires_at: datetime


# ---------------------------------------------------------------------------
# Cycle tracking
# ---------------------------------------------------------------------------


@dataclass
class CycleLog:
    id: str
    thermostat_entity_id: str
    started_at: datetime
    mode: str  # 'heating' | 'cooling'
    rooms_json: str  # JSON snapshot
    ended_at: datetime | None = None
    ended_reason: str | None = None  # 'completed' | 'timeout' | 'system_disabled' | ...
    thermostat_temp_at_start: float | None = None
    thermostat_temp_at_end: float | None = None
    setpoint_at_start: float | None = None
    setpoint_at_end: float | None = None
    vents_at_start: str | None = None  # JSON {entity_id: 'open'|'closed'|'unknown'}
    vents_at_end: str | None = None
    outside_temp_at_start: float | None = None  # °F, NULL if entity unset/unreadable
    outside_temp_at_end: float | None = None
    # Minimum-runtime hold state (Issue #237). Set True once a cycle has hit
    # its goal but is being held open to satisfy min_cycle_runtime_min. While
    # True the per-room close-vent loop is short-circuited so vents the hold
    # opened do not flap back closed on the next tick. Cleared implicitly when
    # the cycle ends.
    in_min_runtime_hold: bool = False

    @classmethod
    def create(cls, thermostat_entity_id: str, mode: str, rooms_json: str) -> CycleLog:
        return cls(
            id=new_id(),
            thermostat_entity_id=thermostat_entity_id,
            started_at=datetime.now(UTC),
            mode=mode,
            rooms_json=rooms_json,
        )


@dataclass
class RoomCycleState:
    cycle_id: str
    room_id: str
    target_temp: float
    reached_at: datetime | None = None
    vent_closed_at: datetime | None = None
    temp_at_start: float | None = None
    temp_at_end: float | None = None
    trigger_detail: str | None = None  # JSON: schedule/override/presence metadata
    joined_at: datetime | None = None  # NULL = present at cycle start
    # Role discriminator (Issue #254). 'active' = a room the cycle was
    # targeting; 'overflow' = a non-active room opened during the
    # minimum-runtime hold to absorb surplus conditioned air (Issue #237).
    role: str = "active"
    # Eco Mode measurability (Issue #404). ``requested_target`` is the room's
    # pre-relaxation target as resolved by the schedule/override/presence logic;
    # ``effective_target`` is what Eco Mode relaxed it to (and what the cycle
    # actually ran to — it equals ``target_temp``). ``eco_active`` is True only
    # when Eco Mode actually moved the target this cycle. With Eco off,
    # ``requested_target == effective_target == target_temp`` and ``eco_active``
    # is False. All °F.
    requested_target: float | None = None
    effective_target: float | None = None
    eco_active: bool = False


@dataclass
class CycleTempSample:
    id: int
    cycle_id: str
    room_id: str | None  # NULL → thermostat-level sample
    timestamp: datetime
    room_temp: float | None
    thermostat_temp: float | None
    setpoint: float | None


@dataclass
class CycleSetpointHistory:
    id: int
    cycle_id: str
    timestamp: datetime
    setpoint: float
    reason: str | None = None


@dataclass
class CycleVentEvent:
    id: int
    cycle_id: str
    timestamp: datetime
    entity_id: str
    room_id: str | None
    action: (
        str  # opened_at_start | closed_reached_target | force_reopened_max_closed |
        # reopened_min_runtime_hold | closed_overflow_hold | opened_overflow_hold
    )
    reason: str | None = None


# ---------------------------------------------------------------------------
# Runtime-only (not persisted to DB)
# ---------------------------------------------------------------------------


@dataclass
class RoomLiveState:
    """In-memory snapshot of a room's current sensor readings."""

    room_id: str
    avg_temp: float | None  # None if all sensors unavailable
    sensor_count: int
    available_sensor_count: int
    vent_states: dict[str, str]  # entity_id → 'open'|'closed'|'unknown'
    presence_active: bool
    holdover_expires_at: datetime | None
    # Target temperature this active room is running to in the current cycle
    # (°F) — the Eco-relaxed effective target when Eco is active, otherwise the
    # plain requested target. None when the room is not an active cycle member.
    target_temp: float | None = None
    # Eco Mode (Issue #404): the pre-relaxation ask and whether Eco is currently
    # relaxing this room, so the Dashboard can show "requested X → effective Y".
    # requested_target == target_temp and eco_active is False when Eco is off.
    requested_target: float | None = None
    eco_active: bool = False


@dataclass
class ZoneStatus:
    """Summarised status for one thermostat zone (all rooms sharing that thermostat)."""

    thermostat_entity_id: str
    hvac_mode: str  # 'heating'|'cooling'|'off'|'unknown'
    hvac_action: str
    current_temp: float | None
    setpoint: float | None
    cycle_id: str | None
    cycle_started_at: datetime | None
    rooms: list[RoomLiveState] = field(default_factory=list)
