# Developer entry points. Every quality gate CI runs is reproducible locally with `make check`.
.DEFAULT_GOAL := help
SHELL := /bin/bash
UV ?= uv
export AGENT_UID ?= $(shell id -u)
export AGENT_GID ?= $(shell id -g)

.PHONY: help
help: ## List targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS=":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

.PHONY: install
install: ## Install all dependency groups and git hooks
	$(UV) sync --all-groups
	$(UV) run pre-commit install

.PHONY: format
format: ## Auto-format and auto-fix
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

.PHONY: lint
lint: ## Ruff lint + format check
	$(UV) run ruff check .
	$(UV) run ruff format --check .

.PHONY: type
type: ## mypy --strict
	$(UV) run mypy

.PHONY: security
security: ## bandit (SAST) + pip-audit (known CVEs in dependencies)
	$(UV) run bandit -q -c pyproject.toml -r src scripts
	$(UV) export --frozen --no-dev --no-hashes --no-emit-project -o /tmp/autoquote-req.txt >/dev/null
	$(UV) run pip-audit --strict -r /tmp/autoquote-req.txt

.PHONY: test
test: ## Full test suite with coverage gate (PostgreSQL via TEST_DATABASE_URL or testcontainers)
	$(UV) run pytest --cov --cov-report=term-missing:skip-covered --cov-report=xml

.PHONY: test-fast
test-fast: ## Unit tests only
	$(UV) run pytest tests/unit -q

.PHONY: migrations-check
migrations-check: ## Alembic: models and migrations are in sync (needs DATABASE_URL)
	$(UV) run alembic upgrade head
	$(UV) run alembic check

.PHONY: check
check: lint type security test ## Every quality gate

.PHONY: up
up: ## Build and start the full stack (postgres, quote-service, agent)
	docker compose up --build -d

.PHONY: down
down: ## Stop the stack
	docker compose down

.PHONY: logs
logs: ## Follow container logs
	docker compose logs -f

.PHONY: simulate
simulate: ## Run the scripted conversations inside the stack
	docker compose exec agent python -m src.main --run-simulation

.PHONY: simulate-local
simulate-local: ## Run scenarios locally (SQLite) against QUOTE_SERVICE_URL
	$(UV) run python -m src.main --run-simulation --sqlite

.PHONY: dataset
dataset: ## Download the challenge dataset (synthetic, PII-shaped; git-ignored)
	$(UV) run python -m scripts.fetch_dataset

.PHONY: eval
eval: ## Evaluate extraction accuracy against the dataset labels
	$(UV) run --group eval python -m scripts.eval_extraction

.PHONY: ai-logs
ai-logs: ## Export (and sanitize) Claude Code sessions into ai-logs/
	$(UV) run python -m scripts.export_ai_logs
