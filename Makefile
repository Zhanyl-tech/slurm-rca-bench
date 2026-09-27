PY ?= python3
VENV := .venv
BIN := $(VENV)/bin
COMPOSE := docker compose --project-name slurmrca --file cluster/docker-compose.yml
SCRIPTS := src/slurmrca/scripts/*.sh cluster/build-image.sh
# Coverage gate. Measured at 0.2.0: see CHANGELOG. Raise it, never lower it to
# make a change pass.
COV_MIN := 90

.PHONY: help install list validate test lint typecheck shellcheck check wheel-check \
	image cluster-up cluster-down heal smoke smoke-full clean

help: ## Show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "};{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

# The first version ran a bare `python3 -m venv`; on a machine whose python3 is
# 3.9 that created a venv pip then refused to install into. uv, when present,
# fetches a suitable interpreter; otherwise $(PY) must be >= 3.11.
$(BIN)/slurm-rca: pyproject.toml
	@if command -v uv >/dev/null 2>&1; then \
		test -d $(VENV) || uv venv -q --python 3.12 $(VENV); \
		uv pip install -q --python $(BIN)/python -e ".[dev]"; \
	else \
		$(PY) -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "need Python >= 3.11; set PY=python3.12 or install uv")'; \
		test -d $(VENV) || $(PY) -m venv $(VENV); \
		$(BIN)/python -m pip install -q --upgrade pip; \
		$(BIN)/python -m pip install -q -e ".[dev]"; \
	fi
	@touch $(BIN)/slurm-rca

install: $(BIN)/slurm-rca ## Create the venv and install
	@true

list: install ## Show the scenario table
	@$(BIN)/slurm-rca list

validate: install ## Check every scenario's ground truth
	@$(BIN)/slurm-rca validate

test: install ## Run the test suite with the coverage gate (no Docker required)
	@$(BIN)/python -m pytest --cov --cov-report=term --cov-fail-under=$(COV_MIN)

lint: install ## ruff check + ruff format --check, exactly as CI runs them
	@$(BIN)/ruff check .
	@# CI runs `ruff format --check` too. It was missing here, so `make check`
	@# could pass on a commit that CI then failed on formatting alone.
	@$(BIN)/ruff format --check .

typecheck: install ## mypy --strict
	@$(BIN)/mypy

shellcheck: install ## shellcheck + sh -n on every script sent into a container
	@$(BIN)/shellcheck --shell=sh $(SCRIPTS)
	@for f in $(SCRIPTS); do sh -n "$$f" || exit 1; done

# CI has two jobs. `check` is its `check` job; `wheel-check` is its `wheel`
# job, kept separate because it needs uv and network access to build. The
# label used to say "Everything CI runs" after the wheel job was added, and a
# packaging regression could pass `make check` and fail CI.
check: lint typecheck shellcheck test validate ## Everything in CI's check job (see wheel-check)
	@true

wheel-check: ## CI's wheel job: build the wheel, install it in a clean venv, run it from elsewhere
	@rm -rf build/wheel-check && mkdir -p build/wheel-check
	@uv build -q --wheel --out-dir build/wheel-check/dist
	@uv venv -q --python 3.12 build/wheel-check/venv
	@uv pip install -q --python build/wheel-check/venv/bin/python build/wheel-check/dist/*.whl
	@# Run from inside build/, away from the source tree's scenarios/, so only
	@# the copy packaged in the wheel can be found. The same four commands as
	@# CI's wheel job; tests/test_scenarios.py checks the two lists match.
	@cd build/wheel-check && venv/bin/slurm-rca validate && venv/bin/slurm-rca list \
		&& venv/bin/slurm-rca baselines && venv/bin/slurm-rca export > /dev/null

image: ## Fetch upstream at the pinned commit, verify SHA and clean tree, build the image (needs Docker)
	@sh cluster/build-image.sh

cluster-up: image ## Bring up the isolated benchmark cluster
	@$(COMPOSE) up -d --wait
	@$(COMPOSE) exec -T slurmctld sinfo

cluster-down: ## Tear it down, volumes and all
	@$(COMPOSE) down --volumes --remove-orphans

heal: install ## Undo every injection. Idempotent; safe to run any time.
	@# An interrupted run leaves the cluster injected — a laptop sleeping
	@# mid-scenario is enough. This used to be a hand-written list that missed
	@# five of ten injections and killed its own shell with `pkill -f`; it now
	@# runs every scenario's own heal.
	@$(BIN)/slurm-rca heal --all

# Transcripts go to evidence/<scenario>/<date>/, which git tracks and `clean`
# leaves alone: commit them. They used to go to results/, which is gitignored
# and which `clean` deletes.
smoke: install ## Inject and heal S01 against a live cluster (quick schedule); commit the transcript
	@$(BIN)/python -m slurmrca.smoke

smoke-full: install ## The ~16-minute S01 probe schedule; commit the transcript
	@$(BIN)/python -m slurmrca.smoke --full

clean: ## Remove venv, build output and caches
	@rm -rf $(VENV) build .pytest_cache .mypy_cache .ruff_cache .coverage results
	@find . -name __pycache__ -type d -prune -exec rm -rf {} +
