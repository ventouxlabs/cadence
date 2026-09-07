You write one strength-training workout for a home gym. Answer with YAML only.

## Who this is for

- Profile kind: [[profile_kind]]
- Age band: [[age_band]]
- Goal: [[goal]]
- Named gap to chase: [[gap_name]]

That is everything you are told about this person. There is no name, no age, no bodyweight and no
training history here, and you must not ask for any of it or invent any of it.

## What they train with

- Equipment: [[equipment_ids]]
- Weights on the rack: [[weight_ranges]]
- Days per week: [[days_per_week]]
- Session length in minutes: [[session_minutes]]

## Rules for this age band

[[youth_rules]]

## The only exercises you may prescribe

Use these ids exactly. Do not invent an exercise, do not rename one, and do not prescribe a
movement that is not on this list.

[[allowed_exercises]]

## The shape of your answer

Reply with exactly one YAML document and nothing else: no prose before it, no explanation after
it, no code fence, no second document. Every field below is required unless marked optional.

```
[[schema_example]]
```

Field rules:

- `id` is a lowercase kebab-case slug.
- `day_type` is one of: upper_a, lower_a, upper_b, lower_full_b, mobility_carry.
- `target_profile_kind` must be `[[profile_kind]]`.
- Each row sets exactly one of `reps`, `seconds`, `meters` or `steps`, and it must be the measure
  the exercise is defined in.
- `load_unit` must be the unit the exercise is held in: `bodyweight` for a bodyweight movement,
  `per_hand` for dumbbells held one in each hand, `per_implement` for a two-handed kettlebell.
- A `bodyweight` row carries no `load_kg` at all.
- `rest_s` is seconds of rest after the row.
- `estimated_minutes` must not exceed the session length above.
- `cue` text is one short line. Never put a URL, a template delimiter, a script tag or any other
  markup in any text field.
