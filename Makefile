SERVICES = services/paper_ingestion services/learning_engine services/telegram_bot
# Compose wrapper: load local .env first when present, then image pins from versions.env.
COMPOSE_ENV_FILES = $(if $(wildcard .env),--env-file .env,) --env-file versions.env
COMPOSE = LETSENCRYPT_DOMAIN=local LETSENCRYPT_EMAIL=local@local.dev docker compose $(COMPOSE_ENV_FILES)
COMPOSE_PERF = $(COMPOSE) -f docker-compose.yml -f docker-compose.perf.yml

.PHONY: setup setup-service deps-export deps-check test test-service lint clean typecheck check ci-smoke up down logs rebuild rebuild-dashboard rebuild-backend rebuild-telegram rebuild-local up-build certs up-https profile profile-stack-up

## Generate locally-trusted dev certs via mkcert (run before `make up-https`)
certs:
	bash scripts/init-mkcert.sh

## Bring stack up with HTTPS on https://localhost:3001 via Caddy + mkcert
up-https:
	$(COMPOSE) --profile caddy-local up -d

## Create/update the root uv environment from uv.lock
setup:
	uv sync

## Deprecated: host development now uses the root uv environment
setup-service:
	@echo "Use 'make setup'. Service requirements are generated from uv.lock."

## Export Docker/pip requirements from uv.lock
deps-export:
	bash scripts/export-service-requirements.sh

## Verify uv.lock and generated requirements are in sync
deps-check:
	bash scripts/check-python-deps.sh

## Run all tests
test:
	uv run pytest -v

## Test a single service: make test-service SERVICE=services/learning_engine
test-service:
	uv run pytest $(SERVICE)/tests/ -v

## Lint all Python code
lint:
	bash scripts/check-migrations-no-tx.sh
	python3 scripts/check-no-jsonb-double-encode.py
	python3 scripts/check-no-unsafe-resolver.py
	uv run ruff check services/ libs/ scripts/

## Format all Python code
format:
	uv run ruff format services/ libs/ scripts/

## Remove all generated files
clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .ruff_cache -exec rm -rf {} + 2>/dev/null || true

## Remove virtual environments
clean-venvs:
	find . -type d -name .venv -exec rm -rf {} + 2>/dev/null || true

## Run pyright type checking
typecheck:
	npx pyright

## Enforce 0600 permissions on all secret files (run on first checkout and in CI)
secure-secrets:
	find secrets -maxdepth 1 -type f -name "*.txt" -exec chmod 600 {} \;

## Boot core Docker services with disposable secrets and probe health endpoints
ci-smoke:
	bash scripts/ci-smoke.sh

## Run all quality checks: dependency parity + lint + typecheck + test
check: secure-secrets deps-check lint typecheck test

## Bring up Langfuse + JARVIS services with observability tracing enabled
observability-up:
	./scripts/gen-langfuse-keys.sh
	OBSERVABILITY_ENABLED=true LANGFUSE_HOST=http://langfuse:3000 \
	  LANGFUSE_INIT_PROJECT_PUBLIC_KEY=$$(cat secrets/langfuse_init_pk.txt) \
	  LANGFUSE_INIT_PROJECT_SECRET_KEY=$$(cat secrets/langfuse_init_sk.txt) \
	  $(COMPOSE) --profile observability up -d langfuse paper_ingestion learning_engine

## Docker shortcuts
up:
	$(COMPOSE) up -d

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f

rebuild: rebuild-dashboard

rebuild-dashboard:
	$(COMPOSE) build --build-arg CACHE_BUST=$(shell date +%s) dashboard
	$(COMPOSE) up -d dashboard

rebuild-backend:
	$(COMPOSE) build paper_ingestion learning_engine
	$(COMPOSE) up -d paper_ingestion learning_engine

rebuild-telegram:
	$(COMPOSE) build telegram_bot
	$(COMPOSE) up -d telegram_bot

rebuild-local:
	$(COMPOSE) build paper_ingestion learning_engine dashboard
	$(COMPOSE) up -d paper_ingestion learning_engine dashboard

up-build:
	$(COMPOSE) up -d --build

## Capture a perf snapshot (frontend bundle + backend timings + py-spy + pg_stat_statements)
## Output: artifacts/perf/<UTC-timestamp>/. See docs/perf/HOWTO.md for prerequisites.
profile:
	bash scripts/profile.sh

## Boot the local stack with profiling-only Postgres/ptrace overrides.
profile-stack-up:
	$(COMPOSE_PERF) up -d --no-deps postgres paper_ingestion dashboard
