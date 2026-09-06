# Cadence — architecture backbone

This is the fixed skeleton every PRP builds against. PRPs may add to it; they may not contradict it without a `docs/DECISIONS.md` entry.

## 1. Shape

- **One process.** FastAPI app `cadence` serving both the HTML UI (Jinja2 + HTMX + a little vanilla JS) and a JSON API under `/api`. SQLite file at `data/cadence.db` (SQLModel). No background workers: the sync queue is drained by an in-process periodic task and by explicit retry calls.
- **PWA.** `manifest.json`, `sw.js` (cache-first for static + last Today page per profile), IndexedDB tick queue that replays to `/api` when back online.
- **Library as data.** `library/exercises/*.yaml`, `library/workouts/*.yaml`, `library/progressions/*.yaml`, `library/youth_rules.yaml`, `library/assessments.yaml`. Pydantic v2 models in `cadence/schema/` are the single source of truth; `make seed` loads the library into the DB. Imported and AI-generated workouts are stored in the DB only (`source = import | generated`), never written back to `library/` (no path handling on import).
- **Integrations.** `cadence/vitalforge/` (read metrics, write activity, retry queue) and `cadence/ia/` (OmniRoute generation). Both mocked in tests; both degrade gracefully when unreachable. VitalForge addresses people by path slug (`/p/{slug}/api/...`) with `Authorization: Bearer <VITALFORGE_TOKEN>`; see `docs/vitalforge-contract.md` for the exact read endpoints (`weight/recent`, `metrics/{name}?days=`, `readiness`) and the `POST /p/{slug}/api/activity` write contract (202 fresh / 200 dedup / 409 cross-person Garmin push / 422). The weight service (`:8085`) hosts `/api/activity`; the dashboard (`:8086`) hosts metrics and readiness, so `VITALFORGE_WEIGHT_URL` and `VITALFORGE_DASHBOARD_URL` are separate settings.

## 2. Naming convention (French project names, English identifiers)

Ventouxlabs projects use French names for the project, services, containers, and top-level domain modules. Code identifiers (classes, fields, JSON keys, routes) stay English so the API matches VitalForge and Garmin.

```
cadence/
  __init__.py
  main.py               # create_app(), lifespan, router registration
  config.py             # Settings from env (.env): CADENCE_*, VITALFORGE_*, OMNIROUTE_*
  db.py                 # engine, session factory, init_db, migrations (simple versioned SQL)
  schema/               # Pydantic v2 models = library DSL (Exercise, Workout, Row, Progression, YouthRuleSet, AssessmentSpec)
  validateur/           # validator: schema, equipment whitelist, youth rules, per-profile checks
  bibliotheque/         # library loader (YAML → DB), lookup, import service
  programme/            # 4-week program builder, day templates, autoregulation, deload, load rounding
  profils/              # Profile + Setting tables, age bands, setup/settings services
  seance/               # today resolution, ticks, adjust, done, session/row tables, summary
  historique/           # history queries, weekly scorecard, streak
  bilan/                # assessments, gaps, challenges
  vitalforge/           # client (read metrics + POST activity), cache, sync queue + retry
  ia/                   # OmniRoute client, prompt templates, generate service
  web/                  # routers for HTML pages + HTMX partials; templates/; static/
  api/                  # JSON routers: health, today, sessions, settings, profiles, import, generate, metrics, sync, assessments
tests/                  # pytest (unit + API), tests/e2e/ (Playwright, 390px viewport)
library/                # seed YAML (see above)
docs/                   # this file, principles, contract, prp/, DECISIONS, BUILD-LOG, HANDOFF
```

## 3. Data model (SQLModel tables)

| Table | Key columns | Notes |
|---|---|---|
| `profile` | `id` (slug: `me`, `son`), `display_name`, `kind` (`adult`/`youth`), `age_years`, `age_recorded_on`, `vitalforge_person` (slug), `push_to_garmin` (bool) | Two seeded rows. Youth band derived from age (see principles). Until `age_years` is set, band = strictest. |
| `setting` | `key` (pk), `value_json`, `updated_at` | Key/value; keys: `equipment`, `weights_available`, `days_per_week`, `session_minutes`, `push_son_to_garmin`, `setup_complete`, `timers_default_on`, `readiness_nudge_on`. |
| `exercise` | `id` (kebab slug), `doc_json`, `source` (`seed`/`import`/`generated`), `created_at` | `doc_json` is the validated `Exercise` model dump. |
| `workout` | `id`, `doc_json`, `source`, `target_profile_kind`, `created_at` | Validated `Workout` model dump. |
| `program` | `id`, `profile_id`, `template`, `start_date`, `weeks`, `days_per_week`, `session_minutes`, `status` | One active program per profile. Rebuilt when settings change. |
| `planned_session` | `id`, `program_id`, `profile_id`, `week` (1–4), `day_index`, `day_type`, `workout_id`, `rows_json`, `status` (`planned`/`done`/`skipped`) | Rolling: "today" = first `planned` row in order. Not date-bound. `rows_json` = materialised rows with concrete loads. |
| `session` | `id` (uuid4, also the VitalForge `session_id`), `profile_id`, `planned_session_id`, `started_at`, `finished_at`, `duration_min`, `felt` (`easy`/`right`/`hard`/null), `readiness_at_start`, `together_group_id`, `notes` | Created on first tick; finalised on Done. |
| `session_row` | `id`, `session_id`, `position`, `exercise_id`, `sets_planned`, `reps_planned`, `seconds_planned`, `load_planned_kg`, `sets_done`, `reps_done`, `seconds_done`, `load_done_kg`, `done` (bool), `done_at`, `is_challenge` | Adjust edits `*_done`. |
| `sync_job` | `id`, `session_id`, `target` (`vitalforge`), `status` (`pending`/`sent`/`failed`/`skipped`), `attempts`, `next_attempt_at`, `last_error`, `payload_json`, `remote_ref` | Idempotent on `session_id`. |
| `assessment` | `id`, `profile_id`, `test_id`, `value`, `unit`, `recorded_on`, `self_rated` | Six tests; baseline + every 4 weeks. |
| `challenge` | `id`, `profile_id`, `name`, `test_id`, `target_value`, `due_on`, `status` (`active`/`met`/`expired`), `row_json` | ≤3 active per profile. |
| `metrics_cache` | `profile_id` (pk), `fetched_at`, `payload_json`, `stale` | Latest VitalForge pull; used when VitalForge is down. |

All rows are immutable from the app's point of view: services build new dicts/models and write them; no in-place mutation of loaded objects except through explicit `update` helpers.

## 4. Routes

### HTML (Jinja + HTMX), `cadence/web/`
| Route | Screen |
|---|---|
| `GET /` | Redirect to `/setup` until `setup_complete`, else `/today` |
| `GET /today?profile=me\|son\|together` | Today checklist (profile switcher at top) |
| `POST /today/{session_id}/rows/{position}/tick` | HTMX: toggle row done, returns row partial |
| `POST /today/{session_id}/rows/{position}/adjust` | HTMX: set reps/load done (≤2 taps) |
| `POST /today/{session_id}/felt` | HTMX: easy/right/hard |
| `POST /today/{session_id}/done` | Finalise → redirect `/done/{session_id}` |
| `GET /done/{session_id}` | 3-line summary + sync status |
| `GET /history?profile=` | Sessions list + weekly scorecard + trend line (me only) |
| `GET /setup` `POST /setup` | First-run setup |
| `GET /settings` `POST /settings` | Same fields, editable; import + generate sections |
| `GET /assess?profile=` `POST /assess` | Baseline / retest form |

### JSON, `cadence/api/`
| Route | Purpose |
|---|---|
| `GET /api/health` | `{status, db, vitalforge, version}` — compose healthcheck |
| `GET /api/today?profile=` | Today payload (for SW cache + offline) |
| `POST /api/sessions/{id}/rows/{position}` | Tick/adjust (offline queue replay target; idempotent) |
| `POST /api/sessions/{id}/done` | Finalise (idempotent) |
| `GET /api/sessions?profile=&limit=` | History |
| `GET /api/scorecard?profile=` | Weekly scorecard |
| `GET/PUT /api/settings`, `GET/PUT /api/profiles/{id}` | Settings |
| `POST /api/import` | Body: YAML/JSON text or file; runs the validator; returns `{ok, errors[], workout_id?}` |
| `POST /api/generate` | `{profile, goal, gap?}` → OmniRoute → validated preview `{ok, workout, errors[]}`; `POST /api/generate/accept` stores it |
| `GET /api/metrics?profile=` | Cached VitalForge metrics + readiness nudge |
| `POST /api/sync/retry` | Drain the sync queue now |
| `GET/POST /api/assessments?profile=` | Assessments + derived challenges |

Response envelope for `/api/*`: `{"ok": bool, "data": ..., "error": str|null, "meta": {...}}`.

## 5. Cross-cutting rules

- **Secrets** only via `.env` (`VITALFORGE_TOKEN`, `OMNIROUTE_KEY`); `cadence/config.py` loads them; they are never logged, rendered, or returned by any endpoint (`/api/health` reports only `configured: true/false`).
- **Validator is the gate.** Seed load, `/api/import`, and `/api/generate` all call `validateur.validate_workout(doc, profile_kind, age_band, equipment)`; nothing reaches the DB or a screen without passing. Youth rules and the equipment whitelist are data in `library/youth_rules.yaml` and `cadence/schema/equipment.py`.
- **Today is the hot path.** No network call on `GET /today`; metrics/readiness come from `metrics_cache` and are refreshed by the periodic task. Page weight target < 60 KB, no external CDN, HTMX vendored in `static/`.
- **Youth profile never sees**: weight, body-fat, muscle %, appearance goals, trend line, or load-focused challenges. Enforced in templates via `profile.kind` and in `bilan` when generating challenges.
- **Together mode** = two sessions (one per profile) sharing `together_group_id`; one Done finalises both; write-back is per session (son's respects `push_to_garmin`).
- **Write-back**: on Done, `sync_job` is created and one POST is attempted inline (timeout 5 s); failures leave it `pending` with exponential backoff (D-016) drained by the periodic task and `POST /api/sync/retry`. The payload is the contract's `ActivityIn`; son's sessions carry `push_to_garmin` = `push_son_to_garmin` setting and, when true, `garmin_target: "credential_person"` (D-015). Readiness `score: null` renders as "not available".
- **Offline**: ticks are optimistic in the UI, queued in IndexedDB as `{session_id, position, patch, ts}`, replayed to `POST /api/sessions/{id}/rows/{position}`; Done is queued the same way; the Done screen shows "will sync" while `sync_job.status = pending`.
