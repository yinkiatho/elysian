# Elysian — Docker Operational Guide

Everything you need to build, run, and operate the Elysian trading system with Docker.

---

## Table of Contents

1. [File Overview](#1-file-overview)
2. [Prerequisites](#2-prerequisites)
3. [One-Time Setup](#3-one-time-setup)
4. [Building the Image](#4-building-the-image)
5. [Running the Stack](#5-running-the-stack)
6. [Environment Variables Reference](#6-environment-variables-reference)
7. [Running Multiple Strategies](#7-running-multiple-strategies)
8. [Development Mode](#8-development-mode)
9. [Observability — Logs & Status](#9-observability--logs--status)
10. [Database Operations](#10-database-operations)
11. [Testing Inside Docker](#11-testing-inside-docker)
12. [Stopping & Cleanup](#12-stopping--cleanup)
13. [Production Checklist](#13-production-checklist)
14. [Architecture Notes](#14-architecture-notes)
15. [Troubleshooting](#15-troubleshooting)

---

## 1. File Overview

| File | Purpose |
|---|---|
| `Dockerfile` | Multi-stage build — produces a lean production image |
| `docker-compose.yml` | Full stack: `postgres`, `redis`, strategy containers |
| `docker-compose.override.yml` | Development overrides (auto-merged by `docker compose up`) |
| `.dockerignore` | Excludes secrets, caches, and dev tooling from the build context |
| `docker/entrypoint.sh` | Container startup script — waits for Postgres, creates tables, then execs the runner |
| `docker/postgres/init.sql` | One-time Postgres initialisation (extensions + settings) |
| `Makefile` | Shortcut commands for the full Docker workflow |

---

## 2. Prerequisites

| Requirement | Minimum version | Check |
|---|---|---|
| Docker Engine | 24.x | `docker --version` |
| Docker Compose plugin | 2.20 | `docker compose version` |
| Available RAM | 2 GB per strategy process | — |
| Available disk | 5 GB (image + Postgres volume) | — |

Install Docker: https://docs.docker.com/engine/install/

---

## 3. One-Time Setup

### 3a. Copy and fill in your `.env` file

```bash
cp .env.example .env
```

Open `.env` and fill in every value. At minimum you need:

```dotenv
# ── Database ──────────────────────────────────────────────────────────────────
POSTGRES_DATABASE=elysian
POSTGRES_USER=elysian
POSTGRES_PASSWORD=your_secure_password_here
POSTGRES_HOST=postgres           # matches the service name in docker-compose.yml
POSTGRES_PORT=5432

# ── Redis ─────────────────────────────────────────────────────────────────────
REDIS_HOST=redis
REDIS_PORT=6379

# ── Exchange API keys (one set per strategy_id) ───────────────────────────────
# Pattern: {EXCHANGE}_API_KEY_{strategy_id}
BINANCE_API_KEY_0=your_binance_key_for_strategy_0
BINANCE_API_SECRET_0=your_binance_secret_for_strategy_0

BINANCE_API_KEY_1=your_binance_key_for_strategy_1
BINANCE_API_SECRET_1=your_binance_secret_for_strategy_1

# Optional: Aster keys
ASTER_API_KEY_0=...
ASTER_API_SECRET_0=...

# Optional: wallet address for withdrawals
BINANCE_WALLET_ADDRESS=0x...
```

> **Security**: `.env` is listed in both `.gitignore` and `.dockerignore`. It is never baked into the image. Verify with `docker inspect <container>` — environment variables appear as keys but values are only visible to root on the host.

### 3b. Make the entrypoint executable

```bash
chmod +x docker/entrypoint.sh
```

---

## 4. Building the Image

```bash
# Standard build (production)
docker compose build

# Force a full rebuild (no layer cache)
docker compose build --no-cache

# Build for development (includes dev deps)
docker compose build --build-arg APP_ENV=development

# Makefile shortcut
make build
make build-no-cache
```

The image is named `elysian_elysian_strategy_0` (compose project + service name) by default. You can override with:

```bash
docker build -t elysian:1.0.0 .
```

---

## 5. Running the Stack

### Start infrastructure only (Postgres + Redis)

```bash
docker compose up -d postgres redis
# or
make up
```

### Start strategy 0

```bash
docker compose --profile strategy_0 up -d
# or
make strategy-0
```

### Start strategy 1

```bash
docker compose --profile strategy_1 up -d
# or
make strategy-1
```

### Start all services at once

```bash
docker compose --profile all up -d
# or
make all
```

### Run in the foreground (see logs directly)

```bash
docker compose --profile all up
```

The first time you start the stack, the `entrypoint.sh` script will:
1. Wait for Postgres to accept connections (up to 60 seconds, retrying every 2 seconds)
2. Create all database tables (`CREATE TABLE IF NOT EXISTS`)
3. Hand off to `python elysian_core/run_strategy.py`

---

## 6. Environment Variables Reference

All variables are read from `.env`. The table below lists every variable consumed by the application.

| Variable | Required | Default | Description |
|---|---|---|---|
| `POSTGRES_DATABASE` | Yes | — | Database name |
| `POSTGRES_USER` | Yes | — | Database user |
| `POSTGRES_PASSWORD` | Yes | — | Database password |
| `POSTGRES_HOST` | Yes | `localhost` | Set to `postgres` in Docker |
| `POSTGRES_PORT` | No | `5432` | |
| `REDIS_HOST` | No | `localhost` | Set to `redis` in Docker |
| `REDIS_PORT` | No | `6379` | |
| `BINANCE_API_KEY_{N}` | Yes | — | Binance API key for strategy N |
| `BINANCE_API_SECRET_{N}` | Yes | — | Binance API secret for strategy N |
| `ASTER_API_KEY_{N}` | No | — | Aster API key for strategy N |
| `ASTER_API_SECRET_{N}` | No | — | Aster API secret for strategy N |
| `BINANCE_WALLET_ADDRESS` | No | — | Binance wallet for withdrawals |
| `NUMEXPR_MAX_THREADS` | No | `4` | Thread count for numexpr |
| `DISCORD_TOKEN` | No | — | Discord bot token (if logging enabled) |
| `DISCORD_GUILD_ID` | No | — | Discord server ID |

### Overriding per-strategy at compose time

You can override any variable for a specific service in `docker-compose.yml` under the service's `environment:` block without affecting other services.

---

## 7. Running Multiple Strategies

Each strategy runs as a separate Docker service with its own API keys and private event bus. This matches Elysian's sub-account architecture exactly — one container per strategy.

### Adding a new strategy

**Step 1** — Create a strategy YAML in `elysian_core/config/strategies/`:

```yaml
# strategy_002_my_new_strategy.yaml
strategy_id: 2
strategy_name: "my_new_strategy"
class: "elysian_core.strategy.my_strategy.MyStrategy"
asset_type: "Spot"
venue: "Binance"
venues: ["Binance"]
symbols: ["BTCUSDT", "ETHUSDT"]
risk: {}
params:
  rebalance_interval_s: 60
```

**Step 2** — Add API keys to `.env`:

```dotenv
BINANCE_API_KEY_2=your_key_for_strategy_2
BINANCE_API_SECRET_2=your_secret_for_strategy_2
```

**Step 3** — Add a service block to `docker-compose.yml`:

```yaml
  elysian_strategy_2:
    <<: *strategy-defaults
    container_name: elysian_strategy_2
    environment:
      POSTGRES_HOST: postgres
      POSTGRES_PORT: "5432"
      REDIS_HOST: redis
      REDIS_PORT: "6379"
      BINANCE_API_KEY_2: ${BINANCE_API_KEY_2}
      BINANCE_API_SECRET_2: ${BINANCE_API_SECRET_2}
    command:
      - python
      - elysian_core/run_strategy.py
    profiles:
      - strategy_2
      - all
```

**Step 4** — Register the YAML in `run_strategy.py`:

```python
strategy_config_yamls = [
    'elysian_core/config/strategies/strategy_000_event_driven.yaml',
    'elysian_core/config/strategies/strategy_001_test_print.yaml',
    'elysian_core/config/strategies/strategy_002_my_new_strategy.yaml',  # add this
]
```

**Step 5** — Start the new strategy:

```bash
docker compose --profile strategy_2 up -d
```

---

## 8. Development Mode

Development mode mounts your source directory into the container so code changes are reflected without rebuilding the image.

```bash
# Start with live source mount
docker compose --profile dev up --build
# or
make dev
```

The `elysian_dev` service in `docker-compose.yml` uses `APP_ENV=development` and mounts `.:/app` so every file edit is visible inside the container immediately.

> **Note**: The Python process must be restarted to pick up changes. Use `docker compose restart elysian_dev` or add `watchdog` / `watchfiles` to `requirements.txt` and update the command to use a file-watcher.

---

## 9. Observability — Logs & Status

### View live logs

```bash
# All services
docker compose logs -f

# One service only
docker compose logs -f elysian_strategy_0

# Last 500 lines then follow
docker compose logs --tail=500 -f elysian_strategy_0

# Makefile shortcuts
make logs
make logs-strategy-0
```

Logs are also written to the `elysian_logs` Docker volume at `/app/logs/` and `/app/elysian_core/utils/logs/`. One file is created per logger name and per run timestamp.

### Container status

```bash
docker compose ps
# or
make ps
```

### Inspect a running container

```bash
# Open a bash shell
docker compose exec elysian_strategy_0 bash
# or
make shell

# Run a one-off Python command
docker compose exec elysian_strategy_0 python -c "
from elysian_core.db.models import PortfolioSnapshot
print(PortfolioSnapshot.select().count(), 'snapshots in DB')
"
```

---

## 10. Database Operations

### Connect to Postgres

```bash
docker compose exec postgres psql -U elysian -d elysian
# or
make db-shell
```

### Create tables manually (already done by entrypoint)

```bash
make db-init
```

### Clear all rows (reset trade history)

```bash
make db-clear
# Equivalent to:
docker compose exec postgres psql -U elysian -d elysian \
  -c "TRUNCATE TABLE cex_trades, dex_trades, portfolio_snapshots, account_snapshots RESTART IDENTITY CASCADE;"
```

### Backup Postgres data

```bash
docker compose exec postgres pg_dump -U elysian elysian > backup_$(date +%Y%m%d_%H%M%S).sql
```

### Restore from backup

```bash
cat backup_20240101_120000.sql | docker compose exec -T postgres psql -U elysian -d elysian
```

---

## 11. Testing Inside Docker

Run the full pytest suite inside a container (uses the same Python environment as production):

```bash
docker compose run --rm --no-deps elysian_strategy_0 \
  python -m pytest tests/ -v --tb=short
# or
make test
```

Run a specific test file:

```bash
docker compose run --rm --no-deps elysian_strategy_0 \
  python -m pytest tests/test_order_fsm.py -v
```

Run linter:

```bash
docker compose run --rm --no-deps elysian_strategy_0 \
  python -m ruff check elysian_core/ tests/
# or
make lint
```

---

## 12. Stopping & Cleanup

### Stop all containers (keeps volumes)

```bash
docker compose down
# or
make down
```

### Stop and remove all volumes (destructive — deletes all data)

```bash
docker compose down -v --remove-orphans
# or
make clean
```

### Remove unused Docker resources system-wide

```bash
docker system prune -f
docker volume prune -f
# or
make prune
```

---

## 13. Production Checklist

Before going live, verify the following:

- [ ] `.env` has real API keys (not placeholders)
- [ ] `POSTGRES_PASSWORD` is a strong random password
- [ ] `.env` is **not** committed to git (`git status` shows it as untracked)
- [ ] `docker inspect <container>` does not show secrets in image layers
- [ ] The server running Docker has `ulimits nofile` set to at least 65536
- [ ] Postgres `postgres_data` volume is on a persistent disk (not ephemeral storage)
- [ ] Log volume is mounted to a path with sufficient disk space
- [ ] Docker daemon is configured with `--log-opt max-size=50m --log-opt max-file=5` (or via `daemon.json`)
- [ ] Container restart policy is `unless-stopped` (already set in `docker-compose.yml`)
- [ ] Binance API keys have IP allowlisting enabled (set to your server IP)
- [ ] `docker compose --profile all ps` shows all services as `Up (healthy)`
- [ ] Snapshot interval is configured (`portfolio.snapshot_interval_s` in `trading_config.yaml`)

### Recommended `daemon.json` settings (at `/etc/docker/daemon.json`)

```json
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "50m",
    "max-file": "5"
  },
  "default-ulimits": {
    "nofile": {
      "Name": "nofile",
      "Hard": 65536,
      "Soft": 65536
    }
  }
}
```

Restart Docker after editing: `sudo systemctl restart docker`

---

## 14. Architecture Notes

### Why one container per strategy?

Elysian's sub-account model assigns each strategy a dedicated exchange sub-account with its own API keys. Mapping this to Docker services provides:

- **Fault isolation**: a crash in strategy 1 does not kill strategy 0
- **Independent restarts**: `docker compose restart elysian_strategy_1`
- **Per-strategy resource limits**: add `mem_limit` and `cpus` to the service block
- **Independent scaling**: deploy strategies across multiple hosts if needed

### Network model

All services share the `elysian_net` bridge network. Services address each other by service name (e.g. `POSTGRES_HOST=postgres`). No ports are exposed to the host in production except the external port overrides in `docker-compose.override.yml`.

### Volume layout

| Volume | Contents |
|---|---|
| `postgres_data` | All Postgres data files |
| `redis_data` | Redis persistence (RDB snapshots) |
| `elysian_logs` | Application log files (per logger, per run timestamp) |

### Single event loop constraint

Elysian runs on a single asyncio event loop. Docker's process model is a perfect match: one Python process per container, no threading across containers. Do not set `--workers` or run multiple Python processes inside a single container.

---

## 15. Troubleshooting

### Container exits immediately

```bash
docker compose logs elysian_strategy_0
```

Common causes:
- Missing `.env` variable — look for `KeyError` or `None` in the logs
- Postgres not ready — the entrypoint waits 60 seconds; if Postgres is still unhealthy, check `docker compose logs postgres`
- Import error — run `docker compose run --rm elysian_strategy_0 python -c "import elysian_core"` to test imports

### `psycopg2.OperationalError: could not connect to server`

Verify `POSTGRES_HOST=postgres` (not `localhost`) in `.env` — inside Docker, the service is reachable by its service name, not `127.0.0.1`.

### WebSocket disconnects / reconnects

Elysian's connectors implement exponential backoff reconnection. Brief disconnects are normal and will be logged as warnings. Persistent disconnects suggest a network issue or rate limit. Check `docker compose logs -f elysian_strategy_0`.

### `OSError: [Errno 24] Too many open files`

The container's file descriptor limit is too low. Ensure `ulimits` in `docker-compose.yml` is applied:

```bash
docker compose exec elysian_strategy_0 bash -c "ulimit -n"
# Should print 65536
```

If not, check that your Docker daemon respects the `--ulimit` flags and restart the container.

### Out of memory

Add resource limits to the service in `docker-compose.yml`:

```yaml
  elysian_strategy_0:
    <<: *strategy-defaults
    deploy:
      resources:
        limits:
          memory: 2g
          cpus: "2.0"
```

### Checking which API keys are loaded

```bash
docker compose exec elysian_strategy_0 python -c "
import os
for k, v in os.environ.items():
    if 'API_KEY' in k:
        print(k, '=', v[:8] + '...' if v else '(empty)')
"
```
