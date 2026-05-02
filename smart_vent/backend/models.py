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
    min_open_vents: int = 1
    overshoot_delta: float = 2.0
    cycle_timeout_hours: float = 3.0
    # How often (minutes) the engine re-checks actual vent/thermostat state against its
    # intended state and corrects external changes (e.g. Flair app, manual HA overrides).
    # 0 = disabled. Should not exceed cycle_timeout_hours * 60.
    reconciliation_interval_min: int = 0


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
