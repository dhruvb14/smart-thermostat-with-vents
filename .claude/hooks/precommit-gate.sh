#!/usr/bin/env bash
# PreToolUse gate for `git commit` (wired in .claude/settings.json).
#
# Runs the frontend lint + test suite and backend ruff ONLY when the pending
# commit touches code. Docs-only commits skip the suite entirely — matching the
# CI path-limiting in .github/workflows/{lint,container-ci,docker,beta}.yml, so
# a markdown/skills edit doesn't burn time (or tokens) on checks that can't be
# affected by it.
#
# It is also SELF-BOOTSTRAPPING: a fresh clone has no node_modules and no ruff
# on PATH, which would make the checks fail spuriously ("Cannot find package
# '@eslint/js'", "ruff: command not found"). When a required tool is missing the
# gate installs it first, then runs the checks. Installs happen only on the code
# path, and only when something is actually missing — a normal commit with deps
# already present pays just a couple of `command -v` / file-exists checks.
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
#
# Note: `set -e` is intentionally NOT used — the bootstrap relies on `cmd ||
# fallback` chains, and the final checks report failure explicitly so any lint/
# test failure still blocks the commit.
set -uo pipefail

# Resolve the repo root from this script's own location (.claude/hooks/…),
# so the gate keeps working if the checkout lives somewhere else.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FRONTEND="$ROOT/smart_vent/frontend"

# ── docs-only classification ────────────────────────────────────────────────
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

if ! printf '%s\n' "$CHANGED" | grep -vE "$DOCS_RE" | grep -q .; then
  echo "Docs-only change — skipping lint + tests."
  exit 0
fi

echo "Code change detected — running lint + tests before commit…"

# ── dependency bootstrap ────────────────────────────────────────────────────
# First pip invocation that exists on this machine ("" if none).
pip_cmd() {
  if command -v pip  >/dev/null 2>&1; then echo "pip";            return; fi
  if command -v pip3 >/dev/null 2>&1; then echo "pip3";           return; fi
  if command -v python3 >/dev/null 2>&1; then echo "python3 -m pip"; return; fi
  echo ""
}

ensure_frontend_deps() {
  # Consider deps present only if node_modules AND the two tools the gate runs
  # (eslint, vitest) resolve — catches both a missing dir and a partial install.
  if [ -d "$FRONTEND/node_modules" ] \
     && [ -x "$FRONTEND/node_modules/.bin/eslint" ] \
     && [ -x "$FRONTEND/node_modules/.bin/vitest" ]; then
    return 0
  fi
  echo "→ frontend deps missing — installing…"
  # `npm ci` is reproducible (mirrors CI) but needs an in-sync lockfile; fall
  # back to `npm install` if the lockfile has drifted locally.
  npm --prefix "$FRONTEND" ci || npm --prefix "$FRONTEND" install
}

ensure_ruff() {
  if command -v ruff >/dev/null 2>&1; then
    return 0
  fi
  local PIP
  PIP="$(pip_cmd)"
  if [ -z "$PIP" ]; then
    echo "✗ neither pip nor python3 is available — cannot install ruff." >&2
    return 1
  fi
  echo "→ ruff missing — installing ruff>=0.15.20 (the pyproject floor CI uses)…"
  # Just ruff, not the full .[dev] extra: the gate only runs ruff, ruff installs
  # on any Python, and .[dev] needs 3.12. Fall back to a --user install when the
  # environment prevents a system/site install (non-root, PEP 668, etc.).
  $PIP install --quiet "ruff>=0.15.20" \
    || $PIP install --quiet --user "ruff>=0.15.20"
}

ensure_frontend_deps || { echo "✗ frontend dependency install failed." >&2; exit 1; }
ensure_ruff          || { echo "✗ ruff install failed." >&2; exit 1; }

# A --user (or script) install may land outside the current PATH — surface it.
if ! command -v ruff >/dev/null 2>&1; then
  USERBASE="$(python3 -c 'import site; print(site.getuserbase())' 2>/dev/null || echo "$HOME/.local")"
  for d in "$USERBASE/bin" "$HOME/.local/bin"; do
    [ -x "$d/ruff" ] && PATH="$d:$PATH"
  done
  export PATH
fi

# ── run the checks (any failure blocks the commit) ──────────────────────────
npm --prefix "$FRONTEND" run lint          || exit 1
npm --prefix "$FRONTEND" run test:coverage || exit 1
cd "$ROOT/smart_vent"
ruff check backend/                        || exit 1
ruff format --check backend/               || exit 1

echo "✓ lint + tests passed."
