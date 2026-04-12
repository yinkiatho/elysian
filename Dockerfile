# ── Elysian Trading System — Dockerfile ──────────────────────────────────────
# Multi-stage build: keeps the final image lean by separating build-time deps
# from runtime deps.
#
# Build args:
#   PYTHON_VERSION   Python release to use (default: 3.12)
#   APP_ENV          "production" | "development" (default: production)
#
# Usage:
#   docker build -t elysian:latest .
#   docker build --build-arg APP_ENV=development -t elysian:dev .
# ─────────────────────────────────────────────────────────────────────────────

ARG PYTHON_VERSION=3.12.10
ARG APP_ENV=production

# ─── Stage 1: dependency builder ─────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim AS builder

WORKDIR /build

# System packages needed to compile certain Python deps (psycopg2, numexpr…)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    g++ \
    libpq-dev \
    libffi-dev \
    libssl-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Upgrade pip and install wheel/setuptools before project deps
RUN pip install --upgrade pip setuptools wheel

# Copy only the requirements file first so Docker can cache this layer
COPY requirements.txt .

# Install all dependencies into a dedicated prefix so we can copy them cleanly
RUN pip install --prefix=/install --no-cache-dir -r requirements.txt


# ─── Stage 2: runtime image ──────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim AS runtime

ARG APP_ENV
ENV APP_ENV=${APP_ENV} \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    # numexpr thread count — override via ENV in docker-compose
    NUMEXPR_MAX_THREADS=8

# Runtime system packages only (libpq for psycopg2)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    libffi8 \
    libssl3 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create a non-root user for security
RUN groupadd -r elysian && useradd -r -g elysian -m -d /home/elysian elysian

# Copy compiled dependencies from builder stage
COPY --from=builder /install /usr/local

WORKDIR /app

# Copy source code
COPY --chown=elysian:elysian . .

# Create required runtime directories and make entrypoint executable
RUN mkdir -p \
    /app/elysian_core/utils/logs \
    /app/logs \
    && chmod +x /app/docker/entrypoint.sh \
    && chown -R elysian:elysian /app

# Switch to non-root user
USER elysian

# Healthcheck — verifies core imports are healthy
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import elysian_core.core.enums; print('ok')" || exit 1

# Entrypoint: waits for Postgres, creates tables, then execs CMD
ENTRYPOINT ["/app/docker/entrypoint.sh"]

# Default command (overridable in docker-compose.yml)
CMD ["python", "elysian_core/run_strategy.py"]
