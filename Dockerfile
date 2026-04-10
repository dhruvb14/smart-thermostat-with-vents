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

# Install Python dependencies
COPY pyproject.toml .
RUN pip install --no-cache-dir -e "."

# Build frontend
COPY frontend/ frontend/
RUN cd frontend && npm ci && npm run build

# Copy backend
COPY backend/ backend/

# Copy run script
COPY run.sh /run.sh
RUN chmod +x /run.sh

EXPOSE 8099

CMD ["/run.sh"]
