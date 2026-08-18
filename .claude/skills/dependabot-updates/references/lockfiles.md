# Lockfile regeneration by ecosystem

Read this when a cherry-pick conflicts on a lockfile during a Dependabot
roll-up. The rule is always the same: resolve the **manifest** by hand (keep
both bumps), then rebuild the **lock** with the ecosystem's own tool. Hand-edited
lockfiles are the one artifact where a plausible-looking merge can be silently
wrong — the file exists to pin a resolved dependency graph, and a hunk-by-hunk
merge of two graphs is not a graph.

Each command below regenerates the lock from the manifest **without** upgrading
anything else, which keeps the roll-up's diff equal to the sum of its bumps.

| Ecosystem | Manifest | Lockfile | Regenerate from manifest |
|---|---|---|---|
| npm | `package.json` | `package-lock.json` | `npm install --package-lock-only` |
| yarn (berry) | `package.json` | `yarn.lock` | `yarn install --mode update-lockfile` |
| yarn (classic) | `package.json` | `yarn.lock` | `yarn install --frozen-lockfile=false` |
| pnpm | `package.json` | `pnpm-lock.yaml` | `pnpm install --lockfile-only` |
| bun | `package.json` | `bun.lock` | `bun install --frozen-lockfile=false` |
| pip / setuptools | `pyproject.toml`, `requirements.txt` | often none | nothing to regenerate |
| uv | `pyproject.toml` | `uv.lock` | `uv lock` |
| poetry | `pyproject.toml` | `poetry.lock` | `poetry lock --no-update` |
| pipenv | `Pipfile` | `Pipfile.lock` | `pipenv lock` |
| bundler | `Gemfile` | `Gemfile.lock` | `bundle lock` |
| cargo | `Cargo.toml` | `Cargo.lock` | `cargo update -p <package> --precise <version>` |
| go modules | `go.mod` | `go.sum` | `go mod tidy` |
| composer | `composer.json` | `composer.lock` | `composer update --lock` |
| gradle | `build.gradle` | `gradle.lockfile` | `gradle dependencies --write-locks` |
| maven | `pom.xml` | none | nothing to regenerate |

## Verifying the result

Regenerating is not proof. Follow it with the ecosystem's strict install, which
fails when the lock and manifest disagree instead of quietly reconciling them:

| Ecosystem | Strict install |
|---|---|
| npm | `npm ci` |
| yarn | `yarn install --immutable` |
| pnpm | `pnpm install --frozen-lockfile` |
| uv | `uv sync --locked` |
| poetry | `poetry check --lock` |
| bundler | `bundle install --frozen` |
| cargo | `cargo build --locked` |
| go | `go mod verify` |
| composer | `composer validate --check-lock` |

If the strict install fails, the conflict was resolved wrong — usually a
same-package bump where both versions survived into the manifest. Fix the
manifest, regenerate, and re-run.

## Ecosystems with no lockfile

pip-without-a-locker, maven, and plain `requirements.txt` repos have no lock to
rebuild, so conflicts there are ordinary manifest conflicts: keep both bumps,
keep the higher version when the same package appears twice. There is nothing to
regenerate and no strict-install check to run — the test suite is the only gate.
