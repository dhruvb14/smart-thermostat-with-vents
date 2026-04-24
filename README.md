# Plenum

A Home Assistant add-on that provides HVAC zoning control for your home. Plenum drives HA cover entities (smart vents like Flair, or any other `cover.*` integration) and climate thermostats using temperature data from your native HA sensors, with per-room scheduling, presence-based activation, and a full web UI accessible via HA Ingress.

---

## How it works

Each thermostat zone gets one HVAC cycle engine. When rooms in a zone become active (via schedule or motion), the engine:

1. Opens all active room vents
2. Sets the thermostat setpoint past the target by an overshoot delta (e.g. +2°F for heating) to keep the HVAC running
3. Monitors each room's average temperature from its sensors
4. Closes a room's vents when it hits its target temperature
5. Once all rooms are at target, resets the thermostat setpoint to its own ambient reading — the HVAC shuts off naturally

Multiple rooms sharing one thermostat are fully supported and are the primary use case.

---

## Installation

### Option A — Home Assistant Add-on (recommended)

1. In HA, go to **Settings → Add-ons → Add-on Store → ⋮ → Repositories**
2. Add this repository URL
3. Find **Plenum** and click **Install**
4. Go to the add-on **Configuration** tab and fill in your HA URL and long-lived access token (see below)
5. Click **Start** — the UI is available via the **Open Web UI** button (HA Ingress)

### Option B — Docker (standalone / testing)

Pull the image built by GitHub Actions:

```bash
docker pull ghcr.io/dhruvb14/smart-thermostat-with-vents:latest
```

Run it:

```bash
docker run -d \
  --name smart-vent \
  -p 8099:8099 \
  -v /path/to/data:/data \
  -e HA_URL=https://your-ha-instance.com \
  -e HA_TOKEN=your_long_lived_token \
  ghcr.io/dhruvb14/smart-thermostat-with-vents:latest
```

Open `http://localhost:8099` in your browser.

### Option C — Local development

```bash
git clone https://github.com/dhruvb14/smart-thermostat-with-vents.git
cd smart-thermostat-with-vents

# Create .env
cat > .env <<EOF
HA_URL=https://your-ha-instance.com
HA_TOKEN=your_long_lived_token
DATA_DIR=/tmp/flair-dev
PORT=8099
EOF

# Install Python deps
pip install -e ".[dev]"

# Build frontend
cd frontend && npm install && npm run build && cd ..

# Run
set -a && source .env && set +a
python -m backend.main
```

---

## Getting a Long-Lived Access Token

1. In HA, click your profile picture (bottom-left)
2. Scroll to **Long-Lived Access Tokens**
3. Click **Create Token**, give it a name (e.g. `smart-vent`)
4. Copy the token — you won't see it again

---

## First-time setup

Once the UI is open, follow these steps in order:

### 1. Register your thermostats

Go to **Thermostats → + Register thermostat**

- Pick the HA climate entity for each of your HVAC systems
- Give it a friendly name (e.g. "Upstairs HVAC", "Downstairs HVAC")
- Set a **Default presence temp** — this is the fallback temperature used when a room is activated by motion and has no room-level presence temp configured
- Adjust safety limits if needed (min/max setpoint, deadband, overshoot delta)

### 2. Create rooms

Go to **Rooms → + Add room**

- Give the room a name
- Select which registered thermostat controls this zone
- Optionally set a room-level presence temperature (overrides the thermostat default)

### 3. Configure sensors and vents

Click **Configure sensors & vents →** on any room card.

**Temperature Sensors** — add all `sensor.*` entities in the room. The engine averages them. Add as many as you want.

**Vents** — add the `cover.*` entities for each vent in the room. These are what gets opened and closed during cycles.

**Presence / Motion Sensors** — add `binary_sensor.*` entities. When any fires, the room activates at the presence temperature and stays active for the holdover period (default 2 hours, reset on each detection).

### 4. Add schedules

Go to **Schedules**, select a room, and add time blocks. Each block has:
- Days of week
- Start and end time
- Target temperature (°F)

Rooms activate when the current time falls inside a matching block.

### 5. Enable the system

The **System On/Off** toggle in the top-right of every page controls whether the engine makes any changes to HA. While **System Off**, the engine monitors state but makes zero calls to HA — no vent moves, no setpoint changes. Use this while transitioning from another HVAC control system.

---

## Room settings reference

| Setting | Description |
|---|---|
| **Thermostat** | Which registered thermostat (zone) this room belongs to |
| **Presence temp** | Target °F when activated by motion, no schedule active. Falls back to thermostat default if blank. |
| **Presence holdover** | How long (hours) to keep the room active after last motion. 0 = disabled. |
| **Include thermostat sensor** | Average the thermostat's own built-in sensor along with the room sensors |
| **Temperature offset** | Added to the room's measured avg before comparing to target. Use to compensate for post-closure drift — e.g. if your room targets 70°F but always ends up at 67°F after the vent closes, set +3 so the vent closes at 67°F actual (67+3=70 "at target") and the room drifts to ~70°F |

---

## Thermostat settings reference

| Setting | Description |
|---|---|
| **Default presence temp** | Fallback target °F for presence-activated rooms that have no room-level presence temp |
| **Min / Max setpoint** | Hard clamps — the engine will never set the thermostat outside this range |
| **Deadband** | ±°F tolerance for "at target". 0 = exact match. 0.5 means a 70°F target is satisfied at 69.5–70.5°F |
| **Overshoot delta** | How far past the most demanding room's target to set the thermostat to keep the HVAC running (default 2°F) |
| **Max vent closed** | Reopen a vent after this many minutes even if the room is still at target (safety valve for systems that need airflow). 0 = disabled |
| **Min open vents** | Always keep at least this many vents open across the zone. 0 = allow all closed |
| **Cycle timeout** | Abort a cycle that has been running longer than this many hours |

---

## Migrating from a dev/local instance

All configuration lives in a single SQLite file (`app.db` in the `DATA_DIR`). To carry your setup over:

1. Stop the add-on
2. Copy your local `app.db` (default: `/tmp/flair-dev/app.db`) to the add-on data directory:
   - **HA OS / Supervised**: accessible via SSH at `/addon_data/<slug>/data/app.db`, or via the Samba share under `addon_configs`
   - **Docker**: wherever you mounted `/data`
3. Start the add-on — it will apply any pending migrations automatically

**Upgrading from ≤0.6.x:** the on-disk database was previously named `flair.db`. The add-on renames it to `app.db` automatically on first boot after upgrade — no manual steps required.

---

## Dashboard

The Dashboard shows one card per thermostat zone with:
- Friendly thermostat name and HA entity ID
- Current HVAC action (heating / cooling / idle / off)
- Ambient temperature and active setpoint
- Cycle state and per-room progress bar
- Live vent states (open/closed) for all active rooms

It updates in real time via WebSocket.

---

## Logs

The **Logs** page has two tabs:

**Live Feed** — every significant event in real time: vent opens/closes, cycle starts/ends, setpoint changes, presence detections, API mutations, system enable/disable. Filter by category (engine, api, presence, ha, system). Click any entry to expand JSON details. Pause button stops auto-scroll.

**Cycle History** — a table of completed and active HVAC cycles with duration, mode, and per-room breakdown.

---

## Architecture

```
HA WebSocket API
      │
      ▼
 ha_client.py        ← state cache + service calls (cover, climate)
      │
      ▼
 scheduler.py        ← one CycleEngine per thermostat, 60s tick
      │
      ├── cycle_engine.py   ← HVAC cycle state machine
      ├── vent_controller.py ← open/close with safety guards
      └── room_manager.py   ← schedule / presence / override resolution
      │
      ▼
 aiohttp server
      ├── REST API  (/api/*)
      ├── WebSocket (/ws)
      └── Static frontend (React + Vite)
```

Data is persisted in SQLite via `aiosqlite`. No external services or cloud dependencies.

---

## Docker image

Built automatically by GitHub Actions on every push to `main` and on `v*.*.*` tags.

```
ghcr.io/dhruvb14/smart-thermostat-with-vents:latest
ghcr.io/dhruvb14/smart-thermostat-with-vents:sha-<commit>
ghcr.io/dhruvb14/smart-thermostat-with-vents:1.2.3   # on version tags
```
