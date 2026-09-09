# Contributing

Cadence is a small self-hosted app for one household, but it holds a child's training data and
writes to a Garmin account, so the bar is higher than the size suggests. Two rules carry most
of the weight:

- **The validator is the only way in.** Seed, import and AI generation all pass through
  `cadence/validateur/`. If you add a write path, it goes through the validator too — not a
  second copy of the rules.
- **Youth rules are enforced, not documented.** A rule that is only "safe by accident today"
  gets a gate and a test that fails without it. See `docs/exercise-principles.md` §3.

## Setup

Python 3.12 exactly (`requires-python = ">=3.12,<3.13"`), managed by [uv](https://docs.astral.sh/uv/).

```bash
uv sync                       # runtime + dev dependencies
uv run playwright install chromium
cp .env.example .env          # blank secrets are fine for local work
make seed                     # 52 exercises, 11 workouts, two demo profiles
make dev                      # http://127.0.0.1:8090
```

No secrets are needed to develop. With `VITALFORGE_TOKEN` and `OMNIROUTE_KEY` blank, sessions
store locally and AI generation is disabled; everything else works. For the two integrations
without credentials, set `CADENCE_VITALFORGE_MODE=mock` and `CADENCE_OMNIROUTE_MODE=mock`
(both refused when `CADENCE_ENV=prod`).

## Commands

<!-- AUTO-GENERATED from Makefile -->

| Command | Description |
|---------|-------------|
| `make dev` | Run the app with reload on `$(CADENCE_HOST):$(CADENCE_PORT)` |
| `make test` | Unit + API tests with coverage |
| `make lint` | The CI gate |
| `make fmt` | Format and autofix in place |
| `make seed` | Load `library/` into the database and build a block per profile (marks setup done) |
| `make seed-fresh` | The same, left un-set-up so the front door opens on `/setup` (D-091) |
| `make deploy` | rsync the working tree to VM-201 and bring the stack up (see `docs/deploy.md`) |
| `make dev-docker` | Build and run the container locally on `$(CADENCE_PORT)` with podman-compose |
| `make smoke` | Run the smoke test against a running instance (`make smoke BASE=https://cadence.grepon.cc`) |
| `make perf` | Fail if `/today` breaks the D-025 page-weight budget |
| `make screenshots` | Write `docs/screenshots/` from a throwaway seeded database |
| `make backup` | Snapshot `data/cadence.db` into `data/backups/`, keeping the newest 30 |

`make e2e` runs the Playwright suite (used by CI alongside `make test`).

<!-- /AUTO-GENERATED -->

## Environment

<!-- AUTO-GENERATED from .env.example -->

| Variable | Required | Description | Example |
|----------|----------|-------------|---------|
| `CADENCE_ENV` | Yes | `dev` \| `test` \| `prod`. `prod` refuses any mocked integration at startup (D-139) | `prod` |
| `CADENCE_DB_PATH` | No | SQLite file, relative to the app root | `data/cadence.db` |
| `CADENCE_HOST` | No | Bind address inside the process | `0.0.0.0` |
| `CADENCE_PORT` | No | Port (8090; 8000 is taken on JD's workstation, D-036) | `8090` |
| `CADENCE_BIND_ADDR` | No | Host interface Compose publishes on. Set to the Tailscale IP in production (D-205) | `100.74.76.39` |
| `CADENCE_VITALFORGE_MODE` | No | `live` \| `mock`. `mock` short-circuits the VitalForge client | `live` |
| `CADENCE_PERIODIC_SYNC` | No | The in-process 5-minute drain and metrics refresh; `0` disables (D-140) | `1` |
| `TZ` | Yes | Drives display **and** the local wall clock Garmin files an activity under | `America/New_York` |
| `VITALFORGE_WEIGHT_URL` | Yes | Weight service — hosts `POST /api/activity` | `http://192.168.1.21:8085` |
| `VITALFORGE_DASHBOARD_URL` | Yes | Dashboard — hosts metrics and readiness | `http://192.168.1.21:8086` |
| `VITALFORGE_TOKEN` | No | Bearer token, minted at `/auth/account` → API Tokens. Blank = store locally, queue sync | *(secret)* |
| `VITALFORGE_PERSON_ME` | No | Adult's person **path slug** in VitalForge | `bash6632` |
| `VITALFORGE_PERSON_SON` | No | Youth's slug. Blank is valid — he degrades gracefully | |
| `CADENCE_OMNIROUTE_MODE` | No | `live` \| `mock`. `mock` returns a canned workout so smoke needs no key | `live` |
| `OMNIROUTE_URL` | No | OpenAI-compatible gateway base URL | `https://llm.grepon.cc/v1` |
| `OMNIROUTE_KEY` | No | Gateway key. Blank disables `/api/generate` only | *(secret)* |
| `OMNIROUTE_MODEL_GENERATE` | No | Model for workout generation | `code-plan` |
| `OMNIROUTE_MODEL_SUMMARY` | No | Model for summaries | `cheap-think` |

Secrets live only in `.env` (git-ignored, mode 600) and are `SecretStr`, redacted by value and
by shape from every log, error and response. Never commit one, and never echo one into a doc.

**Changing a secret on a running container needs `docker compose up -d`, not `restart`** —
`restart` does not re-read `env_file` and fails silently (D-258).

<!-- /AUTO-GENERATED -->

## Tests

```bash
make test            # pytest: unit + API, coverage gate 80% on cadence/
make e2e             # Playwright at 390×844 (and 1024×768 for tablet cases)
make lint            # ruff check + ruff format --check
```

- Unit and API tests are `tests/test_*.py`; browser tests are `tests/e2e/test_*.py`.
- Fixtures are in `tests/conftest.py` (temp DB per test, `httpx.AsyncClient` over
  `ASGITransport`) and `tests/e2e/conftest.py` (a real uvicorn on a free port).
- **Both integrations are always mocked** — `respx` for HTTP, `mock` mode for the clients. A
  test must never reach the real VitalForge or OmniRoute.
- New behaviour needs a test that **fails without the change**. Several bugs in this codebase
  were found by tests that passed vacuously; if a guard cannot be shown to fail, it is not a
  guard. Mutation-check anything security- or youth-rule-shaped.

## Style

- ruff for lint and format (line length and rules in `pyproject.toml`); `make fmt` fixes most.
- Type annotations on function signatures. Prefer immutable data — build new objects rather
  than mutating loaded rows.
- Files ≤ 400 lines as guidance, 800 hard. Functions ≤ 50 lines. Nesting ≤ 4.
- Handle errors explicitly; never swallow one without a log line.
- pre-commit runs ruff, ruff-format, end-of-file-fixer, trailing-whitespace and check-yaml:
  `uv run pre-commit install`.

## Decisions

Every non-obvious choice is an entry in `docs/DECISIONS.md`, numbered `D-NNN`, with the
reasoning and how to change it. If you make a call a reader might otherwise "fix" by accident,
add one. Code and tests cite these numbers; keep them accurate when you renumber.

## Pull request checklist

- [ ] `make lint`, `make test`, `make e2e` green; coverage ≥ 80%
- [ ] New behaviour has a test that fails without the change
- [ ] Youth-facing changes: no weight, body-fat, muscle or appearance text on a youth screen
      (D-027), and no path that exceeds the band caps
- [ ] Untrusted input (import, generate) still goes through the validator, with no second check
- [ ] `docs/DECISIONS.md` updated for anything non-obvious; `CHANGELOG.md` for user-visible work
- [ ] No secret in the diff, the logs, or a doc

## Where things are

`docs/CODEMAPS/` is the token-lean map: `architecture.md` for boundaries, `backend.md` for
routes, `data.md` for tables, `frontend.md` for templates and JS, `dependencies.md` for
external services. `docs/deploy.md` is the operational runbook. `docs/exercise-principles.md`
is the domain spec the library and validator implement.
