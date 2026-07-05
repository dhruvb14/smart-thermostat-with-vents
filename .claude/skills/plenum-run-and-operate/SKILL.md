---
name: plenum-run-and-operate
description: >-
  How to run and operate Plenum (smart-thermostat-with-vents) in its three
  modes — HA add-on, standalone Docker, local dev — and where its data lives.
  Load when installing/starting/restarting the add-on, wiring the docker run
  command, hunting for app.db on HAOS, doing backup/restore, attaching an MCP
  client to port 9099 (or hitting its 503), setting up a fresh instance
  (first-time setup order, System On/Off), or checking what happens across a
  restart/upgrade (migrations, unit-change banner, cycle restore, safety
  defaults). NOT for building from source (plenum-build-and-env) or for
  measuring/diagnosing a running system (plenum-diagnostics-and-tooling).
---

# Running and operating Plenum

Runbook for the three deployment modes, data locations, backup/restore, ports,
and what to check after an install or upgrade. All facts verified against the
repo at v0.22.1 (2026-07). File paths are relative to the repo root unless
absolute.

**When NOT to use this skill**
- Building the image / dev environment from scratch, npm/pip/Playwright setup → `plenum-build-and-env`.
- Reading logs, metrics, cycle history, DB queries to diagnose behavior → `plenum-diagnostics-and-tooling` (symptom triage: `plenum-debugging-playbook`).
- What a specific setting means / its bounds → `plenum-config-and-flags`.
- Why an invariant exists (°F storage, MCP loopback dispatch) → `plenum-architecture-contract`.

## The three modes at a glance

| Mode | Start command | HA credentials | Web UI | Data dir (`DATA_DIR`) |
|---|---|---|---|---|
| HA add-on (recommended) | Supervisor **Start** button | automatic (`SUPERVISOR_TOKEN` → `http://supervisor/core`) | **Open Web UI** (ingress); optional host port 8099 | `/config` in-container (add-on config share) |
| Docker standalone | `docker run` (below) | `-e HA_URL` + `-e HA_TOKEN` (long-lived token) | `http://localhost:8099` | **must** set `-e DATA_DIR=/data` + `-v …:/data` (see trap) |
| Local dev | `python -m backend.main` from `smart_vent/` | `.env` at repo root (`HA_URL`, `HA_TOKEN`) | `http://localhost:8099` (or `PORT`) | `.env`'s `DATA_DIR` (sample: `./data`, i.e. `smart_vent/data/`) |

The same entrypoint runs in all three: `backend/main.py` binds the web UI on
`PORT` (default 8099, `smart_vent/backend/main.py:44`) and starts the MCP
server on `MCP_PORT` (default 9099, line 47). In containers, `run.sh` resolves
config first, then `exec python3 -m backend.main`.

---

## Mode 1 — Home Assistant add-on

Install (README "Installation" Option A):
1. HA → **Settings → Add-ons → Add-on Store → ⋮ → Repositories** → add this repo's URL.
2. Install **Plenum** (slug `plenum`, `smart_vent/config.yaml`).
3. **Configuration** tab → set `timezone`. No token/URL needed — `run.sh` uses
   `SUPERVISOR_TOKEN` and the `http://supervisor/core` proxy automatically
   (`smart_vent/run.sh:51-71`).
4. **Start** → open via **Open Web UI** (ingress, `ingress_port: 8099`).

### Configuration-tab options (`config.yaml` `options:`/`schema:`)

| Option | Default | Meaning |
|---|---|---|
| `ha_url` | `""` | Override HA URL. Blank = supervisor proxy. Can combine custom URL with supervisor token (leave `ha_token` blank). |
| `ha_token` | `""` | Override token. Blank = `SUPERVISOR_TOKEN`. |
| `use_wss` | `false` | Use `wss://` for the HA WebSocket. |
| `ssl_verify` | `true` | Verify TLS certs. |
| `timezone` | `America/New_York` | IANA zone for schedule evaluation — exported as `TZ`. |
| `temperature_unit` | `""` | `"F"`/`"C"` hard override; blank = auto-detect from HA (`run.sh:41-45`, issue #281 — do not default this to `F`). |

Plus the **Network** section: host-port rows for `8099/tcp` (direct web UI,
blank = ingress-only) and `9099/tcp` (MCP — see "Ports & MCP" below). Both
default to `null` (unmapped).

### Timezone requirement and its failure mode

Schedules are evaluated in `TZ`. `run.sh:40` falls back to `UTC` when the
option is unset — with a UTC clock a "06:00–08:00 weekday" block fires at
06:00 *UTC*, i.e. 1–2 a.m. US Eastern: schedules appear to "misfire" hours
early/late. The add-on option defaults to `America/New_York`, so US-East
installs happen to work, but **always set your real zone**. DST is handled by
the zone database. (README "Timezone configuration".)

---

## Mode 2 — Docker standalone

```bash
docker pull ghcr.io/dhruvb14/smart-thermostat-with-vents:latest

docker run -d \
  --name smart-vent \
  -p 8099:8099 \
  -v /path/to/data:/data \
  -e DATA_DIR=/data \
  -e HA_URL=https://your-ha-instance.com \
  -e HA_TOKEN=your_long_lived_token \
  -e TIMEZONE=America/New_York \
  ghcr.io/dhruvb14/smart-thermostat-with-vents:latest
```

Then open `http://localhost:8099`. Add `-p 9099:9099` if you want MCP.

This is the README command **plus two corrections you must not drop**
(both verified against `run.sh` at v0.22.1; the README's Option B predates
the `/config` data migration and still omits them):

- **`-e DATA_DIR=/data` is required.** `run.sh:82` defaults `DATA_DIR` to
  `/config` (for the HAOS addon-config share) and the `Dockerfile` sets no
  `DATA_DIR`, so without this env var `app.db` is written to `/config` inside
  the container — your `-v …:/data` mount sits empty and **all configuration
  is lost on container replacement**. The repo's own test stack sets it
  explicitly (`docker-compose.test.yml`: `DATA_DIR: "/data"`). Alternatively
  mount your volume at `/config` instead.
- **Use `-e TIMEZONE=…`, not only `-e TZ=…`.** `run.sh` computes
  `TZ="${TIMEZONE:-UTC}"` (line 81) from the `timezone` config key, whose
  env-var fallback is the uppercased key `TIMEZONE` (`get_config`,
  `run.sh:16-18`). A plain `-e TZ=` is overwritten with `UTC` → schedule
  misfires as above.

Other env vars read via the same fallback: `HA_URL`, `HA_TOKEN`, `USE_WSS`,
`SSL_VERIFY`, `TEMPERATURE_UNIT` (blank = auto-detect), plus direct `PORT`
(default 8099) and `MCP_PORT` (default 9099). Without a `SUPERVISOR_TOKEN`,
`HA_URL` defaults to `http://homeassistant.local:8123` (`run.sh:74`).

The general volume-loss trap (README, docs/backup-restore.md): if the data
dir is not on a mounted volume, `app.db` lives in the ephemeral container
layer and vanishes on restart/recreate.

---

## Mode 3 — Local development

From a set-up clone (see `plenum-build-and-env` for the install steps):

```bash
cp .env.sample .env        # once; set HA_URL + HA_TOKEN
source .venv/bin/activate
cd smart_vent
python -m backend.main
```

- `backend/main.py:34` loads the repo-root `.env` (python-dotenv); in the
  add-on container the file doesn't exist and env comes from `run.sh`.
- `.env.sample`: `HA_URL`, `HA_TOKEN`, `DATA_DIR=./data`, `PORT=8099`.
  `./data` is relative to the process cwd, so running from `smart_vent/`
  puts the DB at `smart_vent/data/app.db`. If `DATA_DIR` is unset entirely,
  the code default is `/data` (`main.py:42`). (The README's mention of
  `/tmp/flair-dev/app.db` as the local default is stale.)
- UI at `http://localhost:8099` (or your `PORT`). The server serves the built
  frontend from `smart_vent/frontend/dist` — run `npm run build` first, or use
  the Vite dev server on 5173 (proxies `/api` + `/ws`; see `plenum-build-and-env`).

---

## Where the data lives

Everything — rooms, vents, schedules, thermostat configs, cycle history,
event logs, system settings — is one SQLite file: **`app.db` in `DATA_DIR`**
(`main.py:42-43`).

### HAOS / Supervised real host path (issue #92)

Issue #92 established (via live HAOS investigation) that:
- `/root/addon_configs` (the Samba `addon_configs` share) held add-on
  *configuration* files, **not** the `/data` directory — the old docs pointing
  there were wrong.
- The legacy `/data` mount's real host path is
  `/mnt/data/supervisor/addons/data/<repo_id>_plenum/` (an ext4 partition;
  the `hassio_cli` SSH container can't see it, which made it *look* empty).

The authoritative way to find the path — the docker-inspect recipe from
README / `docs/backup-restore.md` — run from the HAOS SSH terminal:

```bash
docker inspect $(docker ps -q --filter name=plenum) --format '{{ json .Mounts }}' | python3 -m json.tool
```

**Since the addon-config migration** (shipped well before v0.22.1;
`config.yaml:42-46` sets `environment: DATA_DIR: /config` and
`map: addon_config:rw`), the picture changed:
- `app.db` now lives at **`/config` inside the container**, which *is* the
  Samba-accessible `addon_configs/<repo_id>_plenum/` share — `run.sh:91-107`
  performs a one-time copy of the DB (+ `-wal`/`-shm` sidecars) from legacy
  `/data` to `/config` precisely so it becomes reachable over Samba.
- In `docker inspect` output, look for the mount whose `Destination` is
  **`/config`** for the live DB. The `/data` mount still exists but now holds
  only Supervisor-written `options.json` (plus a stale pre-migration DB copy).
- README's "Migrating from a dev/local instance" section and
  `docs/backup-restore.md` still describe the pre-migration `/data` location;
  trust `config.yaml`/`run.sh` over them.

### `flair.db` → `app.db` auto-rename (≤0.6.x, issue #89 era)

Installs from before the Flair-replacement → Plenum rename stored the DB as
`flair.db`. On every startup, `backend/main.py:52-61`
(`_migrate_db_filename`) renames `flair.db` → `app.db` (with `-wal`/`-shm`
sidecars) before any connection opens. Idempotent; no manual steps
(`docs/backup-restore.md` "Upgrading from ≤0.6.x"). `run.sh`'s /data→/config
copy also carries `flair.db` so the rename still fires post-move.

---

## Backup & restore

Doc of record: `docs/backup-restore.md`. Verified endpoints in
`smart_vent/backend/api/routes.py`:

| Action | Endpoint | UI | Notes |
|---|---|---|---|
| Backup | `GET /api/backup` (routes.py:2423) | Settings → **Download backup** | Uses `sqlite3.backup()` for a WAL-consistent snapshot — copying `app.db` off disk can miss unflushed `-wal` writes. Serves `app.db` as an attachment; snapshot is read into memory and the temp file deleted (disk-leak fix, issue #298). |
| Restore | `POST /api/restore` (routes.py:2464) | Settings → **Restore** (file upload) | Multipart field `file`; validates SQLite magic bytes (`SQLite format 3\0`), swaps the file, then `scheduler.reload_db()` reloads the connection **in place — no add-on restart needed**. |

Take a backup before any risky config change, restore, or version upgrade.

---

## Ports, ingress, and MCP attach

| Port | What | Exposure |
|---|---|---|
| 8099 | Web UI + REST (`/api/*`) + WS (`/ws`) + Swagger (`/api/docs`) | Add-on: HA ingress always; direct host port only if you map `8099/tcp` in the Network section (default `null` = ingress-only). Docker: `-p 8099:8099`. |
| 9099 | MCP server (Streamable HTTP) at path **`/mcp`** | Default unmapped (`config.yaml` `ports: 9099/tcp: null`). Runs inside the same process (`main.py` `_start_mcp_server`, uvicorn) — no separate program. |

Attaching an MCP client (doc of record: `docs/mcp.md`) needs **both**:
1. **Toggle it on**: web UI settings cog (⚙️) → MCP toggle (POST
   `/api/system/mcp`, persisted as `mcp_enabled`). **Off by default** because
   the endpoint is unauthenticated with full write access (auth is open issue
   #373). Until enabled, `/mcp` returns **503** (`backend/mcp_http.py:123`) —
   a mapped-but-503 port is expected, not broken. Takes effect immediately,
   no restart; unlike System On/Off it never touches HVAC.
2. **Publish the port**: HAOS — add-on **Configuration → Network**, set a host
   port on the `9099/tcp` row, Save, Restart. Docker — `-p 9099:9099`.

Then point the client at `http://<host>:<port>/mcp`.

**The #387 story (read `git show c65d35d`)**: v0.22.1 briefly shipped
`9099/tcp: 9099` — a *default-mapped* host port — so the row would appear in
existing installs' Network config. That was reverted to `null` in commit
`c65d35d` ("Revert MCP port to null…", #387): an open-by-default port for an
unauthenticated write surface was the wrong trade (see
`plenum-failure-archaeology`). Consequences for operators:
- The MCP port must be **manually mapped** after updating; it is never
  exposed by default.
- **"No `9099/tcp` row?"** The Supervisor re-reads an add-on's `config.yaml`
  (including `ports:`) only when the **version changes and you press
  Update** — re-pulling the same version does not. `9099/tcp` was declared in
  v0.22.1, so apply any pending update and the row appears (`docs/mcp.md`).

MCP tools are generated from the OpenAPI spec and dispatched back through the
loopback REST API (`main.py:250`, `base_url=http://127.0.0.1:PORT`) — design
rationale in `plenum-architecture-contract`.

---

## First-time setup order and System On/Off

Follow README "First-time setup" in order:

1. **Register thermostats** (Thermostats → + Register). `total_vents_count`
   is **mandatory** at registration — count *every* register on the zone,
   smart AND passive (routes.py:992, airflow floor #213).
2. **Create rooms** and assign each to a thermostat.
3. **Configure sensors & vents** per room: `sensor.*` temps (averaged),
   `cover.*` vents, `binary_sensor.*` presence.
4. **Add schedules** (days, start/end, target temp).
5. **Check the System On/Off toggle** (top-right, every page).

### System On/Off semantics (owned by this skill; code wins over `docs/system-modes.md`)

- **On** — engines tick every 60 s and drive vents/thermostats.
- **Off** (with Dev Mode also off) — "monitoring only": engine ticks are
  skipped and Plenum makes **zero HA service calls**; UI, logs, and state
  monitoring keep working. Use while migrating off another control system.
- **The engine gate is `system_enabled OR dev_mode`** (`scheduler.py:477`).
  So with System Off + Dev Mode On, engines **DO tick** — they run the full
  decision logic every 60 s while `ha_client.py` intercepts every write and
  logs `[DEV] Would …` instead of sending it. System Off fully stops ticking
  only when Dev Mode is also off. **Known doc bug:** `docs/system-modes.md`
  (~line 26) states the opposite precedence ("System Off takes precedence
  over Dev Mode") — that is wrong vs the code; needs a follow-up docs issue.
- **Caveat — a fresh install starts ON**: `system_enabled` defaults to `"1"`
  (`scheduler.py:56,88`). If you're setting up against live equipment, flip
  it Off *before* step 1 and back On at step 5.
- **The one-time in-flight abort nuance** (verified in code): flipping the
  toggle (either direction) calls `Scheduler._reset_and_reevaluate()`
  (`scheduler.py:248-254, 415-440`), which **force-aborts any in-flight cycle
  immediately** — it does not wait for the next 60 s tick. That abort itself
  issues HA calls even though the system is now Off: `_abort_cycle`
  (`cycle_engine.py:1392+`) re-opens **all** zone vents (active + idle rooms,
  #244) and resets the thermostat setpoint to ambient so the HVAC shuts off
  naturally. So "zero HA calls while Off" holds only *after* this one-time
  cleanup — by design, so equipment is never left mid-cycle with vents shut.
- **Dev Mode** is different: engines run fully but every HA service call is
  intercepted and logged instead of sent — and per the OR gate above, that
  holds even while System is Off. (Flag catalog: `plenum-config-and-flags`.)

---

## Restart / upgrade behavior

`POST /api/restart` (routes.py:2004) SIGTERMs the process; the Supervisor
restarts the add-on. On every startup:

1. `_migrate_db_filename` renames any `flair.db` (see above).
2. **DB migrations run automatically**: `backend/db.py` applies the additive
   `_MIGRATIONS` list (`ALTER TABLE …`, db.py:253-255) plus sentinel-guarded
   one-time data migrations (holdover-timestamp UTC fix #65; short-cycle
   back-fill, below). There is no manual migration step (versioned
   migrations are open debt, issue #21).
3. **Unit detection**: `TEMPERATURE_UNIT` env override wins and is persisted;
   otherwise last-known DB value is used and a background task re-resolves
   from HA once connected (`scheduler.py:99-106, _startup_resolve_unit`). If
   HA's unit differs from the stored one, `unit_change_ack_required` is set
   (`scheduler.py:408`) → the **UnitChangeBanner** appears (polls
   `GET /api/settings`); dismiss via `POST /api/settings/ack-unit-change` or
   restart via `POST /api/restart`.
4. **Cycle restore**: each engine's `restore_from_db`
   (`cycle_engine.py:2680+`) resumes any open cycle log rather than starting
   fresh — preserving which rooms already closed their vents, the original
   start timestamp (cycle-timeout clock keeps running), and vent
   expectations for reconciliation. It closes duplicate open logs, skips
   deleted rooms, discards a restored cycle whose mode contradicts current
   ambient (e.g. "heating" but ambient already above every target), and
   closes rooms already past target.
5. **Min-runtime-hold resume**: `CycleLog.in_min_runtime_hold` is persisted
   (`db.py:144`, #237), so a cycle that was being held open to satisfy
   `min_cycle_runtime_min` restores with the flag intact and the hold path
   (`cycle_engine.py:1134`) re-engages — per-room monitoring stays frozen and
   overflow conditioning continues instead of the hold restarting or being
   forgotten.

---

## Operational safety: post-install / post-upgrade checklist

Plenum assumes a conventional furnace/air-handler + AC compressor. **Heat
pumps are not supported** (README; `models.py` — no heating lockout exists
because of this). Do not point it at a heat pump.

Safety defaults, verified in `ThermostatConfig` (`smart_vent/backend/models.py:157-230`):

| Guard | Field | Default | State |
|---|---|---|---|
| Short-cycle: min runtime | `min_cycle_runtime_min` | 0 | **OFF** for newly registered thermostats* |
| Short-cycle: min off-time | `min_cycle_offtime_min` | 0 | **OFF** for newly registered thermostats* |
| Cooling lockout (outdoor temp) | `cooling_lockout_below_f` | `None` | **OFF** — also needs an outside-temp entity (`PUT /api/settings/outside-temp-entity`) |
| Force-reopen closed vents | `max_vent_closed_min` | 0 | **OFF** |
| Reconciliation of external changes | `reconciliation_interval_min` | 0 | **OFF** |
| Airflow floor (dead-head protection) | `min_open_vents_fraction` | 0.333 | ON once `total_vents_count` set (mandatory for new registrations; older ones show a banner); bypassed if `has_bypass_damper` |
| Unavailability abort | `unavailable_abort_after_min` | 5 min | ON (#267) |
| Overflow during hold | `overflow_during_min_runtime` | `true` | ON (#237) |
| Sensor staleness guard | `sensor_stale_after_min` (system setting) | 30 min | ON (#211) |

\* One-time migration `migration_short_cycle_defaults_v1` (`db.py:315-355`,
#208/#213 hardening wave) back-filled **10 min runtime / 5 min off-time** on
thermostats that existed before the feature shipped and were still at 0/0.
Thermostats registered *after* that migration get the raw 0/0 defaults —
**after adding a thermostat, explicitly set short-cycle protection and
(if you have AC) the cooling lockout.** Field semantics: `plenum-config-and-flags`;
never weaken these to fix comfort (`plenum-change-control`).

After any upgrade also check: unit-change banner (ack it deliberately), that
the MCP port is still mapped if you use it (#387 above), and download a fresh
backup.

### Vacation mode operation

No dedicated docs page; behavior verified in code + `docs/overflow-conditioning.md`.

- **Enable**: UI top-level vacation control (`VacationModeModal` in
  `frontend/src/App.tsx`) or `POST /api/settings/vacation-mode` with
  `{"return_at": "<future ISO-8601 UTC>"}` (required, must be in the future;
  routes.py:1924). **Disable**: `DELETE /api/settings/vacation-mode` or the
  banner's "End vacation mode early". State: `GET /api/settings/vacation-mode`.
- While active: all schedules, presence triggers, and overrides are ignored;
  any running cycle is aborted; each tick applies the per-thermostat hold
  strategy `vacation_hvac_mode` (`cycle_engine.py:_apply_vacation_hold`):
  - `"range"` — thermostat put in heat_cool/auto with low=`min_setpoint`,
    high=`max_setpoint`, re-asserted every tick. Test a thermostat's auto
    support first via `POST /api/thermostats/{id}/test-vacation` (and DELETE
    to revert).
  - `"single"` (default) — thermostat off; heats to `min_setpoint` when below
    it, cools to `max_setpoint` when above it.
- Overflow conditioning is disabled during vacation regardless of settings
  (`docs/overflow-conditioning.md`).
- Ends automatically at `return_at` (checked periodically,
  `scheduler.py:_check_vacation_expiry`) — normal scheduling resumes and the
  comfort envelope is *not* enforced outside vacation (#367/#368, see
  `plenum-failure-archaeology`).

---

## Provenance and maintenance

All facts verified 2026-07-04 against v0.22.1 (`smart_vent/config.yaml` `version`).
Known doc drift at that date (repo files win): README Option B `docker run`
lacks `-e DATA_DIR=/data` and uses `-e TZ` (clobbered by `run.sh:81`);
README/`docs/backup-restore.md` still describe the pre-`/config` HAOS data
location; README's `/tmp/flair-dev` local default is stale (`.env.sample`
says `./data`); `docs/system-modes.md` (~line 26) states the System-Off /
Dev-Mode precedence backwards — the engine gate is `system_enabled OR
dev_mode` (`scheduler.py:477`; see §System On/Off).

Re-verify volatile facts:
- Ports/defaults: `grep -n "PORT\|MCP_PORT\|DATA_DIR" smart_vent/backend/main.py` (8099/9099/`/data` code defaults) and `grep -n "8099\|9099" smart_vent/config.yaml` (both `null`).
- DATA_DIR container default + /data→/config copy: `grep -n "DATA_DIR" smart_vent/run.sh` (`/config`, lines ~82, 89-107).
- Config-tab options: `sed -n '24,46p' smart_vent/config.yaml`.
- Backup/restore/restart/MCP endpoints: `grep -n '"/api/backup\|/api/restore\|/api/restart\|/api/system/mcp\|vacation-mode"' smart_vent/backend/api/routes.py`.
- MCP 503 gate: `grep -n 503 smart_vent/backend/mcp_http.py`.
- Safety defaults: `sed -n '157,230p' smart_vent/backend/models.py`; short-cycle back-fill `grep -n RECOMMENDED_MIN smart_vent/backend/db.py` (10/5).
- Staleness default: `grep -n "SENSOR_STALE_AFTER_MIN" smart_vent/backend/engine/cycle_engine.py` (30.0).
- system_enabled default ON: `sed -n '56p;88p' smart_vent/backend/scheduler.py`.
- Min-runtime-hold persistence: `grep -n in_min_runtime_hold smart_vent/backend/db.py`.
- #387 revert: `git show c65d35d --stat`.
