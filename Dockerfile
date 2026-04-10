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

# Upgrade pip + setuptools before anything else, then install Python deps.
# Backend source must be present for setuptools to build the package, so both
# are copied together. This layer is cached unless pyproject.toml or backend/
# source changes.
COPY pyproject.toml .
COPY backend/ backend/
RUN pip install --upgrade pip setuptools && \
    pip install --no-cache-dir .

# Build frontend (separate layer — only rebuilds when frontend source changes)
COPY frontend/package.json frontend/package-lock.json frontend/
RUN cd frontend && npm ci

COPY frontend/ frontend/
RUN cd frontend && npm run build

# Copy run script
COPY run.sh /run.sh
RUN chmod +x /run.sh

EXPOSE 8099

CMD ["/run.sh"]
