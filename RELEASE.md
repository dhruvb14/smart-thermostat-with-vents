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

1. Confirm the `Build & Push release image` CI job passed (this is the required check)
2. Confirm the Trivy scan shows no CRITICAL vulnerabilities
3. Merge the PR — the Docker image was already pushed during the PR; no second build runs
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
| Open PR → main (non-release) | `docker.yml` → `build-pr` | Docker build only, no push |
| Open release PR | `docker.yml` → `build-release` | Docker build + push `:version` and `:latest` |
| `workflow_dispatch` | `validate-release.yml` | Full dry-run validation, nothing pushed |
| Any PR or push to main | `lint.yml` | Lint, tests, security scan, Docker smoke test |
