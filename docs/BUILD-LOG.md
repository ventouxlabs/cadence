# Build log

Five lines per PRP, appended by the orchestrator after each squash-merge.

## Phase 0 — 2026-09-06
- Repo created at ventouxlabs/cadence (public), AGPL-3.0, `main` branch.
- VitalForge clone found at ../vitalforge (garminconnect pinned 0.3.11).
- Discovery agents spawned: vitalforge-contract, exercise-principles.
- Tooling verified: uv 0.11, Python 3.12.13, ruff, pytest 9, Playwright 1.61 + Chromium, podman-compose, Codex CLI 0.151.
- Decisions D-001..D-007 logged.

## prp-00 — foundation — 2026-09-06
- Implementer built scaffold, schema, validator, health endpoint, CI (108 tests); tester added 306 adversarial cases.
- Devil's-advocate review: 3 Highs (row-declared load_unit, youth+adult band, self-declared prelude) + relocated High on assessment day_type; all fixed and re-verified.
- Codex (gpt-5.6-terra) review: 2 Highs overlapping, 3 Mediums (NaN bodyweight, ceilings as warnings, shallow frozen) fixed; strict numerics added.
- Final: 454 passed, 4 skipped, 99% coverage, lint clean. Decisions D-028..D-037 (+D-040..D-048 from PRP-05 landed in the same tree).
- Squash-merged to main, tagged prp-00, pushed.
