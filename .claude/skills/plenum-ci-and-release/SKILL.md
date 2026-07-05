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
2026-07 (v0.22.1). **The repo YAML is the source of truth** — two docs of
record lag behind it (see "Known doc drift" at the end):

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

All seven workflows live in `.github/workflows/`. Job names below are quoted
exactly from the YAML (`name:` fields); these are the strings you see as PR
checks.

| Workflow file | Trigger | Jobs (check names) |
|---|---|---|
| `lint.yml` ("Lint & Test") | push to `main` AND `pull_request` → main; both `paths-ignore: e2e/screenshots/**` | `Python (ruff)`, `Python (pytest)`, `Python (mypy)`, `Frontend (ESLint + Prettier)`, `Security (Trivy source scan)` |
| `container-ci.yml` ("Container CI") | `pull_request` → main (`paths-ignore: e2e/screenshots/**`) + `workflow_dispatch`; concurrency group per PR, `cancel-in-progress: true` | `Build (PR validation)`, `Docker Smoke Test`, `Round-trip (F)` / `Round-trip (C)`, `E2E visual regression (F)` / `(C)`, `Commit updated goldens` |
| `docker.yml` ("Build & Push Docker Image") | push to `main` only (`paths-ignore: e2e/screenshots/**`) | `Build & Push (main — config changed)` |
| `release-pr.yml` ("Create Release PR") | push of tag `v*.*.*` | `Create Release PR` |
| `validate-release.yml` ("Validate Release") | `workflow_dispatch` (any ref) + `pull_request` → main, job-gated `if: github.event_name == 'workflow_dispatch' \|\| startsWith(github.head_ref, 'release/v')` | `Release Validation` |
| `ci-image-cleanup.yml` ("Cleanup CI Images") | cron `17 4 * * *` (04:17 UTC nightly) + `workflow_dispatch` (inputs `retention_days` default 14, `dry_run`) | `Delete stale ci-* images` |
| `codeql-issue-sync.yml` ("CodeQL Issue Sync") | cron `0 * * * *` (hourly) + `workflow_dispatch` | `create_issue` (files a GitHub issue per open CodeQL alert, labels `security,codeql`, dedupes by "CodeQL Alert #N" in title) |

Key trigger facts:

- **The only path filter anywhere is `e2e/screenshots/**`.** A docs-only or
  skills-only PR still runs the FULL pipeline including both visual-regression
  legs. There is no "docs change → skip E2E" shortcut. (This is why golden-bot
  pushes land on markdown-only PRs — see §3.)
- The screenshots ignore + the fact that **GITHUB_TOKEN pushes never trigger
  workflows** are the two independent guards against the golden bot re-triggering CI.
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

Two more things the build job does:

- **Version pin for determinism**: non-release modes run
  `sed -i 's/^version:.*/version: CI/' smart_vent/config.yaml` before building,
  which bakes `VITE_APP_VERSION=CI` into the bundle → `isCI`/`<Frozen>`
  (`frontend/src/ci.tsx`) freezes volatile UI so goldens are stable. Release
  PRs keep the real version (the published image must show it).
- Outputs `image`, `is_fork`, `is_release` consumed by every downstream job.

**PLENUM_IMAGE handoff**: `docker-compose.test.yml`'s plenum service is
`image: "${PLENUM_IMAGE:-plenum-e2e}"`. Each E2E leg sets
`PLENUM_IMAGE=<ghcr tag>` (same-repo) or `PLENUM_IMAGE=plenum-e2e` (fork,
where the loaded image already carries the compose-default tag). The visual
job additionally `docker tag`s the pulled image `plenum-e2e`. Nothing in a PR
run ever rebuilds the image.

Downstream jobs, exactly as named:

- **`Docker Smoke Test`** (`smoke`): runs the image with dummy `HA_URL`/`HA_TOKEN`,
  polls `/api/healthz` on :8099 (30×1s), then exercises the MCP surface on
  :9099/mcp — asserts 503 while `mcp_enabled` is off, enables it via
  `POST /api/system/mcp`, runs a real `initialize`, and checks `tools/list`
  contains `get_healthz`.
- **`Round-trip (F)` / `Round-trip (C)`** (`conversion`, matrix `unit: [F, C]`,
  `fail-fast: false`, 25 min timeout): full HA + Plenum compose stack
  (°C leg layers `docker-compose.test.celsius.yml`), verifies
  `/api/settings` reports the matrix unit, then runs
  `npx playwright test temperature-units.spec.ts --project=chromium`. This is
  the end-to-end guard against #231-class double conversion.
- **`E2E visual regression (F)` / `(C)`** and **`Commit updated goldens`** — §3.

## 3. Visual-regression golden machinery (current, post-#366)

History in one line: the old standalone `e2e.yml` ran legs serially
(`max-parallel: 1`) and each leg committed its own goldens (two commits, push
races). #366 (`a0e9d04`) moved it into `container-ci.yml` with **parallel
legs + one fan-in commit**; #369/#370 (`c26d395`) fixed the push, which failed
from actions/checkout's detached HEAD — the fix is pushing `HEAD:"$BRANCH"`
instead of `"$BRANCH"`.

### The legs (`e2e`, matrix `unit: [F, C]`, both run in parallel)

Each leg: obtain image (§2) → compose up HA (°C leg layers the celsius
override) → mint HA token (`e2e/scripts/setup-ha.py`) → start Plenum → assert
active unit matches the matrix → then a three-pass Playwright dance, always
with `--grep-invert "Temperature round-trip"` (that spec mutates shared
backend state and is covered by the `conversion` job instead):

1. **Run** (`continue-on-error: true`). All screenshots match committed
   goldens → leg passes, nothing uploaded.
2. **Regenerate** (only if pass 1 failed): `npx playwright test
   --update-snapshots ...`. A non-screenshot failure (missing element,
   timeout) fails here too — no spurious golden update.
3. **Verify**: plain run again against the regenerated goldens. Only if this
   passes does the leg stage **its own unit's PNGs only**
   (`cp e2e/screenshots/*-"${UNIT_LABEL}"-* /tmp/goldens/`) and upload
   artifact `goldens-F` / `goldens-C` (retention 1 day). Staging only the
   leg's unit prevents one leg's stale copy of the *sibling* unit's goldens
   from reverting what the sibling regenerated.

Legs have `contents: read` only — they cannot push.

### The fan-in (`Commit updated goldens`, job id `commit-goldens`)

`needs: e2e`, `if: always() && needs.e2e.result != 'skipped'`,
`contents: write`. Downloads `goldens-*` artifacts with
`merge-multiple: true` into `e2e/screenshots/` (continue-on-error covers the
no-artifacts case), then:

```
git add -- e2e/screenshots/
# empty diff → "nothing to commit", clean exit
git commit -m "ci: update E2E golden screenshots (F + C)"
git fetch origin "$BRANCH" && git rebase "origin/$BRANCH"
git push origin HEAD:"$BRANCH"     # HEAD: form — the #369/#370 detached-HEAD fix
```

Push semantics and races:

- The commit lands **directly on the PR branch** as `github-actions[bot]`.
- **It does not re-trigger CI**: GITHUB_TOKEN pushes never trigger workflows,
  and belt-and-braces, both `lint.yml` and `container-ci.yml` paths-ignore
  `e2e/screenshots/**`. So the E2E legs stay red on that run; the goldens are
  simply now correct for the *next* run.
- If the branch advanced while CI ran, the rebase absorbs it. If the same PNG
  was changed on both sides, the rebase hits a binary conflict and the job
  fails — re-run the workflow (inferred from git semantics; not yet observed).
- **Rule for agents: after any CI run on your PR, `git fetch` and rebase
  before pushing.** A local push made after the bot's push is rejected as
  non-fast-forward; never `--force` over the bot commit or you revert the
  goldens and CI loops.

### Known behavior: golden-bot pushes on docs-only PRs (observed on PR #388)

PR #388 changed only `.claude/skills/` markdown, yet the bot pushed
`f0dd26b` ("ci: update E2E golden screenshots (F + C)") regenerating 8
`room-detail-*` / `rooms-*` PNGs with tiny byte deltas. This is working as
designed, not a bug:

- §1: the visual matrix runs on **every** PR unless the only changed paths are
  goldens. Markdown changes don't exempt it.
- The first pass failed on sub-pixel diffs exceeding the default
  `maxDiffPixels: 100` (`e2e/playwright.config.ts`); regenerate + verify
  passed, so the fan-in committed. Likely cause (inferred): rendering-
  environment drift on the runner (Chromium/font updates) since those goldens
  were last regenerated — the Fahrenheit PNGs shifted by hundreds of bytes,
  the Celsius ones by only a few, and no frontend code changed on the PR.
- **Expect this on any PR.** Budget for it: pull the bot commit before your
  next push, and glance at the changed PNGs in the diff (they should be
  visually identical; a *visible* change on a code-free PR is a red flag).

### Tolerances

Global `maxDiffPixels: 100`; `e2e/tests/metrics.spec.ts` overrides to `800`
because the `mobile` project's `deviceScaleFactor: 3` amplifies native
`<input type="date">` jitter ~9×. Prefer a per-spec bump over masking. Golden
inventory: 92 PNGs = pages × {Fahrenheit, Celsius} × {chromium, mobile}
(as of 2026-07). Adding/inventorying goldens → `plenum-validation-and-qa`.

## 4. Release flow end-to-end

Runbook of record: `RELEASE.md` (but see doc drift, §7). Policy: max one
release/day, everything through PRs, semver, no direct pushes to main.

1. **Pre-flight (optional but recommended)**: Actions → "Validate Release" →
   Run workflow on your ref. `Release Validation` is a full dry run: ruff +
   pytest + frontend lint/tests, Docker build (version pinned to CI),
   healthz smoke, then visual regression against the committed **Fahrenheit**
   goldens on a live HA+Plenum stack (°C leg is covered per-PR by
   container-ci's matrix). Nothing is pushed.
2. **Tag**: `git checkout main && git pull`, `git tag v0.X.Y`,
   `git push origin v0.X.Y`.
3. **`release-pr.yml` fires on the tag**: derives version, bounds the range at
   the previous semver tag, generates changelog bullets from merged-PR titles
   (skipping "Release vX" housekeeping PRs, linking closing issues), collects
   contributors, prepends to `smart_vent/CHANGELOG.md`, bumps the version in
   **three files + lockfile** (`smart_vent/config.yaml`, `smart_vent/pyproject.toml`,
   `smart_vent/frontend/package.json` + `package-lock.json`), force-pushes
   branch `release/vX.Y.Z`, opens PR "Release vX.Y.Z" → main, and writes the
   same notes onto the GitHub Release for the tag.
4. **On the release PR**, container-ci's `Build (PR validation)` runs in
   release mode (§2): builds once, pushes the **real** `:<version>` + `:latest`
   to GHCR, Trivy-scans the published image (CRITICAL ⇒ fail), and the smoke +
   round-trip + visual jobs all test *exactly the artifact being published*
   via the `:<version>` tag. `validate-release.yml` also attaches to release
   PRs but **won't auto-start** — the bot opened the PR with GITHUB_TOKEN, so
   GitHub's loop guard suppresses it; start it with one click from the PR's
   checks ("Run"/"Approve and run").
5. **Merge**: nothing rebuilds. `docker.yml`'s `Build & Push (main — config
   changed)` detects the release-merge commit message
   (`release/vX.Y.Z` pattern) and skips — the image already shipped during
   the PR. `docker.yml` only actually builds when a **non-release** merge to
   main changed `smart_vent/config.yaml` (a version bump landing outside the
   release flow).
6. **If the Docker build fails on a release PR**: do NOT merge — the broken
   image is already in GHCR. Fix via a normal PR, delete the tag
   (`git push origin :v0.X.Y; git tag -d v0.X.Y`), tag the next patch version.

**Branch protection**: per CLAUDE.md, the required check for release PRs is
**`Build (PR validation)`** (container-ci), replacing the old "Build & Push
release image". The full required-check list lives in GitHub repo settings
and is UNVERIFIED from the working tree (needs admin API access).

## 5. Failure triage table

| Red check / symptom | Likely cause | Fix |
|---|---|---|
| `Python (ruff)` fails only on `ruff format --check` | unformatted code | `cd smart_vent && ruff format backend/`, commit |
| `Python (pytest)` fails with coverage error | backend coverage below `fail_under = 90` (`pyproject.toml`) | add tests — patterns in `plenum-validation-and-qa` |
| `Python (pytest)` fails in `test_temperature_field_parity.py` | temperature field added to only 1–2 of the 3 registries (`routes.py` `TEMPERATURE_FIELDS`, `e2e/tests/temperature-fields.ts`, `// @covers:` tag in the spec) | complete all three — checklist in `plenum-change-control` §2.1 |
| `Python (pytest)` fails in `test_addon_config.py` | `config.yaml` option without matching `bashio::config` in `run.sh` | add to both files |
| `Python (pytest)` fails in `test_api_spec_enforcement.py` | new `/api/` route without `@docs` + `@response_schema` | add the decorators |
| `Frontend (ESLint + Prettier)` coverage failure | below `vite.config.ts` thresholds | add tests → `plenum-validation-and-qa` |
| `E2E visual regression (F)/(C)` red, then a bot commit `ci: update E2E golden screenshots (F + C)` appears | screenshots drifted (your UI change, or environment jitter on a code-free PR — §3) | `git pull` the bot commit, review every changed PNG in the diff like code; red legs on *that* run are expected and clear on the next run |
| Visual legs red, **no** bot commit | regenerate or verify pass also failed → a real breakage (missing element, timeout), or new volatile UI | download `playwright-results-F/C` artifact; wrap new timers/feeds/clocks in `<Frozen>` (`frontend/src/ci.tsx`) |
| Goldens never stabilise across runs (bot commits every run) | time-varying UI not frozen | `<Frozen>` wrap — see CLAUDE.md pitfall 8 |
| Your `git push` rejected non-fast-forward after CI | golden bot pushed to your branch first | `git fetch && git rebase origin/<branch>`; never force-push over the bot |
| `Commit updated goldens` job itself fails on rebase/push | branch advanced with a conflicting PNG, or push race | re-run the workflow from the Actions tab (or `workflow_dispatch` container-ci) |
| Fork PR: downstream jobs can't find the image | fork mode hands off via artifact `plenum-image`, not GHCR; if `Build (PR validation)` failed, nothing downstream gets an image | fix the build first; remember fork builds are amd64-only |
| `Round-trip (F)` or `(C)` fails | unit-conversion contract broken (a #231-class bug) or the stack's unit didn't match the matrix | contract → `plenum-architecture-contract`; triage → `plenum-debugging-playbook` |
| `Release Validation` shows as expected-but-not-running on a release PR | GITHUB_TOKEN loop guard — by design | click "Run"/"Approve and run" on the PR checks |
| `Security (Trivy source scan)` fails | plain `grep -q "CRITICAL"` on the fs-scan table — any CRITICAL line fails (stricter/noisier than the image scan's `CRITICAL: [1-9]` regex) | fix or bump the vulnerable dep; image-scan suppressions go in `.trivyignore` |
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

## 7. Known doc drift (as of 2026-07, v0.22.1)

| Doc | Stale claim | Reality |
|---|---|---|
| `CLAUDE.md` "E2E visual regression" section | standalone `.github/workflows/e2e.yml`; `max-parallel: 1`; each leg commits its own goldens; "verify with updated goldens runs in the same job" via `git checkout -f -B` | no `e2e.yml` exists; legs run in parallel inside `container-ci.yml`; verify happens per-leg, commit happens once in the `Commit updated goldens` fan-in (#366) |
| `CLAUDE.md` same section | `mode=pull` polling loop in "Decide image source" | replaced by `needs: build` + direct pull since the move into container-ci |
| `RELEASE.md` "What triggers what" table (pre-2026-07-05) | `docker.yml` → `build-pr` / `build-release`; required check "Build & Push release image" | corrected 2026-07-05 (PR #388): all PR container work is `container-ci.yml`'s `Build (PR validation)`, which is the required check |

When these disagree, the workflow YAML wins. If you touch the workflows,
update `RELEASE.md`'s table and CLAUDE.md's E2E section in the same PR.

## Provenance and maintenance

Facts verified 2026-07-04 on branch `claude/skill-library-continuity-qit89f`
(v0.22.1) by reading all seven workflow files, `RELEASE.md`,
`e2e/playwright.config.ts`, `docker-compose.test.yml`, and commits `a0e9d04`
(#366), `c26d395` (#369/#370), `f0dd26b` (PR #388 golden-bot push). The PR
#388 root-cause attribution to runner rendering drift is **inferred** (no
runner-side evidence available); everything else is read from source.

Re-verify before trusting:

```bash
# Workflow inventory + exact job names
ls .github/workflows/ && grep -H 'name:' .github/workflows/*.yml | grep -v '  '

# Visual regression + fan-in still live in container-ci (no standalone e2e.yml)
grep -n 'commit-goldens\|E2E visual regression\|goldens-' .github/workflows/container-ci.yml

# Build-mode logic and the three modes
sed -n '80,120p' .github/workflows/container-ci.yml

# PLENUM_IMAGE handoff default
grep -n 'PLENUM_IMAGE' docker-compose.test.yml

# Golden tolerance + naming template
grep -n 'maxDiffPixels\|snapshotPathTemplate' e2e/playwright.config.ts e2e/tests/metrics.spec.ts

# Golden inventory count (92 as of 2026-07)
ls e2e/screenshots | wc -l

# Cleanup retention default and cron
grep -n 'cron\|retention_days\|RETENTION_DAYS' .github/workflows/ci-image-cleanup.yml

# Doc-drift check: does RELEASE.md still name the old required check?
grep -n 'Build & Push release image\|build-release' RELEASE.md
```
