"""Builders for validator tests: a tiny exercise catalog and workout constructors."""

from __future__ import annotations

from typing import Any

from cadence.schema import (
    AgeBand,
    Equipment,
    Exercise,
    ExerciseTag,
    LoadType,
    LoadUnit,
    Measure,
    Pattern,
    Region,
    YouthAllow,
)

YOUTH_BANDS = (AgeBand.U10, AgeBand.AGE_10_13, AgeBand.AGE_14_17)
ALL_YOUTH_YES = dict.fromkeys(YOUTH_BANDS, YouthAllow.YES)
OLDER_YOUTH_YES = {AgeBand.AGE_10_13: YouthAllow.YES, AgeBand.AGE_14_17: YouthAllow.YES}

ALL_EQUIPMENT: list[Equipment] = list(Equipment)


def make_exercise(**overrides: Any) -> Exercise:
    base: dict[str, Any] = {
        "id": "push-up",
        "name": "Push-up",
        "pattern": Pattern.PUSH_H,
        "region": Region.UPPER,
        "load_type": LoadType.BODYWEIGHT,
        "load_unit": LoadUnit.BODYWEIGHT,
        "measure": Measure.REPS,
        "equipment": [Equipment.BODYWEIGHT],
        "cue": "Squeeze the glutes, ribs down, elbows at 45 degrees.",
        "youth_ok_by_band": ALL_YOUTH_YES,
    }
    base.update(overrides)
    return Exercise.model_validate(base)


PUSH_UP = make_exercise()
PLANK = make_exercise(
    id="plank",
    name="Plank",
    pattern=Pattern.BRACE,
    region=Region.CORE,
    measure=Measure.SECONDS,
    cue="Ribs down, glutes on, breathe.",
)
DB_ROW = make_exercise(
    id="db-bent-row",
    name="Dumbbell bent-over row",
    pattern=Pattern.PULL_H,
    load_type=LoadType.DUMBBELL,
    load_unit=LoadUnit.PER_HAND,
    equipment=[Equipment.DUMBBELLS],
    cue="Flat back, drive the elbow past the ribs.",
    youth_ok_by_band=OLDER_YOUTH_YES,
)
GOBLET_SQUAT = make_exercise(
    id="goblet-squat",
    name="Goblet squat",
    pattern=Pattern.SQUAT,
    region=Region.LOWER,
    load_type=LoadType.DUMBBELL,
    load_unit=LoadUnit.PER_IMPLEMENT,
    equipment=[Equipment.DUMBBELLS],
    cue="Sit down tall, elbows inside the knees.",
    youth_ok_by_band=OLDER_YOUTH_YES,
)
KB_SWING = make_exercise(
    id="kb-swing",
    name="Kettlebell swing",
    pattern=Pattern.HINGE,
    region=Region.FULL,
    load_type=LoadType.KETTLEBELL,
    load_unit=LoadUnit.PER_IMPLEMENT,
    equipment=[Equipment.KETTLEBELLS],
    # Principles section 3.6: legal for 14-17 only two-handed at 16 kg and only above 45.7 kg
    # bodyweight, so the column is Y* and an unweighed teenager is denied.
    cue="Snap the hips, the arms are ropes.",
    youth_ok_by_band={AgeBand.AGE_14_17: YouthAllow.CONDITIONAL},
)
DEAD_HANG = make_exercise(
    id="dead-hang",
    name="Dead hang",
    pattern=Pattern.PULL_V,
    measure=Measure.SECONDS,
    tags=[ExerciseTag.REQUIRES_ANCHOR, ExerciseTag.GRIP_LIMITED],
    cue="Shoulders active, breathe slow.",
)
MAX_PUSH_UP = make_exercise(
    id="push-up-max-test",
    name="Push-up max test",
    tags=[ExerciseTag.MAX_EFFORT, ExerciseTag.ASSESSMENT_ONLY],
    cue="As many clean reps as you can.",
)
INCLINE_PUSH_UP = make_exercise(
    id="incline-push-up",
    name="Incline push-up",
    load_type=LoadType.BENCH_ASSISTED,
    equipment=[Equipment.BODYWEIGHT, Equipment.BENCH],
    cue="Hands on the bench, body one line.",
)

# Declares every youth band as "no": the subject of tests 33 and 35.
NO_YOUTH = make_exercise(
    id="hollow-hold",
    name="Hollow hold",
    pattern=Pattern.BRACE,
    region=Region.CORE,
    measure=Measure.SECONDS,
    cue="Low back pinned to the floor.",
    youth_ok_by_band=dict.fromkeys(YOUTH_BANDS, YouthAllow.NO),
)
# Declares no bands at all: the allowlist must read this as a denial, not as permission.
BAND_OMITTED = make_exercise(
    id="bird-dog",
    name="Bird dog",
    pattern=Pattern.BRACE,
    region=Region.CORE,
    cue="Reach long, hips level.",
    youth_ok_by_band={},
)

CATALOG: dict[str, Exercise] = {
    e.id: e
    for e in (
        PUSH_UP,
        PLANK,
        DB_ROW,
        GOBLET_SQUAT,
        KB_SWING,
        DEAD_HANG,
        MAX_PUSH_UP,
        INCLINE_PUSH_UP,
        NO_YOUTH,
        BAND_OMITTED,
    )
}


def make_row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "exercise_id": "push-up",
        "sets": 2,
        "reps": 8,
        "load_unit": LoadUnit.BODYWEIGHT.value,
        "rest_s": 60,
    }
    base.update(overrides)
    return base


def make_workout(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "upper-a-w1",
        "name": "Upper A",
        "day_type": "upper_a",
        "target_profile_kind": "both",
        "rows": [make_row()],
        "estimated_minutes": 20,
    }
    base.update(overrides)
    return base
