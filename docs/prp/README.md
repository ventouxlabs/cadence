# PRP execution protocol

Each PRP in this directory is executed by three subagents in sequence, on branch `prp/NN-slug`, then squash-merged to `main` and tagged `prp-NN`.

## Shared rules (all roles)

- Read, in this order: `docs/brief.md`, `docs/architecture.md`, the PRP, `docs/exercise-principles.md` (domain PRPs), `docs/vitalforge-contract.md` (05/06), `docs/DECISIONS.md`.
- Python 3.12 via `uv`. Run everything through `uv run ...` (`make test`, `make lint`, `make e2e`).
- Never read `../vitalforge/.env`. Never write a token anywhere. `.env` is git-ignored; `.env.example` has blank secrets.
- Immutability: services return new objects; no in-place mutation of loaded rows outside explicit `update_*` helpers.
- Files ≤ 400 lines (hard max 800); functions ≤ 50 lines; explicit error handling; no `print` debugging left behind.
- Commit on the PRP branch with conventional commits (`feat:`, `fix:`, `test:`, `docs:`, `chore:`). **Never `git push`.** Never touch `main` directly.
- If the PRP contradicts `docs/architecture.md`, architecture wins; note it in your report.
- If something the PRP needs is missing from an earlier PRP, build the smallest shim in this PRP and report it, rather than stopping.

## Implementer (15–25 turns)

1. `git checkout -b prp/NN-slug` from `main` (if the branch exists, continue on it).
2. Build exactly the PRP's scope. Write tests alongside the code (the tester adds more; you own "it runs").
3. `make lint && make test` green before you finish. For UI PRPs also `make e2e`.
4. Report: files touched, what was descoped and why, anything the PRP got wrong, and any new `docs/DECISIONS.md` entries you added (add them yourself, numbered after the last one).

## Tester (10–15 turns)

1. Start from the PRP's **Acceptance tests** section. Every numbered item becomes a test that exists and passes, or a documented gap.
2. Backend: pytest under `tests/`. UI: Playwright under `tests/e2e/` at viewport 390×844 (and 1024×768 where the PRP says tablet). Use the fixtures in `tests/conftest.py`; VitalForge and OmniRoute are always mocked.
3. Fix bugs you find in the implementation only when the fix is ≤ 20 lines and obviously right; otherwise report them.
4. Report: tests added, pass/fail counts, coverage number, gaps.

## Devil's-advocate reviewer (5–10 turns, fresh context)

You see only: the PRP, `docs/architecture.md`, `docs/DECISIONS.md`, and `git diff main...prp/NN-slug`. Return **numbered findings** with severity (`High` = bug, security hole, spec miss, youth/equipment bypass, data loss; `Medium` = quality/maintainability; `Low` = nit). For each: file:line, what's wrong, why it matters, the smallest fix. Then a one-line verdict: `APPROVE` (no High) or `FIX FIRST` (list High numbers). Do not fix anything yourself.

## Codex adversarial review (PRPs 00, 05, 06, 08, 09)

Run from the repo root by the orchestrator: `codex exec --sandbox read-only --skip-git-repo-check "<brief>"`. Brief covers: auth, secrets handling, prompt injection via imported/generated workouts, path traversal on import, youth-rule and equipment-whitelist bypass, Garmin write idempotency. Findings are triaged like the reviewer's.

## Exit criteria per PRP

- Implementer + tester green (`make lint`, `make test`, `make e2e` where applicable).
- Reviewer verdict `APPROVE`, or all Highs fixed and re-checked.
- `docs/DECISIONS.md` and `CHANGELOG.md` updated.
- Squash-merged to `main`, tagged `prp-NN`, pushed (D-002), five lines appended to `docs/BUILD-LOG.md`.
- A PRP that fails review twice is shrunk (scope logged in DECISIONS) and the build continues.
