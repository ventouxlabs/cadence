# PRP-03 — Profiles, Setup, Settings, youth wiring

Branch `prp/03-profiles-settings`. Depends on PRP-00 (schema, validator, config) and PRP-01 (`profile`/`setting` tables, `age_band`, `parse_weights_available`, `build_program`). Consumed by 02, 04, 07, 08, 10.

## Goal

Everything JD was never asked. A one-time Setup screen that gates the app until the son's age is set, the same fields editable later under Settings, and the youth band wired from that age through the validator, the program engine and every template the son can see.

## Scope in

- `cadence/profils/` services over PRP-01's tables.
- `GET /` redirect logic, `GET|POST /setup`, `GET|POST /settings`.
- `GET|PUT /api/settings`, `GET|PUT /api/profiles/{id}`.
- Program rebuild on a relevant setting change, preserving completed work.
- Youth wiring end to end: band derivation, template guards, `has_overhead_anchor`.
- The Settings page containers into which PRP-08 renders Import and Generate.

## Scope out

- Import and Generate internals and their partials — **owned by PRP-08**. This PRP ships the two empty sections and the exact partial names.
- The Today screen — **owned by PRP-02**. History — **PRP-04**. Assessments — **PRP-07**.
- VitalForge connectivity checks — **owned by PRP-06**. Person slugs are plain text fields here.
- `profile`/`setting` DDL and `age_band()` — **owned by PRP-01**. PRP-00's deferral table assigns them here; that is corrected in PRP-01. This PRP owns the services and screens over those tables, not the tables.

## Data model

No new tables. Setting keys, all stored as JSON in `setting.value_json`:

| key | type | default | validation |
|---|---|---|---|
| `equipment` | `list[str]` | `["bodyweight","dumbbells","kettlebells","bench"]` | subset of the 4-id whitelist, `bodyweight` always present |
| `weights_available` | `str` | `"DB 5-52.5 lb adj step 2.5, KB 16/24 kg, BENCH adjustable"` | parsed by PRP-01; warnings surfaced, never fatal |
| `days_per_week` | `int` | 4 | 2–6 inclusive |
| `session_minutes` | `int` | 30 | one of 15, 30, 45 |
| `push_son_to_garmin` | `bool` | false | — |
| `setup_complete` | `bool` | false | set true only by a successful `POST /setup` |
| `timers_default_on` | `bool` | false | — |
| `readiness_nudge_on` | `bool` | true | — |
| `display_unit` | `str` | `"kg"` | `kg` or `lb`; **added by this PRP** |

`display_unit` is not in architecture §3's key list. Adding a key is an addition, but log a `docs/DECISIONS.md` entry naming it so the reviewer does not read it as invention. Storage stays kg everywhere; `display_unit` affects rendering only, via one Jinja filter `|load(profile)`.

Profile fields edited here: `display_name`, `age_years` (+ `age_recorded_on`), `vitalforge_person`, `push_to_garmin`, `bodyweight_kg`, `has_overhead_anchor`.

### Band derivation

```python
def age_band(profile) -> AgeBand           # PRP-01; this PRP only calls it
# adult kind          -> "adult" at any age
# youth kind, age None-> "u10"            (§3.8 strictest default)
# youth, <10          -> "u10"
# youth, 10..13       -> "age_10_13"
# youth, 14..17       -> "age_14_17"
# youth, >=18         -> "age_14_17"      (a youth profile never becomes adult; kind is authority)
```

`profile.age_band` is recomputed and written on every profile save and on app startup.

## UI surface

### `GET /setup` — 390 px, one column, scrolls

```
┌──────────────────────────────────────┐
│ Set up Cadence                       │
│ Two profiles, five questions.        │
├──────────────────────────────────────┤
│ Son's age                            │
│ [   12   ]  years            *needed │
│ Until this is set he trains on the   │
│ strictest rules.                     │
├──────────────────────────────────────┤
│ Equipment                            │
│ ☑ Bodyweight   ☑ Dumbbells           │
│ ☑ Kettlebells  ☑ Adjustable bench    │
│                                      │
│ Weights available                    │
│ [ DB 5-52.5 lb adj, KB 16/24 kg    ] │
│ ✓ Dumbbells 2.27–23.81 kg (20 rungs) │  ← live preview, HTMX on keyup 400ms
│ ✓ Kettlebells 16, 24 kg              │
├──────────────────────────────────────┤
│ Days per week                        │
│ [ 2 ][ 3 ][*4*][ 5 ][ 6 ]            │
│                                      │
│ Session length                       │
│ [ 15 ][* 30 *][ 45 ]  minutes        │
├──────────────────────────────────────┤
│ Pull-up bar or sturdy beam?          │
│ [ Off |*On* ]                        │
├──────────────────────────────────────┤
│ Push son's sessions to Garmin        │
│ [*Off*| On ]                         │
│ His sessions are always saved in     │
│ VitalForge. This only controls       │
│ whether they also appear in your     │
│ Garmin account.                      │
├──────────────────────────────────────┤
│ Show weights in                      │
│ [* kg *][ lb ]                       │
├──────────────────────────────────────┤
│ VitalForge people                    │
│ Me  [ jd            ]                │
│ Son [ son           ]                │
├──────────────────────────────────────┤
│        [    Start training    ]      │
└──────────────────────────────────────┘
```

Tap flow: age → equipment (already ticked) → Start. Everything else has a working default, so the minimum path to a usable app is one number and one tap.

Bad input renders in place, never on a separate page:

```
│ Son's age                            │
│ [   4    ]  years                    │
│ ⚠ Enter an age between 3 and 19.     │
```

### `GET /settings`

Same form, plus, below it:

```
├──────────────────────────────────────┤
│ Import workout                       │   ← container only; PRP-08 fills it
│ (rendered by partials/import_section)│
├──────────────────────────────────────┤
│ Generate workout                     │   ← container only; PRP-08 fills it
│ (rendered by partials/generate_...)  │
├──────────────────────────────────────┤
│ [ Save ]        Changing days per    │
│                 week or length will  │
│                 rebuild the plan.    │
└──────────────────────────────────────┘
```

### Partial names (contract with PRP-08)

| Partial | Owner | Purpose |
|---|---|---|
| `partials/settings_form.html` | 03 | the whole form, re-rendered on save |
| `partials/weights_preview.html` | 03 | live parse result for `weights_available` |
| `partials/field_error.html` | 03 | one inline error line |
| `partials/import_section.html` | 03 (empty), 08 (content) | textarea, file input, target profile |
| `partials/import_result.html` | 08 | validation result |
| `partials/generate_section.html` | 03 (empty), 08 (content) | goal + gap selects, Generate button |
| `partials/generate_preview.html` | 08 | read-only preview + Accept / Discard |

HTMX targets: `#weights-preview`, `#import-result`, `#generate-preview`.

### Routes

`GET /` → `/setup` while `setup_complete` is false, else `/today`. `/setup` when already complete redirects to `/settings` rather than letting the gate be re-run by accident.

`GET /api/settings` →

```json
{"ok": true, "error": null, "meta": {},
 "data": {"equipment": ["bodyweight","dumbbells","kettlebells","bench"],
          "weights_available": "DB 5-52.5 lb adj step 2.5, KB 16/24 kg",
          "days_per_week": 4, "session_minutes": 30, "push_son_to_garmin": false,
          "setup_complete": true, "timers_default_on": false,
          "readiness_nudge_on": true, "display_unit": "kg",
          "weights_warnings": []}}
```

`PUT /api/settings` takes a partial patch; unknown keys are rejected with 422 naming them. Response repeats the full settings plus `{"rebuilt": ["me", "son"]}` when a rebuild ran.

`PUT /api/profiles/son` body `{"age_years": 12, "bodyweight_kg": 41.5, "has_overhead_anchor": true}` → 200 with the stored profile including the derived `age_band`. Setting `kind` through the API is rejected: 422.

## Implementation notes

- Files: `cadence/profils/{services.py,validation.py}`, `cadence/web/routers/settings.py`, `cadence/api/settings.py`, `cadence/api/profiles.py`, `cadence/web/templates/{setup.html,settings.html}` and the partials above.
- Services are immutable: `update_settings(session, patch) -> Settings` builds and returns a new `Settings` object; it never mutates the one it was handed.

```python
def get_settings(session) -> Settings: ...
def update_settings(session, patch: dict) -> Settings: ...          # validates, writes, returns new
def get_profile(session, pid: str) -> Profile: ...
def update_profile(session, pid: str, patch: dict) -> Profile: ...   # recomputes age_band
def youth_ruleset_for(profile, library) -> YouthRuleSet: ...
def rebuild_programs(session, library, reason: str) -> list[str]: ...
```

- **Rebuild rule.** `days_per_week`, `session_minutes`, `equipment`, `weights_available`, and a profile's `age_years`, `bodyweight_kg` or `has_overhead_anchor` trigger `rebuild_programs`. `display_unit`, `readiness_nudge_on`, `timers_default_on`, `push_son_to_garmin`, `vitalforge_person` and `display_name` do not.
- **Rebuild preserves history.** Delete only `planned_session` rows with `status == "planned"` that have no `session` attached; keep every `done` and `skipped` row and every `session`. Re-run `build_program` and append the new planned sessions after the last completed one, renumbering `week`/`day_index` from there. A rebuild never changes a finished session and never resets the block to week 1 unless the block had no completed sessions at all.
- **Equipment whitelist** is `cadence/schema/equipment.py` (PRP-00). Never accept a value outside it: `"barbell"` → 422 listing the four legal ids. `bodyweight` cannot be unticked.
- **Age bounds**: 3–19 inclusive on `age_years` for a youth profile. Below 3 or above 19 is a data-entry error, not a band. `age_recorded_on` is stamped on every change so PRP-07's 28-day cadence and any future birthday roll have a date to work from.
- **Weights preview** is a `POST` to `/settings/weights/preview` returning `partials/weights_preview.html`; it calls PRP-01's parser and renders ladder summaries plus one warning line per unparsed token. Debounce 400 ms with `hx-trigger="keyup changed delay:400ms"`.
- **Youth template guard.** Every template that could render a body metric wraps it in `{% if profile.kind != "youth" %}`. Add one Jinja global `youth = profile.kind == "youth"` in the base context so the guard reads the same everywhere. The son's Today, History and Settings profile card show no weight, body-fat, muscle, lean-mass or appearance field, and no trend line.
- **The banned-word test needs care.** `\bweight\b` does not match inside "Bodyweight", so `Bodyweight squat` is safe. But `\blean\b` **does** match the legal `suitcase-carry` cue "refuse to lean". So: check banned *phrases* over the full visible text (`body fat`, `bodyfat`, `body-fat`, `body composition`, `muscle %`, `muscle percent`, `lean mass`, `body image`), and check the bare words `weight`, `fat`, `lean`, `abs`, `calories` only inside elements marked `data-metric` or `data-challenge-name`. Never edit seed cues to make the test pass.
- Load display: one filter, `{{ row.load_kg|load(display_unit) }}` → `"14 kg"` or `"31 lb"`, using `lb = kg / 0.45359237` rounded to the nearest 0.5 lb. Storage is always kg.
- Person slugs default from `VITALFORGE_PERSON_ME` / `VITALFORGE_PERSON_SON` (D-017); blank env means a blank field, never a guess.

## Acceptance tests

`tests/test_profiles.py`, `tests/test_settings_api.py`, `tests/e2e/test_setup.py`.

1. `test_root_redirects_to_setup_until_complete` — `GET /` → 307/303 to `/setup`; after `POST /setup` → `/today`.
2. `test_setup_requires_son_age` — `POST /setup` without `age_years` → 422, `setup_complete` still false.
3. `test_age_band_mapping` — parametrised: youth 8 → `u10`, 10 → `age_10_13`, 13 → `age_10_13`, 14 → `age_14_17`, 17 → `age_14_17`, 18 → `age_14_17`; adult kind at 12 → `adult`.
4. `test_strictest_band_when_age_unset` — youth with `age_years=None` → `u10`.
5. **Negative** `test_days_per_week_bounds` — 1 → 422, 7 → 422, 2 and 6 accepted.
6. **Negative** `test_session_minutes_enum` — 50 → 422, 20 → 422; 15/30/45 accepted.
7. **Negative** `test_equipment_whitelist` — `["barbell"]` → 422 naming the four legal ids; `["dumbbells"]` without `bodyweight` → 422.
8. **Negative** `test_unknown_setting_key_rejected` — `{"turbo": true}` → 422 naming `turbo`.
9. **Negative** `test_profile_kind_immutable` — `PUT /api/profiles/son {"kind": "adult"}` → 422.
10. **Negative** `test_age_out_of_range` — 2 → 422, 20 → 422.
11. `test_rebuild_preserves_done_sessions` — complete three sessions, change `days_per_week` 4 → 3, assert the three `done` planned sessions and their `session` rows survive, only `planned` ones were replaced, and the new sessions follow the last completed one.
12. `test_rebuild_not_triggered_by_display_unit` — changing `display_unit` leaves every `planned_session.id` unchanged.
13. `test_weights_preview_parses_and_warns` — good text yields two ladders and no warning; `"DB banana"` yields a warning naming the token and a 200 response.
14. `test_display_unit_rendering` — `load` filter gives `14 kg` and `31 lb` for 14.0.
15. `test_settings_roundtrip` — `PUT` then `GET` returns exactly what was written.
16. `test_youth_ruleset_for` — a `son` at `age_10_13` yields `max_load_kg_per_hand == 5.0`, `good_enough_done_after_n_exercises == 3`.
17. `test_has_overhead_anchor_rebuilds` — toggling it on the parent changes the `upper_b` slot-4 exercise in the rebuilt plan.
18. `e2e_setup_minimum_path` — 390×844: type an age, tap Start, land on `/today`. Assert exactly two taps after typing.
19. `e2e_setup_shows_inline_error` — submit with age 4, the error appears in place and the page does not navigate.
20. `e2e_weights_live_preview` — typing `KB 16/24 kg` updates `#weights-preview` without a page load.
21. `e2e_son_today_has_no_body_words` — grep the son's `/today` DOM text for the banned phrases and for bare words inside `[data-metric]` / `[data-challenge-name]`; assert none, and assert the page does contain "Bodyweight squat" so the test is proven non-vacuous.
22. `e2e_son_history_has_no_trend_line` — no `svg[data-role="trend"]` on the son's `/history`.
23. `e2e_settings_has_import_and_generate_containers` — `#import-result` and `#generate-preview` exist and are empty before PRP-08.

## Devil's-advocate risks

1. **Rebuild wiping history.** The obvious implementation deletes all planned sessions. Mitigation: the rebuild rule above and test 11, which asserts survivors, not just a count.
2. **Rebuild loop.** A rebuild that writes a setting that triggers a rebuild recurses. Mitigation: `rebuild_programs` never writes settings; it is called once at the end of `update_settings`.
3. **Band recomputed only at setup**, so a son who turns 14 keeps `age_10_13` forever. Mitigation: recompute on startup and on every profile save; `age_recorded_on` records when.
4. **A youth profile aged 18 becoming adult** and unlocking kettlebells. Mitigation: `kind` is the authority, band clamps at `age_14_17`, test 3.
5. **`display_unit` leaking into storage.** A lb value written to `load_planned_kg` corrupts every cap check. Mitigation: conversion lives only in the Jinja filter; no service accepts lb; test 14 checks the filter, and PRP-02's adjust stepper works in ladder rungs, not display units.
6. **Banned-word test false positive on "refuse to lean"** breaking CI and tempting someone to edit seed content. Mitigation: the phrase/element split above, called out by name, plus the non-vacuity assertion in test 21.
7. **Setup bypass.** Deep-linking `/today` before setup renders the son a loaded session. Mitigation: a dependency on every HTML route redirecting to `/setup` while `setup_complete` is false, tested by requesting `/today` directly.
8. **Equipment unticked mid-block** leaves planned sessions referencing an absent implement. Mitigation: equipment is a rebuild trigger.
9. **Person slug guessed from the display name.** Mitigation: blank stays blank (D-017); test that a blank env yields an empty field.
10. **Free-text `weights_available` used as a code path.** It is parsed, never `eval`'d; unparsed tokens are ignored with a warning (§8.2), never guessed.

## Done when

- [ ] `GET /` gates on `setup_complete`; `/setup` cannot be bypassed by deep-linking any HTML route.
- [ ] Setup completes in one typed number and one tap.
- [ ] All nine settings keys round-trip through `PUT|GET /api/settings` with the validation above.
- [ ] Changing days/week, length, equipment, weights, age, bodyweight or anchor rebuilds only `planned` sessions.
- [ ] The son's Today, History and Settings render no body-composition or appearance language.
- [ ] Import and Generate containers exist with the agreed partial names and ids.
- [ ] All 23 acceptance tests pass; `make lint && make test && make e2e` green.
- [ ] `docs/DECISIONS.md` records the `display_unit` setting key and the 18+ youth clamp.
