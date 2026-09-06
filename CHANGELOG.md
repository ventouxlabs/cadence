# Changelog

All notable changes to Cadence. Format follows Keep a Changelog; one entry per PRP.

## [Unreleased]

### prp-00 — foundation (2026-09-06)
- uv project on Python 3.12, ruff, pytest, pre-commit config, GitHub Actions CI, Makefile (`dev test lint fmt e2e seed deploy`).
- `cadence/` package: `create_app()`, typed settings from `.env`, SQLite/SQLModel layer, `GET /api/health`, JSON envelope.
- Pydantic v2 library DSL (`Exercise`, `Workout`, `WorkoutRow`, `Progression`, `YouthRuleSet`, `AssessmentSpec`, equipment whitelist, age bands) with strict numerics and deep immutability.
- Validator (`validateur`) as the single gate for seed, import and generation: 14 rule families, youth caps derived from the exercise's `load_unit`, per-band exercise allowlist, assessment-day exemptions per row.
- 454 tests, 99% coverage, including adversarial youth-rule and equipment-whitelist bypass suites.

### Phase 0
- Repository initialised (AGPL-3.0), discovery docs started.

### Phase 1
- Eleven PRPs written under `docs/prp/` with the execution protocol in `docs/prp/README.md`.
- **PRP-00 Foundation.** uv project on Python 3.12, `cadence` package with `create_app()`, typed
  settings, SQLite layer with WAL and a schema-version guard, the full Pydantic v2 library DSL,
  the validator (schema, equipment whitelist, youth rules V1-V14 including the
  fail-closed per-band exercise allowlist), `GET /api/health`, the JSON
  envelope, `library/youth_rules.yaml` for all four age bands, Makefile, pre-commit config and
  GitHub Actions CI. Decisions D-028 to D-034.
