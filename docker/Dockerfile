# ── Stage 1: Build frontend ────────────────────────────────────────────
FROM node:20-alpine AS frontend-builder

WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ .
COPY pyproject.toml /pyproject.toml
RUN npm run build

# ── Stage 2: Production image ──────────────────────────────────────────
FROM python:3.10-slim

# System deps: tmux for standalone bot mode, curl for health checks
RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        tmux \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY pyproject.toml ./
COPY python/ python/
RUN pip install --no-cache-dir -e .

# Copy built frontend assets for platform mode
COPY --from=frontend-builder /build/dist /app/frontend/dist

# Copy project metadata and tools
COPY README.md LICENSE ./
COPY tools/ /app/tools/

# Data directory for SQLite and encryption key
RUN mkdir -p /app/python/lockbot/data

# Default environment
ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    FRONTEND_DIST=/app/frontend/dist

# Default: platform mode (FastAPI + frontend)
EXPOSE 8000
CMD ["uvicorn", "lockbot.backend.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
