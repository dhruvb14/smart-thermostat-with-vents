#!/usr/bin/env bash
# Generate the rolling Plenum Beta changelog to stdout: a single
# "building toward vX.Y.Z" section listing the pull requests merged to `main`
# since the last stable v#.#.# tag. Mirrors the PR-title harvesting in
# .github/workflows/release-pr.yml so the beta and stable changelogs read alike.
#
# Usage: gen-beta-changelog.sh <beta-version>          # e.g. 0.30.1-beta.7
# Env:   REPO=<owner/name> (defaults to the origin remote); GH_TOKEN for gh.
set -euo pipefail

BETA_VERSION="${1:?usage: gen-beta-changelog.sh <beta-version>}"
TARGET="${BETA_VERSION%%-beta.*}"          # 0.30.1-beta.7 -> 0.30.1

if [ -z "${REPO:-}" ]; then
  REPO=$(git config --get remote.origin.url 2>/dev/null \
    | sed -E 's#(git@|https://)github.com[:/]##; s#\.git$##' || echo "")
fi

LAST_TAG=$(git describe --tags --abbrev=0 --match 'v[0-9]*.[0-9]*.[0-9]*' 2>/dev/null || echo "")
if [ -n "$LAST_TAG" ]; then
  RANGE="${LAST_TAG}..HEAD"
  SINCE="$LAST_TAG"
else
  RANGE="HEAD"
  SINCE="the first commit"
fi
SHORT_SHA=$(git rev-parse --short HEAD)

# PR numbers referenced by commit subjects in range (deduped, numeric).
PR_NUMS=$(git log "$RANGE" --pretty=format:'%s' \
  | grep -oE '#[0-9]+' | grep -oE '[0-9]+' | sort -un || true)

BULLETS=""
for NUM in $PR_NUMS; do
  PR_JSON=$(gh pr view "$NUM" --repo "$REPO" --json number,title,url 2>/dev/null || echo "")
  if [ -z "$PR_JSON" ]; then
    continue
  fi
  TITLE=$(printf '%s' "$PR_JSON" | jq -r '.title')
  if printf '%s' "$TITLE" | grep -qE '^Release v[0-9]'; then
    continue
  fi
  URL=$(printf '%s' "$PR_JSON" | jq -r '.url')
  BULLETS="${BULLETS}- ${TITLE} ([#${NUM}](${URL}))\n"
done
if [ -z "$BULLETS" ]; then
  BULLETS="- No merged pull requests since ${SINCE} yet.\n"
fi

printf '# Plenum Beta — Changelog\n\n'
printf '## %s — building toward v%s\n\n' "$BETA_VERSION" "$TARGET"
printf '> ⚠️ **Beta channel.** Built from the tip of `main` (commit `%s`). May be\n' "$SHORT_SHA"
printf '> unstable. For a production install, use the **Plenum** (stable) add-on.\n'
printf '> This channel is currently exercising the auth refactor (#373).\n\n'
printf '**Changes since %s:**\n\n' "$SINCE"
printf '%b' "$BULLETS"
