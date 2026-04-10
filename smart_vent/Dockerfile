# When built via the HA add-on pipeline, BUILD_FROM is injected by the build
# system with the correct arch-specific HA base image. For standalone builds
# (GitHub Actions, local docker build) it falls back to a plain Alpine image.
ARG BUILD_FROM=python:3.12-alpine
FROM $BUILD_FROM

# Install system dependencies
RUN apk add --no-cache \
    nodejs \
    npm \
    sqlite \
    jq

WORKDIR /app

# Install Python runtime dependencies directly — no package build step needed.
# The backend runs as `python -m backend.main` from /app, so installing the
# local package via pyproject.toml is unnecessary in the container.
RUN pip install --no-cache-dir \
    "aiohttp>=3.9" \
    "aiosqlite>=0.20" \
    "apscheduler>=3.10" \
    "mcp>=1.0" \
    "python-dateutil>=2.9" \
    "uvloop>=0.19" \
    "certifi"

# Copy backend source
COPY backend/ backend/

# Install frontend deps then build (separate COPY for better layer caching)
COPY frontend/package.json frontend/package-lock.json frontend/
RUN cd frontend && npm ci

COPY frontend/ frontend/
RUN cd frontend && npm run build

# Copy run script
COPY run.sh /run.sh
RUN chmod +x /run.sh

EXPOSE 8099

CMD ["/run.sh"]
