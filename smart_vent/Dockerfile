# Use the multi-arch Home Assistant Python 3.12 Alpine base image.
# Docker Buildx handles multi-arch automatically (amd64, arm64, etc.).
ARG BUILD_FROM=ghcr.io/home-assistant/base-python:3.12-alpine
FROM $BUILD_FROM

# Install system dependencies (python3/pip included for HA base image builds)
RUN apk add --no-cache \
    python3 \
    py3-pip \
    nodejs \
    npm \
    sqlite \
    jq

WORKDIR /app

# Install Python runtime dependencies directly — no package build step needed.
# The backend runs as `python -m backend.main` from /app, so installing the
# local package via pyproject.toml is unnecessary in the container.
RUN pip3 install --no-cache-dir --break-system-packages \
    "aiohttp>=3.9" \
    "aiosqlite>=0.20" \
    "apscheduler>=3.10" \
    "mcp>=1.0" \
    "python-dateutil>=2.9" \
    "uvloop>=0.19" \
    "certifi"

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
RUN VITE_APP_VERSION=$(grep '^version:' /app/config.yaml | sed "s/version: //;s/[\"']//g" | tr -d ' ') \
    && cd frontend && VITE_APP_VERSION=$VITE_APP_VERSION npm run build

# Copy run script
COPY run.sh /run.sh
RUN chmod +x /run.sh

EXPOSE 8099

CMD ["/run.sh"]
