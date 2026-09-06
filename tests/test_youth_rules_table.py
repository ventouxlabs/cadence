"""``library/youth_rules.yaml`` against ``docs/exercise-principles.md`` section 3, cell for cell.

The YAML is law: the validator refuses whatever it forbids, so a single mistyped cell silently
widens a youth cap for every seed, import and generated workout alike. Every number below is
transcribed from the principles tables rather than from the YAML, so an edit to the YAML that
drifts from the document fails here instead of shipping.

Sources: section 3.2 (rule table), 3.4 (banned tags), 3.5 (banned goal types), 7.4 (assessment
caps). Deviations sanctioned by D-035 are named in ``DOCUMENTED_DEVIATIONS`` rather than skipped.
"""

from __future__ import annotations

from typing import Any

import pytest

from cadence.schema import AgeBand, AssessmentId, ExerciseTag, GoalType, LoadType, YouthRuleSet

BW = LoadType.BODYWEIGHT
DB = LoadType.DUMBBELL
KB = LoadType.KETTLEBELL
BA = LoadType.BENCH_ASSISTED

# docs/exercise-principles.md section 3.2, one entry per row of the table. "n/a" and "none" are
# both written as None. Percentages are stored as fractions: the table's 15 % is 0.15.
RULE_TABLE: dict[str, dict[AgeBand, Any]] = {
    "allowed_load_types": {
        AgeBand.U10: [BW, BA],
        AgeBand.AGE_10_13: [BW, DB, BA],
        AgeBand.AGE_14_17: [BW, DB, KB, BA],
        AgeBand.ADULT: [BW, DB, KB, BA],
    },
    "max_load_kg_per_hand": {
        AgeBand.U10: 0.0,
        AgeBand.AGE_10_13: 5.0,
        AgeBand.AGE_14_17: 12.0,
        AgeBand.ADULT: None,
    },
    "max_load_kg_per_implement": {
        AgeBand.U10: 0.0,
        AgeBand.AGE_10_13: 8.0,
        AgeBand.AGE_14_17: 16.0,
        AgeBand.ADULT: None,
    },
    "max_load_pct_bw_per_hand": {
        AgeBand.U10: 0.0,
        AgeBand.AGE_10_13: 0.15,
        AgeBand.AGE_14_17: 0.25,
        AgeBand.ADULT: None,
    },
    "max_load_pct_bw_per_implement": {
        AgeBand.U10: 0.0,
        AgeBand.AGE_10_13: 0.20,
        AgeBand.AGE_14_17: 0.35,
        AgeBand.ADULT: None,
    },
    "rep_min_loaded": {AgeBand.U10: None, AgeBand.AGE_10_13: 8, AgeBand.AGE_14_17: 8, AgeBand.ADULT: 5},
    "rep_max_loaded": {AgeBand.U10: None, AgeBand.AGE_10_13: 15, AgeBand.AGE_14_17: 15, AgeBand.ADULT: 20},
    "rep_min_bodyweight": {AgeBand.U10: 5, AgeBand.AGE_10_13: 5, AgeBand.AGE_14_17: 5, AgeBand.ADULT: 5},
    "rep_max_bodyweight": {AgeBand.U10: 15, AgeBand.AGE_10_13: 20, AgeBand.AGE_14_17: 25, AgeBand.ADULT: 40},
    "min_rest_s_loaded": {AgeBand.U10: None, AgeBand.AGE_10_13: 60, AgeBand.AGE_14_17: 75, AgeBand.ADULT: 60},
    "max_session_minutes": {AgeBand.U10: 20, AgeBand.AGE_10_13: 25, AgeBand.AGE_14_17: 35, AgeBand.ADULT: 45},
    "max_exercises_per_session": {AgeBand.U10: 5, AgeBand.AGE_10_13: 6, AgeBand.AGE_14_17: 7, AgeBand.ADULT: 8},
    "max_sets_per_exercise": {AgeBand.U10: 2, AgeBand.AGE_10_13: 3, AgeBand.AGE_14_17: 3, AgeBand.ADULT: 4},
    "rpe_cap": {AgeBand.U10: 6, AgeBand.AGE_10_13: 7, AgeBand.AGE_14_17: 7, AgeBand.ADULT: 8},
    "allow_amrap": {AgeBand.U10: False, AgeBand.AGE_10_13: False, AgeBand.AGE_14_17: False, AgeBand.ADULT: True},
    "allow_max_test": {AgeBand.U10: False, AgeBand.AGE_10_13: False, AgeBand.AGE_14_17: False, AgeBand.ADULT: True},
    "good_enough_done_after_n_exercises": {
        AgeBand.U10: 3,
        AgeBand.AGE_10_13: 3,
        AgeBand.AGE_14_17: 4,
        AgeBand.ADULT: 1,
    },
    # Section 3.4.
    "banned_tags": {
        AgeBand.U10: [
            ExerciseTag.MAX_EFFORT,
            ExerciseTag.ONE_RM,
            ExerciseTag.TO_FAILURE,
            ExerciseTag.PLYOMETRIC_DEPTH,
            ExerciseTag.LOADED_SPINAL_FLEXION,
            ExerciseTag.OVERHEAD_LOADED,
            ExerciseTag.LOADED_CARRY,
        ],
        AgeBand.AGE_10_13: [
            ExerciseTag.MAX_EFFORT,
            ExerciseTag.ONE_RM,
            ExerciseTag.TO_FAILURE,
            ExerciseTag.PLYOMETRIC_DEPTH,
            ExerciseTag.LOADED_SPINAL_FLEXION,
            ExerciseTag.OVERHEAD_LOADED,
        ],
        AgeBand.AGE_14_17: [
            ExerciseTag.MAX_EFFORT,
            ExerciseTag.ONE_RM,
            ExerciseTag.TO_FAILURE,
            ExerciseTag.PLYOMETRIC_DEPTH,
            ExerciseTag.LOADED_SPINAL_FLEXION,
        ],
        AgeBand.ADULT: [ExerciseTag.ONE_RM],
    },
    # Section 3.5: the same three for every youth band, none for the adult.
    "banned_goal_types": {
        AgeBand.U10: [GoalType.WEIGHT, GoalType.BODY_FAT, GoalType.APPEARANCE],
        AgeBand.AGE_10_13: [GoalType.WEIGHT, GoalType.BODY_FAT, GoalType.APPEARANCE],
        AgeBand.AGE_14_17: [GoalType.WEIGHT, GoalType.BODY_FAT, GoalType.APPEARANCE],
        AgeBand.ADULT: [],
    },
}

# Section 7.4's "cap" column, which is one column for all three youth bands, not one per band.
YOUTH_ASSESSMENT_CAPS: dict[AssessmentId, int] = {
    AssessmentId.PUSH_UP_MAX: 20,
    AssessmentId.DEAD_HANG_S: 90,
    AssessmentId.PLANK_S: 90,
    AssessmentId.WALL_ANGEL_REACH: 3,
    AssessmentId.GOBLET_SQUAT_QUALITY: 5,
    AssessmentId.FARMER_CARRY_S: 60,
}

# Cells where the shipped YAML deliberately differs from the document, per D-035. Each maps to
# (documented value, reason). Anything else that differs is a failure.
DOCUMENTED_DEVIATIONS: dict[tuple[AgeBand, str], tuple[Any, str]] = {
    (AgeBand.U10, "min_rest_s_loaded"): (
        60,
        "D-035: section 3.2 says n/a, but section 3.9 types the field int; inert because u10 "
        "allows no loaded row at all",
    ),
}

CELLS = [(band, field) for field in RULE_TABLE for band in AgeBand]


def _expected(band: AgeBand, field: str) -> Any:
    override = DOCUMENTED_DEVIATIONS.get((band, field))
    return override[0] if override is not None else RULE_TABLE[field][band]


@pytest.mark.parametrize(("band", "field"), CELLS, ids=lambda v: v.value if isinstance(v, AgeBand) else v)
def test_yaml_matches_principles_cell(band: AgeBand, field: str, youth_rules: dict[AgeBand, YouthRuleSet]) -> None:
    """Every cell of principles section 3.2 / 3.4 / 3.5 as shipped in the YAML."""
    assert band in youth_rules, f"library/youth_rules.yaml is missing band {band.value}"
    actual = getattr(youth_rules[band], field)
    expected = _expected(band, field)
    if isinstance(expected, list):
        assert sorted(item.value for item in actual) == sorted(item.value for item in expected), f"{band.value}.{field}"
    else:
        assert actual == expected, f"{band.value}.{field}"


def test_every_rule_field_is_covered_by_the_table() -> None:
    """A new field on ``YouthRuleSet`` must be transcribed here, not silently left unchecked."""
    covered = set(RULE_TABLE) | {"band", "assessment_caps"}
    assert covered == set(YouthRuleSet.model_fields)


@pytest.mark.parametrize("band", [AgeBand.U10, AgeBand.AGE_10_13, AgeBand.AGE_14_17], ids=lambda b: b.value)
def test_youth_assessment_caps_match_section_7_4(band: AgeBand, youth_rules: dict[AgeBand, YouthRuleSet]) -> None:
    assert youth_rules[band].assessment_caps == YOUTH_ASSESSMENT_CAPS


def test_adult_band_is_uncapped_where_the_document_says_none(youth_rules: dict[AgeBand, YouthRuleSet]) -> None:
    """The adult row's four "none" cells are ``None``, never 0.0 - a 0.0 cap forbids all load."""
    adult = youth_rules[AgeBand.ADULT]
    assert adult.max_load_kg_per_hand is None
    assert adult.max_load_kg_per_implement is None
    assert adult.max_load_pct_bw_per_hand is None
    assert adult.max_load_pct_bw_per_implement is None
    assert adult.assessment_caps == {}


def test_u10_zero_caps_are_zero_not_none(youth_rules: dict[AgeBand, YouthRuleSet]) -> None:
    """0.0 is a real cap of zero. Read as "absent" it would make the youngest band the freest."""
    u10 = youth_rules[AgeBand.U10]
    assert u10.max_load_kg_per_hand == 0.0
    assert u10.max_load_kg_per_implement == 0.0
    assert u10.max_load_pct_bw_per_hand == 0.0
    assert u10.max_load_pct_bw_per_implement == 0.0


def test_every_band_is_present_and_self_labelled(youth_rules: dict[AgeBand, YouthRuleSet]) -> None:
    """A rule set filed under the wrong key would apply another band's caps under its name."""
    assert set(youth_rules) == set(AgeBand)
    for band, rules in youth_rules.items():
        assert rules.band is band


def test_caps_are_monotonic_across_the_bands(youth_rules: dict[AgeBand, YouthRuleSet]) -> None:
    """Younger is never allowed more than older. A transposed pair of rows would show up here."""
    ordered = [AgeBand.U10, AgeBand.AGE_10_13, AgeBand.AGE_14_17]
    for younger, older in zip(ordered, ordered[1:], strict=False):
        y, o = youth_rules[younger], youth_rules[older]
        assert y.max_load_kg_per_hand <= o.max_load_kg_per_hand
        assert y.max_load_kg_per_implement <= o.max_load_kg_per_implement
        assert y.max_session_minutes <= o.max_session_minutes
        assert y.max_exercises_per_session <= o.max_exercises_per_session
        assert y.max_sets_per_exercise <= o.max_sets_per_exercise
        assert y.rpe_cap <= o.rpe_cap
        assert set(y.allowed_load_types) <= set(o.allowed_load_types)
        assert set(o.banned_tags) <= set(y.banned_tags)


def test_no_youth_band_permits_amrap_or_max_testing(youth_rules: dict[AgeBand, YouthRuleSet]) -> None:
    for band in (AgeBand.U10, AgeBand.AGE_10_13, AgeBand.AGE_14_17):
        assert youth_rules[band].allow_amrap is False
        assert youth_rules[band].allow_max_test is False


def test_kettlebells_reach_only_the_oldest_youth_band(youth_rules: dict[AgeBand, YouthRuleSet]) -> None:
    """Principles section 3.6: the household's 16 kg bell is legal for 14-17 and nobody younger."""
    assert LoadType.KETTLEBELL not in youth_rules[AgeBand.U10].allowed_load_types
    assert LoadType.KETTLEBELL not in youth_rules[AgeBand.AGE_10_13].allowed_load_types
    assert LoadType.KETTLEBELL in youth_rules[AgeBand.AGE_14_17].allowed_load_types
    # The 16 kg bell sits exactly on the band's per-implement cap; the 24 kg bell never fits.
    assert youth_rules[AgeBand.AGE_14_17].max_load_kg_per_implement == 16.0
