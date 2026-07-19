SERVICES = services/paper_ingestion services/learning_engine services/telegram_bot
# Compose wrapper: load local .env first when present, then image pins from versions.env.
COMPOSE_ENV_FILES = $(if $(wildcard .env),--env-file .env,) --env-file versions.env
COMPOSE = LETSENCRYPT_DOMAIN=local LETSENCRYPT_EMAIL=local@local.dev docker compose $(COMPOSE_ENV_FILES)
COMPOSE_PERF = $(COMPOSE) -f docker-compose.yml -f docker-compose.perf.yml

.PHONY: setup dev-env setup-service deps-export deps-check test test-service lint clean typecheck frontend-check check ci-smoke up down logs rebuild rebuild-dashboard rebuild-backend rebuild-telegram rebuild-local up-build certs up-https profile profile-stack-up gen-langfuse-keys init-secrets no-tracked-secrets

## Generate locally-trusted dev certs via mkcert (run before `make up-https`)
certs:
	bash scripts/init-mkcert.sh

## Bring stack up with HTTPS on https://localhost:3443 via Caddy + mkcert
## Fails loudly if the mkcert certs are absent (run `make certs` first).
up-https:
	@test -f certs/cert.pem && test -f certs/key.pem || { \
	  echo "mkcert certs missing (certs/cert.pem, certs/key.pem) — run 'make certs' first (needs mkcert installed)."; \
	  exit 1; }
	$(COMPOSE) --profile caddy-local up -d

## Install Python dev dependencies from uv.lock (does NOT run setup.sh / docker setup)
## For a full single-instance install run: ./setup.sh (interactive) or
## ./scripts/jarvis-setup.sh (non-interactive).
dev-env:
	uv sync --group dev

## Alias kept for backward compatibility — delegates to dev-env
setup: dev-env

## Deprecated: host development now uses the root uv environment
setup-service:
	@echo "Use 'make dev-env'. Service requirements are generated from uv.lock."

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
	uv run python scripts/check-complexity-budget.py

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

## Run pyright type checking (pinned to same version as CI)
typecheck:
	npx --yes pyright@1.1.408

## Enforce the secrets contract: directory 700 (owner-only), files 644 (readable by non-root service containers via the compose bind mount)
secure-secrets:
	chmod 700 secrets
	find secrets -maxdepth 1 -type f -name "*.txt" -exec chmod 644 {} \;

## Boot core Docker services with disposable secrets and probe health endpoints
ci-smoke:
	bash scripts/ci-smoke.sh

## Fail if any secrets/*.txt file is tracked by git (recurrence guard)
no-tracked-secrets:
	bash scripts/check-no-tracked-secrets.sh

## Frontend lint + typecheck + unit tests + build (mirrors CI `frontend` job)
frontend-check:
	npm --prefix frontend ci
	npm --prefix frontend run lint
	npm --prefix frontend run typecheck
	npm --prefix frontend run test -- --run --no-file-parallelism
	npm --prefix frontend run build

## Run all local quality gates (mirrors CI lint-test job + frontend job).
##
## Ordered fast → slow:
##   1. Guard: no tracked secrets
##   2. Guard: dependency parity (uv.lock ↔ requirements*.txt)
##   3. Lint (ruff + migrations-no-tx + jsonb-double-encode + unsafe-resolver)
##   4. Tach (module boundary check)
##   5. Pyright (type check)
##   6. Test-shape check
##   7. Guard: burned secrets
##   8. Fast pytest suite (excludes live_pg / integration / slow)
##   9. Frontend lint + typecheck + tests + build
##
## NOT included (require a live Postgres — run in CI or opt-in locally):
##   JARVIS_RUN_LIVE_PG=1 uv run pytest -m contract -v
##   JARVIS_RUN_LIVE_PG=1 uv run pytest -m "integration and live_pg" \
##     services/paper_ingestion/tests/integration/test_cross_user_isolation.py -v
check: no-tracked-secrets secure-secrets deps-check lint
	uv run tach check
	$(MAKE) typecheck
	uv run python3 scripts/check-test-shape.py
	uv run python3 scripts/check_contract_docs.py
	bash scripts/check-burned-secrets.sh
	bash scripts/tests/test_backup_coverage.sh
	bash scripts/tests/test_restore_coverage.sh
	bash scripts/tests/test_prune_coverage.sh
	bash scripts/tests/test_setup_lib_helpers.sh
	bash scripts/tests/test_update_coverage.sh
	bash scripts/tests/test_jarvis_research_cli.sh
	bash scripts/tests/test_uninstall.sh
	@if command -v shellcheck >/dev/null 2>&1; then shellcheck scripts/jarvis-research.sh; else echo "shellcheck not installed; skipping scripts/jarvis-research.sh lint"; fi
	@if command -v shellcheck >/dev/null 2>&1; then shellcheck scripts/uninstall.sh; else echo "shellcheck not installed; skipping scripts/uninstall.sh lint"; fi
	uv run pytest
	$(MAKE) frontend-check

## Bring up Langfuse + JARVIS services with observability tracing enabled.
## Keys are loaded from .env (written by gen-langfuse-keys.sh) so they never
## appear in the process environment or docker inspect output. DATABASE_URL /
## NEXTAUTH_SECRET / SALT are read from /run/secrets/* by the wrapper image at
## ./langfuse/ (they are no longer exposed via
## `docker inspect`). First boot builds the wrapper image (~30s); subsequent
## boots use the cached layer.
# No --build: langfuse is unpublished and rebuilds itself via `pull_policy: build`,
# while paper_ingestion/learning_engine are published and pull. Forcing --build here
# would rebuild the multi-GB torch images from a cold cache.
observability-up: gen-langfuse-keys
	OBSERVABILITY_ENABLED=true LANGFUSE_HOST=http://langfuse:3000 \
	  $(COMPOSE) --profile observability up -d langfuse paper_ingestion learning_engine

## Docker shortcuts
up: gen-langfuse-keys init-secrets
	$(COMPOSE) up -d

## Ensure Langfuse init keypair exists before any compose up (idempotent — never overwrites an existing key)
gen-langfuse-keys:
	./scripts/gen-langfuse-keys.sh

## Ensure Docker-secret source files exist before any compose up (idempotent — never overwrites existing secrets)
init-secrets:
	./scripts/init-secrets.sh

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

up-build: gen-langfuse-keys init-secrets
	$(COMPOSE) up -d --build

## Capture a perf snapshot (frontend bundle + backend timings + py-spy + pg_stat_statements)
## Output: artifacts/perf/<UTC-timestamp>/. See docs/perf/HOWTO.md for prerequisites.
profile:
	bash scripts/profile.sh

## Boot the local stack with profiling-only Postgres/ptrace overrides.
profile-stack-up:
	$(COMPOSE_PERF) --profile perf up -d --no-deps postgres paper_ingestion dashboard
