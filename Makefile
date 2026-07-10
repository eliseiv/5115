# Dev shortcuts. Canonical commands are defined in docs/02-tech-stack.md — this only wraps them.
.DEFAULT_GOAL := help
.PHONY: help install fmt fmt-check lint type test test-cov migrate run \
        docker-build up up-obs down logs ci

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  %-14s %s\n", $$1, $$2}'

install: ## uv sync (deps + venv)
	uv sync

fmt: ## ruff format (writes changes)
	uv run ruff format .

fmt-check: ## ruff format --check (CI)
	uv run ruff format --check .

lint: ## ruff check
	uv run ruff check .

type: ## mypy src
	uv run mypy src

test: ## pytest
	uv run pytest

test-cov: ## pytest with global 80% coverage gate
	uv run pytest --cov=src --cov-report=term-missing --cov-fail-under=80

migrate: ## alembic upgrade head
	uv run alembic upgrade head

run: ## run dev server (uvicorn --reload)
	uv run uvicorn app.main:app --reload

ci: fmt-check lint type test-cov ## run the full local CI gate

docker-build: ## build the production image
	docker build -t claude-ios-backend:local .

up: ## start full stack (postgres + redis + migrate + api)
	docker compose up --build -d

up-obs: ## start stack + Prometheus overlay
	docker compose -f docker-compose.yml -f docker-compose.observability.yml up --build -d

down: ## stop stack and remove volumes
	docker compose -f docker-compose.yml -f docker-compose.observability.yml down -v

logs: ## tail api logs
	docker compose logs -f api
