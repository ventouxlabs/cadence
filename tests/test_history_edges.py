"""The branches a happy path never reaches: broken plans, odd statuses, degenerate series.

None of these is reachable through the UI. They are what the History screen does on a database
that has been hand-edited, half-restored, or written by a PRP that has not shipped yet — and every
one of them has to end in a rendered page rather than a 500.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta

import httpx
from sqlalchemy import text
from sqlmodel import Session as DbSession

from cadence.config import Settings
from cadence.db import get_engine
from cadence.historique.clock import days_ago, format_day, local_date, parse_date, parse_utc
from cadence.historique.detail import session_detail
from cadence.historique.page import build_page
from cadence.historique.queries import recent_sessions
from cadence.historique.scorecard import current_streak, weekly_scorecard
from cadence.historique.sparkline import WIDTH, sparkline
from cadence.historique.trend import TrendSeries, body_comp_trend
from cadence.profils.tables import PROFILE_ME, PROFILE_SON, Profile
from cadence.programme.tables import PlannedSession
from cadence.vitalforge.tables import MetricsCache
from tests.test_historique import ROW_COUNT, _plan, _profiles, _session

assert callable(_session)

# Without the UNIQUE on ``session_id`` the test DDL in ``test_history_api`` carries, so a session
# can hold the two attempts the badge has to choose between. Architecture section 3 calls
# ``sync_job`` idempotent on ``session_id``, but the badge must not depend on that holding.
RETRYABLE_SYNC_JOB_DDL = """
CREATE TABLE sync_job (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    target TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    last_error TEXT,
    payload_json TEXT,
    remote_ref TEXT
)
"""


def _finished_pair(db: DbSession) -> tuple[str, str]:
    """Two finished sessions for the parent, newest first."""
    now = datetime.now(UTC)
    first = _session(db, _plan(db, PROFILE_ME, 1, "done"), ROW_COUNT, now.isoformat())
    second = _session(db, _plan(db, PROFILE_ME, 2, "done"), ROW_COUNT, (now - timedelta(minutes=1)).isoformat())
    return first.id, second.id


# --------------------------------------------------------------------------------- the clock


def test_clock_handles_nonsense() -> None:
    assert parse_utc(None) is None and parse_utc("") is None and parse_utc("not a time") is None
    assert local_date("garbage") is None
    assert format_day(None) == "—"
    assert days_ago(None) is None
    assert parse_date(date(2026, 9, 5)) == date(2026, 9, 5)
    assert parse_date(None) is None and parse_date(7) is None and parse_date("2026-13-99") is None


def test_naive_timestamps_are_read_as_utc() -> None:
    """Everything in this app stores UTC; guessing local would move a session between weeks."""
    parsed = parse_utc("2026-09-05T09:00:00")
    assert parsed is not None and parsed.tzinfo is UTC


def test_days_ago_never_goes_negative() -> None:
    future = datetime.now(UTC) + timedelta(days=3)
    assert days_ago(future) == 0


# ---------------------------------------------------------------------------- the sparkline


def test_sparkline_points_on_one_date_spread_by_index() -> None:
    """Two readings on the same day cannot divide by a zero date span."""
    day = date(2026, 9, 5)
    svg = sparkline(TrendSeries(weight_kg=((day, 84.0), (day, 83.0)), body_fat_pct=()))

    assert svg is not None
    drawn = svg.split('points="')[1].split('"')[0]
    assert [pair.split(",")[0] for pair in drawn.split(" ")] == ["0.00", f"{float(WIDTH):.2f}"]


def test_sparkline_says_when_the_cache_is_stale() -> None:
    points = ((date(2026, 9, 1), 84.5), (date(2026, 9, 5), 84.1))
    svg = sparkline(TrendSeries(weight_kg=points, body_fat_pct=(), stale=True))

    assert svg is not None and "cached" in svg


# --------------------------------------------------------------------------------- the trend


def _cache(db: DbSession, payload: dict) -> None:
    db.add(
        MetricsCache(
            profile_id=PROFILE_ME,
            fetched_at=datetime.now(UTC).isoformat(),
            payload_json=json.dumps(payload),
        )
    )
    db.commit()


def test_trend_rejects_values_that_are_not_numbers(db_session: DbSession) -> None:
    """``True`` is an ``int`` in Python and would plot as 1 kg. It is not a reading."""
    _profiles(db_session)
    _cache(
        db_session,
        {
            "weight_kg": [["2026-09-01", True], ["2026-09-02", None], ["2026-09-03", 84.1]],
            "body_fat_pct": [["2026-09-01"], ["2026-09-02", 18.2, 3], "junk"],
        },
    )
    trend = body_comp_trend(db_session, PROFILE_ME, today=date(2026, 9, 6))

    assert trend is not None
    assert trend.weight_kg == ((date(2026, 9, 3), 84.1),)
    assert trend.body_fat_pct == ()
    assert trend.latest_body_fat is None and trend.body_fat_label == ""


def test_trend_payload_that_is_not_an_object(db_session: DbSession) -> None:
    _profiles(db_session)
    db_session.add(
        MetricsCache(profile_id=PROFILE_ME, fetched_at=datetime.now(UTC).isoformat(), payload_json="[1, 2, 3]")
    )
    db_session.commit()

    trend = body_comp_trend(db_session, PROFILE_ME)
    assert trend is not None and not trend.renderable


def test_stale_note_without_a_usable_timestamp(db_session: DbSession) -> None:
    _profiles(db_session)
    db_session.add(MetricsCache(profile_id=PROFILE_ME, fetched_at="not a time", payload_json="{}", stale=True))
    db_session.commit()

    trend = body_comp_trend(db_session, PROFILE_ME)
    assert trend is not None and trend.stale_note() == "Last updated a while ago"


# -------------------------------------------------------------------------------- the streak


def test_an_unknown_planned_status_holds_the_streak(db_session: DbSession) -> None:
    """Only ``skipped`` resets. A status nobody in this build writes is not a failure."""
    _profiles(db_session)
    _session(db_session, _plan(db_session, PROFILE_ME, 1, "done"), ROW_COUNT)
    _plan(db_session, PROFILE_ME, 2, "abandoned")
    _session(db_session, _plan(db_session, PROFILE_ME, 3, "done"), ROW_COUNT)

    streak = current_streak(db_session, PROFILE_ME)
    assert (streak.current, streak.best) == (2, 2)


def test_a_done_row_with_no_session_holds_the_streak(db_session: DbSession) -> None:
    """D-106: nothing to derive completion from, so it neither counts nor punishes."""
    _profiles(db_session)
    _session(db_session, _plan(db_session, PROFILE_ME, 1, "done"), ROW_COUNT)
    _plan(db_session, PROFILE_ME, 2, "done")
    _session(db_session, _plan(db_session, PROFILE_ME, 3, "done"), ROW_COUNT)

    streak = current_streak(db_session, PROFILE_ME)
    assert (streak.current, streak.best) == (2, 2)


def test_a_streak_for_a_profile_that_does_not_exist(db_session: DbSession) -> None:
    assert current_streak(db_session, "ghost").current == 0
    assert recent_sessions(db_session, "ghost") == []


# --------------------------------------------------------------------------------- the page


async def test_together_names_a_profile_that_is_missing(db_session: DbSession, client: httpx.AsyncClient) -> None:
    """One seeded profile still renders a page; the other is named rather than 404ing the tab."""
    db_session.add(Profile(id=PROFILE_ME, display_name="Me", kind="adult"))
    db_session.commit()

    view = build_page(db_session, "together")
    assert view.missing == (PROFILE_SON,)

    page = await client.get("/history?profile=together")
    assert page.status_code == 200
    assert "No profile named son" in page.text


async def test_scorecard_together_carries_both_profiles(db_session: DbSession, client: httpx.AsyncClient) -> None:
    _profiles(db_session)
    body = (await client.get("/api/scorecard?profile=together")).json()

    assert body["data"]["profile"] == PROFILE_ME
    assert [item["profile"] for item in body["data"]["profiles"]] == [PROFILE_ME, PROFILE_SON]
    # D-105: no body metric on the tab the two of them share.
    assert body["data"]["trend"] is None
    assert "trend" not in body["data"]["profiles"][1]


async def test_sessions_together_merges_one_row_per_profile(db_session: DbSession, client: httpx.AsyncClient) -> None:
    _profiles(db_session)
    group = "grp-1"
    for profile_id in (PROFILE_ME, PROFILE_SON):
        _session(db_session, _plan(db_session, profile_id, 1, "done"), ROW_COUNT, group=group)

    body = (await client.get("/api/sessions?profile=together")).json()
    assert body["meta"]["profiles"] == [PROFILE_ME, PROFILE_SON]
    assert sorted(row["profile"] for row in body["data"]) == [PROFILE_ME, PROFILE_SON]
    assert all(row["together"] is True for row in body["data"])


# -------------------------------------------------------------------------------- the detail


def test_detail_of_a_plan_that_will_not_parse(db_session: DbSession) -> None:
    """A corrupt plan costs the names, not the page: the ticks are still readable."""
    _profiles(db_session)
    planned = _plan(db_session, PROFILE_ME, 1, "done")
    broken = db_session.get(PlannedSession, planned.id)
    assert broken is not None
    broken.rows_json = "{not json"
    db_session.add(broken)
    db_session.commit()
    record = _session(db_session, planned, 2)

    detail = session_detail(db_session, record.id)
    assert detail is not None
    assert len(detail.rows) == ROW_COUNT
    assert detail.rows[0].name == "goblet-squat"
    assert [row.done for row in detail.rows] == [True, True] + [False] * (ROW_COUNT - 2)
    assert detail.rows[0].measure_label == "—"


def test_detail_of_an_unknown_session(db_session: DbSession) -> None:
    assert session_detail(db_session, "nope") is None


# ------------------------------------------------------------------- ordering and formatting


def test_the_date_format_is_the_one_the_spec_names() -> None:
    """``%a %-d %b``, pinned: ``%-d`` is glibc's and would render "05" everywhere else."""
    assert format_day(date(2026, 9, 5)) == "Sat 5 Sep"
    assert format_day(date(2026, 12, 25)) == "Fri 25 Dec"


async def test_sessions_are_ordered_by_the_instant_not_the_string(
    db_session: DbSession, client: httpx.AsyncClient
) -> None:
    """Two identical instants written with different offsets must not sort by their text.

    ``2026-09-07T00:30:00+02:00`` is *earlier* than ``2026-09-06T23:00:00+00:00``, and a plain
    string comparison says the opposite. Every timestamp this build writes is UTC; a Done replayed
    by some other client need not be.
    """
    _profiles(db_session)
    _session(db_session, _plan(db_session, PROFILE_ME, 1, "done"), ROW_COUNT, "2026-09-07T00:30:00+02:00")
    newest = _session(db_session, _plan(db_session, PROFILE_ME, 2, "done"), ROW_COUNT, "2026-09-06T23:00:00+00:00")

    rows = (await client.get("/api/sessions?profile=me")).json()["data"]
    assert [row["id"] for row in rows][0] == newest.id


def test_the_streak_walks_weeks_in_order(db_session: DbSession) -> None:
    """The order is ``(week, day_index)``, not the day index alone.

    Every other streak test lives in week 1, so this is the one that would catch a walk sorting
    week 2 day 1 before week 1 day 4.
    """
    _profiles(db_session)
    for week, day_index in ((1, 3), (1, 4), (2, 1)):
        planned = _plan(db_session, PROFILE_ME, day_index, "done", week=week)
        _session(db_session, planned, ROW_COUNT)
    skipped = _plan(db_session, PROFILE_ME, 2, "skipped", week=2)
    assert skipped.status == "skipped"
    _session(db_session, _plan(db_session, PROFILE_ME, 3, "done", week=2), ROW_COUNT)

    streak = current_streak(db_session, PROFILE_ME)
    assert (streak.current, streak.best) == (1, 3)


async def test_the_stale_note_reaches_the_page(db_session: DbSession, client: httpx.AsyncClient) -> None:
    """A stale cache keeps the card and says how old it is, rather than hiding the line."""
    _profiles(db_session)
    today = datetime.now(UTC).astimezone().date()
    db_session.add(
        MetricsCache(
            profile_id=PROFILE_ME,
            fetched_at=(datetime.now(UTC) - timedelta(days=2)).isoformat(),
            payload_json=json.dumps(
                {
                    "weight_kg": [
                        [(today - timedelta(days=7)).isoformat(), 85.2],
                        [today.isoformat(), 84.1],
                    ],
                    "body_fat_pct": [
                        [(today - timedelta(days=7)).isoformat(), 19.1],
                        [today.isoformat(), 18.2],
                    ],
                }
            ),
            stale=True,
        )
    )
    db_session.commit()

    page = await client.get("/history?profile=me")
    assert page.status_code == 200
    assert 'data-role="trend"' in page.text
    assert "Last updated 2 days ago" in page.text
    assert "84.1 kg" in page.text and "18.2%" in page.text


# ------------------------------------------------------- the review's findings, pinned


def test_the_query_layer_does_not_import_the_web_package() -> None:
    """``historique`` names a day without dragging the Jinja environment in behind it.

    Asserted structurally rather than by reading the imports: the labels moved to
    ``cadence/schema/labels.py`` precisely so this stays true, and an ``import`` added for
    convenience later would put it back without anyone noticing.
    """
    probe = (
        "import sys; import cadence.historique.queries, cadence.historique.page;"
        " print('cadence.web.rendering' in sys.modules)"
    )
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "False", result.stdout

    from cadence.schema.labels import day_label as from_schema
    from cadence.web.rendering import day_label as from_web

    assert from_web is from_schema
    assert from_schema("upper_a") == "Upper A"


def test_an_orphaned_session_is_listed_and_counted_the_same_way(db_session: DbSession) -> None:
    """One definition of "a finished session", not two.

    ``seed(reset=True)`` deletes every ``planned_session`` and leaves the sessions behind. The
    list used to inner-join the plan and the counters did not, so such a session was in the
    scorecard's total and nowhere on the list under it — a number the page could not explain.
    """
    _profiles(db_session)
    planned = _plan(db_session, PROFILE_ME, 1, "done")
    record = _session(db_session, planned, ROW_COUNT)
    db_session.delete(db_session.get(PlannedSession, planned.id))
    db_session.commit()

    listed = recent_sessions(db_session, PROFILE_ME)
    card = weekly_scorecard(db_session, PROFILE_ME, date.today())

    assert [item.id for item in listed] == [record.id]
    assert len(listed) == card.total_sessions == 1
    assert listed[0].rows_done == ROW_COUNT
    # No day type left to name, so it is a session rather than a blank.
    assert listed[0].day_name == "Session"

    detail = session_detail(db_session, record.id)
    assert detail is not None and len(detail.rows) == ROW_COUNT
    assert detail.rows[0].name == "goblet-squat"


async def test_an_orphaned_session_renders(db_session: DbSession, client: httpx.AsyncClient) -> None:
    _profiles(db_session)
    planned = _plan(db_session, PROFILE_ME, 1, "done")
    _session(db_session, planned, ROW_COUNT, datetime.now(UTC).isoformat())
    db_session.delete(db_session.get(PlannedSession, planned.id))
    db_session.commit()

    page = await client.get("/history?profile=me")
    assert page.status_code == 200
    assert page.text.count('data-role="session"') == 1
    assert "1 of 4 sessions" in page.text


async def test_the_sync_badge_is_the_newest_row_for_this_app(
    db_session: DbSession, client: httpx.AsyncClient, settings: Settings
) -> None:
    """Not the alphabetically largest status, and not another target's.

    ``MAX(j.status)`` sorted "sent" above "failed" and "pending", so the badge reported the most
    reassuring answer in the table rather than the current one — exactly backwards for a screen
    whose job is saying whether a session actually left the phone.
    """
    _profiles(db_session)
    stale_then_failed, other_target = _finished_pair(db_session)
    with get_engine(settings).begin() as connection:
        connection.execute(text(RETRYABLE_SYNC_JOB_DDL))
        for job_id, session_id, target, status in (
            ("a1", stale_then_failed, "vitalforge", "sent"),
            ("a2", stale_then_failed, "vitalforge", "failed"),
            ("b1", other_target, "garmin", "sent"),
        ):
            connection.execute(
                text("INSERT INTO sync_job (id, session_id, target, status) VALUES (:i, :s, :t, :st)"),
                {"i": job_id, "s": session_id, "t": target, "st": status},
            )

    rows = {row["id"]: row["sync_status"] for row in (await client.get("/api/sessions?profile=me")).json()["data"]}
    assert rows[stale_then_failed] == "failed"
    assert rows[other_target] == "local"

    page = await client.get("/history?profile=me")
    assert "Sync failed" in page.text
    assert "Synced" not in page.text


async def test_the_legend_only_names_the_lines_that_are_drawn(db_session: DbSession, client: httpx.AsyncClient) -> None:
    """A series dropped for having one point loses its key as well as its line."""
    _profiles(db_session)
    today = datetime.now(UTC).astimezone().date()
    _cache(
        db_session,
        {
            "weight_kg": [
                [(today - timedelta(days=14)).isoformat(), 85.2],
                [(today - timedelta(days=7)).isoformat(), 84.6],
                [today.isoformat(), 84.1],
            ],
            "body_fat_pct": [[today.isoformat(), 18.2]],
        },
    )

    page = await client.get("/history?profile=me")
    assert page.text.count("<polyline") == 1
    assert "weight 84.1 kg" in page.text
    assert "body fat" not in page.text


async def test_a_card_asked_for_on_a_profile_that_does_not_exist(
    db_session: DbSession, client: httpx.AsyncClient
) -> None:
    """The refusal is the same 404 an absent id gets, whichever way the tab is wrong."""
    _profiles(db_session)
    session_id = _session(db_session, _plan(db_session, PROFILE_ME, 1, "done"), ROW_COUNT).id

    for tab in ("nobody", "ME'; DROP TABLE session; --"):
        response = await client.get(
            f"/history/sessions/{session_id}?profile={tab}&expand=1", headers={"HX-Request": "true"}
        )
        assert response.status_code == 404, tab
        assert "Goblet squat" not in response.text, tab

    assert recent_sessions(db_session, PROFILE_ME), "the table is still there"
