PY ?= python3
VENV := .venv
BIN := $(VENV)/bin
COMPOSE := docker compose --project-name slurmrca --file cluster/docker-compose.yml

.PHONY: help install list validate test lint typecheck check cluster-up cluster-down heal smoke clean

help: ## Show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "};{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

$(BIN)/slurm-rca: pyproject.toml
	@test -d $(VENV) || $(PY) -m venv $(VENV)
	@$(BIN)/python -m pip install -q --upgrade pip
	@$(BIN)/python -m pip install -q -e ".[dev]"
	@touch $(BIN)/slurm-rca

install: $(BIN)/slurm-rca ## Create the venv and install

list: install ## Show the scenario table
	@$(BIN)/slurm-rca list

validate: install ## Check every scenario's ground truth
	@$(BIN)/slurm-rca validate

test: install ## Run the test suite (no Docker required)
	@$(BIN)/python -m pytest

lint: install ## ruff check + ruff format --check, exactly as CI runs them
	@$(BIN)/ruff check .
	@# CI runs `ruff format --check` too. It was missing here, so `make check`
	@# could pass on a commit that CI then failed on formatting alone.
	@$(BIN)/ruff format --check .

typecheck: install ## mypy --strict
	@$(BIN)/mypy

check: lint typecheck test validate ## Everything CI runs

cluster-up: ## Bring up the isolated benchmark cluster
	@$(COMPOSE) up -d --wait
	@$(COMPOSE) exec -T slurmctld sinfo

cluster-down: ## Tear it down, volumes and all
	@$(COMPOSE) down --volumes --remove-orphans

heal: install ## Undo every injection. Idempotent; safe to run any time.
	@# An interrupted run leaves the cluster injected — a laptop sleeping
	@# mid-scenario is enough. This restores it without needing to know which
	@# scenario was running.
	@docker unpause slurmrca-mysql 2>/dev/null || true
	@$(COMPOSE) exec -T slurmctld sh -c "pkill -f 'while true' || true" 2>/dev/null || true
	@$(COMPOSE) exec -T cpu-worker sh -c "rm -f /usr/local/bin/nvidia-smi || true" 2>/dev/null || true
	@$(COMPOSE) start cpu-worker 2>/dev/null || true
	@$(COMPOSE) exec -T slurmctld scontrol update NodeName=ALL State=RESUME 2>/dev/null || true
	@echo "healed"

smoke: install ## Inject and heal S01 against a live cluster
	@$(BIN)/python -m slurmrca.smoke

clean: ## Remove venv and caches
	@rm -rf $(VENV) .pytest_cache .mypy_cache .ruff_cache results
	@find . -name __pycache__ -type d -prune -exec rm -rf {} +
