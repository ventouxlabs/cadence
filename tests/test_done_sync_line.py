"""PRP-06 acceptance test 48: the Done screen's five sync states.

Each line reports the **server-side** ``sync_job``. PRP-02's offline banner ("Saved on this phone")
reports the browser's own outbox; both can be on screen at once, and they mean different things,
so the two strings stay deliberately distinct.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cadence.vitalforge.tables import FAILED, PENDING, SENT, SKIPPED, SyncJob
from cadence.vitalforge.writeback import (
    LINE_GARMIN_PENDING,
    LINE_NONE,
    LINE_PENDING,
    LINE_RETRYING,
    LINE_SENT,
    LINE_SKIPPED,
    LINE_TERMINAL,
    line_for,
)

LATER = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
ROWS_JSON = '[{"position": 1, "exercise_id": "goblet-squat", "name": "Goblet squat", "sets": 3, "reps": 10}]'


def _job(status: str, attempts: int = 0, next_attempt_at: str | None = None) -> SyncJob:
    return SyncJob(id="j", session_id="s", status=status, attempts=attempts, next_attempt_at=next_attempt_at)


@pytest.mark.parametrize(
    ("job", "text", "retry"),
    [
        (_job(SENT), LINE_SENT, False),
        # Stored, not filed: a schedule on a sent job is the Garmin half still outstanding.
        (_job(SENT, next_attempt_at=LATER), LINE_GARMIN_PENDING, False),
        (_job(PENDING, next_attempt_at=LATER), LINE_PENDING, False),
        # A 404 or a 401 leaves attempts at zero: nothing about the session is wrong.
        (_job(FAILED, attempts=0, next_attempt_at=LATER), LINE_PENDING, False),
        (_job(FAILED, attempts=3, next_attempt_at=LATER), LINE_RETRYING, False),
        (_job(FAILED, attempts=8), LINE_TERMINAL, True),
        (_job(SKIPPED), LINE_SKIPPED, True),
        (None, LINE_NONE, False),
    ],
)
def test_every_state_has_its_own_line(job: SyncJob | None, text: str, retry: bool) -> None:
    line = line_for(job)

    assert line.text == text
    assert line.retry is retry


def test_retry_appears_where_nothing_else_will_try() -> None:
    """No spinner, no polling loop. The button is offered on the two states a person can act on:
    the queue has given up, or there is no token yet and they have just added one."""
    quiet = [line_for(_job(status, 8, when)) for status, when in ((SENT, None), (PENDING, LATER))]

    assert not any(line.retry for line in quiet)
    assert line_for(_job(FAILED, 8)).retry is True
    assert line_for(_job(SKIPPED)).retry is True


async def test_a_skipped_session_offers_the_retry_button(live_db, vf_profiles, live_client) -> None:
    """High 2, on screen: the person who has just written a token needs a way to use it."""
    _finished(live_db, "s-3", "p-3")
    live_db.add(SyncJob(id="j3", session_id="s-3", status=SKIPPED))
    live_db.commit()

    page = await live_client.get("/done/s-3?profile=me")

    assert LINE_SKIPPED in page.text
    assert "/done/s-3/retry" in page.text


def _finished(db, session_id: str, planned_id: str) -> None:
    """One finished session with the planned row the Done page resolves it through."""
    from cadence.programme.tables import PlannedSession
    from cadence.seance.tables import SessionRecord

    db.add(
        PlannedSession(
            id=planned_id,
            program_id="prog",
            profile_id="me",
            week=1,
            day_index=1,
            day_type="lower_a",
            workout_id="lower-a",
            rows_json=ROWS_JSON,
            status="done",
        )
    )
    db.add(
        SessionRecord(
            id=session_id,
            profile_id="me",
            planned_session_id=planned_id,
            started_at="2026-09-06T08:00:00+00:00",
            finished_at="2026-09-06T09:00:00+00:00",
            duration_min=60,
        )
    )
    db.commit()


async def test_the_done_page_renders_the_line_and_the_button(live_db, vf_profiles, live_client) -> None:
    """The template, not just the helper: a state nobody renders is a state nobody has."""
    _finished(live_db, "s-1", "p-1")
    live_db.add(SyncJob(id="j", session_id="s-1", status=FAILED, attempts=8))
    live_db.commit()

    page = await live_client.get("/done/s-1?profile=me")

    assert LINE_TERMINAL in page.text
    assert "/done/s-1/retry" in page.text


async def test_a_skipped_session_says_so_without_promising_a_sync(live_db, vf_profiles, live_client) -> None:
    """It admits the session is only here. It does not claim a sync is coming."""
    _finished(live_db, "s-2", "p-2")
    live_db.add(SyncJob(id="j2", session_id="s-2", status=SKIPPED))
    live_db.commit()

    page = await live_client.get("/done/s-2?profile=me")

    # Scoped to the sync line. The base template's offline banner also says "will sync", and it
    # is a statement about the phone's own outbox rather than a promise about this session.
    line = page.text.split('id="sync-status"', 1)[1].split("</p>", 1)[0].lower()
    assert LINE_SKIPPED in page.text
    for promise in ("synced", "will sync", "uploaded"):
        assert promise not in line


# ------------------------------------------------------- the Retry button, when it says no


async def _refused(live_db, live_client, status_code: int, last_status: int | None = None):
    """A job in a state the queue will refuse to force, and the page that offers Retry on it."""
    _finished(live_db, "s-9", "p-9")
    job = SyncJob(
        id="j9",
        session_id="s-9",
        status=FAILED,
        attempts=8,
        last_attempt_at=datetime.now(UTC).isoformat(),
        last_status=last_status if last_status is not None else status_code,
    )
    live_db.add(job)
    live_db.commit()
    return await live_client.post("/done/s-9/retry?profile=me", headers={"hx-request": "true"})


async def test_a_retry_within_a_minute_says_so_instead_of_500ing(live_db, vf_profiles, live_client) -> None:
    """Holding the button is the tight loop D-016 forbids, and a refusal is an answer.

    Before this the refusal escaped the HTML route as an unhandled exception: the person got a
    server error for tapping a button twice.
    """
    response = await _refused(live_db, live_client, 500)

    assert response.status_code == 200
    assert "give it a minute" in response.text
    assert 'data-role="sync-notice"' in response.text


async def test_a_422_says_the_retry_cannot_help(live_db, vf_profiles, live_client) -> None:
    """VitalForge rejected the session itself; the same bytes will be rejected again."""
    response = await _refused(live_db, live_client, 422)

    assert response.status_code == 200
    assert "cannot be retried" in response.text


async def test_a_refused_plain_form_retry_carries_its_reason_across_the_redirect(
    live_db, vf_profiles, live_client
) -> None:
    """With JavaScript off the button is a form POST, and the reason has to survive a 303."""
    _finished(live_db, "s-8", "p-8")
    live_db.add(
        SyncJob(
            id="j8",
            session_id="s-8",
            status=FAILED,
            attempts=8,
            last_attempt_at=datetime.now(UTC).isoformat(),
            last_status=500,
        )
    )
    live_db.commit()

    posted = await live_client.post("/done/s-8/retry?profile=me")
    assert posted.status_code == 303
    assert "notice=throttled" in posted.headers["location"]

    page = await live_client.get(posted.headers["location"])
    assert "give it a minute" in page.text


async def test_an_unknown_notice_code_renders_nothing(live_db, vf_profiles, live_client) -> None:
    """The query string is looked up in a fixed table, never rendered. A whitelist, not text."""
    _finished(live_db, "s-7", "p-7")
    live_db.commit()

    page = await live_client.get("/done/s-7?profile=me&notice=<script>alert(1)</script>")

    assert "alert(1)" not in page.text
    assert 'data-role="sync-notice"' not in page.text


async def test_a_retry_the_queue_accepts_carries_no_notice(live_db, vf_profiles, live_client) -> None:
    """The ordinary case still just works, and says nothing extra."""
    import httpx
    import respx

    _finished(live_db, "s-6", "p-6")
    live_db.add(
        SyncJob(
            id="j6",
            session_id="s-6",
            status=FAILED,
            attempts=8,
            last_attempt_at=(datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
            last_status=500,
            payload_json='{"session_id": "s-6", "start": "2026-09-06T08:00:00+00:00", '
            '"duration_min": 40, "exercises": [{"name": "X", "sets": 1, "reps": 1}]}',
            target_slug="jd",
        )
    )
    live_db.commit()
    respx.post("http://weight.test/p/jd/api/activity").mock(return_value=httpx.Response(202, json={"id": 3}))

    response = await live_client.post("/done/s-6/retry?profile=me", headers={"hx-request": "true"})

    assert response.status_code == 200
    assert 'data-role="sync-notice"' not in response.text
    assert "synced" in response.text
