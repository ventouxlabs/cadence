"""Youth checks V1-V14 (principles section 3.10, plus V14 from section 9).

These are the reason the validator exists. A youth workout that breaks a band cap must not
render, whether it arrived from the seed library, a pasted import, or the AI generator.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from cadence.schema.enums import AgeBand, DayType, ExerciseTag, GoalType, LoadUnit, YouthAllow
from cadence.schema.exercise import Exercise
from cadence.schema.workout import Workout, WorkoutRow
from cadence.schema.youth import YouthRuleSet
from cadence.validateur import codes
from cadence.validateur.result import ValidationError, make_error

# Used when neither the exercise nor the row says how long a set takes.
DEFAULT_SECONDS_PER_SET = 45

STRICTEST_BAND = AgeBand.U10


def effective_band(profile_kind: Literal["adult", "youth"] | None, age_band: AgeBand) -> tuple[AgeBand, bool]:
    """The band the youth rules are actually evaluated against, and whether it was coerced.

    A youth profile carrying ``adult`` is the strictest-band default of principles section 3.8
    wearing a disguise: the adult rule set exists in the YAML, so without this the youth checks
    would run against caps of ``None`` and pass everything. Until the son's age is set, a youth
    profile is evaluated as ``u10``.
    """
    if profile_kind == "youth" and age_band is AgeBand.ADULT:
        return STRICTEST_BAND, True
    return age_band, False


def usable_bodyweight(bodyweight_kg: object) -> float | None:
    """A bodyweight that arithmetic can trust, or ``None`` meaning unknown.

    ``None`` was the only "unknown" the first implementation recognised, so ``float("nan")``
    counted as known: every ``nan > cap`` comparison is False, and the percentage cap silently
    evaporated. Anything that is not a finite, positive, real number - ``None``, ``nan``, an
    infinity, zero, a negative, a bool, a string, anything else - is unknown. Unknown makes the
    percentage cap fall back to the absolute one and makes a ``conditional`` exercise deny.
    """
    if isinstance(bodyweight_kg, bool) or not isinstance(bodyweight_kg, int | float):
        return None
    value = float(bodyweight_kg)
    if not math.isfinite(value) or value <= 0:
        return None
    return value


def effective_cap(load_unit: LoadUnit, rules: YouthRuleSet, bodyweight_kg: float | None) -> float | None:
    """Lower of the absolute and percentage caps for this ``load_unit`` (principles section 3.3).

    A missing bodyweight falls back to the absolute cap. It is never treated as unlimited, and
    a cap of ``0.0`` is a real cap, not an absent one - hence the ``is None`` tests throughout.
    """
    if load_unit is LoadUnit.PER_HAND:
        abs_cap, pct = rules.max_load_kg_per_hand, rules.max_load_pct_bw_per_hand
    elif load_unit is LoadUnit.PER_IMPLEMENT:
        abs_cap, pct = rules.max_load_kg_per_implement, rules.max_load_pct_bw_per_implement
    else:
        return None
    weight = usable_bodyweight(bodyweight_kg)
    if pct is None or weight is None:
        return abs_cap
    pct_cap = pct * weight
    return pct_cap if abs_cap is None else min(abs_cap, pct_cap)


def is_loaded(row: WorkoutRow) -> bool:
    """A row carrying external load, and therefore subject to the load/rep/rest floors.

    The kilos decide, not the label: a row that calls itself ``bodyweight`` while carrying load
    would otherwise skip the rep and rest floors. The schema forbids that combination outright,
    and this keeps the check true even if it ever stops doing so.
    """
    return row.load_kg is not None and row.load_kg > 0


def check_row(
    index: int,
    row: WorkoutRow,
    exercise: Exercise | None,
    rules: YouthRuleSet,
    bodyweight_kg: float | None,
) -> list[ValidationError]:
    """Per-row youth checks: V1, V2, V3, V4, V5, V7, V10, V12."""
    at = f"rows[{index}]"
    found: list[ValidationError] = []
    loaded = is_loaded(row)

    if exercise is not None and exercise.load_type not in rules.allowed_load_types:
        allowed = ", ".join(t.value for t in rules.allowed_load_types)
        found.append(
            make_error(
                f"{at}.exercise_id",
                codes.YOUTH_LOAD_TYPE_NOT_ALLOWED,
                f"{exercise.id} is {exercise.load_type.value}; band {rules.band.value} allows only {allowed}",
            )
        )

    if row.load_kg is not None and row.load_kg > 0 and exercise is not None:
        # The cap column follows how the exercise is *held*, never how the row labels it.
        # Reading the row's own load_unit lets a document pick its own cap - or pick "total",
        # which the band table cannot cap at all.
        cap_unit = exercise.load_unit
        cap = effective_cap(cap_unit, rules, bodyweight_kg)
        if cap is None:
            found.append(
                make_error(
                    f"{at}.load_kg",
                    codes.YOUTH_LOAD_UNCAPPABLE,
                    f"{row.load_kg} kg cannot be capped for band {rules.band.value}: {exercise.id} is held "
                    f"{cap_unit.value}, and that unit has no cap in the band table",
                )
            )
        elif row.load_kg > cap:
            found.append(
                make_error(
                    f"{at}.load_kg",
                    codes.YOUTH_LOAD_EXCEEDED,
                    f"{row.load_kg} kg {cap_unit.value} is over the {cap:g} kg cap for band {rules.band.value}",
                )
            )

    if loaded and rules.rep_min_loaded is not None and row.reps is not None and row.reps < rules.rep_min_loaded:
        found.append(
            make_error(
                f"{at}.reps",
                codes.YOUTH_REP_FLOOR,
                f"{row.reps} reps under load is below the {rules.rep_min_loaded}-rep floor for band {rules.band.value}",
            )
        )

    ceiling = rules.rep_max_loaded if loaded else rules.rep_max_bodyweight
    if ceiling is not None and row.reps is not None and row.reps > ceiling:
        found.append(
            make_error(
                f"{at}.reps",
                codes.YOUTH_REP_CEILING,
                f"{row.reps} reps is above the {ceiling}-rep ceiling for band {rules.band.value}",
            )
        )

    if loaded and row.rest_s < rules.min_rest_s_loaded:
        found.append(
            make_error(
                f"{at}.rest_s",
                codes.YOUTH_REST_FLOOR,
                f"{row.rest_s} s rest under load is below the {rules.min_rest_s_loaded} s floor for band "
                f"{rules.band.value}",
            )
        )

    if row.sets > rules.max_sets_per_exercise:
        found.append(
            make_error(
                f"{at}.sets",
                codes.YOUTH_SET_COUNT,
                f"{row.sets} sets is above the {rules.max_sets_per_exercise}-set limit for band {rules.band.value}",
            )
        )

    if row.amrap and not rules.allow_amrap:
        found.append(
            make_error(
                f"{at}.amrap",
                codes.YOUTH_AMRAP_NOT_ALLOWED,
                f"as-many-reps-as-possible sets are not allowed for band {rules.band.value}",
            )
        )

    if row.rpe_target is not None and row.rpe_target > rules.rpe_cap:
        found.append(
            make_error(
                f"{at}.rpe_target",
                codes.YOUTH_RPE_EXCEEDED,
                f"effort target {row.rpe_target} is above the cap of {rules.rpe_cap} for band {rules.band.value}",
            )
        )

    return found


def _allow_state(exercise_doc: Exercise | Mapping[str, Any], age_band: AgeBand) -> YouthAllow:
    """Read one band out of an exercise's allow map, from a model or a raw document.

    Anything unreadable - a missing map, a missing band, a value that is not a ``YouthAllow`` -
    resolves to ``no``. The allowlist denies by default so a malformed document cannot permit.
    """
    if isinstance(exercise_doc, Exercise):
        return exercise_doc.youth_ok_by_band.get(age_band, YouthAllow.NO)
    raw = exercise_doc.get("youth_ok_by_band") if hasattr(exercise_doc, "get") else None
    if not isinstance(raw, Mapping):
        return YouthAllow.NO
    value = raw.get(age_band, raw.get(age_band.value))
    try:
        return YouthAllow(value)
    except ValueError:
        return YouthAllow.NO


def youth_allows(
    exercise_doc: Exercise | Mapping[str, Any],
    age_band: AgeBand,
    *,
    bodyweight_kg: float | None = None,
    profile_kind: Literal["adult", "youth"] | None = None,
) -> bool:
    """Whether this exercise is permitted for this band (principles section 9).

    The single implementation behind V14, shared so the seed loader (PRP-01) and the import and
    generate paths (PRP-08) filter on exactly what the validator enforces. The adult band is
    never gated - but pass ``profile_kind="youth"`` and an ``adult`` band is coerced to the
    strictest one, so a youth profile whose age is not yet set cannot walk through. ``conditional``
    needs a known bodyweight, because principles section 3.6 says an unweighed youth gets the
    substitution rather than the load.
    """
    band, _ = effective_band(profile_kind, age_band)
    if band is AgeBand.ADULT:
        return True
    allowed = _allow_state(exercise_doc, band)
    if allowed is YouthAllow.YES:
        return True
    if allowed is YouthAllow.CONDITIONAL:
        return usable_bodyweight(bodyweight_kg) is not None
    return False


def check_allowlist(
    index: int,
    exercise: Exercise,
    age_band: AgeBand,
    bodyweight_kg: float | None,
) -> list[ValidationError]:
    """V14 - the per-band allowlist of principles section 9, checked in addition to V1-V13.

    A band absent from the dict is a denial, so a seed row that forgets a column denies rather
    than permits. ``conditional`` is the one place a missing bodyweight denies instead of
    falling back to the absolute cap: principles section 3.6 says an unweighed youth gets the
    substitution, not the kettlebell.
    """
    if youth_allows(exercise, age_band, bodyweight_kg=bodyweight_kg):
        return []
    allowed = _allow_state(exercise, age_band)
    reason = (
        f"needs a recorded bodyweight before it is allowed for band {age_band.value}"
        if allowed is YouthAllow.CONDITIONAL
        else f"is not allowed for band {age_band.value}"
    )
    return [
        make_error(
            f"rows[{index}].exercise_id",
            codes.YOUTH_EXERCISE_NOT_ALLOWED,
            f"{exercise.id} {reason}",
        )
    ]


def check_banned_tags(index: int, exercise: Exercise, rules: YouthRuleSet, day_type: DayType) -> list[ValidationError]:
    """V8 - the exercise carries a tag banned for this band.

    Principles section 3.4 exempts ``assessment_only`` exercises from the ``max_effort`` ban on
    assessment day, and from that ban only: ``one_rm`` and the rest stay banned every day.
    """
    banned_set = set(rules.banned_tags)
    if day_type is DayType.ASSESSMENT and ExerciseTag.ASSESSMENT_ONLY in exercise.tags:
        banned_set.discard(ExerciseTag.MAX_EFFORT)
    banned = [tag for tag in exercise.tags if tag in banned_set]
    return [
        make_error(
            f"rows[{index}].exercise_id",
            codes.YOUTH_BANNED_TAG,
            f"{exercise.id} is tagged {tag.value}, which is banned for band {rules.band.value}",
        )
        for tag in banned
    ]


def row_seconds(row: WorkoutRow, exercise: Exercise | None) -> int:
    """How long one row takes: work across the sets, plus the rest between them.

    A row prescribed in seconds is timed by its own number, not by the library's estimate for
    the movement: a one-hour hold takes an hour whatever ``est_seconds_per_set`` says.
    """
    per_set = row.seconds
    if per_set is None:
        per_set = exercise.est_seconds_per_set if exercise is not None else None
    if per_set is None:
        per_set = DEFAULT_SECONDS_PER_SET
    return row.sets * per_set + row.rest_s * max(row.sets - 1, 0)


def computed_minutes(
    workout: Workout,
    resolved: Sequence[Exercise | None],
    skip: frozenset[int] = frozenset(),
) -> int:
    """Session length derived from the rows, rounded up. ``skip`` drops rows by index."""
    total = sum(row_seconds(row, resolved[index]) for index, row in enumerate(workout.rows) if index not in skip)
    return math.ceil(total / 60)


def counts_toward_exercise_cap(exercise: Exercise | None) -> bool:
    """Whether a row counts against the exercise-count cap.

    Only the resolved exercise's own ``is_prelude`` exempts it. A row cannot exempt itself, and
    an unresolved exercise counts, so neither a stray flag nor an unknown id shrinks the count.
    """
    return exercise is None or not exercise.is_prelude


def is_assessment_only(exercise: Exercise | None) -> bool:
    """Whether this movement is one of the measured tests (principles section 10.10)."""
    return exercise is not None and ExerciseTag.ASSESSMENT_ONLY in exercise.tags


def check_session(workout: Workout, rules: YouthRuleSet, resolved: Sequence[Exercise | None]) -> list[ValidationError]:
    """Whole-session youth checks: V6 and V9.

    Assessment day exempts the *measured tests* from both caps, not the session. Principles
    section 10.10 replaces those limits for the battery; it does not delete them, and
    ``day_type`` is a value the document supplies - so exempting the whole session let sixty
    rows and two hours through by writing one word. Only rows whose resolved exercise carries
    ``assessment_only`` step out of the count and out of the clock; every other row, including
    rows 1 and 2 of the battery itself, is measured against the band's caps as usual.
    """
    found: list[ValidationError] = []
    assessment = workout.day_type is DayType.ASSESSMENT
    exempt = frozenset(
        index for index, _ in enumerate(workout.rows) if assessment and is_assessment_only(resolved[index])
    )

    counted = [
        index
        for index, _ in enumerate(workout.rows)
        if index not in exempt and counts_toward_exercise_cap(resolved[index])
    ]
    if len(counted) > rules.max_exercises_per_session:
        found.append(
            make_error(
                "rows",
                codes.YOUTH_EXERCISE_COUNT,
                f"{len(counted)} exercises (prelude excluded) is above the "
                f"{rules.max_exercises_per_session} allowed for band {rules.band.value}",
            )
        )

    # The declared figure is a claim; the computed one is what the rows actually add up to.
    # Taking the larger means a document cannot shorten itself past the cap by understating.
    # On assessment day the declared figure legitimately covers the battery, so only the
    # computed length of the non-test rows is judged.
    derived = computed_minutes(workout, resolved, exempt)
    minutes = derived if assessment else max(workout.estimated_minutes, derived)
    if minutes > rules.max_session_minutes:
        source = "computed from the rows" if assessment or derived > workout.estimated_minutes else "declared"
        found.append(
            make_error(
                "estimated_minutes",
                codes.YOUTH_SESSION_LENGTH,
                f"{minutes} min ({source}) is above the {rules.max_session_minutes} min limit for band "
                f"{rules.band.value}",
            )
        )
    return found


def check_goals(goal_types: Sequence[GoalType] | None, rules: YouthRuleSet) -> list[ValidationError]:
    """V11 - the profile carries a goal type banned for this band."""
    if not goal_types:
        return []
    banned = set(rules.banned_goal_types)
    return [
        make_error(
            "$.goal_types",
            codes.YOUTH_BANNED_GOAL,
            f"goal type {goal.value} is never shown to a youth profile (band {rules.band.value})",
        )
        for goal in goal_types
        if goal in banned
    ]


def check_anchor(
    workout: Workout,
    exercises: Mapping[str, Exercise],
    has_overhead_anchor: bool,
) -> list[ValidationError]:
    """V13 - a row needs a pull-up bar that this household does not have (principles section 1.7).

    Applied to every profile, not only youth: the anchor is a hardware fact, not an age rule.
    """
    if has_overhead_anchor:
        return []
    found: list[ValidationError] = []
    for index, row in enumerate(workout.rows):
        exercise = exercises.get(row.exercise_id)
        if exercise is not None and ExerciseTag.REQUIRES_ANCHOR in exercise.tags:
            found.append(
                make_error(
                    f"rows[{index}].exercise_id",
                    codes.REQUIRES_ANCHOR_UNAVAILABLE,
                    f"{exercise.id} needs an overhead anchor and this profile has none",
                )
            )
    return found
