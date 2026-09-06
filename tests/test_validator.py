"""The validator gate - acceptance tests 16-23."""

from __future__ import annotations

from typing import Any

import pytest

from cadence.schema import AgeBand, Equipment, YouthRuleSet
from cadence.validateur import codes, validate_exercise, validate_workout
from cadence.validateur.youth_rules import effective_cap
from tests.factories import ALL_EQUIPMENT, CATALOG, make_row, make_workout


def adult(doc: Any, **overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "profile_kind": "adult",
        "age_band": AgeBand.ADULT,
        "equipment": ALL_EQUIPMENT,
        "exercises": CATALOG,
    }
    kwargs.update(overrides)
    return validate_workout(doc, **kwargs)


def codes_in(result: Any) -> set[str]:
    return {error.code for error in result.errors}


def test_valid_adult_workout_passes() -> None:
    result = adult(make_workout())
    assert result.ok is True
    assert result.errors == []


def test_unknown_exercise_id_flagged() -> None:
    result = adult(make_workout(rows=[make_row(exercise_id="barbell-back-squat")]))
    assert result.ok is False
    assert result.errors[0].code == codes.UNKNOWN_EXERCISE
    assert result.errors[0].path == "rows[0].exercise_id"


def test_measure_mismatch_flagged() -> None:
    row = make_row(exercise_id="push-up", reps=None, seconds=30)
    result = adult(make_workout(rows=[row]))
    assert result.ok is False
    assert codes.MEASURE_MISMATCH in codes_in(result)


def test_equipment_not_available_flagged() -> None:
    row = make_row(exercise_id="db-bent-row", load_unit="per_hand", load_kg=10.0)
    result = adult(make_workout(rows=[row]), equipment=[Equipment.BODYWEIGHT])
    assert result.ok is False
    assert codes.EQUIPMENT_NOT_AVAILABLE in codes_in(result)


def test_all_errors_collected_not_short_circuited() -> None:
    rows = [
        make_row(exercise_id="barbell-back-squat"),
        make_row(exercise_id="plank", reps=10),
        make_row(exercise_id="db-bent-row", load_unit="per_hand", load_kg=10.0),
    ]
    result = adult(make_workout(rows=rows), equipment=[Equipment.BODYWEIGHT])
    assert result.ok is False
    assert codes_in(result) == {
        codes.UNKNOWN_EXERCISE,
        codes.MEASURE_MISMATCH,
        codes.EQUIPMENT_NOT_AVAILABLE,
    }
    assert len(result.errors) == 3


def test_warn_only_result_is_ok(youth_rules: dict[AgeBand, YouthRuleSet]) -> None:
    """A document whose only finding is a warning still passes.

    Callers gate on ``.ok``, never on ``len(.errors)``. The rep ceiling and the RPE cap became
    errors (D-037), so the band coercion is now the one advisory finding left.
    """
    rules = youth_rules[AgeBand.U10]
    result = validate_workout(
        make_workout(rows=[make_row()], estimated_minutes=rules.max_session_minutes),
        profile_kind="youth",
        age_band=AgeBand.ADULT,
        equipment=ALL_EQUIPMENT,
        exercises=CATALOG,
        youth_rules=youth_rules,
    )
    assert [error.code for error in result.errors] == [codes.YOUTH_BAND_COERCED]
    assert result.errors[0].severity == "warn"
    assert result.ok is True


def test_youth_rep_ceiling_and_rpe_cap_are_errors(youth_rules: dict[AgeBand, YouthRuleSet]) -> None:
    """Safety ceilings block. Nothing downstream of import or generation applies a remedy."""
    rules = youth_rules[AgeBand.U10]
    row = make_row(sets=1, reps=100, rpe_target=10)
    result = validate_workout(
        make_workout(rows=[row], estimated_minutes=rules.max_session_minutes),
        profile_kind="youth",
        age_band=AgeBand.U10,
        equipment=ALL_EQUIPMENT,
        exercises=CATALOG,
        youth_rules=youth_rules,
    )
    assert result.ok is False
    assert {codes.YOUTH_REP_CEILING, codes.YOUTH_RPE_EXCEEDED} <= codes_in(result)
    assert all(e.severity == "error" for e in result.errors)


@pytest.mark.parametrize(
    "doc",
    [None, {}, [], "x" * 1_000_000, {"rows": [{"a": {"b": {"c": {"d": {}}}}}]}, 42],
    ids=["none", "empty-dict", "empty-list", "one-mb-string", "nested", "int"],
)
def test_validator_never_raises(doc: Any) -> None:
    result = adult(doc)
    assert result.ok is False
    assert result.errors
    assert validate_exercise(doc).ok is False


def test_missing_bodyweight_falls_back_to_absolute_cap(youth_rules: dict[AgeBand, YouthRuleSet]) -> None:
    rules = youth_rules[AgeBand.AGE_14_17]
    assert rules.max_load_kg_per_hand is not None
    assert rules.max_load_pct_bw_per_hand is not None
    # No bodyweight: the percentage cap is unknowable, so the absolute cap stands.
    assert effective_cap_for(rules, None) == rules.max_load_kg_per_hand
    over = rules.max_load_kg_per_hand + 0.5
    row = make_row(exercise_id="db-bent-row", sets=1, reps=10, load_unit="per_hand", load_kg=over, rest_s=90)
    result = validate_workout(
        make_workout(rows=[row], estimated_minutes=20),
        profile_kind="youth",
        age_band=AgeBand.AGE_14_17,
        equipment=ALL_EQUIPMENT,
        exercises=CATALOG,
        bodyweight_kg=None,
        youth_rules=youth_rules,
    )
    assert result.ok is False
    assert codes.YOUTH_LOAD_EXCEEDED in codes_in(result)


def effective_cap_for(rules: YouthRuleSet, bodyweight_kg: float | None) -> float | None:
    from cadence.schema import LoadUnit

    return effective_cap(LoadUnit.PER_HAND, rules, bodyweight_kg)


def test_validate_exercise_accepts_a_good_document() -> None:
    assert validate_exercise(CATALOG["push-up"]).ok is True
    assert validate_exercise(CATALOG["push-up"].model_dump(mode="json")).ok is True


def test_youth_workout_without_rules_fails_closed() -> None:
    """No rule table means the band cannot be checked, so the document does not pass."""
    result = validate_workout(
        make_workout(),
        profile_kind="youth",
        age_band=AgeBand.U10,
        equipment=ALL_EQUIPMENT,
        exercises=CATALOG,
        youth_rules=None,
    )
    assert result.ok is False
    assert codes.YOUTH_RULES_UNAVAILABLE in codes_in(result)


def test_missing_exercise_catalog_fails_closed() -> None:
    result = adult(make_workout(), exercises=None)
    assert result.ok is False
    assert codes.UNKNOWN_EXERCISE in codes_in(result)


def test_requires_anchor_row_is_rejected_without_an_anchor() -> None:
    row = make_row(exercise_id="dead-hang", reps=None, seconds=30)
    assert codes.REQUIRES_ANCHOR_UNAVAILABLE in codes_in(adult(make_workout(rows=[row])))
    assert adult(make_workout(rows=[row]), has_overhead_anchor=True).ok is True
