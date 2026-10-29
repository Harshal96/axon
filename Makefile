.PHONY: up down init test lint typecheck evals

up:
	docker compose up -d
	@echo "Waiting for Neo4j..." && sleep 5
	uv run axon-init

down:
	docker compose down

init:
	uv run axon-init

dev:
	uv run uvicorn axon.api.app:app --reload --host 0.0.0.0 --port 8080

test:
	uv run pytest -v --cov=axon --cov-report=term-missing

lint:
	uv run ruff check axon/v2 axon/api/app.py cli tests/v2 tests/cli tests/evals
	uv run ruff format --check axon/v2 axon/api/app.py cli tests/v2 tests/cli tests/evals

typecheck:
	uv run mypy axon/v2 tests/evals

evals:
	uv run python -m tests.evals.harness

fmt:
	uv run ruff format axon/v2 axon/api/app.py cli tests/v2 tests/cli tests/evals
	uv run ruff check --fix axon/v2 axon/api/app.py cli tests/v2 tests/cli tests/evals

test-tier1:
	uv run pytest tests -m "tier1 or regression" -v --tb=short

test-tier2:
	uv run pytest tests -m "tier1 or tier2 or regression" -v --tb=short

test-tier3:
	uv run pytest tests -m "tier1 or tier2 or tier3 or regression" -v --tb=long

test-fast:
	uv run pytest -q --tb=line

test-all:
	uv run pytest tests -v --tb=short

test-coverage:
	uv run pytest tests --cov=axon --cov-report=html --cov-report=term-missing

test-snapshot-update:
	uv run python tests/regression/update_snapshots.py

test-watch:
	uv run pytest-watch -- -m tier1 -q --tb=line

test-docker-up:
	docker compose -f docker-compose.test.yml up -d
	@echo "Waiting for services..." && sleep 20

test-docker-down:
	docker compose -f docker-compose.test.yml down
