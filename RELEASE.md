# Plenum Release Runbook

## Before you release

Run the dry-run validation workflow to confirm the commit is releasable:

1. Go to **Actions → Validate Release → Run workflow**
2. Leave `ref` blank to validate the current branch, or enter a specific SHA
3. All jobs must pass — lint, tests (backend + frontend), Docker build, and healthz smoke test
4. The summary shows whether all three version files agree (`config.yaml`, `pyproject.toml`, `package.json`)

Only proceed once the validation workflow is green.

## Cutting a release

```bash
# 1. Make sure your local main is up to date
git checkout main && git pull origin main

# 2. Tag the release — semver, no skipping patch versions
git tag v0.X.Y

# 3. Push the tag — this triggers the release-pr.yml workflow
git push origin v0.X.Y
```

The workflow will:
- Bump the version in `config.yaml`, `pyproject.toml`, and `frontend/package.json`
- Prepend a section to `CHANGELOG.md` (filtered to real feature PRs, not housekeeping)
- Open a Release PR titled "Release vX.Y.Z"
- Populate the GitHub Release page with the same notes

## Merging the release PR

1. Confirm the **Build (PR validation)** job (in `container-ci.yml`) passed — this is the required check. On a release PR this job builds once and pushes the real `:version` + `:latest` tags (see #337; the old `Build & Push release image` job no longer exists)
2. Confirm the Trivy image scan (also in `container-ci.yml`) shows no CRITICAL vulnerabilities
3. Merge the PR — the Docker image was already pushed during the PR; no second build runs (`docker.yml` skips release merges)
4. The GitHub Release is already published with the correct notes

## Policy

- **One release per day maximum** under normal circumstances
- **All changes must come through a PR** — no direct pushes to `main`, even for hotfixes
- **A patch release is fine** on short notice but still requires a PR and green CI
- **Version numbers are semver**: bug fixes → patch, new features → minor, breaking changes → major

## Hotfix procedure

If something is broken in production:

```bash
# 1. Branch from main (NOT from a feature branch)
git checkout main && git pull origin main
git checkout -b fix/describe-the-problem

# 2. Make the fix, write a test that reproduces the issue
# 3. Open a PR against main — CI must pass
# 4. Once merged, immediately cut a patch release:
git checkout main && git pull origin main
git tag v0.X.Y+1
git push origin v0.X.Y+1
```

Never push directly to `main`, even under pressure. It bypasses CI and creates the exact reactive cycle we've had before.

## If the Docker build fails during a release PR

1. **Do not merge the release PR** — the broken image has already been pushed to GHCR
2. Fix the issue in a new feature PR against `main`
3. After the fix merges, delete the failed release tag and create a new one:
   ```bash
   git push origin :v0.X.Y          # delete remote tag
   git tag -d v0.X.Y                # delete local tag
   git tag v0.X.Y+1                 # new patch version for the fix
   git push origin v0.X.Y+1
   ```
4. The old broken image tag will remain in GHCR but `latest` will not point to it once the new release pushes

## What triggers what

| Action | Workflow | Result |
|--------|----------|--------|
| Push `v*.*.*` tag | `release-pr.yml` | Creates release branch, opens PR, populates GitHub Release notes |
| Open PR → main (non-release) | `container-ci.yml` → `Build (PR validation)` | Single multi-arch build, pushed as throwaway `ci-<sha>` tag; reused by smoke test + °F/°C E2E legs (#333) |
| Open release PR (`release/v*`) | `container-ci.yml` → `Build (PR validation)` | Build pushed as the real `:version` + `:latest`, then Trivy image scan; smoke test + °F/°C round-trip reuse that real image. A second, throwaway, single-arch image is also built with `version: CI` pinned and handed off via artifact — the visual-regression legs use *that* one, since only a `CI`-pinned build freezes volatile UI (`isCI`, `frontend/src/ci.tsx`) enough to match committed goldens |
| Open release PR (`release/v*`) | `validate-release.yml` | Extra dry-run validation pass (also runnable via `workflow_dispatch`) |
| `workflow_dispatch` | `validate-release.yml` | Full dry-run validation, nothing pushed |
| Any PR or push to main | `lint.yml` | Ruff, pytest, mypy, frontend lint+tests, Trivy source scan |
| Any PR | `container-ci.yml` | Docker smoke test, °F/°C round-trip E2E, visual-regression legs + golden fan-in commit |
| Non-release branch merged → main | `docker.yml` → `build-and-push` | Build + push, only when `smart_vent/config.yaml` changed (version bump outside the release flow) |
| Merge to `main` (non-release) | `beta.yml` → `Build & Push Beta` | Builds `./smart_vent`, pushes `ghcr.io/dhruvb14/plenum-beta:X.Y.(Z+1)-beta.N` + `:latest`, then commits the version + changelog bump into `smart_vent_beta/` via `GITHUB_TOKEN` (no CI re-trigger). Skipped on release-PR merges. |

## Beta track (rolling)

Alongside the tagged **stable** track, every non-release merge to `main`
publishes a **beta** build via `.github/workflows/beta.yml`:

- The image is built from the same `./smart_vent` context and pushed to
  `ghcr.io/dhruvb14/plenum-beta:<version>` (multi-arch) + `:latest`.
- The version is derived from the last `v#.#.#` tag as
  `MAJOR.MINOR.(PATCH+1)-beta.<commits-since-tag>` (e.g. `0.30.1-beta.7`), so it
  sorts above the last stable and below the next one for HA update detection.
- The workflow then commits the new `version:` and a regenerated
  `smart_vent_beta/CHANGELOG.md` into the beta pointer add-on, so Home Assistant
  surfaces the update. That push uses `GITHUB_TOKEN`, which does not re-trigger CI.

Beta is a **separate add-on** (`slug: plenum_beta`) with its own database and its
own host ports, so it can be installed alongside stable Plenum. It exists to soak
large or risky changes (currently the auth refactor, #373) before they reach the
stable track — stable only advances when a human pushes a `v#.#.#` tag.

**One-time setup:** make the `ghcr.io/dhruvb14/plenum-beta` GHCR package
**public** (HA Supervisor pulls anonymously) and allow the bot to push the
pointer commit to `main` (branch-protection bypass for `github-actions[bot]`).
