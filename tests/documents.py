"""Workout documents the import and generate tests share.

Kept out of ``conftest.py`` on purpose: these are fixtures of PRP-08 alone, and ``conftest.py``
is shared merge surface with the branches building PRP-06 and PRP-07.
"""

from __future__ import annotations

import json
from typing import Any

import yaml

ADULT_WORKOUT = """id: my-upper
name: My upper
day_type: upper_a
target_profile_kind: adult
estimated_minutes: 30
notes: pasted by hand
rows:
  - exercise_id: push-up
    sets: 3
    reps: 10
    load_unit: bodyweight
    rest_s: 60
  - exercise_id: db-bent-row
    sets: 3
    reps: 10
    load_kg: 9.0
    load_unit: per_hand
    rest_s: 60
  - exercise_id: plank
    sets: 2
    seconds: 45
    load_unit: bodyweight
    rest_s: 45
"""

# Valid for the son at ``age_10_13``: bodyweight only, two sets, reps inside 5..20, and a session
# well inside the band's 25-minute ceiling.
YOUTH_WORKOUT = """id: son-fun
name: Son fun
day_type: upper_a
target_profile_kind: youth
estimated_minutes: 15
rows:
  - exercise_id: push-up
    sets: 2
    reps: 10
    load_unit: bodyweight
    rest_s: 45
  - exercise_id: bodyweight-squat
    sets: 2
    reps: 12
    load_unit: bodyweight
    rest_s: 45
"""

# A loaded youth row at five reps: under ``rep_min_loaded`` for every youth band (V4).
YOUTH_FIVE_REPS = """id: son-heavy
name: Son heavy
day_type: upper_a
target_profile_kind: youth
estimated_minutes: 15
rows:
  - exercise_id: db-bent-row
    sets: 3
    reps: 5
    load_kg: 4.0
    load_unit: per_hand
    rest_s: 60
"""

YOUTH_KETTLEBELL = """id: son-kb
name: Son kettlebell
day_type: lower_a
target_profile_kind: youth
estimated_minutes: 15
rows:
  - exercise_id: kb-deadlift
    sets: 2
    reps: 10
    load_kg: 16.0
    load_unit: per_implement
    rest_s: 75
"""

# ``kb-swing`` is section 3.6's ``conditional`` movement: legal at ``age_14_17`` only two-handed at
# 16 kg and only when 0.35 x bodyweight reaches it. At 40 kg it does not, and at an unknown
# bodyweight it is denied outright - which is what proves the route passes the real number.
YOUTH_KB_SWING = """id: son-swing
name: Son swing
day_type: lower_a
target_profile_kind: youth
estimated_minutes: 15
rows:
  - exercise_id: kb-swing
    sets: 2
    reps: 10
    load_kg: 16.0
    load_unit: per_implement
    rest_s: 75
"""

BARBELL_WORKOUT = """id: barbell-day
name: Barbell day
day_type: lower_a
target_profile_kind: adult
estimated_minutes: 30
rows:
  - exercise_id: barbell-back-squat
    sets: 5
    reps: 5
    load_kg: 100.0
    load_unit: total
    rest_s: 180
exercises:
  - id: barbell-back-squat
    name: Barbell back squat
    pattern: squat
    region: lower
    load_type: barbell
    load_unit: total
    measure: reps
    equipment: [barbell]
    cue: Brace hard, sit between the hips.
"""

INLINE_EXERCISE_WORKOUT = """id: inline-day
name: Inline day
day_type: upper_a
target_profile_kind: adult
estimated_minutes: 20
rows:
  - exercise_id: towel-row
    sets: 3
    reps: 12
    load_unit: bodyweight
    rest_s: 60
exercises:
  - id: towel-row
    name: Towel row
    pattern: pull_h
    region: upper
    load_type: bodyweight
    load_unit: bodyweight
    measure: reps
    equipment: [bodyweight]
    cue: Elbows past the ribs, shoulders down.
"""

# The mock gateway's own reply, which is also what ``CADENCE_OMNIROUTE_MODE=mock`` returns.
GENERATED_ADULT = """id: generated-posture
name: Posture focus
day_type: upper_a
target_profile_kind: adult
estimated_minutes: 20
rows:
  - exercise_id: wall-angel
    sets: 2
    reps: 10
    load_unit: bodyweight
    rest_s: 45
  - exercise_id: prone-ytw-raise
    sets: 2
    reps: 12
    load_unit: bodyweight
    rest_s: 45
  - exercise_id: dead-bug
    sets: 2
    reps: 8
    load_unit: bodyweight
    rest_s: 45
"""

GENERATED_YOUTH = """id: generated-son
name: Son posture
day_type: upper_a
target_profile_kind: youth
estimated_minutes: 15
rows:
  - exercise_id: wall-angel
    sets: 2
    reps: 10
    load_unit: bodyweight
    rest_s: 45
  - exercise_id: dead-bug
    sets: 2
    reps: 8
    load_unit: bodyweight
    rest_s: 45
"""


def as_dict(text: str) -> dict[str, Any]:
    """The document as a mapping, for the accept-path tests that tamper with it."""
    return dict(yaml.safe_load(text))


def as_json(text: str) -> str:
    return json.dumps(as_dict(text))


def with_rows(text: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The same document with its rows replaced - how a tampered preview is built."""
    document = as_dict(text)
    document["rows"] = rows
    return document


def many_rows(count: int) -> str:
    """A document with ``count`` rows, for the row-count cap."""
    rows = "\n".join(
        "  - exercise_id: push-up\n    sets: 2\n    reps: 10\n    load_unit: bodyweight\n    rest_s: 45"
        for _ in range(count)
    )
    return (
        "id: too-many\nname: Too many\nday_type: upper_a\ntarget_profile_kind: adult\n"
        f"estimated_minutes: 30\nrows:\n{rows}\n"
    )
