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
- Periodic drift reconciliation — the engine checks that actual vent/thermostat state still matches its own intent and logs a correction if it doesn't.
- Dev Mode dry-runs — with Dev Mode on, the "would set thermostat / would open vent" actions the engine *would* have sent, logged instead of actually calling HA services.

Each row has a **level** (info/warning/error) and a **category** (`engine` / `presence` / `api` / `ha` / `system` / `reconcile` / `dev`). Filter by category, pause auto-scroll, and click any row to expand the JSON detail payload.

## Cycle history

The second tab on **Logs** is **Cycle History** — one row per completed or running cycle with duration, mode, thermostat, and a drill-down that shows:

- Per-room timeline (entered cycle, hit target, vent closed).
- Temperature samples over the cycle's lifetime.
- Setpoint changes.
- Individual vent events.

Use this to diagnose runaway cycles, rooms that never reach target, or surprises from external overrides.

## WebSocket

Everything above is driven by a single `/ws` endpoint that pushes two event types relevant to observability:

- **`log_event`** — one per persisted log entry (Live Feed).
- **`zone_status`** — pushed whenever the engine's view changes (cycle transitions, vent moves, setpoint updates); the Dashboard treats it as a change notification and re-fetches zone/room state over REST rather than rendering the payload directly.

(The `/ws` endpoint also pushes a handful of non-observability event types — `system_enabled_changed`, `dev_mode_changed`, `mcp_enabled_changed`, `theme_changed` — used to keep other parts of the UI in sync; they're not part of the Logs/Dashboard flow described here.)

The WebSocket is push-only from server to client; the UI opens it once and reconnects automatically if dropped.

## Retention

The **Logs** page has a third tab, **Retention**, with two independent settings — **event log retention (days)** (default 7) and **cycle history retention (days)** (default 30). Both event logs and cycle logs are purged automatically once past their configured age; neither is kept indefinitely. The scheduler runs the purge daily and once on every startup.
