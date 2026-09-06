"""Week schemes, the day-type rotation and the session-length row budget.

Everything here is a pure function of the settings. No clock, no randomness: two calls with equal
inputs produce equal output, which is what makes ``build_program`` re-runnable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from cadence.bibliotheque.template import ProfileKind, RowRole
from cadence.schema.enums import DayType

# Section 6.3. Indexed continuously across weeks so a three-day week does not repeat itself.
ROTATION: tuple[DayType, ...] = (
    DayType.UPPER_A,
    DayType.LOWER_A,
    DayType.UPPER_B,
    DayType.LOWER_FULL_B,
)

# Section 6.2 increments, applied relative to the template's own week-1 value (P7).
REP_STEP = 2
TIME_STEP_S = 10
DISTANCE_STEP_M = 10

DELOAD_SET_PCT = 0.60
MIN_DELOAD_SETS = 2

# Section 6.4. Anything between the named lengths falls to the shorter budget.
_BUDGETS: tuple[tuple[int, int, int, int], ...] = (
    (15, 1, 1, 1),
    (30, 1, 3, 1),
    (45, 1, 4, 2),
)

# Section 10, plus D-058 for the two youth day types section 10 ships no template for.
ADULT_WORKOUTS: dict[DayType, str] = {
    DayType.UPPER_A: "upper-a",
    DayType.LOWER_A: "lower-a",
    DayType.UPPER_B: "upper-b",
    DayType.LOWER_FULL_B: "lower-full-b",
    DayType.MOBILITY_CARRY: "mobility-carry-day",
    DayType.ASSESSMENT: "assessment-day",
}

YOUTH_WORKOUTS: dict[DayType, str] = {
    DayType.UPPER_A: "son-upper-a",
    DayType.LOWER_A: "son-lower-a",
    DayType.UPPER_B: "son-upper-a",
    DayType.LOWER_FULL_B: "together-full-body",
    DayType.MOBILITY_CARRY: "son-play-day",
    DayType.ASSESSMENT: "assessment-day",
}

PRELUDE_ID = "posture-prelude-v1"

Measure = Literal["reps", "seconds", "meters", "steps"]


@dataclass(frozen=True, slots=True)
class RowBudget:
    """How many rows of each role a session of this length carries (section 6.4)."""

    session_minutes: int
    main: int
    secondary: int
    finisher: int

    @property
    def total(self) -> int:
        return self.main + self.secondary + self.finisher


def row_budget(session_minutes: int) -> RowBudget:
    """The row budget for a session length, bucketed to the nearest shorter named length."""
    chosen = _BUDGETS[0]
    for minutes, main, secondary, finisher in _BUDGETS:
        if session_minutes >= minutes:
            chosen = (minutes, main, secondary, finisher)
    return RowBudget(session_minutes=chosen[0], main=chosen[1], secondary=chosen[2], finisher=chosen[3])


def workout_id_for(day_type: DayType, kind: ProfileKind) -> str:
    """Which seed template serves this day type for this profile kind."""
    table = YOUTH_WORKOUTS if kind == "youth" else ADULT_WORKOUTS
    return table[day_type]


def week_day_types(week: int, days_per_week: int, *, is_youth: bool) -> tuple[DayType, ...]:
    """The day types scheduled in one week (section 6.3).

    Two and four to six days are fixed patterns; only the three-day week walks the rotation, and
    it walks it continuously across weeks so it does not repeat the same three days every week.
    """
    if days_per_week == 2:
        return (DayType.UPPER_A, DayType.LOWER_FULL_B)
    if days_per_week == 3:
        start = (week - 1) * 3
        return tuple(ROTATION[(start + offset) % len(ROTATION)] for offset in range(3))
    if days_per_week == 4:
        return ROTATION
    if days_per_week == 5:
        return (*ROTATION, DayType.MOBILITY_CARRY)
    # Six: the five above plus a repeat of upper_a - or, for a youth profile, a second play day,
    # so a six-day youth week legitimately contains two of them.
    sixth = DayType.MOBILITY_CARRY if is_youth else DayType.UPPER_A
    return (*ROTATION, DayType.MOBILITY_CARRY, sixth)


def _step_for(measure: Measure) -> int:
    if measure == "reps":
        return REP_STEP
    if measure == "seconds":
        return TIME_STEP_S
    return DISTANCE_STEP_M


def scheme_sets(week: int, template_sets: int, role: RowRole) -> int:
    """Sets for this block week (section 6.2), relative to the template's own week-1 count."""
    if week <= 2:
        return template_sets
    if week == 3:
        return template_sets + 1 if role == "main" else template_sets
    week_three = scheme_sets(3, template_sets, role)
    return max(MIN_DELOAD_SETS, math.floor(week_three * DELOAD_SET_PCT))


def scheme_value(week: int, template_value: float, role: RowRole, measure: Measure) -> float:
    """The prescribed reps/seconds/metres for this block week.

    Week 4 returns the week-1 value, which is also the row's ``rep_min`` (D-057) - so acceptance
    test 12's ``reps == rep_min`` and section 6.2's "2 sets at the week-1 value" are one rule.
    """
    step = _step_for(measure)
    if week == 1 or week == 4:
        return template_value
    if week == 2:
        return template_value + step
    return template_value if role == "main" else template_value + 2 * step
