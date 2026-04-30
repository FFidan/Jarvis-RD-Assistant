SERVICES = services/paper_ingestion services/learning_engine services/telegram_bot

.PHONY: setup setup-service deps-export deps-check test test-service lint clean typecheck check ci-smoke

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

## Docker shortcuts
up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f

rebuild:
	docker compose up -d --build
