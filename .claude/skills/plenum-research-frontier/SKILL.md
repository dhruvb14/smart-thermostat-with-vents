---
name: plenum-research-frontier
description: Open, SOTA-advancing research problems for Plenum — learned per-room thermal models, weather-aware predictive cycle planning, adaptive overshoot/deadband tuning, heat-pump shadow studies, and LLM/MCP self-explanation — each with the verified data asset behind it, the first three concrete in-repo steps, and a falsifiable numeric milestone. Load when asked "what could Plenum do that Flair/ecobee/research MPC can't", when starting an experiment on cycle-history data, or when scoping a learning/optimization feature. NOT a runbook for measuring today's behavior (plenum-diagnostics-and-tooling) or for how to run an experiment properly (plenum-research-methodology).
---

# Plenum research frontier

Open problems where Plenum could plausibly advance the state of the art in
residential HVAC zoning. Every item below is **candidate/open work — nothing
here is built, promised, or proven**. This skill owns the WHAT (which problems,
which data assets, which first steps, which success numbers). The DISCIPLINE —
how a hunch becomes an accepted result, what evidence bar applies — is owned by
**plenum-research-methodology**; read it before starting any item. The
measurement substrate (DB queries, ready-made analysis scripts) is owned by
**plenum-diagnostics-and-tooling**.

## When NOT to use this skill

| You want | Load instead |
|---|---|
| Numbers about what the system did (cycles, overshoot, duty cycle) | plenum-diagnostics-and-tooling |
| The evidence bar / idea lifecycle for an experiment | plenum-research-methodology |
| To actually change engine/guard code | plenum-change-control (gates), plenum-architecture-contract (invariants) |
| Domain theory (deadband, short-cycling, RC thermal models context) | hvac-zoning-reference |
| Where a config knob lives / its bounds | plenum-config-and-flags |

## Ground rules for all experiments (non-negotiable)

1. **Safety guards are one-way ratchets** (inferred rule from repo history — see
   plenum-change-control). No experiment may weaken short-cycle protection
   (`min_cycle_runtime_min` / `min_cycle_offtime_min`), the cooling lockout
   (`cooling_lockout_below_f`), the airflow floor (`min_open_vents_fraction`),
   the staleness/unavailability guards, or the `min_setpoint`/`max_setpoint`
   envelope — even "temporarily, for data collection".
2. **The sandbox is Dev Mode, not a simulator.** Dev Mode runs the engine
   normally — real sensor reads, real room selection, real vent-move
   computation — but intercepts every outbound HA service call
   (`climate.set_temperature`, `cover.open_cover`, `cover.close_cover`,
   `cover.set_cover_position`, `cover.set_cover_tilt_position`,
   `cover.toggle`) and logs it to `event_log` instead of sending it. There are
   **no simulated cycles and no synthetic house model**: what you get is
   *shadow mode* — the engine's would-be commands against the real house's real
   trajectory, which itself is being driven by whatever is actually controlling
   the HVAC. Mode semantics are owned by **plenum-run-and-operate**; the one
   line that matters here: the engine gate is `system_enabled OR dev_mode`
   (`scheduler.py:477`), so engines keep ticking under Dev Mode even with
   System Off — only both-off stops ticking entirely.
   Design experiments accordingly — Dev Mode gives you counterfactual command
   logs, not counterfactual temperatures.
3. **Offline first.** Every item below starts with read-only analysis of
   `app.db` (see plenum-run-and-operate for where it lives). Copy the DB out and
   open it `mode=ro`, as the diagnostics scripts do.
4. **All stored temperatures are °F; deltas are °F deltas** (no −32). Model in
   °F end-to-end; convert only at the write boundary if a result ever ships
   (see plenum-architecture-contract for the #231 contract; the conversion
   helpers live in `smart_vent/backend/units.py` — pre-2026-07-05 CLAUDE.md
   copies said routes.py; corrected in PR #388 — if docs and repo disagree
   again, the repo wins). Timestamps in the DB are naive UTC ISO strings.
5. **Anything that ships needs a UI control** (CLAUDE.md 100% rule) and clears
   plenum-change-control. Advice-only outputs (a "suggested value" shown in the
   UI) are a deliberately lower-risk shipping target than closed-loop control.

## The verified data substrate (as of 2026-07, v0.22.1)

All verified by reading `smart_vent/backend/db.py` (SCHEMA, lines 42–247) and
the engine. This per-room, per-tick, actuation-annotated dataset is the asset
commercial products don't expose and research MPC testbeds have to build by
hand.

| Table | Columns that matter for research | Written by |
|---|---|---|
| `cycle_logs` | `started_at`, `ended_at`, `mode`, `ended_reason`, `thermostat_temp_at_start/_end`, `setpoint_at_start/_end`, `vents_at_start/_end`, **`outside_temp_at_start`**, **`outside_temp_at_end`**, `in_min_runtime_hold` | engine, cycle open/close (`cycle_engine.py` ~723, ~1275) |
| `room_cycle_states` | per (cycle, room): `target_temp`, **`reached_at`**, **`vent_closed_at`**, `temp_at_start`, `temp_at_end`, `joined_at`, `role` (`'overflow'` rows are conditioning ballast — exclude from comfort analyses) | engine |
| `cycle_temp_samples` | **one row per active room per 60s tick**: `timestamp`, `room_temp`, `thermostat_temp`, `setpoint` (writer: `cycle_engine.py:1098`) | engine |
| `cycle_setpoint_history` | every setpoint command with `reason` | engine |
| `cycle_vent_events` | every vent `action` + `reason` with timestamp and `entity_id`/`room_id` | engine |
| `daily_thermostat_metrics` / `monthly_…` | `heating_seconds`, `cooling_seconds`, cycle counts by outcome, `avg_outside_temp_at_start/_end` | rollup jobs |
| `rooms` | the hand-tuned knobs to replace: `temp_offset`, `deadband_override`, `ambient_suppression_*` | user |
| `thermostat_configs` | `overshoot_delta` (default 2.0 °F), `deadband` (default 0.5 °F), the safety envelope fields | user |
| `event_log` | categorized narrative incl. Dev-Mode intercepted commands | event_logger |

Plus: metrics endpoints (`/api/metrics/thermostats/{id}/timeseries`,
`…/cycles-vs-outside-temp`, `…/overshoot-histogram`, `…/rooms` with
time-to-target — `routes.py` ~2073–2263), the diagnostics scripts
(`plenum-diagnostics-and-tooling/scripts/`: `cycle_report.py`,
`overshoot_stats.py`, `hvac_quality.py`), and the full REST surface as
auto-generated MCP tools (below).

**Data-hygiene caveat found while verifying (check before trusting):** the only
production writer of `cycle_temp_samples` (`cycle_engine.py:1098`) always passes
a non-NULL `room_id`, yet `db._degree_minutes_timeseries` (db.py:1897) filters
`s.room_id IS NULL` (thermostat-level samples), which in the current code only
tests create. Before building on either representation, run on a real DB:
`sqlite3 app.db "SELECT COUNT(*), SUM(room_id IS NULL) FROM cycle_temp_samples"`.
If the NULL count is 0, the degree-minutes chart is computing over nothing —
that is itself a finding worth filing. UNVERIFIED on a live DB (no `app.db` in
the repo checkout).

---

## Frontier item 1 — Learned per-room thermal response models (candidate)

Replace the two hand-tuned per-room fudge factors — `rooms.temp_offset` (born
from incident #86: post-closure drift blocked cycle termination) and
`thermostat_configs.overshoot_delta` — with models fitted from cycle history.

**Why current SOTA falls short.** Flair/Keen/ecobee zoning uses static per-room
offsets and fixed hysteresis; none learn per-room dynamics or expose the data to
let a user do it. Residential MPC research fits whole-house RC models on
purpose-built testbeds; per-room models with *vent actuation as the control
input* are rare because nobody logs per-room temperature trajectories aligned
with vent open/close events. Plenum does, every 60 seconds.

**Plenum's specific asset (verified above):** `cycle_temp_samples` per-tick
room trajectories + `room_cycle_states.reached_at`/`vent_closed_at` giving the
exact conditioning→coasting phase boundary per room + `cycle_vent_events` for
every vent move + `cycle_logs.outside_temp_at_start/_end` for the disturbance
term.

**First three steps in this repo:**

1. Write `scripts/response_rate.py` (this skill's dir, read-only, modeled on
   `plenum-diagnostics-and-tooling/scripts/`): per-room conditioning-phase
   response rate in °F/min vs outdoor temperature. Core query (schema-verified):
   ```sql
   SELECT rcs.room_id, cl.mode,
          (rcs.temp_at_end - rcs.temp_at_start) * 60.0 /
            MAX(1,(julianday(COALESCE(rcs.reached_at, cl.ended_at))
                   - julianday(cl.started_at)) * 86400) AS f_per_min,
          cl.outside_temp_at_start
   FROM room_cycle_states rcs JOIN cycle_logs cl ON cl.id = rcs.cycle_id
   WHERE cl.ended_at IS NOT NULL AND rcs.role != 'overflow'
     AND rcs.temp_at_start IS NOT NULL AND rcs.temp_at_end IS NOT NULL;
   ```
   Refine with per-tick slopes from `cycle_temp_samples` joined on
   `(cycle_id, room_id)` restricted to `timestamp < reached_at`.
2. Write `scripts/drift_model.py`: post-closure drift per room — samples with
   `timestamp > rcs.vent_closed_at`, plus `temp_at_end − target_temp` as the
   terminal drift, regressed against `(outside_temp − room_temp)` and elapsed
   minutes. Compare its predictions against the static `rooms.temp_offset`
   baseline on held-out cycles (methodology: see plenum-research-methodology
   for the split discipline).
3. Fit a 1R1C model per room offline (`dT/dt = k_hvac·u + k_env·(T_out − T)`,
   `u` = vent-open indicator from `cycle_vent_events`), least squares over the
   per-tick samples; evaluate both models on the same holdout.

**You have a result when:** the drift model predicts each room's temperature
10 minutes after `vent_closed_at` within **±0.5 °F RMSE on ≥50 held-out
(cycle, room) participations**, and beats the static-`temp_offset` predictor's
RMSE on the same holdout. Below that, the hand-tuned knob stays.

## Frontier item 2 — Weather-aware predictive cycle planning (candidate)

Extend the shipped ambient-drift heuristic (`docs/precool-presence.md`) from a
fixed threshold gate into a prediction: "will this room coast to target within
N minutes?" — and eventually plan cycles against known future targets.

**Why current SOTA falls short.** Commercial "smart recovery"/pre-cool uses
fixed or opaque lead times and is whole-house. Research MPC handles forecasts
but not per-room vent zoning on commodity hardware. Plenum's current heuristic
(verified: `cycle_engine.py:1575` `_ambient_suppression_eligible`, gate =
`outside ≥ target + min_differential`) is deliberately dumb — a 5 °F fixed
differential regardless of how fast this particular room actually drifts.

**Plenum's specific asset:** the `schedules` table means future targets are
*known* (`days_of_week`, `start_time`, `end_time`, `target_temp`); item 1's
drift model gives per-room coast rates; `daily_thermostat_metrics.heating_seconds/
cooling_seconds` give the energy proxy to minimize; `/api/metrics/…/cycles-vs-outside-temp`
(`db.compute_cycles_vs_outside_temp`, db.py:2128) already correlates cycle cost
with weather.

**First three steps in this repo:**

1. `scripts/coast_replay.py`: for every historical presence/schedule-driven
   cycle in `cycle_logs`, ask the item-1 drift model whether coasting would have
   reached target within the presence holdover window; tally (a) conditioning
   seconds that could have been skipped, (b) predicted comfort misses (>1 °F
   beyond deadband). Pure offline replay — no engine changes.
2. If replay is favorable, prototype replacing the fixed
   `ambient_suppression_min_differential` gate with predicted time-to-target in
   `_ambient_suppression_eligible` — per-room opt-in flag, default off, additive
   schema change only, full plenum-change-control pass (touches engine
   decision logic).
3. Forecast ingestion (candidate, further out): read an HA `weather.*` /
   forecast sensor via `ha_client.py` alongside the existing outside-temp
   sensor, so the gate can use "outside temp in 60 min" instead of now.
   Fail-safe like the current feature: unreadable forecast ⇒ behave exactly as
   today.

**You have a result when:** replay over **≥30 days of real history** predicts
**≥10% reduction in total `heating_seconds + cooling_seconds`** (from
`daily_thermostat_metrics`) with **≤1 predicted comfort miss per week** (room
>1 °F beyond its effective deadband while coasting). If the drift model isn't
past its item-1 milestone, this item is blocked — don't start here.

## Frontier item 3 — Adaptive overshoot & deadband tuning inside the safety envelope (candidate)

`overshoot_delta` (default 2.0 °F) and `deadband` (default 0.5 °F) are static
per-thermostat guesses. The overshoot histogram
(`db.compute_overshoot_histogram`, db.py:1943, semantics replicated in
`overshoot_stats.py`) already measures how wrong they are; today a human closes
the loop by hand.

**Why current SOTA falls short.** ecobee's adaptive recovery is single-zone and
unexplainable; no commercial zoning product tunes per-thermostat hysteresis
from observed per-room overshoot, and none show their work.

**Plenum's specific asset:** the histogram + `cycle_setpoint_history` (every
setpoint command with `reason`) + `ended_reason` outcome buckets
(`_ROLLUP_REASON_BUCKETS`, db.py:1407) + a *hard* envelope to tune inside:
`min_setpoint`/`max_setpoint` (validated 40–90 °F post-normalization, per
`.jules/sentinel.md`), `min_cycle_runtime_min`/`min_cycle_offtime_min`, and
`hvac_quality.py` as an automated regression detector (exit 1 on red flags).

**First three steps in this repo:**

1. `scripts/suggest_overshoot.py`: per thermostat, recommend
   `overshoot_delta` = e.g. the 80th-percentile observed overshoot from the
   histogram data over the last N weeks, floored at the current deadband;
   print current vs suggested with supporting cycle counts.
2. Surface it advice-only: a "suggested value" annotation next to
   `overshoot_delta` on the Thermostats page (satisfies the UI rule; no control
   authority; new read-only endpoint over the existing histogram query).
3. Closed-loop candidate (only after 1–2 prove out): a nightly job proposing a
   bounded step (≤0.5 °F per week) applied **via the existing PUT
   `/api/thermostats/...` route** so the write-boundary validation and logging
   apply — never a direct DB write. Trial first in Dev Mode / on the advice-only
   output; requires plenum-change-control review as an engine-adjacent change.

**You have a result when:** after adopting suggested values on a real system,
the overshoot-histogram mass in bins **≥2 °F drops by ≥50% over ≥40 subsequent
completed cycles**, while `timeout_count` does not rise and `hvac_quality.py`
still exits 0 (no new short cycles / oscillation). A comfort gain that trips a
safety flag is a failed result.

## Frontier item 4 — Heat pumps as a shadow-mode research problem (long-horizon candidate)

Heat pumps are explicitly unsupported (README.md:108, `docs/safety.md` top
note, `models.py:210`) because reversing-valve and defrost semantics invalidate
guards designed for furnace+AC (the cooling lockout has no heating analog —
see plenum-architecture-contract's known-weak-points table). **Nothing in this
item weakens that fence.** Any code change here must clear the
plenum-change-control safety ratchet, and the docs' non-support line stays
until support is real (plenum-docs-and-writing owns that claim discipline).

**Why current SOTA falls short.** Vent zoning of ducted heat pumps is genuinely
open: variable-capacity compressors interact badly with vent-induced static
pressure, and no commercial vent product publishes a safety envelope for it.

**Plenum's specific asset:** Dev Mode (verified, `docs/system-modes.md`) makes
a *zero-actuation shadow study* possible on a real heat-pump home: the engine
runs its full decision logic and every would-be `climate.*`/`cover.*` command
lands in `event_log` — Plenum never touches the equipment.

**First three steps in this repo:**

1. Shadow deployment: run Plenum with **Dev Mode ON** in a heat-pump home
   (rooms/sensors configured, vents mapped or fake `cover` helpers); collect
   ≥2 weeks of `event_log` + `cycle_logs` of would-be behavior while the OEM
   thermostat actually runs the house.
2. Guard-by-guard failure analysis on that data: for each engine guard (mode
   lock, cooling lockout `cycle_engine.py:~426`, min-runtime hold, ambient
   reset), document what a reversing valve / defrost event would have done to
   it. Deliverable: a `docs/`-style analysis note, not code.
3. Only then: a design proposal through plenum-change-control defining a
   heat-pump-specific envelope (e.g. compressor-protective minimum runtimes,
   defrost-window command blackouts) before any `models.py` change.

**You have a result when:** the shadow log over **≥14 days** shows the current
engine would have issued **zero** commands violating a written heat-pump
envelope — concretely: 0 would-be mode reversals within 30 min of a prior
opposite-mode command, 0 would-be cycles shorter than 10 min, 100% of would-be
setpoints inside `min_setpoint`/`max_setpoint`. Any violation is equally a
result: it documents exactly why the fence exists, with data.

## Frontier item 5 — LLM/MCP self-explanation and agent-assisted tuning (candidate)

**Plenum's specific asset (verified in `mcp_openapi.py` / `mcp_http.py`,
shipped 0.22.0 per issue #372/PR #375, port default 9099, off by default):**
the *entire* REST surface — rooms, schedules, thermostat safety config, cycle
and event logs, metrics — is auto-generated into MCP tools from the OpenAPI
spec (`mcp_openapi.build_tool_specs`), each call dispatched back through the
running routes over loopback, so agent writes get the same validation and unit
conversion as the UI. No commercial zoning product exposes anything like this;
"why did my HVAC do X last night?" is unanswerable in Flair/ecobee.

**First three steps in this repo:**

1. Build an explanation eval: 20 questions with DB-ground-truth answers drawn
   from a real `app.db` ("why did the 2pm cycle end?" → `cycle_logs.ended_reason`
   + `cycle_setpoint_history.reason` + `cycle_vent_events`); attach Claude per
   `docs/mcp.md` and grade tool-only answers.
2. If the agent flails from too many round-trips, add one read-only
   "cycle bundle" endpoint aggregating `cycle_logs` + samples + setpoint
   history + vent events for a `cycle_id` (the per-room samples endpoint at
   `routes.py:1581` is the seed). Read-only ⇒ lowest change-control class, but
   still needs `@docs`/`@response_schema` (spec-enforcement test) and becomes
   an MCP tool automatically.
3. Agent-assisted tuning: agent proposes item-3 style config changes via the
   existing PUT tools with a human approving each. **Anything autonomous is
   blocked on #373 (auth)** — the MCP endpoint is unauthenticated and
   off-by-default for exactly this reason; do not route around it.

**You have a result when:** the agent answers **≥90% of the 20-question eval
correctly** (graded against the DB) using only MCP tools, with the transcript
as the explanation artifact. Stretch: same bar from a non-developer's phrasing
of the questions.

---

## Prioritization (one opinion, clearly labeled as such)

Item 1 is the keystone: items 2 and 3 consume its model, and it needs nothing
but a copied `app.db` and a few hundred logged cycles. Item 5 is independent
and cheap to attempt today. Item 4 is deliberately last — highest safety
stakes, longest horizon, and its first step requires access to a heat-pump
home. All numeric milestones above are proposed bars, not attained results.

## Provenance and maintenance

Facts verified against the repo at v0.22.1, 2026-07-05. Re-verify before use:

- Schema (tables/columns cited above): `grep -n "CREATE TABLE" smart_vent/backend/db.py` — SCHEMA block at db.py lines 42–247.
- Per-tick sample writer (only production writer, always non-NULL room_id): `grep -rn insert_cycle_temp_sample smart_vent/backend --include=*.py | grep -v tests` → `cycle_engine.py:1098` only.
- Degree-minutes NULL filter: db.py:1897 (`s.room_id IS NULL`); live-DB check: `sqlite3 app.db "SELECT SUM(room_id IS NULL) FROM cycle_temp_samples"`.
- Hand-tuned knobs + defaults: `rooms.temp_offset` (db.py:50), `overshoot_delta REAL NOT NULL DEFAULT 2.0` (db.py:100), `deadband … DEFAULT 0.5` (db.py:98).
- Ambient-drift heuristic: `docs/precool-presence.md`; `_ambient_suppression_eligible` at `cycle_engine.py:1575`.
- Dev Mode semantics (intercept + log, no simulation): `docs/system-modes.md`.
- Heat-pump fence: README.md:108, `docs/safety.md` top blockquote, `models.py:210`.
- MCP: `mcp_http.py` (port/toggle/503), `mcp_openapi.py` (`build_tool_specs`), `docs/mcp.md`; shipped 0.22.0, port row in config 0.22.1.
- Metrics endpoints: `grep -n "metrics/" smart_vent/backend/api/routes.py` (~lines 2023–2263).
- Diagnostics scripts: `ls .claude/skills/plenum-diagnostics-and-tooling/scripts/`.
- All SQL snippets in this file were executed against an in-memory DB built from the real SCHEMA string (2026-07-05); they parse and run. They have NOT been run against a production `app.db`.
- 60s tick: `smart_vent/backend/scheduler.py` (`seconds=60` at ~lines 133/152).
- Line numbers drift; re-grep rather than trust them after any engine/db change.
