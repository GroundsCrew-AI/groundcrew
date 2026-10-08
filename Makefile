UV ?= uv
API := apps/api
WEB := apps/web

# Local dev and tests use the compose Postgres (host port 5433). Pass DB_URL=... to target another DB.
DB_URL ?= postgresql+asyncpg://groundcrew:groundcrew@localhost:5433/groundcrew
TEST_DATABASE_URL ?= postgresql+asyncpg://groundcrew:groundcrew@localhost:5433/groundcrew_test
export TEST_DATABASE_URL

# The CloudFormation template behind the Connect AWS link (make publish-connect-template).
CONNECT_TEMPLATE_ACCOUNT ?= 881050425883
CONNECT_TEMPLATE_REGION ?= us-east-2
CONNECT_TEMPLATE_BUCKET ?= groundcrew-templates-$(CONNECT_TEMPLATE_ACCOUNT)

.PHONY: help install dev up down db migrate seed worker keygen test lint format models api-types publish-connect-template

help:
	@echo "make install  Install API (uv) and web (npm) dependencies"
	@echo "make dev      Run postgres, api, worker, and web with hot reload (Docker Compose)"
	@echo "make down     Stop the dev stack"
	@echo "make db       Start only Postgres (for tests or running the API on the host)"
	@echo "make migrate  Apply Alembic migrations to DB_URL"
	@echo "make seed     Create the dev user and the Demo workspace in DB_URL"
	@echo "make worker   Run the job worker on the host against DB_URL"
	@echo "make keygen   Print a new Fernet key for SECRETS_KEY"
	@echo "make test     Run API tests (needs Postgres: make db)"
	@echo "make lint     ruff + mypy (api, scripts), eslint + tsc (web)"
	@echo "make format   Auto-format Python code"
	@echo "make models   Smoke test Nemotron models on Token Factory (uses credits)"
	@echo "make api-types  Regenerate the web app's typed API client from FastAPI's OpenAPI schema"
	@echo "make publish-connect-template  Upload infra/aws-connect.yaml to S3 (your AWS CLI credentials)"

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

db:
	docker compose up -d --wait postgres

migrate:
	cd $(API) && DATABASE_URL=$(DB_URL) $(UV) run alembic upgrade head

seed:
	cd $(API) && DATABASE_URL=$(DB_URL) PYTHONPATH=. $(UV) run python ../../scripts/seed.py

worker:
	cd $(API) && DATABASE_URL=$(DB_URL) $(UV) run python -m app.worker

keygen:
	@cd $(API) && $(UV) run python -c "from app.security.crypto import generate_key; print(generate_key())"

test: $(WEB)/node_modules
	cd $(API) && $(UV) run pytest
	cd $(WEB) && npm test

api-types: $(WEB)/node_modules
	cd $(API) && PYTHONPATH=. $(UV) run python ../../scripts/export_openapi.py ../web/lib/api/openapi.json
	cd $(WEB) && npx openapi-typescript lib/api/openapi.json -o lib/api/schema.d.ts

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

# Uses your own AWS CLI credentials (not the platform identity), only after checking the account.
publish-connect-template:
	@account=$$(aws sts get-caller-identity --query Account --output text) || exit 1; \
	if [ "$$account" != "$(CONNECT_TEMPLATE_ACCOUNT)" ]; then \
	  echo "These AWS credentials are for account $$account, not $(CONNECT_TEMPLATE_ACCOUNT). Stopping."; \
	  exit 1; \
	fi
	cmp -s infra/aws-connect.yaml $(API)/app/connectors/templates/aws-connect.yaml || \
	  { echo "infra/aws-connect.yaml and the API's copy differ; copy it over first."; exit 1; }
	aws s3 cp infra/aws-connect.yaml s3://$(CONNECT_TEMPLATE_BUCKET)/aws-connect.yaml \
	  --region $(CONNECT_TEMPLATE_REGION) --content-type text/yaml
	@echo "AWS_CONNECT_TEMPLATE_URL=https://$(CONNECT_TEMPLATE_BUCKET).s3.$(CONNECT_TEMPLATE_REGION).amazonaws.com/aws-connect.yaml"
