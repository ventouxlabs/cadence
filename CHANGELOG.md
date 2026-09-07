# Changelog

All notable changes to Cadence. Format follows Keep a Changelog; one entry per PRP.

## [Unreleased]

### prp-09 — deploy: image, compose, backups, runbook (2026-09-07)
- `Dockerfile`: two stages on `python:3.12-slim`, uv pinned at `ghcr.io/astral-sh/uv:0.11`, `uv sync --frozen --no-dev`, non-root uid 10001, `/app/data` volume, `CADENCE_ENV=prod` in the image, and a `HEALTHCHECK` that parses `ok` rather than trusting a 200 — `/api/health` answers 200 with `ok: false` when the database check fails.
- One uvicorn worker, no `--reload`: a second worker would run its own copy of PRP-06's sync drain and double-POST every session to VitalForge and Garmin.
- `docker-compose.yml` (host 8090 → container 8000, `env_file: .env`, `restart: unless-stopped`, `./data:/app/data`, `TZ` passthrough) and a `docker-compose.prod.yml` overlay whose job is log rotation, so VM-201's disk cannot fill and take VitalForge down with Cadence.
- `docker-compose.dev.yml`, layered by `make dev-docker`, sets `CADENCE_ENV=dev` and `CADENCE_VITALFORGE_MODE=mock`. The base file states production plainly and leaves the mode to the VM's `.env`: a compose `environment:` entry overrides `env_file:` and would silently ignore what the operator wrote there.
- `.dockerignore` excludes `.env` and `data/`, so no token is ever baked into a layer and no database into an image.
- `scripts/backup.sh`: `sqlite3 .backup` (or the identical Python API where there is no CLI), integrity-checked on the copy, newest 30 kept, and a failed run prunes nothing. `cp` is not a backup under WAL.
- `scripts/deploy.sh`: rsync by default with `--exclude '.env' --exclude 'data'` beside `--delete`, `--mode git` as the alternative, an ssh reachability probe before anything is touched, and the bind-mount chown to uid 10001.
- `scripts/smoke.sh`: eight steps through a real session, including the double-tick the PWA replays offline; it refuses to run against a server that is not in `mock` mode. Step 7 requires the Done screen to read `synced ✓` **and** the fake to have recorded exactly one activity for that session; step 8 asserts `GET /api/metrics` answers 200 with `ok: true`.
- `GET /api/_mock/activities` returns the fake's recorded `{session_id, slug}` pairs and nothing else, mounted only at `CADENCE_VITALFORGE_MODE=mock` outside prod. It is what lets the smoke test tell one write-back from none or two, which the Done line cannot (D-169).
- `Makefile`: `deploy`, `dev-docker` (podman on the workstation), `smoke`, `backup`.
- `docs/deploy.md`: the twelve-section VM-201 runbook — Nginx Proxy Manager at `cadence.grepon.cc`, the Tailscale access list on `100.64.0.0/10`, backup cron, restore including the `-wal` and `-shm` files everyone forgets, troubleshooting, rollback, and the phone install steps (D-117).
- CI builds the image without pushing it, and asserts uid 10001 and no `.env` in `/app`.
- 34 acceptance tests across `tests/test_deploy_files.py`, `test_backup_script.py`, `test_smoke_script.py`, `test_deploy_script.py`, `test_docs_deploy.py`, plus `test_mock_inspect.py` for the inspection route's mount guard.
- Post-review hardening: the deploy rsync excludes `docker-compose.dev.yml` so the mock overlay cannot reach the VM; `backup.sh` sets `umask 077`, making snapshots `0600` in a `0700` directory rather than world-readable on a shared host; the runtime image's code and venv are root-owned, leaving `/app/data` the only path the runtime uid can write; `.env.example` ships `CADENCE_ENV=prod` so an unedited copy on VM-201 fails safe; and the troubleshooting table's restart-loop row, which blamed the healthcheck, now says a restart loop means the process is exiting and adds the `unhealthy`-but-running case a restart policy does not cover.
- Second review round: `deploy.sh` validates `CADENCE_DEPLOY_HOST` and `CADENCE_DEPLOY_PATH` before either reaches `ssh` — the path is interpolated into a remote shell command and a host beginning `-` is an ssh *option* that runs locally — and puts `--` before every ssh and rsync target; `.gitignore` covers `.env*` with an explicit `!.env.example`; the published port became one variable-addressed entry, `${CADENCE_BIND_ADDR:-0.0.0.0}:8090:8000`, so binding to the Tailscale interface is an operator variable rather than an overlay that compose would append; `deploy.sh` chmods the backup directory it creates.
- Smoke step 7 is now mode-dependent: `mock` still requires `synced ✓`, while a live run also accepts `will sync` — what a 404 from the undeployed VitalForge endpoint actually renders — and `sync failed (retrying)`, but still fails on the terminal `sync failed` and on `stored locally — VitalForge not configured`.
- Review Lows: the troubleshooting table warns against a bare `docker inspect cadence`, which prints `Config.Env` and therefore both tokens in full — keep it `--format`-scoped; `backup.sh` refuses a database or backup directory beginning `-` or containing a quote, since `.backup '$OUT'` word-splits and `a' 'b` writes the snapshot outside the directory whose permissions protect it; the Dockerfile no longer claims the uv tag closes reproducibility, `0.11` being a minor tag that still moves; and D-167/D-168 now state the credential-shape threshold as 32 characters, matching the regex.
- Decisions D-160 to D-169, D-200 to D-209.
### prp-08 — import and AI generation (2026-09-06)
- `POST /api/import`: JSON text or a multipart file, one pipeline, `source = "import"`. Size (256 KB) is checked on `Content-Length` and again on the read bytes, before the parser; the handler takes a raw `Request` so a body model cannot parse ahead of the gate.
- One pipeline behind import and accept (`cadence/bibliotheque/import_service.py`), pure with respect to the filesystem: it takes `text: str`, never a path, and an upload's filename is read by nobody (D-010).
- Gates in order: size, raw-text content scan, `yaml.safe_load` (single document only), mapping, depth ≤ 6, ≤ 30 rows, string bounds, per-string content scan, unknown top-level keys dropped into `meta.ignored_keys`, inline `exercises:` through `validate_exercise`, id collision, then `validate_workout` with the target profile's real kind, band, equipment, `bodyweight_kg` and `has_overhead_anchor`.
- Untrusted-content rules (`cadence/bibliotheque/untrusted.py`): `http(s)://`, `{{`, `{%`, `{#`, `!!`, `<script`, `javascript:`, `data:text/html`, control characters, and YAML anchors/aliases at a node position — the last of which `safe_load` expands and is the billion-laughs vector. Proven compatible with every cue, name and file under `library/`.
- `POST /api/generate` and `POST /api/generate/accept` over OmniRoute (`cadence/ia/`): OpenAI-compatible completion, `temperature` 0.4, `stream: false`, connect 5 s / read 60 s, exactly one retry with the validator's findings appended. A preview is never stored and never cached server-side; accept re-runs every gate on the posted bytes (D-179).
- The prompt (`cadence/ia/prompts/generate.md`) interpolates a closed set of eleven placeholders and `render` refuses any other. It carries the age **band** and never the age, the name, the slug, the bodyweight, a metric, a session log or a token — asserted on the captured request body.
- Settings gains working Import and Generate cards in PRP-03's containers: textarea plus file input plus target profile, a read-only generated checklist with Accept and Discard, and both panels swapping in place on success and on failure (D-174).
- "Use for: Upper A / Lower A / …" points a stored workout at every untouched planned day of that type, rows shaped as the program engine shapes them so Today renders names and cues (D-173).
- `CADENCE_OMNIROUTE_MODE=mock` returns a canned workout with no key and no network, through every gate, for PRP-09's deploy smoke test (D-180).
- Missing key: `/api/generate` answers 503 `generation_unavailable`, the card renders one explanatory line, import still works, and neither the key nor its variable name appears in a body or a log line.
- 89 tests here — `test_import.py` (13), `test_import_hardening.py` (24), `test_generate.py` (22), `test_ia_prompt.py` (6), `test_settings_ai.py` (24) — plus 6 Playwright tests at 390×844; decisions D-170 to D-198.

### prp-03 — profiles, Setup, Settings and the youth wiring (2026-09-06)
- `cadence/profils/` services over PRP-01's tables: `get_settings` / `update_settings`, `get_profile` / `update_profile`, `apply_household` (one form, one transaction), `youth_ruleset_for`, `has_overhead_anchor`, `bodyweight_kg`, `refresh_age_bands` on boot.
- Setup gate restored: `GET /` sends an install to `/setup` until `setup_complete`, and a dependency on the HTML routers stops `/today` and `/settings` being deep-linked past it. `make seed` now marks demo data set up; `make seed --fresh` is the true first run (D-091).
- `GET|POST /setup` and `GET|POST /settings` over one shared form template, `POST /settings/weights/preview` for the live parse, plus the `import_section` / `generate_section` containers and the `#import-result` / `#generate-preview` ids PRP-08 renders into.
- `GET|PUT /api/settings` and `GET|PUT /api/profiles/{id}`: partial patches, unknown keys named in a 422, the four-id equipment whitelist with `bodyweight` not un-tickable, days 2–6, length 15/30/45, youth ages 3–19, and `kind` refused outright.
- Rebuild on a relevant change that keeps history: only unstarted `planned` sessions are replaced, finished ones and their `session` rows survive, the new work follows the last completed slot, and `program.start_date` is never touched (D-093).
- `display_unit` (kg/lb) as a ninth setting key, converted in one place — which fixed `format_load` labelling kilograms as pounds without converting them (D-090).
- Youth wiring end to end: band from age, strictest until set, `is_youth()` template guard, and no body-composition or appearance language on the son's screens.
- An age never changes which rules protect a profile: a youth profile past the band table uses the loosest youth band, and moving to the adult rules is an explicit, confirmed, reversible control plus `PUT /api/profiles/{id}/kind` (D-099).
- `make seed` and Settings share one rebuild path (D-110); a skipped day spends no slot and a rebuild never empties the queue (D-111); `vitalforge_person` mirrors VitalForge's own slug rule (D-112); a rebuild that cannot run answers 503 rather than a silent success (D-113).
- One error line per person-slug box rather than one for the section, and the env slugs `make seed` writes are checked like typed ones (D-114).
- Merged `main` after PRP-04 landed: History is behind the setup gate like Today, the top bar keeps both PRP-04's History link and PRP-03's setup-screen suppression, and acceptance test 22 is live against the real `/history`.
- Decisions D-090 to D-114. Numbering note: this PRP's original D-100 became D-110, because PRP-04 had already taken D-100 to D-107 in its own worktree.

### prp-02 — Today checklist, Done and the offline PWA (2026-09-06)
- `session` / `session_row` tables and `cadence/seance/` (today resolution, ticks, adjust, felt, done, derived completion, cached library reads); session created on first render, `started_at` on first tick.
- HTML UI in `cadence/web/`: `GET /` front door, `/today?profile=me|son|together`, HTMX partials for tick, adjust, felt and Done, `/done/{id}` three-line summary, plain-form fallback with JavaScript off.
- Together mode: two sessions sharing `together_group_id`, stacked on a phone and side by side from 768 px, one Done finalising both; a solo render detaches.
- JSON API: `GET /api/today`, idempotent `POST /api/sessions/{id}/rows/{position}` and `POST /api/sessions/{id}/done`, all on the standard envelope.
- PWA: `manifest.json`, `/sw.js` at the origin root (network-first for `/today*`, cache-first for `/static/`), SVG plus 192/512/maskable PNG icons, IndexedDB queue `cadence-queue` in `static/app.js`, per-exercise timer, vendored HTMX 2.0.10.
- `GZipMiddleware` (D-025); `/today` cold-loads at **24.6 KB gzipped** against a 60 KB budget.
- 1016 tests at 94% coverage plus 15 Playwright tests at 390x844; decisions D-070 to D-082.

### prp-01 — library and program engine (2026-09-06)
- `library/`: 52 exercises across eight pattern files, 11 workout templates, `progressions/default.yaml`, `assessments.yaml` — transcribed from `docs/exercise-principles.md` sections 9 and 10.
- `WorkoutTemplate` / `TemplateRow` layer (`cadence/bibliotheque/template.py`): roles, per-side rows, load rules, u10 substitutes, per-profile columns — none of which exist on PRP-00's concrete `Workout`.
- Loader (`cadence/bibliotheque/loader.py`): parses, links, compiles every template for every profile kind and band it serves, runs the PRP-00 validator on the result, and aborts the whole load on any problem.
- Program engine (`cadence/programme/`): age bands, `weights_available` parser and load ladder, week schemes with the deload, day-type rotation, youth substitution and clamping, `build_program` and `materialise_rows`.
- `profile` / `setting` / `program` / `planned_session` tables; `make seed` creates 52 exercises, 11 workouts, 2 profiles, 2 programs and 32 planned sessions, and is idempotent.
- Decisions D-050 to D-064.

### prp-00 — foundation (2026-09-06)
- uv project on Python 3.12, ruff, pytest, pre-commit config, GitHub Actions CI, Makefile (`dev test lint fmt e2e seed deploy`).
- `cadence/` package: `create_app()`, typed settings from `.env`, SQLite/SQLModel layer, `GET /api/health`, JSON envelope.
- Pydantic v2 library DSL (`Exercise`, `Workout`, `WorkoutRow`, `Progression`, `YouthRuleSet`, `AssessmentSpec`, equipment whitelist, age bands) with strict numerics and deep immutability.
- Validator (`validateur`) as the single gate for seed, import and generation: 14 rule families, youth caps derived from the exercise's `load_unit`, per-band exercise allowlist, assessment-day exemptions per row.
- 454 tests, 99% coverage, including adversarial youth-rule and equipment-whitelist bypass suites.

### Phase 0
- Repository initialised (AGPL-3.0), discovery docs started.

### prp-01 — library-program (2026-09-06)
- Seed library as data: 52 exercises (8 pattern files), 11 workouts, progressions, assessments, youth rules — transcribed from `docs/exercise-principles.md` §9/§10 and pinned cell-for-cell by tests.
- `WorkoutTemplate` layer compiled to concrete `Workout` rows; loader validates every document; `make seed` is idempotent, keeps done sessions, preserves `start_date`.
- Program engine: 4-week rolling plan, day templates, week schemes, days/week 2–6, session-length scaling, prelude first, week-4 deload, youth filtering, substitution with fallback + breadth-first bodyweight demotion, `weights_available` parsing (200 kg ceiling) and load rounding; compiled rows re-validated for the real profile before persisting.
- `profile`/`setting` tables and `age_band()`; two demo profiles (`me` adult, `son` youth, strictest band until age set).
- 945 tests, 94% coverage.

### prp-02 — today-checklist (2026-09-06)
- Today screen: profile switcher (Me / Son / Both), checklist rows with sets × reps or seconds, load, one-line cue, big tap targets, adjust in ≤ 2 taps, per-exercise timer (off by default), felt toggle, youth "Good enough — done" exit, Done → 3-line summary.
- Together mode: stacked on phone, side-by-side ≥ 768 px, shared `together_group_id`, one shared Done gated on every checklist, per-session youth exit (D-084).
- `session`/`session_row` tables and `cadence/seance/` services; idempotent JSON replay targets; finished-session guard on every write path; youth band rules never fail open (503).
- PWA: manifest, icons, service worker (network-first Today with cache fallback, precached offline Done page), IndexedDB queue for tick/adjust/felt/done with in-flight marking and 4xx parking; HTMX 2.0.10 vendored; GZip; cold `/today` 27.6 KB gzip of 60 KB.
- 1087 tests, 45 Playwright tests at 390×844 (and 1024×768), 94% coverage.

### prp-03 — profiles-settings (2026-09-07)
- First-run `/setup` and editable `/settings` (son's age, equipment locked to the whitelist with bodyweight always on, `weights_available` with live parse preview, days/week, session length, push son's sessions to Garmin (off; son has no Garmin, D-069), display unit, VitalForge person slugs); `/` gate on `setup_complete`; `POST /setup` not replayable.
- `GET/PUT /api/settings`, `GET/PUT /api/profiles/{id}`, explicit confirmed `PUT /api/profiles/{id}/kind`; youth at 18+ stays youth on the 14–17 band (D-099 revised).
- One rebuild path (`profils/rebuild.py`): done sessions kept, `start_date` preserved, unstarted sessions replaced, next block started when the plan is exhausted; a missing library refuses the rebuild (503) instead of leaving stale loads.
- Person slugs validated to VitalForge's rule; settings survive a corrupt key; `lb` display converts loads.
- 1527 tests, 77 Playwright, 94% coverage.

### prp-04 — history-scorecard (2026-09-06)
- History screen at `GET /history?profile=me|son|together`: weekly scorecard (done vs `days_per_week`, one dot per session, streak and best), the finished-session list with date, day name, N of M, duration, how it felt and a sync badge, and cards that expand in place to the per-exercise rows (HTMX, and an ordinary link with JavaScript off).
- Streak defined in exactly one place (`cadence/historique/scorecard.py`): consecutive completed planned sessions in program order, held by a partial, reset by a skip, ended by the first still-planned row, counted once per profile for a Together session.
- Parent-only inline SVG sparkline of the last 30 days of weight and body fat from `metrics_cache`, two polylines on a shared date axis, hidden below two points and never rendered for the son or on the shared Together tab (D-105).
- `GET /api/sessions?profile=&limit=` (1–200, else 422) and `GET /api/scorecard?profile=`; the `trend` key is absent, not null, for a youth profile.
- `metrics_cache` table created empty with the payload contract PRP-06 fills (D-101); `sync_job` read only when it exists, otherwise every badge reads "Stored locally".
- History link added to the base nav; Today stays the landing screen. `history.css` loads only on `/history`, so Today keeps its 60 KB budget.
- 1153 tests, 52 Playwright tests at 390×844, 94% coverage. Decisions D-100 to D-106.

### prp-04 — history-scorecard (2026-09-06)
- `/history?profile=` with completed sessions (date, day type, N/M rows, felt, duration, sync badge), expand-in-place session detail scoped to the profile, weekly scorecard (done vs planned, current and best streak), Together counted once per profile.
- Body-composition sparkline (weight kg + body-fat %) for the adult profile only, from `metrics_cache` (table created here, filled by PRP-06); youth history shows fun stats only, no trend, no banned words.
- `GET /api/sessions`, `GET /api/scorecard`; `cadence/historique/` domain package with no web-layer imports; day labels in `cadence/schema/labels.py`.
- 1277 tests, 58 Playwright, 94% coverage.

### prp-06 — vitalforge-client (2026-09-07)
- `cadence/vitalforge/` (nine modules): httpx client with separate weight/dashboard URLs and bearer auth (token redacted by value and shape everywhere), metrics cache refresh (adult: weight/body-fat/muscle/RHR/sleep/body battery + readiness; youth: readiness only, never body-comp; grams ÷ 1000; malformed responses never overwrite a good cache), readiness nudge on Today (adult), exact `ActivityIn` payload builder (timed rows as reps=1 + seconds, `garmin_category` omitted when null, clamped `start`, frozen `target_slug`).
- Write-back on Done inside the finalisation transaction, one bounded inline attempt, then a leased retry queue (D-016 backoff, 8 attempts, 404/401 exempt, 409/422 terminal, `unknown` terminal, Garmin pending/failed after `sent` rescheduled, throttled manual Retry, periodic drain off under test, backfill of jobless sessions); youth pushes withdrawn when `push_son_to_garmin` turns off.
- `GET /api/metrics`, `POST /api/sync/retry` (429 when throttled), `POST /done/{id}/retry`; Done status line with five states; `CADENCE_ENV` (prod refuses mock modes); `CADENCE_VITALFORGE_MODE=mock` for smoke tests.
- 1739 tests, 84 Playwright, 94% coverage; end-to-end assertion of the exact `/api/activity` JSON for `me` and for `son` with the setting on and off.

### prp-09 — deploy (2026-09-07)
- `Dockerfile` (non-root uid 10001, root-owned app code, healthcheck, `CADENCE_ENV=prod` default), `docker-compose.yml`/`.dev.yml`/`.prod.yml` (parameterised `CADENCE_BIND_ADDR`, `.dev.yml` layers mock mode locally), `.dockerignore` excluding `.env*`/`data/`/`.git`/tests/docs.
- `scripts/backup.sh` (both engines, `umask 077` + `chmod 700`, 30-snapshot retention, injection-hardened), `scripts/deploy.sh` (rsync, injection-guarded host/path, excludes the dev overlay, refuses an uncommitted tree without `--force`), `scripts/smoke.sh` (8 steps; mock-mode activity count via `GET /api/_mock/activities`, mounted only outside prod; live-mode accept-set matches the pre-VitalForge-deploy state).
- `docs/deploy.md`: VM-201 runbook (NPM proxy, Tailscale bind address, backup/restore, upgrade, troubleshooting table).
- GitHub Actions image-build job (no push). 1824 tests, 94% coverage.
### prp-08 — ai-ingestion (2026-09-07)
- `POST /api/import` (YAML/JSON text or file ≤ 256 KB, size checked before parsing; yaml-scanner anchor/alias rejection; inline exercises never carry document-supplied safety metadata and are refused for youth targets; duplicate/oversized/deep documents refused) and its Settings "Import workout" card.
- `POST /api/generate` / `/accept` via OmniRoute (`cadence/ia/`): prompt sends only band/equipment/goal/gap/allowed-exercise-ids, never session data, metrics, names, ages beyond band, or bodyweight-derived numbers even on retry; mock mode passes the validator for both profiles; response streamed with a byte cap; one retry on validation failure only; accept re-validates the posted document against the real target profile, never trusts a client `ok`.
- Same-origin guard on every browser write route; day-type adoption is one guarded transaction.
- 1945 tests, 95 Playwright, 94% coverage.

### prp-05 — vitalforge-activity (2026-09-06) — branch `cadence/activity-endpoint` in ../vitalforge, not pushed
- `POST /p/{slug}/api/activity` (202 fresh / 200 dedup / 409 cross-person / 422), `GET /p/{slug}/api/activity/{session_id}`, `GET /p/{slug}/api/strength-sessions`.
- `strength_sessions` table with `UNIQUE(person_id, session_id)`; push claimed inside `BEGIN IMMEDIATE` (`garmin_claimed_at`, 600 s lease); statuses pending/synced/failed/skipped/unknown; reconciliation by lookup for ambiguous outcomes; session marker in the Garmin activity name.
- Guaranteed path `create_manual_activity` (local wall clock + `TZ`); exercise sets behind `VITALFORGE_GARMIN_EXERCISE_SETS=0`; D-015 override `garmin_target: "credential_person"` with display-name prefix.
- 832 tests green in the VitalForge clone (from 676); ruff clean; no migration marker; `.env` never opened.

### Phase 1
- Eleven PRPs written under `docs/prp/` with the execution protocol in `docs/prp/README.md`.
- **PRP-00 Foundation.** uv project on Python 3.12, `cadence` package with `create_app()`, typed
  settings, SQLite layer with WAL and a schema-version guard, the full Pydantic v2 library DSL,
  the validator (schema, equipment whitelist, youth rules V1-V14 including the
  fail-closed per-band exercise allowlist), `GET /api/health`, the JSON
  envelope, `library/youth_rules.yaml` for all four age bands, Makefile, pre-commit config and
  GitHub Actions CI. Decisions D-028 to D-034.

### prp-06 — VitalForge client (2026-09-06)
- `cadence/vitalforge/`: `client.py` (httpx, separate weight `:8085` and dashboard `:8086` base URLs, bearer header built at one point, 5 s timeout, typed `ActivityResult`), `errors.py` (no `httpx` exception escapes the package; every message and body sanitised of `Bearer …` and truncated to 500 chars), `metrics.py`, `payload.py`, `sync.py`, `writeback.py`, `periodic.py`, `mock.py`.
- `sync_job` table with a unique `session_id`; `enqueue` is get-or-create, so the offline queue's replay of Done cannot file a session twice.
- Write-back on Done: one `sync_job`, one inline POST at the 5 s timeout, redirect never blocked. Bounded retry per D-016 — 1 min → 2 h, 8 attempts; **409 and 422 terminal**; a 404 names the `cadence/activity-endpoint` branch and a 401 says the token was rejected, neither burning an attempt; VitalForge's `unknown` Garmin status is terminal with a warning.
- Metrics cache filled per D-101 and D-120: `weight` and `muscle_mass` divided by 1000 (contract §2.2), `body_fat` untouched, 6 h staleness evaluated on read, a partial failure keeping whatever came back and a total failure keeping the last good payload. The youth profile fetches **readiness only** and its body-composition keys are absent, not null.
- Readiness nudge: "Good day to push" / "Steady day" / "Easy day — hold loads" / "Readiness not available"; a `null` score is never coerced to zero (the son's is permanently null).
- `GET /api/metrics?profile=` (cache only, zero network calls) and `POST /api/sync/retry`, both on the standard envelope; a 5-minute lifespan task drains the queue and refreshes the cache, sleeping first and cancelling cleanly.
- Done screen shows all five sync states, with a Retry button only on the terminal one, kept verbally distinct from PRP-02's offline banner. One-line readiness nudge on Today for the adult profile, from the cache.
- `CADENCE_VITALFORGE_MODE=mock` runs the whole path in process against a fake that records payloads, for PRP-09's deploy smoke test.
- Tests: 108 new (`test_vitalforge_client` 12, `test_metrics` 20, `test_payload` 20, `test_sync` 25, `test_metrics_api` 6, `test_done_sync_line` 10, `test_session_to_activity` 11, plus 4 readiness-nudge cases in `test_web_today`) and 4 Playwright. The brief's finish criterion — one full session through Done asserting the **exact** `/api/activity` JSON — is asserted with `==` for `me` and for `son` with the setting on and off. Together mode files two activities, one per person slug, with the son's `garmin_target` riding on the setting alone. The cached payload is read back through PRP-04's own trend reader, so the D-101 seam is crossed rather than described. Suite-wide respx guard proves no unmocked host is ever reached.
