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
RUN apk upgrade --no-cache \
    && apk add --no-cache --upgrade \
        "jq>=1.8.2-r0" \
        "libcrypto3>=3.5.8-r0" \
        "libssl3>=3.5.8-r0" \
        "libexpat>=2.8.4-r0"

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

# Copy run script
COPY run.sh /run.sh
RUN chmod +x /run.sh

EXPOSE 8099

CMD ["/run.sh"]
