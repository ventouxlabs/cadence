# PRP-02 — Today checklist, Done, offline PWA

Branch `prp/02-today-checklist`. Depends on PRP-00 (app factory, schema, validator) and PRP-01 (library, program, `age_band`, `materialise_rows`). Consumed by 03, 04, 06, 07, 10.

## Goal

The product. Open the app, see today's session as a checklist, tick rows with one thumb, adjust a rep or a load in two taps, hit Done, get three lines back. It works with the network off and syncs when it returns. Under 10 seconds per exercise on a cheap Android phone.

## Scope in

- `GET /today?profile=me|son|together` and its HTMX partials: tick, adjust, felt, done.
- `session` and `session_row` tables; session lifecycle; `good_enough_done` exit; together mode.
- `GET /done/{session_id}` summary.
- `GET /api/today`, `POST /api/sessions/{id}/rows/{position}`, `POST /api/sessions/{id}/done` — idempotent, the offline replay targets.
- PWA: `manifest.json`, `sw.js` at the origin root, icons, IndexedDB queue in `static/app.js`, vendored HTMX.
- Per-exercise timer, vanilla JS, zero server calls.

## Scope out

- Progression maths and the real "next time" note — **owned by PRP-07**. This PRP ships `programme.next_time_note(session) -> str` returning a fixed placeholder.
- Readiness nudge text, sync status wording beyond "stored locally", `sync_job` — **owned by PRP-06**.
- History and scorecard — **owned by PRP-04**. Settings and `display_unit` — **owned by PRP-03** (read it if present, default `kg`).
- Son-mode visual pass, badges, perf budget script — **owned by PRP-10**.

## Deviations from architecture (log in `docs/DECISIONS.md` as the first commit)

1. **The session row is created on first render of `/today`, not on first tick.** Architecture §3 says first tick. The offline queue needs a durable `session_id` before the first tap, otherwise a tick taken offline has nothing to address. `started_at` stays `NULL` until the first tick, so "when did the session begin" is unchanged.
2. **Session completion is derived, not stored.** `planned_session.status` keeps architecture's `planned|done|skipped`. A helper in `cadence/seance/status.py` decides `complete` vs `partial`.
3. **`create_app()` gains `GZipMiddleware`.** PRP-00 states "no middleware". The 60 KB page budget in architecture §5 is unreachable with vendored HTMX served uncompressed, so this is a two-line shim in PRP-00's file, declared here and reported by the implementer.

> **Seam correction.** PRP-00's deferral table assigns `session` and `session_row` to PRP-04. They belong here: this PRP creates a session on first render and ticks its rows. PRP-04 only queries them.

## Data model

New tables, columns exactly as architecture §3. `session.id` is a `uuid4` string and is also the VitalForge `session_id`.

```python
# cadence/seance/status.py
def rows_done(session) -> int: ...
def completion(session, band_rules) -> Literal["complete", "partial"]:
    """complete iff rows_done >= rules.good_enough_done_after_n_exercises, else partial."""
```

Adult threshold is 1, so one tick makes an adult session `complete`. PRP-04 uses this for streaks and PRP-07 for the R7 exclusion.

**Carry rows.** `session_row` has no metres column. Distance lives in `rows_json` only; a row whose `measure == "meters"` is tick-only (no adjust affordance) and its planned distance is taken as done. Stated here so PRP-06 reads distance from `rows_json` when building the activity payload.

Row state transitions: `done=False` → tick → `done=True, done_at=now` → tick again → `done=False, done_at=None`. Adjust writes `reps_done` / `load_done_kg` and never changes `done`.

## UI surface

### `GET /today?profile=me` — 390 px

```
┌──────────────────────────────────────┐
│ Cadence            [ Me ][Son][Both] │  profile switcher, 44px tall
├──────────────────────────────────────┤
│ Upper A · week 2 · 30 min            │
│ Feeling fresh today.                 │  ← readiness line, PRP-06; hidden if absent
├──────────────────────────────────────┤
│ PRELUDE                              │
│ ┌──────────────────────────────────┐ │
│ │ ☐  Cat-cow                       │ │  whole row is the tap target,
│ │    8 reps                        │ │  min height 56px
│ │    Move one vertebra at a time.  │ │
│ └──────────────────────────────────┘ │
│ ┌──────────────────────────────────┐ │
│ │ ☑  Open book              ⏱      │ │  ticked row: strikethrough + dimmed
│ │    6 reps per side               │ │
│ └──────────────────────────────────┘ │
├──────────────────────────────────────┤
│ WORKOUT                              │
│ ┌──────────────────────────────────┐ │
│ │ ☐  DB bench press          ✎     │ │  ✎ = adjust, 56×56 hit area
│ │    3 × 8  ·  14 kg               │ │
│ │    Shoulder blades pinned.       │ │
│ └──────────────────────────────────┘ │
│ ┌──────────────────────────────────┐ │
│ │ ☐  Plank                   ⏱  ✎  │ │  ⏱ only on seconds / rest_s rows
│ │    3 × 40 s                      │ │
│ └──────────────────────────────────┘ │
├──────────────────────────────────────┤
│        ┌────────────────────┐        │
│        │       Done         │        │  56px, always visible, sticky bottom
│        └────────────────────┘        │
└──────────────────────────────────────┘
```

Youth profile, after the band's `good_enough_done_after_n_exercises` rows are ticked, the button promotes in place:

```
│        ┌────────────────────┐        │
│        │ Good enough — done!│        │  larger, filled, same position
│        └────────────────────┘        │
```

Below the threshold a youth session still shows a plain `Done` button — §3.7, P6: it is always tappable.

### Adjust, inline (≤ 2 taps)

Tap `✎` → the row expands in place. Tap `+` or `−` once → value written, partial re-rendered. That is two taps total.

```
│ ┌──────────────────────────────────┐ │
│ │ ☐  DB bench press          ✕     │ │
│ │    3 × 8  ·  14 kg               │ │
│ │    reps  [ − ]   8   [ + ]       │ │  each button 56×56
│ │    load  [ − ] 14 kg [ + ]       │ │  steps the ladder, not free text
│ └──────────────────────────────────┘ │
```

Load steps move one rung on the profile's ladder (PRP-01 `parse_weights_available`) and are clamped to the youth cap; a clamped step shows the row's `notes` line instead of moving.

### Felt toggle — appears when every row is ticked, and inside the Done confirm otherwise

```
│  How did that feel?                  │
│  ┌────────┬────────────┬──────────┐  │
│  │  Easy  │ Just right │   Hard   │  │  three 56px segments
│  └────────┴────────────┴──────────┘  │
```

"Done anyway" path: tapping `Done` with rows outstanding shows the same three-way inline above the button rather than a modal, then a second `Done` tap finalises. Never blocks: `felt` may stay null.

### `GET /done/{session_id}`

```
┌──────────────────────────────────────┐
│              Done. ✓                 │
│                                      │
│  28 minutes                          │
│  6 of 6 exercises                    │
│  Next time: goblet squat 3×10 @ 14kg │  ← placeholder until PRP-07
│                                      │
│  Stored locally.                     │  ← PRP-06 replaces with sync status
│                                      │
│  [ Back to today ]   [ History ]     │
└──────────────────────────────────────┘
```

### Together mode — `?profile=together`

Phone (< 768 px): son's checklist stacked under the parent's, each under a name heading, one shared Done at the bottom. Tablet (≥ 768 px): two columns, `grid-template-columns: 1fr 1fr`, same single Done.

```
┌──────────────────────────────────────┐   ┌───────────────────┬───────────────────┐
│ Cadence           [Me][Son][ Both ]  │   │ JD                │ Son               │
├──────────────────────────────────────┤   ├───────────────────┼───────────────────┤
│ JD — Lower/Full B                    │   │ ☐ Goblet squat    │ ☐ Goblet squat    │
│ ☐ Goblet squat      3 × 10 · 20 kg   │   │   3 × 10 · 20 kg  │   2 × 10 · 8 kg   │
│ ☐ Push-up           3 × 12           │   │ ☐ Push-up 3 × 12  │ ☐ Push-up 2 × 8   │
├──────────────────────────────────────┤   └───────────────────┴───────────────────┘
│ Son — Lower/Full B                   │              ≥ 768 px
│ ☐ Goblet squat      2 × 10 · 8 kg    │
│ ☐ Push-up           2 × 8            │
├──────────────────────────────────────┤
│            [    Done    ]            │
└──────────────────────────────────────┘
```

Two `session` rows share a `together_group_id` (uuid4). One `POST /today/{id}/done` on either finalises both; each keeps its own rows, `felt`, duration and write-back.

### Routes and partials

| Route | Returns | Partial template |
|---|---|---|
| `GET /today` | full page | `today.html` |
| `POST /today/{sid}/rows/{pos}/tick` | one row | `partials/row.html` |
| `POST /today/{sid}/rows/{pos}/adjust` | one row (expanded or collapsed) | `partials/row_adjust.html`, `partials/row.html` |
| `POST /today/{sid}/felt` | the felt block + Done button | `partials/felt.html` |
| `POST /today/{sid}/done` | 303 → `/done/{sid}` | — |
| `GET /done/{sid}` | full page | `done.html` |

Templates live in `cadence/web/templates/`; partials in `cadence/web/templates/partials/`. HTMX targets are `#row-{position}` with `hx-swap="outerHTML"`.

### JSON

`GET /api/today?profile=me`

```json
{"ok": true, "error": null, "meta": {"generated_at": "2026-09-06T08:00:00+00:00"},
 "data": {"session_id": "4f1c...", "profile": "me", "day_type": "upper_a", "week": 2,
          "display_unit": "kg", "good_enough_after": 1, "together_group_id": null,
          "rows": [{"position": 1, "exercise_id": "cat-cow", "name": "Cat-cow", "role": "prelude",
                    "sets": 1, "reps": 8, "seconds": null, "meters": null, "per_side": false,
                    "rest_s": 0, "load_kg": null, "measure": "reps", "done": false,
                    "cue": "Move one vertebra at a time.", "is_challenge": false, "notes": []}]}}
```

`POST /api/sessions/{id}/rows/{position}` — body `{"done": true, "reps_done": 9, "load_done_kg": 15.0, "ts": "2026-09-06T08:12:00+00:00"}`. All fields optional; only those present are written. **Idempotent**: replaying the same body is a no-op returning the current row. A `ts` older than the stored `done_at` is ignored (last-write-wins by client timestamp) so a queue replay cannot resurrect a stale state.

`POST /api/sessions/{id}/done` — body `{"felt": "right", "ts": "..."}`. Returns `{"ok": true, "data": {"session_id": "...", "duration_min": 28, "rows_done": 6, "rows_total": 6, "completion": "complete", "next_time_note": "...", "sync": "local"}}`. Calling it on an already-finished session returns the same body with HTTP 200 and changes nothing.

## Implementation notes

- Files: `cadence/seance/{tables.py,today.py,ticks.py,done.py,status.py}`, `cadence/web/routers/today.py`, `cadence/api/today.py`, `cadence/api/sessions.py`, templates and `cadence/web/static/{app.js,style.css,htmx.min.js,icons/}`, plus `cadence/web/static/manifest.json`.
- **`sw.js` must be served from the origin root**, not `/static/`. A worker scoped at `/static/` cannot control `/today`. Add `GET /sw.js` in `cadence/web/routers/today.py` returning `cadence/web/static/sw.js` with `Content-Type: application/javascript` and `Service-Worker-Allowed: /`. Register with `navigator.serviceWorker.register('/sw.js', {scope: '/'})`.
- **Service worker strategy**: precache `/static/htmx.min.js`, `/static/app.js`, `/static/style.css`, the icons and `/static/manifest.json` on `install`. Network-first with cache fallback for `/today*` and `/api/today*`; cache-first for everything under `/static/`. Bump a `CACHE_VERSION` constant on every change and delete stale caches on `activate`.
- **Offline queue** in `static/app.js`: IndexedDB database `cadence-queue`, one object store `ops` keyed by autoincrement, records `{session_id, kind: "row"|"done", position, patch, ts}`. Apply the change to the DOM immediately (optimistic), enqueue, then try the POST. On success delete the record. Replay on `window.online` and on page load, oldest first, stopping on the first network failure. Show a one-line "Saved on this phone — will sync" banner while the queue is non-empty.
- **HTMX**: download 2.x once into `cadence/web/static/htmx.min.js` and pin the exact version and SRI-style note in a header comment (`/* htmx 2.0.x — vendored <date>, https://unpkg.com/htmx.org@2.0.x/dist/htmx.min.js */`). No CDN, no build step (D-012).
- **Page-weight budget**: `/today` uncached must stay under **60 KB transferred** (architecture §5), measured gzipped. Vendored HTMX is roughly 48 KB raw and ~14 KB gzipped, so gzip is mandatory: `create_app()` needs `GZipMiddleware(minimum_size=500)`. That file belongs to PRP-00 — add it here as a two-line shim and report it. PRP-10 owns the enforcement script.
- **Timer**: pure vanilla, one `setInterval` at a time, `requestAnimationFrame`-free. Off by default; the `⏱` control appears only on rows with `seconds` or a non-zero `rest_s`; one tap starts, a second stops. State lives in the DOM, never on the server. Respect `prefers-reduced-motion` for the tick animation.
- **`GET /today` makes no network call** (architecture §5). Readiness comes from `metrics_cache` if the table exists; guard the read so this PRP works before PRP-06 lands.
- **First render** resolves the first `planned_session` with `status == "planned"` for the profile, in `(week, day_index)` order, creates the `session` and its `session_row`s from `rows_json` inside one transaction, and is safe under concurrent requests: unique index on `(planned_session_id)` for unfinished sessions, and on conflict re-read rather than insert.
- **Together** resolves both profiles' next planned sessions, generating both, and stamps a shared `together_group_id`. If one profile has no planned session, render the other and say so in one line.
- Accessibility: each row is a `<label>` wrapping a real `<input type="checkbox">` with `aria-checked` kept in sync, visible focus ring, logical tab order (row → adjust → next row), the Done button last. Segmented controls use `role="radiogroup"`. Minimum contrast 4.5:1.

## Acceptance tests

Backend `tests/test_today.py`, `tests/test_sessions_api.py`. UI `tests/e2e/test_today.py` at 390×844 unless stated.

1. `test_today_creates_session_on_first_render` — one `session` and N `session_row`s exist after `GET /today`; `started_at is None`.
2. `test_today_is_idempotent_on_reload` — a second `GET /today` reuses the same `session_id`.
3. `test_first_tick_sets_started_at` — and a second tick does not move it.
4. `test_tick_toggles_and_is_idempotent` — replaying the identical POST leaves `done=True` and one `done_at`.
5. **Negative** `test_stale_tick_ignored` — a POST whose `ts` predates `done_at` does not change the row.
6. `test_adjust_writes_done_fields_only` — `reps_done` changes, `done` and `reps_planned` do not.
7. **Negative** `test_adjust_load_clamped_to_youth_cap` — a `son` at `age_10_13` cannot adjust `goblet-squat` above 8.0 kg; the response carries the notes line.
8. **Negative** `test_adjust_rejected_on_carry_row` — a `meters` row returns 400 with `error` set.
9. `test_done_finalises` — `finished_at`, `duration_min`, `felt` written; `planned_session.status == "done"`; response `completion == "complete"`.
10. `test_done_is_idempotent` — a second POST returns 200 and identical data, and does not change `finished_at`.
11. `test_partial_completion_for_youth` — a `u10` session with 2 of 5 rows ticked finalises with `completion == "partial"`.
12. `test_together_creates_two_linked_sessions` — same `together_group_id`, different `profile_id`; one Done finalises both.
13. `test_api_today_shape` — matches the envelope above; `display_unit` defaults to `"kg"` when the setting is absent.
14. `test_no_network_call_on_today` — patch the VitalForge client to raise on any call; `GET /today` still returns 200.
15. **Negative** `test_row_position_out_of_range` — `POST /api/sessions/{id}/rows/99` → 404 with the envelope's `error` set.
16. **Negative** `test_unknown_session_id` → 404, no traceback in the body.
17. `e2e_today_renders_checklist` — 390×844, the first row's bounding box is at least 56 px tall and the Done button is visible without scrolling to the page bottom.
18. `e2e_tick_persists_across_reload` — tick row 2, `page.reload()`, the checkbox is still checked and `aria-checked="true"`.
19. `e2e_adjust_reps_in_two_taps` — count exactly two `page.click` calls between the initial state and `reps_done == 9` in the DOM.
20. `e2e_felt_appears_after_all_ticks` — the segmented control is hidden while a row is unticked and visible once all are ticked.
21. `e2e_done_shows_three_line_summary` — `/done/{id}` renders duration, "N of M exercises" and a next-time line.
22. `e2e_good_enough_done_visible_for_youth` — on the son's Today the Done control is present at zero ticks, and its label changes to "Good enough — done!" after the band's threshold.
23. `e2e_offline_tick_replays` — `context.set_offline(True)`, tick row 1, assert the checkbox is checked; `context.set_offline(False)`; wait with `page.wait_for_response(lambda r: "/api/sessions/" in r.url and r.status == 200)`; then assert via the API that the row is `done`. Do not rely on a fixed sleep.
24. `e2e_together_stacked_then_side_by_side` — at 390×844 the son's heading is below the parent's last row; at 1024×768 the two columns share a row (compare bounding-box `y`).
25. `e2e_timer_off_by_default` — no timer is running on load; one tap on a `seconds` row starts a count that advances, and rows without `seconds` or `rest_s` have no timer control.
26. `e2e_page_weight_under_budget` — sum the transferred bytes of every response on a cold `GET /today` and assert < 60 KB.

## Devil's-advocate risks

1. **Session created on render contradicts architecture §3.** Mitigation: the Deviations block above, plus a DECISIONS entry committed first, so the reviewer sees the reasoning in their own inputs.
2. **Double session on double-tap or a racing prefetch.** Mitigation: unique index on `(planned_session_id)` where `finished_at IS NULL`, insert-or-read inside one transaction, test 2.
3. **Offline queue replays a tick the user has since untapped.** Mitigation: client timestamps and last-write-wins on `ts`, test 5.
4. **Queue grows without bound when the server is permanently 500.** Mitigation: stop replay on the first failure, cap the store at 500 records, drop the oldest with a console warning and surface the banner.
5. **Service worker scope trap.** A worker under `/static/` never controls `/today` and offline silently does nothing while tests pass locally. Mitigation: `/sw.js` at the root, and test 23 exercises the real path.
6. **Stale service worker serves last week's checklist forever.** Mitigation: network-first for `/today*`, `CACHE_VERSION`, stale-cache deletion on `activate`.
7. **Adjust needing three taps.** Opening the panel then `−` then a confirm is three. Mitigation: no confirm — the stepper posts on each press; test 19 counts clicks.
8. **56 px targets lost to CSS.** Mitigation: test 17 measures the bounding box, not the stylesheet.
9. **Together Done finalising only one session.** Mitigation: test 12; the done service takes the `together_group_id` and iterates.
10. **Youth Done suppressed below threshold** — a natural but wrong reading of §3.7. Mitigation: P6 quoted in the wireframe and test 22 asserts presence at zero ticks.
11. **`load_done_kg` free-text entry bypassing the ladder and the cap.** Mitigation: stepper only, server-side clamp, test 7.
12. **Carry rows silently unrecordable.** Accepted limitation, stated in Scope out; the planned distance is taken as done and PRP-06 reads it from `rows_json`.
13. **Gzip missing in production** turns a 60 KB budget into 110 KB. Mitigation: the middleware shim, and test 26 measures transferred bytes.

## Done when

- [ ] `/today` renders for `me`, `son` and `together`; tick, adjust, felt and Done all work with JavaScript enabled and tick/Done still work with it disabled (plain form POSTs as the fallback).
- [ ] `/sw.js` is served at the root, registers, precaches, and a tick taken offline reaches the server after reconnect.
- [ ] `manifest.json` installs (name Cadence, `display: standalone`, theme colour, 192 and 512 px icons in `cadence/web/static/icons/`).
- [ ] HTMX vendored with a pinned version comment; no external requests on any page.
- [ ] `/today` cold-loads under 60 KB transferred.
- [ ] All 26 acceptance tests pass; `make lint && make test && make e2e` green.
- [ ] `docs/DECISIONS.md` carries the three deviations.
