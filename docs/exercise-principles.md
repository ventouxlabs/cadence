# Cadence — Exercise Principles

Domain foundation spec. Every rule here is meant to be machine-enforceable: enums, integers,
thresholds. Implementers derive the Pydantic schema, the validator, the seed library, the program
engine, and the assessment flow from this document alone.

Units are metric throughout (kg, s, m). Loads are stored in kg; the program engine rounds to the
user's available increments at render time (§8).

> A clinician should clear any medical concern before either profile starts training.

---

## 1. Movement taxonomy

### 1.1 `pattern` (enum)

| value | meaning (one line) | examples |
|---|---|---|
| `hinge` | Load moved by flexing/extending the hip with a near-static knee angle and neutral spine. | `db-rdl`, `kb-deadlift`, `kb-swing`, `hip-hinge-bw` |
| `squat` | Simultaneous knee + hip flexion/extension, torso upright, load in front or on body. | `goblet-squat`, `split-squat`, `step-up-bench`, `wall-sit` |
| `push_h` | Force projected away from the torso in the horizontal plane. | `push-up`, `db-bench-press`, `db-floor-press` |
| `push_v` | Force projected overhead in the vertical plane. | `db-overhead-press`, `pike-push-up`, `db-lateral-raise` |
| `pull_h` | Force drawn toward the torso in the horizontal plane. | `db-bent-row`, `bench-supported-db-row`, `prone-ytw-raise` |
| `pull_v` | Force drawn from overhead toward the torso, or the body drawn toward an overhead anchor. | `dead-hang`, `scap-pull-hang`, `db-floor-pullover` |
| `carry` | Load held statically while walking; measured in distance or time, not reps. | `farmer-carry`, `suitcase-carry`, `overhead-carry` |
| `brace` | Isometric resistance to spinal extension or flexion; trunk holds position under load. | `plank`, `hollow-hold`, `dead-bug`, `bird-dog` |
| `rotate_anti` | Isometric or slow resistance to trunk rotation or lateral flexion. | `side-plank`, `half-kneeling-db-antirotation-hold` |
| `mobility` | Low-load, end-range movement to restore joint range; never progressed by load. | `cat-cow`, `open-book`, `wall-angel`, `hip-90-90-switch` |
| `locomotion` | Travelling movement for coordination, play, or conditioning. | `bear-crawl`, `animal-walk-crab`, `line-balance-walk`, `skipping` |

Isolation movements map to the nearest pattern (`db-lateral-raise` → `push_v`,
`db-rear-delt-raise` → `pull_h`). The taxonomy is deliberately coarse; do not extend it.

### 1.2 `region` (enum)

`upper` · `lower` · `core` · `full`

### 1.3 `load_type` (enum)

`bodyweight` · `dumbbell` · `kettlebell` · `bench_assisted`

`bench_assisted` means the adjustable bench changes the leverage or range (incline push-up, step-up,
box squat) — it is **not** the same as lying on the bench with dumbbells, which is `dumbbell`.

### 1.4 `load_unit` (enum)

How a prescribed load is counted. **Required on every exercise and every prescribed row.**
Without it, youth caps cannot be checked (a 16 kg goblet squat is one implement in two hands;
a 2 × 12 kg farmer carry is two implements, one per hand).

| value | meaning | cap checked against |
|---|---|---|
| `per_hand` | Each hand holds one implement of the stated load. | `max_load_kg_per_hand`, `max_load_pct_bw_per_hand` |
| `per_implement` | One implement, held with two hands or on the body. | `max_load_kg_per_implement`, `max_load_pct_bw_per_implement` |
| `total` | Sum across implements (used for reporting and Garmin upload only). | not capped directly |
| `bodyweight` | No external load. | always legal |

### 1.5 `measure` (enum)

`reps` · `seconds` · `meters` · `steps`

`steps` exists for young children who count paces rather than distance; the engine converts
`steps → meters` at 0.5 m/step for Garmin upload.

### 1.6 `exercise_tag` (enum)

Safety and filtering labels. The validator bans tags per age band (§3).

`max_effort` · `one_rm` · `to_failure` · `plyometric_depth` · `loaded_spinal_flexion` ·
`overhead_loaded` · `loaded_carry` · `requires_anchor` · `unilateral` · `assessment_only` ·
`play` · `grip_limited` · `tempo`

`requires_anchor` marks exercises needing a pull-up bar, sturdy beam, or table edge — hardware
**outside** the equipment whitelist. See §1.7.

### 1.7 The pull_v / anchor problem

The equipment whitelist has no bar, yet `dead_hang` is a required assessment and `pull_v` is a
required pattern. Resolution, in three parts:

1. `has_overhead_anchor: bool` is a profile setting, default `false`. It is **not** an equipment id
   and never appears in the equipment picker.
2. Exercises tagged `requires_anchor` are filtered out of every generated session when
   `has_overhead_anchor == false`.
3. When the anchor is absent, `dead_hang` is recorded as `unavailable` (not zero, not a gap), and
   `pull_v` volume is served by `db-floor-pullover`. `inverted-row` is likewise gated and falls back
   to `bench-supported-db-row`. Neither substitution counts as a regression for progression purposes.

---

## 2. Posture prelude

Fixed block, id `posture-prelude-v1`. Runs first in **every** session for both profiles. Not
progressed, not autoregulated, never skipped by the program engine. Order is fixed.

| # | exercise id | prescription | est. seconds | purpose |
|---|---|---|---|---|
| 1 | `cat-cow` | 8 slow reps | 45 | segmental spinal warm-up |
| 2 | `open-book` | 6 per side | 60 | thoracic rotation |
| 3 | `wall-angel` | 8 slow reps | 45 | scapular control, overhead range |
| 4 | `glute-bridge` | 12 reps, 2 s hold at top | 60 | glute/hip activation |
| 5 | `dead-bug` | 8 per side | 60 | anti-extension brace |
| — | transitions | — | 30 | — |
| | | **total** | **300 s** | ≤ 5 min ✓ |

**Youth variant.** Same id, `variant: "youth"`. Row 5 (`dead-bug`) is dropped and row 2 reduced to
4 per side: total **220 s**. Applied automatically for any profile in a youth band.

**Prelude extension.** A `wall_angel_reach` gap (§7) appends 2 × 10 slow `wall-angel` reps to the
prelude tail, raising the total to ≤ 380 s. This is the only permitted modification.

---

## 3. Youth rules by age band

### 3.1 `age_band` (enum)

`u10` (< 10) · `age_10_13` (10–13) · `age_14_17` (14–17) · `adult` (18+)

Band is derived from the son's age at first run and recomputed on each app launch from stored
birth year. The parent profile is always `adult`.

### 3.2 Rule table

| rule | `u10` | `age_10_13` | `age_14_17` | `adult` |
|---|---|---|---|---|
| `allowed_load_types` | `bodyweight`, `bench_assisted` | `bodyweight`, `dumbbell`, `bench_assisted` | `bodyweight`, `dumbbell`, `kettlebell`, `bench_assisted` | all four |
| `max_load_kg_per_hand` | 0 | 5.0 | 12.0 | none |
| `max_load_kg_per_implement` | 0 | 8.0 | 16.0 | none |
| `max_load_pct_bw_per_hand` | 0 | 15 % | 25 % | none |
| `max_load_pct_bw_per_implement` | 0 | 20 % | 35 % | none |
| `rep_min_loaded` | n/a | 8 | 8 | 5 |
| `rep_max_loaded` | n/a | 15 | 15 | 20 |
| `rep_min_bodyweight` | 5 | 5 | 5 | 5 |
| `rep_max_bodyweight` | 15 | 20 | 25 | 40 |
| `min_rest_s_loaded` | n/a | 60 | 75 | 60 |
| `max_session_minutes` | 20 | 25 | 35 | 45 |
| `max_exercises_per_session` (excl. prelude) | 5 | 6 | 7 | 8 |
| `max_sets_per_exercise` | 2 | 3 | 3 | 4 |
| `rpe_cap` | 6 | 7 | 7 | 8 |
| `allow_amrap` | false | false | false | true |
| `allow_max_test` | false | false | false | true |
| `good_enough_done_after_n_exercises` | 3 | 3 | 4 | 1 |

### 3.3 Effective load cap

Both the absolute and the percentage cap apply; the **lower** wins. Bodyweight is optional
(`bodyweight_kg: float | None`); when unknown, only the absolute cap applies.

```
effective_cap_per_hand      = min(max_load_kg_per_hand,      pct_per_hand      * bodyweight_kg)
effective_cap_per_implement = min(max_load_kg_per_implement, pct_per_implement * bodyweight_kg)
```

A prescribed load is legal iff `load_kg <= effective_cap` for its `load_unit`. Illegal loads are
**clamped down** to the cap, never rejected — the row survives, the weight shrinks — and the clamp
is surfaced to the user as a one-line notice.

### 3.4 Banned tags per band

| band | `banned_tags` |
|---|---|
| `u10` | `max_effort`, `one_rm`, `to_failure`, `plyometric_depth`, `loaded_spinal_flexion`, `overhead_loaded`, `loaded_carry` |
| `age_10_13` | `max_effort`, `one_rm`, `to_failure`, `plyometric_depth`, `loaded_spinal_flexion`, `overhead_loaded` |
| `age_14_17` | `max_effort`, `one_rm`, `to_failure`, `plyometric_depth`, `loaded_spinal_flexion` |
| `adult` | `one_rm` |

`assessment_only` exercises are exempt from `max_effort` bans on `assessment-day` **only**, and only
within the youth caps of §7.

### 3.5 Banned goal types

`banned_goal_types` for every youth band: `weight`, `body_fat`, `appearance`.
The validator rejects a youth profile carrying any of these, and the challenge generator (§7) must
never emit a challenge whose `metric` is body-composition-derived for a youth profile.

### 3.6 Kettlebell reality check

The household owns 16 kg and 24 kg kettlebells only. Applying §3.2:

- `u10`, `age_10_13`: kettlebells are not an allowed load type. **No kettlebell is ever legal.**
- `age_14_17`: legal only two-handed (`per_implement`) at 16 kg, and only if
  `0.35 × bodyweight_kg >= 16`, i.e. bodyweight ≥ 45.7 kg. The 24 kg bell is never legal for youth.
- The `kb-swing` seed row is therefore `age_14_17` **conditional**, not unconditional. If the
  bodyweight test fails or bodyweight is unknown, the engine substitutes `kb-deadlift` at 16 kg
  (also `per_implement`) or, failing that, `hip-hinge-bw`.

### 3.7 The `good_enough_done` exit

The Done control is **always** visible and always tappable, on every session, for both profiles.
`good_enough_done_after_n_exercises` is the threshold at which the control is visually promoted
("Good enough — done!") and the session counts as complete for streak and progression purposes.
Below the threshold the session is recorded as `partial`; it still uploads to Garmin, and it does
**not** trigger a regression (§5, R7 applies only to full sessions).

### 3.8 Strictest-band default

Until the son's age is set, every youth profile is evaluated against `u10`. The seed data ships with
`age_band = "u10"` and `bodyweight_kg = None`. First-run onboarding must set the age before any
loaded row can render for the son.

### 3.9 `YouthRuleSet` pseudo-schema

```
YouthRuleSet:
  band:                                AgeBand
  allowed_load_types:                  list[LoadType]
  max_load_kg_per_hand:                float          # 0 == bodyweight only
  max_load_kg_per_implement:           float
  max_load_pct_bw_per_hand:            float | None   # 0..1
  max_load_pct_bw_per_implement:       float | None   # 0..1
  rep_min_loaded:                      int | None
  rep_max_loaded:                      int | None
  rep_min_bodyweight:                  int
  rep_max_bodyweight:                  int
  min_rest_s_loaded:                   int
  max_session_minutes:                 int
  max_exercises_per_session:           int
  max_sets_per_exercise:               int
  rpe_cap:                             int
  allow_amrap:                         bool
  allow_max_test:                      bool
  banned_tags:                         list[ExerciseTag]
  banned_goal_types:                   list[GoalType]
  good_enough_done_after_n_exercises:  int
  assessment_caps:                     dict[AssessmentId, int]   # §7.3
```

### 3.10 Validator contract

`validate_workout(workout: Workout, profile: Profile) -> list[Violation]`

Checks, in order, each producing a `Violation(rule, row_index, severity, message, remedy)`:

| # | check | severity | remedy |
|---|---|---|---|
| V1 | every row's `load_type` ∈ `allowed_load_types` | error | substitute via `regression_of` |
| V2 | `load_kg <= effective_cap` for the row's `load_unit` | error | clamp to cap |
| V3 | loaded rows have `reps >= rep_min_loaded` | error | raise reps to floor, reduce load |
| V4 | `reps <= rep_max_*` | warn | cap reps |
| V5 | `rest_s >= min_rest_s_loaded` on loaded rows | error | raise rest |
| V6 | `len(rows) <= max_exercises_per_session` (prelude excluded) | error | drop lowest-priority rows |
| V7 | `sets <= max_sets_per_exercise` | error | drop sets |
| V8 | no row carries a banned tag | error | substitute or drop |
| V9 | `estimated_minutes <= max_session_minutes` | error | drop lowest-priority rows |
| V10 | no `amrap` row unless `allow_amrap` | error | convert to fixed reps at `rep_max` |
| V11 | profile carries no banned goal type | error | reject profile save |
| V12 | `rpe_target <= rpe_cap` | warn | lower target |
| V13 | every `requires_anchor` row has `has_overhead_anchor == true` | error | substitute per §1.7 |

Severity `error` blocks the session from rendering until the remedy is applied automatically.
The engine applies remedies silently and logs them; it never shows a raw violation to the user.

---

## 4. Adult rules

Light by design — the whitelist is already the main constraint.

| rule | value |
|---|---|
| `banned_tags` | `one_rm` (always); `max_effort` outside `assessment-day` |
| `rpe_cap` | 8 in block weeks 1–3, **6** in week 4 (deload) |
| `rest_s_default` — compound | 90 |
| `rest_s_default` — secondary | 60 |
| `rest_s_default` — isolation / core | 45 |
| `rest_s_default` — carry | 120 |
| `max_sets_per_session` (all rows) | 20 |
| `max_session_minutes` | the `session_length` setting (15/30/45) |
| `max_exercises_per_session` | 8 |
| overhead work | permitted; `overhead_loaded` is not banned for adults |
| load ceiling | the top of the available implement range (§8), no absolute cap |

`assessment-day` is exempt from `rpe_cap` for the six named tests only.

---

## 5. Progression rules as data

### 5.1 `Progression` object

Stored on each seed exercise as a default and copied onto each workout row at generation time, where
it becomes the per-user mutable state.

```
Progression:
  type:                    ProgressionType
  rep_min:                 int | None
  rep_max:                 int | None
  load_step_kg:            float | None
  load_step_pct:           float | None      # used when load_step_kg is None
  time_step_s:             int | None
  distance_step_m:         int | None
  regress_on:              list[RegressTrigger]   # subset of {hard, missed, low_readiness}
  regress_step:            float                  # fraction of current load, default 0.10
  regress_reps:            int                    # reps removed when load cannot drop, default 2
  deload_pct:              float                  # default 0.60, applied to set count
  allow_load_progression:  bool                   # false for every youth band
  cap_load_kg:             float | None           # injected by the validator from §3.3
```

### 5.2 `ProgressionType` (enum)

| value | bump moves | typical use |
|---|---|---|
| `double_progression` | reps up to `rep_max`, then load `+load_step_kg` and reps reset to `rep_min` | loaded compounds |
| `linear_load` | load `+load_step_kg` every successful session, reps fixed | rarely used; heavy hinge only |
| `rep_progression` | reps `+1` per set, no load change, no ceiling below `rep_max` | bodyweight moves |
| `time_progression` | hold `+time_step_s` | `plank`, `wall-sit`, `dead-hang`, carries measured in seconds |

Defaults by measure: `reps` + external load → `double_progression`; `reps` + bodyweight →
`rep_progression`; `seconds` → `time_progression`; `meters`/`steps` → `time_progression` with
`distance_step_m` substituted for `time_step_s`.

### 5.3 Autoregulation inputs

| input | type | source |
|---|---|---|
| `all_rows_ticked` | bool | session log |
| `felt` | `easy` \| `right` \| `hard` | one-tap prompt at session end |
| `readiness` | `low` \| `ok` \| `high` \| `unknown` | VitalForge / Garmin (HRV, sleep, body battery) |
| `missed_sessions_7d` | int | scheduler, count of scheduled-but-unfinished in the last 7 days |
| `week_of_block` | 1–4 | block state |

### 5.4 Decision rules — ordered, first match wins

Evaluated per exercise row against the previous performance of that same row.

| # | condition | outcome |
|---|---|---|
| R1 | `week_of_block == 4` | `deload` |
| R2 | `felt == hard` **and** `not all_rows_ticked` | `regress` |
| R3 | `missed_sessions_7d >= 2` | `regress` |
| R4 | `felt == hard` **and** `readiness == low` | `regress` |
| R5 | `felt == hard` | `hold` |
| R6 | `readiness == low` | `hold` |
| R7 | `not all_rows_ticked` (session was `full`, rows left unticked) | `hold` |
| R8 | `missed_sessions_7d == 1` | `hold` |
| R9 | `all_rows_ticked` **and** `felt == easy` **and** `readiness ∈ {ok, high, unknown}` | `bump` |
| R10 | `all_rows_ticked` **and** `felt == right` | `hold` |
| R11 | (default) | `hold` |

R1 is absolute: **deload week overrides a bump.** A `felt == easy` session in week 4 still deloads;
the bump it earned is applied to week 1 of the next block instead (stored as `pending_bump: true`).

Sessions ended early via `good_enough_done` below the threshold are recorded `partial` and are
**excluded** from R7 — a short session holds, it does not regress.

Illustrative slice (week 1–3, `missed_sessions_7d == 0`):

| all ticked | felt | readiness | outcome |
|---|---|---|---|
| yes | easy | ok | bump |
| yes | easy | low | hold (R6) |
| yes | right | any | hold |
| yes | hard | ok | hold (R5) |
| yes | hard | low | regress (R4) |
| no | easy | ok | hold (R7) |
| no | hard | ok | regress (R2) |

### 5.5 Outcome arithmetic

**bump**
- `double_progression`: `reps += 1` on every set. If all sets are at `rep_max`, then
  `load += load_step_kg` (or `load *= 1 + load_step_pct`) and `reps = rep_min`.
- `rep_progression`: `reps += 1` per set, ceiling `rep_max`; at the ceiling, move to the linked
  `progression_of` exercise and reset to `rep_min`.
- `time_progression`: `seconds += time_step_s`, or `meters += distance_step_m`.
- `linear_load`: `load += load_step_kg`.
- Post-bump, the new load is re-rounded to the available ladder (§8) and re-clamped to `cap_load_kg`.

**hold** — no change. Repeat the prescription exactly.

**regress**
- If the row carries external load and `load - load_step_kg >= ladder_min`: `load -= load_step_kg`.
- Else: `reps -= regress_reps` (floor `rep_min`) or `seconds -= time_step_s` (floor
  `time_step_s × 2`).
- If already at the floor with no load to shed: swap to the linked `regression_of` exercise at its
  `rep_min`.

**deload** (week 4)
- `sets = max(2, floor(sets × deload_pct))` — 3 sets → 2, 4 sets → 2.
- `reps = rep_min`.
- Load unchanged. `rpe_cap = 6`.
- Carries: distance × `deload_pct`, load unchanged.

### 5.6 Youth progression

`allow_load_progression = false` for every youth band. A `bump` for a youth profile is applied in
this fixed order, first applicable wins:

1. `reps += 1` (ceiling `rep_max_loaded` / `rep_max_bodyweight`)
2. `seconds += time_step_s` or `meters += distance_step_m`
3. advance to the linked `progression_of` exercise, reset to `rep_min`
4. **only if all three are exhausted and `load < effective_cap`**: `load += load_step_kg`, clamped
   to `effective_cap`

Load never rises above the band cap by any path, including a challenge row or a manual edit.

### 5.7 Missed sessions

The plan is a queue, not a calendar.

- `today_workout = first scheduled workout with status ∈ {pending, partial}`. A missed day is not
  skipped; it simply stays at the head of the queue.
- `week_of_block` advances on **completed session count**, not dates:
  `week_of_block = floor(completed_sessions_in_block / days_per_week) + 1`, capped at 4.
- `missed_sessions_7d` counts scheduled slots in the trailing 7 days with no completed session.
- If more than 14 days elapse with zero completed sessions, the block restarts at week 1 with every
  load multiplied by 0.90 and re-rounded. A restart is announced in one line.
- A `partial` session advances the queue but is not counted toward `completed_sessions_in_block`.

### 5.8 Precedence

Applied in this order whenever rules conflict:

| # | rule |
|---|---|
| P1 | Youth band caps (§3) override everything: settings, progression, challenges, manual edits. |
| P2 | Week 4 deload overrides any bump (R1). |
| P3 | Equipment availability overrides prescribed load: round down, then substitute (§8). |
| P4 | Session-length row count (§6.3) overrides day-template row count. |
| P5 | A challenge row may exceed the session-length row count by exactly 1, but never `max_exercises_per_session`. |
| P6 | The `good_enough_done` exit is always available and cannot be suppressed by any rule. |
| P7 | A day template's week-1 prescription (§10) overrides the canonical week-1 shape of §6.2; weeks 2–4 transform relative to the template value. |

---

## 6. Program template

### 6.1 Structure

Rolling 4-week block. Four day types; every session is `prelude → main → secondary × n →
finisher → [challenge row]`.

| day type | main (compound) | secondary pool | finisher |
|---|---|---|---|
| `upper_a` | `db-bench-press` | horizontal pull, vertical push, rear delt | brace |
| `lower_a` | `goblet-squat` | hinge, lunge/step-up, hip thrust | anti-rotation |
| `upper_b` | `push-up` | bench-supported pull, floor press, `pull_v` | brace |
| `lower_full_b` | `kb-deadlift` / `kb-swing` | split squat, step-up | **carry** + anti-rotation |
| `mobility_carry` | — | 90/90, couch stretch | carry |
| `assessment` | — | the six tests (§7) | — |

Carries appear on `lower_full_b` every week and on `mobility_carry` when scheduled.

### 6.2 Set/rep scheme by block week

| week | scheme | RPE cap | note |
|---|---|---|---|
| 1 | 3 × 8 | 8 | load set from last block's week 3 |
| 2 | 3 × 10 | 8 | same load as week 1 |
| 3 | 3 × 12 **or** 4 × 8 | 8 | 4 × 8 for `main` rows, 3 × 12 for secondary |
| 4 | 2 × 8 | 6 | deload, same load as week 3 |

Time-measured rows scale the same way: W1 3 × 30 s, W2 3 × 40 s, W3 3 × 50 s, W4 2 × 30 s.
Carries: W1 3 × 30 m, W2 3 × 40 m, W3 3 × 50 m, W4 2 × 30 m.

**The day template owns week 1.** The table above is the canonical shape; where a template in §10
states a different week-1 value (`upper-a` prescribes `plank` 3 × 40 s, not 3 × 30 s), the template
wins. Weeks 2–4 then apply the same transformation *relative to the template value*: +2 reps or
+10 s per week, week 4 = 2 sets at the week-1 value. See P7.

### 6.3 Days per week → day-type mapping

The rotation order is fixed: `[upper_a, lower_a, upper_b, lower_full_b]`, indexed continuously
across weeks so a 3-day week does not repeat the same three days.

| days/week | week pattern |
|---|---|
| 2 | `upper_a`, `lower_full_b` |
| 3 | next 3 from the rotation, continuing across weeks (W1 A/LA/UB, W2 LFB/A/LA, …) |
| 4 | `upper_a`, `lower_a`, `upper_b`, `lower_full_b` **(default)** |
| 5 | the 4 above + `mobility_carry` |
| 6 | the 5 above + a repeat of `upper_a`; if a son profile exists, the repeat is `son-play-day` |

`assessment` replaces the first scheduled day of week 1 in every block (baseline, then every 28 days).

### 6.4 Session length → row count

| session_length | prelude | main | secondary | finisher | rows (excl. prelude) |
|---|---|---|---|---|---|
| 15 min | yes | 1 | 1 | 1 | 3 |
| 30 min | yes | 1 | 3 | 1 | 5 **(default)** |
| 45 min | yes | 1 | 4 | 2 | 7 |

Rows within a day template are stored in priority order; the engine truncates to the row count.
For 45 min it appends from the template's `extras` pool. A challenge row is added after truncation
(P5). Row time estimate: `sets × (reps × 3 s + rest_s)`; carries `sets × (distance_m × 1.2 s + rest_s)`.

### 6.5 Son's sessions

Same day type as the parent, independently generated.

- Exercise picks filtered by `youth_ok[band] == Y` and by `banned_tags`.
- Row count = `min(parent_row_count - 1, max_exercises_per_session)`, floor 3.
- At least one row must carry the `play` tag. On `son-play-day`, every row carries it.
- Prelude uses the youth variant.
- Rest, reps, sets, and session minutes clamped per §3.2.
- Play rows draw from `locomotion` and the fun pool: `bear-crawl`, `animal-walk-crab`,
  `line-balance-walk`, `jumping-jacks`, `skipping`, `throw-and-catch`.

### 6.6 Together sessions

`together-full-body` puts both profiles on the same day type and the same exercise ids, with
per-profile load, rep, and rest columns resolved independently. Both tick the same checklist; the
session writes two Garmin activities, one per profile. The son's column is validated against his
band exactly as a solo session would be.

---

## 7. Assessment protocol & targets

Run at baseline (day 1) and every 28 days. `assessment-day` is a full session slot.

### 7.1 The six tests

| id | unit | protocol |
|---|---|---|
| `push_up_max` | reps | Hands under shoulders, body in one line. Lower until the chest reaches fist height, press up. No time limit. Stop at form breakdown or failure. |
| `dead_hang_s` | s | Overhand grip on an overhead anchor, shoulder-width, feet clear of the floor, shoulders active. Time until the grip releases. Requires `has_overhead_anchor`. |
| `plank_s` | s | Forearms under shoulders, heels-to-head in one line, ribs down. Time until the hips drop or rise out of line. |
| `wall_angel_reach` | 0–3 | Back flat to a wall, heels ~10 cm out, low back and both wrists in contact. Slide arms overhead as far as contact holds. Self-rate per §7.2. |
| `goblet_squat_quality` | 1–5 | Hold one 8–16 kg implement at the chest. 5 controlled reps, filmed or mirrored. Self-rate the best rep per §7.2. |
| `farmer_carry_s` | s | Carry 2 × 16 kg (or 2 × 25 % bodyweight rounded down to the ladder) and walk continuously. Time until the grip fails or the torso side-bends. |

### 7.2 Self-rated rubrics

`wall_angel_reach` (0–3):

| score | description |
|---|---|
| 0 | Wrists or low back leave the wall before the hands pass shoulder height. |
| 1 | Hands reach forehead height with contact held. |
| 2 | Hands pass overhead, wrists on the wall, some ribcage flare. |
| 3 | Full overhead, elbows and wrists on the wall, low back flat, ribs down. |

`goblet_squat_quality` (1–5):

| score | description |
|---|---|
| 1 | Heels lift or knees collapse inward; cannot reach parallel. |
| 2 | Reaches above parallel only; visible knee cave. |
| 3 | Thighs to parallel, neutral spine, minor knee cave. |
| 4 | Below parallel, neutral spine, no cave, controlled descent. |
| 5 | Below parallel, upright torso, full control, 3 s pause at the bottom. |

### 7.3 Adult target tiers

| test | low | ok | strong |
|---|---|---|---|
| `push_up_max` | < 15 | 15–30 | > 30 |
| `dead_hang_s` | < 30 | 30–60 | > 60 |
| `plank_s` | < 60 | 60–120 | > 120 |
| `wall_angel_reach` | 0–1 | 2 | 3 |
| `goblet_squat_quality` | 1–2 | 3 | 4–5 |
| `farmer_carry_s` (2 × 16 kg) | < 30 | 30–60 | > 60 |

### 7.4 Youth fun targets and caps

Youth tests are **capped**: the app stops the count at the cap and shows a win. There is no "low"
tier for a youth profile — only "not yet" and "got it".

| test | `u10` | `age_10_13` | `age_14_17` | cap | phrasing |
|---|---|---|---|---|---|
| `push_up_max` | 8 | 12 | 20 | 20 | "Press up like a plank on the move" |
| `dead_hang_s` | 20 | 30 | 40 | 90 | "Hang like a monkey" |
| `plank_s` | 30 | 45 | 60 | 90 | "Hold the bridge steady" |
| `wall_angel_reach` | 2 | 2 | 3 | 3 | "Make snow angels on the wall" |
| `goblet_squat_quality` | 3 | 3 | 4 | 5 | "Sit down tall and stand up strong" |
| `farmer_carry_s` | 20 | 30 | 40 | 60 | "Carry the buckets across the yard" |

Youth carry load = 2 × 10 % bodyweight, clamped to `effective_cap_per_hand`. For `u10` the carry is
bodyweight only (`bear-crawl` substituted) because `loaded_carry` is a banned tag.

### 7.5 Gap detection

1. A **gap** is any test scoring below the `ok` tier. Youth: below the band's fun target.
2. `unavailable` tests (no anchor) are excluded — never a gap.
3. Rank by `severity = (ok_threshold - measured) / ok_threshold`, descending. Ties break by the
   fixed order `dead_hang_s, push_up_max, plank_s, wall_angel_reach, goblet_squat_quality,
   farmer_carry_s`.
4. **Adult only:** if the 28-day body-composition trend shows body fat flat-or-rising while muscle
   percent is flat-or-falling, append a `body_comp` gap ranked last. Never emitted for youth (§3.5).
5. Take the top 3. Fewer gaps means fewer challenges; zero gaps means zero challenges.

### 7.6 Challenge object

```
Challenge:
  id:            str            # kebab-case, e.g. "dead-hang-60s"
  name:          str            # rendered from the template below
  metric:        AssessmentId | "body_comp"
  target_value:  float
  unit:          str
  baseline_date: date
  due_date:      date           # baseline_date + 28 days, ISO
  inserted_row:  WorkoutRowSpec | None
  frequency:     int            # sessions per week the row is inserted into
  day_types:     list[DayType]
```

Adult name template: `"{test_display} {target_value}{unit} by {due_date:%-d %b}"` →
`"Dead hang 60 s by 15 Oct"`.
Youth name template: a fixed play phrase per test from §7.4, with no number about body, weight, or
appearance → `"Hang like a monkey for 30 seconds"`.

`target_value` = the `ok` threshold for adults, the band fun target for youth.

### 7.7 Gap → inserted row

| gap | exercise id | prescription | frequency | day types |
|---|---|---|---|---|
| `dead_hang_s` | `dead-hang` | 3 × (best_hold_s − 10 s) | 2 | `upper_a`, `upper_b` |
| `push_up_max` | `push-up` | 3 × round(best_reps × 0.6) | 2 | `upper_a`, `upper_b` |
| `plank_s` | `plank` | 3 × (best_s − 10 s) | 2 | `lower_a`, `lower_full_b` |
| `wall_angel_reach` | `wall-angel` | 2 × 10 slow reps, appended to the prelude tail | every session | all |
| `goblet_squat_quality` | `box-squat-bench` | 3 × 8, tempo 3-1-1 | 2 | `lower_a`, `lower_full_b` |
| `farmer_carry_s` | `farmer-carry` | 3 × (best_s − 10 s) | 2 | `lower_full_b`, `upper_b` |
| `body_comp` | — | no row; `+1` carry set on `lower_full_b` | — | — |

Inserted rows obey §3 for youth profiles and P5 for row counts. A challenge closes when a retest
meets `target_value`, or expires at `due_date` and is re-derived from the next assessment.

---

## 8. Equipment whitelist & load rounding

### 8.1 `equipment` (enum)

`bodyweight` · `dumbbells` · `kettlebells` · `bench`

Nothing else is ever selectable. `has_overhead_anchor` is a separate boolean setting, not equipment.

### 8.2 `weights_available` parsing convention

Free text, comma-separated tokens. Grammar per token:

```
IMPL  := "DB" | "KB" | "BENCH"
SPEC  := RANGE | DISCRETE
RANGE := number "-" number
DISCRETE := number ("/" number)*
UNIT  := "kg" | "lb"            # default kg
TOKEN := IMPL SPEC [UNIT] ["adj"] ["step" number]
```

| input | parsed |
|---|---|
| `DB 5-52.5 lb adj step 2.5` | dumbbell, adjustable, 2.27–23.81 kg, step 1.13 kg |
| `KB 16/24 kg` | kettlebell, discrete `{16.0, 24.0}` |
| `BENCH adjustable` | bench present |
| `DB 2-10 kg` | dumbbell, adjustable, 2.0–10.0 kg, step 1.0 kg (default) |

Default step: adjustable in lb → 2.5 lb = 1.13 kg; adjustable in kg → 1.0 kg.
Conversion: `kg = lb × 0.45359237`, stored to 2 decimals.

**Parse failure** on any token: that token is ignored, a settings warning is surfaced naming the
token, and the ladder falls back to bodyweight-only for the affected implement. Never guess.

### 8.3 The load ladder

`ladder(load_type) -> sorted list[float]`. For an adjustable range, the ladder is
`[min, min+step, …, max]`. For a discrete set, the ladder is that set. `bodyweight` has an empty
ladder.

Household default: dumbbell ladder `2.27, 3.40, 4.54, …, 23.81` kg (20 rungs);
kettlebell ladder `16.0, 24.0` kg.

### 8.4 Rounding rule

```
round_to_available(target_kg, ladder, cap_kg):
    candidates = [w for w in ladder if w <= min(target_kg, cap_kg)]
    if candidates:            return max(candidates)
    if min(ladder) <= cap_kg: return min(ladder)      # target below the lightest rung
    return None                                        # nothing legal
```

Always **nearest lower**. Never round up past the target, and never past a youth cap.

### 8.5 When the load is unavailable

| situation | action |
|---|---|
| target below the lightest rung | use the lightest rung if `<= cap`; otherwise substitute the `regression_of` bodyweight variant |
| target above the heaviest rung | cap at the heaviest rung and switch the row's `progression.type` to `rep_progression` (or `time_progression` for holds/carries) |
| nothing on the ladder is `<= cap` | substitute the `regression_of` bodyweight variant; if the exercise has no bodyweight regression, drop the row and take the next from the secondary pool |
| implement absent entirely | filter every exercise with that `load_type` out of the pool before generation |

`bench_assisted` is legal for `u10` because it changes leverage, not load — an incline push-up on
the bench is a regression, not a risk.

---

## 9. Seed library spec

> **Garmin category note (D-018):** `garminconnect` 0.3.11 has no `UNKNOWN` category. Every `UNKNOWN` below means `garmin_category: null` in the seed YAML; the write-back omits the field and VitalForge skips exercise sets for that row.


~52 exercises. Column key:

- **pat / reg / load / meas / unit** — §1 enums (`unit` = `load_unit`).
- **`<10` / `10–13` / `14–17`** — `youth_ok_by_band`. This is an explicit allowlist checked **in
  addition to** the load-type and tag rules of §3; where they disagree, the stricter wins.
- **links** — `regr_of: X` means this is an easier substitute for `X`; `prog_of: X` means this is a
  harder successor to `X`. Only one direction is stored; the engine derives the inverse. Every id
  referenced must resolve to a row in this table.
- **garmin** — **best-effort mapping**, drawn from a closed list. An implementer must verify each
  against the `garminconnect` library's own exercise enum before the first upload; anything
  unverified defaults to `UNKNOWN`. Never invent a category name.

Three seed exercises (`knee-push-up`, `kb-row`, `single-leg-rdl-db`) appear in no seed workout by
design: they exist as progression/regression targets and secondary-pool members, reachable only
via §5.5 and §6.1.

Closed category list: `PUSH_UP` `SQUAT` `DEADLIFT` `ROW` `PLANK` `CARRY` `LUNGE` `HIP_RAISE`
`CORE` `SHOULDER_PRESS` `BENCH_PRESS` `PULL_UP` `FLYE` `LATERAL_RAISE` `HYPEREXTENSION`
`TOTAL_BODY` `WARM_UP` `CARDIO` `UNKNOWN`.

### 9.1 Hinge

| id | name | pat | reg | load | meas | unit | <10 | 10–13 | 14–17 | tags | links | cue | garmin |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `hip-hinge-bw` | Bodyweight hip hinge | hinge | lower | bodyweight | reps | bodyweight | Y | Y | Y | — | regr_of: `db-rdl` | Push hips back, shins vertical, chest proud. | DEADLIFT |
| `glute-bridge` | Glute bridge | hinge | lower | bodyweight | reps | bodyweight | Y | Y | Y | — | regr_of: `bench-hip-thrust` | Ribs down, squeeze glutes, pause two seconds up top. | HIP_RAISE |
| `bench-hip-thrust` | Bench hip thrust | hinge | lower | bench_assisted | reps | bodyweight | Y | Y | Y | — | — | Shoulder blades on the bench, chin tucked, hips to level. | HIP_RAISE |
| `db-rdl` | Dumbbell Romanian deadlift | hinge | lower | dumbbell | reps | per_hand | N | Y | Y | — | — | Hinge back, bar-path close to the legs, stop at mid-shin. | DEADLIFT |
| `single-leg-rdl-db` | Single-leg DB RDL | hinge | lower | dumbbell | reps | per_hand | N | N | Y | `unilateral` | prog_of: `db-rdl` | Hips square, back leg long, reach the floor slowly. | DEADLIFT |
| `kb-deadlift` | Kettlebell deadlift | hinge | lower | kettlebell | reps | per_implement | N | N | Y* | — | — | Bell between the arches, push the floor away, stand tall. | DEADLIFT |
| `kb-swing` | Kettlebell swing | hinge | full | kettlebell | reps | per_implement | N | N | Y* | — | prog_of: `kb-deadlift` | Snap the hips, float the bell, never squat it up. | TOTAL_BODY |

`Y*` = conditional on §3.6 (16 kg only, two-handed, bodyweight ≥ 45.7 kg).

### 9.2 Squat

| id | name | pat | reg | load | meas | unit | <10 | 10–13 | 14–17 | tags | links | cue | garmin |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `bodyweight-squat` | Bodyweight squat | squat | lower | bodyweight | reps | bodyweight | Y | Y | Y | — | — | Knees track over toes, chest tall, sit between the hips. | SQUAT |
| `box-squat-bench` | Box squat to bench | squat | lower | bench_assisted | reps | bodyweight | Y | Y | Y | — | regr_of: `bodyweight-squat` | Sit back to the bench, touch, stand without rocking. | SQUAT |
| `goblet-squat` | Goblet squat | squat | lower | dumbbell | reps | per_implement | N | Y | Y | — | prog_of: `bodyweight-squat` | Hold the bell at the chest, elbows inside the knees. | SQUAT |
| `wall-sit` | Wall sit | squat | lower | bodyweight | seconds | bodyweight | Y | Y | Y | — | — | Thighs parallel, back flat to the wall, breathe steadily. | SQUAT |
| `reverse-lunge` | Reverse lunge | squat | lower | bodyweight | reps | bodyweight | Y | Y | Y | `unilateral` | — | Step back and down, front shin vertical, torso upright. | LUNGE |
| `split-squat` | Split squat | squat | lower | bodyweight | reps | bodyweight | Y | Y | Y | `unilateral` | prog_of: `reverse-lunge` | Feet on train tracks, back knee to the floor, no wobble. | LUNGE |
| `step-up-bench` | Step-up to bench | squat | lower | bench_assisted | reps | bodyweight | Y | Y | Y | `unilateral` | — | Drive through the whole foot, no push off the back leg. | LUNGE |

### 9.3 Push

| id | name | pat | reg | load | meas | unit | <10 | 10–13 | 14–17 | tags | links | cue | garmin |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `knee-push-up` | Knee push-up | push_h | upper | bodyweight | reps | bodyweight | Y | Y | Y | — | regr_of: `push-up` | Straight line knees to head, elbows at forty-five degrees. | PUSH_UP |
| `incline-push-up-bench` | Incline push-up on bench | push_h | upper | bench_assisted | reps | bodyweight | Y | Y | Y | — | regr_of: `push-up` | Hands on the bench, body one plank, chest to the pad. | PUSH_UP |
| `push-up` | Push-up | push_h | upper | bodyweight | reps | bodyweight | Y | Y | Y | — | prog_of: `knee-push-up` | Squeeze glutes, ribs down, chest to fist height. | PUSH_UP |
| `db-floor-press` | DB floor press | push_h | upper | dumbbell | reps | per_hand | N | Y | Y | — | regr_of: `db-bench-press` | Triceps touch the floor, pause, press straight up. | BENCH_PRESS |
| `db-bench-press` | DB bench press | push_h | upper | dumbbell | reps | per_hand | N | Y | Y | — | prog_of: `db-floor-press` | Shoulder blades pinned, wrists stacked, control the descent. | BENCH_PRESS |
| `db-overhead-press` | DB overhead press | push_v | upper | dumbbell | reps | per_hand | N | N | Y | `overhead_loaded` | — | Ribs down, press to ears, no back arch. | SHOULDER_PRESS |
| `pike-push-up` | Pike push-up | push_v | upper | bodyweight | reps | bodyweight | N | Y | Y | — | prog_of: `db-overhead-press` | Hips high, crown of the head to the floor, elbows forward. | PUSH_UP |
| `db-lateral-raise` | DB lateral raise | push_v | upper | dumbbell | reps | per_hand | N | Y | Y | — | — | Lead with the elbows, stop at shoulder height, no swing. | LATERAL_RAISE |

### 9.4 Pull

| id | name | pat | reg | load | meas | unit | <10 | 10–13 | 14–17 | tags | links | cue | garmin |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `prone-ytw-raise` | Prone Y/T/W raise | pull_h | upper | bodyweight | reps | bodyweight | Y | Y | Y | — | regr_of: `bench-supported-db-row` | Face down, thumbs up, lift from the shoulder blades. | FLYE |
| `bench-supported-db-row` | Bench-supported DB row | pull_h | upper | bench_assisted | reps | per_hand | N | Y | Y | — | regr_of: `db-bent-row` | Chest on the incline, row to the hip, no torso twist. | ROW |
| `db-bent-row` | DB bent-over row | pull_h | upper | dumbbell | reps | per_hand | N | Y | Y | — | — | Hinge to forty-five, row to the ribs, elbows past the back. | ROW |
| `kb-row` | Kettlebell row | pull_h | upper | kettlebell | reps | per_hand | N | N | Y* | — | prog_of: `db-bent-row` | Brace hard, pull the bell to the hip, pause one second. | ROW |
| `db-rear-delt-raise` | DB rear-delt raise | pull_h | upper | dumbbell | reps | per_hand | N | Y | Y | — | — | Hinge over, sweep wide, squeeze the shoulder blades. | FLYE |
| `inverted-row` | Inverted row | pull_h | upper | bodyweight | reps | bodyweight | Y | Y | Y | `requires_anchor` | prog_of: `bench-supported-db-row` | Body one line, pull the chest to the bar, hips level. | ROW |
| `dead-hang` | Dead hang | pull_v | upper | bodyweight | seconds | bodyweight | Y | Y | Y | `requires_anchor`, `grip_limited` | — | Overhand grip, shoulders active, breathe and just hang. | PULL_UP |
| `scap-pull-hang` | Scapular pull from hang | pull_v | upper | bodyweight | reps | bodyweight | Y | Y | Y | `requires_anchor` | regr_of: `dead-hang` | Hang long, then pull the shoulders down without bending. | PULL_UP |
| `db-floor-pullover` | DB floor pullover | pull_v | upper | dumbbell | reps | per_implement | N | N | Y | `overhead_loaded` | regr_of: `dead-hang` | Ribs pinned to the floor, arms soft, stop at the ears. | FLYE |

### 9.5 Carry

| id | name | pat | reg | load | meas | unit | <10 | 10–13 | 14–17 | tags | links | cue | garmin |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `bear-crawl` | Bear crawl | carry | full | bodyweight | meters | bodyweight | Y | Y | Y | `play` | — | Knees an inch off the floor, hips low, move opposite limbs. | TOTAL_BODY |
| `suitcase-carry` | Suitcase carry | carry | full | dumbbell | meters | per_hand | N | Y | Y | `loaded_carry`, `unilateral` | regr_of: `farmer-carry` | One bell only, walk tall, refuse to lean. | CARRY |
| `farmer-carry` | Farmer carry | carry | full | dumbbell | meters | per_hand | N | Y | Y | `loaded_carry`, `grip_limited` | — | Shoulders back, short quick steps, crush the handles. | CARRY |
| `overhead-carry` | Overhead carry | carry | full | dumbbell | meters | per_hand | N | N | N | `loaded_carry`, `overhead_loaded` | prog_of: `farmer-carry` | Elbows locked, ribs down, eyes forward, walk slow. | CARRY |

`overhead-carry` is adult-only by product decision, not by tag arithmetic alone.

### 9.6 Brace and anti-rotation

| id | name | pat | reg | load | meas | unit | <10 | 10–13 | 14–17 | tags | links | cue | garmin |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `dead-bug` | Dead bug | brace | core | bodyweight | reps | bodyweight | Y | Y | Y | — | regr_of: `plank` | Low back glued to the floor, move slow, exhale on reach. | CORE |
| `bird-dog` | Bird dog | brace | core | bodyweight | reps | bodyweight | Y | Y | Y | — | regr_of: `plank` | Long spine, no hip tilt, reach opposite hand and heel. | CORE |
| `plank` | Forearm plank | brace | core | bodyweight | seconds | bodyweight | Y | Y | Y | — | — | Elbows under shoulders, squeeze glutes, one straight line. | PLANK |
| `hollow-hold` | Hollow hold | brace | core | bodyweight | seconds | bodyweight | Y | Y | Y | — | prog_of: `plank` | Low back pressed down, arms by the ears, shins together. | CORE |
| `side-plank` | Side plank | rotate_anti | core | bodyweight | seconds | bodyweight | Y | Y | Y | `unilateral` | — | Stack the hips, push the floor away, do not sag. | PLANK |
| `half-kneeling-db-antirotation-hold` | Half-kneeling DB anti-rotation hold | rotate_anti | core | dumbbell | seconds | per_implement | N | Y | Y | `unilateral` | — | Hold the bell out front, resist the twist, stay square. | CORE |

### 9.7 Mobility

| id | name | pat | reg | load | meas | unit | <10 | 10–13 | 14–17 | tags | links | cue | garmin |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `cat-cow` | Cat-cow | mobility | full | bodyweight | reps | bodyweight | Y | Y | Y | — | — | Move one vertebra at a time, breathe with the movement. | WARM_UP |
| `open-book` | Open book | mobility | upper | bodyweight | reps | bodyweight | Y | Y | Y | `unilateral` | — | Knees stacked and still, let the top arm draw a wide arc. | WARM_UP |
| `wall-angel` | Wall angel | mobility | upper | bodyweight | reps | bodyweight | Y | Y | Y | — | — | Wrists and low back on the wall, slide slow, stop at contact. | WARM_UP |
| `scap-push-up` | Scapular push-up | mobility | upper | bodyweight | reps | bodyweight | Y | Y | Y | — | — | Arms locked, only the shoulder blades move, push and sink. | WARM_UP |
| `hip-90-90-switch` | 90/90 hip switch | mobility | lower | bodyweight | reps | bodyweight | Y | Y | Y | — | — | Sit tall, sweep the knees floor to floor, hands off if you can. | WARM_UP |
| `couch-stretch` | Couch stretch | mobility | lower | bodyweight | seconds | bodyweight | Y | Y | Y | `unilateral` | — | Back foot up the bench, tuck the tail, breathe out the tension. | WARM_UP |

### 9.8 Locomotion and play

| id | name | pat | reg | load | meas | unit | <10 | 10–13 | 14–17 | tags | links | cue | garmin |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `animal-walk-crab` | Crab walk | locomotion | full | bodyweight | meters | bodyweight | Y | Y | Y | `play` | — | Hips high off the floor, walk sideways, keep the chest open. | TOTAL_BODY |
| `line-balance-walk` | Balance line walk | locomotion | full | bodyweight | meters | bodyweight | Y | Y | Y | `play` | — | Heel to toe along the line, arms wide, eyes ahead. | UNKNOWN |
| `jumping-jacks` | Jumping jacks | locomotion | full | bodyweight | reps | bodyweight | Y | Y | Y | `play` | — | Land soft on the whole foot, arms all the way overhead. | CARDIO |
| `skipping` | Skipping | locomotion | full | bodyweight | meters | bodyweight | Y | Y | Y | `play` | — | Drive the opposite knee up, stay springy, land quietly. | CARDIO |
| `throw-and-catch` | Throw and catch | locomotion | upper | bodyweight | reps | bodyweight | Y | Y | Y | `play` | — | Count the catches, step to meet the ball, soft hands. | UNKNOWN |

---

## 10. Seed workout spec

Eleven seed workouts. Prescriptions shown are **block week 1** (§6.2 scales weeks 2–4). All rows are
listed in priority order; the engine truncates to the session-length row count (§6.4) and appends
from `extras` at 45 min. `RIR` = reps in reserve.

### 10.1 `posture-prelude-v1`

Defined in full in §2. Referenced by every other workout as `prelude`. Rest between prelude
movements: 0 s (continuous flow).

### 10.2 `upper-a` — adult, day type `upper_a`

| # | exercise | sets × reps | load rule | rest |
|---|---|---|---|---|
| 1 | `db-bench-press` | 3 × 8 | ladder-rounded, RPE 8 target, 2 RIR | 90 s |
| 2 | `db-bent-row` | 3 × 8 | ladder-rounded, match press load ± 1 rung | 90 s |
| 3 | `db-overhead-press` | 3 × 10 | ladder-rounded, ~60 % of bench load | 60 s |
| 4 | `prone-ytw-raise` | 2 × 10 | bodyweight | 45 s |
| 5 | `plank` | 3 × 40 s | bodyweight | 45 s |

`extras` (45 min): `db-lateral-raise` 2 × 12, `db-rear-delt-raise` 2 × 12.

### 10.3 `lower-a` — adult, day type `lower_a`

| # | exercise | sets × reps | load rule | rest |
|---|---|---|---|---|
| 1 | `goblet-squat` | 3 × 8 | ladder-rounded, `per_implement` | 90 s |
| 2 | `db-rdl` | 3 × 8 | ladder-rounded, `per_hand` | 90 s |
| 3 | `reverse-lunge` | 3 × 10 per side | bodyweight, or `per_hand` at 50 % of RDL load | 60 s |
| 4 | `bench-hip-thrust` | 3 × 12 | bodyweight | 60 s |
| 5 | `side-plank` | 2 × 30 s per side | bodyweight | 45 s |

`extras`: `wall-sit` 2 × 45 s, `hip-90-90-switch` 2 × 8 per side.

### 10.4 `upper-b` — adult, day type `upper_b`

| # | exercise | sets × reps | load rule | rest |
|---|---|---|---|---|
| 1 | `push-up` | 3 × 12 | bodyweight, 2 RIR (never to failure) | 90 s |
| 2 | `bench-supported-db-row` | 3 × 10 | ladder-rounded, `per_hand` | 90 s |
| 3 | `db-floor-press` | 3 × 10 | ladder-rounded, `per_hand` | 60 s |
| 4 | `db-floor-pullover` | 2 × 12 | ladder-rounded, `per_implement`, light | 60 s |
| 5 | `hollow-hold` | 3 × 30 s | bodyweight | 45 s |

`extras`: `db-rear-delt-raise` 2 × 12, `scap-push-up` 2 × 10.
If `has_overhead_anchor`, row 4 is replaced by `scap-pull-hang` 3 × 8 — a `pull_v` row, so the day
keeps its vertical-pull slot. `inverted-row` is an `extras` option, never a slot-4 swap.

### 10.5 `lower-full-b` — adult, day type `lower_full_b`

| # | exercise | sets × reps | load rule | rest |
|---|---|---|---|---|
| 1 | `kb-deadlift` | 3 × 10 | 24 kg, `per_implement`; falls back to `db-rdl` if absent | 90 s |
| 2 | `split-squat` | 3 × 8 per side | bodyweight, or `per_hand` light | 90 s |
| 3 | `step-up-bench` | 3 × 10 per side | bodyweight, or `per_hand` light | 60 s |
| 4 | `farmer-carry` | 3 × 30 m | 2 × 16 kg or heaviest ladder rung | 120 s |
| 5 | `half-kneeling-db-antirotation-hold` | 2 × 20 s per side | ladder-rounded, light, `per_implement` | 45 s |

`extras`: `kb-swing` 3 × 12 (24 kg), `suitcase-carry` 2 × 30 m.

### 10.6 `son-upper-a` — youth, day type `upper_a`

Specified at the `age_10_13` band. The `u10 sub` column names the automatic substitute when the band
is `u10` (dumbbell rows are illegal there per §3.2).

| # | exercise | sets × reps | load rule | rest | u10 sub |
|---|---|---|---|---|---|
| 1 | `incline-push-up-bench` | 2 × 10 | bodyweight | 60 s | — |
| 2 | `bench-supported-db-row` | 2 × 10 | ≤ `effective_cap_per_hand` (5 kg) | 60 s | `prone-ytw-raise` |
| 3 | `pike-push-up` | 2 × 8 | bodyweight | 60 s | `scap-push-up` |
| 4 | `bear-crawl` | 2 × 10 m | bodyweight (`play` row) | 60 s | — |
| 5 | `plank` | 2 × 20 s | bodyweight | 60 s | — |

Band check: 5 rows ≤ 5 (`u10` max) ✓ · 2 sets ≤ 2 ✓ · loaded reps 10 ≥ 8 ✓ · rest 60 s ≥ 60 ✓ ·
est. 17.5 min ≤ 20 ✓ · no banned tags ✓ · promote Done after row 3 ✓.

### 10.7 `son-lower-a` — youth, day type `lower_a`

| # | exercise | sets × reps | load rule | rest |
|---|---|---|---|---|
| 1 | `bodyweight-squat` | 2 × 12 | bodyweight | 60 s |
| 2 | `glute-bridge` | 2 × 12 | bodyweight | 60 s |
| 3 | `reverse-lunge` | 2 × 8 per side | bodyweight | 60 s |
| 4 | `line-balance-walk` | 2 × 10 m | bodyweight (`play` row) | 60 s |
| 5 | `dead-bug` | 2 × 8 per side | bodyweight | 60 s |

Every row is bodyweight, so this workout is legal unchanged at every band.

### 10.8 `son-play-day` — youth, day type `mobility_carry`

| # | exercise | sets × reps | load rule | rest |
|---|---|---|---|---|
| 1 | `animal-walk-crab` | 2 × 10 m | bodyweight | 45 s |
| 2 | `jumping-jacks` | 2 × 20 | bodyweight | 45 s |
| 3 | `throw-and-catch` | 2 × 20 catches | bodyweight | 45 s |
| 4 | `line-balance-walk` | 2 × 10 m | bodyweight | 45 s |
| 5 | `bear-crawl` | 2 × 10 m | bodyweight | 45 s |

Every row carries `play` and every row is bodyweight, so this workout is legal unchanged at every
band: 5 rows ≤ 5, 2 sets ≤ 2, no external load, no banned tag.

### 10.9 `together-full-body` — both profiles, day type `lower_full_b`

Shared exercise ids, per-profile load and rep columns.

| # | exercise | adult | son (`age_10_13`) | rest |
|---|---|---|---|---|
| 1 | `goblet-squat` | 3 × 10, ladder-rounded | 2 × 10, ≤ 8 kg `per_implement` | 90 / 60 s |
| 2 | `push-up` | 3 × 12 | 2 × 8 | 60 s |
| 3 | `db-bent-row` | 3 × 10, ladder-rounded | 2 × 10, ≤ 5 kg `per_hand` | 60 s |
| 4 | `farmer-carry` | 3 × 30 m, 2 × 16 kg | 2 × 15 m, ≤ 5 kg `per_hand` | 90 / 60 s |
| 5 | `plank` | 3 × 45 s | 2 × 20 s | 45 / 60 s |

Exercise ids are shared across both columns; only load, reps and rest differ. Cross-band
substitution uses the same `u10 sub` mechanism as §10.6: at `u10`, rows 1–4 fall back to
`bodyweight-squat`, `incline-push-up-bench`, `prone-ytw-raise` and `bear-crawl`.

### 10.10 `assessment-day` — both profiles

Order is fixed; the grip tests are separated so `dead_hang_s` is not contaminated by
`farmer_carry_s`. No autoregulation runs on this session.

| # | exercise | measurement | rest |
|---|---|---|---|
| 1 | `wall-angel` | `wall_angel_reach`, self-rated 0–3 | 60 s |
| 2 | `goblet-squat` | `goblet_squat_quality`, 5 reps, self-rated 1–5 | 90 s |
| 3 | `push-up` | `push_up_max`, one set to form breakdown (youth capped) | 180 s |
| 4 | `dead-hang` | `dead_hang_s`, single max hold; `unavailable` with no anchor | 180 s |
| 5 | `plank` | `plank_s`, single max hold | 180 s |
| 6 | `farmer-carry` | `farmer_carry_s`, single max walk | — |

Rows 3–6 carry `max_effort` and `assessment_only`. They are exempt from the youth `max_effort` ban
**only here**, and only within the caps of §7.4.

**Youth caps on this session.** `assessment-day` is additionally exempt from V6
(`max_exercises_per_session`) and V9 (`max_session_minutes`): every row is a single set, and those
caps govern training volume, not testing. Two youth limits replace them — rest is capped at 90 s
instead of 180 s, and every measured row stops at its §7.4 cap. A `u10` assessment then runs ≈ 13
min. No other exemption applies; `u10` substitutions from §10.6 still resolve rows 2 and 6.

### 10.11 `mobility-carry-day` — adult, 15-min option

| # | exercise | sets × reps | load rule | rest |
|---|---|---|---|---|
| 1 | `hip-90-90-switch` | 2 × 8 per side | bodyweight | 30 s |
| 2 | `couch-stretch` | 2 × 45 s per side | bodyweight | 30 s |
| 3 | `farmer-carry` | 3 × 40 m | 2 × 16 kg | 120 s |

`extras` (30 / 45 min): `suitcase-carry` 2 × 30 m, `bear-crawl` 2 × 15 m,
`open-book` 2 × 6 per side, `scap-push-up` 2 × 10.

---

## 11. Glossary / enums summary

Copy-ready for the schema author.

```
AgeBand          = u10 | age_10_13 | age_14_17 | adult
Pattern          = hinge | squat | push_h | push_v | pull_h | pull_v
                 | carry | brace | rotate_anti | mobility | locomotion
Region           = upper | lower | core | full
LoadType         = bodyweight | dumbbell | kettlebell | bench_assisted
LoadUnit         = per_hand | per_implement | total | bodyweight
Measure          = reps | seconds | meters | steps
Equipment        = bodyweight | dumbbells | kettlebells | bench

ExerciseTag      = max_effort | one_rm | to_failure | plyometric_depth
                 | loaded_spinal_flexion | overhead_loaded | loaded_carry
                 | requires_anchor | unilateral | assessment_only | play
                 | grip_limited | tempo

ProgressionType  = double_progression | linear_load | rep_progression | time_progression
RegressTrigger   = hard | missed | low_readiness
AutoregOutcome   = bump | hold | regress | deload

Felt             = easy | right | hard
Readiness        = low | ok | high | unknown
SessionStatus    = pending | partial | complete | skipped

GoalType         = strength | posture | movement_quality | consistency
                 | weight | body_fat | appearance
                   # the last three are banned on any youth profile

DayType          = upper_a | lower_a | upper_b | lower_full_b | mobility_carry | assessment
AssessmentId     = push_up_max | dead_hang_s | plank_s | wall_angel_reach
                 | goblet_squat_quality | farmer_carry_s
TargetTier       = low | ok | strong
GapId            = AssessmentId | body_comp

GarminCategory   = PUSH_UP | SQUAT | DEADLIFT | ROW | PLANK | CARRY | LUNGE
                 | HIP_RAISE | CORE | SHOULDER_PRESS | BENCH_PRESS | PULL_UP
                 | FLYE | LATERAL_RAISE | HYPEREXTENSION | TOTAL_BODY
                 | WARM_UP | CARDIO | UNKNOWN
                   # best-effort; verify against the garminconnect enum, default UNKNOWN

Settings:
  days_per_week        : int   2..6,  default 4
  session_length_min   : int   {15, 30, 45}, default 30
  son_age              : int | None            # band derived; u10 until set
  weights_available    : str                   # §8.2 grammar
  has_overhead_anchor  : bool  default false
  bodyweight_kg        : float | None          # per profile, from the scale
```

### Defaults at a glance

| setting | default |
|---|---|
| `days_per_week` | 4 |
| `session_length_min` | 30 |
| age band before onboarding | `u10` |
| `has_overhead_anchor` | `false` |
| block length | 4 weeks, week 4 deload |
| assessment cadence | baseline, then every 28 days |
| challenges per cycle | ≤ 3 |
| dumbbell ladder | 2.27–23.81 kg, step 1.13 kg |
| kettlebell ladder | {16.0, 24.0} kg |
