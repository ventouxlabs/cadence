"""One battery in, gaps and challenges out - the orchestration behind both assessment routes.

Order matters: record, close what the retest met, expire what ran out of time, rank what is still
short, then derive at most three challenges and weave their rows into the queue. Closing before
detecting is what stops a test the person has just passed from generating a challenge to pass it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from sqlmodel import Session, select

from cadence.bibliotheque.bundle import LibraryBundle
from cadence.bilan import assessments as bilan
from cadence.bilan import challenges as chal
from cadence.bilan import gaps as gap_rules
from cadence.bilan import rows as challenge_rows
from cadence.bilan.gaps import BODY_COMP, Gap
from cadence.bilan.rows import ASSESSMENT_DAY
from cadence.bilan.tables import UNIT_UNAVAILABLE, Assessment, Challenge
from cadence.profils.settings import ProgramSettings
from cadence.profils.tables import Profile
from cadence.programme.bands import age_band
from cadence.programme.context import make_context
from cadence.programme.tables import DONE as PLANNED_DONE
from cadence.programme.tables import PLANNED, PlannedSession
from cadence.schema.enums import AssessmentId
from cadence.seance.tables import SessionRecord, SessionRowRecord

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class BilanState:
    """What both the JSON API and the HTML page render."""

    profile: Profile
    latest: list[Assessment]
    gaps: list[Gap]
    challenges: list[Challenge]
    baseline_on: date | None = None
    next_due_on: date | None = None
    capped: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self, library: LibraryBundle) -> dict[str, Any]:
        is_youth = self.profile.kind == "youth"
        return {
            "latest": [_result_dict(row, library, is_youth=is_youth) for row in self.latest],
            "gaps": [item.as_dict() for item in self.gaps],
            "challenges": [chal.as_dict(item) for item in self.challenges],
        }

    def meta(self) -> dict[str, Any]:
        return {
            "baseline_on": self.baseline_on.isoformat() if self.baseline_on else None,
            "next_due_on": self.next_due_on.isoformat() if self.next_due_on else None,
            "capped": list(self.capped),
        }


def _result_dict(row: Assessment, library: LibraryBundle, *, is_youth: bool) -> dict[str, Any]:
    """One stored result. ``tier`` is adult vocabulary and is never sent for a youth profile."""
    tier = None if is_youth else bilan.tier_of(library.assessments, row.test_id, row.value)
    return {
        "test_id": row.test_id,
        "value": row.value,
        "unit": row.unit,
        "recorded_on": row.recorded_on,
        "self_rated": row.self_rated,
        "tier": tier,
    }


def state_for(db: Session, profile: Profile, library: LibraryBundle, today: date) -> BilanState:
    """The current picture without changing anything: latest battery, gaps, open challenges."""
    latest = bilan.latest_batch(db, profile.id)
    is_youth = profile.kind == "youth"
    found = gap_rules.detect(
        db,
        profile.id,
        latest,
        library.assessments,
        age_band(profile),
        is_youth=is_youth,
        today=today,
    )
    return BilanState(
        profile=profile,
        latest=latest,
        gaps=found,
        challenges=chal.active_for(db, profile.id),
        baseline_on=bilan.baseline_on(db, profile.id),
        next_due_on=bilan.next_due_on(db, profile.id),
    )


def save_battery(
    db: Session,
    profile: Profile,
    results: list[bilan.Result],
    library: LibraryBundle,
    settings: ProgramSettings,
    recorded_on: date,
    today: date | None = None,
) -> BilanState:
    """Store one battery and re-derive this profile's challenges from it."""
    now = today or recorded_on
    stored = bilan.record_results(db, profile, results, recorded_on)
    chal.close_met(db, profile.id, stored)
    chal.expire_due(db, profile.id, now)

    is_youth = profile.kind == "youth"
    band = age_band(profile)
    found = gap_rules.detect(db, profile.id, stored, library.assessments, band, is_youth=is_youth, today=now)
    ctx = make_context(profile, settings, library)
    candidates: list[Challenge] = []
    for item in found:
        spec = library.assessments.gap_rows.get(item.test_id)  # type: ignore[arg-type]
        row = None
        extra: dict[str, Any] = {}
        if spec is not None:
            best = bilan.best_value(db, profile.id, item.test_id) if item.test_id != BODY_COMP else None
            row = challenge_rows.build_row(spec, best, ctx)
            extra = {"frequency": spec.frequency, "day_types": [day.value for day in spec.day_types]}
        built = chal.build(
            library.assessments, profile.id, item, band, recorded_on, is_youth=is_youth, row=row, extra=extra
        )
        if built is not None:
            candidates.append(built)
    active = chal.store(db, profile.id, candidates)

    _record_placements(db, active, _weave(db, profile, active, library, ctx))
    close_assessment_session(db, profile.id)
    # The one commit of the call. Recording the battery, closing met challenges, deriving the new
    # ones and weaving their rows are a single fact: a failure part-way through used to leave a
    # battery stored with no challenges derived and no screen able to tell (D-223).
    db.commit()

    return BilanState(
        profile=profile,
        latest=bilan.latest_batch(db, profile.id),
        gaps=found,
        challenges=chal.active_for(db, profile.id),
        baseline_on=bilan.baseline_on(db, profile.id),
        next_due_on=bilan.next_due_on(db, profile.id),
        capped=tuple(result.test_id for result in results if result.capped),
    )


def _weave(db: Session, profile: Profile, active: list[Challenge], library: LibraryBundle, ctx: Any) -> dict[str, int]:
    """Put every active challenge's row back into the queue, replacing whatever was there.

    The queue is cleared first so a challenge that has just been met or expired stops appearing
    in sessions that have not been done yet. A session somebody has already started is never
    touched, whichever direction the change goes.

    Returns how many sessions each challenge actually reached, keyed by challenge id. The count
    used to be discarded, so a challenge that landed nowhere was still listed as active and the
    screen said it was part of the plan (D-221).
    """
    challenge_rows.clear_challenge_rows(db, profile.id, ctx)
    placements: dict[str, int] = {}
    for challenge in active:
        if challenge.test_id == BODY_COMP:
            placements[challenge.id] = challenge_rows.add_carry_set(db, profile.id, ctx, profile)
            continue
        spec = library.assessments.gap_rows.get(challenge.test_id)  # type: ignore[arg-type]
        if spec is None or not challenge.row_json:
            continue
        try:
            row = json.loads(challenge.row_json)
        except (TypeError, ValueError):
            logger.warning("challenge %r carries unreadable row_json; no row inserted", challenge.id)
            continue
        if isinstance(row, dict):
            placements[challenge.id] = challenge_rows.apply_row(db, profile.id, row, spec, ctx, profile)
    return placements


def _record_placements(db: Session, active: list[Challenge], placements: dict[str, int]) -> None:
    """Write each challenge's placement count onto its own row, and say so when it is zero.

    A challenge in no session is still a real challenge - it has a target and a due date, and the
    person can work at it - so this does not refuse it. It records what actually happened, so the
    screen can stop implying a row is waiting in a session when none is (D-221).
    """
    for challenge in active:
        placed = placements.get(challenge.id)
        if placed is None or not challenge.row_json:
            continue
        if not placed:
            logger.warning(
                "challenge %r for %r reached no planned session; it is active but not in the queue",
                challenge.id,
                challenge.profile_id,
            )
        try:
            row = json.loads(challenge.row_json)
        except (TypeError, ValueError):
            continue
        if not isinstance(row, dict):
            continue
        challenge.row_json = json.dumps({**row, "placed": placed}, sort_keys=True, separators=(",", ":"))
        db.add(challenge)


def close_assessment_session(db: Session, profile_id: str) -> str | None:
    """Take the assessment day off the head of the queue once its battery is recorded.

    Without this, saving a baseline hides the Today card (``is_due`` goes false) while
    ``resolve_today`` still returns the assessment session, so Today renders the six-test
    checklist with no card and no way past it but ticking through it by hand. The card's Skip was
    the only thing that advanced the queue, and saving had just hidden it (D-218).

    A checklist somebody has started ticking is left alone: they are working through it, and the
    honest record is the one they are making. An untouched one takes D-093's rule - a session that
    was only *rendered* is not work, so it and its empty rows go with the planned row.
    """
    statement = (
        select(PlannedSession)
        .where(
            PlannedSession.profile_id == profile_id,
            PlannedSession.status == PLANNED,
            PlannedSession.day_type == ASSESSMENT_DAY,
        )
        .order_by(PlannedSession.week, PlannedSession.day_index)
    )
    planned = db.exec(statement).first()
    if planned is None:
        return None
    open_sessions = db.exec(
        select(SessionRecord).where(
            SessionRecord.planned_session_id == planned.id,
            SessionRecord.finished_at.is_(None),  # type: ignore[union-attr]
        )
    ).all()
    if any(record.started_at is not None for record in open_sessions):
        return None
    for record in open_sessions:
        for row in db.exec(select(SessionRowRecord).where(SessionRowRecord.session_id == record.id)).all():
            db.delete(row)
        db.delete(record)
    planned.status = PLANNED_DONE
    db.add(planned)
    return planned.id


def card_due(db: Session, profile: Profile, today: date | None = None) -> bool:
    """Whether Today shows the Assessment-day card for ``profile`` (PRP-07 UI surface).

    ``is_due`` already answers ``True`` before the first battery, so the second half only decides
    *which* due day earns the card: the one the plan has queued as an assessment, or any day at
    all while no baseline exists. Skipping marks that queued day, and the next one brings the card
    back - which is the behaviour the PRP asks for, without a second piece of state to store.
    """
    now = today or datetime.now(UTC).date()
    if not bilan.is_due(db, profile.id, now):
        return False
    if bilan.baseline_on(db, profile.id) is None:
        return True
    statement = (
        select(PlannedSession)
        .where(PlannedSession.profile_id == profile.id, PlannedSession.status == PLANNED)
        .order_by(PlannedSession.week, PlannedSession.day_index)  # type: ignore[arg-type]
    )
    head = db.exec(statement).first()
    return head is not None and head.day_type == ASSESSMENT_DAY


def _queued_head(db: Session, profile_id: str) -> PlannedSession | None:
    """The session Today would render next. Ordered exactly as ``seance.today._next_planned``."""
    statement = (
        select(PlannedSession)
        .where(PlannedSession.profile_id == profile_id, PlannedSession.status == PLANNED)
        .order_by(PlannedSession.week, PlannedSession.day_index)  # type: ignore[arg-type]
    )
    return db.exec(statement).first()


def ensure_retest_queued(
    db: Session, profile: Profile, library: LibraryBundle, settings: ProgramSettings, today: date | None = None
) -> str | None:
    """Put an assessment day at the head of the queue when the retest falls due (D-226).

    Section 7 asks for a retest every four weeks, and ``card_due`` shows the card only while the
    head of the queue *is* an assessment day - deliberately, so the reminder never renders over a
    training session. Nothing re-inserted one after the seeded block's own assessment day was
    consumed, so the second reminder never arrived: `is_due` went true on day 28 and the head was
    a training session for ever after.

    Called from the Today route rather than from ``card_due``, which stays a pure read: a GET
    handler that writes on every render is worse than the bug. This returns immediately unless the
    retest is due and no assessment day is queued, so it is a no-op on every render but the first
    one past the due date.

    The row's id is stamped with **today's** date, not the due date (D-227). Two renders on one
    day still collide on the primary key, which is what stops a double queue; but a skipped
    retest keeps its row for ever, and ``next_due_on`` does not move until a new battery is
    recorded, so a due-date id matched the skipped row and the reminder never came back. Skipping
    is a statement about today (``assess_skip``: "the card returns on the next session"), so the
    id says which day the offer was made rather than which day it fell due.

    Returns the new session's id, or ``None`` when nothing needed queueing.
    """
    from cadence.programme.builder import program_id
    from cadence.programme.materialise import materialise_rows
    from cadence.programme.schemes import workout_id_for
    from cadence.schema.enums import DayType

    now = today or datetime.now(UTC).date()
    due = bilan.next_due_on(db, profile.id)
    if due is None or now < due:
        return None
    queued = db.exec(
        select(PlannedSession).where(
            PlannedSession.profile_id == profile.id,
            PlannedSession.status == PLANNED,
            PlannedSession.day_type == ASSESSMENT_DAY,
        )
    ).first()
    if queued is not None:
        return None

    identifier = f"{profile.id}-retest-{now.isoformat()}"
    if db.get(PlannedSession, identifier) is not None:
        return None

    kind = "youth" if profile.kind == "youth" else "adult"
    workout_id = workout_id_for(DayType.ASSESSMENT, kind)
    template = library.template(workout_id)
    # Week 1: an assessment day is the same six-test checklist whenever it falls, and the section
    # 6.2 week scheme has nothing to say about a day that prescribes no training load.
    rows = materialise_rows(template, 1, profile, settings, library)

    head = _queued_head(db, profile.id)
    # Immediately before whatever Today would otherwise have rendered, so the retest is the next
    # thing the person does. A negative ``day_index`` is fine for ordering and is renormalised by
    # the next ``restart_block``; the row belongs to the profile's own program, so a settings
    # rebuild sweeps it and the next render simply queues it again.
    week, day_index = (head.week, head.day_index - 1) if head is not None else (1, 0)

    db.add(
        PlannedSession(
            id=identifier,
            program_id=program_id(profile.id),
            profile_id=profile.id,
            week=week,
            day_index=day_index,
            day_type=DayType.ASSESSMENT.value,
            workout_id=workout_id,
            rows_json=json.dumps(rows, sort_keys=True, separators=(",", ":")),
            status=PLANNED,
        )
    )
    db.commit()
    logger.info("queued the %s retest for %r at (week %s, day %s)", due.isoformat(), profile.id, week, day_index)
    return identifier


def unavailable_tests(profile: Profile, library: LibraryBundle) -> set[str]:
    """Tests this household cannot run: the dead hang without an overhead anchor (D-019)."""
    if profile.has_overhead_anchor:
        return set()
    return {AssessmentId.DEAD_HANG_S.value}


def unavailable_result(library: LibraryBundle, test_id: str) -> bilan.Result:
    spec = library.assessments.spec(AssessmentId(test_id))
    return bilan.Result(test_id=test_id, value=None, unit=UNIT_UNAVAILABLE, self_rated=bool(spec and spec.self_rated))


__all__ = [
    "ASSESSMENT_DAY",
    "BilanState",
    "card_due",
    "ensure_retest_queued",
    "save_battery",
    "state_for",
    "unavailable_result",
    "unavailable_tests",
]
