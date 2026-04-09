# Flair Replacement Add-on — Technical Design Document

## Context

The native Flair app provides basic scheduling and vent control but lacks deep integration with Home Assistant's thermostat ecosystem, flexible room-level logic, and presence-awareness. This add-on replaces the Flair cloud app entirely: it controls Flair vents (exposed as HA `cover` entities via the Flair HACS integration) while sourcing all temperature data and thermostat commands from native HA entities. The result is a fully self-hosted, HVAC-cycle-aware system with per-room scheduling, presence detection, and automatic shutoff.

---

## Tech Stack

| Layer | Choice | Rationale |
|---|---|---|
| Backend | Python 3.12, asyncio, aiohttp | Standard HA add-on ecosystem; async native for WS |
| Frontend | React 18 + Vite | Component-based, fast HMR, easy dashboard/room cards |
| DB | SQLite via `aiosqlite` | Zero-dependency, single file, async-friendly |
| HA comms | Raw HA WebSocket API | Real-time entity subscriptions |
| Container | Docker (HA add-on / app format) | Ingress-served UI, s6-overlay supervisor |

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────┐
│                  Add-on Container                   │
│                                                     │
│  ┌─────────────┐    ┌──────────────────────────┐   │
│  │  React UI   │◄───│   aiohttp REST/WS Server  │   │
│  │  (Vite build│    │   /api/*  +  /ws          │   │
│  │   /ingress) │    └──────────┬───────────────┘   │
│  └─────────────┘               │                   │
│                        ┌───────▼────────┐          │
│                        │  Core Engine   │          │
│                        │  ┌──────────┐  │          │
│                        │  │Scheduler │  │          │
│                        │  │Presence  │  │          │
│                        │  │HVAC Ctrl │  │          │
│                        │  │Vent Ctrl │  │          │
│                        │  └──────────┘  │          │
│                        └───────┬────────┘          │
│                                │                   │
│                        ┌───────▼────────┐          │
│                        │  HA WS Client  │          │
│                        │  (subscribe +  │          │
│                        │   call service)│          │
│                        └───────┬────────┘          │
│                                │                   │
└────────────────────────────────┼────────────────────┘
                                 │ WebSocket
                    ┌────────────▼────────────┐
                    │  Home Assistant Core    │
                    │  - climate entities     │
                    │  - sensor entities      │
                    │  - cover entities       │
                    │  - binary_sensor        │
                    │    (presence/motion)    │
                    └─────────────────────────┘
```

---

## Data Model

```
Room:
  id: UUID
  name: str
  thermostat_entity_id: str           # climate.* entity
  include_thermostat_sensor: bool     # use thermostat's own temp sensor
  system_wide_temp: float | null      # target temp when presence triggers activation
  presence_holdover_hours: float      # configurable per room (default 2.0); 0 = disable
  notes: str

PresenceHoldoverState:    # runtime, persisted to survive restarts
  room_id: FK Room
  last_detected_at: datetime
  expires_at: datetime    # last_detected_at + presence_holdover_hours

RoomSensor:               # temperature sensors in room
  id: UUID
  room_id: FK Room
  entity_id: str          # sensor.* entity

RoomVent:                 # Flair cover entities
  id: UUID
  room_id: FK Room
  entity_id: str          # cover.* entity

RoomPresenceSensor:       # binary_sensor.* (motion/presence)
  id: UUID
  room_id: FK Room
  entity_id: str

Schedule:                 # weekly time-block
  id: UUID
  room_id: FK Room
  days_of_week: JSON      # [0..6], 0=Monday
  start_time: time
  end_time: time
  target_temp: float

ThermostatConfig:         # safety limits per thermostat
  thermostat_entity_id: str   # PK
  min_setpoint: float
  max_setpoint: float
  deadband: float             # ±°F tolerance (0 allowed)
  max_vent_closed_min: int    # 0 = no limit (bypass damper systems)
  min_open_vents: int         # 0 = allow all closed
  overshoot_delta: float      # default 2.0°F
  cycle_timeout_hours: float  # abort cycle after N hours; default 3.0

CycleLog:                 # audit trail
  id: UUID
  thermostat_entity_id: str
  started_at: datetime
  ended_at: datetime | null
  mode: str               # 'heating' | 'cooling'
  rooms_json: JSON

RoomCycleState:           # per-room state during a cycle
  cycle_id: FK CycleLog
  room_id: FK Room
  target_temp: float
  reached_at: datetime | null
  vent_closed_at: datetime | null

RoomOverride:             # manual target set by automation or MCP
  room_id: FK Room (PK)
  target_temp: float
  expires_at: datetime
```

---

## Core Logic: HVAC Cycle Engine

### Scheduling Priority (highest first)
1. **Room override** — set via REST API or MCP (expires after configured duration)
2. **Room schedule** — weekly time block matching current day/time
3. **Presence holdover** — presence was detected within holdover window
4. **Idle** — vents closed, no action

### Presence Holdover
- Per-room configurable window (default 2h, `presence_holdover_hours`)
- Every new presence event resets the countdown (`expires_at = now + holdover_hours`)
- Room stays active until `expires_at` passes with no new detection
- `presence_holdover_hours = 0` disables the feature for that room

### Cycle Lifecycle

```
TRIGGER (scheduler tick every 60s + on HA state change):
  For each thermostat with ≥1 active room:

  1. DETERMINE ACTIVE ROOMS
     Override > Schedule > Presence holdover

  2. READ HVAC MODE
     climate.hvac_action ∈ {'cooling','heating'}
     If 'off': skip thermostat setpoint changes, continue monitoring

  3. COMPUTE SETPOINT
     cooling: setpoint = min(targets) - overshoot_delta
     heating: setpoint = max(targets) + overshoot_delta
     Clamped to [min_setpoint, max_setpoint]
     → climate.set_temperature

  4. OPEN VENTS for all active rooms

  5. MONITOR (30s tick + sensor state change)
     avg_temp = mean(room sensors [+ thermostat sensor if enabled])
     At target if:
       cooling: avg_temp ≤ target + deadband
       heating: avg_temp ≥ target - deadband
     Close vent when at target, respecting min_open_vents

  6. MAX VENT CLOSED CHECK
     If max_vent_closed_min > 0 and vent closed > limit → reopen

  7. TERMINATION
     All active rooms at target →
       setpoint = thermostat.current_temperature (clamped)
       → HVAC shuts off naturally

  8. CYCLE TIMEOUT
     If cycle running > cycle_timeout_hours → log warning + terminate
```

### Edge Cases

| Scenario | Handling |
|---|---|
| Sensor unavailable | Exclude from average; skip room if all sensors down |
| Thermostat unavailable | Close all vents; abort cycle; alert |
| HVAC mode change mid-cycle | Restart cycle with new mode |
| min_open_vents prevents closure | Defer vent close; retry next tick |
| No active rooms | Leave thermostat untouched |
| Setpoint clamped at safety bound | Log warning; cycle timeout eventually terminates |
| Unit mismatch (°C vs °F) | Normalize via HA `unit_of_measurement` |
| Presence holdover_hours = 0 | Presence detection disabled for that room |

---

## REST API (`/api`)

```
Rooms
  GET/POST        /api/rooms
  GET/PUT/DELETE  /api/rooms/{id}
  POST/DELETE     /api/rooms/{id}/sensors/{eid}
  POST/DELETE     /api/rooms/{id}/vents/{eid}
  POST/DELETE     /api/rooms/{id}/presence/{eid}

Schedules
  GET/POST        /api/rooms/{id}/schedules
  PUT/DELETE      /api/rooms/{id}/schedules/{sid}

Thermostats
  GET             /api/thermostats
  PUT             /api/thermostats/{entity_id}

Overrides
  POST/DELETE     /api/rooms/{id}/override

System
  GET             /api/status
  GET             /api/ha/entities?domain=sensor
  GET             /api/logs

WebSocket
  WS              /ws    → RoomStateUpdate, CycleEvent
```

---

## MCP Server

Exposes all room/schedule/thermostat management as MCP tools for Claude.

**Tools:** `list_rooms`, `get_room`, `create_room`, `update_room`, `delete_room`,
`add_sensor`, `remove_sensor`, `add_vent`, `remove_vent`,
`add_presence_sensor`, `remove_presence_sensor`,
`list_schedules`, `create_schedule`, `update_schedule`, `delete_schedule`,
`list_thermostat_configs`, `set_thermostat_config`,
`set_room_override`, `clear_room_override`, `get_system_status`,
`list_ha_entities`

**Transport:** stdio (local) or SSE (remote)

**Claude Code config:**
```json
{
  "mcpServers": {
    "flair-replacement": {
      "command": "python",
      "args": ["/path/to/addon/backend/mcp_server.py"],
      "env": {
        "HA_URL": "http://homeassistant.local:8123",
        "HA_TOKEN": "<long-lived-access-token>"
      }
    }
  }
}
```

---

## Frontend UI (5 pages)

1. **Dashboard** — live zone cards: cycle status, room temp/target/vent/presence
2. **Rooms** — CRUD with HA entity picker autocomplete
3. **Schedules** — weekly calendar grid per room
4. **Settings** — thermostat safety config per zone
5. **Logs** — cycle history with expandable room detail

---

## Project Structure

```
/
├── DESIGN.md
├── config.yaml          # HA add-on manifest
├── Dockerfile
├── run.sh
├── pyproject.toml
├── backend/
│   ├── main.py
│   ├── ha_client.py
│   ├── db.py
│   ├── models.py
│   ├── scheduler.py
│   ├── engine/
│   │   ├── cycle_engine.py
│   │   ├── room_manager.py
│   │   └── vent_controller.py
│   ├── api/
│   │   ├── routes.py
│   │   └── ws_handler.py
│   ├── mcp_server.py
│   ├── mcp_tools/
│   │   ├── rooms.py
│   │   ├── schedules.py
│   │   ├── thermostats.py
│   │   ├── status.py
│   │   └── ha_entities.py
│   └── tests/
└── frontend/
    ├── src/
    │   ├── pages/
    │   ├── components/
    │   └── api.ts
    └── vite.config.ts
```
