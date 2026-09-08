# Cadence - everything runs through uv (D-006: Python 3.12).
CADENCE_HOST ?= 0.0.0.0
CADENCE_PORT ?= 8090

.PHONY: dev test lint fmt seed seed-fresh e2e deploy dev-docker smoke backup perf screenshots

dev:  ## Run the app with reload on $(CADENCE_HOST):$(CADENCE_PORT)
	uv run uvicorn cadence.main:app --reload --host $(CADENCE_HOST) --port $(CADENCE_PORT)

test:  ## Unit + API tests with coverage
	uv run pytest --cov=cadence --cov-report=term-missing --cov-fail-under=80

lint:  ## The CI gate
	uv run ruff check . && uv run ruff format --check .

fmt:  ## Format and autofix in place
	uv run ruff format . && uv run ruff check --fix .

seed:  ## Load library/ into the database and build a block per profile (marks setup done)
	uv run python -m cadence.bibliotheque.seed

seed-fresh:  ## The same, left un-set-up so the front door opens on /setup (D-091)
	uv run python -m cadence.bibliotheque.seed --fresh

e2e:  ## Playwright, 390x844 (PRP-02 adds the tests)
	uv run pytest tests/e2e --browser chromium -o addopts=""

# ---------------------------------------------------------------- deploy and operations
# The workstation runs podman (D-004) and VM-201 runs Docker Compose. Only dev-docker is
# podman-specific; the compose files themselves stay in the Docker-compatible subset.

DEPLOY_MODE ?= rsync
BASE ?= http://localhost:8090

deploy:  ## rsync the working tree to VM-201 and bring the stack up (see docs/deploy.md)
	./scripts/deploy.sh --mode $(DEPLOY_MODE)

dev-docker:  ## Build and run the container locally on $(CADENCE_PORT) with podman-compose
	mkdir -p data/backups
	# Rootless podman maps container uid 10001 to a subuid on the host, so the plain chown the
	# VM uses does not apply here. `podman unshare` runs it inside that mapping instead.
	podman unshare chown -R 10001:10001 data
	# Built here, not by podman-compose, which produces an OCI image - and OCI has no
	# healthcheck field, so podman drops the Dockerfile's HEALTHCHECK with a warning and the
	# container reports no health at all. VM-201's Docker build keeps it either way.
	podman build --format docker -t cadence:local .
	# The dev overlay sets CADENCE_ENV=dev and CADENCE_VITALFORGE_MODE=mock: prod refuses a
	# mock integration (D-139), and mock is what `make smoke` needs to run without touching
	# real VitalForge or Garmin.
	podman-compose -f docker-compose.yml -f docker-compose.dev.yml up --no-build

smoke:  ## Run the smoke test against a running instance (make smoke BASE=https://cadence.grepon.cc)
	BASE=$(BASE) ./scripts/smoke.sh

perf:  ## Fail if /today breaks the D-025 page-weight budget (make perf BASE=https://cadence.grepon.cc)
	uv run python scripts/perf.py $(BASE)

screenshots:  ## Write docs/screenshots/ from a throwaway seeded database
	uv run python scripts/screenshots.py

backup:  ## Snapshot data/cadence.db into data/backups/, keeping the newest 30
	./scripts/backup.sh
