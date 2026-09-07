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

## prp-05 — vitalforge-activity — 2026-09-06 (VitalForge clone, branch cadence/activity-endpoint @ 851eb2d, never pushed)
- Implementer built table, models, route, Garmin helpers, read routes, docs; suite 676 → 771.
- Reviewer: 2 Highs (post-commit claim let concurrent POSTs double-push; parse outside never-raise helper) fixed via in-transaction claim column; re-verified APPROVE.
- Codex (gpt-5.6-terra, second run after a stdin hang): 1 High (ambiguous outcomes retryable) → `unknown` state + reconciliation by lookup; 3 Mediums + 1 Low fixed; `garmin_error` sanitised.
- Final: 832 passed, ruff clean; probe checklist for JD's live Garmin account left in the PRP; D-040..D-049.
- Tag prp-05 is placed on Cadence main after PRP-01 merges (docs-only closeout).

## prp-01 — library-program — 2026-09-06
- Implementer transcribed §9/§10 (52 exercises, 11 workouts), built loader, program engine, profile/setting tables, seed (572 tests).
- Tester added 317 tests incl. principles-parity parser, 480-config program matrix, load grammar; fixed 0 kg rung; reported 1000 kg ceiling (fixed).
- Reviewer swept 15,744 compiled sessions (zero cap breaches); 3 Highs (seed deleted done sessions, no backfill for dropped main, fallback_exercise unread) + 4 Mediums fixed; re-verify found 1 more High (one-hop demotion) fixed via BFS + pattern fallback.
- Final: 945 passed, 94% coverage, lint clean, seed idempotent. D-050..D-068.
- Squash-merged to main, tagged prp-01 (and prp-05 close-out tag placed on main), pushed.

## prp-02 — today-checklist — 2026-09-06
- Implementer built session tables, seance services, Today/Done/Together, PWA + offline queue, HTMX vendored (1016 tests, 15 e2e).
- Tester added 44 pytest + 21 e2e (replay abuse, Together Done, youth surface), fixed NaN-body 500 and a fixture exhausting the plan; screenshots reviewed by orchestrator.
- Reviewer: 2 Highs (writes on a finished session materialised the next one; Together Done ignored the son's checklist) + 5 Mediums fixed and re-verified APPROVE; follow-up batch fixed felt scoping, per-session promotion, footer occlusion, atomic patch.
- Implementer hit the account session limit on the last two items; orchestrator verified the uncommitted work (lint, 1087 pytest, 45 e2e green) and committed it.
- Squash-merged to main, tagged prp-02, pushed. D-070..D-084.

## prp-04 — history-scorecard — 2026-09-06 (worktree, parallel with PRP-03)
- Implementer built historique package, history routes/templates, sparkline, metrics_cache table (1153 tests, 52 e2e).
- Tester added 118 unit + 6 e2e (streak sequences, frozen clock, guards, tablet); fixed cross-profile session detail leak and a TZ fixture leak.
- Reviewer: no Highs; 4 Mediums (unscoped detail route, two definitions of "finished", domain importing web layer, swallowed errors) + 3 Lows fixed (D-107).
- Final: 1277 passed, 58 e2e, 94% coverage, lint clean.
- Squash-merged to main, tagged prp-04, pushed.

## prp-03 — profiles-settings — 2026-09-07 (worktree, parallel with PRP-04)
- Implementer built setup/settings screens, services, validation, rebuild, API (1158 tests, 57 e2e).
- Tester added 159 tests (settings/profile adversarial matrices, e2e first-run flow); tightened the person-slug rule.
- Reviewer: 2 Highs (orchestrator's D-099 made an 18 typo permanently strip youth rules — revised; silent rebuild skip when library missing) + 4 Mediums fixed; re-verify found the setup replay guard on the wrong route — orchestrator applied the 3-line fix + test.
- Merged main (PRP-04) into the branch; exposed and fixed settings_from_rows discarding all keys on one bad value (D-115).
- Final: 1527 passed, 77 e2e, 94% coverage. D-090..D-099, D-110..D-115. Squash-merged to main, tagged prp-03, pushed.

## prp-06 — vitalforge-client — 2026-09-07 (worktree, parallel with 07/08)
- Implementer built client, metrics, payload, sync, writeback, periodic, mock (1385 tests, 62 e2e); merged main (PRP-03).
- Tester added 18 adversarial + 3 e2e (payload determinism, queue under load, three deployments); fixed 404 on unknown retry.
- Reviewer: 2 Highs (fast clock → permanent 422; blank token/slug → silent permanent skip) + 4 Mediums; Codex (gpt-5.6-terra): 4 more Highs (token by value, mock in prod, retry re-resolving slug, youth push surviving opt-out) + 3 Mediums; all fixed; re-verify found one interaction High (SENT-with-schedule youth job escaping opt-out) fixed and confirmed by independent probe → APPROVE.
- Final: 1739 passed, 84 e2e, 94% coverage; D-120..D-143.
- Squash-merged to main, tagged prp-06, pushed.

## prp-09 — deploy — 2026-09-07 (worktree, parallel with 07/08)
- Implementer built Dockerfile/compose/scripts/runbook, verified image build + 8-step smoke + backup/restore under podman (1561 tests); merged main (PRP-06) after a session-limit interruption, resolved by a fresh agent.
- Tester added 18 tests (executable healthcheck probe, restore-from-runbook, mock-mode inspection); fixed the prod-breaking unconditional recorder check in smoke.sh.
- Reviewer + Codex: 2 Highs (deploy.sh command injection, incomplete .env* gitignore) + 6 Mediums (backup dir perms, port binding, restart-vs-healthcheck doc) + 4 Lows (docker inspect leak, quoting, uv pin, threshold typo) — all fixed and re-verified APPROVE.
- Final: 1824 passed, 94% coverage, lint clean; image builds and smokes green under podman.
- Squash-merged to main, tagged prp-09, pushed.
## prp-08 — ai-ingestion — 2026-09-07 (worktree, parallel with 07/09)
- Implementer built import/generate pipeline, OmniRoute client, prompt template, Settings UI (89 tests, 6 e2e); merged main (PRP-06) after a session-limit interruption, resolved by a fresh agent.
- Codex (gpt-5.6-terra): 5 Highs (inline-exercise metadata trust, multipart spooling, yaml anchor gap, retry-prompt bodyweight leak, adoption race) + several Mediums fixed.
- Devil's-advocate: 2 Highs (mock/schema example hardcoded "adult", breaking youth generation) + Mediums fixed; a full independent re-review found 1 Medium (unbounded body read on two settings routes) + 5 Lows, all fixed.
- Final: 1945 passed, 95 e2e, 94% coverage, lint clean.
- Squash-merged to main, tagged prp-08, pushed.
