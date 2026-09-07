"""PRP-06 acceptance tests 31-45: the bounded retry queue.

Bounded is the point (D-016, contract section 5.9). ``garminconnect`` raises on a 429 with no
backoff of its own, and the credential is shared with JD's weight logging, so a tight retry loop
here breaks something that has nothing to do with Cadence.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx

from cadence.config import Settings
from cadence.seance.tables import SessionRecord
from cadence.vitalforge.client import VitalForgeClient
from cadence.vitalforge.periodic import periodic_sync, run_once
from cadence.vitalforge.sync import (
    BAD_TOKEN_ERROR,
    MAX_ATTEMPTS,
    NO_ENDPOINT_ERROR,
    NO_TOKEN_WARNING,
    attempt,
    delay_minutes,
    drain,
    enqueue,
    job_for,
)
from cadence.vitalforge.tables import FAILED, PENDING, SENT, SKIPPED

ACTIVITY = "http://weight.test/p/jd/api/activity"
PAYLOAD = {
    "session_id": "s-1",
    "start": "2026-09-06T08:00:00+00:00",
    "duration_min": 42,
    "exercises": [{"name": "X", "sets": 1, "reps": 1}],
}


@pytest.fixture
def session_row(live_db, vf_profiles) -> SessionRecord:
    record = SessionRecord(id="s-1", profile_id="me", planned_session_id="p-1", started_at="2026-09-06T08:00:00+00:00")
    live_db.add(record)
    live_db.commit()
    return record


@pytest.fixture
def client(live_settings: Settings) -> VitalForgeClient:
    return VitalForgeClient(live_settings)


def _age_last_attempt(db, session_id: str, seconds: int = 300) -> None:
    """Pretend the last attempt was a while ago, so the manual-retry cooldown is not in the way."""
    job = job_for(db, session_id)
    job.last_attempt_at = (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()
    db.add(job)
    db.commit()


def _mock(status: int, body: dict | None = None) -> respx.Route:
    return respx.post(ACTIVITY).mock(return_value=httpx.Response(status, json=body if body is not None else {"id": 7}))


def test_enqueue_is_get_or_create(live_db, session_row) -> None:
    """Test 31. The offline queue replays Done, so a blind insert files the session twice."""
    first = enqueue(live_db, "s-1", PAYLOAD)
    second = enqueue(live_db, "s-1", {**PAYLOAD, "duration_min": 99})

    assert first.id == second.id
    assert json.loads(second.payload_json)["duration_min"] == 42


async def test_202_marks_sent_with_remote_ref(live_db, session_row, client) -> None:
    """Test 33."""
    _mock(202, {"id": 7, "garmin_status": "synced"})

    job = await attempt(live_db, enqueue(live_db, "s-1", PAYLOAD), client)

    assert job.status == SENT
    assert job.next_attempt_at is None
    assert job.remote_ref == "7"


async def test_200_dedup_also_marks_sent(live_db, session_row, client) -> None:
    """Test 34. A repeat is a success: VitalForge already has the session."""
    _mock(200, {"id": 7, "deduplicated": True})

    job = await attempt(live_db, enqueue(live_db, "s-1", PAYLOAD), client)

    assert job.status == SENT and job.next_attempt_at is None


async def test_an_unknown_garmin_status_is_terminal_with_a_warning(
    live_db, session_row, client, caplog: pytest.LogCaptureFixture
) -> None:
    """D-049. Stored, but the push may or may not have landed: only a human should re-POST."""
    caplog.set_level(logging.WARNING)
    _mock(202, {"id": 7, "garmin_status": "unknown", "garmin_error": "transport died mid-call"})

    job = await attempt(live_db, enqueue(live_db, "s-1", PAYLOAD), client)

    assert job.status == SENT and job.next_attempt_at is None
    assert "could not confirm the Garmin push" in caplog.text


async def test_409_is_terminal(live_db, session_row, client) -> None:
    """Test 35. A cross-person push needs a settings change; retrying only hammers Garmin."""
    route = _mock(409, {"detail": "This person has no Garmin account of their own."})

    job = await attempt(live_db, enqueue(live_db, "s-1", PAYLOAD), client)
    assert job.status == FAILED and job.next_attempt_at is None
    assert "no Garmin account" in job.last_error

    report = await drain(live_db, client)
    assert report.drained == 0
    assert route.call_count == 1


async def test_422_is_terminal(live_db, session_row, client) -> None:
    """Test 36. The payload is wrong. No amount of retrying makes it right."""
    _mock(422, {"detail": "unknown Garmin exercise category 'UNKNOWN'"})

    job = await attempt(live_db, enqueue(live_db, "s-1", PAYLOAD), client)

    assert job.status == FAILED and job.next_attempt_at is None


async def test_404_sets_the_branch_message_and_slow_retry(live_db, session_row, client) -> None:
    """Test 37. The expected state until PRP-05 is deployed. Not a bug, and not a burnt attempt."""
    _mock(404, {"detail": "Not Found"})

    job = await attempt(live_db, enqueue(live_db, "s-1", PAYLOAD), client)

    assert job.last_error == NO_ENDPOINT_ERROR
    assert "cadence/activity-endpoint" in job.last_error
    assert job.attempts == 0
    due = datetime.fromisoformat(job.next_attempt_at) - datetime.now(UTC)
    assert timedelta(hours=1, minutes=55) < due <= timedelta(hours=2)


async def test_401_does_not_burn_attempts(live_db, session_row, client) -> None:
    """Test 38. A fixed token should find the job still alive rather than exhausted."""
    _mock(401, {"detail": "Not authenticated"})
    job = enqueue(live_db, "s-1", PAYLOAD)

    for _ in range(3):
        job = await attempt(live_db, job, client)

    assert job.attempts == 0
    assert job.last_error == BAD_TOKEN_ERROR
    assert job.next_attempt_at is not None


@pytest.mark.parametrize(("attempts", "minutes"), [(1, 1), (2, 2), (3, 4), (4, 8), (5, 16), (6, 32), (7, 64), (8, 120)])
def test_backoff_schedule(attempts: int, minutes: int) -> None:
    """Test 39. D-016's schedule, capped at two hours."""
    assert delay_minutes(attempts) == minutes


async def test_attempts_exhausted_becomes_terminal(live_db, session_row, client) -> None:
    """Test 40. Eight tries, then stop and give the user a Retry button instead."""
    _mock(500, {"detail": "boom"})
    job = enqueue(live_db, "s-1", PAYLOAD)

    for _ in range(MAX_ATTEMPTS):
        job.next_attempt_at = datetime.now(UTC).isoformat()
        job = await attempt(live_db, job, client)

    assert job.attempts == MAX_ATTEMPTS
    assert job.next_attempt_at is None
    assert job.status == FAILED


async def test_a_timeout_is_retried_on_the_schedule(live_db, session_row, client) -> None:
    """A five-second timeout on Done is an ordinary outcome, not a terminal one."""
    respx.post(ACTIVITY).mock(side_effect=httpx.ReadTimeout("slow"))

    job = await attempt(live_db, enqueue(live_db, "s-1", PAYLOAD), client)

    assert job.status == FAILED and job.attempts == 1
    assert job.next_attempt_at is not None
    assert "ReadTimeout" in job.last_error


async def test_no_token_marks_skipped_and_logs(live_db, session_row, db_path, clean_env, caplog) -> None:
    """Test 41. Live mode with no token: stored here, sent nowhere, said out loud once."""
    caplog.set_level(logging.WARNING)
    settings = Settings(CADENCE_DB_PATH=db_path, CADENCE_VITALFORGE_MODE="live", VITALFORGE_TOKEN="", _env_file=None)

    job = await attempt(live_db, enqueue(live_db, "s-1", PAYLOAD), VitalForgeClient(settings))

    # Skipped, but **not** terminal: this is the shipped .env.example state, and the backlog has
    # to reach VitalForge once JD fills the token in (D-138).
    assert job.status == SKIPPED
    assert job.next_attempt_at is not None
    assert respx.calls.call_count == 0
    assert NO_TOKEN_WARNING in caplog.text
    assert caplog.text.count(NO_TOKEN_WARNING) == 1


async def test_drain_respects_next_attempt_at(live_db, session_row, client) -> None:
    """Test 42. A job due in an hour is not due now."""
    route = _mock(202)
    job = enqueue(live_db, "s-1", PAYLOAD)
    job.next_attempt_at = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    live_db.add(job)
    live_db.commit()

    report = await drain(live_db, client)

    assert report.drained == 0
    assert route.call_count == 0


async def test_explicit_retry_ignores_backoff(live_db, session_row, live_client, client) -> None:
    """Test 43. A human tapping Retry beats a schedule, including a terminal one."""
    _mock(409, {"detail": "nope"})
    await attempt(live_db, enqueue(live_db, "s-1", PAYLOAD), client)
    assert job_for(live_db, "s-1").next_attempt_at is None
    _age_last_attempt(live_db, "s-1")

    _mock(202, {"id": 9})
    response = await live_client.post("/api/sync/retry", json={"session_id": "s-1"})

    assert response.json()["data"] == {"drained": 1, "sent": 1, "failed": 0, "skipped": 0}
    assert job_for(live_db, "s-1").status == SENT


async def test_drain_survives_one_failing_job(live_db, vf_profiles, client, monkeypatch) -> None:
    """Test 44. A loop that dies on the first exception stops syncing forever and says nothing."""
    for index in (1, 2, 3):
        live_db.add(SessionRecord(id=f"s-{index}", profile_id="me", planned_session_id=f"p-{index}"))
    live_db.commit()
    for index in (1, 2, 3):
        enqueue(live_db, f"s-{index}", {**PAYLOAD, "session_id": f"s-{index}"})
    route = _mock(202)

    real_post = VitalForgeClient.post_activity

    async def _explode(self, slug, payload):
        if payload["session_id"] == "s-2":
            raise RuntimeError("this job is cursed")
        return await real_post(self, slug, payload)

    monkeypatch.setattr(VitalForgeClient, "post_activity", _explode)

    report = await drain(live_db, client)

    assert report.drained == 3
    assert route.call_count == 2
    assert job_for(live_db, "s-2").status == FAILED


async def test_periodic_task_cancels_cleanly(caplog: pytest.LogCaptureFixture) -> None:
    """Test 45. Shutdown leaves no pending task and no ``CancelledError`` traceback."""
    caplog.set_level(logging.WARNING)
    settings = Settings(CADENCE_VITALFORGE_MODE="mock", _env_file=None)
    task = asyncio.create_task(periodic_sync(settings, interval_s=60))
    await asyncio.sleep(0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert task.done() and task.cancelled()
    assert "CancelledError" not in caplog.text


async def test_the_periodic_pass_drains_and_refreshes(live_db, vf_profiles, session_row, live_settings) -> None:
    """The loop's body: the queue, then the cache. Mock mode still runs it (PRP-09)."""
    _mock(202, {"id": 3})
    for name in ("weight", "body_fat", "muscle_mass", "resting_hr", "sleep_score", "body_battery"):
        respx.get(f"http://dash.test/p/jd/api/metrics/{name}").mock(
            return_value=httpx.Response(200, json={"data": [{"date": "2026-09-06", "value": 1000}]})
        )
    respx.get("http://dash.test/p/jd/api/readiness").mock(
        return_value=httpx.Response(200, json={"score": 72, "status": "ok"})
    )
    respx.get("http://dash.test/p/kid/api/readiness").mock(
        return_value=httpx.Response(200, json={"score": None, "status": "insufficient_data"})
    )
    enqueue(live_db, "s-1", PAYLOAD)

    await run_once(live_settings)

    assert job_for(live_db, "s-1").status == SENT


async def test_the_periodic_loop_survives_a_raising_pass(monkeypatch, caplog: pytest.LogCaptureFixture) -> None:
    """Risk 9. A loop that dies on its first exception stops syncing forever and says nothing."""
    caplog.set_level(logging.WARNING)
    calls: list[int] = []

    async def _explode(settings):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("the first pass is cursed")

    monkeypatch.setattr("cadence.vitalforge.periodic.run_once", _explode)
    settings = Settings(CADENCE_VITALFORGE_MODE="mock", _env_file=None)

    task = asyncio.create_task(periodic_sync(settings, interval_s=0.01))
    for _ in range(200):
        await asyncio.sleep(0.01)
        if len(calls) >= 2:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(calls) >= 2, "the loop stopped after the failing pass"
    assert "the VitalForge periodic pass failed" in caplog.text


# ------------------------------------------------- review fixes: leases, throttles, healing


async def test_a_skipped_job_is_picked_up_once_the_token_arrives(live_db, session_row, db_path, clean_env) -> None:
    """High 2. The first Done on a fresh install has no token; the backlog must still land.

    ``skipped`` with no schedule meant ``due_jobs`` never looked at the job again, so every
    session done before ``.env`` was filled in stayed on the phone for good, silently.
    """
    unconfigured = Settings(
        CADENCE_DB_PATH=db_path, CADENCE_VITALFORGE_MODE="live", VITALFORGE_TOKEN="", _env_file=None
    )
    job = await attempt(live_db, enqueue(live_db, "s-1", PAYLOAD), VitalForgeClient(unconfigured))
    assert job.status == SKIPPED

    # Two hours later, with a token this time.
    _age_next_attempt(live_db, "s-1")
    _mock(202, {"id": 5})
    report = await drain(live_db, VitalForgeClient(_live_settings_with_token(db_path)))

    assert report.drained == 1 and report.sent == 1
    assert job_for(live_db, "s-1").status == SENT


def _age_next_attempt(db, session_id: str) -> None:
    job = job_for(db, session_id)
    job.next_attempt_at = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    db.add(job)
    db.commit()


def _live_settings_with_token(db_path) -> Settings:
    return Settings(
        CADENCE_DB_PATH=db_path,
        CADENCE_VITALFORGE_MODE="live",
        VITALFORGE_WEIGHT_URL="http://weight.test",
        VITALFORGE_DASHBOARD_URL="http://dash.test",
        VITALFORGE_TOKEN="vf-test-token-abc123",
        _env_file=None,
    )


async def test_a_stored_session_garmin_has_not_confirmed_is_re_posted(live_db, session_row, client) -> None:
    """Medium 3. Contract section 4.5: a re-POST re-attempts the push while it is pending."""
    _mock(202, {"id": 7, "garmin_status": "pending"})

    job = await attempt(live_db, enqueue(live_db, "s-1", PAYLOAD), client)

    assert job.status == SENT
    assert job.next_attempt_at is not None, "a pending Garmin push must come back around"
    due = datetime.fromisoformat(job.next_attempt_at) - datetime.now(UTC)
    assert timedelta(hours=1, minutes=55) < due <= timedelta(hours=2)

    _age_next_attempt(live_db, "s-1")
    _mock(200, {"id": 7, "deduplicated": True, "garmin_status": "synced"})
    await drain(live_db, client)

    assert job_for(live_db, "s-1").next_attempt_at is None


async def test_a_garmin_status_of_skipped_is_terminal(live_db, session_row, client) -> None:
    """D-042: a session first posted with push off can never be pushed by a later POST."""
    _mock(202, {"id": 7, "garmin_status": "skipped"})

    job = await attempt(live_db, enqueue(live_db, "s-1", PAYLOAD), client)

    assert job.status == SENT and job.next_attempt_at is None


async def test_two_concurrent_drains_make_one_post(live_db, session_row, live_settings) -> None:
    """Medium 4. Without a lease both readers see ``attempts = N`` and both write ``N + 1``,
    so ``MAX_ATTEMPTS`` bounds nothing and two requests race to file the same session."""
    from sqlmodel import Session as DbSession

    from cadence.db import get_engine

    route = _mock(202, {"id": 7})
    enqueue(live_db, "s-1", PAYLOAD)

    with DbSession(get_engine(live_settings)) as other:
        first, second = await asyncio.gather(
            drain(live_db, VitalForgeClient(live_settings)),
            drain(other, VitalForgeClient(live_settings)),
        )

    assert route.call_count == 1, "both drains attempted the same job"
    assert first.sent + second.sent == 1
    assert job_for(live_db, "s-1").attempts == 1


async def test_the_claim_is_written_before_the_request(live_db, session_row, client) -> None:
    """The lease is only a lease if it lands first: a process that dies mid-POST must not leave
    a job that looks due forever."""
    seen: dict[str, Any] = {}

    async def _capture(request):
        job = job_for(live_db, "s-1")
        live_db.refresh(job)
        seen["attempts"] = job.attempts
        seen["last_attempt_at"] = job.last_attempt_at
        return httpx.Response(202, json={"id": 7})

    respx.post(ACTIVITY).mock(side_effect=_capture)

    await attempt(live_db, enqueue(live_db, "s-1", PAYLOAD), client)

    assert seen["attempts"] == 1
    assert seen["last_attempt_at"] is not None


async def test_a_manual_retry_is_throttled_to_once_a_minute(live_db, session_row, live_client, client) -> None:
    """Medium 5. Holding Retry is the tight loop D-016 forbids (contract section 5.9)."""
    _mock(500, {"detail": "boom"})
    await attempt(live_db, enqueue(live_db, "s-1", PAYLOAD), client)
    before = respx.calls.call_count

    response = await live_client.post("/api/sync/retry", json={"session_id": "s-1"})

    assert response.status_code == 429
    assert response.json()["ok"] is False
    assert "give it a minute" in response.json()["error"]
    assert respx.calls.call_count == before, "the throttled retry still made a request"


async def test_a_422_is_never_offered_a_retry(live_db, session_row, live_client, client) -> None:
    """Medium 5. VitalForge rejected the payload itself; the same bytes will be rejected again."""
    _mock(422, {"detail": "unknown Garmin exercise category"})
    await attempt(live_db, enqueue(live_db, "s-1", PAYLOAD), client)
    _age_last_attempt(live_db, "s-1")
    before = respx.calls.call_count

    response = await live_client.post("/api/sync/retry", json={"session_id": "s-1"})

    assert response.status_code == 429
    assert "cannot be retried" in response.json()["error"]
    assert respx.calls.call_count == before


async def test_a_bulk_retry_is_never_throttled(live_db, session_row, live_client, client) -> None:
    """The cooldown is about one person hammering one button, not about the periodic drain."""
    _mock(500, {"detail": "boom"})
    await attempt(live_db, enqueue(live_db, "s-1", PAYLOAD), client)

    response = await live_client.post("/api/sync/retry", json={})

    assert response.status_code == 200


async def test_the_backfill_queues_a_finished_session_with_no_job(live_db, vf_profiles, live_settings) -> None:
    """Medium 6, second half. "Should never happen" is how a session goes missing quietly."""
    from cadence.programme.tables import PlannedSession
    from cadence.vitalforge.writeback import backfill

    live_db.add(
        PlannedSession(
            id="p-orphan",
            program_id="prog",
            profile_id="me",
            week=1,
            day_index=1,
            day_type="lower_a",
            workout_id="lower-a",
            status="done",
            rows_json='[{"position": 1, "exercise_id": "goblet-squat", "name": "Goblet squat", "sets": 3, "reps": 10}]',
        )
    )
    live_db.add(
        SessionRecord(
            id="orphan",
            profile_id="me",
            planned_session_id="p-orphan",
            started_at="2026-09-06T08:00:00+00:00",
            finished_at="2026-09-06T08:40:00+00:00",
            duration_min=40,
        )
    )
    from cadence.seance.tables import SessionRowRecord

    live_db.add(
        SessionRowRecord(
            id="orphan-1",
            session_id="orphan",
            position=1,
            exercise_id="goblet-squat",
            sets_planned=3,
            reps_planned=10,
            done=True,
        )
    )
    live_db.commit()
    assert job_for(live_db, "orphan") is None

    queued = backfill(live_db, config=live_settings)

    assert [job.session_id for job in queued] == ["orphan"]
    assert job_for(live_db, "orphan").status == "pending"


# ------------------------------------------------ Codex batch: slugs, opt-out, shapes, tasks


async def test_a_retry_goes_to_the_person_the_session_was_queued_for(live_db, session_row, vf_profiles, client) -> None:
    """Codex C. A slug edited between a timed-out attempt and its retry must not re-address it.

    The failure is quiet and permanent: VitalForge's idempotency key is per person, so the
    session lands cleanly under the wrong human and nothing downstream disagrees.
    """
    respx.post(ACTIVITY).mock(side_effect=httpx.ReadTimeout("slow"))
    job = await attempt(live_db, enqueue(live_db, "s-1", PAYLOAD), client)
    assert job.target_slug == "jd"

    someone_else = respx.post("http://weight.test/p/other/api/activity").mock(
        return_value=httpx.Response(202, json={"id": 1})
    )
    _mock(202, {"id": 2})
    vf_profiles["me"].vitalforge_person = "other"
    live_db.add(vf_profiles["me"])
    live_db.commit()
    _age_next_attempt(live_db, "s-1")

    await drain(live_db, client)

    assert someone_else.call_count == 0, "a queued session was filed under a different person"
    assert respx.calls.last.request.url.path == "/p/jd/api/activity"
    assert job_for(live_db, "s-1").status == SENT


async def test_a_job_queued_before_anyone_was_configured_heals(live_db, live_settings) -> None:
    """The one exception: a blank frozen slug addresses nobody, so it resolves live (D-138)."""
    from cadence.profils.tables import Profile

    live_db.add(Profile(id="me", display_name="Me", kind="adult", vitalforge_person=""))
    live_db.add(SessionRecord(id="s-9", profile_id="me", planned_session_id="p-9"))
    live_db.commit()
    job = enqueue(live_db, "s-9", {**PAYLOAD, "session_id": "s-9"})
    assert job.target_slug == ""

    profile = live_db.get(Profile, "me")
    profile.vitalforge_person = "jd"
    live_db.add(profile)
    live_db.commit()
    route = _mock(202, {"id": 3})

    job = await attempt(live_db, job_for(live_db, "s-9"), VitalForgeClient(live_settings))

    assert route.call_count == 1
    assert job.target_slug == "jd", "the resolved slug was not frozen for the next retry"


async def test_switching_the_sons_push_off_withdraws_his_queued_pushes(live_db, vf_profiles, client) -> None:
    """Codex D. Timeout, then opt out: the queued body still says ``push_to_garmin: true``.

    Two days later the drain would file the son's session under the parent's Garmin account,
    after the household had explicitly said no. VitalForge cannot help; it does what the body
    asks. So the request is withdrawn rather than the body rewritten.
    """
    from cadence.profils.services import update_settings
    from cadence.vitalforge.tables import CANCELLED

    update_settings(live_db, {"push_son_to_garmin": True})
    live_db.add(SessionRecord(id="kid-1", profile_id="son", planned_session_id="p-kid"))
    live_db.commit()
    body = {**PAYLOAD, "session_id": "kid-1", "push_to_garmin": True, "garmin_target": "credential_person"}
    respx.post("http://weight.test/p/kid/api/activity").mock(side_effect=httpx.ReadTimeout("slow"))
    job = await attempt(live_db, enqueue(live_db, "kid-1", body), client)
    assert job.status == FAILED and job.next_attempt_at is not None

    update_settings(live_db, {"push_son_to_garmin": False})

    job = job_for(live_db, "kid-1")
    assert job.status == CANCELLED
    assert job.next_attempt_at is None
    assert json.loads(job.payload_json)["push_to_garmin"] is True, "the stored body was rewritten"

    _age_next_attempt(live_db, "kid-1")
    report = await drain(live_db, client)
    assert report.drained == 0


async def test_switching_the_push_off_leaves_the_parents_jobs_alone(live_db, session_row, vf_profiles, client) -> None:
    """The parent's Garmin push is his own setting and has nothing to do with the son's."""
    from cadence.profils.services import update_settings

    update_settings(live_db, {"push_son_to_garmin": True})
    respx.post(ACTIVITY).mock(side_effect=httpx.ReadTimeout("slow"))
    await attempt(live_db, enqueue(live_db, "s-1", {**PAYLOAD, "push_to_garmin": True}), client)

    update_settings(live_db, {"push_son_to_garmin": False})

    assert job_for(live_db, "s-1").status == FAILED


async def test_switching_the_push_off_does_not_unfile_a_sent_session(live_db, vf_profiles, client) -> None:
    """Cancelling is a withdrawal, not an undo. A filed activity stays filed and says so."""
    from cadence.profils.services import update_settings

    update_settings(live_db, {"push_son_to_garmin": True})
    live_db.add(SessionRecord(id="kid-2", profile_id="son", planned_session_id="p-kid2"))
    live_db.commit()
    body = {**PAYLOAD, "session_id": "kid-2", "push_to_garmin": True, "garmin_target": "credential_person"}
    respx.post("http://weight.test/p/kid/api/activity").mock(return_value=httpx.Response(202, json={"id": 4}))
    await attempt(live_db, enqueue(live_db, "kid-2", body), client)

    update_settings(live_db, {"push_son_to_garmin": False})

    assert job_for(live_db, "kid-2").status == SENT


def test_the_periodic_task_does_not_start_under_test_or_when_switched_off(db_path, clean_env) -> None:
    """Codex G. A loop that wakes mid-assertion writes to the rows a test is reading."""
    assert Settings(CADENCE_DB_PATH=db_path, CADENCE_ENV="test", _env_file=None).periodic_sync_enabled is False
    off = Settings(CADENCE_DB_PATH=db_path, CADENCE_ENV="dev", CADENCE_PERIODIC_SYNC="0", _env_file=None)
    assert off.periodic_sync_enabled is False
    assert Settings(CADENCE_DB_PATH=db_path, CADENCE_ENV="prod", _env_file=None).periodic_sync_enabled is True


async def test_the_lifespan_starts_no_task_when_sync_is_off(db_path, clean_env) -> None:
    """The setting has to reach the lifespan, not just the settings object."""
    import asyncio

    from cadence.main import create_app

    settings = Settings(CADENCE_DB_PATH=db_path, CADENCE_ENV="test", _env_file=None)
    before = len(asyncio.all_tasks())
    async with create_app(settings).router.lifespan_context(create_app(settings)):
        assert len(asyncio.all_tasks()) == before


async def test_opting_out_stops_a_garmin_push_vitalforge_is_still_holding(live_db, vf_profiles, client) -> None:
    """The two fixes interacting: finding 3 created a live state finding D did not withdraw.

    Setting on, son's session posted, VitalForge answers 202 with ``garmin_status: pending`` — so
    the job is ``sent`` **with a schedule**, because the contract's re-POST is how that push gets
    retried. The parent then turns the setting off. ``open_jobs`` used to look at pending, failed
    and skipped only, so this job was skipped by the withdrawal and the drain re-POSTed the
    frozen body two hours later, filing the son under the parent's Garmin after the household had
    said no.
    """
    from cadence.profils.services import update_settings
    from cadence.vitalforge.schedule import GARMIN_WITHDRAWN

    update_settings(live_db, {"push_son_to_garmin": True})
    live_db.add(SessionRecord(id="kid-3", profile_id="son", planned_session_id="p-kid3"))
    live_db.commit()
    body = {**PAYLOAD, "session_id": "kid-3", "push_to_garmin": True, "garmin_target": "credential_person"}
    route = respx.post("http://weight.test/p/kid/api/activity").mock(
        return_value=httpx.Response(202, json={"id": 11, "garmin_status": "pending"})
    )
    job = await attempt(live_db, enqueue(live_db, "kid-3", body), client)
    assert job.status == SENT and job.next_attempt_at is not None
    posts_before = route.call_count

    update_settings(live_db, {"push_son_to_garmin": False})

    job = job_for(live_db, "kid-3")
    assert job.status == SENT, "the session really is stored; cancelling would say otherwise"
    assert job.next_attempt_at is None
    assert job.last_error == GARMIN_WITHDRAWN

    # No ageing: a withdrawn job has no schedule left to bring forward, which is the point.
    await drain(live_db, client)
    await drain(live_db, client)

    assert route.call_count == posts_before, "the withdrawn Garmin push was re-POSTed anyway"


async def test_open_jobs_sees_all_four_live_states(live_db, vf_profiles) -> None:
    """The set a withdrawal has to look at. ``sent`` with a schedule is the easy one to miss."""
    from cadence.vitalforge.sync import open_jobs
    from cadence.vitalforge.tables import CANCELLED, SyncJob

    schedule = "2026-09-06T08:00:00+00:00"
    # Live: anything a drain or a Retry button could still turn into a request. A terminal
    # ``failed`` is on the list because the Done screen offers Retry on exactly that state.
    live = {
        "pending": (PENDING, schedule),
        "failed-scheduled": (FAILED, schedule),
        "failed-terminal": (FAILED, None),
        "skipped": (SKIPPED, schedule),
        "sent-garmin-pending": (SENT, schedule),
    }
    # Done with: the session is filed and nothing will ask again, or a person already said no.
    settled = {"sent": (SENT, None), "cancelled": (CANCELLED, None)}
    for name, (status, when) in {**live, **settled}.items():
        live_db.add(SyncJob(id=f"j-{name}", session_id=name, status=status, next_attempt_at=when))
    live_db.commit()

    assert sorted(job.session_id for job in open_jobs(live_db)) == sorted(live)


async def test_a_withdrawn_push_still_reads_as_stored(live_db, vf_profiles) -> None:
    """The Done line must not say "synced ✓": the activity is the one thing that was prevented."""
    from cadence.vitalforge.schedule import GARMIN_WITHDRAWN
    from cadence.vitalforge.tables import SyncJob
    from cadence.vitalforge.writeback import LINE_GARMIN_WITHDRAWN, line_for

    job = SyncJob(id="j", session_id="s", status=SENT, next_attempt_at=None, last_error=GARMIN_WITHDRAWN)

    line = line_for(job)

    assert line.text == LINE_GARMIN_WITHDRAWN
    assert line.retry is False
