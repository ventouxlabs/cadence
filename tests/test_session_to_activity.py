"""PRP-06 acceptance test 50, and the brief's finish criterion.

    One full session headlessly through Done, asserting the ``/api/activity`` payload against a
    mocked VitalForge.

In process rather than under Playwright, because the assertion is about the **exact JSON body**
and respx cannot reach a subprocess uvicorn. The browser half - that the Done screen shows the
right line - lives in ``tests/e2e/test_session_to_activity.py``.

The comparison is ``==`` on the whole parsed body, not a subset match, so a key added "for
debugging" fails here rather than as a 422 on the day of a real session.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from cadence.profils.tables import Profile, Setting
from cadence.programme.tables import PlannedSession, Program
from cadence.vitalforge.tables import SENT
from tests.conftest import complete_setup

ACTIVITY_ME = "http://weight.test/p/jd/api/activity"
ACTIVITY_SON = "http://weight.test/p/kid/api/activity"

ROWS = [
    {
        "position": 1,
        "exercise_id": "goblet-squat",
        "name": "Goblet Squat",
        "garmin_category": "SQUAT",
        "sets": 3,
        "reps": 10,
        "seconds": None,
        "load_kg": 20.0,
        "rest_s": 90,
        "measure": "reps",
        "is_prelude": False,
    },
    {
        "position": 2,
        "exercise_id": "plank",
        "name": "Plank",
        "garmin_category": "PLANK",
        "sets": 3,
        "reps": None,
        "seconds": 45,
        "load_kg": None,
        "rest_s": 60,
        "measure": "seconds",
        "is_prelude": False,
    },
]

EXPECTED_EXERCISES = [
    {"name": "Goblet Squat", "garmin_category": "SQUAT", "sets": 3, "reps": 10, "weight_kg": 20.0, "rest_s": 90},
    {"name": "Plank", "garmin_category": "PLANK", "sets": 3, "reps": 1, "seconds": 45, "rest_s": 60},
]


def _seed_two_profiles(db) -> None:
    """The pair ``make seed`` writes, with their VitalForge slugs (D-017)."""
    db.add(Profile(id="me", display_name="Me", kind="adult", push_to_garmin=True, vitalforge_person="jd"))
    db.add(Profile(id="son", display_name="Son", kind="youth", vitalforge_person="kid"))
    db.commit()
    complete_setup(db)


def _seed_plan(db) -> None:
    """One block per profile, one planned day each, with two rows the assertions can pin."""
    for profile_id in ("me", "son"):
        db.add(
            Program(
                id=f"prog-{profile_id}",
                profile_id=profile_id,
                template="strength",
                start_date="2026-09-06",
                weeks=4,
                days_per_week=4,
                session_minutes=30,
            )
        )
        db.add(
            PlannedSession(
                id=f"plan-{profile_id}",
                program_id=f"prog-{profile_id}",
                profile_id=profile_id,
                week=1,
                day_index=1,
                day_type="lower_a",
                workout_id="lower-a",
                rows_json=json.dumps(ROWS),
            )
        )
    db.commit()


@pytest.fixture
def planned(live_db, vf_profiles):
    """The plan both profiles run in these tests."""
    _seed_plan(live_db)


def _push_son(live_db, on: bool) -> None:
    """``merge``, not ``add``: ``complete_setup`` has already written this key."""
    live_db.merge(Setting(key="push_son_to_garmin", value_json=json.dumps(on), updated_at="2026-09-06T00:00:00+00:00"))
    live_db.commit()


async def _full_session(client: httpx.AsyncClient, profile: str) -> str:
    """Tick every row, say how it felt, and hit Done - through the real routes only."""
    data = (await client.get(f"/api/today?profile={profile}")).json()["data"]
    session_id = data["session_id"]
    for row in data["rows"]:
        response = await client.post(f"/api/sessions/{session_id}/rows/{row['position']}", json={"done": True})
        assert response.status_code == 200, response.text
    done = await client.post(f"/api/sessions/{session_id}/done", json={"felt": "right"})
    assert done.status_code == 200, done.text
    return session_id


def _recorded(route: respx.Route) -> dict:
    assert route.call_count == 1, f"expected exactly one POST, got {route.call_count}"
    return json.loads(route.calls.last.request.content)


def _assert_start_and_duration(body: dict) -> None:
    """The two fields a clock decides. Checked by rule, then compared like everything else."""
    assert body["start"].endswith("+00:00")
    assert datetime.fromisoformat(body["start"]).tzinfo is not None
    assert body["duration_min"] >= 1


async def test_full_session_posts_exact_activity_json(live_db, planned, live_client) -> None:
    """The parent's session, end to end, compared key for key."""
    route = respx.post(ACTIVITY_ME).mock(return_value=httpx.Response(202, json={"id": 7, "garmin_status": "synced"}))

    session_id = await _full_session(live_client, "me")

    body = _recorded(route)
    _assert_start_and_duration(body)
    assert body == {
        "session_id": session_id,
        "session_label": "Lower A",
        "start": body["start"],
        "duration_min": body["duration_min"],
        "exercises": EXPECTED_EXERCISES,
        # PRP-07 landed, so the note is the autoregulator's real line rather than PRP-02's
        # placeholder. D-129 said this assertion would move when it did (D-219).
        "notes": "felt: right · Next time: same again — nail the tempo.",
        "source": "cadence",
        "push_to_garmin": True,
    }


async def test_the_son_pushes_only_when_the_setting_says_so(live_db, planned, live_client) -> None:
    """D-021 and D-015 on the wire: the setting decides, and ``garmin_target`` rides with it."""
    _push_son(live_db, True)
    route = respx.post(ACTIVITY_SON).mock(return_value=httpx.Response(202, json={"id": 8}))

    session_id = await _full_session(live_client, "son")

    body = _recorded(route)
    assert body == {
        "session_id": session_id,
        "session_label": "Lower A",
        "start": body["start"],
        "duration_min": body["duration_min"],
        "exercises": EXPECTED_EXERCISES,
        # PRP-07 landed, so the note is the autoregulator's real line rather than PRP-02's
        # placeholder. D-129 said this assertion would move when it did (D-219).
        "notes": "felt: right · Next time: same again — nail the tempo.",
        "source": "cadence",
        "push_to_garmin": True,
        "garmin_target": "credential_person",
    }


async def test_the_son_stores_without_pushing_when_the_setting_is_off(live_db, planned, live_client) -> None:
    """The default. His session is stored in VitalForge and never reaches anyone's Garmin (D-069)."""
    _push_son(live_db, False)
    route = respx.post(ACTIVITY_SON).mock(return_value=httpx.Response(202, json={"id": 9}))

    await _full_session(live_client, "son")

    body = _recorded(route)
    assert body["push_to_garmin"] is False
    assert "garmin_target" not in body


async def test_done_attempts_inline_once(live_db, planned, live_client) -> None:
    """Test 32. One POST on Done, and the response does not wait for a retry."""
    route = respx.post(ACTIVITY_ME).mock(return_value=httpx.Response(202, json={"id": 7}))

    session_id = await _full_session(live_client, "me")

    assert route.call_count == 1
    from cadence.vitalforge.sync import job_for

    assert job_for(live_db, session_id).status == SENT


async def test_a_replayed_done_does_not_file_the_session_twice(live_db, planned, live_client) -> None:
    """The offline queue replays Done. ``enqueue`` is get-or-create, so the job is the same one."""
    route = respx.post(ACTIVITY_ME).mock(return_value=httpx.Response(202, json={"id": 7}))
    session_id = await _full_session(live_client, "me")

    replay = await live_client.post(f"/api/sessions/{session_id}/done", json={"felt": "right"})

    assert replay.json()["meta"]["replayed"] is True
    assert replay.json()["data"]["sync"] == SENT
    assert route.call_count == 1


async def test_done_still_renders_when_vitalforge_is_down(live_db, planned, live_client) -> None:
    """Risk 10: a slow VitalForge must not block the summary. Failure is an ordinary outcome."""
    respx.post(ACTIVITY_ME).mock(side_effect=httpx.ConnectError("refused"))

    session_id = await _full_session(live_client, "me")

    page = await live_client.get(f"/done/{session_id}?profile=me")
    assert page.status_code == 200
    assert "sync failed (retrying)" in page.text


async def test_a_session_with_nothing_ticked_is_never_posted(live_db, planned, live_client) -> None:
    """``exercises`` is ``min_length=1``: an empty list is a guaranteed, permanent 422."""
    data = (await live_client.get("/api/today?profile=me")).json()["data"]
    session_id = data["session_id"]

    await live_client.post(f"/api/sessions/{session_id}/done", json={})

    assert respx.calls.call_count == 0
    page = await live_client.get(f"/done/{session_id}?profile=me")
    assert "Stored locally." in page.text


async def test_a_full_session_with_no_token_stores_locally_and_says_so(
    db_path, clean_env, caplog: pytest.LogCaptureFixture
) -> None:
    """PRP-06 "Done when": blank token, full session, Done renders, one WARNING, verbatim."""
    import logging

    from cadence.config import Settings
    from cadence.db import get_engine, init_db
    from cadence.main import create_app
    from cadence.vitalforge.sync import NO_TOKEN_WARNING
    from cadence.vitalforge.tables import SKIPPED

    caplog.set_level(logging.WARNING)
    settings = Settings(CADENCE_DB_PATH=db_path, CADENCE_VITALFORGE_MODE="live", VITALFORGE_TOKEN="", _env_file=None)
    app = create_app(settings)
    init_db(settings)
    from sqlmodel import Session as DbSession

    with DbSession(get_engine(settings)) as db:
        _seed_two_profiles(db)
        _seed_plan(db)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        session_id = await _full_session(client, "me")
        page = await client.get(f"/done/{session_id}?profile=me")

    assert respx.calls.call_count == 0
    assert page.status_code == 200
    assert "stored locally — VitalForge not configured" in page.text
    assert NO_TOKEN_WARNING in caplog.text
    with DbSession(get_engine(settings)) as db:
        from cadence.vitalforge.sync import job_for

        assert job_for(db, session_id).status == SKIPPED


async def test_a_404_on_activity_reads_as_will_sync_and_names_the_branch(live_db, planned, live_client) -> None:
    """PRP-06 "Done when": the expected state until JD deploys PRP-05. Not a bug on screen."""
    from cadence.vitalforge.sync import NO_ENDPOINT_ERROR, job_for

    respx.post(ACTIVITY_ME).mock(return_value=httpx.Response(404, json={"detail": "Not Found"}))

    session_id = await _full_session(live_client, "me")
    page = await live_client.get(f"/done/{session_id}?profile=me")

    assert "will sync" in page.text
    assert "sync failed" not in page.text
    job = job_for(live_db, session_id)
    assert job.last_error == NO_ENDPOINT_ERROR
    assert job.attempts == 0


async def test_together_done_files_two_activities_one_per_person(live_db, planned, live_client) -> None:
    """D-013 and D-021 on the wire: one Done, two sessions, two POSTs, two person slugs.

    The son's push rides on the setting alone, and his ``notes`` carry the next-time line without
    a ``felt:`` prefix - a shared Done finalises both checklists, but nobody gets to say how
    someone else's session felt (``_finalise_one``).
    """
    _push_son(live_db, True)
    mine = respx.post(ACTIVITY_ME).mock(return_value=httpx.Response(202, json={"id": 1}))
    his = respx.post(ACTIVITY_SON).mock(return_value=httpx.Response(202, json={"id": 2}))

    together = (await live_client.get("/api/today?profile=together")).json()["data"]
    assert together["together_group_id"]
    # Both checklists come off the Together payload. Re-rendering a solo tab would detach the
    # session from the group (D-075) and there would be nothing for one Done to finalise twice.
    for item in together["sessions"]:
        for row in item["rows"]:
            await live_client.post(f"/api/sessions/{item['session_id']}/rows/{row['position']}", json={"done": True})
    done = await live_client.post(f"/api/sessions/{together['session_id']}/done", json={"felt": "right"})
    assert done.status_code == 200
    assert len(done.json()["data"]["group"]) == 2

    parent = _recorded(mine)
    son = _recorded(his)
    assert parent["push_to_garmin"] is True
    assert "garmin_target" not in parent
    assert parent["notes"].startswith("felt: right · ")
    assert son["push_to_garmin"] is True
    assert son["garmin_target"] == "credential_person"
    assert "felt:" not in son["notes"]
    assert parent["session_id"] != son["session_id"]


async def test_the_cached_payload_is_readable_by_prp_04s_trend(live_db, vf_profiles, live_settings) -> None:
    """The D-101 seam, crossed for real.

    Writing a payload that *looks* right proves nothing: PRP-04's reader drops a point whose date
    will not parse or whose value falls outside D-102's plausible range, and a wrong division on
    this side lands as a silently empty History card rather than as a failure.
    """
    from cadence.historique.trend import body_comp_trend
    from cadence.vitalforge.client import VitalForgeClient
    from cadence.vitalforge.metrics import refresh_metrics

    today = datetime.now(UTC).astimezone().date()
    days = [(today - timedelta(days=offset)).isoformat() for offset in (10, 5, 0)]
    for name, value in (("weight", 84100), ("muscle_mass", 34500), ("body_fat", 18.2)):
        respx.get(f"http://dash.test/p/jd/api/metrics/{name}").mock(
            return_value=httpx.Response(200, json={"data": [{"date": day, "value": value} for day in days]})
        )
    for name in ("resting_hr", "sleep_score", "body_battery"):
        respx.get(f"http://dash.test/p/jd/api/metrics/{name}").mock(
            return_value=httpx.Response(200, json={"data": [{"date": days[-1], "value": 50}]})
        )
    respx.get("http://dash.test/p/jd/api/readiness").mock(
        return_value=httpx.Response(200, json={"score": 72, "status": "ok"})
    )

    await refresh_metrics(live_db, vf_profiles["me"], VitalForgeClient(live_settings))
    trend = body_comp_trend(live_db, "me")

    assert trend is not None and trend.renderable
    assert trend.has_weight_line and trend.has_body_fat_line
    assert trend.latest_weight == 84.1
    assert trend.latest_body_fat == 18.2
    assert trend.stale is False


async def test_the_job_is_written_in_the_finalisation_transaction(live_db, planned, live_settings) -> None:
    """Medium 6. A crash between "session finished" and "job queued" must be impossible.

    Rolling the transaction back is how that crash is spelled in a test: if the job were written
    by its own commit, it would survive the rollback that loses the finalisation, and the queue
    would hold a job for a session nobody finished. If it were written *after*, the finished
    session would survive and the job would not, which is the hole this fixes.
    """
    from cadence.seance.tables import SessionRecord
    from cadence.seance.today import resolve_today, view_for_session
    from cadence.vitalforge.sync import job_for
    from cadence.vitalforge.writeback import queue_session

    today = resolve_today(live_db, "me")
    record = today.primary.record
    for row in today.primary.rows:
        row.record.done = True
        live_db.add(row.record)
    record.started_at = "2026-09-06T08:00:00+00:00"
    record.finished_at = "2026-09-06T08:40:00+00:00"
    record.duration_min = 40
    live_db.add(record)

    job = queue_session(live_db, view_for_session(live_db, record).record, config=live_settings)
    assert job is not None

    live_db.rollback()

    assert job_for(live_db, record.id) is None, "the job outlived the transaction that finalised the session"
    assert live_db.get(SessionRecord, record.id).finished_at is None


async def test_a_fast_phone_clock_is_refused_at_the_boundary(planned, live_client) -> None:
    """High 1, first line of defence: a future ``ts`` never becomes a stored timestamp."""
    ahead = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    data = (await live_client.get("/api/today?profile=me")).json()["data"]

    response = await live_client.post(f"/api/sessions/{data['session_id']}/rows/1", json={"done": True, "ts": ahead})

    assert response.status_code == 422
    assert respx.calls.call_count == 0


async def test_a_future_timestamp_already_in_the_database_still_sends(live_db, planned, live_client) -> None:
    """High 1, second line: the boundary cannot reject a row that is already stored.

    A session written before that guard existed, or a clock that drifted between the first tick
    and Done, still has to produce a body VitalForge accepts — a future ``start`` is a 422, and a
    422 is terminal, so that session would never reach Garmin.
    """
    from cadence.seance.tables import SessionRecord
    from cadence.vitalforge.sync import job_for

    route = respx.post(ACTIVITY_ME).mock(return_value=httpx.Response(202, json={"id": 7}))
    data = (await live_client.get("/api/today?profile=me")).json()["data"]
    session_id = data["session_id"]
    for row in data["rows"]:
        await live_client.post(f"/api/sessions/{session_id}/rows/{row['position']}", json={"done": True})

    # The clock jumps forward after the ticks are in.
    record = live_db.get(SessionRecord, session_id)
    record.started_at = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    live_db.add(record)
    live_db.commit()

    await live_client.post(f"/api/sessions/{session_id}/done", json={"felt": "right"})

    body = _recorded(route)
    assert datetime.fromisoformat(body["start"]) <= datetime.now(UTC)
    assert body["duration_min"] >= 1
    assert job_for(live_db, session_id).status == SENT
