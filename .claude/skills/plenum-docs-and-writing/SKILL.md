---
name: plenum-docs-and-writing
description: How to maintain Plenum's documents of record (README.md, DESIGN.md, CLAUDE.md, docs/*.md, CHANGELOG.md, RELEASE.md, screenshots/), the house prose style, and the external-claims discipline (test counts, coverage numbers, heat-pump non-support). Load when writing or editing any doc, refreshing the README "Tested" stats block, adding a docs/ feature page with a feature PR, amending CLAUDE.md after a hard-won lesson, or checking whether a documented number/path still matches the repo. Do NOT load for CI/release mechanics (plenum-ci-and-release) or PR gating rules (plenum-change-control).
---

# Plenum docs and writing

Plenum is a solo-maintained open-source Home Assistant add-on that drives real HVAC
equipment. Its documentation therefore carries two unusual burdens: **external claims
must be exactly true** (people trust it with compressors), and **the docs rot fast**
because one person ships quickly. This skill defines what each document is for, the
house style, and the update discipline that keeps claims and reality in sync.

When NOT to use this skill:
- Release/CI workflow mechanics → **plenum-ci-and-release**
- What evidence a PR needs, non-negotiable engineering rules → **plenum-change-control**
- The temperature-unit contract itself → **plenum-architecture-contract** (this skill
  only covers how to *write about* it)
- Adding a config knob (parity checklist) → **plenum-config-and-flags**

---

## 1. Document inventory — what each file is, and its maintenance rule

| File | Status | Maintenance rule |
|---|---|---|
| `README.md` | **Living, user-facing.** Install, first-time setup, safety, screenshots, "Tested" stats. | Update with every user-visible feature. Stats block: see §4 runbook. |
| `DESIGN.md` | **Historical artifact.** The original pre-implementation design. | Do NOT update. Do NOT cite as truth. See §1.2. |
| `CLAUDE.md` | **Operational rulebook** for AI-assisted development. | Amend after every hard-won lesson (§1.3 checklist). 2026-07-04 stale spots fixed 2026-07-05 (§1.3). |
| `docs/README.md` | Index of feature guides + repo-wide conventions (°F storage, timezone, entity-ID format). | Every new `docs/*.md` page gets an index line with a one-clause summary. |
| `docs/*.md` | **One page per feature**, shipped in the same PR as the feature. 14 pages (as of 2026-07, v0.22.1). | New feature ⇒ new page or a section in the owning page, in the *feature's* PR — never "docs later". |
| `smart_vent/CHANGELOG.md` | **Machine-generated** by `.github/workflows/release-pr.yml`. | Never hand-edit. See §1.4 for the generation mechanism. |
| `RELEASE.md` | Release runbook (tag → release PR → merge). | Update when workflows change. 2026-07 drift fixed 2026-07-05 (§1.5). |
| `screenshots/` | README images, named `NN_PageName.png` (`01_Dashboard.png` … `10_Cycle_History.png`). | Zero-padded number = README order. Retake when a page's UI changes materially; keep the numbering scheme. |
| `e2e/screenshots/` | **Not documentation** — visual-regression goldens, auto-committed by CI. | Never confuse with `screenshots/`. Owned by plenum-ci-and-release. |

### 1.1 README.md specifics

- **AI-generated disclaimer stays first.** The blockquote at the top ("this project —
  code, tests, and documentation — was developed with substantial help from AI coding
  assistants...") is a deliberate honesty stance. Never remove or soften it. Note the
  asymmetry with §3: the *repo README* discloses AI involvement; *commits/PRs/issues*
  never attribute individual changes to AI. Disclosure is global, not per-change.
- **Heat-pump non-support stays prominent.** It appears in the Safety section as a
  bold blockquote ("**Heat pumps are not supported.**"). Any rewrite of README or
  `docs/safety.md` must keep this above the fold of the safety content. Plenum's
  cooling lockout and overshoot logic assume a conventional furnace + AC compressor;
  advertising heat-pump compatibility would be an equipment-damaging oversell.
- **The "Tested" stats block** (README §Tested) is the highest-risk external claim.
  Rules: it is date-stamped ("Stats last updated: <Month Year>"), coverage thresholds
  **only ever increase** (they are CI-enforced ratchets — quote the enforced number,
  never a hoped-for one), and every count must be reproducible by a command (§4).
- Known gap (as of 2026-07): README's Documentation list omits
  `docs/overflow-conditioning.md`, which `docs/README.md` does index. When editing
  that list, re-diff it against `ls docs/*.md`.

### 1.2 DESIGN.md — historical, not truth

DESIGN.md is the **original design document**, kept as a record of intent. It is not
maintained and materially disagrees with the shipped system. Verified drift examples
(as of 2026-07, v0.22.1):

| DESIGN.md says | Reality |
|---|---|
| MCP transport: "stdio (local) or SSE (remote)", launched as `mcp_server.py` with its own HA_URL/HA_TOKEN | MCP is served over **Streamable HTTP on port 9099** inside the add-on process, off by default, tools generated from the OpenAPI spec (#372, PR #375; see `docs/mcp.md`) |
| Cycle monitor: "MONITOR (30s tick + sensor state change)" | The engine ticks every **60s** (see `docs/cycle-engine.md`, CLAUDE.md) |
| "Frontend UI (5 pages)": Dashboard, Rooms, Schedules, Settings, Logs | **7 pages**: Dashboard, Rooms, Schedules, Thermostats, Logs, Metrics, DevMode (`smart_vent/frontend/src/pages/`) |
| Project structure rooted at `/backend`, `/frontend` | Everything lives under `smart_vent/` |

Rules: never cite DESIGN.md to justify a change; never "fix" it piecemeal (a
half-updated design doc is worse than a dated one). If a reader could be misled, the
correct fix is a pointer in the *living* doc, not surgery on the artifact.

### 1.3 CLAUDE.md — the rulebook, and how it rots

CLAUDE.md is the highest-leverage doc in the repo: every AI session obeys it
verbatim, so an error there is replicated into code. Two consequences:

**The capture pattern.** After any painful campaign, distill the lessons into
CLAUDE.md in a dedicated docs commit. Canonical example: commit `5ea05c5`
("docs: capture E2E visual-regression lessons in CLAUDE.md") — 13 lines recording the
`<Frozen>`/`isCI` determinism mechanism, dual-unit goldens, the `us_customary`
fixture requirement behind the 158°F bug, the golden-commit race fix, and a matching
"Common pitfalls" entry. That is the template: *mechanism + why + the incident it
came from + a pitfall entry*, written while the pain is fresh.

**The rot pattern.** CLAUDE.md states facts (paths, thresholds, workflow names) that
later move, and nothing forces an update. Worked example: the 2026-07-04 audit
found these stale spots, all **fixed in CLAUDE.md on 2026-07-05 (PR #388)** —
kept here as the canonical example of how CLAUDE.md facts rot:

| Stale CLAUDE.md claim (pre-2026-07-05) | Actual |
|---|---|
| Temp helpers `_to_f`/`_delta_to_f`/`_from_f` live in `backend/api/routes.py` | They live in `smart_vent/backend/units.py` as `to_f`/`delta_to_f`/`from_f`/`from_f_delta`; `routes.py` imports them under the underscore aliases (routes.py lines 35–38) |
| Visual-regression suite is `.github/workflows/e2e.yml` | No `e2e.yml` exists; the visual matrix moved **into `container-ci.yml`** (v0.21.0, PR #366 — the workflow's own header comment says "was e2e.yml") |
| Backend coverage `fail_under = 90` | `92.5` (`smart_vent/pyproject.toml`) |
| Frontend coverage 80.9 / 71.1 / 77.1 / 80.9 | lines 90 / functions 85 / branches 72 / statements 87 (`smart_vent/frontend/vite.config.ts`) |

**CLAUDE.md amendment checklist** (use for every edit):

1. Is this a *lesson* (mechanism + incident) or a *fact* (path, number, name)?
   Lessons age well; bare facts rot. For facts, prefer stating *where to look*
   ("thresholds in `vite.config.ts`") over the literal value when the value churns.
2. Cite the incident: issue/PR number inline (the #231 sections are the model).
3. Add a matching "Common pitfalls" entry if the lesson is a trap someone will
   re-fall into.
4. Grep CLAUDE.md for every path/number your change moved — the rot table above
   exists because steps like this were skipped. `grep -n "e2e.yml\|fail_under\|80.9" CLAUDE.md`
   style spot-checks are cheap.
5. Never contradict the temperature-unit contract or weaken a safety rule in prose;
   those changes go through plenum-change-control first, docs second.
6. Docs-only commit, imperative subject prefixed `docs:` (matches `5ea05c5`).

### 1.4 CHANGELOG.md — generated, never hand-edited

`smart_vent/CHANGELOG.md` is written by `.github/workflows/release-pr.yml` when a
`v*.*.*` tag is pushed. Mechanism (verified in the workflow, 2026-07):

1. Finds the previous tag (`git tag --sort=-v:refname`), scrapes `#NNN` references
   from commit subjects in `PREV_TAG..HEAD`.
2. For each number, `gh pr view` — issue-only numbers are silently skipped, and PRs
   titled `Release vX...` are filtered out as housekeeping.
3. Bullets are `- <PR title> ([#issue](...), [#PR](...))` with issue links taken from
   the PR's `closingIssuesReferences`. **So the PR title IS the changelog entry** —
   write PR titles as user-facing sentences (see §3).
4. Contributors come from `git log --format='%aN'` with a GitHub email search.
5. The new `## X.Y.Z` section (with `### Added` and `### Contributors`, ending in
   `---`) is prepended to CHANGELOG.md and the same notes populate the GitHub Release.

Rules: never hand-edit the format or insert sections manually — the next release
prepends above whatever is there and hand-edits create inconsistent structure. If an
entry is wrong, the fix was a bad PR title; fix future titles, not history. Fallback
bullet when no PRs found: "Miscellaneous improvements and fixes".

### 1.5 RELEASE.md — a worked example of runbook rot (fixed 2026-07-05)

The tag → release-PR → merge flow it describes was always correct, but after the
CI consolidation (#330–#337) its "What triggers what" table still routed release
image builds through `docker.yml build-release`, and "Merging the release PR"
named `Build & Push release image` as the required check — both replaced by
`container-ci.yml`'s **Build (PR validation)**. Corrected in PR #388 (2026-07-05).
The lesson stands: when you touch workflows, update RELEASE.md's table in the
same PR.
If you touch RELEASE.md, fix that table against the actual workflows in
`.github/workflows/`.

---

## 2. House style (observed in docs/, follow it)

- **Headings**: sentence case, noun phrases ("Short-cycle protection", "How the
  minimum-runtime hold works"). Not Title Case, not imperative.
- **Page shape**: `# Feature name` → one opening paragraph that states *what and why*
  (including the physical/equipment stake, e.g. "short-cycling is a primary
  equipment-failure mode") before any *how* → scope callout → sections.
- **Callouts**: blockquote with a bold lead, e.g.
  `> **Scope:** This logic runs **only** during ...` (overflow-conditioning.md) or
  `> **Heat pumps are not supported.** ...` (safety.md). Use for scope limits,
  safety warnings, and upgrade caveats.
- **Settings tables**: `| Setting | Default | What it does |` with bold setting
  names, explicit defaults including "0 (disabled)", and recommended values inline
  ("Recommended **10 min**"). See `docs/safety.md`, `docs/thermostat-settings.md`.
- **°F-first**: temperatures are quoted in °F with the unit symbol ("70 °F target is
  satisfied at 69.5–70.5 °F"); deltas are marked `±°F`. The °C story is told once, in
  the conventions section of `docs/README.md` ("converted on ingest") and wherever
  the unit system itself is the topic — feature pages just write °F.
- **Inline issue citations**: bare `#NNN` references tie prose to history
  ("the pre-#213 `min_open_vents` default", "(#372)"). Cite the issue/PR whenever a
  behavior exists because of an incident.
- **UI paths in bold**: "**Thermostats** page", "settings cog (⚙️) → **MCP**",
  "**Configuration** tab".
- **Code voice**: backticks for entity domains (`cover.*`), fields
  (`in_min_runtime_hold`), and values (`0`); relative links between docs pages
  (`[overflow conditioning](./overflow-conditioning.md)`).
- **Explain the physics once per page.** Docs assume a smart-home user, not an HVAC
  tech: define terms like short-cycling or dead-heading in one clause at first use
  (deeper theory belongs to **hvac-zoning-reference**).

---

## 3. Prose rules for commits, PRs, and issues (from CLAUDE.md — restated, CLAUDE.md owns them)

1. **No AI attribution.** Never state or imply that code was written by Claude or any
   AI assistant in commit messages, PR titles, PR bodies, or issue comments.
2. **No session links.** Never include Claude session links in any of the above.
3. **PR body updates after every push.** Every push to a PR is followed by updating
   the PR body: what changed, why, test plan. Unprompted, every time.
4. **PR titles are changelog entries** (§1.4): user-facing, capitalized sentence,
   conventional prefix optional (`ci:`, `chore(deps):` appear in history and render
   fine, but a plain descriptive sentence reads best in the changelog).
5. Review fixes are pushed to the PR's own branch, not a side branch (CLAUDE.md).

For what a PR must *contain* (evidence, gates), see **plenum-change-control**.

---

## 4. External claims standard + README stats-refresh runbook

Principle: **every number in a public claim must be reproducible by a command, and
claims decay** — so they carry a date and get recounted, not extrapolated. Evidence
that this matters: the stats block stamped "June 2026" was already stale by 2026-07:

| README claim (June 2026) | Measured 2026-07 | Recount command (repo root) |
|---|---|---|
| Backend: 38 test modules (16 unit + 22 integration) | **56** (24 top-level + 32 in `integration/`) | `find smart_vent/backend/tests -name "test_*.py" \| wc -l` |
| Backend: ~15.4k lines of test code | ~19.1k | `find smart_vent/backend/tests -name "test_*.py" -exec cat {} + \| wc -l` |
| Backend: 733 tests | UNVERIFIED here (pytest not installed in this env) | `cd smart_vent && python -m pytest backend/tests --collect-only -q \| tail -1` (needs `pip install -e ".[dev]"`) |
| Frontend: 243 tests / 16 files / ~4.2k lines | **285** listed / **17** files / ~4.9k lines | `cd smart_vent/frontend && npx vitest list \| wc -l` ; `find src -name "*.test.*" \| wc -l` |
| E2E: 15 tests / 10 spec files | **13** spec files; 17 `test(` call sites (some specs generate more at runtime) | `ls e2e/tests/*.spec.ts \| wc -l` ; authoritative count: `cd e2e && npx playwright test --list` (UNVERIFIED here — needs Playwright installed) |
| Backend coverage gate 92.5% | correct | `grep fail_under smart_vent/pyproject.toml` |
| Frontend gates 90/85/72/87 | correct | `grep -A4 thresholds smart_vent/frontend/vite.config.ts` |

Note the pattern: the **thresholds** (CI-enforced ratchets) stayed true; the **counts**
(unenforced snapshots) rotted. Prefer claims CI enforces; date-stamp the rest.
(Coverage-threshold table of record: `plenum-validation-and-qa` §6 — quote it,
don't re-derive.)

**Stats-refresh runbook** (do this whenever touching the Tested block, and at least
once per minor release):

1. Run every recount command in the table above; use *measured* numbers only.
2. Coverage thresholds: quote the values from `pyproject.toml` / `vite.config.ts`
   verbatim. If a threshold went *down*, stop — that violates the ratchet and is a
   change-control matter, not a docs edit.
3. Update the date line: `> Stats last updated: <Month Year> — coverage thresholds
   are enforced by CI and only ever increase.`
4. Keep the qualitative claims honest: the round-trip suite description ("the only
   layer that exercises the full frontend → API → DB → UI conversion contract
   end-to-end") is load-bearing — do not dilute or copy it onto suites it isn't true
   of.
5. No oversell anywhere: unshipped ideas are labeled planned/experimental; the
   heat-pump non-support line is never weakened; "not Flair-specific" is claimable
   only because it is literally true (Plenum speaks `cover.*`/`climate.*`, README
   §About).

---

## 5. Templates

### 5.1 New docs/ feature page skeleton

```markdown
# <Feature name, sentence case>

<One paragraph: what the feature does and WHY it exists — name the comfort or
equipment problem it solves. Cite the originating issue inline, e.g. (#237).>

> **Scope:** <When this logic runs / does not run. One or two sentences.>

## <How it works — noun-phrase heading>

<Mechanism. °F values with units; backticks for fields/entities; bold UI paths.>

| Setting | Default | What it does |
|---|---|---|
| **<Name as shown in UI>** | <value, or "0 (disabled)"> | <One or two sentences; include the recommended value in bold if there is one.> |

## <Edge cases / interactions>

<Interactions with other features, linked relatively: see
[safety features](./safety.md). Upgrade behavior for pre-existing rows if relevant.>
```

Ship-with-feature checklist: page written in the feature PR → index line added to
`docs/README.md` → README.md Documentation list updated (it drifts — §1.1) → any new
settings also appear in README's settings-reference tables if they're first-run
material → screenshots retaken only if a page changed visibly.

### 5.2 CLAUDE.md amendment checklist — see §1.3 (six steps).

### 5.3 README stats refresh — see §4 runbook.

---

## Provenance and maintenance

All facts verified against the repo at v0.22.1, 2026-07-04. Volatile facts and their
re-verification commands:

- Docs page count (14) and index completeness: `ls docs/*.md | wc -l` vs the list in
  `docs/README.md`; README.md's Documentation section is the drift-prone third copy.
- CLAUDE.md stale-spot table (§1.3): re-check with
  `ls .github/workflows/` (no `e2e.yml`), `grep fail_under smart_vent/pyproject.toml`,
  `sed -n '30,40p' smart_vent/backend/api/routes.py` (units import),
  `grep -n "thresholds" -A4 smart_vent/frontend/vite.config.ts`. Delete rows from the
  table as CLAUDE.md gets fixed.
- Test counts (§4 table): rerun every command in the table; the backend collected
  count and Playwright `--list` count were **not executable in the authoring
  environment** (dev deps not installed) and are marked UNVERIFIED.
- CHANGELOG generation details (§1.4): `.github/workflows/release-pr.yml`, steps
  "Generate changelog entries" and "Update CHANGELOG.md".
- RELEASE.md vs workflows (§1.5): re-check with
  `grep -n 'Build (PR validation)' RELEASE.md .github/workflows/container-ci.yml`.
- Screenshot inventory: `ls screenshots/` (10 files, `01_…10_` as of 2026-07).
- Commit hash for the capture pattern: `git show --stat 5ea05c5`.
