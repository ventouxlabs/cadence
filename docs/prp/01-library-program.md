# PRP-01 — Library DSL, seed content, program engine

Branch `prp/01-library-program`. Depends on PRP-00 (`cadence/schema/`, `cadence/validateur/`, `cadence/db.py`, `cadence/config.py`, test harness). Consumed by 02, 03, 04, 07, 08.

## Goal

Turn `docs/exercise-principles.md` into executable data and one deterministic function that produces a 4-week plan. After this PRP, `make seed` fills an empty database with ~52 exercises, 11 workouts, two demo profiles and one active program per profile, and every planned session carries concrete, legal, ladder-rounded rows.

## Scope in

- YAML DSL for `library/exercises/*.yaml`, `library/workouts/*.yaml`, `library/progressions/default.yaml`, `library/youth_rules.yaml`, `library/assessments.yaml`.
- Seed content transcribed from principles §9 and §10.
- Loader `cadence/bibliotheque/` → validated `LibraryBundle` → `exercise` / `workout` rows with `source=seed`.
- `weights_available` parser and the load ladder (§8).
- Program engine `cadence/programme/`: day templates, week schemes, days/week mapping, session-length row counts, youth filtering and clamping, deload, load rounding.
- SQLModel table definitions for `profile`, `setting`, `exercise`, `workout`, `program`, `planned_session`.
- `make seed` → `python -m cadence.bibliotheque.seed`.

## Scope out

- Pydantic model classes and `validateur.validate_workout` — **owned by PRP-00**. This PRP calls them.
- Any HTTP route or template. Settings *screens* and `cadence/profils/` services — **owned by PRP-03** (this PRP ships only the two tables and `age_band()`).
- Autoregulation, assessments, challenge generation — **owned by PRP-07**. This PRP ships the insertion *hook* only.
- `session` / `session_row` tables — **owned by PRP-02**.
- Import and generation — **owned by PRP-08**.

## Ownership seams (state identically in the consuming PRP)

| Artefact | Owner | Consumers |
|---|---|---|
| `profile` + `setting` tables (incl. `bodyweight_kg`, `has_overhead_anchor`) | 01 | 03 |
| `age_band(profile) -> AgeBand` | 01 | 02, 03, 04, 07 |
| `parse_weights_available(text)` | 01 | 03 (live preview) |
| `week_of_block(completed, days_per_week)` | 01 | 07 |
| `materialise_rows(..., challenge_rows=[])` hook | 01 | 07 |

## Data model

Architecture §3 is authoritative for columns. Additions this PRP makes, each needing a `docs/DECISIONS.md` entry appended by the implementer as the **first** commit on the branch:

| Table | Added column | Why |
|---|---|---|
| `profile` | `bodyweight_kg REAL NULL` | §3.3 percentage caps are uncomputable without it |
| `profile` | `has_overhead_anchor BOOLEAN NOT NULL DEFAULT 0` | D-019, §1.7 anchor filtering |
| `profile` | `age_band TEXT NOT NULL DEFAULT 'u10'` | denormalised cache of `age_band()`, recomputed on write |

`exercise` and `workout` are **created by PRP-00**; this PRP only fills them. New here: `profile` and `setting` in `cadence/profils/tables.py`, `program` and `planned_session` in `cadence/programme/tables.py`. All are imported by `cadence/db.py::init_db()` so `SQLModel.metadata` sees them.

> **Seam correction.** PRP-00's deferral table assigns `profile` / `setting` and age-band derivation to PRP-03. That cannot hold: PRP-01 lands first and `make seed` must create both demo profiles, and the builder needs `age_band`, `bodyweight_kg` and `has_overhead_anchor` for the §3.3 caps and §1.7 filtering. **This PRP owns the two tables and `age_band()`; PRP-03 owns the services and screens over them.** PRP-03 states the same split.

### Library file layout

One file per §9 subsection — eight exercise files, ids must match §9 exactly.

```
library/exercises/hinge.yaml        # §9.1   library/exercises/carry.yaml       # §9.5
library/exercises/squat.yaml        # §9.2   library/exercises/brace.yaml       # §9.6
library/exercises/push.yaml         # §9.3   library/exercises/mobility.yaml    # §9.7
library/exercises/pull.yaml         # §9.4   library/exercises/locomotion.yaml  # §9.8
library/workouts/<id>.yaml          # 11 files, id == filename stem
library/progressions/default.yaml
library/youth_rules.yaml
library/assessments.yaml
```

### Exercise document

Field names and constraints are **PRP-00 §4.3's `Exercise` model**, which is `extra="forbid"` and frozen. Two keys below are template-layer only and are stripped before the model is constructed — see the note after the example.

```yaml
version: 1
kind: exercises          # discriminator; loader rejects anything else in this directory
exercises:
  - id: db-rdl                       # slug, unique across the whole library
    name: Dumbbell Romanian deadlift
    pattern: hinge                   # §1.1
    region: lower                    # §1.2
    load_type: dumbbell              # §1.3
    load_unit: per_hand              # §1.4 — REQUIRED
    measure: reps                    # §1.5
    equipment: [dumbbells]           # list[Equipment], min_length 1
    tags: []                         # §1.6
    cue: "Hinge back, bar-path close to the legs, stop at mid-shin."   # ≤ 120 chars, one line
    garmin_category: DEADLIFT        # 26 confirmed members, or null (D-018); no UNKNOWN member exists
    garmin_exercise: null            # needs a non-null category if set
    regression_of: null              # exercise id or null
    progression_of: null             # exercise id or null
    default_progression: dbl-prog-loaded-compound   # id into progressions/default.yaml
    est_seconds_per_set: 45
    # --- template layer, stripped before building the Exercise model ---
    youth_ok:                        # §9 allowlist; yes | no | conditional
      u10: no
      age_10_13: yes
      age_14_17: yes
```

> **`youth_ok` has no home in PRP-00's schema.** `Exercise` is `extra="forbid"` and has no allowlist field, and `codes.py` has no allowlist code, so §9's per-band columns are currently unenforceable by the validator. Resolution, needing no change to PRP-00: the loader pops `youth_ok` before constructing `Exercise`, keeps it on `LibraryBundle.youth_ok: dict[str, dict[AgeBand, YouthOk]]`, and exposes `youth_allows(exercise_id, band) -> bool`. The program engine filters on it, and **PRP-08's import and generate paths must call it too**, since the validator will not. Report this gap; if PRP-00 later adds the field and a `youth_exercise_not_allowed` code, move the check there and delete the helper.

`conditional` means §3.6: legal only two-handed at 16 kg with `bodyweight_kg >= 45.7`. `Y*` in §9 → `conditional`. Every `UNKNOWN` in §9 → `garmin_category: null` (D-018).

### Workout document

> **These files are templates, not `Workout` documents.** PRP-00's `Workout`/`WorkoutRow` models are concrete: `load_kg` is a number, there is no `role`, `per_side`, `load_rule`, `u10_sub` or per-profile column, and `extra="forbid"` rejects all of them. A seed workout has no concrete load until a profile and a week are applied. So `library/workouts/*.yaml` parses into `WorkoutTemplate` / `TemplateRow`, **owned by this PRP** in `cadence/bibliotheque/template.py`, and `materialise_rows` compiles a template into a PRP-00 `Workout` for one profile and week. The seed loader validates each template by compiling it for every profile kind and band it claims to serve and running `validate_workout` on the compiled result. Imported and generated documents (PRP-08) skip this layer — they are concrete `Workout` docs already.
>
> Template-layer keys, resolved at compile time and never present on a `WorkoutRow`: `role`, `per_side`, `load_rule`, `u10_sub`, `extras`, and the `adult:`/`youth:` column blocks. `role: prelude` compiles to `is_prelude: true`. `per_side` does **not** double `sets` — that would trip the §3.2 set cap — it doubles the row's time estimate and is surfaced in the row label via `cue_override`. `materialise_rows` computes `Workout.estimated_minutes` with the §6.4 formula; it is a required field on the model.

```yaml
version: 1
kind: workout
id: upper-a
name: Upper A
day_type: upper_a                    # §11 DayType
target_profile_kind: adult           # adult | youth | both
variant: null                        # null | youth | play
prelude: posture-prelude-v1          # null only on posture-prelude-v1 itself
rows:                                # priority order; engine truncates (§6.4)
  - exercise: db-bench-press
    role: main                       # main | secondary | finisher | prelude | assessment
    sets: 3
    reps: 8                          # exactly one of reps / seconds / meters
    per_side: false
    rest_s: 90
    rpe_target: 8
    load_rule: {mode: ladder}
  - exercise: plank
    role: finisher
    sets: 3
    seconds: 40
    rest_s: 45
    load_rule: {mode: bodyweight}
extras:                              # appended at 45 min (§6.4)
  - {exercise: db-lateral-raise, role: secondary, sets: 2, reps: 12, rest_s: 45, load_rule: {mode: ladder}}
```

`load_rule.mode` ∈ `bodyweight | ladder | fixed_kg`. Optional keys: `relative_to` (another row's exercise id) with `pct` (float) and `rung_tolerance` (int, default 0); `kg` (required for `fixed_kg`); `fallback_exercise` (id used when the implement is absent); `prefer: heaviest_rung`; `max_kg` (hard ceiling before youth clamping). `u10_sub: <exercise-id>` on a row names the §10.6 substitute.

Together workouts replace `sets/reps/rest_s/load_rule` with two sub-blocks:

```yaml
  - exercise: goblet-squat
    role: main
    adult: {sets: 3, reps: 10, rest_s: 90, load_rule: {mode: ladder}}
    youth: {sets: 2, reps: 10, rest_s: 60, load_rule: {mode: ladder, max_kg: 8}, u10_sub: bodyweight-squat}
```

### `library/youth_rules.yaml`

The §3.2 table verbatim, one mapping per band key (`u10`, `age_10_13`, `age_14_17`, `adult`), field names exactly as §3.9 `YouthRuleSet`. Percentages are stored as fractions (`15 %` → `0.15`). `assessment_caps` comes from the §7.4 cap column.

### `library/progressions/default.yaml`

A list of named `Progression` documents parsed by **PRP-00 §4.6's model**, whose field names differ from principles §5.1. Map them:

| principles §5.1 | PRP-00 `Progression` |
|---|---|
| `rep_min` / `rep_max` | `rep_low` / `rep_high` |
| `regress_on` | `regress_triggers` |
| `load_step_kg`, `time_step_s`, `type`, `deload_pct` | same names |
| `regress_step`, `regress_reps`, `load_step_pct`, `distance_step_m`, `allow_load_progression`, `cap_load_kg` | **not on the model** |

The six unmodelled fields are runtime state, not library data: `allow_load_progression` is derived from the band (false for every youth band), `cap_load_kg` from `effective_cap`, and the rest carry principles' defaults (`regress_step` 0.10, `regress_reps` 2) as module constants in `cadence/programme/`. All six are written into `rows_json` by `materialise_rows`, where PRP-07 reads them.

**`deload_pct` must be set explicitly to `0.60` in every document.** PRP-00's model default is 0.4; principles §5.5 says 0.60, and the two differ materially for carry distance. Do not rely on the default.

### `library/assessments.yaml`

One document with four blocks. `tests:` is a list of **PRP-00 §4.8 `AssessmentSpec`** models (§7.1 protocols, §7.2 rubric text in `protocol`, `youth_cap` from §7.4, `retest_days: 28`). The other three — `adult_tiers` (§7.3), `youth_targets` (§7.4 per-band targets and phrasing) and `gap_rows` (§7.7) — have no PRP-00 model and are parsed by this PRP's own `AssessmentLibrary` in `cadence/bibliotheque/`. PRP-07 reads all four; this PRP only loads and validates.

## API surface (Python, no HTTP)

```python
# cadence/bibliotheque/loader.py
def load_library(path: Path = Path("library")) -> LibraryBundle: ...
# LibraryBundle: exercises: dict[str, Exercise]          (PRP-00 model)
#                youth_ok:  dict[str, dict[AgeBand, YouthOk]]
#                templates: dict[str, WorkoutTemplate]   (this PRP's model)
#                youth_rules: dict[AgeBand, YouthRuleSet]
#                progressions: dict[str, Progression]    (PRP-00 model)
#                assessments: AssessmentLibrary
def youth_allows(bundle, exercise_id: str, band: AgeBand, bodyweight_kg: float | None) -> bool: ...

# cadence/bibliotheque/seed.py
def seed(session, path: Path = Path("library"), reset: bool = False) -> SeedReport: ...

# cadence/programme/bands.py
def age_band(profile: Profile) -> AgeBand: ...
def effective_cap(rules: YouthRuleSet, load_unit: LoadUnit, bodyweight_kg: float | None) -> float | None: ...
def week_of_block(completed_sessions_in_block: int, days_per_week: int) -> int: ...

# cadence/programme/ladder.py
def parse_weights_available(text: str) -> WeightsAvailable: ...   # .ladders: dict[LoadType, list[float]]; .warnings: list[str]
def round_to_available(target_kg: float, ladder: list[float], cap_kg: float | None) -> float | None: ...

# cadence/programme/builder.py
def build_program(profile: Profile, settings: Settings, library: LibraryBundle,
                  start_date: date) -> ProgramPlan: ...          # Program + list[PlannedSession] (unsaved)
def materialise_rows(template: WorkoutTemplate, week: int, profile: Profile, settings: Settings,
                     library: LibraryBundle, prev_rows: list[dict] | None = None,
                     challenge_rows: list[dict] | None = None) -> list[dict]: ...
def compile_workout(template, week, profile, settings, library) -> Workout: ...   # PRP-00 model, for the validator
```

`materialise_rows` returns the exact `planned_session.rows_json` shape:

```json
[{"position": 1, "exercise_id": "cat-cow", "role": "prelude", "name": "Cat-cow",
  "cue": "Move one vertebra at a time, breathe with the movement.",
  "sets": 1, "reps": 8, "seconds": null, "meters": null, "per_side": false,
  "rest_s": 0, "load_kg": null, "load_unit": "bodyweight", "measure": "reps",
  "garmin_category": "WARM_UP", "is_challenge": false, "notes": []}]
```

`notes` carries user-facing one-liners produced by clamping ("Load reduced to 5 kg for this age"), never a raw violation (§3.10).

## Implementation notes

- **Validator call.** PRP-00 owns `validateur.validate_workout(doc, *, profile_kind, age_band, equipment, bodyweight_kg=None, has_overhead_anchor=False, exercises=None, youth_rules=None)`, returning a frozen `ValidationResult(ok: bool, errors: list[ValidationError])`. **Gate on `result.ok`, never on `len(result.errors)` and never on the result's truthiness** — it is a Pydantic model and is always truthy, and two codes (`youth_rep_ceiling`, `youth_rpe_exceeded`) are warnings that a valid seed workout may legitimately carry. The correct call is `if not result.ok: fail(result.errors)`.
- **Loader order**: parse every file with `yaml.safe_load` → build docs → resolve `regression_of` / `progression_of` and derive the inverse links → check every referenced id exists → validate each workout for its `target_profile_kind` at every band it claims to serve → upsert. Any error aborts the whole load with a non-zero exit and a list of `file:doc_id: message` lines. Never partially seed.
- **Seed profiles**: `me` (`kind=adult`, `age_years=None`, `vitalforge_person` from `VITALFORGE_PERSON_ME`, `push_to_garmin=True`) and `son` (`kind=youth`, `age_years=None` → band `u10` per §3.8, `vitalforge_person` from `VITALFORGE_PERSON_SON`, `push_to_garmin=False`). Settings defaults: `equipment=["bodyweight","dumbbells","kettlebells","bench"]`, `weights_available="DB 5-52.5 lb adj step 2.5, KB 16/24 kg, BENCH adjustable"`, `days_per_week=4`, `session_minutes=30`, `push_son_to_garmin=false`, `setup_complete=false`, `timers_default_on=false`, `readiness_nudge_on=true`.
- **`seed(reset=False)`** upserts by id and never deletes rows whose `source != "seed"`. `make seed` is idempotent.
- **`mobility_carry` resolves per profile.** For an adult it is `mobility-carry-day` (§10.11); for a youth profile it is `son-play-day` (§10.8). At `days_per_week=6` the sixth slot is a repeat of `upper_a` for an adult and `son-play-day` for a youth profile, so a six-day youth week legitimately contains two play days. Write this rule down rather than letting the pool picker choose.
- **Rotation** is `[upper_a, lower_a, upper_b, lower_full_b]` indexed continuously across weeks (§6.3). `assessment` **replaces** the first slot of week 1 of the block; the displaced day type is skipped for that block and the rotation index still advances. A 4-day block therefore has 16 planned sessions: 1 assessment + 15 training.
- **Prelude is prepended to every session including `assessment`.** §2 is categorical; §10.10's numbered list is the measured block only. Prelude rows use `variant: youth` for any youth band (drop `dead-bug`, `open-book` → 4 per side).
- **Youth path**: filter the pool by `youth_ok[band] != no` (`conditional` requires the §3.6 bodyweight test, else substitute `kb-deadlift` then `hip-hinge-bw`) and by `banned_tags`; row count `max(3, min(parent_row_count - 1, max_exercises_per_session))`; at least one `play` row; clamp sets, reps, rest and `session_minutes` to the band; clamp loads down to `effective_cap` (§3.3) and append a `notes` line. Never reject a row at build time — clamping is the build-time behaviour, rejection is the validator's job on imported/generated docs (D-019).
- **Session length** clamps against the band: `effective_minutes = min(session_minutes, rules.max_session_minutes)`. A household with a `u10` son and `session_minutes=45` is legal; the son's sessions run at 20 min.
- **Anchor**: when `profile.has_overhead_anchor` is false, drop every `requires_anchor` exercise before selection and apply the §1.7 substitutions (`dead-hang` → `db-floor-pullover`, `inverted-row` → `bench-supported-db-row`). Neither counts as a regression.
- **Rounding** is §8.4 exactly: nearest rung at or below `min(target, cap)`, else the lightest rung if it fits the cap, else `None` → apply §8.5.
- **`parse_weights_available`** implements the §8.2 grammar. Unparsable token → ignore it, append a warning naming the token, fall back to bodyweight-only for that implement. Never guess. `kg = lb * 0.45359237`, round to 2 dp.
- Determinism: `build_program` takes no clock and no RNG. Any pool tie-break is by the template's declared priority order, then by exercise id ascending.
- Libraries: `pyyaml` (`safe_load` only), `pydantic` v2, `sqlmodel`. No new dependencies.

## Acceptance tests

`tests/test_library_load.py`, `tests/test_ladder.py`, `tests/test_program_builder.py`.

1. `test_every_seed_exercise_validates` — `load_library()` returns 52 exercises and 11 workouts with zero errors.
2. `test_seed_ids_match_principles` — the exercise id set equals the literal set transcribed from §9 (list it in the test, do not derive it from the YAML).
3. `test_every_link_resolves` — every `regression_of` / `progression_of` target exists; inverse links are derived and symmetric.
4. `test_three_orphan_exercises_are_expected` — `knee-push-up`, `kb-row`, `single-leg-rdl-db` appear in no seed workout and this is asserted, not incidental.
5. `test_youth_workouts_pass_every_band` — `son-upper-a`, `son-lower-a`, `son-play-day` return `ok is True` at `u10`, `age_10_13` and `age_14_17`. Warnings are permitted; errors are not.
6. **Negative** `test_adult_workouts_fail_youth_validation` — `upper-a`, `lower-a`, `lower-full-b`, `mobility-carry-day` each return `ok is False` at `u10`, carrying `youth_load_type_not_allowed` or `youth_banned_tag` (§3.2/§3.4).
7. **Negative** `test_unknown_garmin_category_rejected` — `garmin_category: BARBELL` fails to load, and so does `garmin_category: UNKNOWN` (no such member, D-018).
7b. **Negative** `test_template_key_rejected_on_concrete_model` — feeding a raw template row to PRP-00's `WorkoutRow` fails with `extra="forbid"`, proving the two layers are distinct.
7c. `test_youth_allows_helper` — `youth_allows("kb-swing", "age_10_13", None)` is False; at `age_14_17` with `bodyweight_kg=50` True, with 40 or None False.
8. **Negative** `test_missing_link_target_aborts_load` — a doc with `regression_of: no-such-exercise` aborts the whole load and seeds nothing.
9. `test_program_4day_30min_has_16_sessions` — `days_per_week=4`, `session_minutes=30` → 16 planned sessions; each `rows_json[0].role == "prelude"`; the first session of week 1 has `day_type == "assessment"`.
10. `test_30min_session_has_five_rows_excluding_prelude` — and 15 min → 3, 45 min → 7 (§6.4).
11. `test_days_per_week_mapping` — 2 → `[upper_a, lower_full_b]`; 3 across two weeks → `[upper_a, lower_a, upper_b]` then `[lower_full_b, upper_a, lower_a]`; 5 adds `mobility_carry`, which resolves to `mobility-carry-day` for `me` and `son-play-day` for `son`; 6 repeats `upper_a` for `me` and gives `son` a second `son-play-day`.
12. `test_deload_week_reduces_sets` — every week-4 row has `sets == max(2, floor(sets_w3 * 0.60))` and `reps == rep_low`, load unchanged (§5.5).
13. `test_week_schemes` — week 1/2/3 reps for a `main` row are 8/10/8 with 3/3/4 sets, and a template week-1 override wins (P7: `upper-a` `plank` is 40 s in week 1, 50 s in week 2).
14. `test_load_rounding_cases` — parametrised: target 20.0 on the household DB ladder → 19.28; target 1.0 → 2.27 when uncapped; target 20.0 with `cap=5.0` → 4.54; `cap=1.0` → `None`; discrete KB ladder target 20.0 → 16.0.
15. `test_weights_available_parser` — `"DB 5-52.5 lb adj step 2.5"` → adjustable 2.27–23.81 kg step 1.13; `"KB 16/24 kg"` → `{16.0, 24.0}`; `"BENCH adjustable"` → bench present; `"DB 2-10 kg"` → step 1.0.
16. **Negative** `test_weights_available_garbage` — `"DB banana"` yields a warning naming the token, an empty dumbbell ladder, and does not raise.
17. `test_youth_load_clamped_not_rejected` — a `son` at `age_10_13` on `together-full-body` gets `goblet-squat` at ≤ 8.0 kg with a `notes` entry, and the row survives.
18. `test_u10_substitution` — `son` at `u10` on `son-upper-a` renders `prone-ytw-raise` and `scap-push-up` in rows 2 and 3.
19. `test_kettlebell_conditional` — `age_14_17` with `bodyweight_kg=40` gets `kb-deadlift` instead of `kb-swing`; with `bodyweight_kg=50` gets `kb-swing` at 16 kg; `bodyweight_kg=None` gets the substitute.
20. `test_no_anchor_filters_pull_v` — `has_overhead_anchor=False` → no `dead-hang` / `inverted-row` / `scap-pull-hang` anywhere in 16 sessions; `upper-b` row 4 is `db-floor-pullover`.
21. `test_seed_is_idempotent` — running `seed()` twice leaves identical row counts and no duplicate ids.
22. `test_seed_creates_two_profiles_and_two_programs` — `me` and `son` exist with one active `program` each.
23. `test_build_program_is_deterministic` — two calls with equal inputs produce byte-identical `rows_json`.

## Devil's-advocate risks

1. **Seed content drifts from §9/§10 when a test fails.** An implementer "fixes" a name or rep count to make a grep pass. Mitigation: test 2 hard-codes the id list; the PRP says *transcribe §9 and §10 verbatim — ids, names, cues, sets, reps, rest and garmin categories must match the document, and a mismatch is a bug in the code, never in the seed*.
2. **Percentage caps silently disabled.** `bodyweight_kg` is `None` for both seeded profiles, so only absolute caps apply and a reviewer may read the pct path as dead. Mitigation: test 17 and 19 set `bodyweight_kg` explicitly; `effective_cap` documents the `None` branch.
3. **Continuous rotation off by one.** A 3-day week that restarts the rotation each week repeats `upper_a` weekly. Mitigation: test 11 asserts across two weeks.
4. **Prelude on assessment day is contested.** §10.10 does not list it. Mitigation: the Implementation notes state the resolution and test 9 asserts it; the implementer logs it in DECISIONS.
5. **Rounding up.** A `round()` instead of "nearest lower" can exceed a youth cap. Mitigation: test 14 includes a case where nearest-higher would be legal-looking but wrong, and `round_to_available` never calls `round()`.
6. **Adjustable-dumbbell step drift.** 2.5 lb → 1.1339805 kg; accumulating the ladder by repeated addition drifts. Mitigation: build rungs as `min + i * step` from an integer index and round each to 2 dp.
7. **Together workout validated only as adult.** Mitigation: test 5 covers the youth column of `together-full-body` at all three bands.
8. **Row-count truncation drops the finisher.** Truncating to 3 rows by priority order can leave no `finisher`. Mitigation: truncation keeps one `main`, then fills from `secondary`, then always appends one `finisher` — assert in test 10 that a 15-min session ends on a `finisher` row.
9. **Program rebuild loses history.** Not this PRP's job, but `build_program` must be pure and return unsaved objects so PRP-03 can splice. Mitigation: signature returns `ProgramPlan`; no `session.add` inside.
10. **`conditional` treated as truthy.** A naive `if youth_ok[band]:` makes the 24 kg bell legal for a 14-year-old. Mitigation: `YouthOk` is a three-value enum, never a bool; test 19.

## Done when

- [ ] `library/` holds 8 exercise files (52 ids), 11 workout files, `progressions/default.yaml`, `youth_rules.yaml`, `assessments.yaml`.
- [ ] `make seed` on an empty DB creates 52 exercises, 11 workouts, 2 profiles, 2 programs, 32 planned sessions, and is idempotent.
- [ ] `load_library()` fails loudly and seeds nothing on any schema, link or validation error.
- [ ] `build_program` and `materialise_rows` are pure, deterministic and clock-free.
- [ ] `library/` parses into PRP-00's `Exercise`, `Progression`, `YouthRuleSet` and `AssessmentSpec` models with `extra="forbid"` satisfied, and templates compile to a valid `Workout`.
- [ ] All 26 acceptance tests pass; `make lint && make test` green.
- [ ] `docs/DECISIONS.md` has entries for the three added `profile` columns and the assessment-day prelude.
