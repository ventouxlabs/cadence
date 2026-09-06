# Cadence — original brief (verbatim from JD, 2026-09-06)

Ventouxlabs · AGPL-3.0 · French naming convention. Repo: this directory (`main` branch, conventional commits).

A very simple daily workout app for me (JD) and my son. One sentence: **open the app, see today's workout as a checklist, tick each exercise as you finish it, hit Done, and the completed session shows up in VitalForge and, from there, in Garmin Connect as a strength-training activity.**

## Known environment (use these; don't ask)

```
VitalForge repo:        https://github.com/bearyjd/vitalforge  (clone read-only into ../vitalforge if not present)
VitalForge services:    VM-201 (192.168.1.21) — weight service :8085 → https://weight.grepon.cc, dashboard :8086 → https://health.grepon.cc
VitalForge stack:       Python/FastAPI/SQLite, unofficial Garmin Connect sync via the `garminconnect` library, bearer-token auth
LLM gateway:            OmniRoute, OpenAI-compatible, https://llm.grepon.cc/v1 — models: `code-plan` (generation), `cheap-think` (summaries)
                        Gotcha: never use `openrouter/google/gemini-2.5-flash` streaming; use direct `gemini/...` if Gemini is needed
Deploy target:          VM-201, docker compose, behind Nginx Proxy Manager, reachable over Tailscale. Proposed hostname: cadence.grepon.cc
Secrets:                VITALFORGE_TOKEN and OMNIROUTE_KEY are read from `.env` at runtime. You will NOT have them. All tests mock these; ship `.env.example`.
```

## Things I have not told you → make them settings

Do not guess these. Build a one-time **Setup** screen (also editable later under Settings) with these defaults:

| Setting | Default | Notes |
|---|---|---|
| Son's age | *required on first run* | Drives youth rules by age band: <10, 10–13, 14–17. Until set, son's profile uses the strictest band. |
| Equipment | bodyweight ✓, dumbbells ✓, kettlebells ✓, adjustable bench ✓ | Checkboxes plus free-text weights available (e.g. "DB 5–52.5 lb adj", "KB 16/24 kg"). Nothing outside this whitelist is ever selectable. |
| Days per week | 4 | 2–6 |
| Session length | 30 min | 15–45 |
| Push son's sessions to Garmin | off | Son's sessions are always stored in VitalForge; pushing to my Garmin account is opt-in. |

## Goals the program must serve

- **Me:** functionally stronger (hinge / squat / push / pull / carry / brace); better posture (thoracic mobility, scapular control, glute/hip activation, anti-rotation core — a 5-minute posture prelude every session); better shirtless appearance (shoulders, upper back, chest, midsection; body-fat trending down while muscle % holds or rises).
- **Son:** movement quality, coordination, fun, consistency. Youth rules are enforced in the validator and program engine, not just documented: bodyweight and light dumbbells only, technique over load, no max-effort or 1RM-style work, generous rest, shorter sessions, and an always-visible "good enough — done" exit. Son gets fun, non-body-image challenges only (no weight, body-fat, or appearance goals ever shown on his profile).

## The UI (this is most of the product — keep it boring and fast)

1. **Today.** Profile switcher at top: Me / Son / Together. Below: today's workout as a plain checklist — exercise name, sets × reps (or seconds), weight to use, one-line form cue. One big checkbox per row. Tap = done. A small "adjust" affordance lets me change weight or reps actually done in ≤2 taps. A "felt easy / just right / hard" three-way toggle appears once all rows are ticked. Per-exercise timer for timed holds and rest, off by default, one tap to enable.
2. **Done.** Shows a 3-line summary (duration, exercises completed, one "next time" note from the progression engine), then fires the VitalForge write-back. If offline, queue it and show "will sync".
3. **History.** List of completed sessions + a small weekly scorecard (done vs planned, streak, and for my profile a body-comp trend line pulled from VitalForge).
4. **Together mode.** Both checklists stacked on a phone, side-by-side on a tablet, one shared Done.
5. **Setup / Settings.** The table above, plus "Import workout" (paste YAML/JSON or upload file) and "Generate workout" (goal + optional gap → AI → preview → accept).

No video, no social, no nutrition, no chat. If a feature makes Today slower, the feature loses. Target: usable one-handed, on the floor, by a kid, under 10 seconds per exercise, on a low-end Android phone.

## Backend

- **Library as data.** Exercises and workouts are schema-validated YAML/JSON under `library/`. Pydantic models are the single source of truth. `/api/import` and the Setup import screen run the same validator, which rejects anything off-schema, off-equipment-whitelist, or breaking youth rules for the target profile.
- **Program engine.** Rolling 4-week plan from settings. Default template: 2 upper + 2 lower/full-body-with-carries days, posture prelude every session, week 4 deload. Simple autoregulation: all rows ticked + "felt easy" → bump load or reps next time; "hard" or missed sessions or low readiness → hold or regress. Progression rules live in data (steal Liftosaur's idea of progression-as-data, not its DSL).
- **AI generation.** `/api/generate` calls OmniRoute with a prompt template (goals, equipment, profile, optional named gap) and returns workouts that still pass the validator before they can be accepted. Send only the fields the prompt needs — never raw logs or health metrics.
- **VitalForge read.** Pull weight, body-fat %, muscle %, and whatever Garmin-derived metrics VitalForge already exposes (resting HR, sleep, body battery/readiness if present). Drive a one-line readiness nudge on Today and the History trend line. Cache locally; degrade gracefully if VitalForge is down.
- **VitalForge write-back (hard requirement).** On Done, POST the session to VitalForge. VitalForge already uses `garminconnect`, which supports `create_manual_activity` and typed strength-workout uploads with per-exercise sets/reps/rest. Extend VitalForge with `POST /api/activity` accepting `{session_id, profile, start, duration_min, exercises:[{name, garmin_category?, sets, reps, weight_kg?, rest_s?}]}` that stores the session and creates a `strength_training` activity in Garmin (manual activity as the guaranteed path; per-exercise upload as an enhancement if the library's strength payload works cleanly against the pinned version). Idempotent on `session_id`. Respect the son-push setting.
- **Assessments → gaps → challenges (light).** Day-1 baseline: push-up max, dead hang, plank, wall-angel reach, goblet squat quality (self-rated), farmer-carry time. Retest every 4 weeks. Compare to targets and to VitalForge body-comp trend; emit up to 3 named challenges (e.g. "Dead hang 60 s by 15 Oct") that the program engine weaves into sessions as one extra row.

## Stack (defaults — override only with a logged reason)

Python 3.12, FastAPI, SQLite (SQLModel or SQLAlchemy), Pydantic v2. PWA front end: server-rendered Jinja + HTMX + minimal vanilla JS, service worker caching Today for offline, tick queue in IndexedDB. Docker Compose, `.env`-driven, healthcheck endpoint. Tooling: uv, ruff, pytest, Playwright, pre-commit, GitHub Actions CI. Makefile targets: `dev`, `test`, `lint`, `seed`, `deploy`.

## Prior art

Hevy/Strong: best ≤3-tap logging, no programming. Fitbod: good programming, closed. Nike Training Club: free guided video, no logging. wger: AGPL, self-hosted, REST API, clunky UI, no Garmin write-back. Liftosaur: best progression DSL, web-first, account-gated. Borrow: Hevy's tap economy, Liftosaur's progression-as-data, wger's exercise schema shape.

## PRP set

```
00-foundation           scaffold, license, pyproject/uv, schema models, validator, CI, pre-commit, Makefile
01-library-program      exercise + workout DSL, seed library (~40 exercises, ~10 workouts incl. posture prelude), 4-week program builder
02-today-checklist      Today screen, checkboxes, adjust, felt-toggle, Done + summary, offline queue, PWA manifest + service worker
03-profiles-settings    Me/Son/Together profiles, Setup + Settings screens, age-band youth rules wired end to end
04-history-scorecard    session/set logs, History, weekly scorecard, streak
05-vitalforge-activity  VitalForge-side branch: POST /api/activity → Garmin strength_training activity, idempotency, tests (mocked Garmin)
06-vitalforge-client    Cadence-side: metrics read + cache, readiness nudge, trend line, write-back on Done with retry queue
07-progression-assess   autoregulation, deload, baseline/retest flow, gap detection, challenge rows
08-ai-ingestion         /api/import, /api/generate via OmniRoute, prompt templates, validator hardening, Settings UI for both
09-deploy               Dockerfile, compose, backups, NPM + Tailscale notes, smoke tests, cadence.grepon.cc
10-polish               son-mode UX pass, badges, low-end Android perf, screenshots, HANDOFF.md
```

Each PRP: implementer subagent → tester subagent (pytest; Playwright smoke at 390px for UI PRPs) → devil's-advocate reviewer (fresh context, diff + PRP only, numbered findings with severity; fix all Highs) → for 00, 05, 06, 08, 09 also a Codex CLI adversarial review on auth, secrets handling, prompt injection via imported/generated workouts, path traversal on import, youth-rule and equipment-whitelist bypass, Garmin write idempotency → update DECISIONS + CHANGELOG → squash-merge to `main`, tag `prp-NN` → 5 lines in BUILD-LOG. The VitalForge-side PRP (05) is done on a branch in the VitalForge clone; never pushed; Cadence tolerates its absence (write-back queues and logs a clear warning).

## Finish

- `make seed && make dev` boots the app with a seed library and two demo profiles.
- One full session headlessly through Done, asserting the `/api/activity` payload against a mocked VitalForge.
- Screenshots of Today (mobile + desktop), Together, History in `docs/screenshots/`.
- `docs/HANDOFF.md`: what shipped, what was descoped, open decisions, the VitalForge branch to review, exact commands to deploy to VM-201 and start day one.

## Hard rules
- Secrets only in `.env`; `.env.example` committed with the URLs above and blank tokens; never echo tokens into logs, docs, or commits.
- Equipment whitelist and youth rules live in the validator and program engine, with tests proving that neither an imported nor an AI-generated workout can bypass them.
- Health data leaves the homelab only via OmniRoute, and only the fields a prompt needs.
- Simplicity beats features. Today must be usable one-handed, on the floor, by a kid, in under 10 seconds per exercise.
