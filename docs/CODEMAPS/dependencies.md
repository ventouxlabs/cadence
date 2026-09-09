<!-- Generated: 2026-09-08 | Runtime deps: 9 | Token estimate: ~600 -->

# Dependencies

## Runtime (`pyproject.toml`, Python 3.12 only)

```
fastapi 0.115      uvicorn[standard] 0.32     sqlmodel 0.0.22
pydantic 2.9       pydantic-settings 2.6      jinja2 3.1
python-multipart   pyyaml 6                   httpx 0.27-0.28
```

No ORM beyond SQLModel, no Celery/Redis, no frontend toolchain. HTMX is vendored as a static
file, pinned with a sha256 in its header comment.

## Dev

`pytest`, `pytest-asyncio`, `pytest-cov`, `respx` (HTTP mocking), `playwright` +
`pytest-playwright`, `ruff`, `pre-commit`.

## External services

| Service | Used for | Failure mode |
|---|---|---|
| **VitalForge** weight `:8085` | `POST /p/{slug}/api/activity` write-back | Queued with bounded backoff; Done says "will sync" |
| **VitalForge** dashboard `:8086` | `weight/recent`, `metrics/{name}`, `readiness` | Last good cache served, marked `stale` |
| **OmniRoute** `llm.grepon.cc/v1` | `/api/generate` only (`code-plan`) | Generation disabled; import and everything else unaffected |
| **Garmin Connect** | Reached *only* through VitalForge, never directly | Cadence never holds Garmin credentials |

Both integrations have a `mock` mode for smoke tests, refused when `CADENCE_ENV=prod`.

## Contract notes

- Auth is `Authorization: Bearer <VITALFORGE_TOKEN>`; tokens are **per-user, not per-person**.
- People are addressed by **path slug** (`/p/{slug}/...`), never a query parameter.
- `POST /api/activity` is idempotent on `session_id`; the endpoint lives on the unmerged
  `cadence/activity-endpoint` branch of the VitalForge repo. Until it is deployed, write-back
  queues and reports it — reads are unaffected.
- One Garmin credential exists, belonging to the adult. A youth session is only pushed with an
  explicit `garmin_target: "credential_person"`, otherwise 409 (D-015).

## Config

All via `.env` (`.env.example` committed with blank secrets). `CADENCE_ENV` dev|test|prod;
prod refuses any `*_MODE=mock`. Secrets are `SecretStr`, redacted by value and by shape from
every log, error and response.
