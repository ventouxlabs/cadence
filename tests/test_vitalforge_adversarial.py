"""PRP-06 adversarial pass: the things that go wrong quietly.

Every test here exists because the acceptance list in ``docs/prp/06-vitalforge-client.md`` names
a behaviour but the shipped tests pin something weaker, or because a reviewer asked "and what if
two of them run at once". They are grouped by the failure they are trying to catch, not by module.

Three of them are deliberately *not* the assertion the task brief asked for, and each says why in
its own docstring: the inline write-back is bounded by the client's five-second timeout and not by
one second (D-134), the Retry button belongs to the terminal state and not to the retrying one
(PRP-06 section 4.3), and there is no ``CADENCE_ENV`` in ``cadence/config.py`` to switch the
periodic loop off with, so what is testable is D-128's "it sleeps before its first pass".
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from cadence.config import Settings
from cadence.profils.tables import Setting
from cadence.seance.catalog import load_settings
from cadence.seance.tables import SessionRecord, SessionRowRecord
from cadence.seance.today import view_for_session
from cadence.vitalforge import metrics as metrics_module
from cadence.vitalforge.client import VitalForgeClient
from cadence.vitalforge.errors import VitalForgeHTTPError, VitalForgeUnavailable
from cadence.vitalforge.metrics import refresh_metrics
from cadence.vitalforge.payload import payload_for_view
from cadence.vitalforge.sync import attempt, drain, enqueue, job_for
from cadence.vitalforge.tables import FAILED, SENT, SKIPPED, MetricsCache, SyncJob
from cadence.vitalforge.writeback import LINE_RETRYING, LINE_SKIPPED, LINE_TERMINAL, sync_session
from tests.conftest import VF_TOKEN

ACTIVITY_ME = "http://weight.test/p/jd/api/activity"
ACTIVITY_SON = "http://weight.test/p/kid/api/activity"
DASH = "http://dash.test"

# The six adult metric reads, so a test can mock them all in one line.
ADULT_METRIC_NAMES = ("weight", "body_fat", "muscle_mass", "resting_hr", "sleep_score", "body_battery")

# How long a deliberately slow mocked POST takes, in seconds.
SLOW_POST_S = 0.5


def _series(value: float) -> dict:
    return {"metric": "x", "days": 30, "count": 1, "data": [{"date": "2026-09-06", "value": value}]}


def _mock_adult_reads(readiness: dict | None = None) -> list[respx.Route]:
    """Every dashboard read a full adult refresh makes, answered 200.

    The routes are handed back so a test can flip the same ones to a failure later. Registering a
    second, broader route instead would not work: respx matches in registration order, so the
    200s would keep winning and "VitalForge is down" would quietly never happen.
    """
    routes = [
        respx.get(url__startswith=f"{DASH}/p/jd/api/metrics/{name}").mock(
            return_value=httpx.Response(200, json=_series(84100.0 if name == "weight" else 20.0))
        )
        for name in ADULT_METRIC_NAMES
    ]
    routes.append(
        respx.get(f"{DASH}/p/jd/api/readiness").mock(
            return_value=httpx.Response(200, json=readiness or {"score": 72, "status": "ok"})
        )
    )
    return routes


# --------------------------------------------------------------------------- payload determinism


@pytest.fixture
def one_session(live_db, vf_profiles) -> SessionRecord:
    """A finished parent session with three rows: ticked, part-done, and untouched.

    Timestamps are pinned rather than taken from the clock, because ``start_iso`` and
    ``duration_min`` fall back to ``now()`` when both are absent and a payload built twice would
    then differ for a reason that has nothing to do with the code under test.
    """
    from cadence.programme.tables import PlannedSession, Program

    live_db.add(
        Program(
            id="prog-adv",
            profile_id="me",
            template="strength",
            start_date="2026-09-06",
            weeks=4,
            days_per_week=4,
            session_minutes=30,
        )
    )
    rows_spec = [
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
            "garmin_category": None,
            "sets": 3,
            "reps": None,
            "seconds": 45,
            "load_kg": None,
            "rest_s": 60,
            "measure": "seconds",
            "is_prelude": False,
        },
        {
            "position": 3,
            "exercise_id": "row",
            "name": "Row",
            "garmin_category": "ROW",
            "sets": 3,
            "reps": 12,
            "seconds": None,
            "load_kg": 15.0,
            "rest_s": 60,
            "measure": "reps",
            "is_prelude": False,
        },
    ]
    live_db.add(
        PlannedSession(
            id="plan-adv",
            program_id="prog-adv",
            profile_id="me",
            week=1,
            day_index=1,
            day_type="lower_a",
            workout_id="lower-a",
            rows_json=json.dumps(rows_spec),
        )
    )
    record = SessionRecord(
        id="adv-1",
        profile_id="me",
        planned_session_id="plan-adv",
        started_at="2026-09-06T08:00:00+00:00",
        finished_at="2026-09-06T08:42:00+00:00",
        felt="right",
    )
    live_db.add(record)
    for spec in rows_spec:
        live_db.add(
            SessionRowRecord(
                id=f"adv-1-{spec['position']}",
                session_id="adv-1",
                position=spec["position"],
                exercise_id=spec["exercise_id"],
                sets_planned=spec["sets"],
                reps_planned=spec["reps"],
                seconds_planned=spec["seconds"],
                load_planned_kg=spec["load_kg"],
            )
        )
    live_db.commit()
    return record


def _mark(live_db, position: int, **fields) -> None:
    row = live_db.get(SessionRowRecord, f"adv-1-{position}")
    for name, value in fields.items():
        setattr(row, name, value)
    live_db.add(row)
    live_db.commit()


def _payload(live_db, record: SessionRecord) -> dict:
    view = view_for_session(live_db, record)
    assert view is not None
    return payload_for_view(view, load_settings(live_db))


def test_a_partial_session_sends_the_done_row_and_the_part_done_row_only(live_db, one_session) -> None:
    """Row 1 ticked, row 2 part-logged, row 3 untouched: two entries, in position order.

    ``sets_done > 0`` counts even without the tick, because a person who logged two of three sets
    and walked away did the work; the untouched row did not happen and a phantom entry in the
    payload is a lie Garmin then keeps.
    """
    _mark(live_db, 1, done=True)
    _mark(live_db, 2, sets_done=2)

    body = _payload(live_db, one_session)

    assert body["exercises"] == [
        {"name": "Goblet Squat", "garmin_category": "SQUAT", "sets": 3, "reps": 10, "weight_kg": 20.0, "rest_s": 90},
        # No ``garmin_category``: the spec's is ``None``, and "UNKNOWN" is not a Garmin category
        # (D-018). ``sets`` is the two that were logged, not the three that were planned.
        {"name": "Plank", "sets": 2, "reps": 1, "seconds": 45, "rest_s": 60},
    ]
    assert "UNKNOWN" not in json.dumps(body)


def test_a_rebuilt_payload_is_byte_identical(live_db, one_session) -> None:
    """A re-POST must send the same bytes, or VitalForge's dedup logs a spurious conflict.

    Contract section 4.5 is first-write-wins and warns when a repeat body differs materially, so
    a payload that drifts between builds turns every offline replay into a warning about fields
    nobody changed.
    """
    _mark(live_db, 1, done=True)

    first = json.dumps(_payload(live_db, one_session), sort_keys=True)
    second = json.dumps(_payload(live_db, one_session), sort_keys=True)

    assert first == second


def test_the_stored_payload_does_not_change_when_the_setting_is_toggled_afterwards(live_db, vf_profiles) -> None:
    """D-042. ``push_to_garmin`` is decided on the first post and never back-filled.

    A session first stored with the push off can never be pushed by a later POST - VitalForge's
    retry set is ``('pending','failed')`` and a skipped one is not in it - and D-041 means
    re-posting it with the flag flipped answers 409 rather than the dedup 200. So the enqueued
    payload has to stay exactly what was sent.
    """
    record = SessionRecord(id="son-1", profile_id="son", planned_session_id="p", started_at="2026-09-06T08:00:00+00:00")
    live_db.add(record)
    live_db.commit()
    job = enqueue(live_db, "son-1", {"session_id": "son-1", "push_to_garmin": False})

    live_db.merge(
        Setting(key="push_son_to_garmin", value_json=json.dumps(True), updated_at="2026-09-06T00:00:00+00:00")
    )
    live_db.commit()
    again = enqueue(
        live_db, "son-1", {"session_id": "son-1", "push_to_garmin": True, "garmin_target": "credential_person"}
    )

    assert again.id == job.id
    stored = json.loads(again.payload_json)
    assert stored["push_to_garmin"] is False
    assert "garmin_target" not in stored


# ------------------------------------------------------------------------------ the retry queue


@pytest.fixture
def queued(live_db, vf_profiles) -> SyncJob:
    """One pending job for the parent, ready to be drained."""
    live_db.add(
        SessionRecord(id="q-1", profile_id="me", planned_session_id="p", started_at="2026-09-06T08:00:00+00:00")
    )
    live_db.commit()
    body = {"session_id": "q-1", "duration_min": 1, "exercises": [{"name": "X", "sets": 1, "reps": 1}]}
    return enqueue(live_db, "q-1", body)


@respx.mock
async def test_two_concurrent_drains_leave_one_sent_job(live_settings: Settings, live_app, queued) -> None:
    """Two drains over the same queue converge on one ``sent`` job with one attempt counted.

    Two database sessions on the same file, which is what the file-backed fixture exists for.
    There is no in-flight claim on ``sync_job``: ``due_jobs`` filters on status and schedule only,
    so both drains can select the same row and both can POST. VitalForge absorbs that - contract
    section 4.5 dedups on ``session_id`` and D-048 claims the Garmin push inside the transaction -
    which is why this test pins the *job* state rather than the request count.
    """
    from sqlmodel import Session as DbSession

    from cadence.db import get_engine

    async def slow(request: httpx.Request) -> httpx.Response:
        # The point of the test. Without a real suspension inside the POST the first drain runs to
        # completion before the second is scheduled, the job is already ``sent``, and the race the
        # test is named after never happens.
        await asyncio.sleep(0.05)
        return httpx.Response(202, json={"id": 7})

    route = respx.post(ACTIVITY_ME).mock(side_effect=slow)
    engine = get_engine(live_settings)

    async def one_drain() -> None:
        with DbSession(engine) as db:
            await drain(db, VitalForgeClient(live_settings))

    await asyncio.gather(one_drain(), one_drain())

    with DbSession(engine) as db:
        job = job_for(db, "q-1")
        assert job is not None
        assert job.status == SENT
        assert job.next_attempt_at is None
        assert job.attempts == 1, "a job attempted twice would count two attempts against the budget"
    # Cadence does not deduplicate concurrent drains: ``due_jobs`` filters on status and schedule
    # and nothing marks a row in flight, so both passes select the row and both POST. What keeps
    # that safe is on the far side - contract section 4.5 dedups on ``session_id`` and D-048
    # claims the Garmin push inside the transaction - which holds only while every POST carries
    # the same key. That is the invariant worth pinning here.
    sent_ids = {json.loads(call.request.content)["session_id"] for call in route.calls}
    assert sent_ids == {"q-1"}, "a concurrent drain must never invent a second session id"


@respx.mock
async def test_a_connection_error_is_retried_and_not_terminal(live_db, queued, live_settings: Settings) -> None:
    """A refused connection is a transport failure, not a rejected payload.

    Distinct from the timeout case: ``httpx`` raises a different class, and the client has to fold
    both into ``VitalForgeUnavailable`` so the queue schedules a retry rather than giving up on a
    session because a container was restarting.
    """
    respx.post(ACTIVITY_ME).mock(side_effect=httpx.ConnectError("connection refused"))

    job = await attempt(live_db, queued, VitalForgeClient(live_settings))

    assert job.status == FAILED
    assert job.attempts == 1
    assert job.next_attempt_at is not None
    assert "ConnectError" in (job.last_error or "")
    assert VF_TOKEN not in (job.last_error or "")


# ------------------------------------------------------------------------------- the youth cache


@respx.mock
async def test_the_cached_row_for_the_son_holds_no_body_composition_at_all(
    live_db, vf_profiles, live_settings: Settings
) -> None:
    """Even when VitalForge answers with weight, the son's stored JSON never contains it.

    The read path already filters (``_latest_of``), which means a template bug is caught but a
    second reader of ``metrics_cache`` - a future export, a debug dump - would still find the
    numbers. This asserts on the raw column: the son's body composition is not fetched, so it is
    not in the row to leak.
    """
    for name in ADULT_METRIC_NAMES:
        respx.get(url__startswith=f"{DASH}/p/kid/api/metrics/{name}").mock(
            return_value=httpx.Response(200, json=_series(84100.0))
        )
    respx.get(f"{DASH}/p/kid/api/readiness").mock(
        return_value=httpx.Response(200, json={"score": None, "status": "insufficient_data"})
    )

    await refresh_metrics(live_db, vf_profiles["son"], VitalForgeClient(live_settings))

    row = live_db.get(MetricsCache, "son")
    assert row is not None
    raw = row.payload_json
    for forbidden in ("weight_kg", "body_fat", "muscle_mass", "84100", "84.1"):
        assert forbidden not in raw, f"the son's cached payload carries {forbidden!r}"


@respx.mock
async def test_a_seven_hour_old_cache_is_replaced_by_a_refresh(live_db, vf_profiles, live_settings: Settings) -> None:
    """Stale on read, fresh after the periodic pass. The row is replaced, not appended to."""
    old = (datetime.now(UTC) - timedelta(hours=7)).isoformat()
    stale_payload = json.dumps({"latest": {"weight_kg": 1.0}})
    live_db.merge(MetricsCache(profile_id="me", fetched_at=old, payload_json=stale_payload, stale=False))
    live_db.commit()
    assert metrics_module.read_cached(live_db, vf_profiles["me"]).stale is True

    _mock_adult_reads()
    refreshed = await refresh_metrics(live_db, vf_profiles["me"], VitalForgeClient(live_settings))

    assert refreshed.stale is False
    assert refreshed.latest["weight_kg"] == 84.1
    assert metrics_module.read_cached(live_db, vf_profiles["me"]).stale is False


@respx.mock
async def test_when_vitalforge_is_down_the_api_still_serves_the_last_good_numbers(
    live_db, vf_profiles, live_settings: Settings, live_client
) -> None:
    """A total read failure keeps the previous payload and flags it, all the way to the route."""
    routes = _mock_adult_reads()
    await refresh_metrics(live_db, vf_profiles["me"], VitalForgeClient(live_settings))

    for route in routes:
        route.mock(side_effect=httpx.ConnectError("down"))
    await refresh_metrics(live_db, vf_profiles["me"], VitalForgeClient(live_settings))

    data = (await live_client.get("/api/metrics?profile=me")).json()["data"]

    assert data["stale"] is True
    assert data["weight_kg"] == 84.1, "a failed refresh must never blank the last good numbers"


# ------------------------------------------------------------------------------- the token, ever


@respx.mock
async def test_the_token_never_reaches_a_log_even_at_debug(live_settings: Settings, caplog) -> None:
    """Success, a 500 and a timeout, with the root logger turned all the way down.

    The shipped test runs at the default level; DEBUG is where a library that logs its own
    requests would surface, and ``httpx`` does exactly that.
    """
    caplog.set_level(logging.DEBUG)
    logging.getLogger().setLevel(logging.DEBUG)
    logging.getLogger("httpx").setLevel(logging.DEBUG)
    logging.getLogger("httpcore").setLevel(logging.DEBUG)
    client = VitalForgeClient(live_settings)

    respx.post(ACTIVITY_ME).mock(return_value=httpx.Response(202, json={"id": 1}))
    await client.post_activity("jd", {"session_id": "x"})
    respx.get(f"{DASH}/p/jd/api/readiness").mock(return_value=httpx.Response(500, text=f"Bearer {VF_TOKEN} exploded"))
    with pytest.raises(VitalForgeHTTPError):
        await client.get_readiness("jd")
    respx.get(url__startswith=f"{DASH}/p/jd/api/metrics").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(VitalForgeUnavailable):
        await client.get_metric("jd", "weight")

    assert VF_TOKEN not in caplog.text
    assert "Bearer " not in caplog.text.replace("Bearer [redacted]", "")


def test_no_rendering_of_settings_shows_the_token(live_settings: Settings) -> None:
    """``repr``, ``str``, and the JSON dump. A ``SecretStr`` that leaks through any of them is
    one ``logger.info("settings=%s", settings)`` away from a token in a log file."""
    renderings = (repr(live_settings), str(live_settings), live_settings.model_dump_json())

    for text in renderings:
        assert VF_TOKEN not in text
    assert VF_TOKEN not in repr(live_settings.vitalforge_token)


@respx.mock
async def test_neither_the_health_route_the_metrics_route_nor_the_done_page_shows_the_token(
    live_db, vf_profiles, live_client
) -> None:
    """Three surfaces a browser can reach, one assertion each. Zero VitalForge calls between them."""
    live_db.add(
        SessionRecord(
            id="t-1",
            profile_id="me",
            planned_session_id="p",
            started_at="2026-09-06T08:00:00+00:00",
            finished_at="2026-09-06T08:30:00+00:00",
        )
    )
    live_db.commit()

    for path in ("/api/health", "/api/metrics?profile=me", "/done/t-1?profile=me"):
        response = await live_client.get(path)
        assert response.status_code in (200, 404), path
        assert VF_TOKEN not in response.text, path
        assert "Bearer" not in response.text, path
    assert not respx.calls, "none of these three routes may talk to VitalForge"


# --------------------------------------------------------------------------- the hot path is dry


@respx.mock
async def test_today_and_the_today_api_open_no_socket(live_db, one_session, live_client) -> None:
    """Architecture section 5. Today is the hot path and the nudge comes from the cache.

    A positive assertion on ``respx.calls`` rather than a reliance on ``assert_all_mocked``
    raising: ``httpx.ASGITransport`` is not intercepted, so the app's own request records nothing
    and any VitalForge call would show up here as the only entry.
    """
    live_db.merge(
        MetricsCache(
            profile_id="me",
            fetched_at=datetime.now(UTC).isoformat(),
            payload_json=json.dumps({"latest": {"weight_kg": 84.1}, "readiness": {"score": 72, "status": "ok"}}),
            stale=False,
        )
    )
    live_db.commit()

    assert (await live_client.get("/api/today?profile=me")).status_code == 200
    assert (await live_client.get("/today?profile=me")).status_code == 200
    assert (await live_client.get("/api/metrics?profile=me")).status_code == 200

    assert not respx.calls, f"Today reached VitalForge: {[c.request.url for c in respx.calls]}"


# ------------------------------------------------------------------------------ the Done screen


@respx.mock
async def test_a_retry_for_a_session_that_does_not_exist_is_a_404(live_db, vf_profiles, live_client) -> None:
    """The Retry button addresses a session by id, and an id nobody owns is not a blank line.

    Answering 200 with "Stored locally." would tell a person their session is safe on a device
    that has never heard of it. ``GET /done/{id}`` already 404s on the same id; this makes the
    write agree with the read.
    """
    response = await live_client.post("/done/not-a-session/retry?profile=me")

    assert response.status_code == 404
    assert not respx.calls


@respx.mock
def test_the_inline_attempt_is_bounded_by_one_client_timeout(live_db, one_session, live_settings: Settings) -> None:
    """A hanging VitalForge costs Done one five-second timeout, not eight retries.

    The task brief asked for "under one second". That is not the design: PRP-06 risk 10 and
    ``client.TIMEOUT_S`` both fix the inline bound at five seconds, and D-134 puts the Together
    worst case at two of them. What is actually guaranteed - and what breaks loudly if somebody
    puts the retry loop inline - is **one** attempt, bounded by **one** timeout.

    Synchronous on purpose. ``POST /today/{id}/done`` is a ``def`` route, so FastAPI runs it in a
    worker thread with no event loop and ``writeback.run_blocking`` reaches ``asyncio.run``; an
    ``async`` test would find a running loop, take the D-122 bail-out branch, and assert nothing.
    """
    _mark(live_db, 1, done=True)

    async def slow(request: httpx.Request) -> httpx.Response:
        # Half a second stands in for the five-second timeout: what is being measured is how many
        # of them Done spends, and a real five would only make the suite slower to say the same
        # thing. Eight attempts inline would show up here as eight multiples.
        await asyncio.sleep(SLOW_POST_S)
        raise httpx.ReadTimeout("VitalForge never answered")

    respx.post(ACTIVITY_ME).mock(side_effect=slow)

    started = time.monotonic()
    job = sync_session(live_db, one_session, config=live_settings)
    elapsed = time.monotonic() - started

    assert job is not None
    assert job.attempts == 1, "Done must attempt once and hand the rest to the periodic drain"
    assert respx.calls.call_count == 1
    assert elapsed < SLOW_POST_S * 3, f"Done spent {elapsed:.2f}s, which is more than one attempt"
    assert job.next_attempt_at is not None, "and the session is still queued for the periodic drain"


@respx.mock
async def test_a_together_done_with_one_side_failing_leaves_the_other_sent(
    live_db, vf_profiles, live_settings: Settings
) -> None:
    """Two people, two jobs, two independent outcomes. One failure never strands the other.

    respx discriminates on the slug in the path - ``jd`` against ``kid`` - which is the same thing
    that makes the two profiles two VitalForge persons in the first place (contract section 4.3).
    """
    respx.post(ACTIVITY_ME).mock(return_value=httpx.Response(202, json={"id": 7}))
    respx.post(ACTIVITY_SON).mock(return_value=httpx.Response(500, text="dashboard on fire"))
    records = []
    for key, slug in (("me", "jd"), ("son", "kid")):
        record = SessionRecord(
            id=f"tog-{key}",
            profile_id=key,
            planned_session_id=f"p-{key}",
            started_at="2026-09-06T08:00:00+00:00",
            finished_at="2026-09-06T08:30:00+00:00",
            together_group_id="g-1",
        )
        live_db.add(record)
        records.append(record)
        assert slug  # the fixture's slugs are what the two routes above key on
    live_db.commit()
    for record in records:
        enqueue(
            live_db,
            record.id,
            {"session_id": record.id, "duration_min": 30, "exercises": [{"name": "X", "sets": 1, "reps": 1}]},
        )

    await drain(live_db, VitalForgeClient(live_settings))

    assert job_for(live_db, "tog-me").status == SENT
    son = job_for(live_db, "tog-son")
    assert son.status == FAILED
    assert son.next_attempt_at is not None, "a 500 on one side is retryable, not terminal"


# ------------------------------------------------------------------- startup, and what is not on


async def test_the_app_starts_without_waiting_on_vitalforge(live_settings: Settings) -> None:
    """D-128: the periodic task sleeps before its first pass, so boot costs no VitalForge call.

    The task brief asked for a ``CADENCE_ENV=test`` switch that turns the loop off. There is no
    such field in ``cadence/config.py`` and the lifespan creates the task unconditionally; the
    guarantee that actually exists is this one, plus ``httpx.ASGITransport`` never running the
    lifespan at all, which is why the rest of the suite never sees the loop.
    """
    from cadence.main import create_app

    app = create_app(live_settings)
    started = time.monotonic()
    # Starlette's own lifespan context, so no extra dependency stands between the test and the
    # thing it is asserting about.
    async with app.router.lifespan_context(app):
        boot = time.monotonic() - started
    assert boot < 2.0, f"startup took {boot:.1f}s; nothing on the boot path may call VitalForge"
    assert not respx.calls


def test_mock_mode_is_never_the_default(live_settings: Settings) -> None:
    """``CADENCE_VITALFORGE_MODE`` defaults to ``live`` and ``.env.example`` agrees.

    Mock mode outranks a blank token (D-121), so a deployment that fell into it would report
    every session as synced while nothing left the box.
    """
    from pathlib import Path

    from cadence.config import Settings as RealSettings

    assert RealSettings.model_fields["vitalforge_mode"].default == "live"
    example = (Path(__file__).resolve().parents[1] / ".env.example").read_text()
    assert "CADENCE_VITALFORGE_MODE=live" in example
    assert "CADENCE_VITALFORGE_MODE=mock" not in example


# ------------------------------------------------------------------------- the five status lines


def test_a_failed_job_with_a_schedule_offers_no_retry_button(live_db) -> None:
    """PRP-06 section 4.3. The button belongs to the terminal state alone.

    The task brief pairs "sync failed (retrying)" with a working button; the spec does not, and
    for a good reason - a job that is already going to retry does not need a person to ask it to,
    and a button there invites the tight loop D-016 exists to prevent.
    """
    from cadence.vitalforge.writeback import line_for

    retrying = SyncJob(
        id="a",
        session_id="a",
        status=FAILED,
        attempts=2,
        next_attempt_at=(datetime.now(UTC) + timedelta(minutes=4)).isoformat(),
    )
    terminal = SyncJob(id="b", session_id="b", status=FAILED, attempts=8, next_attempt_at=None)

    assert line_for(retrying).text == LINE_RETRYING
    assert line_for(retrying).retry is False
    assert line_for(terminal).text == LINE_TERMINAL
    assert line_for(terminal).retry is True
    assert line_for(SyncJob(id="c", session_id="c", status=SKIPPED)).text == LINE_SKIPPED
