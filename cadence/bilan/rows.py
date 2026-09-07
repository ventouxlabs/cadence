"""Building the extra row a challenge inserts, and putting it into the planned sessions.

Principles section 7.7 maps each gap to one row; P5 bounds where it may sit: a challenge row may
take a session one row over the section 6.4 count but never over ``max_exercises_per_session``,
and it is the first row dropped when a youth session will not fit. Both of those already live in
``cadence.programme.selection.fit_to_band``, so this appends and then hands the whole session to
that function rather than keeping a second copy of the rule.

The rows are appended to the ``rows_json`` that is already stored, never re-materialised: a
mid-block session has autoregulation on it, and rebuilding it from the template would throw that
away (D-214).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlmodel import Session, select

from cadence.bibliotheque.assessments import GapRow
from cadence.programme import substitution as sub
from cadence.programme.context import BuildContext
from cadence.programme.selection import fit_to_band
from cadence.programme.tables import PLANNED, PlannedSession
from cadence.schema.enums import DayType, Measure
from cadence.seance.tables import SessionRecord

logger = logging.getLogger(__name__)

DEFAULT_REST_S = 60
ASSESSMENT_DAY = "assessment"
MIN_REPS = 1
MIN_SECONDS = 5

#: Marks the one extra set a ``body_comp`` challenge adds, so it can be added once and taken back
#: off again. Without it the increment is invisible to ``clear_challenge_rows`` and compounds on
#: every save (D-222).
CARRY_SET_MARK = "challenge_carry_set"


def build_row(spec: GapRow, best: float | None, ctx: BuildContext) -> dict[str, Any] | None:
    """The section 7.7 row for one gap, in the shape ``materialise_rows`` produces.

    ``best`` is the person's own best recorded measurement for the test: section 7.7 sizes the
    inserted row off it (``3 x (best_hold_s - 10 s)``), so a challenge is always something the
    body in question has already done rather than a number from a table.
    """
    if spec.exercise is None:
        return None
    exercise = ctx.library.exercises.get(spec.exercise)
    if exercise is None:
        logger.warning("challenge row names %r, which is not in the library", spec.exercise)
        return None
    if not sub.is_offerable(exercise, ctx.band):
        # P1: a challenge never buys its way past a band rule. Section 7.7 names one movement per
        # gap and those are chosen for adults, so a youth profile whose band bans the load type or
        # the tag simply gets no row - the challenge still stands, it just is not woven in.
        logger.info("challenge row %r is not offerable to profile %r; no row inserted", exercise.id, ctx.profile.id)
        return None

    reps = seconds = meters = steps = None
    if spec.reps is not None:
        reps = spec.reps
    elif spec.reps_pct is not None and best is not None:
        reps = max(MIN_REPS, round(best * spec.reps_pct))
    elif spec.seconds is not None:
        seconds = spec.seconds
    elif spec.seconds_offset is not None and best is not None:
        seconds = max(MIN_SECONDS, int(best) + spec.seconds_offset)
    if reps is None and seconds is None:
        # No prescription can be sized without a measurement to size it from.
        return None
    if exercise.measure is Measure.METERS:
        meters, seconds = float(seconds or 0), None
    elif exercise.measure is Measure.STEPS:
        steps, seconds = int(seconds or 0), None

    rules = ctx.rules
    rest = max(DEFAULT_REST_S, rules.min_rest_s_loaded) if rules is not None else DEFAULT_REST_S
    sets = min(spec.sets or 3, rules.max_sets_per_exercise) if rules is not None else (spec.sets or 3)
    if reps is not None and rules is not None:
        reps = min(reps, rules.rep_max_bodyweight)
    return {
        "position": 0,
        "exercise_id": exercise.id,
        "role": "finisher",
        "name": exercise.name,
        "cue": exercise.cue[:120],
        "sets": sets,
        "reps": reps,
        "seconds": seconds,
        "meters": meters,
        "steps": steps,
        "per_side": False,
        "rest_s": rest,
        "rpe_target": None,
        "amrap": False,
        # A challenge never adds load: section 7.7's rows are bodyweight tests, and P1 would
        # clamp anything else to the band cap anyway.
        "load_kg": None,
        "load_unit": exercise.load_unit.value,
        "measure": exercise.measure.value,
        "garmin_category": exercise.garmin_category.value if exercise.garmin_category else None,
        "is_prelude": bool(spec.append_to_prelude),
        "is_challenge": True,
        "assessment_id": None,
        "notes": [],
        # Section 5.6 keys the youth bump order off this flag, and an empty progression would
        # send a child's challenge row down the adult path. It holds today only because the row
        # carries no load; saying so outright is what makes risk 2 structural (D-218).
        "progression": {"allow_load_progression": not ctx.is_youth},
    }


def _renumber(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{**row, "position": index} for index, row in enumerate(rows, start=1)]


def _conflicts(item: dict[str, Any], exercise_id: str, is_prelude: bool) -> bool:
    """Whether a stored row and an incoming challenge row compete for the same slot.

    The same movement inserted twice, or a second body row where P5 allows exactly one. A prelude
    appendix and a body row do not compete for a slot and never evict each other (D-215).

    ``insert_into`` evicts what conflicts and ``target_sessions`` refuses a session that already
    holds a conflict. They have to agree: one notion of "conflicting" that differed between
    choosing a session and filling it is precisely how D-215 happened.
    """
    if not item.get("is_challenge"):
        return False
    if str(item.get("exercise_id")) == exercise_id:
        return True
    return not is_prelude and not item.get("is_prelude")


def insert_into(
    rows: list[dict[str, Any]], row: dict[str, Any], ctx: BuildContext, *, assessment: bool
) -> list[dict[str, Any]]:
    """One challenge row into one session's rows, then the band caps over the result.

    A prelude appendix (the wall-angel challenge, section 2) goes after the last prelude row so
    the warm-up grows to its 380-second allowance; everything else is the **last** row of the
    session, which is also the first one ``fit_to_band`` drops when a youth session runs long.

    Only a challenge row this one would *conflict* with is removed first: the same movement
    inserted twice, or a second body row where P5 allows exactly one. A prelude appendix and a
    body row do not conflict, and clearing every challenge row indiscriminately meant whichever
    challenge was woven last erased the others from the whole queue (D-215).
    """
    is_prelude = bool(row.get("is_prelude"))
    exercise_id = str(row.get("exercise_id"))
    cleaned = [item for item in rows if not _conflicts(item, exercise_id, is_prelude)]
    if is_prelude:
        tail = max((index for index, item in enumerate(cleaned) if item.get("is_prelude")), default=-1)
        merged = [*cleaned[: tail + 1], row, *cleaned[tail + 1 :]]
    else:
        merged = [*cleaned, row]
    return fit_to_band(_renumber(merged), ctx, assessment=assessment)


def _started(db: Session, planned_id: str) -> bool:
    statement = select(SessionRecord).where(
        SessionRecord.planned_session_id == planned_id,
        SessionRecord.started_at.is_not(None),  # type: ignore[union-attr]
    )
    return db.exec(statement).first() is not None


def target_sessions(
    db: Session, profile_id: str, spec: GapRow, limit: int, *, row: dict[str, Any] | None = None
) -> list[PlannedSession]:
    """The untouched planned sessions this challenge's row belongs in, in queue order.

    ``frequency`` is "sessions per week" (section 7.6); the queue is not calendar-bound (D-011),
    so it is read here as the number of upcoming sessions of the matching day types that carry the
    row, which is the same count over one week of a four-day plan.

    A session already holding a row this one would evict is **skipped**, not overwritten (D-221).
    Two challenges can share a day-type pair - the library gives ``dead_hang_s`` and
    ``push_up_max`` both ``[upper_a, upper_b]`` - and taking the earliest matching sessions blind
    meant the second challenge evicted the first from the very sessions it had just been put in,
    leaving one challenge in no session at all while the screen still listed it as active. A
    four-week block has four sessions per day type, so three challenges at frequency 2 fit.
    """
    wanted = {day.value for day in spec.day_types}
    exercise_id = str(row.get("exercise_id")) if row is not None else None
    is_prelude = bool(row.get("is_prelude")) if row is not None else False
    statement = (
        select(PlannedSession)
        .where(PlannedSession.profile_id == profile_id, PlannedSession.status == PLANNED)
        .order_by(PlannedSession.week, PlannedSession.day_index)
    )
    found: list[PlannedSession] = []
    for planned in db.exec(statement).all():
        if planned.day_type not in wanted or _started(db, planned.id):
            continue
        # Section 7.7: the wall-angel appendix is suppressed on assessment day so the test it
        # trains is not pre-fatigued by the warm-up that precedes it.
        if planned.day_type == ASSESSMENT_DAY and spec.append_to_prelude:
            continue
        if exercise_id is not None and any(_conflicts(item, exercise_id, is_prelude) for item in parse_rows(planned)):
            continue
        found.append(planned)
        if len(found) >= limit:
            break
    return found


def parse_rows(planned: PlannedSession) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(planned.rows_json)
    except (TypeError, ValueError):
        return []
    return sorted(parsed, key=lambda row: int(row.get("position", 0))) if isinstance(parsed, list) else []


def _drop_carry_set(row: dict[str, Any]) -> dict[str, Any]:
    """Take back the one set ``add_carry_set`` put on, and the mark that says it did."""
    stripped = {key: value for key, value in row.items() if key != CARRY_SET_MARK}
    stripped["sets"] = max(1, int(row.get("sets") or 1) - 1)
    return stripped


def clear_challenge_rows(db: Session, profile_id: str, ctx: BuildContext) -> None:
    """Take every challenge row out of the queue, before a fresh set is derived.

    The ``body_comp`` challenge inserts no row - it adds a set to a carry the session already
    prescribes - so it is undone here by its mark rather than by being filtered out. Leaving it
    behind meant the increment survived the clear and the next save stacked another on top, so
    N saves of the same battery left the carry at N extra sets (D-222).
    """
    statement = select(PlannedSession).where(PlannedSession.profile_id == profile_id, PlannedSession.status == PLANNED)
    for planned in db.exec(statement).all():
        rows = parse_rows(planned)
        if _started(db, planned.id):
            continue
        if not any(row.get("is_challenge") or row.get(CARRY_SET_MARK) for row in rows):
            continue
        kept = _renumber(
            [_drop_carry_set(row) if row.get(CARRY_SET_MARK) else row for row in rows if not row.get("is_challenge")]
        )
        planned.rows_json = json.dumps(kept, sort_keys=True, separators=(",", ":"))
        db.add(planned)


def apply_row(db: Session, profile_id: str, row: dict[str, Any], spec: GapRow, ctx: BuildContext, profile: Any) -> int:
    """Put one challenge row into up to ``frequency`` sessions. Returns how many took it."""
    from cadence.programme.autoregulation import rows_pass_validator

    placed = 0
    for planned in target_sessions(db, profile_id, spec, spec.frequency, row=row):
        assessment = planned.day_type == ASSESSMENT_DAY
        candidate = insert_into(parse_rows(planned), dict(row), ctx, assessment=assessment)
        if not candidate or not any(item.get("is_challenge") for item in candidate):
            continue
        if not rows_pass_validator(candidate, planned, profile, ctx):
            continue
        planned.rows_json = json.dumps(candidate, sort_keys=True, separators=(",", ":"))
        db.add(planned)
        placed += 1
    return placed


def carry_day_types() -> tuple[str, ...]:
    """Where a ``body_comp`` gap adds its extra carry set (section 7.7)."""
    return (DayType.LOWER_FULL_B.value,)


__all__ = [
    "add_carry_set",
    "apply_row",
    "build_row",
    "carry_day_types",
    "clear_challenge_rows",
    "insert_into",
    "parse_rows",
    "target_sessions",
]


def add_carry_set(db: Session, profile_id: str, ctx: BuildContext, profile: Any) -> int:
    """The ``body_comp`` gap's remedy (section 7.7): one more carry set on ``lower_full_b``.

    No row is inserted - a carry the session already prescribes simply runs one more time - so
    this cannot push a session over the row count and P5 has nothing to say about it.
    """
    from cadence.programme.autoregulation import rows_pass_validator
    from cadence.schema.enums import Pattern

    wanted = set(carry_day_types())
    statement = (
        select(PlannedSession)
        .where(PlannedSession.profile_id == profile_id, PlannedSession.status == PLANNED)
        .order_by(PlannedSession.week, PlannedSession.day_index)
    )
    changed = 0
    for planned in db.exec(statement).all():
        if planned.day_type not in wanted or _started(db, planned.id):
            continue
        rows = parse_rows(planned)
        if any(row.get(CARRY_SET_MARK) for row in rows):
            # This session already carries the extra set from an earlier save. Re-posting the
            # same battery is explicitly permitted, so a second increment would be a bug: the
            # remedy is "one more carry set", not "one more per time you pressed save" (D-222).
            continue
        updated: list[dict[str, Any]] = []
        touched = False
        for row in rows:
            exercise = ctx.library.exercises.get(str(row.get("exercise_id") or ""))
            is_carry = exercise is not None and exercise.pattern is Pattern.CARRY
            if is_carry and not touched:
                updated.append({**row, "sets": int(row.get("sets") or 1) + 1, CARRY_SET_MARK: True})
                touched = True
            else:
                updated.append(dict(row))
        if not touched or not rows_pass_validator(updated, planned, profile, ctx):
            continue
        planned.rows_json = json.dumps(updated, sort_keys=True, separators=(",", ":"))
        db.add(planned)
        changed += 1
    return changed
