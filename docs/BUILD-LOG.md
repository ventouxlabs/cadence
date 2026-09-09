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

## prp-07 — progression-assess — 2026-09-07 (worktree, parallel with 08/09, merged last)
- Implementer built autoregulation, assessments, gaps, challenges (78 tests, 4 e2e); merged main (PRP-06) after a session-limit interruption, derived a muscle_pct series PRP-06 didn't provide.
- Tester added the full §5.4 cartesian product (1024 cases) plus wiring tests (2866 total); found the retest reminder never re-appears after baseline (spec deviation).
- Reviewer: 1 High (two same-day-type challenges could evict each other) + 4 Mediums fixed; retest-reminder fix added self-queuing (28-day, skip-safe); re-verify APPROVE.
- Merged main (PRP-08/09): resolved a decision-number range collision (renumbered D-190..D-207 to D-210..D-227) and fixed 4 tests whose hand-rolled `challenge` fixtures predated PRP-07's real table.
- Final: 3163 passed, 100 e2e, 92% coverage, lint clean.
- Squash-merged to main, tagged prp-07, pushed. All eleven PRPs now merged.

## PRP-10 — Son mode, badges, performance, screenshots, handoff (tag `prp-10`)

- Son-mode UX pass as one CSS scope (`[data-profile-kind="youth"]`), no second template tree: type up one step, 64 px rows, an inline-SVG icon per movement pattern from one reused `<symbol>` sprite, a 200 ms check animation that shifts no layout, and a promoted "Good enough — done!" at 72 px.
- Seven badges in `cadence/historique/badges.py`, derived on every read and stored nowhere; streaks and "complete" are imported from PRP-04 rather than restated, and the strip renders into PRP-04's `#badges` container with a caption route that never navigates.
- `make perf`: `scripts/perf.py` measures gzip transfer bytes for `/today` on both profiles against D-025's budgets, asserts `Content-Encoding: gzip`, no external host and no webfont, and is importable so a test can prove the gate fires without the middleware.
- `make screenshots`: seven PNGs into `docs/screenshots/` from two throwaway seeded databases, motion forced off and the metrics cache fixed; it refuses to run against `data/cadence.db`. CI uploads the directory as an artifact.
- Late review fixes: `youth_safe` became a real per-rule field filtered on in `build_column`, so the youth badge guard is enforced rather than true by accident (D-254), and the Together Done screen carries the youth scope on the son's summary card so his half is not rendered at the adult scale (D-255).
- Review fixes: the son's Done screen got the son-mode pass it had been claimed to have and a derived "New badge" line (D-247); "Skip" became "Not today" and the guard that missed it now matches stems on a word boundary (D-246); the badge-caption guard no longer resolves its tab from the path it is authorising (D-249); screenshots are viewport captures so the fixed footer stops painting over a row (D-250), and `tests/test_screenshots.py` pins the seven names and dimensions since a byte diff cannot survive two machines (D-251); HANDOFF quotes the real branch base (D-252) and names the two badge edges (D-231, D-232, D-248).
- Pre-merge fixes: the badge-caption route gained the ownership check its sibling route already had (D-243), the icon lookup was scoped to youth rows so the parent's hot path pays nothing (D-244), and the end-to-end banned-word list imports the shared one instead of forking it (D-245).
- `docs/HANDOFF.md` (nine sections plus "Installing as an app" for D-117's PWA and TWA paths) and a rewritten `README.md`. D-230..D-242 record the departures.

## prp-10 — polish — 2026-09-07 (final PRP)
- Implementer built badges, son-mode UX pass, perf/screenshot scripts, HANDOFF.md (3193 tests, 111 e2e).
- Reviewer: 2 Highs (a "Skip" button reached the son's screen past a mis-calibrated word-list guard; the son's Done screen got no youth pass despite the changelog claiming it did) + 6 Mediums + 4 Lows fixed and re-verified APPROVE.
- Tester added ~46 tests, caught two vacuous e2e tests (empty-string parse, mid-swap computed style), fixed a real HANDOFF/screenshot test file duplication.
- Orchestrator pushed back once more: a hardcoded `youth_safe=True` with an explanatory comment was replaced with a real per-rule filter and a test proving an unsafe rule is actually excluded, matching the project's standing rule that "safe by accident" always gets an enforced gate.
- Final: 3241 passed, 129 e2e, 92% coverage, lint clean.
- Squash-merged to main, tagged prp-10, pushed. **All eleven PRPs complete.**

## solo mode — 2026-09-09 (post-PRP feature, JD request)
- JD: "lets hide my son's window and i will just do it. if he wants to participate later, i will reenable."
- Implementer built `son_enabled` + `profils/visibility.py`, `web/solo.py`, `api/gates.py`; found and fixed two pre-existing bugs (D-267 stale tab strip, D-268 history_card authorising itself).
- Reviewer: 1 High (`/done/{id}` rendered his session in full while hidden — proven empirically, 200/1856 bytes) + 4 Lows. Implementer correctly rejected the orchestrator's prescribed fix (query-keyed check would have 404'd every offline Done) and gated on session owner instead.
- Finding 5's worst branch was unreported: the redisplay-on-rejected-value path leaked his whole form without needing a valid post.
- Re-verified by probe: all 5 FIXED, writes still land (assessment rows 0→6 while hidden), no regressions. APPROVE.
- Also landed: docs/CODEMAPS (5 files), docs/CONTRIBUTING.md, and D-275 — `CADENCE_BIND_ADDR` was missing from `.env.example` because the parity test could not express a Compose-only key.
- Final: 3326 passed, 135 e2e, 93% coverage. Squash-merged to main, tagged solo-mode, pushed, deployed to VM-201.
