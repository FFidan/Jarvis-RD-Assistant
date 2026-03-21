SERVICES = services/paper_ingestion services/learning_engine services/telegram_bot

.PHONY: setup setup-service test test-service lint clean

## Create all virtual environments and install dependencies
setup:
	@for dir in $(SERVICES); do \
		echo "=== Setting up $$dir ==="; \
		cd $$dir && python3 -m venv .venv && \
		.venv/bin/pip install --upgrade pip && \
		.venv/bin/pip install -e $(CURDIR)/libs/jarvis_common && \
		.venv/bin/pip install -r requirements.txt && \
		cd $(CURDIR); \
	done
	@echo "=== All services set up ==="

## Setup a single service: make setup-service SERVICE=services/paper_ingestion
setup-service:
	cd $(SERVICE) && python3 -m venv .venv && \
	.venv/bin/pip install --upgrade pip && \
	.venv/bin/pip install -r requirements.txt

## Run all tests
test:
	@for dir in $(SERVICES); do \
		echo "=== Testing $$dir ===" && \
		(cd $$dir && .venv/bin/pytest tests/ -v) || exit 1; \
	done

## Test a single service: make test-service SERVICE=services/learning_engine
test-service:
	cd $(SERVICE) && .venv/bin/pytest tests/ -v

## Lint all Python code
lint:
	ruff check services/ libs/ scripts/

## Format all Python code
format:
	ruff format services/ libs/ scripts/

## Remove all generated files
clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .ruff_cache -exec rm -rf {} + 2>/dev/null || true

## Remove virtual environments
clean-venvs:
	find . -type d -name .venv -exec rm -rf {} + 2>/dev/null || true

## Docker shortcuts
up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f

rebuild:
	docker compose up -d --build
