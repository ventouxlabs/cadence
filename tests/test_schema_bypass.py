"""The gate cannot be walked around - acceptance tests 24-27.

Test 24's equipment list is fixed and must never be relaxed. Test 25 is driven off
``library/youth_rules.yaml``: PRP-01 extends the coverage by filling the YAML, not by editing
this file.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from pydantic import ValidationError as PydanticValidationError

from cadence.schema import (
    AgeBand,
    AssessmentId,
    DayType,
    Exercise,
    ExerciseTag,
    LoadType,
    LoadUnit,
    Measure,
    Pattern,
    Workout,
    WorkoutRow,
    YouthAllow,
    YouthRuleSet,
)
from cadence.validateur import (
    codes,
    effective_cap,
    usable_bodyweight,
    validate_exercise,
    validate_workout,
    youth_allows,
)
from tests.factories import ALL_EQUIPMENT, CATALOG, make_exercise, make_row, make_workout
from tests.test_schema import VALID_EXERCISE

YOUTH_BANDS = (AgeBand.U10, AgeBand.AGE_10_13, AgeBand.AGE_14_17)

LOAD_TYPE_EXERCISE = {
    LoadType.BODYWEIGHT: "push-up",
    LoadType.DUMBBELL: "db-bent-row",
    LoadType.KETTLEBELL: "kb-swing",
    LoadType.BENCH_ASSISTED: "incline-push-up",
}
TAG_EXERCISE = {ExerciseTag.MAX_EFFORT: "push-up-max-test", ExerciseTag.REQUIRES_ANCHOR: "dead-hang"}

# One case per checkable rule field. A field with no entry here has no validator code by design.
FIELD_TO_CODE: dict[str, str] = {
    "allowed_load_types": codes.YOUTH_LOAD_TYPE_NOT_ALLOWED,
    "max_load_kg_per_hand": codes.YOUTH_LOAD_EXCEEDED,
    "max_load_kg_per_implement": codes.YOUTH_LOAD_EXCEEDED,
    "max_load_pct_bw_per_hand": codes.YOUTH_LOAD_EXCEEDED,
    "max_load_pct_bw_per_implement": codes.YOUTH_LOAD_EXCEEDED,
    "rep_min_loaded": codes.YOUTH_REP_FLOOR,
    "rep_max_loaded": codes.YOUTH_REP_CEILING,
    "rep_max_bodyweight": codes.YOUTH_REP_CEILING,
    "min_rest_s_loaded": codes.YOUTH_REST_FLOOR,
    "max_exercises_per_session": codes.YOUTH_EXERCISE_COUNT,
    "max_sets_per_exercise": codes.YOUTH_SET_COUNT,
    "max_session_minutes": codes.YOUTH_SESSION_LENGTH,
    "banned_tags": codes.YOUTH_BANNED_TAG,
    "banned_goal_types": codes.YOUTH_BANNED_GOAL,
    "allow_amrap": codes.YOUTH_AMRAP_NOT_ALLOWED,
    "rpe_cap": codes.YOUTH_RPE_EXCEEDED,
}
# Rule fields the validator deliberately does not check: they steer the program engine and the UI.
UNCHECKED_FIELDS = {
    "band",
    "rep_min_bodyweight",
    "allow_max_test",
    "good_enough_done_after_n_exercises",
    "assessment_caps",
}

Case = tuple[dict[str, Any], dict[str, Any], str]
Builder = Callable[[YouthRuleSet], Case | None]


def _bw_row(rules: YouthRuleSet, **over: Any) -> dict[str, Any]:
    base = {"sets": 1, "reps": rules.rep_min_bodyweight, "rest_s": rules.min_rest_s_loaded}
    base.update(over)
    return make_row(**base)


def _loaded_row(rules: YouthRuleSet, exercise_id: str, unit: str, load_kg: float, **over: Any) -> dict[str, Any]:
    base = {
        "exercise_id": exercise_id,
        "sets": 1,
        "reps": rules.rep_min_loaded or 8,
        "load_unit": unit,
        "load_kg": load_kg,
        "rest_s": rules.min_rest_s_loaded,
    }
    base.update(over)
    return make_row(**base)


def _wo(rules: YouthRuleSet, rows: list[dict[str, Any]], **over: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {"rows": rows, "estimated_minutes": rules.max_session_minutes}
    fields.update(over)
    return make_workout(**fields)


def _case_allowed_load_types(r: YouthRuleSet) -> Case | None:
    banned = [lt for lt in LoadType if lt not in r.allowed_load_types]
    if not banned:
        return None
    row = _bw_row(r, exercise_id=LOAD_TYPE_EXERCISE[banned[0]])
    return _wo(r, [row]), {}, FIELD_TO_CODE["allowed_load_types"]


def _case_kg_per_hand(r: YouthRuleSet) -> Case | None:
    if r.max_load_kg_per_hand is None:
        return None
    row = _loaded_row(r, "db-bent-row", "per_hand", r.max_load_kg_per_hand + 0.5)
    return _wo(r, [row]), {"bodyweight_kg": None}, FIELD_TO_CODE["max_load_kg_per_hand"]


def _case_kg_per_implement(r: YouthRuleSet) -> Case | None:
    if r.max_load_kg_per_implement is None:
        return None
    row = _loaded_row(r, "goblet-squat", "per_implement", r.max_load_kg_per_implement + 0.5)
    return _wo(r, [row]), {"bodyweight_kg": None}, FIELD_TO_CODE["max_load_kg_per_implement"]


def _case_pct_per_hand(r: YouthRuleSet) -> Case | None:
    if r.max_load_pct_bw_per_hand is None:
        return None
    bodyweight = 20.0
    row = _loaded_row(r, "db-bent-row", "per_hand", r.max_load_pct_bw_per_hand * bodyweight + 0.5)
    return _wo(r, [row]), {"bodyweight_kg": bodyweight}, FIELD_TO_CODE["max_load_pct_bw_per_hand"]


def _case_pct_per_implement(r: YouthRuleSet) -> Case | None:
    if r.max_load_pct_bw_per_implement is None:
        return None
    bodyweight = 20.0
    row = _loaded_row(r, "goblet-squat", "per_implement", r.max_load_pct_bw_per_implement * bodyweight + 0.5)
    return _wo(r, [row]), {"bodyweight_kg": bodyweight}, FIELD_TO_CODE["max_load_pct_bw_per_implement"]


def _case_rep_min_loaded(r: YouthRuleSet) -> Case | None:
    if r.rep_min_loaded is None or r.rep_min_loaded <= 1:
        return None
    row = _loaded_row(r, "db-bent-row", "per_hand", 1.0, reps=r.rep_min_loaded - 1)
    return _wo(r, [row]), {}, FIELD_TO_CODE["rep_min_loaded"]


def _case_rep_max_loaded(r: YouthRuleSet) -> Case | None:
    if r.rep_max_loaded is None:
        return None
    row = _loaded_row(r, "db-bent-row", "per_hand", 1.0, reps=r.rep_max_loaded + 1)
    return _wo(r, [row]), {}, FIELD_TO_CODE["rep_max_loaded"]


def _case_rep_max_bodyweight(r: YouthRuleSet) -> Case | None:
    return _wo(r, [_bw_row(r, reps=r.rep_max_bodyweight + 1)]), {}, FIELD_TO_CODE["rep_max_bodyweight"]


def _case_min_rest(r: YouthRuleSet) -> Case | None:
    if r.min_rest_s_loaded <= 0:
        return None
    row = _loaded_row(r, "db-bent-row", "per_hand", 1.0, rest_s=r.min_rest_s_loaded - 1)
    return _wo(r, [row]), {}, FIELD_TO_CODE["min_rest_s_loaded"]


def _case_exercise_count(r: YouthRuleSet) -> Case | None:
    rows = [_bw_row(r) for _ in range(r.max_exercises_per_session + 1)]
    return _wo(r, rows), {}, FIELD_TO_CODE["max_exercises_per_session"]


def _case_set_count(r: YouthRuleSet) -> Case | None:
    return _wo(r, [_bw_row(r, sets=r.max_sets_per_exercise + 1)]), {}, FIELD_TO_CODE["max_sets_per_exercise"]


def _case_session_minutes(r: YouthRuleSet) -> Case | None:
    doc = _wo(r, [_bw_row(r)], estimated_minutes=r.max_session_minutes + 1)
    return doc, {}, FIELD_TO_CODE["max_session_minutes"]


def _case_banned_tags(r: YouthRuleSet) -> Case | None:
    usable = [tag for tag in r.banned_tags if tag in TAG_EXERCISE]
    if not usable:
        return None
    row = _bw_row(r, exercise_id=TAG_EXERCISE[usable[0]])
    return _wo(r, [row]), {"has_overhead_anchor": True}, FIELD_TO_CODE["banned_tags"]


def _case_banned_goals(r: YouthRuleSet) -> Case | None:
    if not r.banned_goal_types:
        return None
    return _wo(r, [_bw_row(r)]), {"goal_types": [r.banned_goal_types[0]]}, FIELD_TO_CODE["banned_goal_types"]


def _case_amrap(r: YouthRuleSet) -> Case | None:
    if r.allow_amrap:
        return None
    return _wo(r, [_bw_row(r, amrap=True)]), {}, FIELD_TO_CODE["allow_amrap"]


def _case_rpe(r: YouthRuleSet) -> Case | None:
    if r.rpe_cap >= 10:
        return None
    return _wo(r, [_bw_row(r, rpe_target=r.rpe_cap + 1)]), {}, FIELD_TO_CODE["rpe_cap"]


BUILDERS: dict[str, Builder] = {
    "allowed_load_types": _case_allowed_load_types,
    "max_load_kg_per_hand": _case_kg_per_hand,
    "max_load_kg_per_implement": _case_kg_per_implement,
    "max_load_pct_bw_per_hand": _case_pct_per_hand,
    "max_load_pct_bw_per_implement": _case_pct_per_implement,
    "rep_min_loaded": _case_rep_min_loaded,
    "rep_max_loaded": _case_rep_max_loaded,
    "rep_max_bodyweight": _case_rep_max_bodyweight,
    "min_rest_s_loaded": _case_min_rest,
    "max_exercises_per_session": _case_exercise_count,
    "max_sets_per_exercise": _case_set_count,
    "max_session_minutes": _case_session_minutes,
    "banned_tags": _case_banned_tags,
    "banned_goal_types": _case_banned_goals,
    "allow_amrap": _case_amrap,
    "rpe_cap": _case_rpe,
}


def _validate_youth(doc: dict[str, Any], band: AgeBand, rules_table: Any, **over: Any) -> Any:
    kwargs: dict[str, Any] = {
        "profile_kind": "youth",
        "age_band": band,
        "equipment": ALL_EQUIPMENT,
        "exercises": CATALOG,
        "youth_rules": rules_table,
    }
    kwargs.update(over)
    return validate_workout(doc, **kwargs)


@pytest.mark.parametrize("off_whitelist", ["barbell", "machine", "cable", "smith-machine", "resistance-band"])
def test_off_whitelist_equipment_is_rejected(off_whitelist: str) -> None:
    """Fixed case. Nothing outside bodyweight/dumbbells/kettlebells/bench is ever selectable."""
    exercise_result = validate_exercise({**VALID_EXERCISE, "equipment": [off_whitelist]})
    assert exercise_result.ok is False
    assert {codes.EQUIPMENT_NOT_WHITELISTED, codes.SCHEMA} & {e.code for e in exercise_result.errors}

    workout_result = validate_workout(
        make_workout(),
        profile_kind="adult",
        age_band=AgeBand.ADULT,
        equipment=[off_whitelist],  # type: ignore[list-item]
        exercises=CATALOG,
    )
    assert workout_result.ok is False
    assert codes.EQUIPMENT_NOT_WHITELISTED in {e.code for e in workout_result.errors}


def test_every_checkable_rule_field_has_a_case() -> None:
    """A new rule field must land in FIELD_TO_CODE or UNCHECKED_FIELDS, never in neither."""
    assert set(FIELD_TO_CODE) | UNCHECKED_FIELDS == set(YouthRuleSet.model_fields)
    assert set(FIELD_TO_CODE) == set(BUILDERS)


@pytest.mark.parametrize("field", sorted(FIELD_TO_CODE))
@pytest.mark.parametrize("band", YOUTH_BANDS, ids=lambda b: b.value)
def test_youth_rule_violation_is_rejected(band: AgeBand, field: str, youth_rules: dict[AgeBand, YouthRuleSet]) -> None:
    rules = youth_rules[band]
    case = BUILDERS[field](rules)
    if case is None:
        pytest.skip(f"{field} is not applicable to band {band.value}")
    doc, extra, expected = case
    result = _validate_youth(doc, band, youth_rules, **extra)
    found = {error.code for error in result.errors}
    assert expected in found, f"{band.value}/{field}: expected {expected}, got {sorted(found)}"
    if codes.SEVERITY[expected] == "error":
        assert result.ok is False


def test_youth_banned_tag_is_rejected(youth_rules: dict[AgeBand, YouthRuleSet]) -> None:
    rules = youth_rules[AgeBand.U10]
    row = make_row(exercise_id="push-up-max-test", sets=1, reps=rules.rep_min_bodyweight)
    result = _validate_youth(_wo(rules, [row]), AgeBand.U10, youth_rules)
    assert result.ok is False
    assert codes.YOUTH_BANNED_TAG in {e.code for e in result.errors}


def test_adult_profile_is_not_youth_checked(youth_rules: dict[AgeBand, YouthRuleSet]) -> None:
    """The youth gate is band-scoped. The same over-cap workout is fine for the parent."""
    u10 = youth_rules[AgeBand.U10]
    row = make_row(exercise_id="db-bent-row", sets=4, reps=10, load_unit="per_hand", load_kg=20.0, rest_s=45)
    doc = make_workout(rows=[row], estimated_minutes=45)

    youth_result = _validate_youth(doc, AgeBand.U10, youth_rules, bodyweight_kg=30.0)
    assert youth_result.ok is False
    assert codes.YOUTH_LOAD_EXCEEDED in {e.code for e in youth_result.errors}
    assert u10.max_load_kg_per_hand == 0.0

    adult_result = validate_workout(
        doc,
        profile_kind="adult",
        age_band=AgeBand.ADULT,
        equipment=ALL_EQUIPMENT,
        exercises=CATALOG,
        youth_rules=youth_rules,
        bodyweight_kg=30.0,
    )
    assert adult_result.ok is True
    assert adult_result.errors == []


# --- V14, the per-band allowlist (tests 31-35) --------------------------------------------

KB_ROW = {"exercise_id": "kb-swing", "sets": 2, "reps": 10, "load_unit": "per_implement", "rest_s": 90}


def _kb_workout(rules: YouthRuleSet, load_kg: float) -> dict[str, Any]:
    return _wo(rules, [make_row(**KB_ROW, load_kg=load_kg)])


def test_conditional_denied_when_bodyweight_unknown(youth_rules: dict[AgeBand, YouthRuleSet]) -> None:
    """The section 5.2 absolute-cap fallback must not rescue a conditional row."""
    rules = youth_rules[AgeBand.AGE_14_17]
    assert rules.max_load_kg_per_implement == 16.0
    result = _validate_youth(_kb_workout(rules, 16.0), AgeBand.AGE_14_17, youth_rules, bodyweight_kg=None)
    found = {error.code for error in result.errors}
    assert result.ok is False
    assert codes.YOUTH_EXERCISE_NOT_ALLOWED in found


def test_conditional_allowed_when_bodyweight_known(youth_rules: dict[AgeBand, YouthRuleSet]) -> None:
    """At 50 kg the section 3.6 arithmetic clears 16 kg two-handed, so V1/V2 decide and pass."""
    rules = youth_rules[AgeBand.AGE_14_17]
    result = _validate_youth(_kb_workout(rules, 16.0), AgeBand.AGE_14_17, youth_rules, bodyweight_kg=50.0)
    assert result.errors == []
    assert result.ok is True


def test_band_absent_from_allowlist_denies(youth_rules: dict[AgeBand, YouthRuleSet]) -> None:
    """An exercise that declares no bands is denied, not permitted. The allowlist fails closed."""
    rules = youth_rules[AgeBand.U10]
    row = make_row(exercise_id="bird-dog", sets=1, reps=rules.rep_min_bodyweight)
    result = _validate_youth(_wo(rules, [row]), AgeBand.U10, youth_rules)
    assert result.ok is False
    assert [error.code for error in result.errors] == [codes.YOUTH_EXERCISE_NOT_ALLOWED]


def test_allowlist_denies_even_when_load_rules_pass(youth_rules: dict[AgeBand, YouthRuleSet]) -> None:
    """The kb-swing case of principles section 3.6, which proves V14 is not redundant with V1/V2.

    Unweighed, 16 kg sits exactly on the band's absolute cap, so V1 and V2 both pass and only
    the allowlist stops it. At 40 kg bodyweight the percentage cap is 14 kg and V2 stops it too.
    """
    rules = youth_rules[AgeBand.AGE_14_17]
    unweighed = _validate_youth(_kb_workout(rules, 16.0), AgeBand.AGE_14_17, youth_rules, bodyweight_kg=None)
    unweighed_codes = {error.code for error in unweighed.errors}
    assert codes.YOUTH_LOAD_EXCEEDED not in unweighed_codes
    assert codes.YOUTH_LOAD_TYPE_NOT_ALLOWED not in unweighed_codes
    assert unweighed_codes == {codes.YOUTH_EXERCISE_NOT_ALLOWED}

    light = _validate_youth(_kb_workout(rules, 16.0), AgeBand.AGE_14_17, youth_rules, bodyweight_kg=40.0)
    assert light.ok is False
    assert codes.YOUTH_LOAD_EXCEEDED in {error.code for error in light.errors}


def test_adult_ignores_allowlist() -> None:
    """The adult band is never consulted, so a youth-denied exercise is fine for the parent."""
    row = make_row(exercise_id="hollow-hold", sets=3, reps=None, seconds=45)
    result = validate_workout(
        make_workout(rows=[row], estimated_minutes=40),
        profile_kind="adult",
        age_band=AgeBand.ADULT,
        equipment=ALL_EQUIPMENT,
        exercises=CATALOG,
    )
    assert result.ok is True
    assert result.errors == []


# --- youth_allows(), the helper PRP-01 and PRP-08 reuse ------------------------------------

ALLOW_CASES = [
    # (allow map, band, bodyweight_kg, expected)
    ({AgeBand.U10: YouthAllow.YES}, AgeBand.U10, None, True),
    ({AgeBand.U10: YouthAllow.NO}, AgeBand.U10, None, False),
    ({AgeBand.U10: YouthAllow.NO}, AgeBand.U10, 30.0, False),
    ({}, AgeBand.U10, 30.0, False),
    ({AgeBand.AGE_10_13: YouthAllow.YES}, AgeBand.U10, 30.0, False),
    ({AgeBand.AGE_14_17: YouthAllow.CONDITIONAL}, AgeBand.AGE_14_17, None, False),
    ({AgeBand.AGE_14_17: YouthAllow.CONDITIONAL}, AgeBand.AGE_14_17, 50.0, True),
    ({AgeBand.AGE_14_17: YouthAllow.CONDITIONAL}, AgeBand.AGE_14_17, 30.0, True),
    ({}, AgeBand.ADULT, None, True),
    ({AgeBand.U10: YouthAllow.NO}, AgeBand.ADULT, None, True),
]


@pytest.mark.parametrize(("allow_map", "band", "bodyweight", "expected"), ALLOW_CASES)
def test_youth_allows(
    allow_map: dict[AgeBand, YouthAllow], band: AgeBand, bodyweight: float | None, expected: bool
) -> None:
    exercise = make_exercise(id="probe-move", youth_ok_by_band=allow_map)
    assert youth_allows(exercise, band, bodyweight_kg=bodyweight) is expected
    as_doc = exercise.model_dump(mode="json")
    assert youth_allows(as_doc, band, bodyweight_kg=bodyweight) is expected


@pytest.mark.parametrize(
    "broken",
    [{}, {"youth_ok_by_band": None}, {"youth_ok_by_band": []}, {"youth_ok_by_band": {"u10": "maybe"}}],
    ids=["no-key", "null", "list", "bad-value"],
)
def test_youth_allows_denies_unreadable_documents(broken: dict[str, Any]) -> None:
    """A malformed allow map must deny. Fail-closed is the whole point of the allowlist."""
    assert youth_allows(broken, AgeBand.U10) is False


def test_youth_allows_is_the_implementation_behind_v14(youth_rules: dict[AgeBand, YouthRuleSet]) -> None:
    """The helper and the validator must not drift: PRP-01 filters on what PRP-08 is judged by."""
    rules = youth_rules[AgeBand.AGE_14_17]
    for bodyweight, permitted in ((None, False), (50.0, True)):
        result = _validate_youth(_kb_workout(rules, 16.0), AgeBand.AGE_14_17, youth_rules, bodyweight_kg=bodyweight)
        denied = codes.YOUTH_EXERCISE_NOT_ALLOWED in {error.code for error in result.errors}
        assert denied is not permitted
        assert youth_allows(CATALOG["kb-swing"], AgeBand.AGE_14_17, bodyweight_kg=bodyweight) is permitted


# --- review regressions: closed bypasses ---------------------------------------------------


@pytest.mark.parametrize(
    ("band", "exercise_id", "unit", "load_kg", "expected"),
    [
        (AgeBand.U10, "push-up", "total", 250.0, codes.YOUTH_LOAD_UNCAPPABLE),
        (AgeBand.AGE_14_17, "kb-swing", "total", 24.0, codes.YOUTH_LOAD_EXCEEDED),
        (AgeBand.AGE_10_13, "db-bent-row", "total", 60.0, codes.YOUTH_LOAD_EXCEEDED),
    ],
    ids=["u10-total", "kb-as-total", "db-as-total"],
)
def test_row_cannot_relabel_its_load_unit_to_escape_the_cap(
    band: AgeBand,
    exercise_id: str,
    unit: str,
    load_kg: float,
    expected: str,
    youth_rules: dict[AgeBand, YouthRuleSet],
) -> None:
    """The cap column follows how the exercise is held, not how the row labels itself.

    Reading the row's own load_unit let a document pick its own cap, and 'total' has no cap at
    all, so 250 kg passed for a nine-year-old.
    """
    rules = youth_rules[band]
    row = make_row(
        exercise_id=exercise_id, sets=1, reps=10, load_unit=unit, load_kg=load_kg, rest_s=rules.min_rest_s_loaded
    )
    result = _validate_youth(_wo(rules, [row]), band, youth_rules, bodyweight_kg=60.0)
    found = {error.code for error in result.errors}
    assert result.ok is False
    assert codes.LOAD_UNIT_MISMATCH in found
    assert expected in found


def test_a_bodyweight_row_carrying_load_is_rejected_before_the_cap_is_reached(
    youth_rules: dict[AgeBand, YouthRuleSet],
) -> None:
    """The other half of the relabelling bypass, closed one layer earlier by the schema."""
    rules = youth_rules[AgeBand.AGE_10_13]
    row = make_row(exercise_id="goblet-squat", sets=1, reps=10, load_unit="bodyweight", load_kg=60.0)
    result = _validate_youth(_wo(rules, [row]), AgeBand.AGE_10_13, youth_rules, bodyweight_kg=60.0)
    assert result.ok is False
    assert {error.code for error in result.errors} == {codes.SCHEMA}


def test_youth_profile_with_adult_band_is_evaluated_as_u10(
    youth_rules: dict[AgeBand, YouthRuleSet],
) -> None:
    """The adult band exists in the YAML, so a youth profile carrying it ran zero youth checks."""
    row = make_row(exercise_id="db-bent-row", sets=1, reps=10, load_unit="per_hand", load_kg=40.0, rest_s=90)
    doc = make_workout(rows=[row], estimated_minutes=20)
    result = _validate_youth(doc, AgeBand.ADULT, youth_rules)
    found = {error.code for error in result.errors}

    assert result.ok is False
    assert codes.YOUTH_BAND_COERCED in found
    assert codes.YOUTH_LOAD_EXCEEDED in found
    assert codes.YOUTH_LOAD_TYPE_NOT_ALLOWED in found
    coercion = next(e for e in result.errors if e.code == codes.YOUTH_BAND_COERCED)
    assert coercion.severity == "warn"

    # The same document for a real adult profile is fine: the coercion is scoped to youth.
    adult_result = validate_workout(
        doc,
        profile_kind="adult",
        age_band=AgeBand.ADULT,
        equipment=ALL_EQUIPMENT,
        exercises=CATALOG,
        youth_rules=youth_rules,
    )
    assert adult_result.ok is True


def test_youth_allows_does_not_short_circuit_on_adult_band_for_a_youth_profile() -> None:
    exercise = make_exercise(id="probe-move", youth_ok_by_band={})
    assert youth_allows(exercise, AgeBand.ADULT) is True
    assert youth_allows(exercise, AgeBand.ADULT, profile_kind="adult") is True
    assert youth_allows(exercise, AgeBand.ADULT, profile_kind="youth") is False


def test_rows_cannot_exempt_themselves_by_declaring_prelude(
    youth_rules: dict[AgeBand, YouthRuleSet],
) -> None:
    """V6 counted rows by a flag the document sets, so twelve 'preludes' beat a cap of five."""
    rules = youth_rules[AgeBand.U10]
    faked = [make_row(sets=1, reps=8, is_prelude=True) for _ in range(12)]
    result = _validate_youth(_wo(rules, faked), AgeBand.U10, youth_rules)
    assert result.ok is False
    assert codes.YOUTH_EXERCISE_COUNT in {error.code for error in result.errors}

    # A real prelude movement, marked on the exercise, is still excluded from the count.
    prelude_move = make_exercise(id="cat-cow", name="Cat-cow", is_prelude=True)
    catalog = {**CATALOG, prelude_move.id: prelude_move}
    genuine = [make_row(exercise_id="cat-cow", sets=1, reps=8) for _ in range(12)]
    genuine_result = _validate_youth(_wo(rules, genuine), AgeBand.U10, youth_rules, exercises=catalog)
    assert codes.YOUTH_EXERCISE_COUNT not in {error.code for error in genuine_result.errors}


def test_session_length_is_computed_from_rows_not_declared(
    youth_rules: dict[AgeBand, YouthRuleSet],
) -> None:
    """V9 read a number the document supplied, so a 33-minute session could declare one minute."""
    rows = [make_row(sets=2, reps=8, rest_s=300) for _ in range(5)]
    doc = make_workout(rows=rows, estimated_minutes=1)
    result = _validate_youth(doc, AgeBand.U10, youth_rules)
    assert result.ok is False
    assert codes.YOUTH_SESSION_LENGTH in {error.code for error in result.errors}


def test_assessment_day_exempts_max_effort_and_the_session_caps(
    youth_rules: dict[AgeBand, YouthRuleSet],
) -> None:
    """Principles 3.4 and 10.10: the six-test battery is longer and wider than a session."""
    rules = youth_rules[AgeBand.U10]
    battery = [make_row(exercise_id="push-up-max-test", sets=1, reps=8) for _ in range(8)]
    doc = make_workout(rows=battery, estimated_minutes=rules.max_session_minutes + 20, day_type="assessment")
    result = _validate_youth(doc, AgeBand.U10, youth_rules)
    assert result.ok is True
    assert result.errors == []

    # Same rows on a training day: the ban and both caps apply again.
    training = make_workout(rows=battery, estimated_minutes=rules.max_session_minutes + 20)
    training_result = _validate_youth(training, AgeBand.U10, youth_rules)
    found = {error.code for error in training_result.errors}
    assert {codes.YOUTH_BANNED_TAG, codes.YOUTH_EXERCISE_COUNT, codes.YOUTH_SESSION_LENGTH} <= found


def test_assessment_day_exempts_only_max_effort_on_assessment_only_exercises(
    youth_rules: dict[AgeBand, YouthRuleSet],
) -> None:
    """The exemption is narrow: other banned tags, and other exercises, are untouched."""
    one_rm = make_exercise(
        id="one-rm-attempt",
        name="One-rep-max attempt",
        tags=[ExerciseTag.ONE_RM, ExerciseTag.ASSESSMENT_ONLY],
        cue="Do not do this.",
    )
    untagged = make_exercise(
        id="burnout-set", name="Burnout set", tags=[ExerciseTag.MAX_EFFORT], cue="Go until it burns."
    )
    catalog = {**CATALOG, one_rm.id: one_rm, untagged.id: untagged}
    for exercise_id in (one_rm.id, untagged.id):
        doc = make_workout(rows=[make_row(exercise_id=exercise_id, sets=1, reps=8)], day_type="assessment")
        result = _validate_youth(doc, AgeBand.U10, youth_rules, exercises=catalog)
        assert codes.YOUTH_BANNED_TAG in {e.code for e in result.errors}, exercise_id


@pytest.mark.parametrize(
    ("target", "profile_kind", "flagged"),
    [
        ("both", "youth", False),
        ("both", "adult", False),
        ("youth", "youth", False),
        ("adult", "adult", False),
        ("adult", "youth", True),
        ("youth", "adult", True),
    ],
)
def test_profile_kind_mismatch_flagged(
    target: str, profile_kind: str, flagged: bool, youth_rules: dict[AgeBand, YouthRuleSet]
) -> None:
    doc = make_workout(rows=[make_row(sets=1, reps=8)], target_profile_kind=target, estimated_minutes=20)
    result = validate_workout(
        doc,
        profile_kind=profile_kind,  # type: ignore[arg-type]
        age_band=AgeBand.U10 if profile_kind == "youth" else AgeBand.ADULT,
        equipment=ALL_EQUIPMENT,
        exercises=CATALOG,
        youth_rules=youth_rules,
    )
    assert (codes.PROFILE_KIND_MISMATCH in {e.code for e in result.errors}) is flagged


# --- batch two: bodyweight validity, strictness, deep immutability -------------------------


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), 0, 0.0, -5.0, True, "70"], ids=str)
def test_unusable_bodyweight_is_rejected_and_treated_as_unknown(
    bad: object, youth_rules: dict[AgeBand, YouthRuleSet]
) -> None:
    """nan counted as *known*, so every ``nan > cap`` was False and the percentage cap vanished."""
    rules = youth_rules[AgeBand.AGE_14_17]
    row = make_row(**KB_ROW, load_kg=16.0)
    result = _validate_youth(_wo(rules, [row]), AgeBand.AGE_14_17, youth_rules, bodyweight_kg=bad)
    found = {error.code for error in result.errors}
    assert result.ok is False
    assert codes.BODYWEIGHT_INVALID in found
    # Unknown, so the conditional allowlist denies rather than falling back to the absolute cap.
    assert codes.YOUTH_EXERCISE_NOT_ALLOWED in found
    assert youth_allows(CATALOG["kb-swing"], AgeBand.AGE_14_17, bodyweight_kg=bad) is False


def test_nan_bodyweight_does_not_evaporate_the_percentage_cap(
    youth_rules: dict[AgeBand, YouthRuleSet],
) -> None:
    """At 40 kg the percentage cap is 14 kg; nan must not be a way to reach the 16 kg one."""
    rules = youth_rules[AgeBand.AGE_14_17]
    assert effective_cap(LoadUnit.PER_IMPLEMENT, rules, float("nan")) == rules.max_load_kg_per_implement
    assert effective_cap(LoadUnit.PER_IMPLEMENT, rules, 40.0) == 14.0
    assert usable_bodyweight(float("nan")) is None
    assert usable_bodyweight(40.0) == 40.0


@pytest.mark.parametrize(
    "patch",
    [
        {"sets": True},
        {"sets": "2"},
        {"reps": "8"},
        {"reps": True},
        {"rest_s": False},
        {"load_kg": "12", "load_unit": "per_hand"},
        {"load_kg": True, "load_unit": "per_hand"},
        {"rpe_target": "7"},
        {"seconds": "30", "reps": None},
    ],
    ids=lambda p: "-".join(f"{k}={v!r}" for k, v in p.items()),
)
def test_numeric_fields_reject_strings_and_booleans(patch: dict[str, Any]) -> None:
    """YAML ``true`` read as 1 and ``"12"`` read as twelve. A cap must not be compared to those."""
    with pytest.raises(PydanticValidationError):
        WorkoutRow.model_validate(make_row(**patch))


def test_progression_numbers_are_strict() -> None:
    from cadence.schema import Progression

    with pytest.raises(PydanticValidationError):
        Progression.model_validate({"id": "p", "type": "double_progression", "rep_min": "8"})
    with pytest.raises(PydanticValidationError):
        Progression.model_validate({"id": "p", "type": "double_progression", "deload_pct": True})
    # An integer for a float field stays legal: YAML writes 12, not 12.0.
    assert Progression.model_validate({"id": "p", "type": "linear_load", "load_step_kg": 2}).load_step_kg == 2.0


def test_dsl_containers_cannot_be_mutated_through(youth_rules: dict[AgeBand, YouthRuleSet]) -> None:
    """``frozen=True`` blocks attribute assignment but not ``exercise.tags.append(...)``."""
    exercise = CATALOG["push-up"]
    assert isinstance(exercise.equipment, tuple)
    assert isinstance(exercise.tags, tuple)
    with pytest.raises(TypeError):
        exercise.youth_ok_by_band[AgeBand.U10] = YouthAllow.YES  # type: ignore[index]

    workout = Workout.model_validate(make_workout())
    assert isinstance(workout.rows, tuple)

    rules = youth_rules[AgeBand.U10]
    assert isinstance(rules.allowed_load_types, tuple)
    assert isinstance(rules.banned_tags, tuple)
    with pytest.raises(TypeError):
        rules.assessment_caps[AssessmentId.PLANK_S] = 9999  # type: ignore[index]


def test_a_model_instance_is_revalidated_not_trusted(youth_rules: dict[AgeBand, YouthRuleSet]) -> None:
    """An ``Exercise`` or ``Workout`` instance is not proof that validation ever ran."""
    smuggled = WorkoutRow.model_construct(
        exercise_id="push-up", sets=99, reps=500, load_unit=LoadUnit.BODYWEIGHT, rest_s=0
    )
    workout = Workout.model_construct(
        id="smuggled",
        name="Smuggled",
        day_type=DayType.UPPER_A,
        target_profile_kind="both",
        rows=(smuggled,),
        estimated_minutes=1,
    )
    result = _validate_youth(workout, AgeBand.U10, youth_rules)
    assert result.ok is False
    assert codes.SCHEMA in {error.code for error in result.errors}

    bad_exercise = Exercise.model_construct(id="Not A Slug", name="", cue="x\ny")
    assert validate_exercise(bad_exercise).ok is False


def test_a_long_hold_is_timed_by_the_row_not_the_library_estimate(
    youth_rules: dict[AgeBand, YouthRuleSet],
) -> None:
    """One row of ``seconds: 3600`` is an hour, whatever the exercise's estimate says."""
    quick = make_exercise(
        id="wall-sit", name="Wall sit", measure=Measure.SECONDS, est_seconds_per_set=45, cue="Sit and hold."
    )
    catalog = {**CATALOG, quick.id: quick}
    row = make_row(exercise_id="wall-sit", sets=1, reps=None, seconds=3600)
    doc = make_workout(rows=[row], estimated_minutes=1)
    result = _validate_youth(doc, AgeBand.U10, youth_rules, exercises=catalog)
    assert result.ok is False
    assert codes.YOUTH_SESSION_LENGTH in {error.code for error in result.errors}


# --- assessment day exempts the tests, not the session ------------------------------------


def _battery_catalog() -> dict[str, Exercise]:
    """The principles 10.10 battery, in a u10-legal form. Rows 1-2 are not assessment_only."""
    measured = (ExerciseTag.ASSESSMENT_ONLY, ExerciseTag.MAX_EFFORT)
    return {
        **CATALOG,
        "wall-angel": make_exercise(id="wall-angel", name="Wall angel", cue="Slide the arms up the wall."),
        "bodyweight-squat": make_exercise(
            id="bodyweight-squat", name="Bodyweight squat", pattern=Pattern.SQUAT, cue="Sit down tall."
        ),
        "push-up-max": make_exercise(id="push-up-max", name="Push-up max", tags=measured, cue="Clean reps only."),
        "dead-hang-max": make_exercise(
            id="dead-hang-max", name="Dead hang", measure=Measure.SECONDS, tags=measured, cue="Hang and breathe."
        ),
        "plank-max": make_exercise(
            id="plank-max", name="Plank hold", measure=Measure.SECONDS, tags=measured, cue="Ribs down."
        ),
        "bear-crawl-max": make_exercise(
            id="bear-crawl-max", name="Bear crawl", measure=Measure.SECONDS, tags=measured, cue="Knees low."
        ),
    }


def test_assessment_day_does_not_exempt_ordinary_rows(youth_rules: dict[AgeBand, YouthRuleSet]) -> None:
    """day_type is a word the document supplies, so it must not switch the caps off wholesale."""
    rows = [make_row(sets=1, reps=8) for _ in range(60)]
    doc = make_workout(rows=rows, estimated_minutes=120, day_type="assessment")
    result = _validate_youth(doc, AgeBand.U10, youth_rules)
    found = {error.code for error in result.errors}
    assert result.ok is False
    assert codes.YOUTH_EXERCISE_COUNT in found
    assert codes.YOUTH_SESSION_LENGTH in found


def test_the_principles_battery_still_passes_at_u10(youth_rules: dict[AgeBand, YouthRuleSet]) -> None:
    """Principles 10.10 replaces V6 and V9 for the measured tests; the battery must still pass."""
    catalog = _battery_catalog()
    rows = [
        make_row(exercise_id="wall-angel", sets=1, reps=8, rest_s=60),
        make_row(exercise_id="bodyweight-squat", sets=1, reps=5, rest_s=90),
        make_row(exercise_id="push-up-max", sets=1, reps=8, rest_s=90),
        make_row(exercise_id="dead-hang-max", sets=1, reps=None, seconds=20, rest_s=90),
        make_row(exercise_id="plank-max", sets=1, reps=None, seconds=30, rest_s=90),
        make_row(exercise_id="bear-crawl-max", sets=1, reps=None, seconds=20, rest_s=0),
    ]
    doc = make_workout(rows=rows, estimated_minutes=13, day_type="assessment")
    result = _validate_youth(doc, AgeBand.U10, youth_rules, exercises=catalog, has_overhead_anchor=True)
    assert result.errors == []
    assert result.ok is True

    # The same six rows on a training day: the max_effort ban and the caps come back.
    training = make_workout(rows=rows, estimated_minutes=13)
    training_result = _validate_youth(training, AgeBand.U10, youth_rules, exercises=catalog, has_overhead_anchor=True)
    assert training_result.ok is False
    assert codes.YOUTH_BANNED_TAG in {error.code for error in training_result.errors}
