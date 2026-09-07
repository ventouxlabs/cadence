"""Autoregulation: what the finished session does to the next one of the same day type.

The public surface of principles section 5. ``decide`` (the section 5.4 table) and
``apply_outcome`` (the section 5.5 arithmetic) are pure and live next door; this module is the
part that reads a database, rewrites one ``planned_session.rows_json`` immutably, and puts the
result through ``validate_workout`` before storing it. The validator is the gate: rows that would
not pass are dropped and the previous prescription stands.

``after_done`` is the single entry point PRP-02's Done service calls. It never raises: a failed
autoregulation logs and leaves the plan exactly as it was, because nothing here is worth blocking
the redirect off the Done screen for.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any, Literal

from sqlmodel import Session, select

from cadence.bibliotheque.bundle import LibraryBundle
from cadence.historique.clock import parse_utc
from cadence.profils.settings import ProgramSettings
from cadence.profils.tables import Profile
from cadence.programme import substitution as sub
from cadence.programme.arithmetic import Resolver, apply_outcome, consume_pending_bump
from cadence.programme.context import BuildContext, make_context
from cadence.programme.decision import AutoregInput, Decision, Outcome, decide
from cadence.programme.materialise import workout_from_rows
from cadence.programme.notes import HOLD_NOTE, best_note
from cadence.programme.signals import (
    RESTART_NOTE,
    lighten_row,
    missed_sessions_7d,
    needs_block_restart,
    readiness_for,
)
from cadence.programme.tables import PLANNED, PlannedSession
from cadence.seance.catalog import display_unit, library_bundle, load_settings
from cadence.seance.status import completion
from cadence.seance.tables import SessionRecord, SessionRowRecord
from cadence.validateur import validate_workout

logger = logging.getLogger(__name__)

ASSESSMENT_DAY = "assessment"
FIRST_WEEK = 1


# ------------------------------------------------------------------------------------ the inputs


def _session_rows(db: Session, session_id: str) -> list[SessionRowRecord]:
    statement = (
        select(SessionRowRecord).where(SessionRowRecord.session_id == session_id).order_by(SessionRowRecord.position)  # type: ignore[arg-type]
    )
    return list(db.exec(statement).all())


def gather_input(
    db: Session,
    record: SessionRecord,
    profile: Profile,
    target_week: int,
    settings: ProgramSettings,
    library: LibraryBundle | None,
) -> AutoregInput:
    """Everything section 5.3 lists, read once.

    ``week_of_block`` is the week of the session being **written to**, not of the one that just
    finished: R1 asks whether the prescription about to be handed over is a deload week, and the
    stored ``planned_session.week`` is the number ``materialise_rows`` already built those rows at
    (D-213).
    """
    rows = _session_rows(db, record.id)
    band_rules = None
    if library is not None and profile.kind == "youth":
        from cadence.programme.bands import age_band

        band_rules = library.youth_rules.get(age_band(profile))
    ticked = bool(rows) and all(row.done for row in rows)
    return AutoregInput(
        all_rows_ticked=ticked,
        felt=record.felt,  # type: ignore[arg-type]
        readiness=readiness_for(db, profile.id),
        missed_sessions_7d=missed_sessions_7d(db, profile.id, settings.days_per_week, parse_utc(record.finished_at)),
        week_of_block=target_week,
        completion=completion(rows, band_rules),
    )


# ----------------------------------------------------------------------------------- the target


def _has_work(db: Session, planned_id: str) -> bool:
    """Whether somebody has already started ticking this planned session.

    D-024 creates the session on the first *render*, so "a session exists" is the normal state of
    a checklist that has only been looked at. ``started_at`` is what moves on the first tick, and
    a checklist with a tick on it is never rewritten underneath the person doing it (risk 4).
    """
    statement = select(SessionRecord).where(
        SessionRecord.planned_session_id == planned_id,
        SessionRecord.started_at.is_not(None),  # type: ignore[union-attr]
    )
    return db.exec(statement).first() is not None


def next_of_day_type(db: Session, profile_id: str, finished: PlannedSession) -> PlannedSession | None:
    """The next untouched ``planned`` session of the same day type, in queue order."""
    statement = (
        select(PlannedSession)
        .where(
            PlannedSession.profile_id == profile_id,
            PlannedSession.status == PLANNED,
            PlannedSession.day_type == finished.day_type,
        )
        .order_by(PlannedSession.week, PlannedSession.day_index)
    )
    for candidate in db.exec(statement).all():
        if candidate.id == finished.id or (candidate.week, candidate.day_index) <= (finished.week, finished.day_index):
            continue
        if not _has_work(db, candidate.id):
            return candidate
    return None


# ---------------------------------------------------------------------------------- the rewrite


def _resolver(ctx: BuildContext) -> Resolver:
    """Exercise ids to movements this profile may legally be handed, and nothing else.

    A swap that ``sub.is_offerable`` refuses simply does not happen, so section 5.6's step 3 can
    never walk a youth profile onto a movement his band bans.
    """

    def resolve(exercise_id: str) -> Any:
        exercise = ctx.library.exercises.get(exercise_id)
        if exercise is None or not sub.is_offerable(exercise, ctx.band):
            return None
        return exercise

    return resolve


def _pair_rows(source: list[dict], target: list[dict]) -> dict[int, dict]:
    """Match target rows to the rows just performed, by ``exercise_id`` then by ``position``.

    Keyed by the target row's position. A target row with no counterpart is absent from the map
    and is left exactly as it was.
    """
    by_exercise: dict[str, dict] = {}
    for row in source:
        by_exercise.setdefault(str(row.get("exercise_id")), row)
    by_position = {int(row.get("position", 0)): row for row in source}
    paired: dict[int, dict] = {}
    for row in target:
        match = by_exercise.get(str(row.get("exercise_id"))) or by_position.get(int(row.get("position", 0)))
        if match is not None:
            paired[int(row.get("position", 0))] = match
    return paired


def rewrite_rows(
    source: list[dict],
    target: list[dict],
    decision: Decision,
    ctx: BuildContext,
    *,
    week: int,
) -> tuple[list[dict], list[tuple[dict, dict]]]:
    """The target's new rows, and the (before, after) pairs of the rows that moved."""
    resolve = _resolver(ctx)
    paired = _pair_rows(source, target)
    rows: list[dict] = []
    moved: list[tuple[dict, dict]] = []
    for row in target:
        position = int(row.get("position", 0))
        performed = paired.get(position)
        if performed is None:
            rows.append(dict(row))
            continue
        ladder = _ladder_for(row, ctx)
        seed = dict(row)
        if week == FIRST_WEEK and performed.get("pending_bump"):
            seed = consume_pending_bump({**seed, "pending_bump": True}, ctx.rules, ladder, resolve=resolve)
        new_row = apply_outcome(
            seed,
            decision.outcome,
            ctx.rules,
            ladder,
            resolve=resolve,
            earned_bump=decision.earned_bump,
            # These rows came out of ``materialise_rows`` at ``week``, so section 6.2 has already
            # applied the week-4 shape to them (D-218).
            pre_scheduled=True,
        )
        rows.append(new_row)
        moved.append((row, new_row))
    return rows, moved


def _ladder_for(row: dict, ctx: BuildContext) -> list[float]:
    exercise = ctx.library.exercises.get(str(row.get("exercise_id") or ""))
    return list(ctx.weights.ladder(exercise.load_type)) if exercise is not None else []


def rows_pass_validator(rows: list[dict], planned: PlannedSession, profile: Profile, ctx: BuildContext) -> bool:
    """The gate. Rows that would not validate for this profile are never stored."""
    template = ctx.library.templates.get(planned.workout_id)
    if template is None:
        logger.warning("no template %r for planned session %r; leaving its rows alone", planned.workout_id, planned.id)
        return False
    kind: Literal["adult", "youth"] = "youth" if ctx.is_youth else "adult"
    result = validate_workout(
        workout_from_rows(template, rows, is_youth=ctx.is_youth),
        profile_kind=kind,
        age_band=ctx.band.band,
        equipment=list(ctx.settings.equipment),
        bodyweight_kg=profile.bodyweight_kg,
        has_overhead_anchor=profile.has_overhead_anchor,
        exercises=ctx.library.exercises,
        youth_rules=ctx.library.youth_rules,
    )
    if not result.ok:
        logger.warning(
            "autoregulated rows for %r would not validate (%s); the previous prescription stands",
            planned.id,
            ", ".join(f"{error.code}@{error.path}" for error in result.errors if error.severity == "error")[:300],
        )
    return result.ok


# --------------------------------------------------------------------------------- block restart


def restart_block(db: Session, profile: Profile, ctx: BuildContext) -> str:
    """Section 5.7: after a fortnight away, start again at week 1 with every load × 0.90.

    Renumbers the remaining queue from week 1 rather than editing the four-week span, so
    ``(week, day_index)`` ordering and the block invariant D-110 asserts both survive.
    """
    statement = (
        select(PlannedSession)
        .where(PlannedSession.profile_id == profile.id, PlannedSession.status == PLANNED)
        .order_by(PlannedSession.week, PlannedSession.day_index)
    )
    remaining = [row for row in db.exec(statement).all() if not _has_work(db, row.id)]
    per_week = max(ctx.settings.days_per_week, 1)
    for index, planned in enumerate(remaining):
        rows = [lighten_row(row, _ladder_for(row, ctx)) for row in _parse_rows(planned)]
        if rows_pass_validator(rows, planned, profile, ctx):
            planned.rows_json = json.dumps(rows, sort_keys=True, separators=(",", ":"))
        planned.week = min(index // per_week + 1, 4)
        planned.day_index = index % per_week
        db.add(planned)
    return RESTART_NOTE


# ------------------------------------------------------------------------------------- the hook


def _parse_rows(planned: PlannedSession) -> list[dict]:
    try:
        parsed = json.loads(planned.rows_json)
    except (TypeError, ValueError):
        return []
    return sorted(parsed, key=lambda row: int(row.get("position", 0))) if isinstance(parsed, list) else []


def autoregulate_next(db: Session, record: SessionRecord, ctx: BuildContext, profile: Profile) -> tuple[str, Outcome]:
    """Rewrite the next session of this day type, and return the line to show for it."""
    finished = db.get(PlannedSession, record.planned_session_id)
    if finished is None:
        return HOLD_NOTE, "hold"
    target = next_of_day_type(db, profile.id, finished)
    if target is None:
        return HOLD_NOTE, "hold"

    decision = decide(gather_input(db, record, profile, target.week, ctx.settings, ctx.library))
    rows, moved = rewrite_rows(_parse_rows(finished), _parse_rows(target), decision, ctx, week=target.week)
    logger.info("autoregulation for %s: %s fired %s over %s", profile.id, decision.rule_id, decision.outcome, target.id)
    if not rows or not rows_pass_validator(rows, target, profile, ctx):
        return HOLD_NOTE, "hold"
    target.rows_json = json.dumps(rows, sort_keys=True, separators=(",", ":"))
    db.add(target)
    note = best_note(moved, decision.outcome, display_unit(ctx.settings), is_youth=ctx.is_youth)
    return note, decision.outcome


def after_done(db: Session, record: SessionRecord, *, commit: bool = True) -> str | None:
    """PRP-02's Done service calls this once, during finalisation. It never raises.

    The assessment-day guard is here rather than inside ``decide``: section 10.10 exempts the
    whole session, and a rule buried in the decision table would be missed by any future caller
    that reached the arithmetic another way.

    ``commit=False`` runs inside the caller's transaction, which is how ``finish`` uses it: the
    "next time" line has to be on the session before PRP-06 prices the write-back payload from it
    (D-129), and that payload is built before the commit. The work is wrapped in a **savepoint**
    either way, so a failure here rolls back only the plan rewrite and never the finished session
    that PRP-06 queues in the same transaction (D-219).
    """
    savepoint = db.begin_nested()
    try:
        note = _after_done(db, record)
        savepoint.commit()
    except Exception:  # noqa: BLE001 - Done must not fail because the plan could not be nudged
        logger.exception("autoregulation after session %s failed; the plan is unchanged", record.id)
        savepoint.rollback()
        return None
    if commit:
        db.commit()
    return note


def _after_done(db: Session, record: SessionRecord) -> str | None:
    profile = db.get(Profile, record.profile_id)
    planned = db.get(PlannedSession, record.planned_session_id)
    if profile is None or planned is None or planned.day_type == ASSESSMENT_DAY:
        return None
    library = library_bundle()
    if library is None:
        logger.warning("the library will not load; session %s changes nothing in the plan", record.id)
        return None

    settings = load_settings(db)
    ctx = make_context(profile, settings, library)
    finished_at = parse_utc(record.finished_at) or datetime.now(UTC)

    if needs_block_restart(db, profile.id, finished_at):
        note = restart_block(db, profile, ctx)
    else:
        note, _ = autoregulate_next(db, record, ctx, profile)

    record.notes = note
    db.add(record)
    db.flush()
    return note


__all__ = [
    "AutoregInput",
    "Decision",
    "after_done",
    "autoregulate_next",
    "decide",
    "gather_input",
    "next_of_day_type",
    "restart_block",
    "rewrite_rows",
    "rows_pass_validator",
]
