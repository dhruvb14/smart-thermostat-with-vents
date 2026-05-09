# Multi-arch HA base image — includes bashio, s6-overlay, python3, pip, jq, tzdata.
# Supports linux/amd64 and linux/arm64 via a single manifest.
FROM ghcr.io/home-assistant/base-python:latest

# Upgrade all base-image packages to pick up the latest security patches.
RUN apk upgrade --no-cache

# Install additional system dependencies not included in the HA base image.
# python3, pip, jq are already present — only add nodejs/npm/sqlite.
RUN apk add --no-cache \
    nodejs \
    npm \
    sqlite

WORKDIR /app

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
