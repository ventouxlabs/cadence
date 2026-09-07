"""``writeback.py``'s failure paths: the ones that only run when something else has gone wrong.

Most of these are **synchronous** tests on purpose. ``run_blocking`` exists to drive the async
client from a FastAPI ``def`` handler, which runs in a worker thread with no event loop; an
``async def`` test would put a loop back and be refused, which is a different code path.

Every branch here exists so that a broken integration cannot break Done. They are worth testing
precisely because nothing reaches them in a healthy system, which is also why they rot unnoticed.
"""

from __future__ import annotations

import logging

import httpx
import pytest
import respx
from sqlalchemy import text

from cadence.db import get_engine
from cadence.seance.tables import SessionRecord
from cadence.vitalforge.sync import enqueue, job_for
from cadence.vitalforge.tables import SENT
from cadence.vitalforge.writeback import (
    LINE_NONE,
    attempt_now,
    line_for,
    line_for_session,
    run_blocking,
    sync_finished,
    sync_session,
)

ACTIVITY = "http://weight.test/p/jd/api/activity"
PAYLOAD = {
    "session_id": "s-1",
    "start": "2026-09-06T08:00:00+00:00",
    "duration_min": 42,
    "exercises": [{"name": "X", "sets": 1, "reps": 1}],
}


def _session(db, session_id: str = "s-1", planned_id: str = "p-1") -> SessionRecord:
    record = SessionRecord(id=session_id, profile_id="me", planned_session_id=planned_id)
    db.add(record)
    db.commit()
    return record


async def test_run_blocking_refuses_to_run_inside_a_loop(caplog: pytest.LogCaptureFixture) -> None:
    """Called from an async caller by mistake: leave the job for the drain, do not break Done."""
    caplog.set_level(logging.WARNING)

    async def _work() -> str:
        return "ran"

    assert run_blocking(_work()) is None
    assert "should await it instead" in caplog.text


def test_run_blocking_runs_when_there_is_no_loop() -> None:
    """The ordinary case: a synchronous request handler in a FastAPI worker thread."""

    async def _work() -> str:
        return "ran"

    assert run_blocking(_work()) == "ran"


def test_a_session_with_no_plan_is_not_queued(live_db, vf_profiles, live_settings, caplog) -> None:
    """A session whose planned row has gone cannot be described, and is not a crash."""
    caplog.set_level(logging.WARNING)
    record = _session(live_db, "orphan", "gone")

    assert sync_session(live_db, record, config=live_settings) is None
    assert respx.calls.call_count == 0
    assert "no profile or plan" in caplog.text


def test_an_unreadable_exercise_catalog_still_sends(live_db, vf_profiles, live_settings, caplog) -> None:
    """The materialised row already carries the name and the category; the catalog only adds the
    optional sub-category, so losing it must cost the sub-category and nothing else."""
    from tests.test_sync import PAYLOAD as _  # noqa: F401 - keeps the fixtures aligned

    caplog.set_level(logging.WARNING)
    route = respx.post(ACTIVITY).mock(return_value=httpx.Response(202, json={"id": 1}))
    _seed_plan_and_rows(live_db)
    with get_engine(live_settings).begin() as connection:
        connection.execute(text("DROP TABLE exercise"))

    job = sync_session(live_db, live_db.get(SessionRecord, "s-1"), config=live_settings)

    assert route.call_count == 1
    assert job.status == SENT
    assert "could not read the exercise catalog" in caplog.text


def _seed_plan_and_rows(db) -> None:
    from cadence.programme.tables import PlannedSession
    from cadence.seance.tables import SessionRowRecord

    rows = '[{"position": 1, "exercise_id": "goblet-squat", "name": "Goblet Squat", "sets": 3, "reps": 10}]'
    db.add(
        PlannedSession(
            id="p-1",
            program_id="prog",
            profile_id="me",
            week=1,
            day_index=1,
            day_type="lower_a",
            workout_id="lower-a",
            rows_json=rows,
            status="done",
        )
    )
    db.add(
        SessionRecord(
            id="s-1",
            profile_id="me",
            planned_session_id="p-1",
            started_at="2026-09-06T08:00:00+00:00",
            finished_at="2026-09-06T08:42:00+00:00",
            duration_min=42,
        )
    )
    db.add(
        SessionRowRecord(
            id="s-1-1",
            session_id="s-1",
            position=1,
            exercise_id="goblet-squat",
            sets_planned=3,
            reps_planned=10,
            done=True,
        )
    )
    db.commit()


def test_sync_finished_keeps_going_when_one_session_cannot_be_queued(
    live_db, vf_profiles, live_settings, caplog
) -> None:
    """Together mode files two. One of them failing must not take the other with it."""
    caplog.set_level(logging.WARNING)
    respx.post(ACTIVITY).mock(return_value=httpx.Response(202, json={"id": 1}))
    _seed_plan_and_rows(live_db)
    broken = _session(live_db, "broken", "missing")

    jobs = sync_finished(live_db, [broken, live_db.get(SessionRecord, "s-1")], config=live_settings)

    assert list(jobs) == ["s-1"]


def test_a_session_already_sent_is_not_sent_again(live_db, vf_profiles, live_settings) -> None:
    """A replayed Done must not file the session twice."""
    route = respx.post(ACTIVITY).mock(return_value=httpx.Response(202, json={"id": 1}))
    _seed_plan_and_rows(live_db)
    first = sync_session(live_db, live_db.get(SessionRecord, "s-1"), config=live_settings)

    second = sync_session(live_db, live_db.get(SessionRecord, "s-1"), config=live_settings)

    assert route.call_count == 1
    assert first.id == second.id and second.status == SENT


def test_attempt_now_on_nothing_is_nothing(live_db) -> None:
    """A session with no rows produces no job, and the caller passes that ``None`` straight on."""
    assert attempt_now(live_db, None) is None


def test_the_line_for_a_session_nobody_queued(live_db, vf_profiles) -> None:
    assert line_for_session(live_db, "never-heard-of-it").text == LINE_NONE
    assert line_for(None).text == LINE_NONE


def test_the_backfill_finds_nothing_in_a_healthy_queue(live_db, vf_profiles, live_settings) -> None:
    """The expected result. It runs every pass anyway, because "should never" is how a session
    goes missing quietly."""
    from cadence.vitalforge.writeback import backfill

    respx.post(ACTIVITY).mock(return_value=httpx.Response(202, json={"id": 1}))
    _seed_plan_and_rows(live_db)
    sync_session(live_db, live_db.get(SessionRecord, "s-1"), config=live_settings)

    assert backfill(live_db, config=live_settings) == []


def test_cancelling_ignores_a_job_whose_body_will_not_parse(live_db, vf_profiles) -> None:
    """A corrupt payload is not a youth push, and must not stop the ones that are."""
    from cadence.vitalforge.writeback import cancel_youth_pushes

    _session(live_db, "bad", "p-bad")
    job = enqueue(live_db, "bad", PAYLOAD)
    job.payload_json = "{not json"
    live_db.add(job)
    live_db.commit()

    assert cancel_youth_pushes(live_db) == []
    assert job_for(live_db, "bad").status == "pending"
