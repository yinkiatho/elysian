# ── Elysian Trading System — Makefile ────────────────────────────────────────
# Convenience targets for the Docker workflow.
# Run: make <target>
# ─────────────────────────────────────────────────────────────────────────────

.PHONY: help build up down logs ps shell db-shell clean prune test lint \
        strategy-0 strategy-1 dev prod

# Default target
help:
	@echo ""
	@echo "  Elysian Docker Workflow"
	@echo "  ─────────────────────────────────────────────────────────────────"
	@echo "  make build          Build all images"
	@echo "  make up             Start infrastructure (postgres + redis)"
	@echo "  make strategy-0     Start strategy 0 (EventDrivenStrategy)"
	@echo "  make strategy-1     Start strategy 1 (PrintEventsStrategy)"
	@echo "  make all            Start all services"
	@echo "  make down           Stop all containers"
	@echo "  make logs           Tail logs from all running containers"
	@echo "  make ps             Show container status"
	@echo "  make shell          Open a shell in the elysian_strategy_0 container"
	@echo "  make db-shell       Open psql in the postgres container"
	@echo "  make clean          Stop and remove all containers + volumes"
	@echo "  make prune          Remove all unused Docker resources"
	@echo "  make test           Run pytest inside a container"
	@echo "  make lint           Run ruff linter inside a container"
	@echo "  make dev            Start in development mode (hot-reload)"
	@echo ""

# ── Build ─────────────────────────────────────────────────────────────────────
build:
	docker compose build

build-no-cache:
	docker compose build --no-cache

# ── Lifecycle ─────────────────────────────────────────────────────────────────
up:
	docker compose up -d postgres redis

strategy-0:
	docker compose --profile strategy_0 up -d

strategy-1:
	docker compose --profile strategy_1 up -d

all:
	docker compose --profile all up -d

down:
	docker compose down

restart:
	docker compose restart

# ── Development ───────────────────────────────────────────────────────────────
dev:
	docker compose --profile dev up --build

prod:
	docker compose -f docker-compose.yml --profile all up -d

# ── Observability ─────────────────────────────────────────────────────────────
logs:
	docker compose logs -f --tail=200

logs-strategy-0:
	docker compose logs -f --tail=200 elysian_strategy_0

logs-strategy-1:
	docker compose logs -f --tail=200 elysian_strategy_1

ps:
	docker compose ps

# ── Debugging ─────────────────────────────────────────────────────────────────
shell:
	docker compose exec elysian_strategy_0 bash

db-shell:
	docker compose exec postgres psql -U $${POSTGRES_USER:-elysian} -d $${POSTGRES_DATABASE:-elysian}

redis-shell:
	docker compose exec redis redis-cli

# ── Testing & linting ─────────────────────────────────────────────────────────
test:
	docker compose run --rm --no-deps elysian_strategy_0 \
		python -m pytest tests/ -v --tb=short

lint:
	docker compose run --rm --no-deps elysian_strategy_0 \
		python -m ruff check elysian_core/ tests/

# ── Cleanup ───────────────────────────────────────────────────────────────────
clean:
	docker compose down -v --remove-orphans

prune:
	docker system prune -f
	docker volume prune -f

# ── Database utilities ────────────────────────────────────────────────────────
db-init:
	docker compose run --rm --no-deps elysian_strategy_0 \
		python -c "from elysian_core.db.models import create_tables; create_tables(safe=True); print('Tables created.')"

db-clear:
	docker compose exec postgres psql -U $${POSTGRES_USER:-elysian} -d $${POSTGRES_DATABASE:-elysian} \
		-c "TRUNCATE TABLE cex_trades, dex_trades, portfolio_snapshots, account_snapshots RESTART IDENTITY CASCADE;"
