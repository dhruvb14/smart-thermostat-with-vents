#!/usr/bin/env bash
# Generate the rolling Plenum Beta changelog to stdout: a single
# "building toward vX.Y.0" section listing every pull request that has landed
# (or is landing) on the beta channel since the last stable minor release.
# Because every change reaches beta before stable, this is simply "all PRs
# since v#.#.0".
#
# When run from a PR, pass the current (not-yet-merged) PR via env so its title
# is included alongside the already-merged ones:
#   PR_NUMBER, PR_TITLE
#
# Usage: gen-beta-changelog.sh <beta-version>          # e.g. 0.31.0-beta.7
# Env:   REPO=<owner/name> (defaults to the origin remote); GH_TOKEN for gh;
#        PR_NUMBER / PR_TITLE (optional, the current PR).
set -euo pipefail

BETA_VERSION="${1:?usage: gen-beta-changelog.sh <beta-version>}"
TARGET="${BETA_VERSION%%-beta.*}"          # 0.31.0-beta.7 -> 0.31.0

REPO="${REPO:-$(git config --get remote.origin.url 2>/dev/null \
  | sed -E 's#(git@|https://)github.com[:/]##; s#\.git$##' || echo "")}"

LAST_TAG=$(git describe --tags --abbrev=0 --match 'v[0-9]*.[0-9]*.0' 2>/dev/null || echo "")

# Prefer the merged tip (origin/main) so we list only PRs that have actually
# landed on the beta channel; fall back to HEAD when origin/main isn't fetched.
MERGED_REF="origin/main"
git rev-parse --verify --quiet "$MERGED_REF" >/dev/null 2>&1 || MERGED_REF="HEAD"

if [ -n "$LAST_TAG" ]; then
  RANGE="${LAST_TAG}..${MERGED_REF}"
  SINCE="$LAST_TAG"
else
  RANGE="$MERGED_REF"
  SINCE="the first release"
fi

# Merged PR numbers referenced by commit subjects in range, plus the current PR.
PR_NUMS=$(git log "$RANGE" --pretty=format:'%s' \
  | grep -oE '#[0-9]+' | grep -oE '[0-9]+' | sort -un || true)
if [ -n "${PR_NUMBER:-}" ]; then
  PR_NUMS=$(printf '%s\n%s\n' "$PR_NUMS" "$PR_NUMBER" | grep -E '^[0-9]+$' | sort -un)
fi

BULLETS=""
for NUM in $PR_NUMS; do
  # For the current PR use the title straight from the event (it's still open,
  # so `gh pr view` would work too, but this avoids an API call and works even
  # before the PR is indexed).
  if [ -n "${PR_NUMBER:-}" ] && [ "$NUM" = "${PR_NUMBER}" ] && [ -n "${PR_TITLE:-}" ]; then
    TITLE="$PR_TITLE"
    URL="https://github.com/${REPO}/pull/${NUM}"
  else
    PR_JSON=$(gh pr view "$NUM" --repo "$REPO" --json number,title,url 2>/dev/null || echo "")
    if [ -z "$PR_JSON" ]; then
      continue        # a bare issue reference, not a PR — skip
    fi
    TITLE=$(printf '%s' "$PR_JSON" | jq -r '.title')
    URL=$(printf '%s' "$PR_JSON" | jq -r '.url')
  fi
  if printf '%s' "$TITLE" | grep -qE '^Release v[0-9]'; then
    continue          # release-automation PR — housekeeping, not user-facing
  fi
  BULLETS="${BULLETS}- ${TITLE} ([#${NUM}](${URL}))\n"
done
if [ -z "$BULLETS" ]; then
  BULLETS="- (nothing on the beta channel yet since ${SINCE})\n"
fi

printf '# Plenum Beta — Changelog\n\n'
printf '## %s — building toward v%s\n\n' "$BETA_VERSION" "$TARGET"
printf '> ⚠️ **Beta channel.** Tracks the tip of `main` and may be unstable. For a\n'
printf '> production install, use the **Plenum** (stable) add-on. Everything below is\n'
printf '> heading for the next stable release (v%s).\n\n' "$TARGET"
printf '**Landed on beta since %s:**\n\n' "$SINCE"
printf '%b' "$BULLETS"
