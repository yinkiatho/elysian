#!/usr/bin/env bash
# ── Elysian entrypoint.sh ─────────────────────────────────────────────────────
# Runs inside the container before the main Python process starts.
# Responsibilities:
#   1. Wait for PostgreSQL to be ready
#   2. Create DB tables (idempotent — safe=True)
#   3. Exec the strategy runner (replaces this shell process)
#
# Usage (automatically invoked by Docker CMD):
#   /app/docker/entrypoint.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

echo "[entrypoint] Elysian Trading System starting..."
echo "[entrypoint] APP_ENV=${APP_ENV:-production}"
echo "[entrypoint] Python: $(python --version)"

# ── 0. Skip DB init for containers that don't need it (e.g. market-data) ─────
if [ "${SKIP_DB_INIT:-false}" = "true" ]; then
  echo "[entrypoint] SKIP_DB_INIT=true — skipping postgres wait and table creation"
  exec "$@"
fi

# ── 1. Wait for Postgres ──────────────────────────────────────────────────────
POSTGRES_HOST="${POSTGRES_HOST:-localhost}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"
POSTGRES_USER="${POSTGRES_USER:-elysian}"
POSTGRES_DATABASE="${POSTGRES_DATABASE:-elysian}"

MAX_RETRIES=30
RETRY_INTERVAL=2
attempt=0

echo "[entrypoint] Waiting for PostgreSQL at ${POSTGRES_HOST}:${POSTGRES_PORT}..."
until python -c "
import psycopg2, os, sys
try:
    psycopg2.connect(
        host=os.environ['POSTGRES_HOST'],
        port=int(os.environ.get('POSTGRES_PORT', 5432)),
        user=os.environ['POSTGRES_USER'],
        password=os.environ['POSTGRES_PASSWORD'],
        dbname=os.environ['POSTGRES_DATABASE'],
        connect_timeout=3,
    ).close()
    sys.exit(0)
except Exception as e:
    print(f'  not ready: {e}', file=sys.stderr)
    sys.exit(1)
" 2>&1; do
    attempt=$((attempt + 1))
    if [ $attempt -ge $MAX_RETRIES ]; then
        echo "[entrypoint] ERROR: PostgreSQL did not become ready after $MAX_RETRIES attempts. Exiting."
        exit 1
    fi
    echo "[entrypoint]   attempt $attempt/$MAX_RETRIES — retrying in ${RETRY_INTERVAL}s..."
    sleep $RETRY_INTERVAL
done

echo "[entrypoint] PostgreSQL is ready."

# ── 2. Create tables (idempotent) ─────────────────────────────────────────────
echo "[entrypoint] Running DB table creation..."
python -c "
from elysian_core.db.models import create_tables
create_tables(safe=True)
print('[entrypoint] DB tables OK.')
"

# ── 3. Hand off to the main process ───────────────────────────────────────────
echo "[entrypoint] Starting strategy runner..."
exec "$@"
