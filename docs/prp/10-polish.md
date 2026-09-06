# PRP-10 — Son-mode pass, badges, performance, screenshots, handoff

Branch `prp/10-polish`. Last PRP. Depends on 01–09. Nothing depends on it.

## Goal

Make the son's half of the app feel like it was built for a kid rather than derived from an adult screen, give both profiles something to collect, prove the page is small enough for a cheap phone, take the screenshots, and write the handoff that lets JD run day one without reading the code.

## Scope in

- Son-mode UX pass on Today, Done and History.
- Badges: `cadence/historique/badges.py`, computed on read, rendered into PRP-04's `#badges` container.
- Performance budget and `make perf`.
- Screenshots into `docs/screenshots/` and `make screenshots`.
- `docs/HANDOFF.md` and a README refresh.

## Scope out

- Any new feature, route or table. If a fix needs one, it is a bug fix in the owning PRP's module, kept small and reported.
- Changing seed content, youth rules, autoregulation or the validator.
- Deployment — **owned by PRP-09**. This PRP only quotes its commands in HANDOFF.

## Data model

None. Badges are derived on every read from `session`, `session_row`, `planned_session` and `challenge`. Nothing is stored, so a rule change is never a migration.

## Badge rules

```python
# cadence/historique/badges.py
@dataclass(frozen=True)
class Badge:
    id: str; name: str; earned: bool; earned_on: date | None; youth_safe: bool

def badges_for(session, profile_id: str) -> list[Badge]: ...
```

| id | name (adult) | name (youth) | earned when |
|---|---|---|---|
| `first-session` | First session | You started! | ≥ 1 session with `completion == "complete"` |
| `streak-3` | Three in a row | Three in a row! | `best_streak >= 3` |
| `streak-7` | Seven in a row | Seven in a row! | `best_streak >= 7` |
| `sessions-10` | Ten sessions | Ten workouts! | ≥ 10 complete sessions |
| `sessions-25` | Twenty-five sessions | Twenty-five workouts! | ≥ 25 complete sessions |
| `prelude-4-weeks` | Prelude, four weeks | Warm-up champion | every prelude row ticked in every complete session across four consecutive ISO weeks, each week having ≥ 1 session |
| `challenge-met` | Challenge met | You did it! | ≥ 1 `challenge` with `status == "met"` |

Streaks come from PRP-04's `scorecard.current_streak` / `best_streak` — do not re-derive them. Every badge is `youth_safe`; nothing here references body composition, so the youth filter is a no-op today but exists so a future badge cannot leak.

`prelude-4-weeks` needs care: "four consecutive weeks" means four ISO weeks with no gap, each containing at least one complete session, and in every complete session in that window every row with `role == "prelude"` is `done`.

## UI surface

### Son-mode Today — 390 px

Same structure as PRP-02, three differences: type scale up one step, an inline SVG icon on locomotion and play rows, and the "Good enough — done!" control given real visual weight.

```
┌──────────────────────────────────────┐
│ Cadence            [ Me ][*Son*][Both]│
├──────────────────────────────────────┤
│ Play day · today                     │
│ Five things. Go.                     │  ← copy, not stats
├──────────────────────────────────────┤
│ ┌──────────────────────────────────┐ │
│ │ ☐  🦀  Crab walk                 │ │  inline SVG, 32px, currentColor
│ │       2 × 10 m                   │ │  row min-height 64px in son mode
│ │       Hips high, walk sideways.  │ │  18px name, 16px detail
│ └──────────────────────────────────┘ │
│ ┌──────────────────────────────────┐ │
│ │ ☑  ⭐  Jumping jacks             │ │  tick animation: 200ms scale+fade
│ │       2 × 20                     │ │  on the ✓ only, no confetti
│ └──────────────────────────────────┘ │
│ ┌──────────────────────────────────┐ │
│ │ ☐  🐻  Bear crawl                │ │
│ └──────────────────────────────────┘ │
├──────────────────────────────────────┤
│   ┌──────────────────────────────┐   │
│   │    Good enough — done!       │   │  filled, 64px, promoted after
│   └──────────────────────────────┘   │  the band threshold
└──────────────────────────────────────┘
```

Icons are hand-written inline SVG in `cadence/web/templates/partials/icons.html`, one `<symbol>` per exercise family (crawl, jump, balance, throw, hang, carry, squat, press, pull, brace, mobility), referenced with `<use>`. No emoji in the markup — the sketch above uses them only to show placement. No icon font, no image files, `fill="currentColor"` so themes work.

### Son-mode Done

```
┌──────────────────────────────────────┐
│              Nice one. ✓             │
│                                      │
│  18 minutes                          │
│  5 of 5 things done                  │
│  Next time: bear crawl 2 × 12 m      │
│                                      │
│  🏅 New badge: Three in a row!       │
│                                      │
│  [ Back ]              [ History ]   │
└──────────────────────────────────────┘
```

### Badges strip on History

```
├──────────────────────────────────────┤
│ Badges                               │
│ ┌────┐┌────┐┌────┐┌────┐┌────┐┌────┐ │
│ │ ✓  ││ ✓  ││ ✓  ││    ││    ││    │ │  earned = filled, unearned = outline
│ │1st ││ 3  ││ 10 ││ 7  ││ 25 ││warm│ │  56×56 each, wraps to two rows
│ └────┘└────┘└────┘└────┘└────┘└────┘ │
│ Three in a row! · earned 3 Sep       │  ← tapping one shows its line here
└──────────────────────────────────────┘
```

Rendered into PRP-04's `#badges` container via `partials/badges.html`. Tapping a badge swaps the caption line only (HTMX target `#badge-caption`), never navigates.

Copy rules for son mode: second person, present tense, no numbers about his body, no comparison to the parent, no failure language. "Good enough — done!" is the only exit wording. Never "skipped", "incomplete" or "missed".

## Implementation notes

- Files: `cadence/historique/badges.py`, `cadence/web/templates/partials/{badges.html,icons.html}`, son-mode CSS in `cadence/web/static/style.css` under a single `[data-profile-kind="youth"]` scope, `scripts/perf.py`, `scripts/screenshots.py`, `docs/HANDOFF.md`, `README.md`, Makefile targets `perf` and `screenshots`.
- Son mode is a CSS scope, not a second template tree. The `<body>` carries `data-profile-kind="youth"`; every son-mode rule lives under that attribute selector. No duplicated Jinja.
- The tick animation is ≤ 300 ms, applies to the check glyph only, and is wrapped in `@media (prefers-reduced-motion: no-preference)`. No layout-affecting property is animated — `transform` and `opacity` only, so it cannot cause layout shift.

### Performance budget

All figures are **transfer bytes with `Accept-Encoding: gzip`**, measured on a cold `GET /today` with an empty cache.

| Asset | Ceiling |
|---|---|
| HTML document | 30 KB |
| JavaScript total (vendored HTMX + `app.js`) | 25 KB |
| CSS total | 10 KB |
| Icons and manifest | 5 KB |
| **Total for the page, hard gate** | **60 KB** |

The per-asset ceilings sum above the total on purpose: the 60 KB aggregate from architecture §5 is the gate that fails the build, and the per-asset lines are ceilings no single file may cross. Uncompressed, vendored HTMX alone is roughly 48 KB — the implementer must record the real number from `ls -l` in `scripts/perf.py`'s output — so `GZipMiddleware` is mandatory and `make perf` fails loudly if a response arrives without `Content-Encoding: gzip`.

`make perf` starts the app, requests `/today` for `me` and for `son`, follows every subresource, prints a table of transfer and raw sizes, and exits non-zero on any breach. It also asserts zero external hosts in the response bodies.

Layout shift: every image and SVG has explicit `width`/`height`, the sticky Done button is `position: sticky` in normal flow rather than injected after load, and no font is loaded over the network (system stack only). `make perf` asserts no `@font-face` and no `<link rel=preconnect>` exist.

### Screenshots

`scripts/screenshots.py` drives Playwright against a seeded temporary database, so the images are reproducible and contain no real data.

| File | Viewport | Screen |
|---|---|---|
| `today-mobile.png` | 390×844 | `/today?profile=me`, two rows ticked |
| `today-desktop.png` | 1280×800 | `/today?profile=me` |
| `together-mobile.png` | 390×844 | `/today?profile=together` |
| `together-tablet.png` | 1024×768 | `/today?profile=together` |
| `history-mobile.png` | 390×844 | `/history?profile=me` with a seeded trend |
| `setup-mobile.png` | 390×844 | `/setup` |

Add `son-today-mobile.png` at 390×844 so the son-mode pass is visible in review. Deterministic: fix the clock with a seeded `started_at`, disable the tick animation via `prefers-reduced-motion`, and seed the metrics cache with fixed values. `make screenshots` writes them locally; CI runs the same script and uploads `docs/screenshots/` as an artifact.

### `docs/HANDOFF.md` outline

1. **What Cadence is** — three sentences and one screenshot.
2. **What shipped, per PRP** — one line each for 00–10, with the tag.
3. **What was descoped** — pulled from each PRP's report, with the reason.
4. **Open decisions** — every `docs/DECISIONS.md` entry that is a default JD never confirmed, listed with how to change it. Call out D-009 (no login), D-015 (son's Garmin push under the parent credential), D-016 (retry policy) and D-018 (`garmin_category` nulls) explicitly.
5. **The VitalForge branch to review** — the PRP-05 branch name in `../vitalforge`, what it adds, why it is unpushed (D-005), and the exact `git log`/`git diff` commands to read it.
6. **Deploy to VM-201** — the exact commands from PRP-09, verbatim, including the `.env` keys to fill and the healthcheck to watch.
7. **Day one** — run setup; set the son's age; do the baseline assessment; do the first session; check it landed in VitalForge and in Garmin Connect.
8. **Where things live** — the module map from architecture §2 in one table.
9. **Known limitations** — carry rows are tick-only (PRP-02); generated workouts are stored but not auto-scheduled (PRP-08); the son's readiness is permanently null on one Garmin credential (contract §2.3).

README gets: what it is, a screenshot, `make seed && make dev`, the settings table, the AGPL-3.0 notice, and a link to HANDOFF.

## Acceptance tests

`tests/test_badges.py`, `tests/test_perf.py`, `tests/e2e/test_son_mode.py`.

1. `test_first_session_badge` — one complete session earns it; a partial-only history does not.
2. `test_streak_badges` — best streak 3 earns `streak-3` and not `streak-7`; 7 earns both.
3. `test_session_count_badges` — 10 and 25 thresholds, counting complete sessions only.
4. `test_prelude_four_weeks` — four consecutive ISO weeks each with a session and every prelude row ticked → earned.
5. **Negative** `test_prelude_four_weeks_gap` — a missing week breaks it, even with five qualifying weeks either side.
6. **Negative** `test_prelude_four_weeks_one_missed_prelude_row` — one unticked prelude row in one session breaks it.
7. `test_challenge_met_badge` — a `met` challenge earns it.
8. `test_badges_are_derived_not_stored` — no new table, and calling `badges_for` twice returns equal results with no writes (assert with a read-only session).
9. `test_badges_youth_safe` — every badge name passes PRP-03's banned-phrase check at all three youth bands.
10. `test_badge_earned_on_date` — the date is the qualifying session's `finished_at` date, not today.
11. `test_perf_budget_today_me` — transfer bytes for `/today?profile=me` under 60 KB total, and each asset under its ceiling.
12. `test_perf_budget_today_son` — the same for the son, whose bigger type and icons must not push it over.
13. **Negative** `test_perf_fails_without_gzip` — with the middleware disabled, `make perf`'s checker returns non-zero.
14. `test_no_external_hosts` — no response body contains `//cdn`, `https://unpkg`, `fonts.googleapis` or any `http` scheme pointing off-origin.
15. `test_no_webfont` — no `@font-face` and no `preconnect` in the served CSS or HTML.
16. `e2e_son_mode_type_scale` — the son's first row name renders at a larger computed `font-size` than the parent's.
17. `e2e_son_mode_done_prominent` — after the band threshold the Done button's bounding box is taller than before and its label reads "Good enough — done!".
18. `e2e_son_mode_icons_present` — at least one `<use>` reference resolves on a play row, and no `<img>` is used for it.
19. `e2e_tick_animation_under_300ms` — the animation's computed duration is ≤ 300 ms and no layout shift occurs (compare the row's bounding box before and after).
20. `e2e_reduced_motion_disables_animation` — with `reduced_motion="reduce"` the computed duration is 0.
21. `e2e_badges_render_on_history` — earned badges are filled, unearned outlined; tapping one changes only `#badge-caption`.
22. `e2e_screenshots_script_writes_seven_files` — running `scripts/screenshots.py` against a temp DB produces the seven named PNGs at the right dimensions.
23. `test_handoff_sections_present` — `docs/HANDOFF.md` contains all nine headings above; a cheap grep test that keeps the doc from rotting.
24. `test_readme_has_quickstart` — `README.md` contains `make seed` and `make dev`.

## Devil's-advocate risks

1. **Son mode forked into a second template tree**, doubling every future change. Mitigation: one CSS scope, no duplicated Jinja; the reviewer should treat a `son_today.html` as a High.
2. **Bigger type and icons blowing the byte budget.** Mitigation: test 12 measures the son's page specifically; icons are inline `<symbol>` reused by `<use>`, so the cost is paid once.
3. **Badges stored, then a rule change needs a migration and a backfill.** Mitigation: derived only, test 8.
4. **Badge streak logic re-derived** and drifting from PRP-04's. Mitigation: import `scorecard`, never re-walk sessions; a test asserts the badge module contains no session query for streaks.
5. **`prelude-4-weeks` quietly always false** because prelude rows are excluded from the row count somewhere upstream. Mitigation: tests 4–6 build the fixture explicitly, including the negative cases.
6. **Screenshots capturing real data** if run against `data/cadence.db`. Mitigation: the script builds its own temp DB from `make seed` and refuses to run if `CADENCE_DB` points at the default path.
7. **Flaky screenshots** from the tick animation or a live clock. Mitigation: reduced motion forced, clock seeded, metrics cache fixed.
8. **The perf gate passing locally and failing on the VM** because the local server gzips and Nginx Proxy Manager re-encodes. Mitigation: `make perf` asserts `Content-Encoding: gzip` on the app's own responses and HANDOFF notes to re-run it against `cadence.grepon.cc` after deploy.
9. **A polish change breaking a youth rule** — a nicer stepper that lets the load go up. Mitigation: the whole suite runs; PRP-07's cap property test is the backstop, and this PRP adds no server-side behaviour.
10. **HANDOFF written from memory** rather than from the PRP reports, inventing what shipped. Mitigation: section 2 is assembled from `docs/BUILD-LOG.md` and the tags, section 3 from each PRP's implementer report, and test 23 keeps the headings present.
11. **Confetti creeping back in** as "just a little celebration" that costs 30 KB of JavaScript. Mitigation: explicitly out of scope; a ≤ 300 ms check animation is the whole reward; test 11 would catch the weight anyway.

## Done when

- [ ] The son's Today, Done and History read like they were written for a kid, with no body or failure language anywhere.
- [ ] Seven badges compute correctly, are derived not stored, and render in PRP-04's container.
- [ ] `make perf` passes: `/today` under 60 KB transferred for both profiles, no external host, no webfont, no layout shift.
- [ ] `make screenshots` writes the seven PNGs; CI uploads them as an artifact.
- [ ] `docs/HANDOFF.md` has all nine sections, with the real VitalForge branch name and the real deploy commands from PRP-09.
- [ ] `README.md` refreshed.
- [ ] All 24 acceptance tests pass; `make lint && make test && make e2e` green.
