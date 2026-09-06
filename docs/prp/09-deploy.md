# PRP-09 — Deploy

## 1. Goal

Get Cadence running on VM-201 (192.168.1.21) as a container behind Nginx Proxy Manager at `https://cadence.grepon.cc`, reachable over Tailscale, with a nightly SQLite backup and a smoke test that proves a real session survives the round trip. One `make deploy` from the workstation redeploys. This PRP owns packaging and operations only; it changes no application behaviour.

## 2. Scope

**In**

- `Dockerfile`, `.dockerignore`, `docker-compose.yml`, `docker-compose.prod.yml`.
- `scripts/backup.sh`, `scripts/smoke.sh`, `scripts/deploy.sh`.
- `Makefile` targets `deploy`, `dev-docker`, `smoke`, `backup`.
- Nginx Proxy Manager and Tailscale runbook in `docs/deploy.md`.

**Out**

| Not this PRP | Where |
|---|---|
| Any change to `cadence/` application code | its owning PRP |
| An auth middleware | not built (D-009); §6.3 documents how to add one |
| CI (GitHub Actions) | PRP-00 |
| `HANDOFF.md`, screenshots | PRP-10 |
| VitalForge's own deployment | it is already running; do not touch its compose file |

**Deployment facts that are operator knowledge, not repo facts.** VM-201, Nginx Proxy Manager, Tailscale and `*.grepon.cc` appear nowhere in the VitalForge tree; its sample nginx config uses `yourdomain.com` placeholders (contract §5.8). Everything in §6 comes from the brief's "Known environment" block and must be re-confirmed by JD on first deploy, not assumed by a later reader.

## 3. Data model

No tables. This PRP owns the **lifecycle of the database file**.

| Path (in container) | Host path | Contents |
|---|---|---|
| `/app/data/cadence.db` | `./data/cadence.db` | the SQLite database |
| `/app/data/cadence.db-wal`, `-shm` | same | WAL sidecars, present whenever the app runs |
| `/app/data/backups/` | `./data/backups/` | `cadence-YYYYmmdd-HHMM.db`, 30 kept |

> **WAL means `cp` is not a backup.** PRP-00 enables `PRAGMA journal_mode=WAL`, so the `.db` file alone can be missing the newest committed transactions. Every backup goes through `sqlite3 "$DB" ".backup '$OUT'"`, which takes a consistent snapshot of a live database. Do not `cp`, do not `tar` the live file, and do not back up while holding the app stopped as a workaround.

`data/` is bind-mounted, not a named volume, so a backup and a restore are ordinary file operations on the host. `.gitignore` must already cover `data/` (PRP-00).

## 4. Interfaces

### 4.1 Ports and hostnames

| Layer | Value |
|---|---|
| Container listen | `0.0.0.0:8000` (uvicorn) |
| Host publish | `8090` → container `8000` |
| VM | VM-201, `192.168.1.21` |
| Public name | `cadence.grepon.cc` → `192.168.1.21:8090` via Nginx Proxy Manager |
| Reachability | Tailscale only; no port forward from the internet |

### 4.2 Container healthcheck

```dockerfile
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import json,urllib.request; \
d=json.load(urllib.request.urlopen('http://localhost:8000/api/health',timeout=4)); \
raise SystemExit(0 if d.get('ok') else 1)"
```

Follows VitalForge's precedent of putting the healthcheck in the Dockerfile rather than the compose file (contract §1.7). It parses `ok`, not just the HTTP status: `/api/health` returns **200 with `ok: false`** when the database check fails (PRP-00 §6), so a status-only check would call a broken app healthy.

> `/api/health` is a static config read and makes **no** network call to VitalForge (PRP-00 §6, PRP-06 §5.7). That is why polling it every 30 s is safe. If a future change makes it probe VitalForge, this healthcheck turns a VitalForge outage into a Cadence restart loop.

## 5. Implementation notes

### 5.1 `Dockerfile`

Two stages, `python:3.12-slim` for both. Python is pinned to 3.12 (D-006).

```
FROM python:3.12-slim AS builder
  COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv   # pin a tag, not :latest, in the committed file
  WORKDIR /app
  COPY pyproject.toml uv.lock .python-version ./
  RUN uv sync --frozen --no-dev --no-install-project
  COPY cadence/ ./cadence/
  COPY library/ ./library/
  RUN uv sync --frozen --no-dev

FROM python:3.12-slim
  RUN useradd --system --uid 10001 --create-home cadence
  WORKDIR /app
  COPY --from=builder --chown=cadence:cadence /app /app
  ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
      CADENCE_DB_PATH=/app/data/cadence.db
  RUN mkdir -p /app/data && chown cadence:cadence /app/data
  USER cadence
  EXPOSE 8000
  HEALTHCHECK ...
  CMD ["uvicorn", "cadence.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

Gotchas:

- **Non-root plus a bind mount is the classic first-deploy failure.** The host `./data` directory is created by the compose invocation as root and uid 10001 cannot write to it, so `init_db()` fails with `unable to open database file`. Fix on the host, once: `mkdir -p data && sudo chown -R 10001:10001 data`. `scripts/deploy.sh` does this before `up`. VitalForge solves the same problem with an entrypoint that `chown`s `/app/data` as root before dropping privileges (contract §1.7) — that alternative is documented in `docs/deploy.md` but not used, because a bind mount the operator owns is easier to back up.
- `--frozen` requires `uv.lock` to be committed. It is.
- `.dockerignore`: `.git`, `.venv`, `data`, `tests`, `docs`, `*.md`, `.env`, `__pycache__`, `.ruff_cache`, `.pytest_cache`. **`.env` in `.dockerignore` is a hard requirement** — secrets arrive via `env_file` at runtime, never baked into a layer.
- No `--reload`, no `--workers` above 1. A second worker would give each its own in-process sync drain task and its own periodic metrics refresh, double-POSTing every job. If workers are ever needed, the drain must move out of the app first.

### 5.2 `docker-compose.yml`

```yaml
services:
  cadence:
    build: .
    image: cadence:local
    container_name: cadence
    restart: unless-stopped
    ports: ["8090:8000"]
    env_file: .env
    environment:
      TZ: ${TZ:-Europe/Paris}
    volumes: ["./data:/app/data"]
```

- `env_file: .env`, matching VitalForge's single-shared-`.env` shape (contract §1.7). `.env` is git-ignored; `.env.example` is committed with blank tokens.
- No `healthcheck:` stanza — it lives in the Dockerfile (§4.2).
- No published port other than 8090; NPM reaches it over the VM's LAN address.

`docker-compose.prod.yml` overlay, used on VM-201:

```yaml
services:
  cadence:
    build: null            # or omit and rely on a pulled image once one is published
    logging:
      driver: json-file
      options: {max-size: "10m", max-file: "3"}
    deploy:
      resources:
        limits: {memory: 512M}
```

Log rotation is the point of the overlay: an unrotated `json-file` log will fill VM-201's disk and take VitalForge down with it, since they share the host. Everything else stays in the base file.

**Runtimes differ by machine (D-004).** The workstation aliases `docker` → `podman` and uses `podman-compose`; VM-201 runs real Docker Compose. The compose files stay Docker-compatible so both work. `make dev-docker` uses `podman-compose up --build` locally; `make deploy` uses `docker compose` on the VM.

### 5.3 `scripts/backup.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail
DB="${CADENCE_DB_PATH:-/app/data/cadence.db}"
OUT_DIR="$(dirname "$DB")/backups"
KEEP="${CADENCE_BACKUP_KEEP:-30}"
mkdir -p "$OUT_DIR"
OUT="$OUT_DIR/cadence-$(date +%Y%m%d-%H%M).db"
sqlite3 "$DB" ".backup '$OUT'"
sqlite3 "$OUT" "PRAGMA integrity_check;" | grep -qx ok
ls -1t "$OUT_DIR"/cadence-*.db | tail -n +$((KEEP + 1)) | xargs -r rm --
```

- `set -euo pipefail` so a failed `.backup` never silently prunes good backups.
- The `integrity_check` runs **on the copy**: a backup that cannot be opened is worse than none, and this catches it while the original is still there.
- Pruning is `ls -1t | tail -n +31`, newest-first, so exactly 30 survive. `xargs -r` handles the empty case.
- Runs on the **host**, against `./data/cadence.db`, so it needs `sqlite3` on VM-201 (`apt install sqlite3`) and does not require entering the container.

Cron line, documented in `docs/deploy.md` and installed by hand:

```cron
17 3 * * * cd /opt/cadence && CADENCE_DB_PATH=/opt/cadence/data/cadence.db ./scripts/backup.sh >> /var/log/cadence-backup.log 2>&1
```

03:17 rather than 03:00 to avoid colliding with whatever else runs on the hour.

**Restore**, in `docs/deploy.md`: stop the container, move the live `.db`, `.db-wal` and `.db-shm` aside (all three — a stale WAL against a restored DB is corruption), copy the chosen backup to `data/cadence.db`, `chown 10001:10001`, start.

### 5.4 `make deploy`

Two mechanisms exist; **`rsync` is the default.**

| | rsync (default) | `git pull` on the VM |
|---|---|---|
| How | rsync the working tree to `vm-201:/opt/cadence`, then `docker compose up -d --build` over SSH | SSH in, `git pull`, `docker compose up -d --build` |
| Pro | deploys exactly what is on the workstation, including an unpushed branch | the VM's state is a git ref, trivially auditable |
| Con | the VM's state is not a commit | needs the commit pushed first (D-002 pushes `main` after each PRP, so this is usually fine) |

Default is rsync because PRP-05's VitalForge branch is deliberately never pushed (D-005) and JD will want to try things before pushing. `docs/deploy.md` documents both; `scripts/deploy.sh` takes `--mode rsync|git`.

```bash
rsync -az --delete \
  --exclude '.git' --exclude '.venv' --exclude 'data' --exclude '.env' \
  --exclude '__pycache__' --exclude '.pytest_cache' --exclude '.ruff_cache' \
  ./ vm-201:/opt/cadence/
ssh vm-201 'cd /opt/cadence && mkdir -p data && sudo chown -R 10001:10001 data \
  && docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build'
ssh vm-201 'cd /opt/cadence && docker compose ps && docker compose logs --tail 30 cadence'
```

- **`--exclude '.env'` is load-bearing.** The VM's `.env` holds the real `VITALFORGE_TOKEN` and `OMNIROUTE_KEY`; `--delete` without that exclude wipes them and Cadence comes back with sync `skipped`. The workstation's `.env` must never be pushed either.
- `--exclude 'data'` for the same reason, one level worse: `--delete` would remove the database.
- SSH host alias `vm-201` comes from `~/.ssh/config`. `make deploy` fails early with a clear message if `ssh -o BatchMode=yes vm-201 true` does not succeed, rather than half-deploying.
- First deploy only: `ssh vm-201 'mkdir -p /opt/cadence'` and copy `.env` by hand from `.env.example`. Never rsync a `.env`.

### 5.5 `scripts/smoke.sh`

Runs against a **running** instance, locally or on the VM, with `CADENCE_VITALFORGE_MODE=mock` so no real VitalForge or Garmin write happens. `BASE` defaults to `http://localhost:8090`.

| Step | Check |
|---|---|
| 1 | `GET /api/health` → HTTP 200 **and** `.ok == true` **and** `.data.db == "ok"` |
| 2 | `GET /today?profile=me` → 200, body contains at least one checklist row |
| 3 | `GET /api/today?profile=me` → capture `session_id` and the first row `position` |
| 4 | `POST /api/sessions/{id}/rows/{position}` with `{"done": true}` → 200, `.data.done == true` |
| 5 | repeat step 4 → still 200 and still `done` (idempotency, the offline replay path) |
| 6 | `POST /api/sessions/{id}/done` → 200 |
| 7 | `GET /done/{id}` → 200 and the sync line is one of the five PRP-06 §4.3 strings |
| 8 | `GET /api/metrics?profile=me` → 200, envelope intact |

`set -euo pipefail`, `jq` for parsing, non-zero exit on the first failure with the failing step number and the response body. `make smoke` runs it against localhost; `make smoke BASE=https://cadence.grepon.cc` against the VM.

> Step 5 exists because a tick is the one operation the PWA replays from IndexedDB after an offline spell; a non-idempotent tick corrupts the count silently.

### 5.6 `docs/deploy.md`

Sections, in order: prerequisites · first deploy · routine deploy · NPM setup · Tailscale · backups and restore · smoke test · troubleshooting · rollback.

Troubleshooting entries that must be present, because each has already been reasoned about:

| Symptom | Cause |
|---|---|
| `unable to open database file` | `./data` not owned by uid 10001 (§5.1) |
| `sync failed`, `last_error` names the `cadence/activity-endpoint` branch | PRP-05 is not deployed on VitalForge; expected until JD deploys it (PRP-06 §5.5) |
| Done shows `stored locally — VitalForge not configured` | blank `VITALFORGE_TOKEN` in the VM's `.env`, most likely rsynced over |
| container restart loop | healthcheck failing; check `docker inspect --format '{{json .State.Health}}' cadence` |
| 502 from NPM | container down, or NPM pointed at the container port 8000 instead of the host port 8090 |
| disk full on VM-201 | unrotated logs; the prod overlay exists to prevent this |

**Rollback:** `git checkout <previous tag>` (tags are `prp-NN`, brief §PRP set) then `make deploy`; the database is forward-compatible within a schema version, and PRP-00's version check fails loudly rather than corrupting if it is not.

## 6. Runbook content

### 6.1 Nginx Proxy Manager — `cadence.grepon.cc`

New Proxy Host:

| Field | Value |
|---|---|
| Domain Names | `cadence.grepon.cc` |
| Scheme | `http` |
| Forward Hostname / IP | `192.168.1.21` |
| Forward Port | `8090` |
| Cache Assets | off — the service worker already caches, and a proxy cache serves a stale Today |
| Block Common Exploits | **on** |
| Websockets Support | **off** — Cadence uses HTMX over plain HTTP, no websockets |
| SSL | request a certificate, Force SSL on, HTTP/2 on |
| Access List | optional, see §6.2 |

Advanced tab, one directive:

```nginx
client_max_body_size 5m;
```

`/api/import` accepts a pasted or uploaded YAML/JSON workout (PRP-08). NPM's default body limit is 1 MB and a larger paste fails as a 413 that looks like an app bug. This follows VitalForge's own precedent of raising the limit only for its import path (`nginx/nginx.conf:39-46`). 5 MB, not 25 MB — a workout document is kilobytes, and the wider limit is a needless upload surface.

### 6.2 Tailscale and the access question

**Cadence has no login of its own (D-009).** A login screen makes Today slower for a kid on the floor, and the brief's security boundary is the homelab. That decision has a consequence: anything that can reach `cadence.grepon.cc` can read both profiles and write sessions.

Recommended guard, in order of preference:

1. **Do not expose port 8090 beyond the LAN, and reach the host only over Tailscale.** This is the baseline and is already true.
2. **Add an NPM Access List** on the proxy host, allowing the Tailscale CGNAT range `100.64.0.0/10` and denying the rest. This is the recommended addition and costs one form. Verify afterwards that a request from a non-Tailscale LAN address gets a 403, because an allow-list that never denies anything is not a guard.
3. Only if Cadence ever needs to be reachable from outside Tailscale: add the `CADENCE_ACCESS_TOKEN` middleware sketched in §6.3. **Do not build it now** — D-009 says the app has no auth and PRP-09 documents how, which is what this section is.

> Note that the access list applies at NPM. A client on the LAN hitting `192.168.1.21:8090` directly bypasses it entirely. If that matters, bind the published port to the Tailscale interface rather than `0.0.0.0`.

### 6.3 If auth is ever needed (documentation only, not built)

A single middleware reading `CADENCE_ACCESS_TOKEN` from `.env`, comparing with `hmac.compare_digest` against either an `Authorization: Bearer` header or a long-lived cookie set by a one-field `/unlock` page; skip `/api/health` and `/static/*`. Blank token means disabled, so the default stays D-009's no-auth. Roughly 40 lines. Do not add it in this PRP.

## 7. Acceptance tests

Container and script behaviour, run in CI where possible and by hand where not.

1. `tests/test_deploy_files.py::test_dockerfile_uses_python_312` — the base image line matches `python:3.12-slim`, matching `.python-version`.
2. `::test_dockerfile_runs_as_non_root` — a `USER cadence` line appears after the last `RUN`, and no `USER root` follows it. **Negative.**
3. `::test_dockerfile_has_healthcheck_hitting_api_health` — the `HEALTHCHECK` line contains `/api/health`.
4. `::test_healthcheck_checks_ok_field_not_just_status` — the healthcheck command references `ok`. **Guards the 200-with-`ok:false` case.**
5. `::test_dockerignore_excludes_env_and_data` — both `.env` and `data` are listed. **Negative, and the one that prevents a baked-in secret.**
6. `::test_compose_maps_8090_to_8000` — the `ports` entry is exactly `8090:8000`.
7. `::test_compose_uses_env_file_not_inline_secrets` — `env_file: .env` present, and no key matching `TOKEN|KEY|PASSWORD` appears with a value in either compose file. **Negative.**
8. `::test_compose_restart_policy` — `restart: unless-stopped`.
9. `::test_compose_binds_data_volume` — `./data:/app/data`.
10. `::test_compose_has_single_worker` — no `--workers` flag with a value above 1 in either file. **Guards double-draining the sync queue. Negative.**
11. `::test_prod_overlay_rotates_logs` — `max-size` and `max-file` present.
12. `tests/test_backup_script.py::test_backup_creates_timestamped_copy` — run `scripts/backup.sh` against a temp SQLite DB in WAL mode with an uncommitted-to-main-file row; the backup contains that row. **This is the test that proves `cp` would have been wrong.**
13. `::test_backup_passes_integrity_check` — the produced file passes `PRAGMA integrity_check`.
14. `::test_backup_keeps_only_30` — create 35 fake backups, run, exactly 30 remain and they are the 30 newest.
15. `::test_backup_prune_does_not_run_if_backup_failed` — point at a nonexistent DB; the script exits non-zero and deletes nothing. **Negative, and the one that protects the backup history.**
16. `::test_backup_handles_empty_backup_dir` — first-ever run does not fail on `xargs`. **Negative.**
17. `tests/test_smoke_script.py::test_smoke_passes_against_mock_mode` — boot the app with `CADENCE_VITALFORGE_MODE=mock` on an ephemeral port, run `scripts/smoke.sh`, exit 0.
18. `::test_smoke_fails_when_health_not_ok` — patch the DB path to an unwritable location; the script exits non-zero naming step 1. **Negative.**
19. `::test_smoke_makes_no_real_vitalforge_call` — a `respx`/proxy assertion that no request leaves for `*.grepon.cc`. **Negative.**
20. `::test_smoke_tick_is_idempotent` — step 5's double-tick assertion is present and passes.
21. `tests/test_deploy_script.py::test_rsync_excludes_env_and_data` — `scripts/deploy.sh` contains `--exclude '.env'` and `--exclude 'data'` alongside `--delete`. **The single most destructive omission possible in this PRP. Negative.**
22. `::test_deploy_checks_ssh_before_acting` — a `BatchMode` reachability check precedes any rsync.
23. `::test_deploy_supports_both_modes` — `--mode rsync` and `--mode git` are both handled; an unknown mode exits non-zero. **Negative.**
24. `tests/test_docs_deploy.py::test_deploy_doc_documents_npm_body_size` — `docs/deploy.md` mentions `client_max_body_size`.
25. `::test_deploy_doc_documents_tailscale_cidr` — contains `100.64.0.0/10`.
26. `::test_deploy_doc_documents_restore_removes_wal` — the restore section mentions `-wal` and `-shm`. **The step everyone forgets.**
27. `::test_deploy_doc_documents_data_ownership` — mentions `10001`.
28. `::test_no_real_secrets_in_repo` — `grep -rn` over the tree finds no value assigned to `VITALFORGE_TOKEN` or `OMNIROUTE_KEY` outside `.env.example`, where both are blank. **Negative.**

Manual, run once by JD on the first deploy and recorded in `docs/deploy.md`:

- [ ] `make deploy` from a clean workstation checkout succeeds end to end.
- [ ] `https://cadence.grepon.cc/today` loads over Tailscale, and does **not** load from a non-Tailscale address once the access list is on.
- [ ] `docker compose ps` shows `healthy`, not just `running`.
- [ ] `./scripts/backup.sh` produces a file and the cron line is installed.
- [ ] `make smoke BASE=https://cadence.grepon.cc` exits 0.

## 8. Devil's-advocate risks

1. **`rsync --delete` without `--exclude '.env' --exclude 'data'` destroys the production database and both secrets in one command.** → Test 21, and the excludes are in `scripts/deploy.sh`, never typed by hand.
2. **A `.env` baked into a Docker layer** ships the VitalForge token in the image. → `.dockerignore` plus test 5; the image is never pushed to a registry in this PRP.
3. **`cp`-based backups silently lose the newest transactions** under WAL, and nobody notices until a restore. → `sqlite3 .backup`, plus test 12 which writes a row that lives only in the WAL.
4. **A failed backup prunes the good ones**, leaving 29 backups and no new one, repeated nightly until the history is gone. → `set -euo pipefail`, test 15.
5. **Restore leaves a stale `-wal` next to a restored `.db`** and SQLite happily replays it over the restored data. → §5.3 and test 26.
6. **Non-root plus a root-owned bind mount** makes the first deploy fail in a way that looks like a code bug. → `chown` in `scripts/deploy.sh` and a troubleshooting row.
7. **A second uvicorn worker** gives each worker its own sync drain and metrics refresh, double-POSTing every session and doubling Garmin traffic against a credential with no rate-limit backoff (contract §5.9). → Single worker, test 10, and a comment in the Dockerfile saying why.
8. **The healthcheck passes on a broken app** because `/api/health` returns 200 with `ok: false`. → Test 4.
9. **A future live VitalForge probe in `/api/health`** turns a VitalForge outage into a 30-second Cadence restart loop. → Called out in §4.2 and forbidden in PRP-06 §5.7.
10. **NPM's 1 MB default body limit** makes `/api/import` fail as a 413 that reads like an app bug. → §6.1.
11. **NPM asset caching serves a stale Today** after a deploy, on top of the service worker's own cache. → Cache Assets off.
12. **An access list that allows everything** looks configured and guards nothing. → §6.2 requires verifying a denial, not just an allow.
13. **Direct `192.168.1.21:8090` bypasses NPM** and therefore the access list entirely. → Stated plainly in §6.2 rather than left as a false sense of security.
14. **Unrotated container logs fill VM-201's disk** and take VitalForge down with Cadence, since they share the host. → Prod overlay, test 11.
15. **`podman-compose` and `docker compose` diverge** on some directive and the VM deploy fails in a way the workstation never reproduces. → Compose files stay to the Docker-compatible subset (D-004); no podman-only keys.
16. **A rollback to an older tag meets a newer database.** → PRP-00's `schema_version` check raises rather than corrupting; the rollback section says to restore a matching backup.

## 9. Done when

- [ ] `podman-compose up --build` (workstation, via `make dev-docker`) serves `/api/health` with `ok: true` on `localhost:8090`.
- [ ] `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build` on VM-201 reaches `healthy`.
- [ ] `https://cadence.grepon.cc/today` loads over Tailscale through NPM with a valid certificate.
- [ ] The container runs as uid 10001 (`docker exec cadence id -u` → `10001`).
- [ ] `make deploy` works twice in a row and the second run preserves `data/` and `.env` on the VM.
- [ ] `scripts/backup.sh` produces a file that passes `integrity_check`; the cron line is installed on VM-201.
- [ ] `make smoke` exits 0 locally and against the VM.
- [ ] `docs/deploy.md` covers all nine sections in §5.6.
- [ ] All 28 automated acceptance tests pass; the five manual items are ticked and dated in `docs/deploy.md`.
- [ ] No secret value exists anywhere in the repo (test 28), and `.env` is absent from the deployed image.
