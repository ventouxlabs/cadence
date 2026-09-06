"""``done_this_week`` against a frozen clock, and the plan it is counted against.

``weekly_scorecard`` takes a ``today``, and the arithmetic is already pinned that way. What was
untested is the seam the *routes* use: ``today=None``, which asks the real clock. So the clock is
frozen here rather than the date passed in, and every assertion goes through ``GET /history`` or
``GET /api/scorecard``.

The instants are chosen twenty seconds either side of a local midnight, and once inside a week
that is twenty-five hours long, because that is where a UTC/local mix-up shows up and nowhere
else (PRP-04 risk 4). Europe/Paris is the household's zone (D-004's deployment).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, date, datetime

import httpx
import pytest
from sqlmodel import Session as DbSession

from cadence.historique import scorecard as scorecard_module
from cadence.historique import trend as trend_module
from cadence.historique.scorecard import MAX_DOTS, Scorecard, weekly_scorecard
from cadence.profils.tables import PROFILE_ME, Setting
from tests.test_historique import ROW_COUNT, _plan, _profiles, _session

PARIS = "Europe/Paris"

# Sunday 6 September 2026 23:59:50 and Monday 7 September 00:00:05, both local, twenty seconds
# apart across the week boundary.
SUNDAY_LATE = "2026-09-06T21:59:50+00:00"
MONDAY_EARLY = "2026-09-06T22:00:05+00:00"
FROZEN_MONDAY = datetime(2026, 9, 6, 22, 0, 10, tzinfo=UTC)  # Monday 00:00:10 local
FROZEN_SUNDAY = datetime(2026, 9, 6, 21, 59, 50, tzinfo=UTC)  # Sunday 23:59:50 local

# The week of Monday 19 October 2026 runs into the night the clocks go back: 03:00 CEST becomes
# 02:00 CET at 01:00 UTC on Sunday the 25th, so that Sunday is twenty-five hours long.
DST_BEFORE = "2026-10-25T00:30:00+00:00"  # Sunday 02:30 CEST
DST_AFTER = "2026-10-25T02:30:00+00:00"  # Sunday 03:30 CET, after the switch
DST_NEXT_WEEK = "2026-10-25T23:30:00+00:00"  # Monday 00:30 CET
FROZEN_DST_SUNDAY = datetime(2026, 10, 25, 22, 0, tzinfo=UTC)  # Sunday 23:00 local
FROZEN_DST_MONDAY = datetime(2026, 10, 25, 23, 40, tzinfo=UTC)  # Monday 00:40 local


def freeze(monkeypatch: pytest.MonkeyPatch, instant: datetime) -> None:
    """Pin every clock the History page reads to one instant.

    Both modules are frozen together even where a test only asserts the weekly count: the trend
    keeps its own thirty-day window off ``datetime.now``, and a half-frozen page is one that can
    pass for the wrong reason.
    """

    class _Date(date):
        @classmethod
        def today(cls) -> date:
            return instant.astimezone().date()

    class _Datetime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:  # type: ignore[override]
            return instant.astimezone(tz) if tz is not None else instant.astimezone()

    monkeypatch.setattr(scorecard_module, "date", _Date)
    monkeypatch.setattr(trend_module, "datetime", _Datetime)


def _two_sessions_across_midnight(db: DbSession) -> None:
    _session(db, _plan(db, PROFILE_ME, 1, "done"), ROW_COUNT, SUNDAY_LATE)
    _session(db, _plan(db, PROFILE_ME, 2, "done"), ROW_COUNT, MONDAY_EARLY)


# --------------------------------------------------------------- 7: the ISO week boundary


async def test_the_week_starts_at_local_monday_midnight(
    db_session: DbSession,
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    tz: Callable[[str], None],
) -> None:
    """Frozen ten seconds into Monday, only the session five seconds old is this week's."""
    tz(PARIS)
    _profiles(db_session)
    _two_sessions_across_midnight(db_session)
    freeze(monkeypatch, FROZEN_MONDAY)

    data = (await client.get("/api/scorecard?profile=me")).json()["data"]
    assert data["week_start"] == "2026-09-07"
    assert data["done_this_week"] == 1
    assert data["total_sessions"] == 2

    page = await client.get("/history?profile=me")
    assert "1 of 4 sessions" in page.text


async def test_a_sunday_night_session_belongs_to_the_week_just_lived(
    db_session: DbSession,
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    tz: Callable[[str], None],
) -> None:
    """Frozen ten seconds earlier, the same two sessions swap weeks and neither is lost."""
    tz(PARIS)
    _profiles(db_session)
    _two_sessions_across_midnight(db_session)
    freeze(monkeypatch, FROZEN_SUNDAY)

    data = (await client.get("/api/scorecard?profile=me")).json()["data"]
    assert data["week_start"] == "2026-08-31"
    assert data["done_this_week"] == 1
    assert data["total_sessions"] == 2


@pytest.mark.parametrize(
    ("frozen", "week_start", "done"),
    [(FROZEN_DST_SUNDAY, "2026-10-19", 2), (FROZEN_DST_MONDAY, "2026-10-26", 1)],
    ids=["the long sunday", "the monday after"],
)
async def test_the_week_the_clocks_go_back(
    db_session: DbSession,
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    tz: Callable[[str], None],
    frozen: datetime,
    week_start: str,
    done: int,
) -> None:
    """Two sessions either side of the switch are the same local Sunday, and count together.

    The offsets differ (+02:00 before, +01:00 after) so a count that compared UTC dates, or that
    added a fixed offset, would split them across two weeks.
    """
    tz(PARIS)
    _profiles(db_session)
    for index, stamp in enumerate((DST_BEFORE, DST_AFTER, DST_NEXT_WEEK), start=1):
        _session(db_session, _plan(db_session, PROFILE_ME, index, "done"), ROW_COUNT, stamp)
    freeze(monkeypatch, frozen)

    data = (await client.get("/api/scorecard?profile=me")).json()["data"]
    assert data["week_start"] == week_start
    assert data["done_this_week"] == done
    assert data["total_sessions"] == 3

    rows = (await client.get("/api/sessions?profile=me")).json()["data"]
    assert [row["date"] for row in rows] == ["2026-10-26", "2026-10-25", "2026-10-25"]


async def test_the_page_and_the_json_agree_on_the_week(
    db_session: DbSession,
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    tz: Callable[[str], None],
) -> None:
    """The HTML and the JSON read the same clock; a drift between them is a bug nobody would see."""
    tz(PARIS)
    _profiles(db_session)
    _two_sessions_across_midnight(db_session)
    freeze(monkeypatch, FROZEN_MONDAY)

    data = (await client.get("/api/scorecard?profile=me")).json()["data"]
    page = await client.get("/history?profile=me")
    assert f"{data['done_this_week']} of {data['planned_this_week']} sessions" in page.text
    assert f"Streak {data['current_streak']} · Best {data['best_streak']}" in page.text


# ------------------------------------------------- 8: the plan the count is measured against


def _setting(db: DbSession, key: str, value: object) -> None:
    db.add(Setting(key=key, value_json=json.dumps(value), updated_at=datetime.now(UTC).isoformat()))
    db.commit()


async def test_days_per_week_changed_mid_week(
    db_session: DbSession, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, tz: Callable[[str], None]
) -> None:
    """Three sessions into a four-day week, the plan drops to two. The answer is ``3 of 2``.

    ``planned_this_week`` is the setting as it stands now, not as it stood when the week began, so
    the count can exceed it the moment the plan shrinks. Clamping would report ``2 of 2`` and hide
    a session the user actually did.
    """
    tz(PARIS)
    _profiles(db_session)
    for index in range(1, 4):
        _session(db_session, _plan(db_session, PROFILE_ME, index, "done"), ROW_COUNT, MONDAY_EARLY)
    freeze(monkeypatch, FROZEN_MONDAY)

    before = (await client.get("/api/scorecard?profile=me")).json()["data"]
    assert (before["done_this_week"], before["planned_this_week"]) == (3, 4)

    _setting(db_session, "days_per_week", 2)

    after = (await client.get("/api/scorecard?profile=me")).json()["data"]
    assert (after["done_this_week"], after["planned_this_week"], after["days_per_week"]) == (3, 2, 2)

    page = await client.get("/history?profile=me")
    assert "3 of 2 sessions" in page.text
    # One dot per session actually done, never fewer than the plan asked for.
    assert page.text.count('class="dot dot-on"') == 3
    assert page.text.count('class="dot"') == 0


async def test_a_corrupt_days_per_week_falls_back_rather_than_planning_zero(
    db_session: DbSession, client: httpx.AsyncClient
) -> None:
    """A hand-edited ``0`` is not a plan. The stored settings are rejected whole and the shipped
    default stands, so the page never divides a week by nothing."""
    _profiles(db_session)
    _session(db_session, _plan(db_session, PROFILE_ME, 1, "done"), ROW_COUNT)
    _setting(db_session, "days_per_week", 0)

    data = (await client.get("/api/scorecard?profile=me")).json()["data"]
    assert data["planned_this_week"] == 4

    page = await client.get("/history?profile=me")
    assert page.status_code == 200


def test_a_week_with_nothing_planned_still_renders(db_session: DbSession) -> None:
    """``planned_this_week == 0`` is unreachable through settings, and harmless if it ever is."""
    _profiles(db_session)
    empty = Scorecard(
        profile_id=PROFILE_ME,
        week_start=date(2026, 9, 7),
        done_this_week=0,
        planned_this_week=0,
        days_per_week=0,
        current_streak=0,
        best_streak=0,
        total_sessions=0,
        total_rows_ticked=0,
    )
    assert empty.headline == "0 of 0"
    assert empty.dots == []

    card = weekly_scorecard(db_session, PROFILE_ME, date(2026, 9, 7))
    assert (card.done_this_week, card.total_sessions, card.total_rows_ticked) == (0, 0, 0)


def test_the_dot_row_is_capped_for_the_layout(db_session: DbSession) -> None:
    """A very good week must not wrap a row of dots off a 390 px screen."""
    _profiles(db_session)
    for index in range(1, MAX_DOTS + 4):
        _session(db_session, _plan(db_session, PROFILE_ME, index, "done"), ROW_COUNT)

    card = weekly_scorecard(db_session, PROFILE_ME, date(2026, 9, 7))
    assert card.done_this_week == MAX_DOTS + 3
    assert card.headline == f"{MAX_DOTS + 3} of 4"
    assert len(card.dots) == MAX_DOTS and all(card.dots)
