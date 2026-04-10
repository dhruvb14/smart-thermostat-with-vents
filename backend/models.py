"""
Dataclasses for all domain entities.
All temperatures in °F internally. Conversion happens at HA ingestion.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Optional


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
    system_wide_temp: Optional[float] = None
    presence_holdover_hours: float = 2.0
    notes: str = ""
    temp_offset: float = 0.0  # °F added to measured avg before comparing to target

    @classmethod
    def create(cls, name: str, thermostat_entity_id: str, **kwargs) -> "Room":
        return cls(id=new_id(), name=name, thermostat_entity_id=thermostat_entity_id, **kwargs)


@dataclass
class RoomSensor:
    id: str
    room_id: str
    entity_id: str  # sensor.*

    @classmethod
    def create(cls, room_id: str, entity_id: str) -> "RoomSensor":
        return cls(id=new_id(), room_id=room_id, entity_id=entity_id)


@dataclass
class RoomVent:
    id: str
    room_id: str
    entity_id: str  # cover.*

    @classmethod
    def create(cls, room_id: str, entity_id: str) -> "RoomVent":
        return cls(id=new_id(), room_id=room_id, entity_id=entity_id)


@dataclass
class RoomPresenceSensor:
    id: str
    room_id: str
    entity_id: str  # binary_sensor.*

    @classmethod
    def create(cls, room_id: str, entity_id: str) -> "RoomPresenceSensor":
        return cls(id=new_id(), room_id=room_id, entity_id=entity_id)


@dataclass
class Schedule:
    id: str
    room_id: str
    days_of_week: list[int]  # 0=Monday … 6=Sunday
    start_time: time          # local time
    end_time: time
    target_temp: float        # °F

    @classmethod
    def create(
        cls,
        room_id: str,
        days_of_week: list[int],
        start_time: time,
        end_time: time,
        target_temp: float,
    ) -> "Schedule":
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
    thermostat_entity_id: str  # PK
    min_setpoint: float = 60.0
    max_setpoint: float = 85.0
    deadband: float = 0.5       # ±°F
    max_vent_closed_min: int = 0  # 0 = unlimited
    min_open_vents: int = 1
    overshoot_delta: float = 2.0
    cycle_timeout_hours: float = 3.0


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
    mode: str               # 'heating' | 'cooling'
    rooms_json: str         # JSON snapshot
    ended_at: Optional[datetime] = None

    @classmethod
    def create(cls, thermostat_entity_id: str, mode: str, rooms_json: str) -> "CycleLog":
        return cls(
            id=new_id(),
            thermostat_entity_id=thermostat_entity_id,
            started_at=datetime.utcnow(),
            mode=mode,
            rooms_json=rooms_json,
        )


@dataclass
class RoomCycleState:
    cycle_id: str
    room_id: str
    target_temp: float
    reached_at: Optional[datetime] = None
    vent_closed_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Runtime-only (not persisted to DB)
# ---------------------------------------------------------------------------

@dataclass
class RoomLiveState:
    """In-memory snapshot of a room's current sensor readings."""
    room_id: str
    avg_temp: Optional[float]         # None if all sensors unavailable
    sensor_count: int
    available_sensor_count: int
    vent_states: dict[str, str]       # entity_id → 'open'|'closed'|'unknown'
    presence_active: bool
    holdover_expires_at: Optional[datetime]


@dataclass
class ZoneStatus:
    """Summarised status for one thermostat zone (all rooms sharing that thermostat)."""
    thermostat_entity_id: str
    hvac_mode: str              # 'heating'|'cooling'|'off'|'unknown'
    hvac_action: str
    current_temp: Optional[float]
    setpoint: Optional[float]
    cycle_id: Optional[str]
    cycle_started_at: Optional[datetime]
    rooms: list[RoomLiveState] = field(default_factory=list)
