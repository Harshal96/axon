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
	uv run ruff check axon/v1 axon/api/app.py cli tests/v1 tests/cli tests/evals
	uv run ruff format --check axon/v1 axon/api/app.py cli tests/v1 tests/cli tests/evals

typecheck:
	uv run mypy axon/v1 tests/evals

evals:
	uv run python -m tests.evals.harness

fmt:
	uv run ruff format axon/v1 axon/api/app.py cli tests/v1 tests/cli tests/evals
	uv run ruff check --fix axon/v1 axon/api/app.py cli tests/v1 tests/cli tests/evals

test-fast:
	uv run pytest -q --tb=line

test-all:
	uv run pytest tests/v1 tests/cli tests/evals -v --tb=short

test-coverage:
	uv run pytest --cov=axon --cov-report=html --cov-report=term-missing
