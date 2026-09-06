# Changelog

All notable changes to Cadence. Format follows Keep a Changelog; one entry per PRP.

## [Unreleased]

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
