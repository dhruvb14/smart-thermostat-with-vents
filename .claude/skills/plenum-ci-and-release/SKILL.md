---
name: plenum-ci-and-release
description: >
  The CI/CD machine of Plenum (smart-thermostat-with-vents): every GitHub Actions
  workflow job-by-job, container-ci's three build modes (normal PR / release PR /
  fork PR) and the PLENUM_IMAGE handoff, the visual-regression golden-screenshot
  machinery (parallel F/C legs, update-then-verify, fan-in auto-commit bot),
  the tag→release-PR→publish release flow, branch protection, the failure-triage
  table for red checks, and the nightly ci-image cleanup. Load when a CI check
  fails, when a bot pushed "ci: update E2E golden screenshots" onto your branch,
  when cutting or validating a release, or when editing anything under
  .github/workflows/.
---

# Plenum CI and Release

Everything here was verified against the workflow YAML on the branch as of
2026-09-01 (v0.35.0 stable / v0.36.0-beta.3 beta). **The repo YAML is the
source of truth** — two docs of record lagged behind it until corrected on
2026-07-05 (see "Doc drift history" at the end):

- Pre-2026-07-05 `CLAUDE.md` copies described a standalone
  `.github/workflows/e2e.yml` with `max-parallel: 1` and in-job golden commits
  (corrected in PR #388). **That file no longer exists.** Visual regression
  moved *into* `container-ci.yml` with parallel legs and a fan-in commit job
  (commit `a0e9d04`, #366; push fix #369/#370).
- `RELEASE.md`'s "What triggers what" table named `docker.yml` jobs
  `build-pr` / `build-release` and a required check "Build & Push release
  image" until corrected on 2026-07-05 (PR #388) — all replaced by
  `container-ci.yml`'s `Build (PR validation)` (#333/#337).

**When NOT to use this skill**: writing or extending tests, coverage gates,
parity-test mechanics, golden inventory → `plenum-validation-and-qa`. What
evidence a change class needs before merge → `plenum-change-control`. Local
build/compose stack anatomy → `plenum-build-and-env`. The #231 story behind
the dual-unit matrix → `plenum-failure-archaeology`.

Jargon: a **golden** is a committed reference PNG in `e2e/screenshots/` that
Playwright compares fresh screenshots against pixel-by-pixel. A **leg** is one
cell of a matrix job (e.g. the °C run). **Fan-in** = a job that waits for all
matrix legs and combines their outputs.

## 1. Workflow inventory — what fires when

The workflows below live in `.github/workflows/` — exactly seven files, no
more. Job names are quoted exactly from the YAML (`name:` fields); these are
the strings you see as PR checks.

| Workflow file | Trigger | Jobs (check names) |
|---|---|---|
| `lint.yml` ("Lint & Test") | push to `main` AND `pull_request` → main; job-level `if:` gate off a `changes` job (see below) — no `paths-ignore` | `Detect changed paths`, `Python (ruff)`, `Python (pytest)`, `Python (mypy)`, `Frontend (ESLint + Prettier)`, `Security (Trivy source scan)` |
| `container-ci.yml` ("Container CI") | `pull_request` → main + `workflow_dispatch`; job-level `if:` gate off a `changes` job — no `paths-ignore`; concurrency group per PR, `cancel-in-progress: true` | `Detect changed paths`, `Build (PR validation)`, `Docker Smoke Test`, `Round-trip (F)` / `Round-trip (C)`, `MQTT round-trip`, `MCP conformance (stateless)` / `(stateful)`, `E2E visual regression (F)` / `(C)`, `E2E visual regression (auth)`, `Commit updated goldens` |
| `docker.yml` ("Build & Push Docker Image") | push to `main` only (workflow-level `paths-ignore` — safe here since this workflow never gates a required PR check) | `Build & Push (main — config changed)` |
| `beta.yml` ("Beta channel") | `pull_request` → main + push to `main` + `workflow_dispatch` (`force` input); `concurrency` per PR/ref, `cancel-in-progress: false` | `Update beta pointer (in PR)` (writes the beta version + changelog onto the PR's own branch, same-repo non-release PRs only), `Publish beta image` (push-to-main or manual dispatch; builds+pushes `ghcr.io/dhruvb14/plenum-beta:<version>` only if that tag isn't already in the registry) |
| `release-pr.yml` ("Create Release PR") | push of tag `v*.*.*` + `workflow_dispatch` (`tag` input, notes-resync only) | `Create Release PR` |
| `ci-image-cleanup.yml` ("Cleanup CI Images") | cron `17 4 * * *` (04:17 UTC nightly) + `workflow_dispatch` (inputs `retention_days` default 14, `dry_run`) | `Delete stale ci-* images` |
| `codeql-issue-sync.yml` ("CodeQL Issue Sync") | cron `0 6 * * *` (06:00 UTC daily — downgraded from hourly, #414: "hourly was ~24 mostly-no-op runs/day") + `workflow_dispatch` | `create_issue` (files a GitHub issue per open CodeQL alert, labels `security,codeql`, dedupes by "CodeQL Alert #N" in title) |

There is no `validate-release.yml`. It was removed (2026-07): its "Release
Validation" job (lint + both test suites + Docker build + healthz smoke +
Fahrenheit-only visual regression) ran only on manual `workflow_dispatch` or
on the bot-opened release PR — and on that release PR, every one of those
checks was already run automatically and more completely by `lint.yml`
(unconditional on all PRs) and `container-ci.yml`'s `Build (PR validation)` /
`Docker Smoke Test` / `E2E visual regression (F)+(C)` (required checks that
always run on a release PR, since the version bump in `smart_vent/config.yaml`
always trips container-ci's UI-diff classifier). It caught nothing a release
PR's own required checks didn't already catch. There is also no `e2e.yml` —
that history is covered in §3 below.

Key trigger facts (corrected — the old `paths-ignore: e2e/screenshots/**`
mechanism described here in earlier revisions of this skill is GONE from both
`lint.yml` and `container-ci.yml`; #412 replaced it):

- **Both `lint.yml` and `container-ci.yml` now gate with a job-level `changes`
  job, not a workflow-level `paths`/`paths-ignore` filter.** A job skipped by
  `if:` still reports "skipped" (satisfies branch protection); a
  path-filtered workflow never creates its required check and the PR hangs
  forever — that was the #412 lesson. The ignore set is identical across
  `lint.yml`'s `changes`, `container-ci.yml`'s `changes`, and `beta.yml`'s
  inline classifier: root-level `*.md`, `docs/`, `.claude/`, `.jules/`,
  `.vscode/`, `screenshots/`, `e2e/screenshots/`. `.github/` is deliberately
  code (a workflow edit must still run the full suite).
- **`container-ci.yml`'s `changes` job emits TWO independent flags**, `code`
  and `ui` — this is new since the single-flag version this skill used to
  describe:
  - `code=false` (docs-only diff) skips `build` and therefore every job that
    `needs: build` — smoke, both round-trip legs, the MQTT leg, both MCP
    conformance legs, and both visual-regression leg families.
  - `ui=false` (no path under `smart_vent/frontend/`, `e2e/` (excluding
    `e2e/screenshots/`), `docker-compose.test*`, `smart_vent/config.yaml`,
    `smart_vent/Dockerfile`, or this workflow file itself) skips ONLY the
    visual-regression legs (`e2e`, `e2e-auth`) even when `code=true` — a
    **backend-only PR still builds, smoke-tests, and round-trips, but skips
    screenshots**. Known trade-off (stated in the YAML): a backend change
    that alters what a fixture page displays (e.g. a setpoint computation)
    won't refresh goldens until the next UI-touching PR regenerates them.
  - A docs-only or skills-only PR now skips the ENTIRE container pipeline,
    including both visual-regression legs — this reverses what earlier
    revisions of this skill said ("A docs-only PR still runs the FULL
    pipeline"). Golden-bot pushes on a markdown-only PR (§3's PR #388 example)
    describe the OLD `paths-ignore` regime and can no longer happen the way
    that section describes; they are now only possible on a UI-touching PR.
- The fact that **GITHUB_TOKEN pushes never trigger workflows** is still an
  independent guard against the golden bot re-triggering CI, on top of the
  `changes` gating.
- `container-ci.yml`'s concurrency group cancels a superseded run when a newer
  commit lands on the same PR (also avoids the buildx/qemu "another job may be
  creating this cache" GHA-cache race warning).

## 2. container-ci build modes and the PLENUM_IMAGE handoff (#333, #337)

`Build (PR validation)` builds the addon image **once**; every downstream job
(`needs: build`) reuses it. The `meta` step picks one of three modes:

| Mode | Detected by | Build | Handoff to downstream jobs |
|---|---|---|---|
| Normal same-repo PR | head repo == this repo, branch not `release/v*` | multi-arch (amd64+arm64), push throwaway `ghcr.io/<repo>:ci-<head-sha>` | jobs `docker pull` the tag |
| Release PR | same-repo AND head branch `release/v*` | multi-arch, push **real** `:<version>` (read from `smart_vent/config.yaml`) **+ `:latest`**, then Trivy image scan (fails on `CRITICAL: [1-9]`, respects `.trivyignore`, uploads SARIF) | jobs pull the explicit `:<version>` tag — never `:latest` |
| Fork PR / `workflow_dispatch` | head repo != this repo (fork tokens are read-only → can't push to GHCR), or no PR at all | single-arch amd64, `load: true`, tagged `plenum-e2e:latest`, `docker save` → artifact `plenum-image` (retention 1 day) | jobs `download-artifact` + `docker load` |

Fork takes precedence: a fork branch named `release/v*` still uses the
artifact path, never the publish path.

Three more things the build job does:

- **Version pin for determinism**: non-release modes run
  `sed -i 's/^version:.*/version: CI/' smart_vent/config.yaml` before building,
  which bakes `VITE_APP_VERSION=CI` into the bundle → `isCI`/`<Frozen>`
  (`frontend/src/ci.tsx`) freezes volatile UI so goldens are stable. Release
  PRs keep the real version for the published `:<version>`/`:latest` image
  (it must show the real version).
- **A release PR ALSO gets a second, throwaway, frozen-UI image.** After the
  real `:<version>`/`:latest` image is built and pushed, the build job pins
  `config.yaml` to `version: CI` again and builds a SECOND single-arch image
  (`load: true`, tagged `plenum-e2e:latest`, saved to the same `plenum-image`
  artifact used by forks) — this is what the visual-regression legs consume
  for a release PR, since the published real-version image is never
  `isCI`-frozen and could never match a committed golden. Smoke test and the
  round-trip/MQTT/MCP-conformance legs still test the real published image.
- Outputs `image`, `is_fork`, `is_release` consumed by every downstream job.

**PLENUM_IMAGE handoff**: `docker-compose.test.yml`'s plenum service is
`image: "${PLENUM_IMAGE:-plenum-e2e}"`. Each downstream job sets
`PLENUM_IMAGE=<ghcr tag>` (same-repo, non-visual jobs) or
`PLENUM_IMAGE=plenum-e2e` (fork, or the two visual-regression job families on
a release PR — the loaded image already carries the compose-default tag).
The two visual jobs additionally `docker tag` a pulled same-repo-non-release
image `plenum-e2e` for consistency. Nothing in a PR run ever rebuilds the
image beyond what `build` produces.

Downstream jobs, exactly as named (all `needs: [build, changes]`, gated
`if: needs.changes.outputs.code == 'true'` except the two visual-regression
families, which gate on `needs.changes.outputs.ui == 'true'` instead — see §1):

- **`Docker Smoke Test`** (`smoke`): runs the image with dummy `HA_URL`/`HA_TOKEN`,
  polls `/api/healthz` on :8099 (30×1s), then exercises the MCP surface on
  :9099/mcp — asserts 503 while `mcp_enabled` is off, enables it via
  `POST /api/system/mcp`, runs a real `initialize`, and checks `tools/list`
  contains `get_healthz`. Runs with `REQUIRE_AUTH=false` — it proves boot and
  the MCP transport, not the #373 auth boundary (that's `e2e-auth`, below).
- **`Round-trip (F)` / `Round-trip (C)`** (`conversion`, matrix `unit: [F, C]`,
  `fail-fast: false`, 25 min timeout): full HA + Plenum compose stack
  (°C leg layers `docker-compose.test.celsius.yml`), verifies
  `/api/settings` reports the matrix unit, then runs
  `npx playwright test temperature-units.spec.ts --project=chromium`. This is
  the end-to-end guard against #231-class double conversion.
- **`MQTT round-trip`** (`mqtt`, #519, 25 min timeout): layers
  `docker-compose.test.mqtt.yml` to stand up a real mosquitto broker, then
  runs `e2e/scripts/mqtt-roundtrip.py` — publishes a command over MQTT, checks
  it lands in the database, and checks a rejected command reports failure on
  its result topic instead of silently no-op'ing. Not a Playwright leg (no
  browser). Complements `test_mqtt_real_broker.py` in the backend suite, which
  covers the same contract against an in-process amqtt broker.
- **`MCP conformance (stateless)` / `(stateful)`** (`mcp-conformance`, #543,
  matrix `transport: [stateless, stateful]`, 25 min timeout): drives real MCP
  tool calls against the built image + a real HA over
  `docker-compose.test.mcp.yml`, verifying state three ways (MCP read-back,
  REST cross-check, the HA entity itself) via
  `backend/tests/integration/test_mcp_conformance.py -v --no-cov`. Regression
  baseline for the `mcp` Python SDK v1→v2 migration — the suite speaks raw
  JSON-RPC and imports nothing from the `mcp` package. The `stateful` leg sets
  `PLENUM_MCP_STATELESS=false` so the server issues an `Mcp-Session-Id` the
  client must echo back; `stateless` is production's default. Auth/scope
  coverage for MCP tokens lives in the backend suite instead (every compose
  leg here runs `REQUIRE_AUTH=false`).
- **`E2E visual regression (F)` / `(C)`**, **`E2E visual regression (auth)`**,
  and **`Commit updated goldens`** — §3.

## 3. Visual-regression golden machinery (current, post-#519/#458/#373-UI)

History in one line: the old standalone `e2e.yml` ran legs serially
(`max-parallel: 1`) and each leg committed its own goldens (two commits, push
races). #366 (`a0e9d04`) moved it into `container-ci.yml` with **parallel
legs + one fan-in commit**; #369/#370 (`c26d395`) fixed the push, which failed
from actions/checkout's detached HEAD. Since then two more legs joined
(`e2e-auth` for the #373 login/MCP-token UI) and dark mode (#458) doubled
every golden; the fan-in's git strategy was **also rewritten** — it no longer
rebases (see below).

### The legs — three families, all in parallel, gated on `ui == 'true'` (§1)

**`e2e` (matrix `unit: [F, C]`)**: obtain image (§2) → compose up HA (°C leg
layers the celsius override) → mint HA token (`e2e/scripts/setup-ha.py`) →
start Plenum → assert active unit matches the matrix → then a three-pass
Playwright dance, always with
`--grep-invert "Temperature round-trip|@auth"` (the round-trip spec mutates
shared backend state and is covered by the `conversion` job instead; the
`@auth`-tagged specs belong to the `e2e-auth` leg below, which runs with
`require_auth=true` — the F/C legs run with auth off):

1. **Run** (`continue-on-error: true`). All screenshots match committed
   goldens → leg passes, nothing uploaded.
2. **Regenerate** (only if pass 1 failed): `npx playwright test
   --update-snapshots ...`. A non-screenshot failure (missing element,
   timeout) fails here too — no spurious golden update.
3. **Verify**: plain run again against the regenerated goldens. Only if this
   passes does the leg stage **its own unit's PNGs only**
   (`cp e2e/screenshots/*-"${UNIT_LABEL}"-* /tmp/goldens/`) and then strip out
   `login-*` / `settings-menu-auth-*` / `mcp-tokens-card-*` from that staged
   copy (this leg never regenerates the auth screens — they're grep-inverted
   out — so a stale sweep-in of them here could non-deterministically clobber
   a fresh golden the `e2e-auth` leg just produced) before uploading artifact
   `goldens-F` / `goldens-C` (retention 1 day). Staging only the leg's unit
   also prevents one leg's stale copy of the *sibling* unit's goldens from
   reverting what the sibling regenerated.

**`e2e-auth`** (#373, single leg, no unit matrix — always °F,
`docker-compose.test.auth.yml` layered on top so `require_auth=true`):
starts the same image with auth enabled, asserts `GET /api/rooms` with no
credential 401s, authenticates the Playwright harness by injecting a session
cookie signed with the stack's pinned `PLENUM_SESSION_SECRET`
(`e2e/auth-cookie.ts`), then runs the identical three-pass dance but scoped to
`--grep "@auth"` — covering the login screen, the MCP-token card, and the
settings "Signed in / Log out" state. Its regenerated PNGs
(`login-*`, `settings-menu-auth-*`, `mcp-tokens-card-*`) upload as artifact
`goldens-auth`.

Both `e2e` and `e2e-auth` have `contents: read` only — neither can push.

### The fan-in (`Commit updated goldens`, job id `commit-goldens`)

`needs: [e2e, e2e-auth]`,
`if: always() && (needs.e2e.result != 'skipped' || needs.e2e-auth.result != 'skipped')`,
`contents: write` + `pull-requests: write` (the write scope grew to cover the
PR-comment step, below). Downloads `goldens-*` artifacts (now three possible
sources: `goldens-F`, `goldens-C`, `goldens-auth`) with `merge-multiple: true`
into `e2e/screenshots/` (continue-on-error covers the no-artifacts case), then
**no longer rebases** — the strategy was rewritten after a real incident (a
v0.26.0 release-branch push broke the rebase when the PR was behind main) to
a snapshot-and-transplant instead:

```
git add -- e2e/screenshots/
# empty diff → "nothing to commit", clean exit
git commit -m "ci: goldens snapshot (transplant source, never pushed)"
GOLDEN_SRC=$(git rev-parse HEAD)          # this checkout is the PR MERGE ref —
                                           # never rebase it onto the branch
git fetch origin "$BRANCH"
git checkout -f -B golden-update "origin/$BRANCH"   # the REAL branch tip
git checkout "$GOLDEN_SRC" -- e2e/screenshots/      # transplant ONLY the goldens
git add -- e2e/screenshots/
# empty diff here → branch tip already carries these goldens, clean exit
git commit -m "ci: update E2E golden screenshots (F + C)"
git push origin HEAD:"$BRANCH"     # HEAD: form — the #369/#370 detached-HEAD fix
```

Why the checkout used for the working tree matters: `actions/checkout` on a
`pull_request` event checks out the **merge ref** (head merged into main),
not the branch tip. Committing goldens there and rebasing that commit onto
the branch replays a merge commit — which conflicts the instant the PR is
behind main. The fix snapshots the regenerated PNGs on the merge ref, then
switches to the actual branch tip and transplants only `e2e/screenshots/`
onto it, so no merge commit is ever replayed.

Push semantics and races:

- The commit lands **directly on the PR branch** as `github-actions[bot]`.
- **It does not re-trigger CI**: GITHUB_TOKEN pushes never trigger workflows
  (belt-and-braces: `container-ci.yml`'s `changes` job classifies a
  goldens-only diff as docs-only, so even a manual re-run of the same diff
  would skip the pipeline — see #412).
- **The legs go GREEN on the run that rewrote the goldens** (#415): pass 1 is
  `continue-on-error`, so a leg whose regenerate+verify pass succeeds
  concludes success. The signals that a rewrite happened are the bot commit
  itself and the PR comment the `commit-goldens` job posts (`Announce the
  golden rewrite on the PR` step) listing every changed PNG — review them
  like code.
- Because the transplant only ever writes `e2e/screenshots/` onto the current
  branch tip (fetched fresh each run), there is no rebase-conflict failure
  mode any more; a losing push race just means the next run's transplant
  starts from a newer tip.
- **Rule for agents: after any CI run on your PR, `git fetch` before pushing.**
  A local push made after the bot's push is rejected as non-fast-forward;
  never `--force` over the bot commit or you revert the goldens and CI loops.

### Known behavior: docs-only and backend-only PRs skip the visual legs entirely

This reverses what earlier revisions of this skill said. Under the OLD
`paths-ignore: e2e/screenshots/**` regime (pre-#412), the visual matrix ran on
**every** PR regardless of what changed, which is why PR #388 (a
`.claude/skills/`-only markdown change) got a golden-bot push (`f0dd26b`)
regenerating `room-detail-*` / `rooms-*` PNGs from runner rendering drift.
Under the current `changes` job's `ui` flag (§1), a docs-only PR has `ui=false`
and the `e2e` / `e2e-auth` jobs are `if:`-skipped outright — no image is even
pulled for them. A **backend-only** code PR (`code=true`, `ui=false`) also
skips the visual legs, since goldens capture frontend rendering only. Expect a
golden-bot push only on a PR that touches `smart_vent/frontend/`, `e2e/`
(excluding `e2e/screenshots/`), `docker-compose.test*`,
`smart_vent/config.yaml`, `smart_vent/Dockerfile`, or `container-ci.yml`
itself — still budget for sub-pixel rendering drift on any such PR (pull the
bot commit before your next push, and glance at the changed PNGs; a *visible*
change with no matching frontend diff is a red flag).

### Tolerances

Global `maxDiffPixels: 100`; `e2e/tests/metrics.spec.ts` overrides to `800`
because the `mobile` project's `deviceScaleFactor: 3` amplifies native
`<input type="date">` jitter ~9×. Prefer a per-spec bump over masking. Golden
inventory: **368 PNGs as of 2026-09** (`ls e2e/screenshots | wc -l`) — up
from the 92 last recorded here in 2026-07. The growth is the theme axis
(#458): every spec now renders through `chromium` / `chromium-dark` /
`mobile` / `mobile-dark` projects (`playwright.config.ts`'s
`colorScheme: "dark"` emulation, no extra specs needed) on top of the
existing `{Fahrenheit, Celsius}` unit axis, plus the new `e2e-auth` leg's
`login-*` / `login-filled-*` / `settings-menu-auth-*` / `mcp-tokens-card-*`
screens (°F-only, no Celsius counterpart — auth UI doesn't render a
temperature). Adding/inventorying goldens → `plenum-validation-and-qa`.

## 4. Release flow end-to-end

Runbook of record: `RELEASE.md` (but see doc drift, §7). Policy: max one
release/day, everything through PRs, semver, no direct pushes to main.

1. **Tag**: `git checkout main && git pull`, `git tag v0.X.Y`,
   `git push origin v0.X.Y`.
2. **`release-pr.yml` fires on the tag**: derives version, bounds the range at
   the previous semver tag, generates changelog bullets from merged-PR titles
   (skipping "Release vX" housekeeping PRs, linking closing issues), collects
   contributors, prepends to `smart_vent/CHANGELOG.md`, bumps the version in
   **three files + lockfile** (`smart_vent/config.yaml`, `smart_vent/pyproject.toml`,
   `smart_vent/frontend/package.json` + `package-lock.json`), force-pushes
   branch `release/vX.Y.Z`, opens PR "Release vX.Y.Z" → main, and writes the
   same notes onto the GitHub Release for the tag.
3. **On the release PR**, container-ci's `Build (PR validation)` runs in
   release mode (§2): builds once, pushes the **real** `:<version>` + `:latest`
   to GHCR, Trivy-scans the published image (CRITICAL ⇒ fail). The smoke,
   round-trip, MQTT, and MCP-conformance jobs pull that exact `:<version>` tag
   — the artifact being published is what they test. The visual-regression
   jobs (`e2e`, `e2e-auth`) do NOT use that tag: the build job also produces a
   second, throwaway, `version: CI`-pinned image specifically for them (§2),
   since the real published image is never `isCI`-frozen and could never
   match a committed golden. This is the only pre-merge validation pass;
   there is no separate manual dry-run workflow.
4. **Merge**: nothing rebuilds. `docker.yml`'s `Build & Push (main — config
   changed)` detects the release-merge commit message
   (`release/vX.Y.Z` pattern) and skips — the image already shipped during
   the PR. `docker.yml` only actually builds when a **non-release** merge to
   main changed `smart_vent/config.yaml` (a version bump landing outside the
   release flow).
5. **If the Docker build fails on a release PR**: do NOT merge — the broken
   image is already in GHCR. Fix via a normal PR, delete the tag
   (`git push origin :v0.X.Y; git tag -d v0.X.Y`), tag the next patch version.

**Branch protection**: per CLAUDE.md, the required check for release PRs is
**`Build (PR validation)`** (container-ci), replacing the old "Build & Push
release image". Whether the newer `MQTT round-trip` / `MCP conformance
(stateless)/(stateful)` / `E2E visual regression (auth)` checks are also
branch-protection-required is UNVERIFIED from the working tree (needs admin
API access) — same caveat as the rest of the required-check list, which lives
in GitHub repo settings.

## 5. Failure triage table

| Red check / symptom | Likely cause | Fix |
|---|---|---|
| `Python (ruff)` fails only on `ruff format --check` | unformatted code | `cd smart_vent && ruff format backend/`, commit |
| `Python (pytest)` fails with coverage error | backend coverage below the `fail_under` ratchet in `pyproject.toml` — 96.7 as of 2026-09; see `plenum-validation-and-qa` | add tests — patterns in `plenum-validation-and-qa` |
| `Python (pytest)` fails in `test_temperature_field_parity.py` | temperature field added to only 1–2 of the 3 registries (`routes.py` `TEMPERATURE_FIELDS`, `e2e/tests/temperature-fields.ts`, `// @covers:` tag in the spec) | complete all three — checklist in `plenum-change-control` §2.1 |
| `Python (pytest)` fails in `test_addon_config.py` | `config.yaml` option without matching `bashio::config` in `run.sh` | add to both files |
| `Python (pytest)` fails in `test_api_spec_enforcement.py` | new `/api/` route without `@docs` + `@response_schema` | add the decorators |
| `Frontend (ESLint + Prettier)` coverage failure | below `vite.config.ts` thresholds — lines 94.2, functions 91.3, branches 79.9, statements 92.0 as of 2026-09; see `plenum-validation-and-qa` | add tests → `plenum-validation-and-qa` |
| `E2E visual regression (F)/(C)` or `(auth)` red, then a bot commit `ci: update E2E golden screenshots (F + C)` appears | screenshots drifted (your UI change, or environment jitter on a UI-touching PR — §3) | `git fetch`/pull the bot commit, review every changed PNG in the diff like code; red legs on *that* run are expected and clear on the next run |
| Visual legs red, **no** bot commit | regenerate or verify pass also failed → a real breakage (missing element, timeout), or new volatile UI | download `playwright-results-F/C`/`-auth` artifact; wrap new timers/feeds/clocks in `<Frozen>` (`frontend/src/ci.tsx`) |
| Goldens never stabilise across runs (bot commits every run) | time-varying UI not frozen | `<Frozen>` wrap — see CLAUDE.md pitfall 8 |
| Your `git push` rejected non-fast-forward after CI | golden bot pushed to your branch first | `git fetch && git rebase origin/<branch>`; never force-push over the bot |
| `Commit updated goldens` job itself fails | the fan-in no longer rebases (§3) — it transplants `e2e/screenshots/` onto a freshly-fetched branch tip, so the main failure mode left is a push race (the branch tip moved again between its fetch and its push) | re-run the workflow from the Actions tab (or `workflow_dispatch` container-ci) |
| Fork PR: downstream jobs can't find the image | fork mode hands off via artifact `plenum-image`, not GHCR; if `Build (PR validation)` failed, nothing downstream gets an image | fix the build first; remember fork builds are amd64-only |
| `Round-trip (F)` or `(C)` fails | unit-conversion contract broken (a #231-class bug) or the stack's unit didn't match the matrix | contract → `plenum-architecture-contract`; triage → `plenum-debugging-playbook` |
| `MQTT round-trip` fails | a published command didn't land in the DB, or a rejected command didn't report on its result topic — `backend/mqtt/registry.py` / `naming.py` contract broken (#519) | check `e2e/scripts/mqtt-roundtrip.py` output and the plenum service logs; cross-check against `backend/tests/test_mqtt_real_broker.py` |
| `MCP conformance (stateless)` or `(stateful)` fails | an MCP tool call's result disagrees across MCP read-back / REST / the HA entity itself, or (stateful only) the server mishandled `Mcp-Session-Id` | `backend/tests/integration/test_mcp_conformance.py` output; this is the regression baseline for the `mcp` SDK v1→v2 migration, so a fresh red here after touching `mcp_http.py` is the first thing to suspect |
| `Security (Trivy source scan)` fails | `jq` counts `Severity == "CRITICAL"` entries in the native `trivy-output.json` — same real-count semantics as the image scan's `CRITICAL: [1-9]` regex. (Previously a plain `grep -q "CRITICAL"` on the fs-scan *table*, which false-failed on ANY severity — the table's summary line always prints a literal `CRITICAL: 0`; fixed alongside PR #514.) | fix or bump the vulnerable dep; image-scan suppressions go in `.trivyignore` |
| "another job may be creating this cache" warning | two container-ci runs raced on the buildx/qemu GHA cache | benign; the concurrency group cancels superseded runs |
| Nightly `Delete stale ci-* images` fails with 403 on DELETE | GITHUB_TOKEN lacks delete rights on a user-owned GHCR package | add repo secret `GHCR_CLEANUP_TOKEN` (classic PAT, `delete:packages` + `read:packages`) — preferred automatically when present |

## 6. Nightly ci-image cleanup rules (`ci-image-cleanup.yml`)

Every same-repo PR pushes a throwaway `ci-<sha>` tag to GHCR; this job prunes
them nightly (04:17 UTC). Deletion rules, exactly as coded:

- A container version is deleted only if **all** of its tags start with `ci-`
  AND its `updated_at` is older than `RETENTION_DAYS` (default 14).
- Versions carrying any non-`ci-` tag (`:latest`, semver releases) are never
  touched; **untagged** versions are also left alone (the `length > 0` guard).
- Manual run: `workflow_dispatch` with `retention_days` and a `dry_run`
  boolean that lists deletions without deleting — use it before changing
  retention.

## 7. Doc drift history (all corrected 2026-07-05, PR #388)

| Doc | Pre-2026-07-05 stale claim | Reality (and current text) |
|---|---|---|
| `CLAUDE.md` "E2E visual regression" section | standalone `.github/workflows/e2e.yml`; `max-parallel: 1`; each leg commits its own goldens; "verify with updated goldens runs in the same job" via `git checkout -f -B` | no `e2e.yml` exists; legs run in parallel inside `container-ci.yml`; verify happens per-leg, commit happens once in the `Commit updated goldens` fan-in (#366). Corrected in PR #388. |
| `CLAUDE.md` same section | `mode=pull` polling loop in "Decide image source" | replaced by `needs: build` + direct pull since the move into container-ci. Corrected in PR #388. |
| `RELEASE.md` "What triggers what" table | `docker.yml` → `build-pr` / `build-release`; required check "Build & Push release image" | corrected 2026-07-05 (PR #388): all PR container work is `container-ci.yml`'s `Build (PR validation)`, which is the required check |
| `RELEASE.md` / this skill / `plenum-change-control`'s "Cut a release" steps | a manual "Validate Release" dry-run workflow as pre-flight step 1 | `.github/workflows/validate-release.yml` was deleted (2026-07): every check it ran (lint, both test suites, Docker build, healthz smoke, °F visual regression) was already run automatically, and more completely, by `lint.yml` + `container-ci.yml` on the release PR itself — it caught nothing extra. |

If docs and repo disagree again, the workflow YAML wins. If you touch the
workflows, update `RELEASE.md`'s table and CLAUDE.md's E2E section in the
same PR.

## Provenance and maintenance

Facts re-verified **2026-09-01** (v0.35.0 stable / v0.36.0-beta.3 beta) by
reading all seven workflow files in `.github/workflows/` end to end
(`beta.yml` and `codeql-issue-sync.yml` included — both are now fully covered
by this skill, closing the gap the 2026-07-04 pass left open), plus
`RELEASE.md`, `e2e/playwright.config.ts`, `smart_vent/pyproject.toml`,
`smart_vent/frontend/vite.config.ts`, and `ls e2e/screenshots`. Since the prior
2026-07-04 pass, `container-ci.yml` grew three new job families
(`mqtt` #519, `mcp-conformance` #543, `e2e-auth` #373) and its `changes` job
split into two flags (`code`/`ui`); the golden fan-in's git strategy was
rewritten from a rebase to a snapshot-and-transplant; both `lint.yml` and
`container-ci.yml` replaced their `paths-ignore: e2e/screenshots/**` filters
with the job-level `changes` gate (#412); and dark mode (#458) plus the auth
UI roughly quadrupled the golden count (92 → 368). The original 2026-07-04
provenance (branch `claude/skill-library-continuity-qit89f`, commits
`a0e9d04` #366, `c26d395` #369/#370, `f0dd26b` PR #388 golden-bot push, with
the PR #388 root-cause attribution to runner drift still **inferred**, no
runner-side evidence available) remains valid history for §7's doc-drift
table, which is a frozen record of that correction and is not re-verified
here.

Re-verify before trusting:

```bash
# Workflow inventory + exact job names (7 files, no e2e.yml, no validate-release.yml)
ls .github/workflows/ && grep -H 'name:' .github/workflows/*.yml | grep -v '  '

# The changes job's two flags (code/ui) and what each gates
grep -n "outputs.code\|outputs.ui\|changes.outputs" .github/workflows/container-ci.yml

# All container-ci job families, including the newer ones
grep -n '^  [a-z-]*:$\|    name:' .github/workflows/container-ci.yml

# Visual regression + fan-in mechanics (no standalone e2e.yml; transplant not rebase)
grep -n 'commit-goldens\|E2E visual regression\|goldens-\|golden-update' .github/workflows/container-ci.yml

# Build-mode logic and the three modes, plus the release-PR frozen-UI second image
sed -n '150,340p' .github/workflows/container-ci.yml

# PLENUM_IMAGE handoff default
grep -n 'PLENUM_IMAGE' docker-compose.test.yml

# Golden tolerance + naming template + theme axis
grep -n 'maxDiffPixels\|snapshotPathTemplate\|colorScheme' e2e/playwright.config.ts e2e/tests/metrics.spec.ts

# Golden inventory count (368 as of 2026-09)
ls e2e/screenshots | wc -l

# Coverage ratchets (backend / frontend)
grep -n fail_under smart_vent/pyproject.toml
grep -n -A5 thresholds smart_vent/frontend/vite.config.ts

# Cleanup retention default and cron
grep -n 'cron\|retention_days\|RETENTION_DAYS' .github/workflows/ci-image-cleanup.yml

# codeql-issue-sync.yml cadence (daily, not hourly)
grep -n 'cron' .github/workflows/codeql-issue-sync.yml

# Doc-drift check: does RELEASE.md still name the old required check?
grep -n 'Build & Push release image\|build-release' RELEASE.md
```
