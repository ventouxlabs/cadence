"""D-093's single rule: what counts as started, and the cleanup that goes with a rewrite."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlmodel import Session as DbSession
from sqlmodel import select

from cadence.programme.tables import PLANNED, PlannedSession
from cadence.seance.tables import SessionRecord, SessionRowRecord
from cadence.seance.today import ensure_session
from cadence.seance.untouched import discard_untouched_copy, has_started_session, is_started


def _head(db: DbSession) -> PlannedSession:
    statement = (
        select(PlannedSession)
        .where(PlannedSession.profile_id == "me", PlannedSession.status == PLANNED)
        .order_by(PlannedSession.week, PlannedSession.day_index)  # type: ignore[arg-type]
    )
    head = db.exec(statement).first()
    assert head is not None
    return head


def test_started_means_a_tick_or_a_finish_and_not_a_look() -> None:
    now = datetime.now(UTC)
    assert is_started(SessionRecord(id="a", profile_id="me", planned_session_id="p")) is False
    assert is_started(SessionRecord(id="b", profile_id="me", planned_session_id="p", started_at=now)) is True
    assert is_started(SessionRecord(id="c", profile_id="me", planned_session_id="p", finished_at=now)) is True


def test_discard_drops_an_untouched_copy_and_its_rows(seeded: Any, db_session: DbSession) -> None:
    planned = _head(db_session)
    record = ensure_session(db_session, planned)
    assert db_session.exec(select(SessionRowRecord).where(SessionRowRecord.session_id == record.id)).all()
    assert has_started_session(db_session, planned.id) is False

    assert discard_untouched_copy(db_session, planned.id) == 1
    db_session.commit()
    assert db_session.exec(select(SessionRecord).where(SessionRecord.planned_session_id == planned.id)).all() == []
    assert db_session.exec(select(SessionRowRecord).where(SessionRowRecord.session_id == record.id)).all() == []


def test_discard_leaves_a_started_session_alone(seeded: Any, db_session: DbSession) -> None:
    planned = _head(db_session)
    record = ensure_session(db_session, planned)
    record.started_at = datetime.now(UTC)
    db_session.add(record)
    db_session.commit()

    assert has_started_session(db_session, planned.id) is True
    assert discard_untouched_copy(db_session, planned.id) == 0
    assert db_session.get(SessionRecord, record.id) is not None
