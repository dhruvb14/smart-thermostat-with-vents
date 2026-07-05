---
name: plenum-research-methodology
description: >-
  The discipline that turns a hunch into an accepted result in the Plenum repo
  — the evidence bar (one mechanism must explain every observation, hypotheses
  must predict numbers before you measure, adversarial refutation), the
  hunch→issue→sandbox-experiment→PR→docs lifecycle, how to run a systematic
  audit like the #280–#305 wave, experiment hygiene (both-directions tests,
  timezone matrix, restart-mid-state, simulated-vs-real HVAC), and a
  result-acceptance checklist. Load when you have a theory about a bug or a
  design idea and need to know what proof this project demands before it
  ships.
---

# Plenum research methodology: hunch → accepted result

This skill is the *process* discipline. It does not tell you what to research
(load **plenum-research-frontier**), how to compute a specific analysis (load
**plenum-proof-and-analysis-toolkit**), how to gate a change through review
(load **plenum-change-control**), or how to pull numbers out of a running
system (load **plenum-diagnostics-and-tooling**). Load it when you are about
to claim "I know why X happens" or "we should build Y" and want that claim to
survive review in this repo.

## When NOT to use this skill

- You need a symptom→cause triage table *right now* → **plenum-debugging-playbook**.
- You want the specific SQL/log/metric to measure something → **plenum-diagnostics-and-tooling**.
- You want analysis math with worked examples → **plenum-proof-and-analysis-toolkit**.
- You want what's worth researching next → **plenum-research-frontier**.
- You're past the result and shipping it → **plenum-change-control**, **plenum-validation-and-qa**.
- You want the history of a specific settled battle → **plenum-failure-archaeology**.

Jargon used below (see **hvac-zoning-reference** for full definitions):
*deadband* = tolerance band around a target temperature; *overshoot* =
conditioning past target; *short-cycling* = compressor starting/stopping too
fast; *cycle* = one engine-managed conditioning run; *golden* = committed
reference screenshot for visual regression.

---

## 1. The evidence bar

A root-cause claim is accepted here when it clears three tests. All three,
every time — the project's history shows what each one costs when skipped.

### 1a. One mechanism must explain ALL observations — including the negatives

A mechanism that explains the headline symptom but not the side observations
is at best half the story. Two canonical cases:

**#281 — unit auto-detection had TWO independent root causes.** Symptom:
"leave `temperature_unit` blank to auto-detect" always resolved to °F. Root
cause 1: `ha_client.py` compared HA's `unit_system` — which is an **object**
(`{"temperature": "°C", ...}`) — against the string `"metric"`, so detection
always returned `"F"`. Root cause 2: `run.sh` defaulted a blank option to
`'F'` and exported it, which `scheduler.py` treats as a hard override *lock*
that disables the detection task entirely. Fixing only the string comparison
would have left detection dead on every HAOS install, because the run.sh lock
meant the fixed code never executed. The tell that one cause wasn't enough:
the fix for cause 1 predicts "detection now returns C on metric HA," but the
observation "the detection task never even starts on HAOS" remains
unexplained. **Rule: after you find a cause, re-walk every observation and
ask "does my mechanism produce exactly this?" Any leftover observation means
a second cause, or the wrong cause.** (Corollary from the same incident:
tests can mask a bug by mocking the wrong shape — `test_ha_client_internals`
mocked `{"unit_system": "metric"}`, a payload HA never sends. A passing test
is an observation too; ask what it *actually* exercised.)

**#86 — the surface hunch was wrong.** Symptom: schedule-triggered cycles ran
for hours after every room reached target. The surface hunch — "the schedule
keeps the rooms active, so the cycle can't end" — was plausible and even
partially true, but it doesn't explain the decisive observation: the engine's
own logs showed `all_at_target` flipping back to False *after* every vent had
closed. The actual mechanism was physical + logical: once a vent closes, the
room's temperature naturally drifts back across the target (post-closure
drift), and `_monitor_rooms` had no `vent_closed_at` guard on its
`elif not at_target` branch, so a served room re-blocked termination forever.
The timeline in the issue (vents closed 1:33 AM, cycle still running at
2:21 AM, `engine=running` in reconcile logs throughout) is what killed the
surface hunch — the schedule theory predicts a *re-started* cycle, not one
continuous never-terminating cycle. **Rule: build the event timeline before
theorizing. The observation your hunch cannot reproduce is the one that
matters.**

### 1b. Hypotheses must predict NUMBERS before you run anything

Vague predictions ("the value will be too high") are unfalsifiable — almost
any bug produces "too high." Compute the exact number your mechanism implies,
*then* measure:

- **Double conversion (#231):** if the frontend converts 16 °C → 60.8 °F and
  the backend converts again treating 60.8 as °C, the DB must hold exactly
  `60.8 × 9/5 + 32 = 141.44` °F. Not "over 100" — **141.44**. If you measure
  140.9, your mechanism is wrong (or there's a second conversion/rounding
  step you haven't found).
- **Absolute-vs-delta confusion (#291):** converting a 2 °F deadband with the
  absolute formula predicts `(2−32) × 5/9 = −16.7` °C — a negative number for
  a width. The delta formula predicts `2 × 5/9 = 1.1` °C. One measurement
  discriminates the two instantly.
- **Missing °C→°F normalization (#280/#182's 158 °F bug):** a raw 21 °C read
  as °F predicts readings ~48° low; a 70 °C setpoint sent raw predicts HA
  clamping to the entity max (~35 °C → displayed 158 °F after correct
  back-conversion: `70 × 9/5 + 32 = 158`). The signature offsets — ×1.8,
  +32, −32, ~48° — each fingerprint a distinct wrong code path (the
  discrimination table lives in **plenum-debugging-playbook**).
- **Timezone bugs (#301, #294, #26, #65):** predict the shift exactly: a
  naive-UTC timestamp rendered as local is off by *your current UTC offset*,
  changing at DST. "Off by 5 hours in winter, 4 in summer, in this direction"
  is a prediction; "times look wrong" is not.

Write the predicted number in the issue *before* running the measurement.
If the measurement matches to the decimal, you have the mechanism. If it's
merely "in the same direction," you don't — keep digging.

### 1c. Adversarial refutation — try to kill your own mechanism first

Before accepting a mechanism, construct the observation that would **falsify**
it, then go collect that observation. Concretely:

| If your mechanism is… | The falsifying observation to go look for |
|---|---|
| "frontend double-converts" | a °F-mode install showing the same corruption (double conversion is a no-op in °F — if °F is also wrong, the cause is elsewhere) |
| "schedule keeps the cycle alive" (#86's rejected hunch) | one continuous cycle ID across the whole window instead of repeated restarts |
| "the detection code has a parsing bug" (#281 cause 1 alone) | a debug log proving the detection task *ran at all* on HAOS (it didn't — cause 2) |
| "post-closure drift blocks termination" (#86 accepted) | a cycle where temps held exactly at target after closure and it *still* didn't terminate |
| "vents thrash because monitor re-closes them" (#237) | a hold period with zero open/close events in the event log |

Each row is cheap to check with the event log / cycle history / a SQL query
(recipes in **plenum-diagnostics-and-tooling**). A mechanism you have tried
and failed to refute is worth ten mechanisms that merely fit the first
symptom. This is also the reviewer's job in change control — do it before
they do.

---

## 2. The idea lifecycle in this project

```
hunch ──► issue with predicted numbers ──► sandboxed experiment
                                                │
                                    measured result vs prediction
                                          match?│
                              yes ◄─────────────┴─────────────► no
                               │                                │
                     PR through change control          revise mechanism,
                               │                        or retire with a
                     docs/ page (user-visible           written post-mortem
                     features) + CHANGELOG              (plenum-failure-
                                                        archaeology entry)
```

**Step 1 — Hunch → issue.** File the issue *before* the fix. House style
(visible in #280–#305, #86, #237): Summary, Severity, exact file:line
"Where", predicted numeric impact, "Suggested fix", and reproduction
conditions. One issue per independent mechanism — #281's two causes lived in
one issue only because both gated the same user-visible feature and had to
ship together, and the issue explicitly says "two independent root causes;
both must be fixed."

**Step 2 — Experiment in a sandbox, never on live HVAC first.** Three
sandboxes exist, in increasing realism. Know exactly what each does and does
not give you:

| Sandbox | What it IS (verified in code/docs) | What it is NOT |
|---|---|---|
| **System Off** (`system_enabled` flag) | Semantics owned by **plenum-run-and-operate** (engine gate is `system_enabled OR dev_mode`, `scheduler.py:477`): with Dev Mode also off, engines skip their ticks; any running cycle is aborted immediately (vents restored, setpoint released). Plenum keeps monitoring HA state and serving the UI with **zero** service calls to HA once that one-time in-flight abort completes (abort nuance: plenum-run-and-operate). | Not an experiment mode on its own — with Dev Mode off, nothing computes, so you observe HA passively but learn nothing about engine decisions. With Dev Mode ON, engines still tick even while System is Off (writes intercepted) — that combination is Dev Mode's shadow mode, not a quieter System Off. |
| **Dev Mode** (`developer_mode` flag) | Pure **write interception**: every HA write in `ha_client.py` (`set_thermostat_temperature`, `set_thermostat_hvac_mode`, `set_thermostat_temperature_range`, `open_cover`, `close_cover`, position/tilt/toggle) checks `self.dev_mode` and logs a `[DEV] Would …` line to the event log instead of calling HA. The engine runs fully: real 60s ticks, real sensor reads, real room selection and setpoint math. See `docs/system-modes.md`. | **No fake clock, no simulated physics, no simulated cycles.** Reads are live and time is wall-clock. Because commands never reach HA, rooms never respond to the "conditioning" — so closed-loop behavior (reaching target, post-closure drift, min-runtime holds ending naturally) will NOT play out as it would live. Dev Mode validates *decisions*, not *outcomes*. Cycles started in Dev Mode also historically lingered ACTIVE across toggles (#54) — check cycle state after toggling. |
| **Compose test stack / fake HA** | The E2E docker-compose stack (see **plenum-build-and-env**) and `backend/tests/integration/fake_ha.py` give scripted HA responses — you control the physics. | Not real thermal mass or compressor behavior (§4d). |

For data-shape experiments (migrations, metric queries, trim/pagination
logic), work against a **copy** of the production DB, never the live file:

```bash
sqlite3 /path/to/app.db ".backup '/tmp/experiment.db'"
```

(`app.db` lives in `DATA_DIR`, default `/data` — locations per install mode
in **plenum-run-and-operate**.) Point a local dev backend at the copy via
`DATA_DIR`, or query it directly with the recipes in
**plenum-diagnostics-and-tooling**.

Only after the mechanism survives a sandbox do you touch a live system — and
even then, System Off → Dev Mode → live, watching the Logs page at each step.

**Step 3 — Measured result vs prediction.** Paste both into the issue: the
number you predicted and the number you measured. #86's issue body is the
model — a minute-by-minute event table that any reviewer can re-derive.

**Step 4 — PR through change control.** No shortcuts: classification,
required tests, evidence expectations are owned by **plenum-change-control**.
If your fix adds a temperature field, config option, or UI change, the add-a-
knob / parity checklists apply (**plenum-config-and-flags**,
**plenum-validation-and-qa**).

**Step 5 — Docs or documented retirement.** A shipped user-visible behavior
gets a `docs/` page (house rules in **plenum-docs-and-writing** — e.g.
`docs/overflow-conditioning.md` came out of #237). A *dead* idea gets a
written record too: the refuting observation and the rejected approach go
into the issue before closing, so the next person finds the corpse instead of
re-running the experiment. The settled-battle chronicle is
**plenum-failure-archaeology**; if your retirement is load-bearing (someone
will plausibly re-propose it), add it there.

---

## 3. Where good ideas historically came from — four generators

Evidence: the 48 closed bugs and 58 closed features. Four repeatable
generators produced nearly all of them.

### 3a. Production symptom → design change

The best features here were extracted from bug timelines, not imagined:
- **#237**: vent thrashing during minimum-runtime holds (open→close→open
  churn, verified against `_monitor_rooms` code paths) → the fix half
  (persisted hold state) *plus* a design insight: excess conditioning during
  the hold could be redirected into presence-idle rooms → **overflow
  conditioning** (`docs/overflow-conditioning.md`, follow-ups #254, #268,
  #305).
- **#86**: the post-closure-drift investigation established that room
  temperature measurably drifts after vent closure — a physical fact the
  engine now respects (the `vent_closed_at` termination guard, and drift
  compensation via the room `temp_offset` field — the #86→`temp_offset`
  lineage is *inferred* from the briefing history, not traceable in git log).

Method: when closing a bug, ask "what did the timeline teach us about the
physical system that the design didn't know?" That delta is a feature
candidate.

### 3b. Systematic audits — how to run one (the #32/#48 and #280–#305 method)

Two engine audits (#32, #48) and one pre-launch full-stack audit (~25 issues,
#280–#305) found the highest-severity latent bugs in the project's history —
runaway-HVAC-grade (#280), safety-monitoring gaps (#286), and every °C
sibling of #231. The repeatable method:

1. **Scope one subsystem end-to-end**, not a grep pattern: "the engine's
   per-tick flow," "every temperature that crosses the API," "everything the
   WS reconnect path can do." #32's issue opens by *documenting how the
   system works today* — write that description first; the bugs fall out of
   the places where the code refuses to match your description.
2. **Read adversarially.** For each statement of intent (docstring, CLAUDE.md
   invariant, issue promise), ask "what input makes this false?" #280 came
   from reading the unit contract ("all temps are °F internally") and then
   checking every read site against it — climate-entity attributes were raw.
   #281 came from distrusting a test fixture's payload shape.
3. **One issue per independent finding**, each self-contained: severity,
   file:line, mechanism, predicted numeric impact, suggested fix, and a
   provenance footer (`_Found during full front-end + back-end logic
   audit._`). Never batch unrelated findings — they get triaged, fixed, and
   reverted independently.
4. **Severity-triage before fixing anything.** The #280–#305 wave marked
   HIGH the ones with physical-world consequences (runaway HVAC, suspended
   safety monitoring) and fixed those first; UI paper cuts (#263's missing
   Apply button) queued behind them.
5. **Audit the tests too** — a mock that pre-normalizes data (#280's
   `test_vacation_mode.py` comment claiming HA returns °F) is itself a
   finding.

### 3c. Test-review sweeps (#268–#270)

A cheaper audit variant: read the *test suite* against the feature list and
file an issue per untested guard. #268 (min-runtime hold gate — the #237
anti-thrash fix — had zero coverage), #269 (in-tick system-disabled and
vacation abort guards untested), #270 (nice-to-fix engine test items). The
question that generates these: "which safety-critical branch would a mutation
in production leave green in CI?" Also #212 — cycle timeout had no
end-to-end test.

### 3d. Safety reviews (#208–#213)

A checklist review against domain knowledge rather than code: enumerate the
ways real HVAC equipment gets damaged (short-cycling, dead-heading, running
AC in cold weather, acting on stale sensors) and check the engine has a guard
for each, *on by default* (#213 specifically reviewed defaults). Domain
checklist source: **hvac-zoning-reference**. Safety guards, once shipped, are
one-way ratchets — never weakened to fix comfort (inferred project norm; see
**plenum-change-control**).

---

## 4. Experiment hygiene specific to Plenum

### 4a. Any symmetric contract needs both-directions tests

The generalized #231 lesson: a contract with two symmetric sides passes
trivially when both sides make the same mistake or when you only exercise the
identity direction. °F mode made double conversion a no-op — only the °C leg
of the E2E matrix could catch it, and per CLAUDE.md it is the *only* test
that would have. Apply the same rule to every symmetric pair:

- **°F/°C**: run both matrix legs; assert exact values, not "roundtrips."
- **restart-resume**: test stop-mid-state → restart → resume, not just clean
  start (see 4c).
- **enable/disable**: #54 (dev-mode cycles linger ACTIVE across toggles) and
  #51 (system stop leaves cycle rows open) both came from testing only the
  enable direction. Toggle both ways mid-cycle and assert the state table.
- **connect/disconnect**: #283 (frontend WS reconnects after intentional
  close) and #297 (backend reconnect busy-loop) — test the *teardown*
  direction of every connection path.

### 4b. Timezone matrix

Time bugs here recur (#26, #65, #294, #301). Any experiment involving
timestamps must run at minimum under: UTC, a negative-offset zone
(`America/Chicago`), and ideally across a DST boundary. Predict the exact
shift (§1b). Naive-UTC-rendered-as-local is the house pathology; the tz
helpers live in `smart_vent/backend/tz.py`.

### 4c. Restart-mid-state survival

The engine persists cycle state and restores it on startup. Any experiment
that changes cycle/hold/override state must include: kill the backend
mid-cycle → restart → verify the state restored *and* the invariant still
holds. Precedents: #300 (restore leaked overflow-room state because
`ON CONFLICT` dropped `role`/`joined_at`), #237's fix persisting
`min_runtime_until` specifically so holds survive restarts, #51 (rows left
"Active/running" after stop). A result that only holds within one process
lifetime is not accepted.

### 4d. Simulated pass is necessary, not sufficient

Every sandbox in §2 has fake physics. Real systems differ in ways that have
bitten this project:

- **Thermal mass**: real rooms keep changing temperature after a vent closes
  (#86's post-closure drift) — a simulator that freezes temp at target would
  never have shown the bug.
- **Compressor minimum runtimes and off-times**: real equipment cannot start/
  stop per-tick; the short-cycle guards (#208) exist because of it, and #237's
  thrash only manifests *during* a real minimum-runtime hold.
- **HA entity quirks**: clamping (a 69.8 °C setpoint silently becomes ~35 °C,
  #280), unsupported cover services (#57), entities going unavailable
  mid-cycle (#267).

So the ladder is: unit test → integration/fake-HA → Dev Mode against real
sensors → System On on live equipment, watched. Claim success only at the
rung you actually reached, and say which rung in the PR.

### 4e. Don't let the instrument lie

Two recurring traps: mocks that encode the wrong reality (#280, #281 — the
test asserted the bug), and CI env vars that mask the production path (#281:
compose stacks always pass `TEMPERATURE_UNIT` explicitly, so the blank-auto
path never ran in CI). Before trusting a green run, ask: does any fixture,
env var, or mock differ from production in the exact dimension under test?

---

## 5. Result acceptance checklist (copy into the issue/PR)

```markdown
## Result acceptance — <issue #>

### Mechanism
- [ ] One stated mechanism, in one paragraph, at file:line granularity.
- [ ] It reproduces EVERY observation in the timeline, including negatives
      (things that did NOT happen) and passing tests.
- [ ] Independent co-causes ruled out or separately filed (#281 precedent).

### Prediction vs measurement
- [ ] Numeric prediction written BEFORE measuring (exact value, not a bound).
- [ ] Measured value pasted next to it; matches to the decimal.
- [ ] The falsifying observation was constructed and checked (§1c) — state
      what it was and what you found.

### Experiment provenance
- [ ] Sandbox rung reached: unit / fake-HA / Dev Mode / live (state which).
- [ ] DB experiments ran against a `.backup` copy, never live app.db.
- [ ] No live-HVAC action before a sandbox pass.

### Hygiene
- [ ] Symmetric contract? Both directions tested (F/C, resume, disable,
      disconnect).
- [ ] Timestamps involved? Non-UTC zone tested; shift predicted exactly.
- [ ] State involved? Restart-mid-state survival verified.
- [ ] Fixtures/mocks verified against real payload shapes (#280/#281 trap).
- [ ] If only simulated: limitations vs real HVAC stated (thermal mass,
      min runtimes, entity clamping).

### Landing
- [ ] Change-control class + required evidence met (plenum-change-control).
- [ ] Regression test that fails on the old code, at the mechanism (not a
      symptom snapshot).
- [ ] docs/ page updated or created if user-visible; CHANGELOG entry.
- [ ] If retired instead: refuting observation written into the issue;
      archaeology entry if re-proposable.
```

---

## Provenance and maintenance

Facts verified against the repo on 2026-07-05 (v0.22.1):

- Dev Mode = write interception only, no fake clock/physics:
  `grep -n "dev_mode" smart_vent/backend/ha_client.py` (interception sites)
  and `docs/system-modes.md`.
- System Off semantics (skip ticks unless Dev Mode is on, abort running
  cycle, zero HA calls after the one-time abort): engine gate
  `system_enabled OR dev_mode` at `scheduler.py:477`; details owned by
  **plenum-run-and-operate**. Note `docs/system-modes.md` (~line 26) states
  the precedence backwards — a known doc bug; the code wins.
- Conversion helpers live in `smart_vent/backend/units.py` (`to_f`,
  `delta_to_f`, `from_f`, `from_f_delta`) — consolidated by #251.
  Pre-2026-07-05 CLAUDE.md copies said `_to_f`/`_delta_to_f` in `routes.py`;
  corrected in PR #388 — if docs and repo disagree again, the repo wins
  (see **plenum-architecture-contract**).
  Re-verify: `grep -n "def " smart_vent/backend/units.py`.
- 141.44 double-conversion arithmetic: `python3 -c "print((16*9/5+32)*9/5+32)"`.
- `app.db` path: `smart_vent/backend/main.py:42-43` (`DATA_DIR` default
  `/data`, `app.db`). Re-verify: `grep -n "DATA_DIR" smart_vent/backend/main.py`.
- `temp_offset` field: `smart_vent/backend/models.py:44`. Its origin in #86
  is **inferred** from project history, not traceable in git log.
- Issue narratives (#32, #48, #86, #231, #237, #280–#305, #208–#213,
  #268–#270, #51, #54, #57, #267, #283, #297, #300, #301) summarized from
  the closed-issue record; re-verify with
  `gh issue view <n> --repo dhruvb14/smart-thermostat-with-vents`.
- Line numbers cited inside issue quotes (e.g. `cycle_engine.py:994-1000`)
  are as-of the issue's filing date and will drift; search by symbol name.
