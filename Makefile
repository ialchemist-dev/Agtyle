# Agtyle canonical developer commands.
# Every target must work from a clean clone with only `uv` and `make` installed.

UV ?= uv
RUN := $(UV) run
PY := $(RUN) python

.DEFAULT_GOAL := help
.PHONY: help bootstrap init format lint typecheck test-unit test-contract test-persistence \
        test-integration test-e2e test-failures test verify demo-reminder clean-clone-verify \
        migrate-check report clean

help: ## Show available targets
	@grep -E '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

bootstrap: ## Install locked Python dependencies and the pinned Cedar CLI
	$(UV) sync --locked --all-extras
	$(PY) scripts/install_cedar.py
	@# Policy validation runs during bootstrap, in CI, and at application startup.
	$(RUN) agtyle validate

init: ## Create data directories, run migrations, validate registry and policies
	$(RUN) agtyle init

format: ## Apply formatting
	$(RUN) ruff format src tests scripts migrations
	$(RUN) ruff check --fix src tests scripts

lint: ## Check formatting and lint rules
	$(RUN) ruff format --check src tests scripts migrations
	$(RUN) ruff check src tests scripts

typecheck: ## Strict static typing for the package
	$(RUN) mypy --strict src/agtyle

test-unit: ## Domain unit tests
	$(RUN) pytest tests/unit

test-contract: ## Schema, manifest, port and policy contract tests
	$(RUN) pytest tests/contract

test-persistence: ## Migration, constraint and concurrency tests
	$(RUN) pytest tests/persistence

test-integration: ## Service-level integration tests with real Cedar
	$(RUN) pytest tests/integration

test-e2e: ## Deterministic end-to-end scenario
	$(RUN) pytest tests/e2e

test-failures: ## Failure-injection and recovery tests
	$(RUN) pytest tests/failure_injection

test: ## All deterministic tests; no live external APIs
	$(RUN) pytest tests

migrate-check: ## Prove migrations are reversible on a throwaway database
	$(PY) scripts/migration_check.py

verify: lint typecheck migrate-check ## Full deterministic gate
	$(RUN) agtyle validate
	$(RUN) pytest tests --cov=agtyle --cov-branch --cov-report=term-missing \
		--cov-report=json:artifacts/verification/coverage.json
	$(PY) scripts/check_coverage.py
	$(PY) scripts/generate_report.py

demo-reminder: ## Live local multi-process reminder proof
	$(PY) scripts/reminder_demo.py

clean-clone-verify: ## Clone at HEAD into a temporary directory and verify there
	$(PY) scripts/clean_clone_verify.py

clean: ## Remove generated runtime data
	rm -rf .agtyle .pytest_cache .mypy_cache .ruff_cache .coverage
