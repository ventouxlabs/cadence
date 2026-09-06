"""Finalising a session: duration, ``felt``, the planned row's status, and the summary.

Idempotent by construction. A second Done -- a double tap, a queued replay, a reload of the Done
screen -- returns the same numbers and moves nothing, because a session that already carries a
``finished_at`` is summarised rather than finalised.

Together mode: one Done finalises every session in the group (D-013). Each keeps its own rows,
``felt``, duration and, from PRP-06, its own write-back.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlmodel import Session, select

from cadence.programme import next_time_note
from cadence.programme.tables import PlannedSession
from cadence.seance.status import Completion, completion, rows_done
from cadence.seance.tables import FELT_VALUES, SessionRecord
from cadence.seance.today import SessionView, view_for_session

# PRP-06 replaces this with the real sync status; until then a finished session is on this phone
# and nowhere else, and the Done screen says so rather than promising a sync that cannot happen.
SYNC_LOCAL = "local"

PLANNED_DONE = "done"
MIN_DURATION_MIN = 1


@dataclass(frozen=True, slots=True)
class Summary:
    """The three lines of the Done screen, plus what the JSON API returns."""

    session_id: str
    profile_id: str
    display_name: str
    duration_min: int
    rows_done: int
    rows_total: int
    completion: Completion
    felt: str | None
    next_time_note: str
    sync: str = SYNC_LOCAL

    def as_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "profile": self.profile_id,
            "duration_min": self.duration_min,
            "rows_done": self.rows_done,
            "rows_total": self.rows_total,
            "completion": self.completion,
            "felt": self.felt,
            "next_time_note": self.next_time_note,
            "sync": self.sync,
        }


def is_finished(record: SessionRecord) -> bool:
    """Whether this session has been summarised and may no longer be written to.

    The one predicate the HTML routes and the JSON API both ask. A stale tab, a back button or a
    queue drained late otherwise writes into a session that is already finished - and, worse, the
    next ``resolve_today`` then materialises the *following* planned session, leaving two open
    sessions over one profile and the user ticking a checklist that is not the one on screen.
    """
    return record.finished_at is not None


def normalise_felt(raw: str | None) -> str | None:
    """``easy``, ``right``, ``hard`` or nothing. Never blocks: ``felt`` may stay null."""
    value = (raw or "").strip().lower()
    return value if value in FELT_VALUES else None


def _parse(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def duration_minutes(record: SessionRecord, finished: datetime) -> int:
    """Wall-clock minutes from the first tick, at least one for a session that was started.

    A session finished without a single tick has no start, and reporting the time since it was
    rendered would count however long the phone sat on the floor. That is zero minutes, honestly.
    """
    started = _parse(record.started_at)
    if started is None:
        return 0
    elapsed = (finished - started).total_seconds() / 60
    return max(MIN_DURATION_MIN, int(round(elapsed)))


def group_members(db: Session, record: SessionRecord) -> list[SessionRecord]:
    """Every session finalised by one Done: the group, or this session alone."""
    if not record.together_group_id:
        return [record]
    statement = select(SessionRecord).where(SessionRecord.together_group_id == record.together_group_id)
    members = list(db.exec(statement).all())
    return members or [record]


def summarise(view: SessionView) -> Summary:
    """The summary of an already-finished session, computed from what is stored."""
    return Summary(
        session_id=view.record.id,
        profile_id=view.profile.id,
        display_name=view.profile.display_name,
        duration_min=int(view.record.duration_min or 0),
        rows_done=rows_done(row.record for row in view.rows),
        rows_total=view.rows_total,
        completion=completion([row.record for row in view.rows], view.rules),
        felt=view.record.felt,
        next_time_note=next_time_note(view.record),
    )


def _finalise_one(db: Session, record: SessionRecord, felt: str | None, finished: datetime) -> None:
    """Write the end of one session and close its planned row. Never re-finalises."""
    if record.finished_at is not None:
        return
    stamp = finished.isoformat()
    record.finished_at = stamp
    record.duration_min = duration_minutes(record, finished)
    if felt is not None:
        record.felt = felt
    db.add(record)
    planned = db.get(PlannedSession, record.planned_session_id)
    if planned is not None and planned.status != PLANNED_DONE:
        planned.status = PLANNED_DONE
        db.add(planned)


def set_felt(db: Session, record: SessionRecord, raw: str | None) -> str | None:
    """Record how it felt, before or after Done. Returns the stored value."""
    felt = normalise_felt(raw)
    if felt is None or record.felt == felt:
        return record.felt
    record.felt = felt
    db.add(record)
    db.commit()
    db.refresh(record)
    return record.felt


def finish(
    db: Session,
    view: SessionView,
    felt: str | None = None,
    ts: str | None = None,
    *,
    group: bool = True,
) -> list[Summary]:
    """Finalise this session, and its Together partner unless ``group`` says otherwise.

    ``group=False`` is the youth exit of D-084: the son saying he has had enough finishes *his*
    checklist and leaves the parent's open, which is the whole difference between a personal exit
    and the shared Done. Everything else keeps D-013's "one Done finalises both".

    Returns the summaries with this session's first, so the Done screen leads with the profile
    whose button was tapped.
    """
    chosen = normalise_felt(felt)
    finished = _parse(ts) or datetime.now(UTC)
    members = group_members(db, view.record) if group else [view.record]
    for member in members:
        # ``felt`` is this session's answer, not the other person's: a shared Done finalises both
        # but nobody gets to say how someone else's session felt.
        _finalise_one(db, member, chosen if member.id == view.record.id else None, finished)
    db.commit()

    summaries: list[Summary] = []
    for member in members:
        db.refresh(member)
        refreshed = view_for_session(db, member)
        if refreshed is not None:
            summaries.append(summarise(refreshed))
    summaries.sort(key=lambda item: item.session_id != view.record.id)
    return summaries
