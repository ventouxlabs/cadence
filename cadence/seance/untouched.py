"""D-093: when a planned session may be rewritten, and what a rewrite has to clean up.

A planned session is safe to rewrite only while nobody has worked on it. D-024 creates a
``session`` over it (and copies its rows into ``session_row``) on the first *render* of Today, so
"a session exists" is the normal state of a checklist that has only been looked at; what marks real
work is ``started_at`` (first tick) or ``finished_at``. Every package that edits the plan asks that
question here, and every rewrite of ``rows_json`` drops the untouched copy so Today re-reads it.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import ColumnElement, or_
from sqlmodel import Session, select

from cadence.seance.tables import SessionRecord, SessionRowRecord


def is_started(record: SessionRecord) -> bool:
    """Whether anyone has actually worked on this session, as opposed to merely opening it."""
    return record.started_at is not None or record.finished_at is not None


def started_clause() -> ColumnElement[bool]:
    """The SQL form of :func:`is_started`, for statements that must decide at write time."""
    return or_(SessionRecord.started_at.is_not(None), SessionRecord.finished_at.is_not(None))  # type: ignore[union-attr]


def has_started_session(db: Session, planned_id: str) -> bool:
    """Whether any session over this planned session has been started or finished."""
    statement = select(SessionRecord).where(SessionRecord.planned_session_id == planned_id, started_clause())
    return db.exec(statement).first() is not None


def discard_records(db: Session, records: Iterable[SessionRecord]) -> None:
    """Drop these sessions and their ``session_row`` copies, so nothing points at nothing."""
    for record in records:
        for row in db.exec(select(SessionRowRecord).where(SessionRowRecord.session_id == record.id)).all():
            db.delete(row)
        db.delete(record)


def discard_untouched_copy(db: Session, planned_id: str) -> int:
    """Delete the unstarted session(s) over a planned session that is being rewritten.

    Returns how many were dropped. A started or finished session is left alone: callers check
    :func:`has_started_session` before rewriting, this only removes the stale first-render copy.
    """
    statement = select(SessionRecord).where(
        SessionRecord.planned_session_id == planned_id,
        SessionRecord.started_at.is_(None),  # type: ignore[union-attr]
        SessionRecord.finished_at.is_(None),  # type: ignore[union-attr]
    )
    records = db.exec(statement).all()
    discard_records(db, records)
    return len(records)
