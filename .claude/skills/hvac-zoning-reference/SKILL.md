---
name: hvac-zoning-reference
description: >-
  Domain theory for Plenum: HVAC physics for programmers (compressor, plenum,
  short-cycling, dead-heading, coil icing), zoning control theory (setpoint,
  deadband, overshoot, hysteresis, opposite-cycle prevention tiers), absolute
  vs delta °F/°C math, and the Home Assistant entity model (climate hvac_action
  vs hvac_mode, cover control methods, sensor unit normalization, binary_sensor
  presence, ingress). Load when you need to understand WHY a guard/field exists,
  what a domain term means, why a conversion subtracts 32 (or must not), or how
  Plenum reads/commands HA entities. Do NOT load for project invariants or
  write-boundary contracts — that is plenum-architecture-contract.
---

# HVAC zoning reference — the domain theory behind Plenum

Audience: a mid-level software engineer with zero HVAC background working on
this repo. Everything here is verified against `docs/`, `smart_vent/backend/`,
and the issue history (as of 2026-07, v0.22.1).

**When NOT to use this skill:** project invariants, the #231 write-boundary
conversion contract, and "which layer converts" rules are owned by
**plenum-architecture-contract**. Config-field inventories and add-a-knob
checklists are owned by **plenum-config-and-flags**. Incident narratives are
owned by **plenum-failure-archaeology**. This skill explains the *physics and
theory* those rules encode.

---

## 1. HVAC basics for programmers

A **conventional forced-air system** — the only kind Plenum supports — has two
independent halves sharing one blower and one duct network:

- **Furnace** (gas/electric): makes heat. Cheap to start/stop; the dangerous
  component on the heating side is overheating from restricted airflow, not
  restart stress.
- **AC**: a **compressor** (outdoors) pumps refrigerant in a loop; the
  **evaporator coil** (indoors, in the airstream) absorbs heat; the blower —
  the **air handler** — pushes indoor air across the coil and through the
  ducts. The compressor is the expensive, fragile part: an electric motor
  pumping refrigerant mixed with lubricating oil, switched by a **contactor**
  (a heavy-duty relay).

Airflow path: air handler → **supply plenum** (the sheet-metal distribution
box the project is named after) → **ducts** → **registers** (the grilles in
each room, also called **vents**). Plenum-the-software motorizes the registers:
an HA `cover.*` entity per smart vent opens/closes each room's air supply, so
one thermostat's system conditions rooms selectively — that is **zoning**.

The **thermostat** is a dumb feedback controller: when ambient drifts past its
setpoint it issues a **call for heat** or **call for cool** (closes a relay
circuit), and the equipment runs until ambient crosses back. Plenum doesn't
replace the thermostat; it *manipulates* it — commanding the HA `climate.*`
entity's setpoint so the thermostat calls for heat/cool exactly when Plenum's
per-room logic wants conditioning, then restoring the setpoint to ambient so
the HVAC shuts off naturally (`docs/cycle-engine.md`).

**Why heat pumps are excluded** (`docs/safety.md`, top note): a heat pump uses
the compressor for *both* directions — heating is the refrigeration cycle run
in reverse — so every safety assumption here would need a heating-side twin
(reversing-valve behavior, defrost cycles, low-ambient heating lockouts,
auxiliary/emergency heat staging). Plenum assumes furnace-heat + AC-cool; that
is why `cooling_lockout_below_f` has no heating equivalent
(`models.py` comment: "heat pumps are not supported, so there is no
corresponding heating lockout").

---

## 2. Why software can destroy equipment — and the exact Plenum guard for each

Plenum sends real commands to real compressors. Each physical failure mode
below maps to a specific guard and config field. These guards are one-way
ratchets — never weaken one to fix a comfort complaint (inferred house rule;
see plenum-change-control).

| Physical failure mode | What happens | Plenum guard | Config field(s) |
|---|---|---|---|
| **Short-cycling** (rapid stop/start) | See below | Min-runtime hold + off-time lockout | `ThermostatConfig.min_cycle_runtime_min` (default 0, recommended 10), `min_cycle_offtime_min` (default 0, recommended 5) |
| **Dead-heading / high static pressure** (too many vents closed) | Blower strain, furnace high-limit trip, frozen evaporator coil | Airflow floor | `total_vents_count`, `has_bypass_damper`, `min_open_vents_fraction` (default 0.333) |
| **Liquid slugging + coil icing** (AC in cold weather) | Compressor destroyed by incompressible liquid; iced coil | Outdoor-temperature cooling lockout | `cooling_lockout_below_f` (default None = disabled, ~55 °F recommended) |
| **Acting on dead sensors** | Confident wrong decisions | Staleness guard | `system_settings` key `sensor_stale_after_min` (default 30 min, range 1–1440) |
| **Blind running** (climate entity unavailable mid-cycle suspends all per-tick monitors, #267) | HVAC keeps running at last setpoint, vents closed, nobody watching | Unavailability abort | `unavailable_abort_after_min` (default 5, 0 = never abort) |

### 2.1 Short-cycling (the compressor killer)

Three separate physics reasons rapid stop/start destroys compressors:

1. **Oil return.** Lubricating oil circulates *with* the refrigerant; it only
   migrates back to the compressor sump during sustained running. Short runs
   strand oil in the lines → the compressor runs progressively drier.
2. **Contactor and motor stress.** Motor inrush current is several times run
   current; every start heats windings and arcs the contactor. Start count,
   not run hours, wears these out.
3. **Refrigerant pressure equalization.** While running, the compressor holds
   a large high-side/low-side pressure differential. After stop, pressures
   take minutes to equalize through the metering device. Restarting against
   the differential stalls the motor against enormous torque (and can pull
   liquid refrigerant into the cylinder). This is why **~5 minutes** is the
   industry-standard anti-short-cycle off delay — Plenum's recommended
   `min_cycle_offtime_min` value (`docs/safety.md`).

Plenum-side mapping:
- `min_cycle_runtime_min` — a cycle that satisfies every room early is **held
  open** (not stopped) until the runtime clock is satisfied; the hold is
  persisted as `in_min_runtime_hold` on the cycle log and survives restarts.
- `min_cycle_offtime_min` — after a cycle ends, new cycles are refused with a
  logged warning until the off-timer expires. A running cycle is never
  interrupted by the lockout.
- **In-place cycle updates** are short-cycle protection too: a mid-cycle
  target/source change updates the running cycle instead of tearing it down
  (teardown = an unnecessary compressor stop, and with the off-time lockout on,
  minutes of no conditioning). See `docs/safety.md` "In-place cycle updates".
- Back-fill rule: on upgrade, *pre-existing* thermostats get the recommended
  10/5 values (they presumably control live equipment); *new* thermostats
  start at 0 and opt in.

### 2.2 Dead-heading and static pressure

Zoning's inherent hazard: the blower moves a roughly fixed air volume; every
closed register raises **duct static pressure**. Past a threshold you strain
the blower, trip the furnace high-limit, or — on cooling — reduce airflow
across the evaporator enough that its temperature drops below freezing and
the coil ices over (icing further blocks airflow: a runaway). "Dead-heading"
is the limit case: pushing against a (nearly) fully closed system.

The **airflow floor** (`docs/safety.md`, Issue #213) keeps a fraction of *all*
registers open. Key subtlety: `total_vents_count` counts smart **and passive**
registers — passive ones are always open and count toward the floor. Each tick:

```
required_smart_open = max(0, ceil(total_vents_count × min_open_vents_fraction)
                             − (total_vents_count − smart_vents_count))
```

Worked example from the doc: 12 total registers, 4 smart, fraction 1/3 →
`ceil(4) − 8 = −4` → clamped to 0: all smart vents may close, the 8 passive
registers are the floor. On a 4/4 all-smart system: `2` smart vents must stay
open. A **bypass damper** (mechanical pressure-relief valve between supply and
return) makes the floor unnecessary — `has_bypass_damper=True` disables
enforcement. Most homes do not have one.

Related belt-and-braces fields: `max_vent_closed_min` (force-reopen a vent
after N minutes regardless), `cycle_timeout_hours` (default 3 — abort runaway
cycles), and the hold behavior itself (the min-runtime hold re-opens all
active rooms' vents precisely so the compressor never dead-heads through
whichever room finished last).

### 2.3 Cold-weather AC: liquid slugging and coil icing

Refrigerant condenses in the coldest part of the circuit. When it's cold
outside, refrigerant pools as liquid in the outdoor unit; starting the
compressor can pull that **liquid** into the cylinders — liquid is
incompressible, and the compressor is a pump built for vapor ("slugging" =
hydraulic-locking your compressor). Low ambient also drops system pressures so
the evaporator runs below freezing and ices. Guard:
`cooling_lockout_below_f` — the engine refuses to *start* a cooling cycle
while the (home-wide, single) outdoor sensor reads below the threshold, with
an auditable event-log warning. **Fail-open**: if the threshold is set but the
outdoor sensor is unreadable, cooling is allowed and a warning logged — a
dropped weather entity must not silently kill summer cooling
(`docs/safety.md`). The outdoor sensor is deliberately *not* subject to the
staleness guard (weather entities may legitimately report hourly).

---

## 3. Control theory as Plenum uses it

- **Setpoint**: the target temperature a controller regulates toward. Two
  distinct setpoints exist here: each *room's* target (schedule/override/
  presence) and the *thermostat's* commanded setpoint (which Plenum
  manipulates to make the HVAC run or stop).
- **Deadband** (`ThermostatConfig.deadband`, default 0.5 °F, stored as a ±
  tolerance): "at target" means within `target ± deadband`. **Why zero
  deadband oscillates**: temperature is noisy and sensors quantize; with an
  exact-match condition, a room sitting at 70.0 flickers across the boundary
  every tick, toggling vents and cycle decisions each 60 s. The deadband is
  classic **hysteresis** — separate the turn-on and turn-off thresholds so
  small noise cannot cause state changes. Per-room override:
  `deadband_override` (nullable delta, Issue #277).
- **Overshoot delta** (`overshoot_delta`, default 2 °F): when a cycle starts,
  Plenum sets the thermostat *past* the most demanding room's target by this
  amount. Why: the thermostat's own sensor sits in one hallway; if Plenum set
  it exactly to the target, the HVAC would shut off when the *hallway* reached
  target, long before the zoned rooms (behind partially-restricted ducts) got
  there. Overshoot keeps the equipment running while the easier rooms are
  satisfied and their vents close; the min/max setpoint clamps (60/85 °F
  defaults) bound it, and `cycle_timeout_hours` catches a misconfigured
  overshoot that never lets the HVAC stop. On cycle end the setpoint is
  restored to the thermostat's own ambient so the HVAC shuts off naturally.
- **Mode locking**: cycle direction (heat/cool) is inferred from room temps at
  cycle start and then locked; rooms wanting the opposite direction are
  dropped from the cycle. Prevents frame-to-frame mode flip during idle
  phases (`docs/cycle-engine.md`).

### 3.1 Post-closure thermal drift and #86

When a room reaches target its vent closes and conditioning stops — so the
temperature immediately drifts back (a just-cooled room warms). Bug #86: the
engine's "all rooms at target?" termination check had no guard for
already-closed vents, so one tick of post-closure drift (68.0 → 68.1 °F in
cooling) flipped `at_target` back to False and the cycle **never terminated**
— it ran the entire schedule window with every vent closed. The fix encodes a
domain rule: *a room whose vent is closed has been served; its subsequent
drift is irrelevant to cycle completion* (`elif not at_target and
rcs.vent_closed_at is None`).

`Room.temp_offset` (default 0.0, a **delta**: "°F added to measured avg before
comparing to target", `models.py:44`) is per-room sensor calibration — it
compensates for a sensor that systematically reads high/low for where the
occupant actually sits. Note it is a lever with teeth: #38 showed a large
`temp_offset` can make the engine infer a mode that contradicts the
thermostat's own ambient (offset −4.0 on a 73.6 °F reading → "needs heat" at
74 °F ambient). Treat big offsets as a smell.

### 3.2 Opposite-cycle prevention: the overflow tiers (docs/overflow-conditioning.md)

During the minimum-runtime hold, the compressor must keep running but every
active room is done. Before #237 the surplus was dumped back into the active
rooms — over-conditioning them until they'd call for the *opposite* direction
next pass (heat/cool oscillation), and the per-tick close loop would fight the
hold's re-opened vents (open/close thrashing; fixed by the persisted
`in_min_runtime_hold` flag gating the close loop). Overflow conditioning
(`overflow_during_min_runtime`, default True; disabled entirely in vacation
mode; recomputed every tick of the hold) instead routes surplus air to
non-active rooms via tiers. Definitions: `effective_setpoint` is the room's
own `Room.system_wide_temp`, else the thermostat's `default_temp`, else the
room is unrankable and skipped by all tiers; `active_cycle_target_f` is the
most aggressive active-room target (lowest for cooling, highest for heating);
`deadband` is the thermostat's.

**Tier 1 — surplus rooms (outside deadband).** A non-active room qualifies if:
- Cooling: `room.current_temp > effective_setpoint + deadband` **and**
  `room.current_temp > active_cycle_target_f`
- Heating: `room.current_temp < effective_setpoint − deadband` **and**
  `room.current_temp < active_cycle_target_f`

Every Tier-1 candidate is opened. These rooms genuinely want the conditioning.

**Tier 2 — inside-deadband rooms.** Only if Tier 1 is empty: same direction
check *without* the deadband margin. Nudges a room slightly further toward
its setpoint when there is no better destination.

**Tier 3 — headroom rooms (accept pushing past goal).** Only if Tiers 1–2 are
empty. Headroom = distance the room can drift in the conditioning direction
before *it* would call for the opposite cycle:
- Cooling: `headroom = room.current_temp − (effective_setpoint − deadband)`
- Heating: `headroom = (effective_setpoint + deadband) − room.current_temp`

Zero-or-negative-headroom rooms are **excluded** — conditioning them creates
exactly the oscillation this prevents. Largest positive headroom wins.

**Tier 4 — fallback.** All tiers empty → pre-#237 behavior: only originally
active rooms stay open.

The construction guarantees: no room is pushed past its goal while a better
destination exists, and no room is ever pushed across its opposite-direction
trigger. Observability: `opened_overflow_hold` / `closed_overflow_hold` vent
events carry the tier and (tier 3) the computed headroom.

---

## 4. Temperature math: absolute vs delta

Two conversion kinds exist and confusing them silently corrupts data. All four
functions live in `smart_vent/backend/units.py` (backend) and
`frontend/src/contexts.ts` `buildUnitContext` (frontend).

| Kind | °C→°F | °F→°C | Applies to |
|---|---|---|---|
| **Absolute** (a temperature) | `F = C × 9/5 + 32` | `C = (F − 32) × 5/9` | target_temp, setpoints, default_temp, system_wide_temp, cooling_lockout_below_f |
| **Delta** (a temperature *difference*) | `F = C × 9/5` | `C = F × 5/9` | deadband, overshoot_delta, temp_offset, deadband_override, ambient_suppression_* |

Why no −32 for deltas: the 32 is the offset between the scales' *zero points*;
a difference of two temperatures cancels it. Worked wrong-vs-right example
(Issue #291, the overshoot-histogram subtitle bug): a **2 °F overshoot**
(a delta) rendered through the absolute formula gives
`(2 − 32) × 5/9 = −16.7 °C` — a negative "overshoot". The delta formula gives
`2 × 5/9 = 1.1 °C`, the true value. Same class of bug as CLAUDE.md pitfall #4.
The `kind` tag in the `TEMPERATURE_FIELDS` manifests (`routes.py`:
`absolute` / `absolute_nullable` / `delta` / `delta_nullable`) exists to pin
this choice per field and parity-test it.

**Rounding conventions** (verified in `units.py` and `contexts.ts`):

| Function | Direction | Precision |
|---|---|---|
| backend `to_f` / `delta_to_f` | display→°F storage (write boundary) | **2 dp** |
| backend `from_f` / `from_f_delta` | °F→display (CSV export only; `from_f(None)` → `""`) | **1 dp** |
| frontend `toDisplay` | °F→display absolute | **1 dp** |
| frontend `toDisplayDelta` | °F→display delta | **2 dp** |
| frontend `toStorage` / `toStorageDelta` | display→°F (reserved; never on outgoing payloads) | 2 dp |
| frontend `fmtTemp` | formatted string, e.g. `"21.1°C"` | 1 dp |

Rule of thumb: storage direction gets 2 dp (don't lose precision in the DB);
display direction gets 1 dp (humans). Who converts where (frontend never
converts outgoing payloads, #231) is owned by **plenum-architecture-contract**
— do not re-derive it from here.

---

## 5. The HA entity model as Plenum uses it

### 5.1 `climate.*` — three different "what is it doing" fields (#38 territory)

Classic confusion — these are NOT interchangeable:

| Field | Where | Meaning | Example values |
|---|---|---|---|
| **`state`** = **`hvac_mode`** | entity state | What the thermostat is *allowed/configured* to do | `heat`, `cool`, `heat_cool`, `off` |
| **`hvac_action`** | attribute | What the equipment is doing *right now* | `heating`, `cooling`, `idle`, `off` |
| **`temperature`** / `current_temperature` | attributes | commanded setpoint / ambient reading | numbers in **HA's system unit** |

A thermostat in mode `heat` with action `idle` is ON but satisfied. In mode
`heat_cool` (auto), the *mode tells you nothing about direction* — only the
action does. Plenum's `_read_hvac_mode()` (`cycle_engine.py`) encodes the
correct precedence: trust `hvac_action` when it is `heating`/`cooling`; when
`idle`, fall back to `hvac_mode` **only** for unambiguous single-direction
modes (`heat`/`cool`) — mapping `heat_cool` to "heating" was a real bug that
made `_is_at_target` compare in the wrong direction during a cooling cycle's
idle phase. #38 is the cautionary tale of mode inference gone wrong (skipped
unavailable sensors, no ambient cross-check, stale restored mode).

Unit trap: `climate` temperature attributes report in **HA's configured system
unit** (`HAClient.ha_temp_unit`, derived from `/api/config`'s `unit_system.temperature`),
*not* a per-entity `unit_of_measurement`. Every climate read goes through
`_climate_temp_to_f(value, ha_temp_unit)` (Issue #280); every climate *write*
must convert the stored °F setpoint to HA's unit or a metric install drives
the HVAC to a wildly wrong temperature (`ha_client.py`). This is also the E2E
`158°F` golden bug: an HA fixture without `unit_system:` defaults to metric,
so `target_temp: 70` means 70 °C = 158 °F (see CLAUDE.md).

### 5.2 `cover.*` — four ways to move a vent (docs/vent-control.md)

Stored per vent as its control method:

| Method | Open | Close | Use for |
|---|---|---|---|
| `open_close` (default) | `cover.open_cover` | `cover.close_cover` | Most HA cover integrations |
| `set_position` | `cover.set_cover_position` `position=100` | `position=0` | Covers exposing 0–100 position but not open/close |
| `set_tilt_position` | `cover.set_cover_tilt_position` `tilt_position=100` | `tilt_position=0` | **Flair vents** (RobertD502 HACS integration) and anything reporting `current_tilt_position` |
| `toggle` | `cover.toggle` | `cover.toggle` | Stateful flip-flop vents; last resort |

Plenum judges whether a vent is passing air using the attribute that matches
its own configured `control_method` (#425) — it does not check both position
attributes on every vent: a `set_tilt_position` vent reads
`current_tilt_position`, a `set_position` vent reads `current_position`;
`open_close`/`toggle` vents (and any vent whose matching attribute is absent)
fall back to HA's plain cover `state`. This is why Flair needs
`set_tilt_position` (the integration reports tilt, not position; symptom of
the wrong method: vent physically moves but UI shows it closed). Single-vent
command failures are logged and skipped — one bad vent never aborts a cycle.
The UI's per-vent **Test** button trials a method before saving.

### 5.3 `sensor.*` — unit normalization at ingest

Sensors carry a per-entity `unit_of_measurement` attribute. Two ingest paths
normalize °C→°F so **everything downstream is °F**:
- `HAClient` numeric reads: `unit_of_measurement == "°C"` → `to_f(value,"C")`
  (`ha_client.py` ~line 156).
- `POST /api/ha/states` (`routes.py` `ha_states`): same conversion, rewrites
  `unit` to `"°F"`, rounds `numeric` to 1 dp — so `EntityState.numeric` in the
  frontend is *always* °F and must be rendered via `fmtTemp()`.

This normalization is independent of the active *display* unit. Staleness:
a dead battery sensor keeps its last value in HA (it does not flip to
`unavailable`), hence the `last_updated`-based staleness guard (section 2).
Rooms average multiple sensors (`_get_avg_temp`), excluding stale ones; if all
are stale, fallback is the thermostat's own `current_temperature`.

### 5.4 `binary_sensor.*` — presence semantics (docs/presence.md)

State machine, not a level: any configured room `binary_sensor.*` turning
`on` (motion/occupancy) activates the room and starts a **holdover timer**
(per-room, default 2.0 h; 0 disables presence for the room). Each new `on`
event *resets* the expiry — occupancy is inferred from recency of motion, not
from the sensor staying `on`. Target resolution: room `system_wide_temp` →
thermostat `default_temp` → skipped. Priority: manual override > schedule >
presence. Holdover expiry is persisted in the DB so restarts don't drop an
occupied room.

### 5.5 Ingress

HA **ingress** is HA's authenticated reverse proxy for add-on UIs: the Plenum
SPA is served through HA at a rewritten, token-protected base path rather than
a raw port. Consequences: `config.yaml` sets `ingress: true`,
`ingress_port: 8099`; asset URLs must be base-path-relative (issue #74 was a
blank screen from a base-path mismatch); direct port 8099 and the MCP port
9099 are optional host mappings, blank by default. Swagger UI is self-hosted
(no CDN) specifically to work under ingress/CSP.

---

## 6. Glossary of domain terms used across the skill library

| Term | Meaning here |
|---|---|
| **Air handler** | The blower unit that pushes air through the ducts (and across the AC coil). |
| **Airflow floor** | Minimum fraction of registers kept open; the anti-dead-head guard (#213). |
| **Bypass damper** | Mechanical duct valve that relieves static pressure; if present, the airflow floor is not enforced. |
| **Call for heat / cool** | The thermostat's demand signal that turns equipment on. |
| **Coil icing** | Evaporator freezing over from low airflow or low ambient; blocks airflow further. |
| **Compressor** | The AC's refrigerant pump; the expensive component all short-cycle/lockout guards protect. |
| **Contactor** | Heavy-duty relay switching the compressor; worn by start count. |
| **Cycle** | One Plenum-managed conditioning run: IDLE → RUNNING → TERMINATING → IDLE, one at a time per thermostat, 60 s ticks. |
| **Deadband** | ± tolerance around a target within which a room counts as "at target"; hysteresis against oscillation. |
| **Dead-heading** | Running the blower against a (nearly) closed duct system. |
| **Delta** | A temperature *difference*; converts °F↔°C without the 32 offset. |
| **Evaporator coil** | Indoor coil where refrigerant absorbs heat from the airstream. |
| **Golden** | Committed reference screenshot the E2E visual-regression suite compares against (see plenum-ci-and-release). |
| **Headroom** | How far a room can drift in the conditioning direction before it would call for the opposite cycle (overflow Tier 3 ranking). |
| **Holdover** | Presence-activation timer that keeps a room active after the last motion event; persisted in DB. |
| **HVAC action vs mode** | What equipment is doing now (`hvac_action` attribute) vs what it's configured to do (`hvac_mode` = entity state). |
| **Hysteresis** | Separating on/off thresholds so noise can't toggle state; implemented via deadband. |
| **Ingress** | HA's authenticated proxy that serves add-on UIs under a rewritten base path. |
| **Liquid slugging** | Liquid refrigerant entering the compressor cylinders — hydraulic lock; why cold-weather AC is locked out. |
| **Min-runtime hold** | Keeping a satisfied cycle running until `min_cycle_runtime_min` elapses; flagged `in_min_runtime_hold`. |
| **Off-time lockout** | Refusing to start a new cycle for `min_cycle_offtime_min` after one ends; the ~5-min pressure-equalization delay. |
| **Opposite-cycle prevention** | The guards ensuring a hold/overflow never over-conditions a room into calling for the reverse direction. |
| **Overflow conditioning** | Routing surplus hold-phase air to non-active rooms via the tier system (#237). |
| **Overshoot (delta)** | Setting the thermostat past the most demanding target so the HVAC keeps running for the slower rooms. |
| **Passive register** | A vent with no motor; always open; counts toward the airflow floor. |
| **Plenum (the word)** | The sheet-metal distribution box between air handler and ducts; also this project's name. |
| **Presence** | Room activation from `binary_sensor` motion/occupancy events. |
| **Reconciliation** | Periodic re-read of actual vent/thermostat state from HA to correct external overrides. |
| **Register / vent** | The room-level grille; the `cover.*` entity Plenum opens and closes. |
| **Setpoint** | A controller's target temperature (room target vs commanded thermostat setpoint — distinct). |
| **Short-cycling** | Rapid compressor stop/start; kills compressors via oil starvation, start stress, and pressure-differential restarts. |
| **Staleness (sensor)** | A reading whose HA `last_updated` exceeds the threshold; excluded from room averages. |
| **Static pressure** | Duct back-pressure; rises as vents close. |
| **Zoning** | Conditioning rooms selectively on a single HVAC system by controlling their registers. |

---

## Provenance and maintenance

Written 2026-07-04 against v0.22.1; re-verified 2026-09-01 against v0.35.0
(§5.2's vent-read description was stale — updated to match the per-`control_method`
attribute read in `vent_controller.py` `_is_open`/`_position_attr_for`, #425 —
everything else checked out unchanged: `ThermostatConfig` defaults, the four
conversion functions' rounding, the airflow-floor formula, the overflow-tier
conditions, `_read_hvac_mode` precedence, staleness default/range, and ports/
ingress). Sources: `docs/safety.md`,
`docs/cycle-engine.md`, `docs/overflow-conditioning.md`, `docs/vent-control.md`,
`docs/presence.md`, `docs/thermostat-settings.md`,
`smart_vent/backend/units.py`, `smart_vent/backend/models.py`,
`smart_vent/backend/api/routes.py`, `smart_vent/backend/engine/cycle_engine.py`,
`smart_vent/backend/engine/vent_controller.py`, `smart_vent/backend/ha_client.py`,
`smart_vent/frontend/src/contexts.ts`,
`smart_vent/config.yaml`; issues #38, #86, #123, #208–#213, #237, #248, #251,
#267, #277, #280, #291, #425. The physics explanations (oil return, contactor wear,
pressure equalization, slugging, static pressure) are standard HVAC domain
knowledge, not repo-derivable; the repo-side mappings are all verified.

Volatile facts and re-verification (run from repo root):

- Defaults on `ThermostatConfig` (deadband 0.5, overshoot 2.0, setpoints 60/85,
  fraction 0.333, timeouts, `overflow_during_min_runtime=True`,
  `unavailable_abort_after_min=5`):
  `grep -n 'class ThermostatConfig' -A 70 smart_vent/backend/models.py`
- Rounding precision: `sed -n 23,55p smart_vent/backend/units.py` and
  `grep -n 'toFixed' smart_vent/frontend/src/contexts.ts`
- Tier conditions: `sed -n 20,50p docs/overflow-conditioning.md`
- Airflow-floor formula: `grep -n -A6 'required_smart_open' docs/safety.md`
- `TEMPERATURE_FIELDS` kinds: `grep -n -A 20 'TEMPERATURE_FIELDS: dict' smart_vent/backend/api/routes.py`
- Staleness key/range: `grep -n 'sensor_stale_after_min' smart_vent/backend/api/routes.py`
- `_read_hvac_mode` precedence: `grep -n -A 25 'def _read_hvac_mode' smart_vent/backend/engine/cycle_engine.py`
- Ports/ingress: `grep -n 'ingress\|8099\|9099' smart_vent/config.yaml`
- Cover methods table: `sed -n 1,25p docs/vent-control.md`
- Vent-read attribute logic: `grep -n -A 20 'def _is_open' smart_vent/backend/engine/vent_controller.py`
