# PRP-07 — Autoregulation, deload, assessments, gaps, challenges

Branch `prp/07-progression-assess`. Depends on PRP-01 (library, `materialise_rows`, `week_of_block`, `library/assessments.yaml`), PRP-02 (`session`, `completion()`, `next_time_note` stub), PRP-03 (bands, settings), PRP-04 (streak/queries), PRP-06 (`metrics_cache` for readiness — optional). Consumed by 10.

## Goal

Make the plan respond. After a session finishes, the next session of that day type changes: reps up, load up a rung, or back off. Every 28 days six tests are run, the gaps they expose become at most three named challenges, and each challenge puts one extra row into matching sessions.

## Scope in

- `cadence/programme/autoregulation.py` — the §5.4 decision table and the §5.5 arithmetic.
- Real `next_time_note(session) -> str`, replacing PRP-02's stub.
- Deload and missed-session handling, including the 14-day restart (§5.7).
- `cadence/bilan/` — assessments, gap detection, challenge generation, challenge row insertion.
- `GET|POST /assess?profile=`, `GET|POST /api/assessments?profile=`.
- The "Assessment day" card on Today until the baseline exists.

## Scope out

- The library file `library/assessments.yaml` itself — **owned by PRP-01**. This PRP reads it.
- `metrics_cache` population and the readiness value — **owned by PRP-06**. Treat a missing cache as `readiness = "unknown"`.
- Body-composition trend rendering — **owned by PRP-04**. This PRP reads the same cache for the §7.5.4 `body_comp` gap.
- Badges — **owned by PRP-10**.

## Data model

`assessment` and `challenge` tables, columns exactly as architecture §3. This PRP creates them in `cadence/bilan/tables.py`.

`assessment.value` is a float; `unit` is `reps | s | score`; `self_rated` is a bool. A test that could not be run is stored with `value = NULL` and `unit = "unavailable"` — **not** zero, and never a gap (§7.5.2).

`challenge.row_json` holds the `WorkoutRowSpec` from §7.7 in the same shape `materialise_rows` produces, plus `{"frequency": 2, "day_types": ["upper_a", "upper_b"]}`.

Progression state lives in `planned_session.rows_json`. Each row gains, on top of PRP-01's shape:

```json
{"progression": {"type": "double_progression", "rep_low": 8, "rep_high": 12,
                 "load_step_kg": 1.13, "time_step_s": 10, "deload_pct": 0.60,
                 "regress_triggers": ["hard", "missed", "low_readiness"],
                 "cap_load_kg": 5.0, "allow_load_progression": false,
                 "regress_step": 0.10, "regress_reps": 2, "distance_step_m": 10},
 "pending_bump": false, "last_outcome": "hold"}
```

Field names are **PRP-00 §4.6's `Progression`**, so principles §5.1's `rep_min`/`rep_max`/`regress_on` are `rep_low`/`rep_high`/`regress_triggers` here. The six fields PRP-00 does not model (`cap_load_kg`, `allow_load_progression`, `regress_step`, `regress_reps`, `distance_step_m`, `load_step_pct`) are runtime state written into `rows_json` by PRP-01's `materialise_rows`; read them from the row, never from the schema model. `deload_pct` is 0.60 per §5.5, not PRP-00's model default of 0.4.

`pending_bump` is set when R1 (deload) suppresses an earned bump; it is consumed by week 1 of the next block (§5.4).

## API / UI surface

### `GET /assess?profile=son` — 390 px

```
┌──────────────────────────────────────┐
│ Check-in            [ Me ][  Son  ]  │
│ Six quick tests. Once every 4 weeks. │
├──────────────────────────────────────┤
│ 1  Make snow angels on the wall      │
│    ┌────┬────┬────┬────┐             │
│    │ 0  │ 1  │ 2  │ 3  │             │  segmented, 56px, rubric text below
│    └────┴────┴────┴────┘             │
│    2 = hands overhead, wrists on     │
│        the wall, ribs flaring a bit  │
├──────────────────────────────────────┤
│ 2  Sit down tall and stand up strong │
│    ┌───┬───┬───┬───┬───┐             │
│    │ 1 │ 2 │ 3 │ 4 │ 5 │             │
│    └───┴───┴───┴───┴───┘             │
├──────────────────────────────────────┤
│ 3  Press up like a plank on the move │
│    [  −  ]     12     [  +  ]  reps  │
│    Stops at 20. That's a win.        │  ← youth cap phrasing
├──────────────────────────────────────┤
│ 4  Hang like a monkey                │
│    [  −  ]     ——     [  +  ]  s     │
│    ⚠ No bar or beam set up.          │
│      [ Mark unavailable ]            │  ← shown when has_overhead_anchor false
├──────────────────────────────────────┤
│ 5  Hold the bridge steady            │
│    [  −  ]     45     [  +  ]  s     │
├──────────────────────────────────────┤
│ 6  Carry the buckets across the yard │
│    [  −  ]     22     [  +  ]  s     │
├──────────────────────────────────────┤
│        [    Save check-in    ]       │
└──────────────────────────────────────┘
```

Adult phrasing uses the §7.1 test names ("Push-up max", "Dead hang", "Plank", "Wall angel reach", "Goblet squat quality", "Farmer carry"). Youth phrasing uses the §7.4 play strings verbatim. Steppers move by 1 for reps and scores, by 5 for seconds; hold-to-repeat is not required.

After saving, the same page swaps in `partials/assess_result.html`:

```
│ Saved. Three things to chase:        │
│  1  Dead hang 60 s by 4 Oct          │
│  2  Push-up max 15 by 4 Oct          │
│  3  Farmer carry 30 s by 4 Oct       │
│  These now appear in your sessions.  │
```

### Today's assessment card (rendered by PRP-02's template, context supplied here)

```
├──────────────────────────────────────┤
│ ┌──────────────────────────────────┐ │
│ │ Assessment day                   │ │
│ │ Six tests, about 15 minutes.     │ │
│ │ [ Start ]            [ Skip ]    │ │
│ └──────────────────────────────────┘ │
```

Shown while the profile has no assessment, or the newest is ≥ 28 days old. `Skip` marks the assessment `planned_session` `skipped` and moves to the next session; the card returns the next day.

Partials: `partials/assess_form.html`, `partials/assess_result.html`, `partials/assessment_card.html`, `partials/challenges.html`.

### JSON

`GET /api/assessments?profile=me`

```json
{"ok": true, "error": null, "meta": {"baseline_on": "2026-09-06", "next_due_on": "2026-10-04"},
 "data": {"latest": [{"test_id": "push_up_max", "value": 12, "unit": "reps",
                      "recorded_on": "2026-09-06", "self_rated": false, "tier": "low"},
                     {"test_id": "dead_hang_s", "value": null, "unit": "unavailable",
                      "recorded_on": "2026-09-06", "self_rated": false, "tier": null}],
          "gaps": [{"test_id": "push_up_max", "severity": 0.2, "rank": 1}],
          "challenges": [{"id": "push-up-15", "name": "Push-up max 15 by 4 Oct",
                          "metric": "push_up_max", "target_value": 15, "unit": "reps",
                          "due_on": "2026-10-04", "status": "active"}]}}
```

`POST /api/assessments` body `{"profile": "son", "recorded_on": "2026-09-06", "results": [{"test_id": "plank_s", "value": 45}, {"test_id": "dead_hang_s", "unavailable": true}]}`. Returns the same shape plus the newly derived challenges. Re-posting the same date replaces that date's results rather than appending a second set.

`POST /api/assessments` rejects, with 422: an unknown `test_id`; a `value` outside the test's plausible range (reps 0–200, seconds 0–600, scores within their rubric); a body-composition metric for a youth profile; a self-rated score that is not an integer.

## Implementation notes

- Files: `cadence/programme/autoregulation.py`, `cadence/programme/notes.py`, `cadence/bilan/{tables.py,assessments.py,gaps.py,challenges.py}`, `cadence/web/routers/assess.py`, `cadence/api/assessments.py`, templates above.

### Autoregulation

```python
@dataclass(frozen=True)
class AutoregInput:
    all_rows_ticked: bool
    felt: Literal["easy", "right", "hard"] | None
    readiness: Literal["low", "ok", "high", "unknown"]
    missed_sessions_7d: int
    week_of_block: int
    completion: Literal["complete", "partial"]

def decide(inp: AutoregInput) -> Literal["bump", "hold", "regress", "deload"]: ...
def apply_outcome(row: dict, outcome: str, rules: YouthRuleSet, ladder: list[float]) -> dict: ...
def autoregulate_next(session, finished_session) -> list[str]: ...   # returns human notes
```

- `decide` is the §5.4 table, **rules in order, first match wins**, implemented as an explicit ordered list of `(name, predicate, outcome)` so a test can assert which rule fired. Return the rule id alongside the outcome for logging.
- `completion == "partial"` excludes the session from R7 (§5.4). A partial session with `felt == "hard"` still reaches R5.
- `felt is None` never matches R2, R4 or R5; it falls through to R6–R11.
- `apply_outcome` is pure: it takes a row dict and returns a new one. Never mutate.
- Youth: `allow_load_progression` is false, so a bump follows the §5.6 ladder — reps, then time/distance, then `progression_of`, and only then load, always clamped to `effective_cap`. Step 4 is reachable only when the first three are exhausted.
- Post-bump, re-round to the ladder (§8.4) and re-clamp (§3.3), in that order.
- `autoregulate_next` finds the **next `planned` session with the same `day_type`** for that profile and rewrites its `rows_json` row by row, matching rows by `exercise_id` and falling back to `position`. Rows with no counterpart are left alone. It runs on Done, after finalisation, inside the same transaction, and never touches a session that already has a `session` attached.
- **Assessment day runs no autoregulation** (§10.10). Put the guard at the **call site**: PRP-02's Done service checks `planned_session.day_type == "assessment"` and returns before calling `autoregulate_next`. Do not bury it inside `decide()`, where a future caller would miss it.
- **Deload week 4**: `sets = max(2, floor(sets * 0.60))`, `reps = rep_low`, load unchanged, `rpe_cap = 6`, carries scale distance by 0.60. An earned bump sets `pending_bump: true` and is applied at week 1 of the next block.
- **Missed sessions** (D-011, §5.7): `missed_sessions_7d` counts scheduled slots in the trailing seven days with no completed session, where a slot is `days_per_week` evenly spread. More than 14 days with zero completed sessions restarts the block at week 1 with every load × 0.90, re-rounded, and one line on Today: `Welcome back. Starting the block again, a little lighter.`
- `week_of_block = min(4, floor(complete_sessions_in_block / days_per_week) + 1)` — PRP-01's `week_of_block`, counting only `complete` sessions (PRP-02's `completion()`).

### `next_time_note`

One line, no more than 80 characters, built from the single highest-value change the autoregulator just made, in this priority: a load bump, then a rep bump, then a time bump, then a regression, then deload, then hold.

```
"Next time: goblet squat 3×10 @ 14 kg"
"Next time: plank 3×50 s"
"Next time: same again — nail the tempo."          # hold
"Next time: easing off a rung on the bench press." # regress
"Next time: deload week — 2 sets, same weight."
```

Loads render in the profile's `display_unit` (PRP-03). For a youth profile the note never mentions a body metric and never uses the word "weight"; use the exercise name and the number with its unit.

### Assessments, gaps, challenges

- `library/assessments.yaml` supplies protocols, rubrics, adult tiers, youth targets and caps, and the §7.7 gap→row map. Read it through PRP-01's `LibraryBundle`; do not re-parse the file.
- Youth measured values are **capped at the §7.4 cap on save** and the UI says so. Storing an uncapped value and capping at render time is wrong: the cap is the protocol.
- Gap detection is §7.5 exactly: below the `ok` tier (adult) or the band fun target (youth); `unavailable` excluded; ranked by `(ok_threshold - measured) / ok_threshold` descending; ties broken by the fixed order `dead_hang_s, push_up_max, plank_s, wall_angel_reach, goblet_squat_quality, farmer_carry_s`; adult-only `body_comp` gap appended last when the 28-day trend shows body fat flat-or-rising and muscle percent flat-or-falling; top 3.
- "Flat" means a slope within ±0.05 percentage points per week over the cached window. With fewer than 10 cached points, no `body_comp` gap is emitted.
- Challenge names: adult `"{test_display} {target_value}{unit} by {due_date:%-d %b}"`; youth the fixed play phrase from §7.4 with a number that is a count or a duration only. `target_value` is the `ok` threshold (adult) or the band fun target (youth).
- A challenge row for a youth profile must pass PRP-01's `youth_allows(exercise_id, band, bodyweight_kg)` as well as the validator, because §9's per-band allowlist is not enforced by PRP-00's validator.
- **Challenge rows** are handed to `materialise_rows(..., challenge_rows=[...])` (PRP-01's hook) and land as the **last row** of each matching `day_type`, `is_challenge=true`, at most one per session, up to `frequency` sessions per week. P5: a challenge row may exceed the session-length row count by exactly one but never `max_exercises_per_session`. The `wall_angel_reach` challenge appends to the prelude tail instead (§2), raising it to ≤ 380 s, and is **suppressed on assessment day** so the test is not pre-fatigued.
- A challenge closes `met` when a later assessment reaches `target_value`, and `expired` at `due_on`. Expired challenges are re-derived from the next assessment.
- Baseline: the program's week-1 assessment session (PRP-01). Until an assessment exists the Today card shows; `Skip` marks it skipped, and the card returns on the next session until a baseline is recorded.

## Acceptance tests

`tests/test_autoregulation.py`, `tests/test_bilan.py`, `tests/e2e/test_assess.py`.

1. `test_decision_table` — parametrised over the **full** §5.4 table, one case per rule R1–R11, asserting both the outcome and the rule id that fired.
2. `test_illustrative_slice` — the seven rows of the §5.4 illustrative table reproduce exactly.
3. `test_r1_beats_r9` — week 4 with all ticked and `felt == easy` → `deload`, and `pending_bump` becomes true.
4. `test_pending_bump_applied_next_block` — the stored bump lands on week 1 of the next block and clears.
5. `test_partial_excluded_from_r7` — a partial session with rows unticked and `felt == right` → `hold` via R10/R11, never `regress`.
6. `test_felt_none_falls_through` — `felt is None` with `readiness == "low"` → `hold` (R6).
7. `test_bump_arithmetic_double_progression` — reps +1 until `rep_high`, then load +step and reps reset to `rep_low`, re-rounded to the ladder.
8. `test_bump_rep_progression_advances_exercise` — at `rep_high` the row swaps to `progression_of` at `rep_low`.
9. `test_regress_drops_load_then_reps_then_exercise` — the three-step fallback of §5.5.
10. `test_deload_values` — 3 sets → 2, 4 sets → 2, reps → `rep_low`, load unchanged, carry distance × 0.60.
11. `test_youth_cap_never_exceeded` — property-style: 20 consecutive all-ticked `felt=easy` sessions for a `son` at `age_10_13`; after every one, assert every row's `load_kg <= effective_cap` and that reps stay within `rep_max_loaded`.
12. `test_youth_bump_order` — reps first, then seconds, then `progression_of`, and load only when the first three are exhausted.
13. `test_missed_sessions_two_regress` — `missed_sessions_7d == 2` → `regress` (R3) even with `felt == easy`.
14. `test_block_restart_after_14_days` — loads × 0.90, re-rounded, week reset to 1, one announcement line.
15. `test_no_autoregulation_on_assessment_day` — the next session's `rows_json` is byte-identical after an assessment session.
16. `test_next_time_note_formats` — parametrised over the six shapes above; each ≤ 80 chars; the youth variant contains none of the banned phrases from PRP-03.
17. `test_gap_ranking` — a fixture with four sub-`ok` results ranks by severity and returns exactly 3.
18. `test_gap_tie_break` — two equal severities resolve in the §7.5.3 fixed order.
19. **Negative** `test_unavailable_is_never_a_gap` — `dead_hang_s` unavailable produces no gap and no challenge.
20. **Negative** `test_body_comp_gap_never_for_youth` — a youth profile with a falling muscle trend yields no `body_comp` gap.
21. `test_body_comp_gap_needs_ten_points` — nine cached points → no gap; eleven with the right slopes → a gap ranked last.
22. `test_challenge_names_youth_banned_words` — generate a challenge for every one of the six tests at all three youth bands and assert none matches the banned-phrase list, and none contains a digit followed by `kg` or `lb`.
23. `test_challenge_row_appears_in_today` — after a baseline with a `push_up_max` gap, the next `upper_a` session's last row is `push-up` with `is_challenge == true`.
24. `test_challenge_row_respects_p5` — a 15-min session gets 3 + 1 rows, and a `u10` session never exceeds `max_exercises_per_session`.
25. `test_wall_angel_challenge_appends_to_prelude` — prelude length rises, no extra main row appears, and the appendix is absent on assessment day.
26. `test_challenge_met_on_retest` — a retest at or above `target_value` sets `status == "met"`.
27. `test_challenge_expires` — a retest after `due_on` below target sets `expired` and a new challenge is derived.
28. `test_at_most_three_active_challenges` — a fifth cannot be created.
29. **Negative** `test_assessment_value_out_of_range` — `push_up_max: 500` → 422.
30. **Negative** `test_unknown_test_id` → 422 naming it.
31. `test_youth_value_capped_on_save` — a son recording 30 push-ups stores the §7.4 cap of 20, and the response says the count was capped.
32. `test_reposting_same_date_replaces` — two POSTs for one date leave six assessment rows, not twelve.
33. `e2e_assess_form_renders_six_tests` — 390×844, six blocks, segmented controls for the two self-rated tests.
34. `e2e_dead_hang_unavailable_without_anchor` — with `has_overhead_anchor` false the dead-hang block shows the unavailable control and no stepper.
35. `e2e_assessment_card_on_today` — the card appears before a baseline and disappears after one is saved.
36. `e2e_youth_assess_uses_play_phrasing` — the son's form contains "Hang like a monkey" and none of the adult test names.

## Devil's-advocate risks

1. **Rule order collapsed into `if/elif` written from memory**, silently reordering R2 and R3. Mitigation: an explicit ordered list of predicates plus test 1 asserting the rule id, not just the outcome.
2. **Youth load creeping up over many sessions** — the single most dangerous bug in the product. Mitigation: `allow_load_progression=false`, clamp after every mutation, and the 20-iteration property test 11.
3. **The clamp applied before rounding**, so rounding pushes back over the cap. Mitigation: round then clamp, asserted in test 11 at every iteration.
4. **Autoregulating a session already in progress.** Mitigation: only sessions with `status == "planned"` and no attached `session` are rewritten; test 15's byte-identity check catches over-reach.
5. **Autoregulation firing twice** on a double Done. Mitigation: it runs inside the idempotent Done path (PRP-02) and is a no-op when `finished_at` was already set.
6. **`readiness` absent read as `low`**, quietly holding every session forever. Mitigation: missing cache → `"unknown"`, which R9 accepts as bumpable; test 6 covers the `low` path separately.
7. **A challenge row pushing a `u10` session over five rows.** Mitigation: P5 encoded, test 24.
8. **A youth challenge named with a weight.** Mitigation: test 22 across every test and band, checking for digits adjacent to `kg`/`lb` as well as words.
9. **Wall-angel challenge fatiguing the wall-angel test.** Mitigation: suppressed on assessment day, test 25.
10. **Zero-division in gap severity** when an `ok` threshold is 0. Mitigation: thresholds are all positive in §7.3/§7.4; assert that at load time and raise a clear error if a future edit breaks it.
11. **Capping at render rather than at save**, so a raw 30 leaks into the trend. Mitigation: test 31.
12. **The 14-day restart triggering on a fresh install** with no sessions at all. Mitigation: the restart requires at least one prior completed session; test with an empty profile.

## Done when

- [ ] `decide()` reproduces §5.4 in order, with the firing rule id available for logging.
- [ ] Youth loads cannot rise above the band cap by any path, proven over 20 iterations.
- [ ] Deload, pending bump, missed sessions and the 14-day restart all behave as §5.7.
- [ ] `next_time_note` replaces PRP-02's stub and renders in the profile's display unit.
- [ ] Six tests recorded, gaps ranked, at most three challenges, rows inserted as the last row of matching sessions.
- [ ] Youth challenge names never mention weight, body or appearance.
- [ ] All 36 acceptance tests pass; `make lint && make test && make e2e` green.
