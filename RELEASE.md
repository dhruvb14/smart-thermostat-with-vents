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
| Open/update a PR → main | `beta.yml` → `Update beta pointer (in PR)` | Writes the beta version + changelog (incl. the PR's title) onto the PR's OWN branch, so it lands on main at merge. No image build; no push to main. Same-repo PRs only. |
| Merge to `main` (non-release) | `beta.yml` → `Publish beta image` | Reads the merged beta version and builds + pushes `ghcr.io/dhruvb14/plenum-beta:X.(Y+1).0-beta.N` + `:latest`. Registry push only — never writes to main. Skipped on release-PR merges. |

## Beta track (rolling)

The **beta** add-on (`slug: plenum_beta`) tracks the tip of `main` so risky work
(currently the auth refactor, #373) can soak on real installs before it reaches
stable. Because `main` is PR-only (the branch ruleset forbids direct pushes),
`.github/workflows/beta.yml` splits into two event-gated jobs that **never push
to `main`**:

- **On a pull request → `main`** (`Update beta pointer`): computes the next beta
  version (`MAJOR.(MINOR+1).0-beta.<commits-since-last-minor-tag>`), regenerates
  `smart_vent_beta/CHANGELOG.md` (including *this PR's* title + number), bumps
  `smart_vent_beta/config.yaml`, and commits them onto the **PR's own branch**
  with `GITHUB_TOKEN`. The beta bump is part of the PR diff and lands on `main`
  when the PR merges — so no post-merge write to `main` is ever needed. (Same-repo
  PRs only; fork PRs skip, since their token is read-only.) No image is built here.
- **On push to `main`** (`Publish beta image`): the version is already merged, so
  this job just reads it and builds + pushes the multi-arch image to
  `ghcr.io/dhruvb14/plenum-beta:<version>` + `:latest`. A registry push — never a
  git push to `main`. It builds only when the beta version actually changed.

Because the pointer rides inside the feature PR (not a separate bot PR) there is
no PR-generating loop, and because `GITHUB_TOKEN` commits don't re-trigger
workflows the bot's own pointer commit never re-runs the job. The version counter
excludes `smart_vent_beta/` commits, so those bot commits can't inflate it.

Beta is a **separate add-on** with its own database and host ports, installable
alongside stable Plenum. Stable still advances only on a human `v#.#.#` tag.

**One-time setup:** make the `ghcr.io/dhruvb14/plenum-beta` GHCR package
**public** (HA Supervisor pulls anonymously). No branch-protection change is
needed — the PR-based flow keeps `main` strictly PR-only.
