# Cadence - two-stage build on python:3.12-slim (D-006 pins 3.12; .python-version agrees).
#
# Stage 1 resolves the locked dependency set with uv; stage 2 carries the finished /app across
# and never sees uv, a compiler, or the lockfile's build machinery.
#
# The uv tag is pinned deliberately, but `0.11` is a *minor* tag and still moves across patch
# releases: it narrows reproducibility rather than closing it. Two builds of one commit months
# apart can carry different uv patch versions - what they cannot carry is different *Cadence*
# dependencies, which is `uv sync --frozen` plus uv.lock's job and is the property that matters
# here. `:latest` would give up even that.
#
# Closing the remaining gap means `ghcr.io/astral-sh/uv@sha256:<index digest>`. Deliberately not
# done: the digest has to be the multi-arch *index*, not a platform manifest, and pinning the
# amd64 one - which is what `podman image inspect` hands you - breaks any build that is not
# amd64. Revisit if the build ever needs to be bit-reproducible (D-208).

FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /bin/uv

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first, in their own layer: pyproject.toml and uv.lock change far less often than
# cadence/ does, so an ordinary code change reuses this layer instead of re-resolving the tree.
# ``--frozen`` fails rather than silently re-locking if uv.lock is stale; it is committed.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY cadence/ ./cadence/
COPY library/ ./library/
COPY README.md ./
RUN uv sync --frozen --no-dev


FROM python:3.12-slim

# uid 10001 is fixed, not allocated: docs/deploy.md tells the operator to chown the host bind
# mount to it, and a uid that moved between builds would break every existing deployment.
RUN useradd --system --uid 10001 --create-home --home-dir /home/cadence cadence

WORKDIR /app

# Root-owned deliberately, and therefore read-only to the runtime uid. The app never writes to
# its own code or venv, so handing uid 10001 write access to both only widens what a compromised
# process can rewrite in place. /app/data below is the one directory that must be writable
# (D-202). The files keep their 0755/0644 modes, so `cadence` can still read the venv and
# execute scripts/backup.sh, which is how the in-container backup runs.
COPY --from=builder /app /app
COPY scripts/ ./scripts/

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CADENCE_DB_PATH=/app/data/cadence.db \
    CADENCE_ENV=prod \
    CADENCE_VITALFORGE_MODE=live

# Created here so a run with no bind mount still starts; a bind mount shadows it, which is why
# docs/deploy.md makes the host directory's ownership a first-deploy step.
RUN mkdir -p /app/data/backups && chown -R cadence:cadence /app/data

USER cadence

EXPOSE 8000
VOLUME ["/app/data"]

# Parses ``ok``, not just the HTTP status: /api/health answers 200 with ``ok: false`` when the
# database check fails (PRP-00), so a status-only probe would call a broken app healthy.
# It makes no network call to VitalForge, which is what makes a 30 s interval safe.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import json,urllib.request; d=json.load(urllib.request.urlopen('http://localhost:8000/api/health',timeout=4)); raise SystemExit(0 if d.get('ok') else 1)"

# Exactly one worker, and no --reload. A second worker would run its own copy of the PRP-06 sync
# drain and the periodic metrics refresh, double-POSTing every session to VitalForge and doubling
# Garmin traffic against a shared credential with no rate-limit backoff. If workers are ever
# wanted, the drain has to move out of the app process first.
CMD ["uvicorn", "cadence.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
