UV ?= uv
API := apps/api
WEB := apps/web

.PHONY: help install dev down test lint format models

help:
	@echo "make install  Install API (uv) and web (npm) dependencies"
	@echo "make dev      Run api on :8000 and web on :3000 with hot reload (Docker Compose)"
	@echo "make down     Stop the dev stack"
	@echo "make test     Run API tests"
	@echo "make lint     ruff + mypy (api, scripts), eslint + tsc (web)"
	@echo "make format   Auto-format Python code"
	@echo "make models   Smoke test Nemotron models on Token Factory (uses credits)"

install:
	cd $(API) && $(UV) sync
	cd $(WEB) && npm ci

$(WEB)/node_modules: $(WEB)/package-lock.json
	cd $(WEB) && npm ci
	@touch $@

dev:
	docker compose up --build

down:
	docker compose down

test:
	cd $(API) && $(UV) run pytest

lint: $(WEB)/node_modules
	cd $(API) && $(UV) run ruff check . ../../scripts
	cd $(API) && $(UV) run ruff format --check . ../../scripts
	cd $(API) && $(UV) run mypy app tests ../../scripts
	cd $(WEB) && npm run lint
	cd $(WEB) && npm run typecheck

format:
	cd $(API) && $(UV) run ruff format . ../../scripts
	cd $(API) && $(UV) run ruff check --fix . ../../scripts

models:
	$(UV) run --project $(API) python scripts/test_models.py
