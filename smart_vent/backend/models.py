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

    @classmethod
    def create(
        cls,
        room_id: str,
        days_of_week: list[int],
        start_time: time,
        end_time: time,
        target_temp: float,
    ) -> Schedule:
        return cls(
            id=new_id(),
            room_id=room_id,
            days_of_week=days_of_week,
            start_time=start_time,
            end_time=end_time,
            target_temp=target_temp,
        )


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
        str  # opened_at_start | closed_reached_target | force_reopened_max_closed | closed_at_end
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
