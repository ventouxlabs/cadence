"""Attacks on the two hard rules: the equipment whitelist and the youth caps.

``tests/test_schema_bypass.py`` holds the numbered acceptance tests. This file holds the cases an
implementer would not think to write: near-miss equipment spellings, unicode lookalikes, boolean
and string coercion into numeric fields, unit relabelling, and the boundaries of every cap rather
than a single value comfortably past it.

Nothing here may be relaxed to make a change pass. A failure means a document that must not
render now renders.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

import cadence.schema as schema_module
from cadence.schema import (
    AgeBand,
    Equipment,
    Exercise,
    ExerciseTag,
    LoadType,
    LoadUnit,
    Workout,
    WorkoutRow,
    YouthAllow,
    YouthRuleSet,
)
from cadence.validateur import codes, validate_exercise, validate_workout
from cadence.validateur.result import ValidationError, ValidationResult
from tests.factories import ALL_EQUIPMENT, CATALOG, make_exercise, make_row, make_workout
from tests.test_schema import VALID_EXERCISE

YOUTH_BANDS = (AgeBand.U10, AgeBand.AGE_10_13, AgeBand.AGE_14_17)


def _codes(result: Any) -> set[str]:
    return {error.code for error in result.errors}


def _youth(doc: Any, band: AgeBand, rules: Any, **over: Any) -> Any:
    kwargs: dict[str, Any] = {
        "profile_kind": "youth",
        "age_band": band,
        "equipment": ALL_EQUIPMENT,
        "exercises": CATALOG,
        "youth_rules": rules,
    }
    kwargs.update(over)
    return validate_workout(doc, **kwargs)


def _adult(doc: Any, **over: Any) -> Any:
    kwargs: dict[str, Any] = {
        "profile_kind": "adult",
        "age_band": AgeBand.ADULT,
        "equipment": ALL_EQUIPMENT,
        "exercises": CATALOG,
    }
    kwargs.update(over)
    return validate_workout(doc, **kwargs)


# --- The equipment whitelist ---------------------------------------------------------------

# Every one of these is a spelling somebody could plausibly paste, type, or have an LLM emit.
NEAR_MISS_EQUIPMENT = [
    "Dumbbells",  # title case
    "DUMBBELLS",  # shouting
    "dumbBells",  # camel
    "dumbbell",  # singular: also a LoadType value, which is the tempting confusion
    "dumbbell ",  # trailing space
    " dumbbells",  # leading space
    "dumbbells ",  # trailing space on the right word
    "\tdumbbells",  # leading tab
    "dumb-bells",  # hyphenated
    "dumb bells",  # spaced
    "kettlebell",  # singular again
    "Kettlebells",
    "benches",  # plural of the right word
    "Bench",
    "body weight",  # spaced
    "bodyweights",
    "barbell",
    "band",
    "bands",
    "resistance-band",
    "resistance band",
    "machine",
    "cable",
    "smith-machine",
    "trap-bar",
    "sandbag",
    "dumbbеlls",  # Cyrillic small ie inside "dumbbells"
    "bаrbell",  # Cyrillic small a inside "barbell"
    "bodyweight​",  # zero-width space
    "﻿bodyweight",  # byte-order mark
    "ｄumbbells",  # fullwidth latin d
    "bodyweight\n",
    "bodyweight ",  # non-breaking space
    "",
]


@pytest.mark.parametrize("spelling", NEAR_MISS_EQUIPMENT)
def test_near_miss_equipment_spellings_are_rejected_on_an_exercise(spelling: str) -> None:
    """The enum is the whitelist. Nothing is normalised, trimmed, case-folded or guessed."""
    result = validate_exercise({**VALID_EXERCISE, "equipment": [spelling]})
    assert result.ok is False, f"{spelling!r} was accepted as equipment"
    assert {codes.EQUIPMENT_NOT_WHITELISTED, codes.SCHEMA} & _codes(result)


@pytest.mark.parametrize("spelling", NEAR_MISS_EQUIPMENT)
def test_near_miss_equipment_spellings_are_rejected_on_a_profile(spelling: str) -> None:
    """The caller's own equipment list is checked too: a bad profile cannot widen a workout."""
    result = _adult(make_workout(), equipment=[spelling])
    assert result.ok is False, f"{spelling!r} was accepted as profile equipment"
    assert codes.EQUIPMENT_NOT_WHITELISTED in _codes(result)


@pytest.mark.parametrize("spelling", ["Dumbbells", "barbell", "dumbbell ", "bаrbell", ""])
def test_near_miss_equipment_is_rejected_by_the_model_itself(spelling: str) -> None:
    """Not only by the validator: parsing must fail, so no code path can build the object."""
    with pytest.raises(PydanticValidationError):
        Exercise.model_validate({**VALID_EXERCISE, "equipment": [spelling]})


def test_equipment_list_may_not_be_empty() -> None:
    with pytest.raises(PydanticValidationError):
        Exercise.model_validate({**VALID_EXERCISE, "equipment": []})


def test_equipment_whitelist_is_exactly_the_four_of_principles_8_1() -> None:
    """A fifth member added anywhere makes it selectable everywhere. Pin the set."""
    from cadence.schema import EQUIPMENT_WHITELIST

    assert {item.value for item in EQUIPMENT_WHITELIST} == {"bodyweight", "dumbbells", "kettlebells", "bench"}
    assert frozenset(Equipment) == EQUIPMENT_WHITELIST


# --- extra="forbid" on every document model ------------------------------------------------

EXPECTED_SCHEMA_MODELS = {
    "AssessmentSpec",
    "CadenceModel",
    "Exercise",
    "Progression",
    "Workout",
    "WorkoutRow",
    "YouthRuleSet",
}


def test_the_exported_model_set_is_exactly_what_we_expect() -> None:
    """An exact set, not a floor: dropping a model from ``__all__`` must not pass silently."""
    exported = {
        name
        for name in schema_module.__all__
        if isinstance(getattr(schema_module, name), type)
        and issubclass(getattr(schema_module, name), BaseModel)
        and getattr(schema_module, name).__name__ != "YouthRules"
    }
    assert exported == EXPECTED_SCHEMA_MODELS


@pytest.mark.parametrize("name", sorted(EXPECTED_SCHEMA_MODELS | {"YouthRules"}))
def test_every_schema_model_forbids_extra_and_is_frozen(name: str) -> None:
    model = getattr(schema_module, name)
    assert model.model_config.get("frozen") is True, f"{name} is mutable"
    if name == "YouthRules":
        # A RootModel has no fields of its own to forbid; its values are YouthRuleSet.
        return
    assert model.model_config.get("extra") == "forbid", f"{name} accepts extra keys"


@pytest.mark.parametrize("model", [ValidationError, ValidationResult])
def test_the_validator_result_models_forbid_extra_too(model: type[BaseModel]) -> None:
    """The report an API hands back must not carry fields nobody declared."""
    assert model.model_config.get("extra") == "forbid"
    assert model.model_config.get("frozen") is True


@pytest.mark.parametrize(
    ("path", "doc"),
    [
        ("workout root", lambda: make_workout(sneaky="barbell")),
        ("row", lambda: make_workout(rows=[make_row(sneaky="barbell")])),
        ("row, plausible name", lambda: make_workout(rows=[make_row(load_lb=400)])),
        ("workout, plausible name", lambda: make_workout(equipment=["barbell"])),
    ],
)
def test_a_stray_key_fails_closed_at_any_depth(path: str, doc: Any) -> None:
    """``extra='forbid'`` is what makes an imported YAML with a stray key fail rather than drop it.

    A stray key named ``equipment`` is reported as ``equipment_not_whitelisted`` rather than
    ``schema``, because the flattener reads the path. Either way it is an error and the document
    does not pass, which is the only thing that matters here.
    """
    result = _adult(doc())
    assert result.ok is False, path
    assert {codes.SCHEMA, codes.EQUIPMENT_NOT_WHITELISTED} & _codes(result)


# --- numeric coercion ----------------------------------------------------------------------


def test_booleans_are_rejected_on_numeric_fields() -> None:
    """YAML ``true`` used to arrive as ``1`` on an int field. Strict numerics reject it (D-037).

    A bool could only ever become 0 or 1, so it could not inflate a value past a cap - but it
    could drop one below a floor, and a document nobody wrote a number in should not validate.
    """
    with pytest.raises(PydanticValidationError):
        WorkoutRow.model_validate(
            {"exercise_id": "push-up", "sets": True, "reps": 8, "load_unit": "bodyweight", "rest_s": 60}
        )
    with pytest.raises(PydanticValidationError):
        WorkoutRow.model_validate(
            {"exercise_id": "push-up", "sets": 1, "reps": "8", "load_unit": "bodyweight", "rest_s": 60}
        )


def test_a_false_rest_is_rejected_before_the_rest_floor(youth_rules: dict[AgeBand, YouthRuleSet]) -> None:
    """``rest_s: false`` used to coerce to 0 and trip the floor. Now it never parses."""
    row = make_row(exercise_id="db-bent-row", sets=1, reps=10, load_unit="per_hand", load_kg=5.0, rest_s=False)
    result = _youth(make_workout(rows=[row], estimated_minutes=30), AgeBand.AGE_14_17, youth_rules)
    assert result.ok is False
    assert codes.SCHEMA in _codes(result)


def test_a_true_load_is_rejected_before_the_cap(youth_rules: dict[AgeBand, YouthRuleSet]) -> None:
    """``load_kg: true`` used to mean 1.0 kg, over the u10 cap of zero. Now it never parses."""
    row = make_row(exercise_id="db-bent-row", sets=1, reps=10, load_unit="per_hand", load_kg=True, rest_s=60)
    result = _youth(make_workout(rows=[row], estimated_minutes=20), AgeBand.U10, youth_rules)
    assert result.ok is False
    assert codes.SCHEMA in _codes(result)


OUT_OF_RANGE = [
    ("sets", 0),
    ("sets", -1),
    ("sets", 11),
    ("sets", 10**9),
    ("reps", 0),
    ("reps", -5),
    ("reps", 101),
    ("rest_s", -1),
    ("rest_s", 3601),
    ("load_kg", -0.1),
    ("load_kg", 501.0),
    ("load_kg", float("inf")),
    ("load_kg", float("-inf")),
    ("load_kg", float("nan")),
    ("load_kg", 1e308),
    ("rpe_target", 0),
    ("rpe_target", 11),
    ("rpe_target", -3),
    ("seconds", 0),
    ("seconds", 3601),
]


@pytest.mark.parametrize(("field", "value"), OUT_OF_RANGE, ids=lambda v: str(v))
def test_out_of_range_row_numbers_are_rejected(field: str, value: Any) -> None:
    """Zero, negative, infinite, not-a-number and absurd values all fail at parse time.

    ``nan`` matters more than it looks: every cap check is a ``>`` comparison, and ``nan > cap``
    is ``False``, so an accepted nan would sail through every youth load rule.
    """
    payload: dict[str, Any] = {
        "exercise_id": "push-up",
        "sets": 2,
        "load_unit": "per_hand" if field == "load_kg" else "bodyweight",
        "rest_s": 60,
    }
    payload["reps" if field != "seconds" else "seconds"] = 8 if field != "seconds" else 30
    payload[field] = value
    with pytest.raises(PydanticValidationError):
        WorkoutRow.model_validate(payload)


@pytest.mark.parametrize("value", [0, -1, 121, 10**9])
def test_out_of_range_session_length_is_rejected(value: int) -> None:
    with pytest.raises(PydanticValidationError):
        Workout.model_validate(make_workout(estimated_minutes=value))


def test_a_workout_may_not_be_empty_or_enormous() -> None:
    with pytest.raises(PydanticValidationError):
        Workout.model_validate(make_workout(rows=[]))
    with pytest.raises(PydanticValidationError):
        Workout.model_validate(make_workout(rows=[make_row() for _ in range(61)]))


# --- load_unit: the row may not relabel how the load is counted -----------------------------

RELABELLINGS = [
    ("db-bent-row", "per_hand", "total"),
    ("db-bent-row", "per_hand", "per_implement"),
    ("db-bent-row", "per_hand", "bodyweight"),
    ("goblet-squat", "per_implement", "total"),
    ("goblet-squat", "per_implement", "per_hand"),
    ("kb-swing", "per_implement", "total"),
]


@pytest.mark.parametrize(("exercise_id", "declared", "claimed"), RELABELLINGS)
def test_a_row_may_not_relabel_the_load_unit(
    exercise_id: str, declared: str, claimed: str, youth_rules: dict[AgeBand, YouthRuleSet]
) -> None:
    """Principles section 1.4: the unit belongs to the exercise, and the cap column follows it.

    ``total`` and ``bodyweight`` are not capped directly, so a per-hand dumbbell row relabelled
    ``total`` would otherwise reach a fourteen-year-old with any weight at all.
    """
    assert CATALOG[exercise_id].load_unit.value == declared
    row = make_row(exercise_id=exercise_id, sets=1, reps=10, load_unit=claimed, load_kg=100.0, rest_s=90)
    doc = make_workout(rows=[row], estimated_minutes=30)

    youth = _youth(doc, AgeBand.AGE_14_17, youth_rules, bodyweight_kg=60.0)
    assert youth.ok is False
    assert {codes.LOAD_UNIT_MISMATCH, codes.SCHEMA} & _codes(youth)
    # Adults have no load cap, so this is the only thing that catches a relabelled row for them.
    assert _adult(doc).ok is False


def test_the_matching_load_unit_still_passes(youth_rules: dict[AgeBand, YouthRuleSet]) -> None:
    """The guard must not reject a correctly-labelled row: that would be worse than the hole."""
    row = make_row(exercise_id="db-bent-row", sets=1, reps=10, load_unit="per_hand", load_kg=5.0, rest_s=90)
    result = _youth(make_workout(rows=[row], estimated_minutes=30), AgeBand.AGE_14_17, youth_rules, bodyweight_kg=60.0)
    assert result.ok is True
    assert result.errors == []


@pytest.mark.parametrize("load_kg", [0.1, 20.0, 500.0])
def test_a_bodyweight_unit_row_may_not_carry_a_load(load_kg: float) -> None:
    """Section 1.4 defines ``bodyweight`` as no external load.

    A row claiming both skips the load cap, the rep floor and the rest floor at once, because all
    three read the unit rather than the number.
    """
    with pytest.raises(PydanticValidationError, match="no external load"):
        WorkoutRow.model_validate(
            {
                "exercise_id": "push-up",
                "sets": 2,
                "reps": 8,
                "load_unit": "bodyweight",
                "load_kg": load_kg,
                "rest_s": 60,
            }
        )


def test_a_bodyweight_unit_row_may_still_state_zero_load() -> None:
    row = WorkoutRow.model_validate(
        {"exercise_id": "push-up", "sets": 2, "reps": 8, "load_unit": "bodyweight", "load_kg": 0.0, "rest_s": 60}
    )
    assert row.load_kg == 0.0


# --- per_hand vs per_implement caps, at the boundary, for every band ------------------------

CAP_CASES = [
    ("db-bent-row", LoadUnit.PER_HAND, "max_load_kg_per_hand"),
    ("goblet-squat", LoadUnit.PER_IMPLEMENT, "max_load_kg_per_implement"),
]


@pytest.mark.parametrize(("exercise_id", "unit", "field"), CAP_CASES, ids=[c[2] for c in CAP_CASES])
@pytest.mark.parametrize("band", YOUTH_BANDS, ids=lambda b: b.value)
def test_the_absolute_cap_is_evaluated_per_unit_and_at_the_boundary(
    band: AgeBand, exercise_id: str, unit: LoadUnit, field: str, youth_rules: dict[AgeBand, YouthRuleSet]
) -> None:
    """Exactly at the cap is legal; a hair over is not. Each unit reads its own column."""
    rules = youth_rules[band]
    cap = getattr(rules, field)
    assert cap is not None

    def run(load_kg: float) -> set[str]:
        row = make_row(
            exercise_id=exercise_id,
            sets=1,
            reps=rules.rep_min_loaded or 8,
            load_unit=unit.value,
            load_kg=load_kg,
            rest_s=rules.min_rest_s_loaded,
        )
        doc = make_workout(rows=[row], estimated_minutes=rules.max_session_minutes)
        return _codes(_youth(doc, band, youth_rules, bodyweight_kg=None))

    assert codes.YOUTH_LOAD_EXCEEDED not in run(cap), f"{band.value}: {cap} kg is the cap and must be legal"
    assert codes.YOUTH_LOAD_EXCEEDED in run(cap + 0.5), f"{band.value}: {cap + 0.5} kg is over the cap"


@pytest.mark.parametrize("band", [AgeBand.AGE_10_13, AgeBand.AGE_14_17], ids=lambda b: b.value)
def test_the_lower_of_the_two_caps_wins(band: AgeBand, youth_rules: dict[AgeBand, YouthRuleSet]) -> None:
    """Principles section 3.3. A light child is held to the percentage, not to the absolute kg."""
    rules = youth_rules[band]
    assert rules.max_load_kg_per_hand is not None and rules.max_load_pct_bw_per_hand is not None
    # A bodyweight low enough that the percentage cap bites first.
    bodyweight = rules.max_load_kg_per_hand / rules.max_load_pct_bw_per_hand / 2
    pct_cap = rules.max_load_pct_bw_per_hand * bodyweight
    assert pct_cap < rules.max_load_kg_per_hand

    def run(load_kg: float) -> set[str]:
        row = make_row(
            exercise_id="db-bent-row",
            sets=1,
            reps=rules.rep_min_loaded or 8,
            load_unit="per_hand",
            load_kg=load_kg,
            rest_s=rules.min_rest_s_loaded,
        )
        doc = make_workout(rows=[row], estimated_minutes=rules.max_session_minutes)
        return _codes(_youth(doc, band, youth_rules, bodyweight_kg=bodyweight))

    assert codes.YOUTH_LOAD_EXCEEDED not in run(pct_cap)
    assert codes.YOUTH_LOAD_EXCEEDED in run(pct_cap + 0.1)
    # Under the absolute cap but over the percentage cap: the absolute cap must not rescue it.
    assert codes.YOUTH_LOAD_EXCEEDED in run(rules.max_load_kg_per_hand)


def test_the_test_catalog_declares_no_uncappable_unit() -> None:
    """A fixture invariant, not a library one.

    ``total`` has no cap column (section 1.4), so an exercise declaring it would make every case
    above vacuous. The shipped library is guarded by ``youth_load_uncappable`` instead; this only
    keeps ``tests/factories`` honest.
    """
    for exercise in CATALOG.values():
        assert exercise.load_unit is not LoadUnit.TOTAL, f"{exercise.id} would make these cases vacuous"


# --- the conditional allow, at the bodyweight boundary --------------------------------------

# Principles section 3.6: 16 kg two-handed is legal for 14-17 only when 0.35 x bodyweight >= 16,
# i.e. bodyweight >= 45.714... kg. The document rounds that to 45.7.
KB_THRESHOLD_KG = 16.0 / 0.35


def _kb_doc(rules: YouthRuleSet, load_kg: float = 16.0) -> dict[str, Any]:
    row = make_row(exercise_id="kb-swing", sets=2, reps=10, load_unit="per_implement", load_kg=load_kg, rest_s=90)
    return make_workout(rows=[row], estimated_minutes=rules.max_session_minutes)


@pytest.mark.parametrize(
    ("bodyweight", "expected_pass"),
    [
        (None, False),  # unknown bodyweight: V14 denies before the section 5.2 fallback runs
        (20.0, False),
        (40.0, False),
        (45.0, False),  # 0.35 x 45 = 15.75 kg, so the 16 kg bell is over the cap
        (45.7, False),  # the document's rounded threshold is fractionally short of 16 kg
        (46.0, True),  # 0.35 x 46 = 16.1 kg, so the bell fits
        (60.0, True),
    ],
    ids=["unknown", "20kg", "40kg", "45kg", "45.7kg", "46kg", "60kg"],
)
def test_the_kettlebell_bodyweight_threshold(
    bodyweight: float | None, expected_pass: bool, youth_rules: dict[AgeBand, YouthRuleSet]
) -> None:
    """V14 only decides *whether* the load rules run; V2's percentage cap enforces the 45.7 kg."""
    rules = youth_rules[AgeBand.AGE_14_17]
    result = _youth(_kb_doc(rules), AgeBand.AGE_14_17, youth_rules, bodyweight_kg=bodyweight)
    assert result.ok is expected_pass, f"{bodyweight} kg: {sorted(_codes(result))}"
    if not expected_pass:
        expected_code = codes.YOUTH_EXERCISE_NOT_ALLOWED if bodyweight is None else codes.YOUTH_LOAD_EXCEEDED
        assert expected_code in _codes(result)


def test_the_documented_threshold_matches_the_arithmetic() -> None:
    assert 45.7 < KB_THRESHOLD_KG < 45.72


@pytest.mark.parametrize("band", [AgeBand.U10, AgeBand.AGE_10_13], ids=lambda b: b.value)
def test_the_kettlebell_is_denied_outright_to_the_younger_bands(
    band: AgeBand, youth_rules: dict[AgeBand, YouthRuleSet]
) -> None:
    """No bodyweight makes a kettlebell legal below 14. Section 3.6 leaves no conditional there."""
    rules = youth_rules[band]
    for bodyweight in (None, 30.0, 80.0):
        result = _youth(_kb_doc(rules), band, youth_rules, bodyweight_kg=bodyweight)
        assert result.ok is False
        found = _codes(result)
        assert codes.YOUTH_EXERCISE_NOT_ALLOWED in found
        assert codes.YOUTH_LOAD_TYPE_NOT_ALLOWED in found


def test_the_24kg_bell_is_never_legal_for_any_youth_band(youth_rules: dict[AgeBand, YouthRuleSet]) -> None:
    """The household owns 16 kg and 24 kg. Section 3.6: the 24 kg bell is never legal for youth."""
    for band in YOUTH_BANDS:
        rules = youth_rules[band]
        result = _youth(_kb_doc(rules, load_kg=24.0), band, youth_rules, bodyweight_kg=90.0)
        assert result.ok is False
        assert codes.YOUTH_LOAD_EXCEEDED in _codes(result)


# --- the allowlist fails closed -------------------------------------------------------------

ABSENT_BAND_MAPS = [
    ({}, "no bands at all"),
    ({AgeBand.AGE_10_13: YouthAllow.YES}, "a different band"),
    ({AgeBand.ADULT: YouthAllow.YES}, "the adult band, which is never consulted"),
    ({AgeBand.AGE_14_17: YouthAllow.CONDITIONAL}, "an older band, conditionally"),
]


@pytest.mark.parametrize(("allow_map", "why"), ABSENT_BAND_MAPS, ids=[c[1] for c in ABSENT_BAND_MAPS])
def test_an_absent_band_key_denies(
    allow_map: dict[AgeBand, YouthAllow], why: str, youth_rules: dict[AgeBand, YouthRuleSet]
) -> None:
    """A seed row that forgets a column must deny, never permit. The allowlist fails closed."""
    exercise = make_exercise(id="probe-move", youth_ok_by_band=allow_map)
    catalog = {**CATALOG, exercise.id: exercise}
    rules = youth_rules[AgeBand.U10]
    doc = make_workout(
        rows=[make_row(exercise_id="probe-move", sets=1, reps=rules.rep_min_bodyweight)],
        estimated_minutes=rules.max_session_minutes,
    )
    result = _youth(doc, AgeBand.U10, youth_rules, exercises=catalog)
    assert result.ok is False, why
    assert codes.YOUTH_EXERCISE_NOT_ALLOWED in _codes(result)


def test_an_unparseable_allow_value_cannot_be_stored() -> None:
    """The map's values are an enum, so "maybe" never reaches the validator to be interpreted."""
    for bad in ("maybe", "Yes", "YES", "true", 1, None):
        with pytest.raises(PydanticValidationError):
            make_exercise(id="probe-move", youth_ok_by_band={AgeBand.U10: bad})


def test_an_unknown_band_key_cannot_be_stored() -> None:
    for bad in ("u11", "child", "adult ", "U10"):
        with pytest.raises(PydanticValidationError):
            make_exercise(id="probe-move", youth_ok_by_band={bad: "yes"})


# --- banned tags, every tag of every band ---------------------------------------------------


@pytest.mark.parametrize("band", YOUTH_BANDS, ids=lambda b: b.value)
def test_every_banned_tag_of_every_band_is_enforced(band: AgeBand, youth_rules: dict[AgeBand, YouthRuleSet]) -> None:
    """Not a sample: one exercise per banned tag, so no entry in the list is decorative."""
    rules = youth_rules[band]
    assert rules.banned_tags, f"{band.value} bans nothing, which is itself suspicious"
    for tag in rules.banned_tags:
        exercise = make_exercise(
            id="tagged-move",
            tags=[tag],
            youth_ok_by_band=dict.fromkeys(YOUTH_BANDS, YouthAllow.YES),
        )
        catalog = {**CATALOG, exercise.id: exercise}
        doc = make_workout(
            rows=[make_row(exercise_id="tagged-move", sets=1, reps=rules.rep_min_bodyweight)],
            estimated_minutes=rules.max_session_minutes,
        )
        result = _youth(doc, band, youth_rules, exercises=catalog, has_overhead_anchor=True)
        assert result.ok is False, f"{band.value} accepted an exercise tagged {tag.value}"
        assert codes.YOUTH_BANNED_TAG in _codes(result), f"{band.value}/{tag.value}"


@pytest.mark.parametrize("spelling", ["MAX_EFFORT", "Max_Effort", "max effort", "max-effort", "maxeffort", "one_RM"])
def test_tag_spelling_is_not_normalised(spelling: str) -> None:
    """A tag that parses under a near-miss spelling would sit outside every banned_tags list."""
    with pytest.raises(PydanticValidationError):
        Exercise.model_validate({**VALID_EXERCISE, "tags": [spelling]})


def test_a_banned_tag_is_caught_even_among_permitted_ones(youth_rules: dict[AgeBand, YouthRuleSet]) -> None:
    exercise = make_exercise(
        id="tagged-move",
        tags=[ExerciseTag.UNILATERAL, ExerciseTag.TEMPO, ExerciseTag.MAX_EFFORT, ExerciseTag.PLAY],
        youth_ok_by_band=dict.fromkeys(YOUTH_BANDS, YouthAllow.YES),
    )
    rules = youth_rules[AgeBand.U10]
    doc = make_workout(
        rows=[make_row(exercise_id="tagged-move", sets=1, reps=rules.rep_min_bodyweight)],
        estimated_minutes=rules.max_session_minutes,
    )
    result = _youth(doc, AgeBand.U10, youth_rules, exercises={**CATALOG, exercise.id: exercise})
    assert codes.YOUTH_BANNED_TAG in _codes(result)


# --- session-level caps, at the boundary ----------------------------------------------------


@pytest.mark.parametrize("band", YOUTH_BANDS, ids=lambda b: b.value)
def test_session_length_and_exercise_count_bite_one_step_over(
    band: AgeBand, youth_rules: dict[AgeBand, YouthRuleSet]
) -> None:
    rules = youth_rules[band]
    row = make_row(sets=1, reps=rules.rep_min_bodyweight)

    at_limit = make_workout(
        rows=[row for _ in range(rules.max_exercises_per_session)],
        estimated_minutes=rules.max_session_minutes,
    )
    assert _youth(at_limit, band, youth_rules).ok is True

    one_long = make_workout(rows=[row], estimated_minutes=rules.max_session_minutes + 1)
    assert codes.YOUTH_SESSION_LENGTH in _codes(_youth(one_long, band, youth_rules))

    one_many = make_workout(
        rows=[row for _ in range(rules.max_exercises_per_session + 1)],
        estimated_minutes=rules.max_session_minutes,
    )
    assert codes.YOUTH_EXERCISE_COUNT in _codes(_youth(one_many, band, youth_rules))

    one_set = make_workout(
        rows=[make_row(sets=rules.max_sets_per_exercise + 1, reps=rules.rep_min_bodyweight)],
        estimated_minutes=rules.max_session_minutes,
    )
    assert codes.YOUTH_SET_COUNT in _codes(_youth(one_set, band, youth_rules))


@pytest.mark.parametrize("band", YOUTH_BANDS, ids=lambda b: b.value)
def test_the_rest_floor_bites_one_second_short(band: AgeBand, youth_rules: dict[AgeBand, YouthRuleSet]) -> None:
    """Only on loaded rows: a bodyweight row has no rest floor (principles section 3.2)."""
    rules = youth_rules[band]
    if LoadType.DUMBBELL not in rules.allowed_load_types or not rules.max_load_kg_per_hand:
        pytest.skip(f"{band.value} permits no loaded dumbbell row")

    def run(rest_s: int) -> set[str]:
        row = make_row(
            exercise_id="db-bent-row",
            sets=1,
            reps=rules.rep_min_loaded or 8,
            load_unit="per_hand",
            load_kg=rules.max_load_kg_per_hand,
            rest_s=rest_s,
        )
        doc = make_workout(rows=[row], estimated_minutes=rules.max_session_minutes)
        return _codes(_youth(doc, band, youth_rules))

    assert codes.YOUTH_REST_FLOOR not in run(rules.min_rest_s_loaded)
    assert codes.YOUTH_REST_FLOOR in run(rules.min_rest_s_loaded - 1)
    assert codes.YOUTH_REST_FLOOR in run(0)


# --- the adult path never reaches a youth rule ----------------------------------------------

YOUTH_CODES = {code for code in codes.ALL_CODES if code.startswith("youth_")}


@pytest.mark.parametrize(
    "row",
    [
        {"exercise_id": "db-bent-row", "sets": 4, "reps": 30, "load_unit": "per_hand", "load_kg": 40.0, "rest_s": 0},
        {"exercise_id": "kb-swing", "sets": 4, "reps": 25, "load_unit": "per_implement", "load_kg": 24.0, "rest_s": 5},
        {"exercise_id": "hollow-hold", "sets": 4, "reps": None, "seconds": 60, "load_unit": "bodyweight", "rest_s": 0},
        {
            "exercise_id": "goblet-squat",
            "sets": 4,
            "reps": 5,
            "load_unit": "per_implement",
            "load_kg": 30.0,
            "rest_s": 10,
            "amrap": True,
            "rpe_target": 10,
        },
    ],
    ids=["over-cap-dumbbell", "24kg-bell", "youth-denied-everywhere", "amrap-at-rpe-10"],
)
def test_no_youth_code_ever_fires_for_an_adult(row: dict[str, Any], youth_rules: dict[AgeBand, YouthRuleSet]) -> None:
    """The gate is band-scoped. Passing the rule table in must not make an adult youth-checked."""
    doc = make_workout(rows=[make_row(**row)], estimated_minutes=45)
    result = _adult(doc, youth_rules=youth_rules, bodyweight_kg=30.0)
    assert _codes(result) & YOUTH_CODES == set(), sorted(_codes(result))
    assert result.ok is True


def test_an_adult_still_obeys_the_equipment_and_anchor_rules() -> None:
    """Band-scoped does not mean unchecked: the non-age rules apply to the parent too."""
    anchor_row = make_row(exercise_id="dead-hang", reps=None, seconds=30)
    assert codes.REQUIRES_ANCHOR_UNAVAILABLE in _codes(_adult(make_workout(rows=[anchor_row])))

    db_row = make_row(exercise_id="db-bent-row", sets=1, reps=10, load_unit="per_hand", load_kg=10.0, rest_s=60)
    result = _adult(make_workout(rows=[db_row]), equipment=[Equipment.BODYWEIGHT])
    assert codes.EQUIPMENT_NOT_AVAILABLE in _codes(result)


# --- unknown and malformed exercise ids ------------------------------------------------------

UNKNOWN_IDS = [
    "barbell-back-squat",
    "push-up-",
    "push--up",
    "PUSH-UP",
    "push up",
    "push_up",
    "pušh-up",
    "x" * 64,
    "../../etc/passwd",
    "push-up; DROP TABLE exercise",
    "0",
]


@pytest.mark.parametrize("exercise_id", UNKNOWN_IDS)
def test_an_unknown_or_malformed_exercise_id_never_resolves(exercise_id: str) -> None:
    """Either the slug pattern rejects it, or it is simply not in the library. Never both silent."""
    result = _adult(make_workout(rows=[make_row(exercise_id=exercise_id)]))
    assert result.ok is False, f"{exercise_id!r} resolved to something"
    assert {codes.UNKNOWN_EXERCISE, codes.SCHEMA} & _codes(result)


def test_an_unknown_exercise_is_not_youth_checked_into_a_pass(youth_rules: dict[AgeBand, YouthRuleSet]) -> None:
    """An unresolvable row must fail, not skip the youth rules and be reported clean."""
    doc = make_workout(rows=[make_row(exercise_id="barbell-back-squat", load_kg=None)], estimated_minutes=20)
    result = _youth(doc, AgeBand.U10, youth_rules)
    assert result.ok is False
    assert codes.UNKNOWN_EXERCISE in _codes(result)


def test_an_empty_catalog_denies_every_row(youth_rules: dict[AgeBand, YouthRuleSet]) -> None:
    result = _youth(make_workout(), AgeBand.U10, youth_rules, exercises={})
    assert result.ok is False
    assert codes.UNKNOWN_EXERCISE in _codes(result)
