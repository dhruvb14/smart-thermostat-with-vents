# Observability

## Dashboard

The Dashboard shows one card per thermostat zone:

- Friendly thermostat name and HA entity ID.
- Current HVAC action (heating / cooling / idle / off) and mode.
- Ambient temperature and current setpoint.
- Cycle state (idle / running / terminating) and a per-room progress indicator.
- Live vent states (open / closed) for each room in the active cycle.

Cards update in real time — the Dashboard subscribes to the WebSocket and re-renders on each status push.

## Live feed

The **Logs** page has a **Live Feed** tab that streams every significant event as it happens:

- Engine decisions (cycle start, vents opened/closed, setpoint changes, cycle end).
- Presence activations and holdover expiries.
- API mutations (rooms added, schedules edited, config changed).
- HA connection events (connected, reconnected, service-call failures).
- System enable/disable and Dev Mode toggles.

Each row has a **level** (info/warning/error) and a **category** (engine / presence / api / ha / system). Filter by category, pause auto-scroll, and click any row to expand the JSON detail payload.

## Cycle history

The second tab on **Logs** is **Cycle History** — one row per completed or running cycle with duration, mode, thermostat, and a drill-down that shows:

- Per-room timeline (entered cycle, hit target, vent closed).
- Temperature samples over the cycle's lifetime.
- Setpoint changes.
- Individual vent events.

Use this to diagnose runaway cycles, rooms that never reach target, or surprises from external overrides.

## WebSocket

Everything above is driven by a single `/ws` endpoint that pushes two event types:

- **`log_event`** — one per persisted log entry.
- **`status`** — zone-status snapshots whenever the engine's view changes (cycle transitions, vent moves, setpoint updates).

The WebSocket is push-only from server to client; the UI opens it once and reconnects automatically if dropped.

## Retention

Event log retention is configurable per category on the **Logs** page. Cycle logs are kept indefinitely unless you clear them manually.
