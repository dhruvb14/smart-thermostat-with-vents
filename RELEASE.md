# Plenum Release Runbook

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
- Populate the GitHub Release page with the same notes (retried on transient failure — see below)

## Merging the release PR

1. Confirm the **Build (PR validation)** job (in `container-ci.yml`) passed — this is the required check. On a release PR this job builds once and pushes the real `:version` + `:latest` tags (see #337; the old `Build & Push release image` job no longer exists)
2. Confirm the Trivy image scan (a step of that same `Build (PR validation)` job) reports no blocking findings — any CRITICAL, any HIGH/MEDIUM with a released fix, or any committed secret. On a release PR the scan runs there rather than in the separate `Image vulnerability scan` job (#596) precisely so a blocking finding fails the required check that gates the publish; both use the shared `.github/actions/scan-image` action, so the severity gate is identical
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

## If the GitHub Release page is missing its title/notes

The last step of `release-pr.yml` (`gh release edit`) writes the same notes
onto the GitHub Release object. It's a single API call with no fallback if it
merged after the release PR was already opened — a transient GitHub API 5xx
there (as happened for v0.32.0) leaves the release page showing the bare tag
name instead of "Plenum vX.Y.Z" and no notes, even though the release PR body
is correct. It's now retried automatically (5 attempts, backoff) on the
normal tag-push path, but if it still comes up blank:

1. Go to **Actions → Create Release PR → Run workflow**
2. Set **tag** to the affected release (e.g. `v0.32.0`) and run it against `main`
3. This resync path only edits the GitHub Release notes — it does not
   regenerate `CHANGELOG.md`, bump versions, or reopen the release PR. It
   copies the body of the already-merged "Release vX.Y.Z" PR verbatim, so the
   release PR must already exist (it always does by the time you'd notice
   this — the release-notes step runs after the PR is opened).

## What triggers what

| Action | Workflow | Result |
|--------|----------|--------|
| Push `v*.*.*` tag | `release-pr.yml` | Creates release branch, opens PR, populates GitHub Release notes (retried on transient failure) |
| Manual `workflow_dispatch` on `release-pr.yml` (`tag` input) | `release-pr.yml` | Resync-only repair path: re-populates GitHub Release notes for an already-tagged/PR'd release from its merged release PR body — see "If the GitHub Release page is missing its title/notes" above |
| Open PR → main (non-release) | `container-ci.yml` → `Build (PR validation)` | Single amd64-only build (#413 — multi-arch is release-only), pushed as throwaway `ci-<sha>` tag; reused by smoke test + °F/°C E2E legs (#333). Skipped for docs-only diffs (see note below). |
| Open release PR (`release/v*`) | `container-ci.yml` → `Build (PR validation)` | Build pushed as the real `:version` + `:latest`, then Trivy image scan; smoke test + °F/°C round-trip reuse that real image. A second, throwaway, single-arch image is also built with `version: CI` pinned and handed off via artifact — the visual-regression legs use *that* one, since only a `CI`-pinned build freezes volatile UI (`isCI`, `frontend/src/ci.tsx`) enough to match committed goldens. (A release PR always bumps `config.yaml`/`pyproject.toml`, so it never classifies as docs-only.) |
| Any PR or push to main | `lint.yml` | Ruff, pytest, mypy, frontend lint+tests, Trivy source scan. Skipped for docs-only diffs (see note below). |
| Any PR | `container-ci.yml` | Docker smoke test, °F/°C round-trip E2E, MQTT round-trip E2E (#519), MCP conformance E2E (stateless + stateful, #543), °F/°C + auth visual-regression legs, golden fan-in commit. Skipped for docs-only diffs (see note below). |
| Open PR → main (non-release) | `container-ci.yml` → `Image vulnerability scan` | Trivy-scans the `ci-<sha>` image the build just produced (fork PRs: the `docker load`ed artifact), so base-image CVEs surface on the PR that could fix them instead of piling up until the next release PR (#596). Blocking: any CRITICAL, any HIGH/MEDIUM with a released fix, and any committed secret. LOW, UNKNOWN and unfixable HIGH/MEDIUM are reported to the job summary and the Security tab without blocking. Skipped on release PRs — `Build (PR validation)` scans the published image there. |
| Non-release branch merged → main | `docker.yml` → `build-and-push` | Build + push, only when `smart_vent/config.yaml` changed (version bump outside the release flow). Docs-only pushes are `paths-ignore`d. |
| Open/update a PR → main | `beta.yml` → `Update beta pointer (in PR)` | Writes the beta version + changelog (incl. the PR's title) onto the PR's OWN branch, so it lands on main at merge. No image build; no push to main. Same-repo PRs only. Skipped for docs-only diffs (a docs change produces a byte-identical image). |
| Merge to `main` (non-release) | `beta.yml` → `Publish beta image` | Reads the merged beta version and builds + pushes `ghcr.io/dhruvb14/plenum-beta:X.(Y+1).0-beta.N` + `:latest`. Registry push only — never writes to main. Skipped on release-PR merges. (A docs-only PR that skipped the pointer bump leaves the beta version unchanged, so nothing builds here.) |

### Docs-only path limiting

A PR (or push) whose diff touches **only** documentation-like paths skips the
build/test pipeline. The ignore set is identical across `lint.yml`,
`container-ci.yml`, `beta.yml`, `docker.yml`, and the local `git commit` gate
(`.claude/hooks/precommit-gate.sh`):

- root-level markdown (`*.md` at the repo root)
- `docs/`, `.claude/`, `.jules/`, `.vscode/`, `screenshots/`, `e2e/screenshots/`

`.github/` is intentionally **not** in the set — a workflow change must run the
full pipeline so CI validates itself. Nested markdown outside those dirs (e.g.
`smart_vent/CHANGELOG.md`) is treated as code. PR-triggered workflows gate with
a job-level `if:` on a `changes` job (a skipped required check reports "skipped"
and satisfies branch protection; a workflow-level `paths` filter would strand
the required check and hang the PR — the #412 lesson). `docker.yml` is
push-to-main only, so it can safely use a workflow-level `paths-ignore`.

## Beta track (rolling)

The **beta** add-on (`slug: plenum_beta`) tracks the tip of `main` so risky work
can soak on real installs before it reaches stable. (Auth, #373, and MQTT,
#519, both shipped this way and are now in stable too.) Because `main` is
PR-only (the branch ruleset forbids direct pushes),
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
