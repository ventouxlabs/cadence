"""The library DSL - acceptance tests 10-15, plus the two structural guards from PRP-00 section 9."""

from __future__ import annotations

import inspect
import re

import pytest
from pydantic import BaseModel, RootModel, ValidationError

import cadence.schema as schema_module
from cadence.schema import Exercise, GarminCategory, WorkoutRow

VALID_EXERCISE = {
    "id": "db-bent-row",
    "name": "Dumbbell bent-over row",
    "pattern": "pull_h",
    "region": "upper",
    "load_type": "dumbbell",
    "load_unit": "per_hand",
    "measure": "reps",
    "equipment": ["dumbbells"],
    "tags": ["unilateral"],
    "cue": "Flat back, drive the elbow past the ribs.",
    "garmin_category": "ROW",
    "garmin_exercise": "BENT_OVER_ROW_WITH_DUMBBELL",
    "est_seconds_per_set": 45,
}


def test_exercise_roundtrip() -> None:
    exercise = Exercise.model_validate(VALID_EXERCISE)
    dumped = exercise.model_dump(mode="json", exclude_none=True)
    assert Exercise.model_validate(dumped) == exercise
    assert dumped["id"] == "db-bent-row"
    assert dumped["garmin_category"] == "ROW"


def test_exercise_rejects_extra_key() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        Exercise.model_validate({**VALID_EXERCISE, "sneaky": "barbell"})


def test_exercise_rejects_bad_slug() -> None:
    with pytest.raises(ValidationError):
        Exercise.model_validate({**VALID_EXERCISE, "id": "Push Up"})


def test_garmin_category_has_no_unknown_member() -> None:
    assert "UNKNOWN" not in {c.value for c in GarminCategory}
    assert "UNKNOWN" not in {c.name for c in GarminCategory}


def test_garmin_exercise_requires_category() -> None:
    doc = {**VALID_EXERCISE, "garmin_category": None}
    with pytest.raises(ValidationError, match="garmin_category"):
        Exercise.model_validate(doc)


@pytest.mark.parametrize(
    "measures",
    [
        {},
        {"reps": 10, "seconds": 30},
        {"reps": 10, "meters": 20.0},
        {"seconds": 30, "steps": 40},
        {"reps": 10, "seconds": 30, "meters": 20.0},
        {"reps": 10, "seconds": 30, "meters": 20.0, "steps": 40},
    ],
    ids=["zero", "two-a", "two-b", "two-c", "three", "four"],
)
def test_row_requires_exactly_one_measure(measures: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        WorkoutRow.model_validate(
            {"exercise_id": "push-up", "sets": 2, "load_unit": "bodyweight", "rest_s": 60, **measures}
        )


def _exported_models() -> list[type[BaseModel]]:
    models = []
    for name in schema_module.__all__:
        obj = getattr(schema_module, name)
        if inspect.isclass(obj) and issubclass(obj, BaseModel) and not issubclass(obj, RootModel):
            models.append(obj)
    return models


def test_every_exported_model_forbids_extra_and_is_frozen() -> None:
    """One model with extra='allow' would silently reopen import. Assert it structurally."""
    models = _exported_models()
    assert len(models) >= 6
    for model in models:
        assert model.model_config.get("extra") == "forbid", f"{model.__name__} does not forbid extra keys"
        assert model.model_config.get("frozen") is True, f"{model.__name__} is not frozen"


def test_garmin_category_members_are_upper_snake() -> None:
    """A typo that slipped through would earn a Garmin 400 rather than a local failure."""
    for category in GarminCategory:
        assert re.fullmatch(r"[A-Z][A-Z0-9_]*", category.value), category.value
        assert category.name == category.value


def test_cue_must_be_one_line() -> None:
    with pytest.raises(ValidationError, match="single line"):
        Exercise.model_validate({**VALID_EXERCISE, "cue": "Flat back.\nDrive the elbow."})


def test_equipment_list_is_deduped() -> None:
    exercise = Exercise.model_validate({**VALID_EXERCISE, "equipment": ["dumbbells", "dumbbells", "bench"]})
    assert [e.value for e in exercise.equipment] == ["dumbbells", "bench"]


def test_is_whitelisted_rejects_non_equipment_values() -> None:
    from cadence.schema import is_whitelisted
    from cadence.schema.enums import Equipment

    assert is_whitelisted(Equipment.BENCH) is True
    assert is_whitelisted("bench") is True
    assert is_whitelisted("barbell") is False
    assert is_whitelisted(7) is False
    assert is_whitelisted(None) is False


def test_youth_rules_accessors(youth_rules) -> None:
    from cadence.schema import AgeBand, YouthRules

    parsed = YouthRules(root=youth_rules)
    assert AgeBand.U10 in parsed
    assert parsed[AgeBand.U10].band is AgeBand.U10
    assert parsed.get(AgeBand.ADULT) is not None


def test_progression_field_names_match_principles() -> None:
    """A rename back to rep_low/regress_triggers, or a dropped field, breaks PRP-07 silently."""
    from cadence.schema import Progression

    assert set(Progression.model_fields) == {
        "id",
        "type",
        "rep_min",
        "rep_max",
        "load_step_kg",
        "load_step_pct",
        "time_step_s",
        "distance_step_m",
        "regress_on",
        "regress_step",
        "regress_reps",
        "deload_pct",
        "allow_load_progression",
        "cap_load_kg",
    }


def test_progression_defaults() -> None:
    """deload_pct scales carry distance directly, so 0.60 is not interchangeable with 0.40."""
    from cadence.schema import Progression

    progression = Progression(id="db-double", type="double_progression")
    assert progression.deload_pct == 0.60
    assert progression.regress_step == 0.10
    assert progression.regress_reps == 2
    assert progression.allow_load_progression is True
    assert progression.cap_load_kg is None
