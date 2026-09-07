"""Putting a stored workout on the plan: "use this for my Upper A days".

The smallest thing that makes an imported or generated workout reachable from Today. It writes
``workout_id`` and ``rows_json`` straight onto the matching planned sessions rather than going
through ``rebuild_one``: the rebuild path re-plans from the on-disk library bundle, which by
D-010 will never contain a database-only workout, so a rebuild would quietly undo the swap
(D-173).

Two rules borrowed from the rebuild so this cannot do more damage than a settings change:

- only a session that is still ``planned`` and that nobody has started is touched (D-093);
- ``program.start_date`` and the block's shape are not touched at all.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping

from sqlalchemy import delete, exists, or_, update
from sqlmodel import Session, select

from cadence.programme.tables import PLANNED, PlannedSession
from cadence.schema.enums import DayType
from cadence.schema.exercise import Exercise
from cadence.schema.workout import Workout, WorkoutRow
from cadence.seance.tables import SessionRecord, SessionRowRecord

logger = logging.getLogger(__name__)

# The day types a person may point a workout at. `assessment` is not among them: an assessment day
# is a measurement protocol, not a workout, and swapping it out would silently drop the retest.
ADOPTABLE_DAY_TYPES: tuple[DayType, ...] = (
    DayType.UPPER_A,
    DayType.UPPER_B,
    DayType.LOWER_A,
    DayType.LOWER_FULL_B,
    DayType.MOBILITY_CARRY,
)


def day_type_choices() -> tuple[tuple[str, str], ...]:
    """The select options behind "Use for", as ``(value, label)``."""
    from cadence.schema.labels import day_label

    return tuple((item.value, day_label(item.value)) for item in ADOPTABLE_DAY_TYPES)


def row_spec(position: int, row: WorkoutRow, exercise: Exercise) -> dict[str, object]:
    """One ``rows_json`` entry, shaped exactly as ``materialise._row_dict`` shapes it.

    Today reads the name, the cue and the measure off the spec rather than off the exercise
    table, so a bare ``WorkoutRow`` dump renders a checklist of blank lines. ``progression`` is
    ``None`` on purpose: ``seance.ticks.load_cap`` computes the band cap live from the rule
    table, so an absent stored cap narrows nothing.
    """
    return {
        "position": position,
        "exercise_id": exercise.id,
        "role": "prelude" if row.is_prelude else "main",
        "name": exercise.name,
        "cue": (row.cue_override or exercise.cue)[:120],
        "sets": row.sets,
        "reps": row.reps,
        "seconds": row.seconds,
        "meters": row.meters,
        "steps": row.steps,
        "per_side": False,
        "rest_s": row.rest_s,
        "rpe_target": row.rpe_target,
        "amrap": row.amrap,
        "load_kg": row.load_kg,
        "load_unit": row.load_unit.value,
        "measure": exercise.measure.value,
        "garmin_category": exercise.garmin_category.value if exercise.garmin_category else None,
        "is_prelude": row.is_prelude,
        "is_challenge": row.is_challenge,
        "assessment_id": None,
        "notes": [],
        "progression": None,
    }


def rows_for(workout: Workout, catalog: Mapping[str, Exercise]) -> list[dict[str, object]]:
    """The whole workout as materialised rows. Every row resolved, or nothing at all."""
    specs: list[dict[str, object]] = []
    for position, row in enumerate(workout.rows, start=1):
        exercise = catalog.get(row.exercise_id)
        if exercise is None:
            raise KeyError(row.exercise_id)
        specs.append(row_spec(position, row, exercise))
    return specs


def _eligible_ids(profile_id: str, day_type: str):
    """The planned days this swap may touch, as a *subquery* rather than as a list of ids.

    A subquery and not a snapshot: it is re-evaluated by the database inside the write
    transaction, so a session that is started between the read and the commit is excluded by the
    same statement that would have overwritten it. The first cut read the sessions once at the top
    of the function and trusted that list three statements later, which is the race Codex found.

    "Eligible" is D-093's rule expressed in SQL: still ``planned``, and carrying no session anyone
    has started or finished. ``started_at`` lives on ``session``, not on ``planned_session``, so
    the condition is a correlated ``NOT EXISTS`` rather than a column test.
    """
    touched = (
        select(SessionRecord.id)
        .where(SessionRecord.planned_session_id == PlannedSession.id)
        .where(or_(SessionRecord.started_at.is_not(None), SessionRecord.finished_at.is_not(None)))  # type: ignore[union-attr]
    )
    return (
        select(PlannedSession.id)
        .where(
            PlannedSession.profile_id == profile_id,
            PlannedSession.status == PLANNED,
            PlannedSession.day_type == day_type,
            ~exists(touched),
        )
        .scalar_subquery()
    )


def adopt(
    session: Session,
    workout: Workout,
    *,
    profile_id: str,
    day_type: str,
    catalog: Mapping[str, Exercise],
) -> int:
    """Swap this workout into every untouched planned session of that day type. Returns the count.

    Three statements in one transaction, each guarded by the same subquery, so the set of days
    being changed is decided by the database at write time and not by a snapshot taken earlier
    (D-185). Deleting the open-but-untouched session is what makes the swap visible: D-024 creates
    it on the first *render* of Today, and its ``session_row`` entries are copies of the rows this
    swap is replacing.

    Raises ``ValueError`` for a day type nobody may point a workout at, and ``KeyError`` for a row
    whose exercise is not in the catalog - which cannot happen for a document that has just passed
    the validator, and is a refusal rather than a half-written plan if it ever does.
    """
    if day_type not in {item.value for item in ADOPTABLE_DAY_TYPES}:
        raise ValueError(f"{day_type!r} is not a day a workout can be pointed at")
    # Built before the transaction opens: an unresolvable row must raise without having written.
    payload = json.dumps(rows_for(workout, catalog))

    eligible = _eligible_ids(profile_id, day_type)
    doomed = select(SessionRecord.id).where(SessionRecord.planned_session_id.in_(eligible))  # type: ignore[attr-defined]
    session.execute(
        delete(SessionRowRecord).where(SessionRowRecord.session_id.in_(doomed))  # type: ignore[attr-defined]
    )
    session.execute(delete(SessionRecord).where(SessionRecord.planned_session_id.in_(eligible)))  # type: ignore[attr-defined]
    result = session.execute(
        update(PlannedSession)
        .where(PlannedSession.id.in_(_eligible_ids(profile_id, day_type)))  # type: ignore[attr-defined]
        .values(workout_id=workout.id, rows_json=payload)
    )
    swapped = int(result.rowcount or 0)
    session.commit()
    # The statements above went round the identity map, so anything this request already loaded
    # still holds the old rows. Today is rendered from those objects in the very next call.
    session.expire_all()
    logger.info("workout %r now serves %d %s session(s) for %s", workout.id, swapped, day_type, profile_id)
    return swapped


__all__ = ["ADOPTABLE_DAY_TYPES", "adopt", "day_type_choices", "row_spec", "rows_for"]
