# PRP-04 — History, weekly scorecard, streak, trend line

Branch `prp/04-history-scorecard`. Depends on PRP-01 (program, `age_band`, `week_of_block`), PRP-02 (`session`, `session_row`, `completion()`), PRP-03 (`display_unit`, youth guard). Consumed by 07 and 10.

## Goal

The only screen that answers "am I actually doing this". A list of what happened, a weekly count against the plan, a streak, and — for the parent only — a 30-day body-composition sparkline. No charts library, no pagination beyond a limit, no analytics.

## Scope in

- `GET /history?profile=me|son`.
- `GET /api/sessions?profile=&limit=`, `GET /api/scorecard?profile=`.
- Streak and scorecard arithmetic in `cadence/historique/`.
- Inline SVG sparkline rendered from `metrics_cache`.
- Youth history variant: fun stats only.

## Scope out

- Filling `metrics_cache` and the VitalForge read — **owned by PRP-06**. This PRP renders whatever is in the table and shows "No data yet" when it is empty or absent.
- `sync_job` writes and retry — **owned by PRP-06**. This PRP renders the badge from `sync_job.status` if the table exists, and shows "Stored locally" if it does not.
- Badges — **owned by PRP-10**, which adds `cadence/historique/badges.py` and a strip on this page. Ship a placeholder container `#badges` with the comment naming PRP-10.
- Assessment history and challenge progress — **owned by PRP-07**.

## Data model

No new tables. Everything is a query.

```python
# cadence/historique/queries.py
def recent_sessions(session, profile_id: str, limit: int = 30) -> list[SessionSummary]: ...
# SessionSummary: id, date, day_type, day_name, rows_done, rows_total, felt,
#                 duration_min, completion, together, sync_status

# cadence/historique/scorecard.py
def weekly_scorecard(session, profile_id: str, today: date) -> Scorecard: ...
# Scorecard: week_start, done_this_week, planned_this_week, days_per_week,
#            current_streak, best_streak, total_sessions, total_rows_ticked

# cadence/historique/trend.py
def body_comp_trend(session, profile_id: str, days: int = 30) -> TrendSeries | None: ...
# TrendSeries: weight_kg: list[(date, float)], body_fat_pct: list[(date, float)]
```

### Streak definition (write this down once; PRP-10's badges reuse it)

A streak counts **consecutive completed planned sessions in program order**, not calendar days.

- Walk `planned_session` for the profile in `(week, day_index)` order, oldest first.
- `status == "done"` **and** the attached session's `completion() == "complete"` → the streak continues, +1.
- `status == "done"` with `completion() == "partial"` → the streak **holds** at its current value and does not reset. A short session is not a failure (§3.7).
- `status == "skipped"` → the streak resets to 0.
- A `planned` row ends the walk; nothing after it counts.
- Together sessions count once per profile, from that profile's own session.
- `current_streak` is the value at the end of the walk; `best_streak` is the maximum reached during it.

`done_this_week` counts sessions whose `finished_at` falls in the current ISO week (Monday start, server local date). `planned_this_week` is the `days_per_week` setting. The count can exceed the plan; render `5 of 4` rather than clamping.

## UI surface

### `GET /history?profile=me` — 390 px

```
┌──────────────────────────────────────┐
│ History            [ Me ][Son][Both] │
├──────────────────────────────────────┤
│ This week                            │
│ ┌──────────────────────────────────┐ │
│ │  3 of 4 sessions                 │ │
│ │  ●●●○                            │ │  filled dot per completed session
│ │  Streak 7   ·   Best 11          │ │
│ └──────────────────────────────────┘ │
├──────────────────────────────────────┤
│ Body composition · 30 days           │  ← parent only
│ ┌──────────────────────────────────┐ │
│ │      ╭─╮                         │ │  inline <svg>, 320×64, two polylines
│ │ ─────╯ ╰──╮      weight  84.1 kg │ │  solid = weight, dashed = body fat
│ │ ··········╰····   body fat 18.2% │ │
│ └──────────────────────────────────┘ │
├──────────────────────────────────────┤
│ Sessions                             │
│ ┌──────────────────────────────────┐ │
│ │ Fri 5 Sep   Upper A              │ │
│ │ 6 of 6 · 28 min · just right  ✓  │ │  ✓ = synced, ⟳ pending, ! failed
│ └──────────────────────────────────┘ │
│ ┌──────────────────────────────────┐ │
│ │ Wed 3 Sep   Lower A     together │ │
│ │ 4 of 5 · 24 min · hard        ⟳  │ │
│ └──────────────────────────────────┘ │
│ ┌──────────────────────────────────┐ │
│ │ Mon 1 Sep   Assessment           │ │
│ │ 6 of 6 · 31 min               ✓  │ │
│ └──────────────────────────────────┘ │
│                                      │
│ [ #badges — placeholder, PRP-10 ]    │
└──────────────────────────────────────┘
```

With fewer than two trend points the whole body-composition card is replaced by one line: `No data yet.` With `metrics_cache.stale` true, the card keeps rendering and adds `Last updated 2 days ago`.

### `GET /history?profile=son` — youth variant

No trend card, no body words, no felt-toggle history (the son's `felt` is recorded but shown as an emoji-free word only in the row).

```
┌──────────────────────────────────────┐
│ History            [ Me ][Son][Both] │
├──────────────────────────────────────┤
│ This week                            │
│ ┌──────────────────────────────────┐ │
│ │  2 of 4 sessions                 │ │
│ │  ●●○○                            │ │
│ │  Streak 5   ·   Best 5           │ │
│ │  184 exercises ticked all-time   │ │
│ └──────────────────────────────────┘ │
├──────────────────────────────────────┤
│ [ #badges — placeholder, PRP-10 ]    │
├──────────────────────────────────────┤
│ Sessions                             │
│ ┌──────────────────────────────────┐ │
│ │ Fri 5 Sep   Play day             │ │
│ │ 5 of 5 · 18 min               ✓  │ │
│ └──────────────────────────────────┘ │
└──────────────────────────────────────┘
```

`?profile=together` renders the two profiles' lists under name headings; a together session appears once in each, tagged `together`.

Partials: `partials/history_list.html`, `partials/scorecard.html`, `partials/trend.html`, `partials/session_row.html`. Tap flow: History is read-only. Tapping a session row expands it in place (HTMX, `partials/session_detail.html`) to show the per-exercise rows done, with one tap to collapse. No navigation away from the page.

### JSON

`GET /api/sessions?profile=me&limit=10`

```json
{"ok": true, "error": null, "meta": {"count": 2, "limit": 10},
 "data": [{"id": "4f1c...", "date": "2026-09-05", "day_type": "upper_a", "day_name": "Upper A",
           "rows_done": 6, "rows_total": 6, "felt": "right", "duration_min": 28,
           "completion": "complete", "together": false, "sync_status": "sent"}]}
```

`GET /api/scorecard?profile=me`

```json
{"ok": true, "error": null, "meta": {},
 "data": {"week_start": "2026-09-01", "done_this_week": 3, "planned_this_week": 4,
          "days_per_week": 4, "current_streak": 7, "best_streak": 11,
          "total_sessions": 34, "total_rows_ticked": 201,
          "trend": {"weight_kg": [["2026-08-08", 85.2], ["2026-09-05", 84.1]],
                    "body_fat_pct": [["2026-08-08", 19.1], ["2026-09-05", 18.2]],
                    "stale": false}}}
```

For a youth profile the `trend` key is **absent**, not null — the serializer must not emit it at all.

`limit` is 1–200, default 30; out of range → 422.

## Implementation notes

- Files: `cadence/historique/{queries.py,scorecard.py,trend.py,sparkline.py}`, `cadence/web/routers/history.py`, `cadence/api/history.py`, templates and partials above.
- **Sparkline** is generated server-side in `sparkline.py` as an SVG string: `viewBox="0 0 320 64"`, `preserveAspectRatio="none"` off, two `<polyline>` elements with `vector-effect="non-scaling-stroke"`, colours from CSS custom properties so the stylesheet owns them. Normalise each series to its own min/max with a 5 % vertical pad; a flat series renders as a centred straight line rather than dividing by zero. Fewer than two points → return `None`, the template hides the card. Add `role="img"` and an `aria-label` reading the first and last values.
- **Two series, two units.** Weight in kg from `metrics/weight` (VitalForge returns grams — PRP-06 divides by 1000 before caching), body fat in percent. They share an x-axis of dates, not indices, so a gap in the data does not compress the line.
- **`metrics_cache` may not exist yet.** Guard the read with a table-exists check or a caught `OperationalError`, and fall through to "No data yet". The same for `sync_job`: absent → every badge reads "Stored locally".
- Queries: one `SELECT` for the session list joined to `planned_session` and left-joined to `sync_job`; one aggregate for totals. No N+1 — the session list must not query `session_row` per session; use a grouped count. Cap the list at `limit` in SQL, never in Python.
- Dates render as `%a %-d %b` in server local time. Store and compare in UTC; format at the edge.
- Youth guard: the route builds the context with `trend=None` for any `profile.kind == "youth"` before the template is reached, so a template bug cannot leak it.

## Acceptance tests

`tests/test_historique.py`, `tests/test_history_api.py`, `tests/e2e/test_history.py`.

1. `test_streak_simple` — five consecutive complete sessions → `current_streak == 5`.
2. `test_streak_reset_on_skip` — complete, complete, skipped, complete → current 1, best 2.
3. `test_streak_holds_on_partial` — complete, partial, complete → current 2, best 2. The partial neither increments nor resets.
4. `test_streak_stops_at_first_planned` — a `planned` row after two `done` rows ends the walk; later `done` rows (impossible in practice) are ignored.
5. `test_streak_first_week` — a profile with zero sessions → current 0, best 0, and no exception.
6. `test_streak_together_counts_once_per_profile` — one together group of two sessions gives each profile +1, not +2.
7. `test_scorecard_counts_current_week` — sessions on Sunday and Monday fall in different ISO weeks.
8. `test_scorecard_can_exceed_plan` — 5 done against `days_per_week=4` renders `5 of 4`, not `4 of 4`.
9. `test_scorecard_youth_omits_trend` — the `trend` key is absent from the son's payload.
10. `test_total_rows_ticked` — counts `session_row.done` across all sessions for the profile.
11. `test_sparkline_two_points` — returns an SVG string containing two `<polyline>` elements.
12. **Negative** `test_sparkline_one_point_returns_none` — and the template omits the card.
13. `test_sparkline_flat_series` — identical values render a centred line and do not raise `ZeroDivisionError`.
14. `test_history_without_metrics_cache` — with the table empty, `GET /history` is 200 and contains "No data yet".
15. `test_history_without_sync_job_table` — badges read "Stored locally", no 500.
16. **Negative** `test_limit_out_of_range` — `limit=0` and `limit=500` → 422.
17. **Negative** `test_unknown_profile` — `?profile=nobody` → 404 with the envelope's `error` set.
18. `test_no_n_plus_one` — count statements with a SQLAlchemy event listener and assert the count for a 1-session fixture **equals** the count for a 30-session fixture. Never assert a literal number: session setup and the `sync_job` join make an absolute count brittle.
19. `e2e_history_renders_sessions` — 390×844, three session cards visible, each showing "N of M".
20. `e2e_sparkline_visible_for_parent` — `svg[data-role="trend"]` present with two polylines after seeding two cache points.
21. `e2e_no_trend_line_for_son` — the same selector is absent on `?profile=son`.
22. `e2e_session_row_expands_in_place` — tapping a card reveals per-exercise rows without a navigation event.
23. `e2e_youth_history_has_no_body_words` — the banned-phrase check from PRP-03 run against the son's `/history` DOM.

## Devil's-advocate risks

1. **Streak defined three different ways** across this PRP, PRP-07's missed-session logic and PRP-10's badges. Mitigation: one function, `scorecard.current_streak`, imported by both; the rules table above is the single statement of it; tests 1–6 pin every branch.
2. **Partial sessions breaking the streak**, punishing the exact behaviour §3.7 encourages. Mitigation: test 3.
3. **Calendar assumptions.** The plan is a queue, not a calendar (D-011), so a "days in a row" streak would read as broken after a rest day. Mitigation: the streak counts sessions in program order; the wording on screen is "Streak 7", never "7 days".
4. **ISO week boundary.** A Sunday-evening session landing in next week, or a UTC/local mismatch putting it in the wrong one. Mitigation: test 7 with explicit timestamps either side of midnight.
5. **Trend leaking to the son** through a template that forgets the guard. Mitigation: the route nulls it before rendering, plus tests 9 and 21.
6. **Grams vs kilograms.** VitalForge's `metrics/weight` is grams (contract §2.2). A raw render shows 84100 kg. Mitigation: the conversion is PRP-06's, stated here so the renderer asserts a plausible range and the seeded test fixture uses kg.
7. **N+1 on the session list** — 30 sessions, 31 queries, slow on a cheap phone over Tailscale. Mitigation: test 18.
8. **Chart library creeping in.** Mitigation: inline SVG only; no new dependency in `pyproject.toml` for this PRP, and the reviewer should treat one as a High.
9. **Sync badge implying success before PRP-06 exists.** Mitigation: absent table → "Stored locally", never "Synced"; test 15.
10. **Together sessions double-counted** in `total_sessions` for the `together` view. Mitigation: test 6, and the together view renders two independent lists rather than a merged one.

## Done when

- [ ] `/history` renders for `me`, `son` and `together` with scorecard, session list and expandable rows.
- [ ] Streak follows the six rules above and is computed in exactly one place.
- [ ] The sparkline is inline SVG, hides below two points, and never appears on a youth profile.
- [ ] The page degrades cleanly with no `metrics_cache` and no `sync_job`.
- [ ] `#badges` placeholder container exists for PRP-10.
- [ ] All 23 acceptance tests pass; `make lint && make test && make e2e` green.
