---
name: plenum-failure-archaeology
description: >-
  The chronicle of every settled battle in Plenum
  (smart-thermostat-with-vents) — major investigations, dead ends, rejected
  approaches, and reverts, each as symptom → root cause → evidence → fix →
  status. Load when you suspect a bug was seen before, when someone proposes
  re-fixing or weakening something (deadband in at-target, frontend toStorage
  on payloads, default-mapped MCP port, per-selector screenshot masks), when
  you need the incident behind a guard/test name, or before filing an issue
  that might duplicate #26–#369 history. NOT for live triage of a new symptom
  (use plenum-debugging-playbook) or for the invariant statements themselves
  (plenum-architecture-contract).
---

# Plenum Failure Archaeology

Every major battle this project has fought, organized by campaign,
chronological within each. Purpose: **nobody re-fights a settled war, and
nobody "fixes" something that is a deliberate decision.** Each entry is
symptom → root cause → evidence → fix → status, with the guard or test that
now pins the behavior named exactly as it exists in the repo (verified
2026-07-04, v0.22.1).

**When NOT to use this skill:**
- You have a live symptom to diagnose *now* → `plenum-debugging-playbook`.
- You want the invariant/contract itself (°F storage, write boundary) → `plenum-architecture-contract` (the #231 contract is owned there; only the incident story lives here).
- You are about to make a change and need the gates → `plenum-change-control`.
- You need the HVAC physics behind a guard (short-cycling, dead-heading, delta math) → `hvac-zoning-reference`.

**Reading a status tag:**
- **Settled — do not reopen**: root-caused, fixed, and pinned by a named guard/test. Re-proposing the rejected alternative is a regression, not an idea.
- **Partially addressed — see #N**: the acute bug is fixed; a broader follow-up is tracked in an open issue.

Jargon used once: *golden* = committed reference screenshot for visual
regression; *hold* = the min-runtime phase where a finished cycle is kept
running to satisfy `min_cycle_runtime_min`; *write boundary* = the backend
POST/PUT/PATCH handlers where display-unit temps become °F.

---

## Campaign 1 — Early engine correctness era (v0.4–v0.7, issues #26–#86)

The engine's first months in a real house. Theme: control decisions derived
from *live HA state at the wrong moment* instead of from persisted intent.

**#26 — vents never closed on `heat_cool` thermostats.** Symptom: 2–3 h
cycles, zero `close_cover` calls, rooms blowing past target. Root cause:
`_read_hvac_mode()` fell back to `"heating"` when `hvac_action="idle"` — and
the HVAC goes idle *exactly* when the room reaches target, so the at-target
comparison inverted at the only moment it mattered. Fix: cycle direction is
locked at cycle start (`_cycle_mode`), never re-read mid-cycle from
`hvac_action`. Same issue: UTC timestamps rendered as local (frontend now
appends `"Z"` and converts). Settled — do not reopen.

**#29 — heat/cool oscillation at cycle boundaries.** After #26's fix made
cycles terminate, each *new* cycle picked its direction from live
`hvac_action`, which reflects post-termination drift → cool cycle, heat
cycle, cool cycle, forever. Fix: `_infer_mode_from_room_temps()` — direction
comes from room temps vs targets; all-rooms-in-deadband returns `"off"` (no
cycle). Settled — never derive cycle direction from `hvac_action`.

**#32 — full engine audit #1.** Decisions that still stand: every vent
decision writes to the event log regardless of dev mode; setpoint calls pass
`hvac_mode` explicitly (the `heat_cool` inference path was eliminated);
cycles end by setting setpoint = ambient in the cycle's own mode;
`min_setpoint`/`max_setpoint` were **repurposed** from setpoint-clamps into
the comfort-envelope bounds (clamping ambient resets had prevented HVAC from
stopping). Settled.

**#38 — engine heated a 74 °F room toward a 72 °F target.** Root causes:
rooms with unavailable sensors were silently skipped from the mode vote, and
nothing cross-checked the vote against the thermostat's own ambient. Fix:
thermostat-ambient fallback when room sensors are unreadable + an ambient
sanity check on the inferred mode. Settled.

**#48 — full engine audit #2.** Three fixes: (1) setpoint anchored to
thermostat ambient (`min(setpoint, ambient − overshoot)` for cooling) so a
hallway probe cooler than the bedrooms can't make the HVAC think it's
already satisfied; (2) explicit mode guard — unexpected `hvac_mode` values
log an error instead of silently defaulting to heat; (3) mixed heat/cool
votes no longer drag losing-side rooms into the wrong cycle
(`_filter_rooms_for_mode`). Settled.

**#51 — cycles stuck "Active" after system stop.** `_abort_cycle` cleared
in-memory state even when `db.close_cycle_log` failed, orphaning
`ended_at IS NULL` rows. Fix: close-ordering hardened + safety nets
(`close_open_cycle_logs` at cycle start, on restore, and in the scheduler's
`_reset_and_reevaluate`). Settled.

**#54 — dev-mode cycles lingered ACTIVE across toggles.** Only
`set_system_enabled(False)` cleaned up; the other three system/dev
transitions did nothing (engine gate is `system_enabled OR dev_mode`, so a
fast dev-off→system-on flip was never observed disabled). Fix: **all four**
transitions run `Scheduler._reset_and_reevaluate()` (force-abort every
engine, close orphans, immediate re-tick) — see `scheduler.py` calls at the
enabled/dev-mode setters. Related: #35 made system-off abort immediate
rather than waiting ≤60 s for the next tick. Settled.

**#67 — idle-room vents left open when a new cycle starts.**
`_terminate_cycle` re-opens ALL zone vents (correct idle state); the next
cycle only opened active-room vents and never closed the rest. Fix:
`_close_idle_room_vents()` at fresh cycle start; pinned by
`tests/integration/test_idle_room_vents_closed_on_cycle_start.py` and
`test_idle_vents_closed_on_restore.py`. Settled.

**#70 — cycles ended at the deadband boundary (72 °F target "reached" at
73 °F).** Deliberate design decision, not a bug fix: `_is_at_target` is a
**pure target comparison — no deadband**. Deadband gates cycle *start/join*
only. Do not "optimize" by re-adding deadband to the completion check; that
re-introduces #70. Settled — do not reopen.

**#82 — "Closed idle room X vents" logged, vents physically still open.**
Two call sites bypassed the `control_method` dispatcher and called
`close_cover` directly — a no-op for `set_position`/`set_tilt_position`/
`toggle` vents (Flair's common config, introduced by #57). Fix: all close
paths route through `VentController._invoke_close`; reconciler also watches
idle-room vents during RUNNING. Pinned by
`tests/integration/test_idle_vent_close_dispatch.py` and
`test_vent_types.py`. Settled — never call `ha.close_cover` directly.

**#86 — cycle never terminated after all rooms reached target.** After a
vent closes, the room drifts back past target; the `elif not at_target`
branch in `_monitor_rooms` had no `vent_closed_at` guard, so post-closure
drift blocked `all_at_target` forever (48 min of all-vents-closed runtime
observed). Fix: a room whose vent is already closed is "served" — its drift
cannot block termination (`vent_closed_at is None` guards throughout
`_monitor_rooms`). Settled.

Also from this era: **#65** — `datetime.now()` (local) in `room_manager`
mixed with `utcnow()` everywhere else shifted presence-holdover display;
everything is naive-UTC now. Settled.

---

## Campaign 2 — The unit-system saga (#123 → #231 → parity system)

The project's costliest bug class. The invariant that resulted (°F-only
storage, conversion at exactly one boundary) is stated in
`plenum-architecture-contract`; this is how it was learned.

**#123 — the feature.** All temps stored °F at 2 dp (no migration); unit
priority: `TEMPERATURE_UNIT` env → HA `/api/config` → last-known DB value;
unit changes require restart + `UnitChangeBanner` ack. Settled design.

**#231 — the double conversion (landmark).** Symptom: typing `16` °C for
`min_setpoint` stored **141.44 °F** (16→60.8→141.44). Root cause: frontend
called `toStorage()` on the outgoing payload AND the backend's `_to_f()`
converted again. Both sides' tests passed because each asserted its own
(contradictory) contract — the frontend test expected a pre-converted body,
the backend test expected a display-unit body. Fix: conversion belongs to
the backend only; frontend forms hold display units and POST them raw.
Guards created: the 3-file `TEMPERATURE_FIELDS` parity system
(`backend/api/routes.py` dict ↔ `e2e/tests/temperature-fields.ts` ↔
`// @covers:` tags in `e2e/tests/temperature-units.spec.ts`) enforced by
`backend/tests/test_temperature_field_parity.py`, plus the dual-unit (F/C)
round-trip E2E matrix — the only test that would have caught it. Settled —
do not reopen; never call `toStorage`/`toStorageDelta` on an outgoing
payload.

**The °C sibling bugs (pre-launch audit, all HIGH/MEDIUM):**
- **#280** — the *engine* read climate-entity temps raw and sent °F
  setpoints to metric HA (which reads them as °C → clamps to ~35 °C =
  runaway heating hazard). Tests masked it by mocking pre-converted values.
  Fix: `HAClient._setpoint_to_native()` converts outgoing setpoints/ranges;
  climate reads normalized like sensors (`ha_client.py`, comments cite
  "Issue #280"). Settled.
- **#281** — auto-detect could never return Celsius: HA returns
  `unit_system` as an *object* (`{"temperature": "°C", ...}`), the code
  compared `== "metric"`; AND `run.sh` defaulted blank to `"F"` which the
  scheduler treats as a hard override lock. Both fixed
  (`ha_client.py` reads `unit_system["temperature"]`; `run.sh` defaults
  blank → auto). Unit-test fixtures had mocked the wrong shape — lesson:
  mock the real payload shape. Settled.
- **#284** — MCP write tools bypassed the write-boundary conversion entirely
  ("set the bedroom to 21" in a °C home stored 21 °F ≈ −6 °C). Fix: MCP
  tools convert via the shared helpers (`mcp_tools/_units.py`,
  `units.to_f`). Settled.
- **#288** — banner dismiss was undone every 60 s tick; and the banner stuck
  after the restart that applied the change. Fix: `ack_unit_change` records
  the acknowledged HA unit; startup clears the flag when units match.
  Settled.
- **#291/#292** — charts applied the **absolute** °F→°C conversion to
  **deltas**: a 2 °F overshoot rendered "-16.7 °C"; degree-minutes were
  mislabeled ×1.8. This is CLAUDE.md pitfall #4 exactly (absolute conversion
  subtracts 32; deltas must not). Fixed in commit `e3a9473`. Settled.
- **#293** — `buildUnitContext(unit)` rebuilt every render → new function
  identities → form-reset effects fired on unrelated re-renders, discarding
  edits and even regressing saved values. Fix:
  `useMemo(() => buildUnitContext(unit), [unit])` (`App.tsx`). Settled **for
  that trigger only** — see #597, which closed the rest of the family. Do not
  read this entry as "prop-derived form resets are solved".
- **#597** — the same clobber through the other two triggers the #293 memo
  never touched. (a) `ThermostatCard`'s reset effect keyed on `config`
  **identity**, and `load()` (a delete, a registration, an Eco suspend) hands
  every surviving card a brand-new object with byte-identical content — so
  editing card A and then removing card B silently discarded A's edit.
  (b) The reset lived in a **passive effect**: React commits the DOM and then
  schedules passive effects as a separate host task, so the reset could flush
  *after* an input's `onChange` had queued its update, and a plain-object
  `setForm` replaces a queued functional update. That is what made
  `Thermostats.test.tsx` flaky — 8 of 400 isolated edit-then-save exposures,
  every failure having the mount effect still pending when the query resolved.
  Fix: re-derive on derived **content**, **during render**, with a
  `lastDerivedForm` state cell + `sameDerivedForm` (`Object.is`, union of
  keys, `FORM_UNOWNED_FIELDS` excluding the read-only `eco_suspend_until`).
  Beware the near-misses: comparing derived-vs-**current form** does not fix
  it (the user's edit is *why* they differ), and skipping only the effect's
  first run still clobbers under `<StrictMode>`. Settled.
- **#600** — the *other* edit-clobber shape, filed out of the #597 sweep and
  deliberately not the same mechanism: a **mount fetch racing typing**, with no
  prop and no recurrence. `SensorStalenessCard` rendered its editable input
  immediately from `useState<number | null>(null)` and filled it when
  `getSensorStaleness()` resolved, so anything typed first was replaced. The
  window opened at the exact moment the page-level gate cleared and the page
  looked interactive. Two consequences beyond the lost keystrokes: the
  `.catch` arm fabricated a hardcoded `30` with **nothing surfaced**, so an
  operator running a non-default threshold saw `30` and one Save reset the
  engine's #211 staleness guard to it; and `save()`'s `if (value === null)
  return` made a Save inside the window a completely silent no-op (0 coverage
  hits — a guard whose only job was papering over the race). Fix: the loading
  gate CLAUDE.md pitfall #9 already prescribed — seed the state with the
  backend's own default (never `null`), a separate `loading` cell starting
  `true`, `.then` sets the value **and** clears the gate while `.catch` clears
  the gate and posts a warning that the shown `30` is a default, and no input
  exists until the gate clears. `RetentionSettings` (`Logs.tsx`) was the
  precedent **for the loading gate only** — it had none of the `.catch` half
  at the time, which is the bug #605 then filed and fixed. Two near-misses
  the issue measured so nobody re-derives them: a `useState` dirty flag does
  **not** work (the `[]`-deps effect closes over `dirty === false`, so the
  value is still clobbered — it needs a ref), and a
  "render, type, assert" test is **non-discriminating** because the mocked
  promise may resolve before the typing; the test has to own the resolver.
  Settled.

- **#605** — the `.catch` half of #600's pattern, missing in the very
  component #600's entry (and CLAUDE.md pitfall #9) cited as its precedent.
  `RetentionSettings` (`Logs.tsx`, the Logs page's Retention tab) had the
  loading gate right — seeded with the backend's own `7`/`30`, a separate
  `loading` cell, no input until it cleared — but its `.catch` cleared the
  gate and **recorded nothing**, so a form showing 7/30 after a failed
  `GET /api/settings/log-retention` was pixel-identical to one showing a
  genuinely-default install. Save stayed live and `save()` POSTs the whole
  `form`, so one click wrote 7/30 over a configured 90/365 — and
  `Scheduler._purge_old_logs` is a hard `DELETE` that runs on a 24 h interval
  and again on every startup, so the event logs and cycle history outside the
  new window are gone with no undo (and the operator gets a green "Saved!").
  Editing only one field still shipped the other's fabricated default. Blast
  radius is history and diagnostics only — nothing here reaches HVAC control,
  which is why this was Medium where its #600 sibling (which could reset the
  #211 staleness guard) was higher. Fix: the sibling's shape, not a second
  one — a dedicated `loadFailed` cell set in `.catch`, the `cancelled`
  cleanup flag, and a warning naming both fabricated numbers. Settled
  decisions, do not re-fight: `loadFailed` is deliberately **not** the
  component's `error` cell (`save()` clears that one, and the warning must
  survive the Save it exists to warn about); **only a successful write**
  clears it (a rejected Save leaves the numbers fabricated); and Save is
  deliberately **warn-only** rather than blocked or confirm-gated, matching
  `SensorStalenessCard` — the issue left that judgement to the implementer
  and the test asserts the whole-form POST body rather than pretending the
  write was blocked. Guards: `Logs.coverage.test.tsx` pins the warning
  sentence as one text node (copy duplicated in the test so a reword is a
  deliberate two-file change), its survival across a failed Save, its
  disappearance after a good one, the POST body, and that the inputs lock
  while the write is in flight. One near-miss worth keeping: an
  **unmount-ordering** test does not pin the `.catch` cancellation guard —
  after unmount both setStates are no-ops either way, so it passes with the
  guard deleted (#602's "tests that cannot fail" class). The ordering that
  discriminates is `<StrictMode>`'s cancelled first GET **rejecting after**
  the live second GET landed a real 90/365: unguarded, the page then paints
  "the numbers below started as the 7-day … defaults" over correctly-loaded
  values. Considered and deliberately not taken: POSTing only the fields the
  operator actually edited while `loadFailed` is set (both `setLogRetention`'s
  type and `set_log_retention` in `routes.py` accept a partial body). It would
  soften the collateral clobber, but it invents a second pattern the sibling
  does not have, makes the warning's "Saving overwrites both fields" false,
  and is a behaviour change #605 did not sanction — file it as its own issue
  if the whole-form write ever bites someone. Settled.

**Consolidation (#251):** the four helpers moved from `routes.py` into
`smart_vent/backend/units.py` (`to_f`, `delta_to_f`, `from_f`,
`from_f_delta`), also used by HA ingest and MCP. (CLAUDE.md said `routes.py`
until corrected on 2026-07-05 — trust `units.py`.)

**Sentinel lesson (`.jules/sentinel.md`, 2026-05):** setpoint bounds
validation must run on the **normalized °F value** (40–90 °F), not the raw
display input — bounds on an unknown-unit raw value are meaningless. Pinned
by `tests/integration/test_sentinel_validation.py`. Settled.

---

## Campaign 3 — Safety hardening wave (#208–#213, #267, #367)

A deliberate pre-launch review: "which protective interlocks are missing or
untested?" Every guard below is a **one-way ratchet** — never weaken one to
fix a comfort complaint (inferred house rule; consistent with every fix in
this campaign).

**#208 — no short-cycle protection.** Grep for `min_runtime|lockout|compressor`
returned zero hits; cycles could start/stop within one tick. Fix:
`ThermostatConfig.min_cycle_runtime_min` / `min_cycle_offtime_min` — hold a
finished cycle open until minimum runtime; refuse restarts during off-time
lockout. Pinned by `tests/integration/test_short_cycle_protection.py`.
Follow-up **#215**: the "trigger changed" teardown tore down a healthy cycle
for a same-direction target change (68→72 mid-heat = full furnace
stop/start, then the new lockout blocked reheat). Fix: **update in place**
when direction is unchanged; teardown reserved for direction flips (see the
`#215 in-place update` comments in `cycle_engine.py`, pinned by
`test_trigger_change_characterization.py`). Settled.

**#209 — AC could run in cold weather.** Outdoor temp was plumbed but used
only for diagnostics; a mixed-vote tie ("ties go to cooling") could start a
compressor at 40 °F outside (coil icing / slugging). Fix:
`cooling_lockout_below_f` (nullable), enforced at mode-inference; pinned by
`tests/integration/test_outdoor_cooling_lockout.py`. Settled.

**#210 — the last-vent bypass could dead-head the air handler.** The
deliberate `min_open_vents` bypass for the final closing vent must always be
paired with immediate termination + reopen. Fix/pin:
`min_open_vents_fraction` (default 0.333 of zone vents) and
`tests/integration/test_airflow_floor_bypass.py` (ordered close→reopen
assertions). Settled.

**#211 — stale sensors drove control.** Battery Zigbee sensors drop off the
mesh but keep their last numeric state; a 3-h-old "70 °F" closed vents on a
80 °F room. Fix: `get_numeric_state` freshness check against
`last_updated` (missing timestamp = infinitely stale), stale-sensor warning
events + `StaleSensorsBanner`; pinned by
`tests/integration/test_sensor_staleness.py`. Settled.

**#212 — the cycle timeout had no end-to-end test.** "A protective
interlock with no test is one that can silently regress." Fixed with
`tests/integration/test_cycle_timeout.py` (over-timeout terminates with
`ended_reason="timeout"`, under-timeout does not). Settled.

**#213 — unsafe defaults.** `max_vent_closed_min=0` and
`reconciliation_interval_min=0` shipped OFF. Principle adopted: *defaults
must be thermodynamically sound; don't rely on the user*. As of this
writing (re-verified 2026-09-01, v0.35.0) both fields default back to `0`
(off) in `models.py`, documented there as deliberate — `max_vent_closed_min`
"0 = no limit (feature off by default)", `reconciliation_interval_min`
"0 = disabled" — see `plenum-config-and-flags` for the current default
catalog and guards. Settled.

**#267 — unavailable thermostat suspended ALL safety monitoring.**
`_do_tick` returned early on unavailability, skipping timeout, the
closed-vent watchdog, and reconciliation — HVAC running, vents closed,
forever. The docstring *claimed* an unavailability abort existed; the code
path didn't (`_abort_cycle(safe_close=True)` was dead code). Fix: tolerate
transient blips, abort after `unavailable_abort_after_min` (default 5)
consecutive unavailable ticks, reopen vents (cover entities are independent
of the climate entity). Pinned by
`tests/integration/test_thermostat_health.py`. Settled.

**#367/#368 — comfort envelope not enforced with zero active rooms
(production incident, 2026-06-28).** After a vacation hold expired with no
room demand, the upstairs zone sat at 81 °F against a 77 °F `max_setpoint`
for 2+ hours: the cap lived only in `_suppression_vote()`, reachable only
from the active-room path, and `_do_tick` exits early on
`active_rooms=0`. Fix is two-layer: `_add_safety_rooms` pulls any
sensor-breaching room into the cycle with `source="safety"` (target one
deadband inside the bound, for hysteresis), plus a thermostat-ambient
backstop for zones with no readable room sensor. Pinned by
`tests/integration/test_safety_room_protection.py` and
`test_safety_setpoint_backstop.py`. Settled.

---

## Campaign 4 — Min-runtime hold / overflow conditioning arc (#237 → #305)

**#237 — vent thrashing during the hold (+ the overflow idea).** When rooms
hit target before minimum runtime, `_enter_min_runtime_hold()` re-opened
vents once — but the next `_monitor_rooms` tick had no idea a hold was in
progress and closed them again: open→close→open against a running
compressor. Fix: the hold became **persisted state**
(`CycleLog.in_min_runtime_hold`, `models.py`) that gates the close loop.
Same issue birthed **overflow conditioning**: surplus runtime is dumped into
non-active rooms via three candidate tiers — full design in
`docs/overflow-conditioning.md` (the doc of record; don't re-derive tiers
from code). Settled.

**#254 — overflow rooms were invisible in cycle history.** Their temps were
buried in vent-event reason strings. Fix: `room_cycle_states.role`
discriminator (`'active'` | `'overflow'`; note `'safety'` is a *source* on
`ActiveRoom`, not a role — `cycle_engine.py:3013`), one collapsed
row per overflow room, surfaced in the Logs cycle detail. Settled.

**#268 — the anti-thrash gate had zero real coverage (test-quality
lesson).** `test_hold_gate_skips_close_loop` passed via the wrong branch —
its setup triggered hold-*entry*, so the continuation gate never executed;
coverage was green while the #237 guard was unguarded. Fix: a test where a
room drifts off target *during* the hold. Lesson: **verify which branch a
safety test actually executes** (coverage line data, not test names).
Companion #269 covered the untested in-tick system-disabled/vacation abort
backstops. Settled.

**#300 — ON CONFLICT dropped `role`/`joined_at`.** An overflow room promoted
to active silently kept `role='overflow'` (excluded from metrics, mislabeled
in UI); the restore path leaked `_overflow_room_states` into the next cycle.
Fix: upsert sets `role=excluded.role`,
`joined_at=COALESCE(existing, excluded)` (`db.py`); pinned by
`tests/test_room_cycle_state_upsert.py`. Settled.

**#305 — overflow candidate selection ignored `deadband_override`** (the
per-room deadband from #277). Fix: `_effective_deadband(room, tc.deadband)`
applied inside the candidate loop (`room_manager.py`). Settled.

---

## Campaign 5 — The pre-launch audit wave (#280–#305, ~25 latent bugs)

One systematic front-end + back-end logic audit (2026-06) filed ~25 issues in
a week; all are fixed. The °C ones (#280/#281/#284/#288/#291/#292/#293) are
in Campaign 2; #300/#305 in Campaign 4. The rest, one line each — all
**settled** unless noted:

| # | Symptom → fix (guard) |
|---|---|
| #282 | MCP server crashed at startup: `@server.tool()` doesn't exist on the low-level `mcp.server.Server`; coverage exclusions meant no test ever imported it → rewritten on `FastMCP` (`mcp_server.py`), smoke-tested by `tests/test_mcp_server.py`. (Post-mcp-SDK-v2-migration, this class is `MCPServer` — v2's rename of `FastMCP` — not a further rewrite of the #282 fix.) Lesson: a coverage-excluded module is an untested module. |
| #283 | `connectWS` reconnected after intentional close → zombie sockets, N+1 duplicated feed handlers → liveness flag in the closure (`api.ts`, `let closed = false`). |
| #285 | Deleting a thermostat's last room dropped the engine mid-cycle, leaving HVAC at the overshoot setpoint forever → `force_abort(reason="thermostat removed")` + close logs before `del` (`scheduler.py`). |
| #286 | `_tick_engine` pre-tick DB calls sat outside the try; a locked DB silently killed a zone with zero log output → whole body inside try. |
| #287 | Presence was edge-triggered only; mmWave sensors holding continuous `on` lost their holdover and the occupied room went idle → holdover refreshed from current sensor state each tick (`tests/integration/test_presence_holdover_refresh.py`). |
| #289 | Per-room time-to-target averaged cycles outside the date range (filter in `LEFT JOIN ON`, CASE didn't gate on join success) → `cl.id IS NOT NULL` gate (`db.py`). |
| #290 | Overshoot histogram mixed thermostat-level samples into every room's extremes → per-(cycle,room) fallback only when no room-level samples. |
| #294 | Vacation modal `datetime-local` min computed in UTC blocked valid times west of UTC → local-component string via `toDatetimeLocalString` (`VacationModeModal.tsx`). |
| #295 | Several numeric config fields stored unvalidated (a string `cycle_timeout_hours` then crashed every tick — silently, per #286) → `_validate_thermostat_numeric()` in `routes.py`. Note: `@request_schema` decorators remain documentation-only by design; handlers self-validate. |
| #296 | Idle in-deadband engine re-sent setpoint=ambient every 60 s (cloud-thermostat rate-limit hazard) → skip within `_SETPOINT_DRIFT_TOLERANCE_F` (`tests/integration/test_idle_setpoint_churn.py`). |
| #297 | HA WS client busy-looped on clean close and could hang forever in an unguarded handshake → min reconnect delay, `heartbeat=`, `wait_for` on handshake (`ha_client.py`). |
| #298 | `/api/backup` leaked a full DB temp copy on every download (disk + data exposure) → temp file unlinked on the success path too (`routes.py`). |
| #299 | `event_log` trim off-by-one deleted the oldest row even under the cap (`id <=` → `id <`), pinned by `tests/test_event_log_trim.py`. |
| #301 | Vent timeline printed naive-UTC verbatim → localized (commit `6a81447`). |
| #302 | Offset pagination duplicated feed rows as new events arrived → de-dupe by seen ids on merge (`Logs.tsx`). |
| #303 | Dev-mode "Clear" repopulated 3 s later → cleared-before-id watermark (`DevMode.tsx`). |
| #304 | Fire-and-forget startup tasks held only by weak refs (GC could eat unit resolution) → `Scheduler._bg_tasks` strong-ref set with done-callback discard. |

Meta-lesson from this wave: the audit found bugs precisely where tests
mocked the convenient shape (#280, #281, #282) or where coverage was
excluded/green-but-wrong (#268, #282). When auditing, start from "what does
the real payload/state look like."

---

## Campaign 6 — E2E / visual-regression campaign (#175 → #182 → #369)

**#175** introduced Playwright + docker-compose visual regression with
goldens committed to git. It promptly proved **flaky beyond rescue by
masking** (**#182**): live temps, countdowns, engine feeds, and Recharts
float jitter differed between the update pass and the verify pass;
per-selector masks were whack-a-mole. The suite was **disabled** (PR #180)
for months — the dead end to remember. The rescue (these became the project's
hard E2E rules, since restated in CLAUDE.md and superseded in mechanism by
#366 below):

- **Determinism by construction**: `isCI` (`VITE_APP_VERSION === "CI"`) +
  `<Frozen>` in `frontend/src/ci.tsx` — freeze anything that changes between
  two renders of the same fixture; never freeze static fixture values. One
  `import.meta.env` branch, tested both ways in `ci.test.tsx`.
- **The 158 °F golden bug**: HA YAML with no `unit_system` defaults to
  metric, so a `target_temp: 70` fixture was 70 °C and Plenum's *correct*
  normalization displayed 158 °F. Fix: the HA fixture pins
  `unit_system: us_customary`; the matrix varies only Plenum's display unit.
  Dual-unit goldens (`-Fahrenheit-`/`-Celsius-` filenames) catch conversion
  regressions in both directions.
- Golden auto-commit *needed* `max-parallel: 1` (legs pushed to the same ref)
  and a forced checkout — until #366 replaced that design with parallel legs
  + a single fan-in commit job; high-DPI mobile needs a per-spec
  `maxDiffPixels` bump (metrics: 800) instead of masks.

**#329** — a *distinct* flake: `global-setup.ts` room-creation clicks raced
a list re-render ("element detached from the DOM"), reddening unrelated PRs
(#315/#324/#326). Fixed alongside the #330-wave CI work (commit `fbe9801`).
Lesson: not every red E2E is #182 — check *where* it failed first.

**#366** moved the visual-regression suite **into
`container-ci.yml`** (parallel goldens-F/goldens-C legs + a `commit-goldens`
fan-in job) and deleted `e2e.yml`. (CLAUDE.md referenced the standalone
`e2e.yml` until corrected on 2026-07-05 — the suite lives in `container-ci.yml`.)

**#369** — the fan-in job couldn't push regenerated goldens:
`actions/checkout` leaves detached HEAD, so `git push origin "$BRANCH"` had
no local refspec after the rebase. Fix: `git push origin HEAD:"$BRANCH"`
(commit `c26d395`). Settled — if golden commits fail with "src refspec …
does not match any", this is it; don't re-invent the checkout dance.

---

## Campaign 7 — CI build-once consolidation (#5, #43, #141, #330–#337)

**#5** (early): PR builds and GHCR pushes split into separate jobs so PR
runs have no `packages: write` at all — structural enforcement over a
runtime `if:`. Settled principle: permissions, not conditionals.

**#43**: release-PR contributor lookup leaked a GitHub 422 JSON error body
verbatim into CHANGELOG/PR bodies — a three-bug chain (`+`/`[` in bot
emails; `gh api` writes 4xx to *stdout*; `$(A || B)` captures A's stdout
anyway). Fix in `release-pr.yml`: skip bot emails, gate on exit code with
`if RAW=$(...)`, `jq '// empty'`. Settled — reuse that exact pattern for any
`gh api` in a workflow.

**#141**: nine releases in one day (v0.9.4–v0.9.12) from a
merge→break→hotfix-direct-to-main loop. Outcome: branch protection,
`/api/healthz` + real smoke test, single-source deps in `pyproject.toml`,
`RELEASE.md`. (The `validate-release.yml` manual dry-run workflow was also
added at the time, but was removed 2026-07 once `container-ci.yml` grew its
own required, automatic checks on release PRs — everything the dry-run caught
was already caught there.) Settled process (details owned by
`plenum-ci-and-release`).

**#330–#333 → #334**: the same image was built ~4× per PR (one under QEMU
arm64) with no shared cache. Consolidated into `container-ci.yml`'s single
`build` job pushing `ghcr.io/<repo>:ci-<sha>`, reused by smoke, round-trip
F/C, and (post-#366) the visual legs. **#337** follow-ups: fork-PR artifact
handoff (`docker save`/`load` — fork tokens can't push), nightly `ci-*` tag
pruning (`.github/workflows/ci-image-cleanup.yml`), and release PRs building
the real `:<version>` once in container-ci (required check is now **Build
(PR validation)**, not the old docker.yml job). Settled.

---

## Campaign 8 — Infra & security

**Security alert #4 — CWE-209 exception detail in API responses.** Route
handlers embedded `str(exc)` in error bodies. Rule (now in CLAUDE.md, and
the top of every reviewer's checklist): `log.exception("…context…")` then a
generic `error("Failed to <action>", status=5xx)` — never `{exc}` in a
response. `tests/integration/test_security.py` exists; grep before merging
any new `except` in `routes.py`. Settled — this is a hard rule, not a style
preference.

**#298 / #295** (see Campaign 5 table) were the other security-adjacent
audit finds: backup temp-file data left on disk, and unvalidated numeric
config inputs.

**MCP (#372/#375, shipped v0.22.0):** MCP now runs over HTTP on port 9099
behind a settings toggle (503 until enabled). Its earlier failures (#282
startup crash, #284 unit bypass) are settled above. Authentication for the
web UI and MCP endpoint was the project's next open problem after this —
**#373 has since shipped** (plus OIDC single sign-on, #464); see
`plenum-auth-campaign` for the design history and `docs/auth.md` for current
behavior. Settled.

---

## Campaign 9 — Notable reverts

**Commit `c65d35d` — "Revert MCP port to null" (#387, reverting #385,
2026-07-01).** #385 had default-mapped `9099/tcp: 9099` in `config.yaml`
because the port row wasn't appearing in existing installs' HA
**Configuration → Network** section. The revert's finding: the row was
missing only because **HA Supervisor re-reads an add-on's `config.yaml`
(including ports) when the version changes on Update** — no default mapping
needed; a `null` mapping still shows an empty, fillable row after the next
version bump. So the port is back to `null` (unpublished by default) and
`docs/mcp.md` documents the update-to-see-the-row behavior. At the time,
the MCP endpoint was unauthenticated (#373 was still open), so not
auto-publishing it was also the safer default (that rationale is inferred;
the documented one is the Supervisor behavior) — #373 has since shipped, but
the port-mapping decision itself doesn't depend on that. **Settled — do not
re-add a default host mapping for 9099/tcp**; if a user reports "no
9099/tcp row", the answer is "update the add-on", not a config.yaml change.

Honorable mention: `min_setpoint`/`max_setpoint`'s original meaning
(overshoot-setpoint clamps) was deliberately **abandoned** in #32 and the
fields repurposed as the comfort envelope — any proposal to clamp the
commanded setpoint with them again is re-fighting #32.

---

## Campaign 10 — Platform mishaps (Home Assistant quirks)

**#74 — blank screen under HA ingress.** Ingress serves at
`/api/hassio_ingress/<TOKEN>/`; React Router had no `basename`, so no route
matched. `api.ts` already computed the ingress base — the router didn't.
Fix: same regex feeds `<BrowserRouter basename=…>` (`main.tsx`). Settled —
any new absolute frontend path must survive the ingress prefix.

**#92 — add-on data "missing" on HAOS.** Docs pointed at paths that don't
exist; the real bind mount is
`/mnt/data/supervisor/addons/data/<repo_id>_<slug>/`, invisible to the
Samba `addon_configs` share because the manifest only mapped `data`. Fix:
`config.yaml` adds `addon_config:rw` with `DATA_DIR=/config` (config.yaml:43,
run.sh:82) + a
one-shot `run.sh` migration; docs corrected with `docker inspect` recipes
(`docs/backup-restore.md`). Settled.

**#89 — rename to Plenum.** DB migrated `flair.db` → `app.db` via one-shot
startup rename; the **add-on slug stayed `flair_replacement` on purpose**
(changing it orphans every install's data dir) and the GitHub repo name
stayed. Settled — do not "clean up" the slug.

---

## Provenance and maintenance

Mined 2026-07-04 from the closed-issue corpus (48 bug + 58 feature bodies),
the git log, repo `CLAUDE.md`, and direct code verification at v0.22.1.
Guard/test names above were verified to exist on disk on that date. Volatile
facts and how to re-check them:

- Guard tests still present:
  `ls smart_vent/backend/tests/{test_temperature_field_parity.py,test_event_log_trim.py,test_room_cycle_state_upsert.py} smart_vent/backend/tests/integration/{test_short_cycle_protection.py,test_cycle_timeout.py,test_sensor_staleness.py,test_thermostat_health.py,test_safety_room_protection.py,test_safety_setpoint_backstop.py,test_airflow_floor_bypass.py,test_outdoor_cooling_lockout.py,test_presence_holdover_refresh.py,test_sentinel_validation.py,test_idle_setpoint_churn.py}`
- Conversion helpers live in `smart_vent/backend/units.py` (post-#251):
  `grep -n "def to_f" smart_vent/backend/units.py`
- Visual-regression E2E lives inside `.github/workflows/container-ci.yml`
  (post-#366; no `e2e.yml`): `ls .github/workflows/`
- MCP port default is `9099/tcp: null` (post-`c65d35d`):
  `grep -n "9099" smart_vent/config.yaml`
- #367 backstop still wired: `grep -n "_add_safety_rooms" smart_vent/backend/engine/cycle_engine.py`
- Open issues referenced as "partially addressed": #21
  (versioned DB migrations), #23, #25, #173 — re-check with
  `gh issue list --state open` before citing them as still open. (#373 auth
  shipped since the initial mining pass — re-verified 2026-09-01 against
  v0.35.0 — and is no longer listed here; see `plenum-auth-campaign`.)
- The local clone is shallow (~51 commits as of 2026-07-04); hashes cited
  (`c65d35d`, `c26d395`, `e3a9473`, `6a81447`, `fbe9801`, `bd560de`,
  `a0e9d04`) are within it; older incidents are cited by issue number only.
- Spot-checked 2026-09-01 against v0.35.0: guard tests in the list above
  still on disk, `9099/tcp: null` unchanged, `_add_safety_rooms` still
  wired. Three "current state" claims had drifted and were corrected in
  place: the Campaign 8 MCP paragraph (auth was open, now shipped), the
  #213 entry (`max_vent_closed_min`/`reconciliation_interval_min` default
  back to `0`/off in current `models.py`, not non-zero), and the #282 row
  (the class is now `MCPServer`, mcp SDK v2's rename of `FastMCP`).
