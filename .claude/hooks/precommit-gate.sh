#!/usr/bin/env bash
# PreToolUse gate for `git commit` (wired in .claude/settings.json).
#
# Runs the frontend lint + test suite and backend ruff ONLY when the pending
# commit touches code. Docs-only commits skip the suite entirely — matching the
# CI path-limiting in .github/workflows/{lint,container-ci,docker,beta}.yml, so
# a markdown/skills edit doesn't burn minutes (or tokens) on checks that can't
# be affected by it.
#
# Docs-only ignore set (kept identical to the CI `changes` classifiers):
#   - root-level markdown (README/CLAUDE/DESIGN/RELEASE, *.md at repo root)
#   - docs/            (feature docs)
#   - .claude/         (skill library, hooks, settings)
#   - .jules/ .vscode/ (editor/agent config)
#   - screenshots/     (README images)
#   - e2e/screenshots/ (committed golden PNGs)
# .github/ is deliberately NOT docs — a workflow edit must still run the suite.
# Nested markdown outside those dirs (e.g. smart_vent/CHANGELOG.md) is code.
set -euo pipefail

# Resolve the repo root from this script's own location (.claude/hooks/…),
# so the gate keeps working if the checkout lives somewhere else.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# Everything the pending commit could include: staged changes, plus unstaged
# modifications to already-tracked files (covers `git commit -a`). Untracked
# files aren't committed unless explicitly `git add`-ed, so staged captures them.
CHANGED="$(
  {
    git -C "$ROOT" diff --cached --name-only
    git -C "$ROOT" diff --name-only
  } | sort -u
)"

DOCS_RE='^(docs/|\.claude/|\.jules/|\.vscode/|screenshots/|e2e/screenshots/)|^[^/]+\.md$'

if printf '%s\n' "$CHANGED" | grep -vE "$DOCS_RE" | grep -q .; then
  echo "Code change detected — running lint + tests before commit…"
  npm --prefix "$ROOT/smart_vent/frontend" run lint
  npm --prefix "$ROOT/smart_vent/frontend" run test:coverage
  cd "$ROOT/smart_vent"
  ruff check backend/
  ruff format --check backend/
else
  echo "Docs-only change — skipping lint + tests."
fi
