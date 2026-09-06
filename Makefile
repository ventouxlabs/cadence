# Cadence - everything runs through uv (D-006: Python 3.12).
CADENCE_HOST ?= 0.0.0.0
CADENCE_PORT ?= 8090

.PHONY: dev test lint fmt seed e2e deploy

dev:  ## Run the app with reload on $(CADENCE_HOST):$(CADENCE_PORT)
	uv run uvicorn cadence.main:app --reload --host $(CADENCE_HOST) --port $(CADENCE_PORT)

test:  ## Unit + API tests with coverage
	uv run pytest --cov=cadence --cov-report=term-missing --cov-fail-under=80

lint:  ## The CI gate
	uv run ruff check . && uv run ruff format --check .

fmt:  ## Format and autofix in place
	uv run ruff format . && uv run ruff check --fix .

seed:  ## Load library/ into the database and build a block per profile
	uv run python -m cadence.bibliotheque.seed

e2e:  ## Playwright, 390x844 (PRP-02 adds the tests)
	uv run pytest tests/e2e --browser chromium -o addopts=""

deploy:  ## Placeholder until PRP-09
	@echo "see docs/deploy.md (PRP-09)"
