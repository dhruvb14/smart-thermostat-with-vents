---
name: plenum-proof-and-analysis-toolkit
description: First-principles analysis recipes for PROVING a Plenum change is correct before shipping it — unit-conversion algebra (round-trip identity, absolute-vs-delta typing), state-machine exhaustiveness tables, boundary/off-by-one inequality proofs, oscillation/hysteresis analysis, naive-vs-aware timezone audits, asyncio task-retention and reconnect-liveness proofs, and DB-invariant checks (migration idempotence, upsert column preservation). Load when a review comment says "prove it", when touching conversion/engine/trim/reconnect/migration code, or when a bug smells like an off-by-one, a missing state×event cell, or a double conversion. Each recipe has a worked example from a real closed issue with real numbers.
---

# Plenum Proof-and-Analysis Toolkit

Seven recipes for turning "I think this change is right" into "here is why it
cannot be wrong." Each recipe: **when to reach for it → steps → worked example
from this repo's bug history (with real numbers) → you-are-done-when.**

When NOT to use this skill:
- You need to *measure* a running system (queries, metrics, logs) → load **plenum-diagnostics-and-tooling**.
- You want the incident *story* behind a bug number → load **plenum-failure-archaeology**.
- You need the canonical invariant statements (write boundary, °F storage) → load **plenum-architecture-contract**.
- You need to know which tests CI demands for your change class → load **plenum-change-control** / **plenum-validation-and-qa**.

Jargon used below: a **delta** is a temperature *difference* (deadband, offset,
overshoot) as opposed to an **absolute** temperature; **deadband** is the band
around a target inside which no HVAC demand exists; **overshoot** is degrees
past target; **short-cycling** is compressor on/off flapping; a **golden** is a
committed reference screenshot.

All paths below are relative to the repo root. Backend code lives in
`smart_vent/backend/`. Run python checks from `smart_vent/` so `backend.*`
imports resolve.

---

## Recipe 1 — Unit-conversion algebra: prove the round trip

**When to reach for it:** any change that touches a temperature on a write or
display boundary; any new field in `TEMPERATURE_FIELDS`; any reviewer question
of the form "is this a delta or an absolute?".

The four converters are the single source of truth in
`smart_vent/backend/units.py` (as of 2026-07, v0.22.1; CLAUDE.md corrected
2026-07-05 — `routes.py` lines 35-38 just re-import them as
`_to_f` etc.):

| fn | direction | formula (°C mode) |
|---|---|---|
| `to_f(v,"C")` | display→storage, absolute | `v*9/5 + 32`, 2dp |
| `delta_to_f(v,"C")` | display→storage, delta | `v*9/5`, 2dp |
| `from_f(v,"C")` | storage→display, absolute | `(v-32)*5/9`, 1dp |
| `from_f_delta(v,"C")` | storage→display, delta | `v*5/9`, 1dp |

### Steps

1. **Identify the type of every temperature variable** in your diff: absolute
   or delta, display-unit or °F-storage. Write it down. A variable whose type
   you cannot name is the bug.
2. **Prove the round-trip identity** for the pair you use:
   `from_f(to_f(x,"C"),"C") == x` (up to rounding) and likewise for the delta
   pair. Composition of *mismatched* pairs is never identity — that is the
   detector.
3. **Count conversions per path.** Exactly one display→storage conversion
   (backend write boundary) and one storage→display (frontend render). Two of
   the same direction = #231. Zero = #280/#284.
4. **Run the check** (verified working from `smart_vent/`, 2026-07):

```bash
cd smart_vent && python3 -c "
from backend.units import to_f, delta_to_f, from_f, from_f_delta
print('16C ->', to_f(16,'C'))                                    # 60.8  (correct single conversion)
print('double:', to_f(to_f(16,'C'),'C'))                          # 141.44 (the #231 bug, reproduced)
print('roundtrip abs:', from_f(to_f(16,'C'),'C'))                 # 16.0
print('roundtrip delta:', from_f_delta(delta_to_f(1.0,'C'),'C'))  # 1.0
print('2F delta as absolute:', from_f(2.0,'C'), 'as delta:', from_f_delta(2.0,'C'))  # -16.7 vs 1.1 (the #291 bug)
"
```

### Worked example A — #231 double conversion, step by step

User types 16 °C for `min_setpoint`.

- **Correct:** frontend sends `16` raw → backend `_to_f(16,"C")` =
  `16 × 9/5 + 32` = `28.8 + 32` = **60.8 °F** stored. Display:
  `(60.8−32)×5/9 = 16.0 °C`. Identity holds.
- **Buggy (pre-fix):** frontend called `toStorage(16)` = 60.8, sent `60.8`;
  backend applied `_to_f(60.8,"C")` = `60.8 × 9/5 + 32` = `109.44 + 32` =
  **141.44 °F** stored. Display: `(141.44−32)×5/9 = 60.8 °C`. The round trip
  returns `toStorage(x)`, not `x` — the composition `to_f∘to_f` is affine with
  slope 81/25 and can never be identity. That algebraic fact is the proof the
  conversion must live on exactly one side.

### Worked example B — #291 delta-vs-absolute typing

`avg_overshoot_f = 2.0` (a room ended 2 °F past target — a **delta**). The
histogram subtitle formatted it with the *absolute* converter:
`(2 − 32) × 5/9 = −16.7 °C` — a physically absurd negative overshoot. The
correct delta conversion is `2 × 5/9 = 1.1 °C`. Typing rule: **any quantity
that is a difference of two temperatures transforms without the 32 offset**
(the offset cancels in the subtraction: `(a−32)·5/9 − (b−32)·5/9 =
(a−b)·5/9`). The fix (`MetricsCharts.tsx` now routes through `toDisplayDelta`,
verified 2026-07) plus the module docstring in `units.py` encode this.
Sibling delta bugs: #292 (degree-minutes magnitudes), CLAUDE.md pitfall #4.

**You are done when:** every temperature variable in the diff has a named type
(absolute/delta × display/storage), each path has exactly one conversion per
direction, and the round-trip python check above prints identities for the
pairs you touched.

---

## Recipe 2 — State-machine exhaustiveness: enumerate states × events

**When to reach for it:** any change to `CycleEngine` lifecycle, scheduler
engine management, or anything that adds a new way for a cycle to start, end,
or lose its inputs.

### Steps

1. **Get the real states.** `CycleState` in
   `smart_vent/backend/engine/cycle_engine.py` (~line 71) has exactly three
   enum members: `IDLE`, `RUNNING`, `TERMINATING` (as of 2026-07, v0.22.1).
   The docstring's `ABORTED`/`TERMINATED` are *exit reasons* recorded in
   `cycle_logs.ended_reason`, not enum states. Do not invent states.
2. **Enumerate the events.** From code, not memory: 60s tick, room set change,
   trigger change (same rooms, new target/source — #215's in-place update),
   thermostat unavailable, thermostat available again, system disable,
   vacation on, engine deleted (room/thermostat removed), process restart
   (restore path), cycle timeout, min-runtime hold expiry.
3. **Draw the table** — 3 states × N events. For each cell, point at the line
   of code that handles it or write **MISSING**. "The early-return skips it"
   counts as MISSING if safety logic lives past the early return.
4. Every MISSING cell is either provably unreachable (say why) or a bug.

### Worked example A — #267 as a missing cell (RUNNING × thermostat-unavailable)

Pre-fix, `_do_tick` returned immediately when the climate entity read
`unavailable`. The cell (RUNNING, unavailable) fell through to "do nothing
forever": timeout check, `max_vent_closed_min` watchdog, and reconciliation
all live *after* the early return, so a sustained outage left the physical
HVAC running at the commanded overshoot setpoint with satisfied rooms' vents
closed, indefinitely. The docstring even claimed `RUNNING → ABORTED
(thermostat unavailable)` but no code path did it — the dead
`_abort_cycle(safe_close=True)` had zero production callers and zero test
coverage across 733 tests. The exhaustiveness table finds this in minutes: the
cell is blank and the docstring says it shouldn't be. Fix (verified in code
today, `cycle_engine.py` ~lines 271-313): `_unavailable_since` tracks the
outage start; after `tc.unavailable_abort_after_min` minutes (default 5, 0 =
never) the cycle aborts and vents reopen (vent entities are independent of
the climate entity, so those commands still work).

### Worked example B — #285 as a missing cell (RUNNING × engine-deleted)

`Scheduler._sync_engines` did `del self._engines[tid]` when the last room
referencing a thermostat was removed — with no abort. The orphan-closing
safety net in `_reset_and_reevaluate` only iterates engines *still in the
map*, so a just-deleted engine's open `cycle_log` row could never be caught:
thermostat stuck at the overshoot setpoint, vents stuck, UI showing a
permanently "Active" cycle. The event "engine removed" existed in the code but
the state RUNNING was never consulted before removal. Fix (verified today,
`scheduler.py` ~lines 497-503): `force_abort(reason="thermostat removed")` +
`db.close_open_cycle_logs(tid)` before the `del`. Related same-shape bug: #286
(exceptions *before* `engine.tick()` swallowed — a tick event that silently
becomes a no-event).

**You are done when:** the table has no unexplained blank cells, every "cannot
happen" claim cites the guard that prevents it, and each newly filled cell has
a test (see #267's test list: transient outage no-abort, sustained outage
abort+reopen, counter reset on recovery, IDLE+unavailable no-crash).

---

## Recipe 3 — Boundary / off-by-one proofs: solve the inequality

**When to reach for it:** any cap/trim/limit/pagination/retention SQL or loop;
any `<` vs `<=` review comment; any "keeps N" claim.

### Steps

1. **Write the keep-set explicitly.** "Trim to cap 5000" means: after the
   statement, exactly `min(rowcount, 5000)` rows remain, and they are the
   newest ones.
2. **Substitute the boundary values** into the predicate by hand: the
   below-cap case, the exactly-at-cap case, cap+1.
3. **Check both directions** of the postcondition: nothing kept that should be
   deleted AND nothing deleted that should be kept. Off-by-ones live in the
   second direction.
4. **Run it against real SQLite** — 5 lines, no app needed.

### Worked example — #299 event_log trim

The (fixed) SQL in `smart_vent/backend/db.py` (`insert_event_log`, ~line 2300,
`_EVENT_LOG_MAX = 5000`, runs every 100th insert):

```sql
DELETE FROM event_log WHERE id < (
    SELECT MIN(id) FROM (
        SELECT id FROM event_log ORDER BY id DESC LIMIT ?   -- 5000
    )
)
```

The subquery selects the newest ≤5000 ids; `MIN` of that set is the **oldest
row of the keep-set** — call it `k`. The keep-set is exactly `{id ≥ k}`, so
the delete predicate must be `id < k`. The original code used `id <= k`,
which deletes `k` itself — the oldest row *of the set we decided to keep*:

- **At/above cap:** keeps 4999 rows, not 5000. Cosmetic.
- **Below cap** (the nasty case): the subquery returns *all* rows, so `k` is
  simply the oldest event in the table, and `id <= k` deletes it. A quiet
  install below the cap silently lost its oldest event on every 100th insert,
  forever, despite never reaching the cap. The inequality analysis catches
  this because the below-cap substitution makes the keep-set "everything" and
  the correct delete-set empty — any nonempty delete is wrong.

Runnable proof (executed 2026-07; output shown):

```bash
python3 -c "
import sqlite3
for op,label in (('<=','buggy'),('<','fixed')):
    c=sqlite3.connect(':memory:'); c.execute('CREATE TABLE event_log(id INTEGER PRIMARY KEY)')
    c.executemany('INSERT INTO event_log VALUES(?)',[(i,) for i in range(1,101)])
    c.execute(f'DELETE FROM event_log WHERE id {op} (SELECT MIN(id) FROM (SELECT id FROM event_log ORDER BY id DESC LIMIT 5000))')
    print(label, c.execute('SELECT COUNT(*),MIN(id) FROM event_log').fetchone())
# buggy (99, 2)  ← lost row 1 while 4900 under cap
# fixed (100, 1)
"
```

Adjacent boundary bug in the same audit wave: #302 — offset pagination
duplicates rows when new events arrive between pages (the boundary is a
*moving* one; keyset pagination `WHERE id < last_seen` is the fix shape).

**You are done when:** you have hand-substituted below-cap / at-cap / cap+1
and each yields the specified keep-set, and an in-memory sqlite (or 5-line
python) run reproduces the spec for all three.

---

## Recipe 4 — Oscillation / hysteresis analysis: prove the loop terminates

**When to reach for it:** any change to demand thresholds, deadband handling,
mode selection, cycle termination, or overflow candidate selection. The
question is always: *after this cycle ends, what immediately re-triggers, and
in which direction?*

### The model (as implemented today, verified 2026-07)

- **Demand (cycle start)** — `_suppression_vote` in `cycle_engine.py`
  (~line 1662): vote `cool` iff `effective > target + deadband`, `heat` iff
  `effective < target - deadband`, else `off`. Demand fires only *outside*
  the band.
- **Completion (cycle end)** — `_is_at_target` (~line 3608): cooling done iff
  `avg ≤ target`, heating done iff `avg ≥ target`. **Exact target, no
  deadband** — that asymmetry IS the hysteresis.
- **Direction at start** — `_infer_mode_from_room_temps` (~line 1700): rooms
  vote from their own sensors vs targets; majority wins, ties go cooling; the
  vote is sanity-checked against thermostat ambient (#38's fix) — a `heating`
  vote with ambient above every `target + deadband` is flipped with a warning.
- **Direction mid-cycle** — locked in `_cycle_mode` at start and used for all
  monitoring; live `hvac_action` is never re-read for direction (#26/#29), and
  `_filter_rooms_for_mode` drops any room that starts demanding the opposite
  direction rather than flipping the cycle (#215).

### Steps

1. Write the **re-trigger condition**: after a cooling cycle ends at exactly
   `target`, a new cooling cycle needs drift **up** across `target +
   deadband`; the *opposite* (heating) cycle needs drift **down** across
   `target − deadband`. The gap between end-state and either trigger is the
   hysteresis margin — it must be > sensor noise + one-tick drift.
2. **Set deadband = 0 and watch it break**: end at `target`, and any noise
   ε > 0 in either direction is instant demand — `effective > target + 0`
   re-fires cooling, `effective < target − 0` fires heating. Zero margin =
   oscillation guaranteed. This is why `deadband` exists and why the #70 fix
   (drop deadband from *completion*, keep it for *starting*) is safe: it
   moves the endpoint to the exact target but leaves the full ±deadband
   margin on the re-trigger side.
3. For direction logic, prove the **flip-flop terminates**: trace two
   consecutive cycle boundaries and show the second cannot pick the opposite
   direction unless the temperature genuinely crossed the whole band.

### Worked example A — #29 boundary flip-flop and why mode-locking alone didn't fix it

heat_cool thermostat, target 70 °F, overshoot 2 °F. Cooling cycle drives room
75 → 68 (setpoint 70−2), terminates, presence holdover keeps the room active.
Next tick, pre-fix code took direction from live `hvac_action`: the
thermostat at 68 was below its own heat setpoint → `heating` → engine heats
to 72 → terminates → next tick `hvac_action="cooling"` → cools to 68 →
forever. `_cycle_mode` couldn't help: it locks direction *within* a cycle and
is cleared at termination; the oscillation lives at the *boundary between*
cycles. Termination proof for the fix: direction now comes from the room-vote,
and at 68 with target 70 and deadband d ≥ 0.5, `68 < 70 − 0.5` votes `heat`
only if that is *true room demand* — and once the room re-enters
`[70−d, 70+d]` the vote is `off` and **no cycle starts at all**. The
flip-flop requires the temperature to fully cross a 2d-wide band between
ticks, which bounded drift cannot do.

### Worked example B — #237/tier-3 headroom math (overflow conditioning)

During a minimum-runtime hold (goal met but compressor must keep running to
avoid short-cycling), surplus air goes to non-active rooms in three tiers
(`docs/overflow-conditioning.md`, `get_overflow_candidates` in
`room_manager.py`). Tier 3 fires only when every candidate is already at/past
goal, and picks by **headroom** — distance to the *opposite-direction
trigger*:

- Cooling: `headroom = current_temp − (effective_setpoint − deadband)`.
  Example: room at 74 °F, setpoint 72, deadband 1 → `74 − 71 = 3 °F` of
  cooling absorbable before the room would vote `heat`.
- Heating: `headroom = (effective_setpoint + deadband) − current_temp`.

Zero-or-negative headroom rooms are **excluded** — dumping air into them would
manufacture the opposite-direction demand the hysteresis band exists to
prevent (oscillation by proxy). Note #305: tier math must use each room's
`_effective_deadband` (per-room `deadband_override`), not the thermostat's.

Worked example C, one line: #70 itself — completion at `target + deadband`
(cooling) meant a 72° target with 1° deadband declared done at 73°, i.e. the
system used its hysteresis margin as its goal. Separating "start threshold"
(band edge) from "stop threshold" (target) is the general fix shape.

**You are done when:** you can state the re-trigger inequality for both
directions after your change, the margin between endpoint and each trigger is
strictly positive (and ≥ realistic sensor noise), and a two-boundary trace
shows no direction flip without a genuine full-band crossing.

---

## Recipe 5 — Timezone reasoning: the naive-vs-aware audit

**When to reach for it:** anything touching timestamps — new stored datetime,
new UI rendering of a time, schedule matching, date bucketing, `datetime`
import in a diff.

### The convention today (verified by running the greps, 2026-07, v0.22.1)

- **Storage**: aware `datetime.now(UTC)`, then stripped to a **naive-UTC ISO
  string** at the DB boundary — `db.py` `_dts()` (~line 454) does
  `dt.replace(tzinfo=None).isoformat()`. 50 call sites of
  `datetime.now(UTC)` in non-test backend code; **zero** `datetime.utcnow()`
  (deprecated form fully purged).
- **Local wall-clock** (schedule matching, "today" rollups): only via
  `smart_vent/backend/tz.py` — `get_timezone()` (from the `TZ` env var set by
  `run.sh`), `now_local()`, `today_local()`, `to_local()`, `to_local_naive()`.
  The only `datetime.now()` (no arg) in non-test backend code are the
  documented-intentional ones in `tz.py:38` (OS-zone fallback) and `db.py:276`
  (the #65 data-migration offset computation, `noqa: DTZ005`).
- **Frontend**: backend strings are naive-UTC, so every render must append
  `"Z"` before parsing: `new Date(ts + "Z").toLocaleString()` (the
  Logs.tsx/DevMode.tsx pattern).

### Steps — the audit greps (run them; expected shape as of 2026-07)

```bash
cd smart_vent/backend
grep -rn "utcnow()" --include="*.py" . | grep -v tests          # expect: empty
grep -rn "datetime\.now()" --include="*.py" . | grep -v tests   # expect: only tz.py:38, db.py:276
grep -rn "datetime.now(UTC)" --include="*.py" . | grep -v tests | wc -l   # ~50 — the storage norm
grep -rn "astimezone" --include="*.py" . | grep -v tests        # expect: only inside tz.py
```

Any new hit outside those allowlists is your finding. On the frontend, grep
for timestamp fields rendered without the `+ "Z"` treatment.

### Worked examples

- **#65** — `room_manager.py` used `datetime.now()` (naive **local**) for
  `holdover_expires_at` while everything else stored naive UTC. Frontend
  appended `"Z"` (assumes UTC), so a server-local `08:28` EDT became `08:28
  UTC` → displayed `04:28` EDT: the offset applied **twice**, once by the
  wrong producer and once by the honest consumer. The mixed-convention grep
  above finds this class instantly. The cleanup left behind a sentinel-gated
  data migration (`_migrate_holdover_timestamps_to_utc` in `db.py`).
- **#301** — the inverse consumer bug: vent-timeline rendered the naive-UTC
  string **verbatim** (no `+ "Z"`), so events appeared shifted by the
  browser's UTC offset relative to the (correctly converted) cycle times on
  the same page. Rule: naive-UTC producer ⟹ every consumer must add `"Z"`.
- **#294** — a third shape: `datetime-local` inputs interpret `min=` as
  **local**, but the code built it with `toISOString()` (UTC). West of UTC
  (offset −5) at 10:00 local the computed min was 15:01 local — 5 valid hours
  rejected by the picker. East of UTC the min lands harmlessly in the past,
  which is why only western users saw it. Boundary check for any tz fix:
  reason through one zone on *each side* of UTC, never just your own.

**You are done when:** the four greps return only the documented allowlist,
every new stored timestamp is `datetime.now(UTC)` (stripped by `_dts` at the
DB edge), every new local-time computation goes through `tz.py`, every new
frontend render appends `"Z"`, and you traced one westward and one eastward
UTC-offset example by hand.

---

## Recipe 6 — Concurrency & lifecycle: task retention and loop liveness

**When to reach for it:** any `asyncio.create_task`, background loop,
reconnect logic, or fire-and-forget coroutine in a diff.

### 6a. Task-retention proof (#304)

The asyncio event loop holds only **weak** references to tasks: a task whose
returned handle you drop can be garbage-collected mid-await, silently
cancelling the work. Proof obligation for every `create_task`: *name the
strong reference that outlives the task.*

Worked example: `scheduler.py` created
`asyncio.get_event_loop().create_task(self._startup_resolve_unit())` and
dropped the handle. That coroutine waits up to 60 s on `ha.wait_connected()`
— a long GC window; if collected, the HA temperature unit is never resolved
for the session and a stale DB unit stays active (a silent °C/°F hazard).
The fix — verified in `scheduler.py` today (~lines 68-75) — is the canonical
retention idiom:

```python
self._bg_tasks: set[asyncio.Task] = set()
task = asyncio.create_task(coro)
self._bg_tasks.add(task)
task.add_done_callback(self._bg_tasks.discard)
```

(`main.py` keeps its handles on the app: `app["ha_task"]`,
`app["ha_log_task"]`.) Audit grep: `grep -rn "create_task" backend/ | grep -v
tests` — every hit must assign to something that survives.

### 6b. Reconnect-loop liveness (#297)

Two dual proof obligations for any retry loop:
1. **No busy-spin** — every path around the loop (including the *success*
   path returning early!) reaches a sleep. Pre-fix, `_connect()` returning
   normally on a clean close skipped the sleep AND reset backoff to 1 → HA
   restarting = connect/auth/full-state-fetch/subscribe/close at full speed.
2. **No permanent stall** — every `await` inside the loop body is bounded.
   Pre-fix, the handshake `receive_json()` had no timeout and no heartbeat: a
   half-open socket that never sent `auth_required` blocked `start()` forever,
   so the retry loop itself never ran again.

Verified fix in `ha_client.py` (constants at ~lines 40-56, loop ~100-126):
`_MIN_RECONNECT_DELAY_S=1`, `_MAX_RECONNECT_DELAY_S=60` (exponential ×2);
the sleep executes on **every** loop iteration, clean close included; backoff
resets only if the connection stayed up ≥ `_STABLE_CONNECTION_S=30` (measured
via `time.monotonic()` from `_connected_since`); handshake/subscribe receives
wrapped in `asyncio.wait_for(..., _HANDSHAKE_TIMEOUT_S=10)`;
`ws_connect(heartbeat=_WS_HEARTBEAT_S=30)`. Frontend sibling: #283 — the
inverse liveness bug, a loop too alive (reconnecting after *intentional*
close → duplicated handlers, duplicated live-feed rows).

**You are done when:** every `create_task` in the diff has a named strong
reference with a discard path; every retry loop provably sleeps on all paths
and provably cannot block forever inside one iteration (each await bounded by
timeout or heartbeat); and backoff reset is conditioned on stability, not on
mere connection success.

---

## Recipe 7 — DB invariants: migration idempotence and upsert preservation

**When to reach for it:** any change to `db.py` schema, `_MIGRATIONS`, or an
`ON CONFLICT` clause.

### 7a. Migration idempotence (#21 pattern — still open debt)

There is no schema-version table. `init_db` (`db.py` ~line 251) runs
`executescript(SCHEMA)` (all `CREATE ... IF NOT EXISTS`), then executes every
entry of the `_MIGRATIONS` list (~line 357, plain `ALTER TABLE` strings) under
`try/except Exception: pass` — "duplicate column name" on an already-migrated
DB is swallowed, which is what makes re-running safe. Obligations this design
imposes on every new migration:

1. **Idempotent-by-failure**: the statement must be one whose re-execution
   error is safe to swallow (`ADD COLUMN`, `DROP COLUMN`). A data-mutating
   `UPDATE` in that list would re-run on *every* startup — data migrations
   instead use the sentinel pattern (`_migrate_holdover_timestamps_to_utc`
   writes `migration_holdover_timestamps_utc_v1` into `system_settings` and
   early-returns if present).
2. **Additive only, with a default**: rows exist mid-upgrade; `NOT NULL`
   needs `DEFAULT`. Check the default is the *pre-feature behaviour* (e.g.
   `unavailable_abort_after_min DEFAULT 5` chose a new safety default
   deliberately — that is a decision, record it).
3. **Append, never reorder/edit** shipped entries — the bare `except` also
   hides genuine typos, so a broken new migration fails *silently*. Proof:
   run the suite (it inits fresh DBs), then init twice against one file:
   `python3 -c "import asyncio,aiosqlite; from backend import db;
   asyncio.run((lambda: ...)())"` — or simplest, run
   `pytest backend/tests/ -k db` from `smart_vent/` and additionally call
   `init_db` twice in a scratch test. Double-init must be a no-op.

### 7b. Upsert preserves columns (#300)

An `INSERT ... ON CONFLICT DO UPDATE` silently **keeps the old value of every
column you omit from the `SET` list** — and silently **overwrites** every one
you include. Both directions can be bugs. Proof technique: for each column,
ask "on conflict, which value must win — old, new, or first-non-null?" and
check the clause encodes exactly that.

Worked example: `upsert_room_cycle_state` omitted `role` and `joined_at`.
When a `role='overflow'` room started genuinely demanding HVAC and was
promoted into the running cycle, the engine's fresh `role='active'` INSERT
conflicted and the row **silently stayed `'overflow'`** with a stale
`joined_at` — excluded from metrics, mislabeled in the Logs UI, and its
end-state later clobbered by `_finalize_overflow_rooms`. The fixed clause
(verified in `db.py` ~line 1198 today) is a three-policy exhibit:

```sql
ON CONFLICT(cycle_id,room_id) DO UPDATE SET
  target_temp=excluded.target_temp,          -- new wins
  reached_at=excluded.reached_at,
  vent_closed_at=excluded.vent_closed_at,
  temp_at_end=excluded.temp_at_end,
  trigger_detail=COALESCE(excluded.trigger_detail, room_cycle_states.trigger_detail),
  role=excluded.role,                        -- promotion must relabel (the #300 fix)
  joined_at=COALESCE(room_cycle_states.joined_at, excluded.joined_at)  -- first write wins
```

`temp_at_start` is deliberately absent (old wins — the overflow re-open path
depends on it). Note the two COALESCE directions are opposite: prefer-new for
`trigger_detail`, prefer-old for `joined_at`. Every upsert edit should produce
a per-column table like this in the PR description.

**You are done when:** (7a) `init_db` run twice on the same file changes
nothing, every new `_MIGRATIONS` entry is additive-with-default, and any data
migration is sentinel-gated; (7b) every column of the target table appears in
your old/new/first-non-null decision table and the SQL matches it, with a test
that upserts twice with different values and asserts each column's policy.

---

## Provenance and maintenance

Written 2026-07-05 against v0.22.1 (`smart_vent/config.yaml` `version:`).
Sources: repo code (all line references verified by reading the files on this
date), closed issues #21 #26 #29 #38 #48 #65 #70 #215 #231 #237 #254 #267
#280 #283 #284 #285 #286 #291 #292 #294 #297 #299 #300 #301 #302 #304 #305,
`docs/overflow-conditioning.md`, `smart_vent/backend/tz.py` and `units.py`
module docstrings. Both embedded python checks (Recipe 1 and Recipe 3) were
executed on this date and printed the outputs shown. The Recipe 7a double-init
check could NOT be executed here (aiosqlite not installed in this
environment) — treat it as the re-verification command, not an executed
result; `pip install ".[dev]"` from `smart_vent/` first.

Volatile facts — re-verify before trusting:

| Fact | Re-verify with |
|---|---|
| Converters live in `backend/units.py` (CLAUDE.md drift) | `grep -n "def to_f" smart_vent/backend/units.py` |
| `CycleState` = IDLE/RUNNING/TERMINATING only | `grep -n -A4 "class CycleState" smart_vent/backend/engine/cycle_engine.py` |
| Unavailability abort default 5 min, field `unavailable_abort_after_min` | `grep -n unavailable_abort_after_min smart_vent/backend/db.py` |
| Trim uses `id <`, `_EVENT_LOG_MAX=5000`, every 100th insert | `grep -n -B2 -A8 "_EVENT_LOG_MAX" smart_vent/backend/db.py` |
| Demand/completion thresholds (band-edge start, exact-target stop) | `grep -n -A8 "def _is_at_target\|if effective > target" smart_vent/backend/engine/cycle_engine.py` |
| tz greps allowlist (tz.py:38, db.py:276 only) | rerun the Recipe 5 grep block |
| Backoff constants 1/60/30/10/30 in ha_client.py | `grep -n "_S = " smart_vent/backend/ha_client.py` |
| `_bg_tasks` retention idiom in scheduler.py | `grep -n "_bg_tasks" smart_vent/backend/scheduler.py` |
| ON CONFLICT clause of `upsert_room_cycle_state` | `grep -n -A9 "ON CONFLICT(cycle_id,room_id)" smart_vent/backend/db.py` |
| Cycle-engine line numbers (~271 guard, ~1662 vote, ~1700 infer, ~3608 at-target) | drift-prone; re-grep the symbol, not the line |
