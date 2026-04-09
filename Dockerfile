ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.12
FROM $BUILD_FROM

# Install system dependencies
RUN apk add --no-cache \
    nodejs \
    npm \
    sqlite

WORKDIR /app

# Install Python dependencies
COPY pyproject.toml .
RUN pip install --no-cache-dir -e ".[dev]"

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
