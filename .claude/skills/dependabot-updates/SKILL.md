---
name: dependabot-updates
description: >
  Consolidate every open Dependabot pull request in a repository into a single
  combined "dependency roll-up" PR whose body lists `Closes #N` for each original
  PR, then verify it and close out the originals once it merges. Use this
  whenever someone wants to batch, combine, consolidate, roll up, squash,
  merge-together, or "deal with all" their Dependabot / dependency-bot PRs —
  including phrasings like "I have 12 dependabot PRs, make them one PR",
  "clean up the dependabot backlog", "combine the dependency bumps", "one PR for
  all the version bumps", or "merge all the dependabot stuff". Also use it when
  someone is drowning in weekly bot PRs and wants fewer CI runs or one review
  instead of many, even if they don't say the word "Dependabot".
---

# Dependabot roll-up

Turn N open Dependabot PRs into one reviewable PR, one CI run, one merge.

The value is not just tidiness. Each Dependabot PR burns a full CI matrix, and
they are all cut from the same base, so merging them one at a time forces every
remaining PR to rebase and re-run. Rolling them up collapses N × CI into 1 × CI
and gives the reviewer a single diff where the whole dependency delta is visible
at once. The cost is coupling: one bad bump now blocks the whole batch, which is
why the triage step below takes red CI seriously instead of sweeping everything
in.

## The shape of the work

1. Discover the open Dependabot PRs and read their state
2. Triage — decide what goes in the batch and what stays behind
3. Build the consolidated branch by cherry-picking bot commits
4. Resolve lockfiles by regenerating, never by hand
5. Verify with the repo's own checks
6. Open the roll-up PR with `Closes #N` for each original
7. After it merges, close out the originals

Work through these in order. Steps 1–2 are cheap and shape everything after
them, so do not skip ahead to branching.

## 1. Discover

Identify the repo and its default branch first — never assume `main`.

Prefer the `gh` CLI when it exists; fall back to the GitHub MCP tools
(`list_pull_requests`, `pull_request_read`) when it doesn't. Both are common;
check with `command -v gh` rather than guessing.

```bash
gh pr list --state open --author "app/dependabot" \
  --json number,title,headRefName,headRefOid,baseRefName,createdAt,labels,url \
  --limit 100
```

With MCP: `list_pull_requests` with `state: open`, then keep only rows whose
`user.login` is `dependabot[bot]`.

Filter on **author**, not labels. Labels are configured per repo in
`dependabot.yml` and are frequently absent, renamed, or applied by other
automation — author identity is the only reliable signal. Note that a
human-authored PR that happens to bump a dependency is *not* a Dependabot PR and
must not be swept in; the roll-up's promise to the reviewer is "this is only
bot-generated version bumps."

Then parse each title into structured facts — ecosystem, package, from-version,
to-version. Dependabot titles are formulaic and worth reading carefully:

- `chore(deps): bump vite from 8.2.0 to 8.2.1 in /frontend` → npm, vite, 8.2.0→8.2.1
- `chore(deps): update ruff requirement from >=0.16.0 to >=0.16.2 in /backend` → pip, ruff, >=0.16.0→>=0.16.2
- `chore(deps): bump the github-actions group with 3 updates` → a grouped update; expand it by reading the PR body

This table is what the reviewer actually reads in the final PR, so build it
properly rather than pasting raw titles.

## 2. Triage before you batch

Pull each PR's check state (`gh pr checks <N>`, or `pull_request_read` with
`method: get_check_runs`). Then sort into three buckets:

- **Green or still pending** → include. Pending is fine; the roll-up re-runs CI
  from scratch anyway, so an unfinished run tells you nothing you won't learn
  better in step 5.
- **Red** → leave out by default and say so explicitly in your report. A failing
  bump inside a roll-up converts one broken dependency into a blocked batch, and
  the reviewer loses the ability to merge the other five while someone fixes it.
  Excluding it keeps its own PR alive and independently fixable.
- **Major-version bumps** → surface them, even when green. Semver-major means a
  deliberate migration decision, and burying one among nine patch bumps is how
  a breaking change gets merged unread. Many repos already tell Dependabot to
  ignore majors (check `.github/dependabot.yml`); if one appears anyway, it
  earned its own review.

Report the buckets and your proposed batch before you start branching. If
everything is green and minor, just proceed and mention it — asking permission
for the obvious wastes the user's time. Ask only when the split is genuinely a
judgment call: several red PRs, or a major bump the user may want anyway.

## 3. Build the branch — cherry-pick, don't merge

Cut a fresh branch from the up-to-date base:

```bash
git fetch origin <base>
git checkout -B chore/dependabot-rollup-<YYYY-MM-DD> origin/<base>
```

Now the important part. **Take only the commits Dependabot itself authored on
each branch, not the whole branch.**

Bot branches accumulate commits from *other* automation — regenerated golden
screenshots, version-pointer bumps, changelog regeneration, formatter passes.
Every Dependabot branch gets its own copy of those, so merging N branches means
N conflicting variants of the same generated file, and you'd spend the whole
task hand-resolving artifacts that the repo's own bots will simply regenerate on
the new branch anyway. Cherry-picking the dependency commits sidesteps that
entirely and yields a history a reviewer can read: one `chore(deps)` commit per
bump, in PR order.

```bash
git fetch origin <headRefName>            # per PR
git log --author="dependabot" --format=%H --reverse \
  origin/<base>..origin/<headRefName>     # the commits worth taking
git cherry-pick <sha>...                  # oldest PR first
```

Go in ascending PR number order. It makes the run reproducible, and when two
PRs touch the same manifest line the later (usually newer) bump lands on top,
which is the outcome you want.

If a branch has *no* Dependabot-authored commit, something is off — a rebase may
have rewritten authorship. Fall back to `git cherry-pick` of the commits whose
message matches the PR title, and say what you did.

## 4. Lockfile conflicts: regenerate, never hand-merge

Conflicts fall into three kinds, and only one of them is real work.

**Lockfiles** (`package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`, `poetry.lock`,
`uv.lock`, `Gemfile.lock`, `Cargo.lock`, `go.sum`, `composer.lock`). These will
conflict constantly, because every npm bump rewrites overlapping regions of the
same file. Never resolve them by choosing hunks — a hand-merged lockfile can be
internally inconsistent in ways that install cleanly today and break
reproducibly later, which is precisely the failure a lockfile exists to prevent.
Instead take the manifest resolution, then let the ecosystem's own tool rebuild
the lock from it. See `references/lockfiles.md` for the regeneration command per
ecosystem.

```bash
git checkout --theirs <lockfile>   # or --ours; either way it gets rebuilt
# resolve the manifest by hand (below), then regenerate:
npm install --package-lock-only --prefix <dir>
git add <lockfile> <manifest>
git cherry-pick --continue
```

**Manifests** (`package.json`, `pyproject.toml`, `requirements.txt`, `go.mod`).
Real but easy: two bumps landed on adjacent or identical lines. Keep *both*
bumps at their new versions. The only trap is a same-package conflict — if two
PRs bump the same package, keep the higher version and note that the lower PR is
subsumed.

**Generated artifacts** (screenshots, changelogs, version pointers, build
output). Drop them; take the base branch's version. The automation that produced
them re-runs against your consolidated branch and regenerates them correctly.
Carrying six branch-local variants forward is pure noise.

## 5. Verify before you push

A roll-up is only worth batching if it is verified as a batch. Run the checks the
repo actually uses — read `CLAUDE.md`, `CONTRIBUTING.md`, or the CI workflows to
find them rather than assuming a stack. Typically:

- install from the regenerated lockfiles (`npm ci` is the one that proves the
  lockfile is coherent — it fails loudly where `npm install` would silently fix
  it, which is exactly the property you want here)
- linters and formatters, especially when a linter itself was bumped, since a
  new lint release routinely adds rules that fail existing code
- the test suite
- a build, when the toolchain (bundler, compiler, type-checker) was bumped

When something fails, identify **which bump** caused it before doing anything
else. `git log --oneline` plus the failure message usually names it outright (a
ruff bump breaking `ruff format --check` is not subtle). Drop that one commit
from the batch, re-verify, and report it as excluded with the reason — the other
bumps should not be held hostage. Resist the urge to fix the underlying code as
part of a dependency roll-up; that turns a mechanical PR into a reviewed change
and defeats the point.

## 6. Open the roll-up PR

Push and open a normal, ready-for-review PR. Title it so the batch is obvious at
a glance:

```
chore(deps): consolidate N Dependabot updates
```

Structure the body like this — the table is what makes a ten-bump PR reviewable,
and the `Closes` list is what ties it back to the originals:

```markdown
Rolls up the N open Dependabot PRs into one branch so the batch gets a single
review and a single CI run instead of N.

## Updates

| PR | Ecosystem | Package | From | To |
|----|-----------|---------|------|-----|
| #551 | pip | ruff | >=0.16.0 | >=0.16.2 |
| #556 | npm | vite | 8.2.0 | 8.2.1 |

## Excluded

- #557 — react 18→19 (major; wants its own review)

## Test plan

- [x] `npm ci` — lockfile installs clean
- [x] <lint command>
- [x] <test command>

Closes #551
Closes #552
```

One `Closes #N` per line, each on its own line — GitHub only parses the keyword
form, so `Closes #551, #552` links the first and silently ignores the rest.

**What the `Closes` lines actually do.** Merging the roll-up into the default
branch closes each linked PR on its own, within a second or two, and Dependabot
follows with its "OK, I won't notify you again about this release"
acknowledgement on each. This was verified on a real six-PR roll-up: all six
flipped to closed 1–3 seconds after the merge, sequentially, with no repo
automation involved and far too fast for Dependabot's own supersede path (which
posts a different comment — "looks like X is up-to-date now" — and takes minutes,
not seconds).

Worth knowing because it is easy to get backwards: GitHub's documentation frames
closing keywords almost entirely in terms of *issues*, which invites the
confident-sounding claim that they do nothing for pull requests. Observed
behaviour says otherwise. Trust the check in step 7 over either assumption.

## 7. Verify the close-out

Once the roll-up merges, confirm the originals actually closed:

```bash
gh pr list --state open --author "app/dependabot" --json number,title
```

Expect an empty list — or only the PRs you deliberately excluded in step 2. In
the normal case the `Closes` lines have already done the work and there is
nothing to do; say so and stop rather than posting redundant comments.

If a rolled-up PR is somehow still open, close it explicitly with a comment
pointing at the roll-up. Prefer `@dependabot close`, which closes the PR *and*
records that it was handled so Dependabot doesn't resurrect the branch:

```bash
gh pr comment <N> --body "Superseded by #<rollup> (merged). @dependabot close"
```

Never close an original before the roll-up has merged — that strands the update
with nothing carrying it. Also delete the merged bot branches if the repo
doesn't prune them automatically.

## Reporting back

Give the user the roll-up PR link, the count rolled up, anything excluded with
its reason, and the verification results — including failures, stated plainly.
A dependency PR that claims a green test plan it never ran is worse than no PR,
because it is exactly the kind of change reviewers approve on trust.
