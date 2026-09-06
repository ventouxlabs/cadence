"""The week scheme, the band clamps and the row-time model.

One row's numbers, in order: section 6.2's week scheme relative to the template's own week 1
(P7), then section 3.2's band clamps, then section 7.4's assessment targets. Clamping, never
rejection - a youth row shrinks to fit and survives (D-019, D-063).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any

from cadence.programme.context import MEASURE_FIELD, BuildContext, Draft
from cadence.programme.schemes import scheme_sets, scheme_value
from cadence.schema.enums import Measure
from cadence.schema.workout import WorkoutRow
from cadence.validateur.youth_rules import row_seconds

# Section 6.4 row-time model.
SECONDS_PER_REP = 3.0
SECONDS_PER_METRE = 1.2
METRES_PER_STEP = 0.5

# The span of one block: week 1 reps to the week-3 secondary top (section 6.2).
REP_WINDOW = 4


@dataclass(frozen=True, slots=True)
class Prescription:
    sets: int
    reps: int | None
    seconds: int | None
    meters: float | None
    steps: int | None
    rest_s: int
    rpe_target: int | None
    amrap: bool
    notes: tuple[str, ...]


def prescribe(draft: Draft, week: int, load_kg: float | None, cap_kg: float | None, ctx: BuildContext) -> Prescription:
    """Apply the week scheme (section 6.2) then the band clamps (section 3.2, D-063)."""
    column = draft.column
    measure = draft.exercise.measure
    field = MEASURE_FIELD[measure]
    base = getattr(column, field)
    values: dict[str, float | None] = {"reps": None, "seconds": None, "meters": None, "steps": None}

    frozen_week = week if draft.role not in ("prelude", "assessment") else 1
    sets = scheme_sets(frozen_week, column.sets, draft.role)
    scaled = scheme_value(frozen_week, float(base if base is not None else 0), draft.role, field)  # type: ignore[arg-type]
    values[field] = round(scaled, 1) if measure is Measure.METERS else int(round(scaled))

    rest = column.rest_s
    rpe = column.rpe_target
    amrap = column.amrap
    notes: list[str] = []

    rules = ctx.rules
    if rules is not None:
        loaded = load_kg is not None and load_kg > 0
        sets = min(sets, rules.max_sets_per_exercise)
        if loaded:
            rest = max(rest, rules.min_rest_s_loaded)
        if values["reps"] is not None:
            reps = int(values["reps"])
            if loaded:
                floor = rules.rep_min_loaded or 1
                ceiling = rules.rep_max_loaded or rules.rep_max_bodyweight
                reps = min(max(reps, floor), max(ceiling, floor))
            else:
                reps = min(reps, rules.rep_max_bodyweight)
            values["reps"] = reps
        if rpe is not None:
            rpe = min(rpe, rules.rpe_cap)
        amrap = amrap and rules.allow_amrap
        if loaded and cap_kg is not None:
            # "Load", never "weight": this line renders on the son's Today screen, where
            # body-image language is banned outright (D-027, brief "Goals the program must serve").
            notes.append(f"Load stays at or under {cap_kg:g} kg at this age.")

    return Prescription(
        sets=sets,
        reps=None if values["reps"] is None else int(values["reps"]),
        seconds=None if values["seconds"] is None else int(values["seconds"]),
        meters=None if values["meters"] is None else float(values["meters"]),
        steps=None if values["steps"] is None else int(values["steps"]),
        rest_s=rest,
        rpe_target=rpe,
        amrap=amrap,
        notes=tuple(notes),
    )


def assessment_value(draft: Draft, prescribed: Prescription, ctx: BuildContext) -> Prescription:
    """Replace a youth assessment row's number with the band's fun target (section 7.4, D-062)."""
    if not ctx.is_youth or draft.assessment_id is None:
        return prescribed
    spec = ctx.library.assessments.spec(draft.assessment_id)
    target = ctx.library.assessments.youth_prescription(draft.assessment_id, ctx.band.band)
    # A substituted row is no longer the test it was named for - `farmer_carry_s` is timed but
    # the movement is walked in metres - so the target only lands when the measures still agree.
    if target is None or spec is None or spec.measure is not draft.exercise.measure:
        return prescribed
    if draft.exercise.measure is Measure.REPS and prescribed.reps is not None:
        return replace(prescribed, reps=target)
    if draft.exercise.measure is Measure.SECONDS and prescribed.seconds is not None:
        return replace(prescribed, seconds=target)
    return prescribed


def progression_state(draft: Draft, week_one: Prescription, cap_kg: float | None, ctx: BuildContext) -> dict[str, Any]:
    """The per-row mutable progression state PRP-07 rewrites (D-057)."""
    progression = ctx.library.progression(draft.exercise.default_progression)
    if progression is None:
        return {}
    update: dict[str, Any] = {"cap_load_kg": cap_kg}
    if ctx.is_youth:
        # Section 5.6: load never rises by any path for a youth profile.
        update["allow_load_progression"] = False
    if week_one.reps is not None:
        update["rep_min"] = week_one.reps
        update["rep_max"] = min(week_one.reps + REP_WINDOW, 100)
    return progression.model_copy(update=update).model_dump(mode="json")


def estimated_minutes(rows: list[dict]) -> int:
    """Section 6.4's row-time model, summed and rounded up. ``per_side`` doubles a row.

    This is the figure that goes on the ``Workout`` as ``estimated_minutes``, so it is what the
    session *declares*. It is not the only clock the session is judged by - see ``band_minutes``.
    """
    total = 0.0
    for row in rows:
        if row.get("reps") is not None:
            work = row["reps"] * SECONDS_PER_REP
        elif row.get("seconds") is not None:
            work = float(row["seconds"])
        elif row.get("meters") is not None:
            work = row["meters"] * SECONDS_PER_METRE
        else:
            work = (row.get("steps") or 0) * METRES_PER_STEP * SECONDS_PER_METRE
        seconds = row["sets"] * (work + row["rest_s"])
        total += seconds * 2 if row.get("per_side") else seconds
    return max(1, min(120, math.ceil(total / 60)))


def validator_minutes(rows: list[dict], ctx: BuildContext) -> int:
    """The same rows through V9's own clock, borrowed rather than reimplemented.

    ``row_seconds`` belongs to PRP-00 and is what the gate actually measures a youth session by:
    ``sets x est_seconds_per_set + rest x (sets - 1)``, where section 6.4 counts three seconds a
    rep and charges rest on every set. Two clocks that disagree is how a session gets trimmed to
    fit one number and then rejected against the other, so the engine imports the gate's rather
    than keeping a second copy of it.
    """
    total = sum(row_seconds(_as_row(row), ctx.library.exercises.get(row["exercise_id"])) for row in rows)
    return math.ceil(total / 60)


def band_minutes(rows: list[dict], ctx: BuildContext, *, assessment: bool) -> int:
    """The length V9 will judge these rows by, so the trim can never disagree with the gate.

    V9 takes the larger of the declared figure and the one computed from the rows, except on
    assessment day where the declared figure legitimately covers the battery and only the computed
    length counts. Mirrored here exactly; claiming a number V9 would not use is the whole bug.
    """
    derived = validator_minutes(rows, ctx)
    return derived if assessment else max(estimated_minutes(rows), derived)


def _as_row(row: dict) -> WorkoutRow:
    """The row as PRP-00's model, which is what ``row_seconds`` reads."""
    return WorkoutRow.model_validate(
        {
            "exercise_id": row["exercise_id"],
            "sets": row["sets"],
            "reps": row["reps"],
            "seconds": row["seconds"],
            "meters": row["meters"],
            "steps": row["steps"],
            "load_kg": row["load_kg"],
            "load_unit": row["load_unit"],
            "rest_s": row["rest_s"],
            "rpe_target": row["rpe_target"],
            "amrap": row["amrap"],
            "is_prelude": row["is_prelude"],
            "is_challenge": row["is_challenge"],
        }
    )
