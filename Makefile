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

.PHONY: help install dev up down db migrate seed worker keygen test lint format models api-types publish-connect-template \
	kind-up kind-down kind-kubeconfig kind-check kind-register dind-check smoke-ecs cleanup-deploys

# KIND=1 joins api and worker to the kind network (make kind-up first).
COMPOSE := docker compose$(if $(filter 1,$(KIND)), -f docker-compose.yml -f docker-compose.kind.yml)

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
	@echo "make kind-up  Local kind cluster, registry localhost:5001, ingress, deployer RBAC (METRICS=1 adds metrics-server)"
	@echo "make kind-down  Delete the kind cluster and its registry"
	@echo "make kind-kubeconfig  Kubeconfigs for the groundcrew-deployer ServiceAccount (.kind/)"
	@echo "make kind-check  Check the deployer's RBAC and admission policy on the live cluster"
	@echo "make dev KIND=1  The dev stack, with api, worker, and dind on the kind network"
	@echo "make dind-check  From the worker: build a fixture in dind, push to kind-registry:5000, run it in kind"
	@echo "make kind-register LOGIN=<github login> [DISCOVER=1]  Register kind as a Kubernetes connection (dev)"
	@echo "make smoke-ecs CONNECTION=<aws connection id> [DRY_RUN=0] [CLEANUP=1]  Prove the AWS deploy role on real AWS"
	@echo "make cleanup-deploys HOURS=<n> [APPLY=1] [CONNECTION=<id>]  Delete deploy leftovers older than n hours (dry run by default)"

install:
	cd $(API) && $(UV) sync
	cd $(WEB) && npm ci

$(WEB)/node_modules: $(WEB)/package-lock.json
	cd $(WEB) && npm ci
	@touch $@

dev:
	$(COMPOSE) up --build

down:
	$(COMPOSE) down

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

kind-up:
	scripts/kind/up.sh

kind-down:
	scripts/kind/down.sh

kind-kubeconfig:
	scripts/kind/kubeconfig.sh

kind-check:
	scripts/kind/check.sh

# Needs make kind-up, make kind-kubeconfig, and make dev KIND=1 (docs/deploy-live.md).
dind-check:
	scripts/kind/dind-check.sh

# Registers .kind/groundcrew-deployer.kubeconfig through the API in LOGIN's workspace (dev only:
# signs an API token with INTERNAL_JWT_SECRET). DISCOVER=1 also runs discovery and checks the Map.
kind-register:
	@test -n "$(LOGIN)" || { echo "Usage: make kind-register LOGIN=<your GitHub login> [DISCOVER=1]"; exit 1; }
	DATABASE_URL=$(DB_URL) $(UV) run --project $(API) python scripts/kind_register.py --login $(LOGIN) \
		$(if $(filter 1,$(DISCOVER)),--discover)

# Smoke test of the AWS deploy tier (docs/deploy-live.md). DRY_RUN=1 (default) only reads and
# prints the plan; DRY_RUN=0 deploys a tiny app, runs the negative checks, and cleans up;
# CLEANUP=1 removes leftovers of a failed run. Uses the connection's deploy role only.
DRY_RUN ?= 1
smoke-ecs:
	@test -n "$(CONNECTION)" || { echo "Usage: make smoke-ecs CONNECTION=<aws connection id> [DRY_RUN=0] [CLEANUP=1]"; exit 1; }
	DATABASE_URL=$(DB_URL) $(UV) run --project $(API) python scripts/smoke_ecs_deploy.py --connection $(CONNECTION) \
		$(if $(filter 1,$(CLEANUP)),--cleanup,$(if $(filter 0,$(DRY_RUN)),--real))

# Deletes what deploys left behind (project=groundcrew, older than HOURS) on AWS (every
# deploy-tier connection, or CONNECTION) and in kind. Dry run unless APPLY=1 (docs/phase4-infra.md).
# AWS_ACCOUNT: the run stops unless the platform identity is in it (checked before any AssumeRole).
AWS_ACCOUNT ?= $(CONNECT_TEMPLATE_ACCOUNT)
cleanup-deploys:
	@test -n "$(HOURS)" || { echo "Usage: make cleanup-deploys HOURS=<n> [APPLY=1] [CONNECTION=<aws connection id>]"; exit 1; }
	DATABASE_URL=$(DB_URL) $(UV) run --project $(API) python scripts/cleanup_deploys.py --hours $(HOURS) \
		--account $(AWS_ACCOUNT) \
		$(if $(filter 1,$(APPLY)),--apply) $(if $(CONNECTION),--connection $(CONNECTION))
