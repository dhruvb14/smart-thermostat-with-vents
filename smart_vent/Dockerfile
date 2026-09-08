# Multi-arch HA base image — includes bashio, s6-overlay, python3, pip, jq, tzdata.
# Supports linux/amd64 and linux/arm64 via a single manifest.
FROM ghcr.io/home-assistant/base-python:latest

# ── Base-image security refresh ───────────────────────────────────────────────
# A bare `RUN apk upgrade --no-cache` is a no-op on every build after the first.
# Its command string never changes, so BuildKit's GHA cache re-serves the layer
# verbatim and the image ships whatever apk state was current when that layer was
# FIRST built. The v0.36.0 release build logged exactly that — `#14 [linux/amd64
# 2/17] RUN apk upgrade --no-cache` → `CACHED` — which is how the release-image
# scan on PR #595 found jq 1.8.1-r0, libcrypto3/libssl3 3.5.7-r0 and libexpat
# 2.8.3-r0 in an image whose Dockerfile claims to upgrade everything. Only
# release PRs scanned the image back then, so no earlier PR could have caught
# it; #596 closed that gap — every code PR now scans the image it builds.
#
# The explicit `>=` floors below are load-bearing twice over:
#   1. they are part of the RUN command string, so editing one invalidates this
#      layer and forces apk to re-resolve against a freshly fetched index;
#   2. apk FAILS the build when a floor cannot be satisfied, so a missing patch
#      is loud instead of silently shipping.
# When an image scan flags a new OS package, add it here with the "Fixed
# Version" Trivy printed. Bumping a floor rebuilds every layer below this one —
# that is the intended cost of a security refresh, not a regression.
#
# curl/libcurl are floored even though their findings arrive UNKNOWN-severity
# (Alpine secdb rates them; NVD had not yet scored them), and even though a
# COLD build's `apk upgrade` already pulls 8.22.0-r0 on its own. That "already
# works" is the trap: it only holds while the layer is actually rebuilt, and
# per the paragraph above a warm GHA cache re-serves this RUN verbatim. Without
# a floor naming them, curl silently stays at whatever the layer first baked —
# the base image ships 8.21.0-r0, carrying 20 findings — and unpinned rot is
# worse than it looks under a severity gate: UNKNOWN is advisory today, but
# these get rescored, and the first one that lands as a fixable MEDIUM turns a
# cached layer nobody touched into a red required check.
RUN apk upgrade --no-cache \
    && apk add --no-cache --upgrade \
        "jq>=1.8.2-r0" \
        "libcrypto3>=3.5.8-r0" \
        "libssl3>=3.5.8-r0" \
        "libexpat>=2.8.4-r0" \
        "curl>=8.22.0-r0" \
        "libcurl>=8.22.0-r0"

# /usr/bin/tempio is a Go binary the HA base image ships for add-ons that render
# their config from a template. Plenum does not use it: run.sh reads options
# through bashio, and nothing in the base image's own init scripts references it
# either. It belongs to no apk package, so the upgrade above can never patch it,
# and the copy in the current base (go1.26.5, golang.org/x/crypto v0.31.0) held
# the ONLY CRITICAL in #595's scan (CVE-2026-56854 — x/crypto/ssh source-address
# bypass) plus 26 more stdlib and x/crypto findings. Deleting the unused binary
# fixes all 27 at the source instead of suppressing them in .trivyignore.
RUN rm -f /usr/bin/tempio

# Install additional system dependencies not included in the HA base image.
# python3, pip, jq are already present — only add nodejs/npm/sqlite.
#
# No `>=` floors here, unlike the base-image refresh block above, and that is a
# considered choice rather than an oversight: floors there exist because those
# packages had findings this repo had to clear. These three do not. Verified by
# pulling the built image and diffing all 67 installed apk packages against
# Alpine 3.24's security database — every one is at or above its latest secfix,
# nodejs and npm included. Add a floor here if and when one of them is the
# package a scan actually names; do not add one speculatively, because a floor
# that tracks nothing still has to be maintained.
#
# npm is deleted again further down — it is build-time only, and its bundled
# node_modules is what the image scan reads. See the strip block near the end.
RUN apk add --no-cache \
    nodejs \
    npm \
    sqlite

WORKDIR /app

# Upgrade pip to clear CVE-2026-6357 (fixed in pip 26.1). The `>=` floor is the
# cache key, for the same reason as the apk block above: a bare `--upgrade pip`
# is served from a stale cached layer and can silently ship a pip older than the
# fix. Raise the floor to re-resolve.
RUN pip3 install --no-cache-dir --upgrade "pip>=26.2.1"

# Install Python runtime dependencies from pyproject.toml.
COPY pyproject.toml .
# We need the backend directory structure for the install to succeed,
# but we'll COPY the actual code later for better caching.
RUN mkdir -p backend && touch backend/__init__.py
RUN pip3 install --no-cache-dir .

# Copy backend source
COPY backend/ backend/

# Copy config.yaml so the version can be extracted and baked into the frontend bundle
COPY config.yaml config.yaml

# Install frontend deps then build (separate COPY for better layer caching)
COPY frontend/package.json frontend/package-lock.json frontend/
RUN cd frontend && npm ci

COPY frontend/ frontend/
# Beta builds swap in the red-ribbon "BETA" apple-touch-icon before the Vite
# build so the PWA/home-screen icon differs from stable's — both variants
# live in frontend/public/ and this is the only place that picks between them
# (see .github/workflows/beta.yml's "Build & push beta image" step).
ARG APPLE_TOUCH_ICON_VARIANT=stable
RUN if [ "$APPLE_TOUCH_ICON_VARIANT" = "beta" ]; then \
        mv frontend/public/apple-touch-icon-beta.png frontend/public/apple-touch-icon.png; \
    else \
        rm -f frontend/public/apple-touch-icon-beta.png; \
    fi
# Extract the version from config.yaml and pass it as a Vite build-time constant.
# Vite replaces import.meta.env.VITE_APP_VERSION with the literal string in the bundle.
# Remove node_modules after building — they are build-time only and esbuild's bundled
# Go binary carries CVEs that have no runtime impact.
RUN VITE_APP_VERSION=$(grep '^version:' /app/config.yaml | sed "s/version: //;s/[\"']//g" | tr -d ' ') \
    && cd frontend && VITE_APP_VERSION=$VITE_APP_VERSION npm run build \
    && rm -rf /app/frontend/node_modules

# ── Strip build-time-only Python tooling ──────────────────────────────────────
# pip is a BUILD-time tool here: every `pip3 install` above has already run, and
# nothing in the shipped add-on invokes it. run.sh reads options through bashio
# (which needs only curl and jq), the s6 init tree never calls pip, and no
# backend module imports pip or pkg_resources.
#
# Keeping it was costing the image every finding it had. Trivy reads pip's own
# vendored manifest, /usr/local/lib/python3.*/site-packages/pip/_vendor/vendor.txt,
# and reports the pinned versions listed there as if they were installed
# packages. On pip 26.2.1 — the newest release, so there is no "bump pip" fix —
# that manifest pins msgpack==1.1.2 (GHSA-6v7p-g79w-8964, HIGH) and
# setuptools==70.3.0 (CVE-2025-47273 HIGH, CVE-2026-59890 MEDIUM). Those three
# were the ONLY vulnerabilities left in the image; deleting pip takes it to
# zero. The setuptools pair is doubly misleading: pip vendors pkg_resources, not
# setuptools itself, and the real setuptools in the image is 84.0.0 — the
# version pyproject.toml's `setuptools>=82.0.1` floor installs, which survives
# this step untouched.
#
# Same reasoning as the tempio deletion above, and the same precedence the
# .trivyignore header sets out: fix at the source rather than suppress. This is
# what makes the MEDIUM-and-above gate in .github/actions/scan-image sustainable
# — with pip present, every image scan would block on findings no dependency
# bump in this repo could reach.
#
# Escape hatch if a future build step ever needs pip back at runtime:
# `python3 -m ensurepip` reinstalls it (ensurepip stays in the stdlib).
RUN pip3 uninstall --yes pip

# ── Strip build-time-only Node tooling ────────────────────────────────────────
# npm is the exact same story as pip above, one ecosystem over, and it is the
# reason `Image vulnerability scan` was failing on PR #618 with one fixable HIGH.
#
# npm is BUILD-time only: `npm ci` and `npm run build` have both already run by
# this point, the build's own `rm -rf node_modules` cleaned up after them, and
# nothing in the shipped add-on invokes npm or node. run.sh does not reference
# either, no backend module shells out to them, and the frontend is served as a
# static bundle from frontend/dist by aiohttp (backend/main.py's add_static).
#
# What it costs to keep: apk installs npm as a real Node application, so
# /usr/lib/node_modules/npm/node_modules ships ~143 bundled packages, each with
# its own package.json. Trivy's node-pkg scanner reads those exactly as it reads
# pip's vendored vendor.txt — as installed packages — and nine of them carry
# advisories (@sigstore/core, @sigstore/verify, brace-expansion, ip-address,
# pacote, picomatch, postcss-selector-parser, sigstore, tar). None is reachable
# from this add-on, none is declared anywhere in this repo, and none can be
# fixed by a dependency bump here: they are pinned by whichever npm version
# Alpine ships, and npm 11.12.1-r0 is already current in v3.24/community.
#
# That last point is what makes deletion the only real fix, and the same
# argument the pip block makes: the finding is invisible to `trivy fs` (the
# source tree has no such manifest — frontend/package-lock.json is clean, all
# 301 entries), unreachable by any bump, and would otherwise leave
# .trivyignore as the only lever — the habit that file's header exists to
# discourage.
#
# nodejs itself is deliberately KEPT. It is not what the scan flagged (the apk
# package is current and finding-free), removing it is a larger change than this
# failure calls for, and it leaves an escape hatch: `apk add --no-cache npm`
# restores npm if a future build step ever needs it at runtime.
RUN apk del npm

# Copy run script
COPY run.sh /run.sh
RUN chmod +x /run.sh

EXPOSE 8099

CMD ["/run.sh"]
