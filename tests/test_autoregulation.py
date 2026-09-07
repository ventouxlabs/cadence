"""The autoregulation decision table and its arithmetic - principles sections 5.4, 5.5 and 5.6.

PRP-07 acceptance tests 1-13 and 16, plus the missed-session hold the task brief names. Everything
here is a pure function of its arguments, so nothing in this file opens a database.

The band caps are read from ``library/youth_rules.yaml`` off disk and the rungs from the shipped
``weights_available`` default, the way ``tests/test_program_matrix.py`` does it: the law is the
file, not a constant retyped into the test, and test 11 is worthless if the cap it asserts against
is not the cap the arithmetic saw.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from cadence.profils.settings import DEFAULT_SETTINGS
from cadence.programme.arithmetic import apply_outcome, consume_pending_bump
from cadence.programme.bands import effective_cap
from cadence.programme.decision import RULES, AutoregInput, decide
from cadence.programme.ladder import parse_weights_available
from cadence.schema import AgeBand, YouthRules
from cadence.schema.enums import LoadType, LoadUnit
from tests.factories import make_exercise

YOUTH_RULES_YAML = Path(__file__).resolve().parents[1] / "library" / "youth_rules.yaml"
BANDS = YouthRules.model_validate(yaml.safe_load(YOUTH_RULES_YAML.read_text(encoding="utf-8"))).root
SON_BAND = BANDS[AgeBand.AGE_10_13]

DB_LADDER = parse_weights_available(DEFAULT_SETTINGS.weights_available).ladder(LoadType.DUMBBELL)

# D-027's list, restated rather than imported: ``tests/e2e/test_setup.py`` pulls in playwright, and
# this file has to run without it. Keep the two copies in step.
BANNED_PHRASES: tuple[str, ...] = (
    "body fat",
    "bodyfat",
    "body-fat",
    "body composition",
    "muscle %",
    "muscle percent",
    "lean mass",
    "body image",
)
BANNED_BARE_WORDS: tuple[str, ...] = ("weight", "fat", "lean", "abs", "calories")


def _row(**overrides: Any) -> dict[str, Any]:
    """A materialised row in the shape ``materialise_rows`` writes into ``rows_json``."""
    progression = {
        "type": "double_progression",
        "rep_min": 8,
        "rep_max": 12,
        "load_step_kg": 1.25,
        "load_step_pct": None,
        "time_step_s": 10,
        "distance_step_m": 10,
        "deload_pct": 0.60,
        "regress_step": 0.10,
        "regress_reps": 2,
        "allow_load_progression": True,
        "cap_load_kg": None,
    }
    progression.update(overrides.pop("progression", {}))
    base: dict[str, Any] = {
        "position": 0,
        "exercise_id": "goblet-squat",
        "name": "Goblet squat",
        "sets": 3,
        "reps": 8,
        "seconds": None,
        "meters": None,
        "load_kg": 6.8,
        "load_unit": LoadUnit.PER_IMPLEMENT.value,
        "is_prelude": False,
        "pending_bump": False,
        "last_outcome": "hold",
        "progression": progression,
    }
    base.update(overrides)
    return base


def _resolver(*exercises: Any):
    catalog = {exercise.id: exercise for exercise in exercises}
    return lambda exercise_id: catalog.get(exercise_id)


# ------------------------------------------------------------------- 1: the section 5.4 table

# One case per rule, each chosen so every earlier rule declines it. `id` is the rule that must
# fire; a reordering of the table changes the id even when the outcome is unchanged (risk 1).
TABLE_CASES: tuple[tuple[str, AutoregInput, str], ...] = (
    ("R1", AutoregInput(all_rows_ticked=True, felt="easy", readiness="ok", week_of_block=4), "deload"),
    ("R2", AutoregInput(all_rows_ticked=False, felt="hard", readiness="ok"), "regress"),
    ("R3", AutoregInput(all_rows_ticked=True, felt="easy", readiness="ok", missed_sessions_7d=2), "regress"),
    ("R4", AutoregInput(all_rows_ticked=True, felt="hard", readiness="low"), "regress"),
    ("R5", AutoregInput(all_rows_ticked=True, felt="hard", readiness="ok"), "hold"),
    ("R6", AutoregInput(all_rows_ticked=True, felt=None, readiness="low"), "hold"),
    ("R7", AutoregInput(all_rows_ticked=False, felt="easy", readiness="ok"), "hold"),
    ("R8", AutoregInput(all_rows_ticked=True, felt="easy", readiness="ok", missed_sessions_7d=1), "hold"),
    ("R9", AutoregInput(all_rows_ticked=True, felt="easy", readiness="ok"), "bump"),
    ("R10", AutoregInput(all_rows_ticked=True, felt="right", readiness="ok"), "hold"),
    ("R11", AutoregInput(all_rows_ticked=True, felt=None, readiness="unknown"), "hold"),
)


@pytest.mark.parametrize(("rule_id", "inp", "outcome"), TABLE_CASES, ids=[case[0] for case in TABLE_CASES])
def test_decision_table(rule_id: str, inp: AutoregInput, outcome: str) -> None:
    """1. Section 5.4 in order: both the outcome and the rule that produced it."""
    decision = decide(inp)
    assert decision.rule_id == rule_id
    assert decision.outcome == outcome


def test_decision_table_covers_every_rule() -> None:
    """1. The parametrisation is not allowed to skip a rule the table declares."""
    assert [case[0] for case in TABLE_CASES] == [rule_id for rule_id, _, _ in RULES]


# ---------------------------------------------------------------- 2: the illustrative slice

_ANY_READINESS = ("low", "ok", "high", "unknown")

# Section 5.4's seven-row slice: weeks 1-3, `missed_sessions_7d == 0`. The third row's readiness is
# "any", and R6 legitimately claims it when readiness is low, so that row asserts the outcome only.
SLICE_CASES: tuple[tuple[bool, str | None, str, str, str | None], ...] = (
    (True, "easy", "ok", "bump", "R9"),
    (True, "easy", "low", "hold", "R6"),
    *((True, "right", readiness, "hold", None) for readiness in _ANY_READINESS),
    (True, "hard", "ok", "hold", "R5"),
    (True, "hard", "low", "regress", "R4"),
    (False, "easy", "ok", "hold", "R7"),
    (False, "hard", "ok", "regress", "R2"),
)


@pytest.mark.parametrize(("ticked", "felt", "readiness", "outcome", "rule_id"), SLICE_CASES)
def test_illustrative_slice(ticked: bool, felt: str, readiness: str, outcome: str, rule_id: str | None) -> None:
    """2. The seven rows of section 5.4's illustrative table, reproduced exactly."""
    decision = decide(AutoregInput(all_rows_ticked=ticked, felt=felt, readiness=readiness, week_of_block=2))
    assert decision.outcome == outcome
    if rule_id is not None:
        assert decision.rule_id == rule_id


# --------------------------------------------------------- 3, 4: the bump week 4 suppresses


def test_r1_beats_r9() -> None:
    """3. Week 4 deloads a session that earned a bump, and stores the bump for next block."""
    decision = decide(AutoregInput(all_rows_ticked=True, felt="easy", readiness="ok", week_of_block=4))
    assert (decision.outcome, decision.rule_id, decision.earned_bump) == ("deload", "R1", True)

    row = _row(reps=10)
    deloaded = apply_outcome(row, "deload", None, DB_LADDER, earned_bump=decision.earned_bump)
    assert deloaded["pending_bump"] is True
    assert row["pending_bump"] is False, "apply_outcome must never write through to its argument"


def test_pending_bump_applied_next_block() -> None:
    """4. Week 1 of the next block applies the stored bump and clears the flag."""
    carried = _row(reps=8, pending_bump=True)
    week_one = consume_pending_bump(carried, None, DB_LADDER)
    assert week_one["reps"] == 9
    assert week_one["pending_bump"] is False
    assert consume_pending_bump(_row(reps=8), None, DB_LADDER)["reps"] == 8


# ------------------------------------------------------- 5, 6, 13: the rules that hold or drop


def test_partial_excluded_from_r7() -> None:
    """5. A short session holds. It never regresses, whatever is left unticked."""
    decision = decide(
        AutoregInput(all_rows_ticked=False, felt="right", readiness="ok", completion="partial", week_of_block=2)
    )
    assert decision.outcome == "hold"
    assert decision.rule_id == "R11"

    hard = decide(
        AutoregInput(all_rows_ticked=False, felt="hard", readiness="ok", completion="partial", week_of_block=2)
    )
    assert (hard.outcome, hard.rule_id) == ("hold", "R5"), "a partial session with felt=hard still reaches R5"


def test_felt_none_falls_through() -> None:
    """6. An unanswered prompt matches neither R2, R4 nor R5; low readiness holds it at R6."""
    decision = decide(AutoregInput(all_rows_ticked=True, felt=None, readiness="low", week_of_block=2))
    assert (decision.outcome, decision.rule_id) == ("hold", "R6")


def test_missed_sessions_two_regress() -> None:
    """13. Two missed slots regress at R3, whatever the session felt like."""
    decision = decide(AutoregInput(all_rows_ticked=True, felt="easy", readiness="high", missed_sessions_7d=2))
    assert (decision.outcome, decision.rule_id) == ("regress", "R3")


def test_missed_session_one_holds() -> None:
    """One missed slot holds at R8 - before R9 can bump it."""
    decision = decide(AutoregInput(all_rows_ticked=True, felt="easy", readiness="ok", missed_sessions_7d=1))
    assert (decision.outcome, decision.rule_id) == ("hold", "R8")


# ------------------------------------------------------------- 7, 8, 9, 10: the arithmetic


def test_bump_arithmetic_double_progression() -> None:
    """7. Reps to `rep_max`, then one rung of load with reps back at `rep_min`."""
    row = _row(reps=8, load_kg=6.8)
    for expected in (9, 10, 11, 12):
        row = apply_outcome(row, "bump", None, DB_LADDER)
        assert row["reps"] == expected
        assert row["load_kg"] == 6.8, "load does not move while there are reps left to add"

    stepped = apply_outcome(row, "bump", None, DB_LADDER)
    assert stepped["reps"] == 8, "reps reset to rep_min once the load moves"
    assert stepped["load_kg"] in DB_LADDER, "the new load is re-rounded onto the ladder"
    assert 6.8 < stepped["load_kg"] <= 6.8 + 1.25


def test_bump_rep_progression_advances_exercise() -> None:
    """8. At the ceiling a bodyweight row takes the harder movement at its rep floor."""
    origin = make_exercise(id="incline-push-up", name="Incline push-up", progression_of="push-up")
    harder = make_exercise(id="push-up", name="Push-up")
    row = _row(
        exercise_id="incline-push-up",
        name="Incline push-up",
        load_kg=None,
        reps=20,
        progression={"type": "rep_progression", "rep_min": 8, "rep_max": 20, "load_step_kg": None},
    )
    bumped = apply_outcome(row, "bump", None, (), resolve=_resolver(origin, harder))
    assert bumped["exercise_id"] == "push-up"
    assert bumped["name"] == "Push-up"
    assert bumped["reps"] == 8


def test_regress_drops_load_then_reps_then_exercise() -> None:
    """9. Section 5.5's three-step fallback, in order."""
    loaded = apply_outcome(_row(reps=12, load_kg=6.8), "regress", None, DB_LADDER)
    assert loaded["load_kg"] in DB_LADDER
    assert loaded["load_kg"] < 6.8
    assert loaded["reps"] == 12, "reps only move once there is no load left to shed"

    bodyweight = _row(
        exercise_id="push-up",
        name="Push-up",
        load_kg=None,
        reps=12,
        progression={"type": "rep_progression", "load_step_kg": None},
    )
    fewer_reps = apply_outcome(bodyweight, "regress", None, ())
    assert fewer_reps["reps"] == 10, "regress_reps comes off before the movement changes"
    assert fewer_reps["exercise_id"] == "push-up"

    easier = make_exercise(id="incline-push-up", name="Incline push-up")
    at_the_floor = {**bodyweight, "reps": 8}
    origin = make_exercise(id="push-up", name="Push-up", regression_of="incline-push-up")
    swapped = apply_outcome(at_the_floor, "regress", None, (), resolve=_resolver(origin, easier))
    assert swapped["exercise_id"] == "incline-push-up"
    assert swapped["reps"] == 8


@pytest.mark.parametrize(("sets", "expected"), [(3, 2), (4, 2), (2, 2)])
def test_deload_values(sets: int, expected: int) -> None:
    """10. Fewer sets, reps at the floor, the load untouched, the carry cut to 60%."""
    row = _row(sets=sets, reps=12, load_kg=6.8, meters=40, rpe_target=8)
    deloaded = apply_outcome(row, "deload", None, DB_LADDER)
    assert deloaded["sets"] == expected
    assert deloaded["reps"] == 8
    assert deloaded["load_kg"] == 6.8
    assert deloaded["meters"] == pytest.approx(24.0)
    assert deloaded["rpe_target"] == 6


# ----------------------------------------------------------------- 11, 12: the youth ceiling


def _youth_row(load_unit: LoadUnit = LoadUnit.PER_HAND, **overrides: Any) -> dict[str, Any]:
    """The son's row: ``allow_load_progression`` off and the real band cap injected, as built."""
    fields: dict[str, Any] = {
        "exercise_id": "db-bent-row",
        "name": "Dumbbell bent-over row",
        "sets": 2,
        "reps": 8,
        "load_kg": 2.27,
        "load_unit": load_unit.value,
        "progression": {
            "allow_load_progression": False,
            "cap_load_kg": effective_cap(SON_BAND, load_unit, None),
        },
    }
    fields.update(overrides)
    return _row(**fields)


# The two units the band table caps, with the cap it caps them at and the heaviest rung of the
# household ladder that fits underneath. Per-implement is the one with real headroom: six rungs of
# climbing, and a last step whose raw target (6.8 + 1.25 = 8.05) sits *above* the cap, which is
# the round-then-clamp case of risk 3.
@pytest.mark.parametrize(
    ("load_unit", "cap", "highest_rung"),
    [(LoadUnit.PER_HAND, 5.0, 4.54), (LoadUnit.PER_IMPLEMENT, 8.0, 7.94)],
    ids=["per_hand", "per_implement"],
)
def test_youth_cap_never_exceeded(load_unit: LoadUnit, cap: float, highest_rung: float) -> None:
    """11. Twenty all-ticked, felt-easy sessions for the son never lift him over the band cap."""
    assert effective_cap(SON_BAND, load_unit, None) == cap, "the shipped cap is what this asserts against"

    row = _youth_row(load_unit)
    seen: set[float] = {row["load_kg"]}
    for _ in range(20):
        decision = decide(AutoregInput(all_rows_ticked=True, felt="easy", readiness="ok", week_of_block=1))
        assert decision.outcome == "bump"
        row = apply_outcome(row, decision.outcome, SON_BAND, DB_LADDER, earned_bump=decision.earned_bump)
        assert row["load_kg"] <= cap, f"{row['load_kg']} kg is over the {cap} kg band cap"
        assert row["load_kg"] in DB_LADDER, "a load off the ladder is a load nobody owns"
        assert row["reps"] <= SON_BAND.rep_max_loaded
        seen.add(row["load_kg"])

    assert len(seen) > 1, "a load that never moved would pass this test vacuously"
    assert max(seen) == highest_rung, "the climb stops on the heaviest rung under the cap, and there"


def test_youth_bump_order() -> None:
    """12. Reps, then seconds, then the harder movement, and only then load."""
    reps_first = apply_outcome(_youth_row(reps=8), "bump", SON_BAND, DB_LADDER)
    assert reps_first["reps"] == 9
    assert reps_first["load_kg"] == 2.27, "step 1 never touches the load"

    seconds_next = apply_outcome(_youth_row(reps=12, seconds=30), "bump", SON_BAND, DB_LADDER)
    assert seconds_next["seconds"] == 40
    assert seconds_next["reps"] == 12
    assert seconds_next["load_kg"] == 2.27, "step 2 never touches the load"

    origin = make_exercise(id="db-bent-row", name="Dumbbell bent-over row", progression_of="push-up")
    harder = make_exercise(id="push-up", name="Push-up")
    swapped = apply_outcome(_youth_row(reps=12), "bump", SON_BAND, DB_LADDER, resolve=_resolver(origin, harder))
    assert swapped["exercise_id"] == "push-up", "step 3 comes before any load step"
    assert swapped["reps"] == 8

    # Step 4 only: reps at the ceiling, nothing to hold or walk, and no movement to advance to.
    load_last = apply_outcome(_youth_row(reps=12), "bump", SON_BAND, DB_LADDER)
    assert load_last["load_kg"] == 3.4
    assert load_last["reps"] == 12, "a youth load step does not reset the reps"
