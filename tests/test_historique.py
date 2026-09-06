"""PRP-04 acceptance tests 1-13: the streak walk, the weekly counts, and the sparkline.

The streak is the number every other screen will quote (PRP-07's missed-session logic, PRP-10's
badges), so each of its six rules gets its own test rather than one composite. The fixtures write
``planned_session`` and ``session`` rows directly: a streak over a skipped day and a partial
session is not reachable through the UI in one test, and the arithmetic is what is under test.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

from sqlmodel import Session as DbSession

from cadence.historique.scorecard import current_streak, weekly_scorecard
from cadence.historique.sparkline import HEIGHT, WIDTH, sparkline
from cadence.historique.trend import TrendSeries, body_comp_trend
from cadence.profils.tables import PROFILE_ME, PROFILE_SON, Profile
from cadence.programme.tables import PlannedSession
from cadence.seance.catalog import band_rules
from cadence.seance.status import good_enough_after
from cadence.seance.tables import SessionRecord, SessionRowRecord
from cadence.vitalforge.tables import MetricsCache

ROW_COUNT = 6
# The son's band (no recorded age, so the strictest one) promotes at three ticked exercises.
YOUTH_THRESHOLD = 3
FINISHED = "2026-09-07T09:00:00+00:00"
# The Monday of ``FINISHED``'s ISO week, for every scorecard assertion that pins a week.
WEEK_MONDAY = date(2026, 9, 7)


def _profiles(db: DbSession) -> None:
    db.add(Profile(id=PROFILE_ME, display_name="Me", kind="adult"))
    db.add(Profile(id=PROFILE_SON, display_name="Son", kind="youth", age_years=None))
    db.commit()


def _rows_json() -> str:
    return json.dumps(
        [
            {"position": index, "exercise_id": "goblet-squat", "name": "Goblet squat", "reps": 8, "sets": 3}
            for index in range(1, ROW_COUNT + 1)
        ]
    )


def _plan(db: DbSession, profile_id: str, day_index: int, status: str, week: int = 1) -> PlannedSession:
    planned = PlannedSession(
        id=f"{profile_id}-{week}-{day_index}",
        program_id=f"prog-{profile_id}",
        profile_id=profile_id,
        week=week,
        day_index=day_index,
        day_type="upper_a",
        workout_id="upper-a",
        rows_json=_rows_json(),
        status=status,
    )
    db.add(planned)
    db.commit()
    return planned


def _session(
    db: DbSession,
    planned: PlannedSession,
    ticks: int,
    finished_at: str | None = FINISHED,
    group: str | None = None,
) -> SessionRecord:
    record = SessionRecord(
        id=str(uuid.uuid4()),
        profile_id=planned.profile_id,
        planned_session_id=planned.id,
        started_at=finished_at,
        finished_at=finished_at,
        duration_min=24,
        felt="right",
        together_group_id=group,
    )
    db.add(record)
    for position in range(1, ROW_COUNT + 1):
        db.add(
            SessionRowRecord(
                id=f"{record.id}-{position}",
                session_id=record.id,
                position=position,
                exercise_id="goblet-squat",
                done=position <= ticks,
                done_at=finished_at if position <= ticks else None,
            )
        )
    db.commit()
    return record


def _day(db: DbSession, profile_id: str, index: int, status: str, ticks: int, finished_at: str = FINISHED) -> None:
    """One planned day, with the session behind it when the day was actually worked."""
    planned = _plan(db, profile_id, index, status)
    if status == "done":
        _session(db, planned, ticks, finished_at)


# ------------------------------------------------------------------ 1-6: the streak rules


def test_streak_simple(db_session: DbSession) -> None:
    _profiles(db_session)
    for index in range(1, 6):
        _day(db_session, PROFILE_ME, index, "done", ROW_COUNT)

    streak = current_streak(db_session, PROFILE_ME)
    assert (streak.current, streak.best) == (5, 5)


def test_streak_reset_on_skip(db_session: DbSession) -> None:
    """A skipped day resets. The best the walk reached is still remembered."""
    _profiles(db_session)
    _day(db_session, PROFILE_ME, 1, "done", ROW_COUNT)
    _day(db_session, PROFILE_ME, 2, "done", ROW_COUNT)
    _day(db_session, PROFILE_ME, 3, "skipped", 0)
    _day(db_session, PROFILE_ME, 4, "done", ROW_COUNT)

    streak = current_streak(db_session, PROFILE_ME)
    assert (streak.current, streak.best) == (1, 2)


def test_streak_holds_on_partial(db_session: DbSession) -> None:
    """A short session neither increments nor resets (principles section 3.7).

    Driven off the son, because the adult threshold is one tick: on an adult profile every
    finished session is complete and "partial" is not reachable at all.
    """
    _profiles(db_session)
    # The reason this test is driven off the son, stated as an assertion rather than as prose:
    # move the adult threshold off 1 and a "partial" adult session becomes reachable, at which
    # point this branch should be exercised on both profiles rather than only on his.
    assert good_enough_after(band_rules(db_session.get(Profile, PROFILE_ME))) == 1
    assert good_enough_after(band_rules(db_session.get(Profile, PROFILE_SON))) == YOUTH_THRESHOLD

    _day(db_session, PROFILE_SON, 1, "done", YOUTH_THRESHOLD)
    _day(db_session, PROFILE_SON, 2, "done", YOUTH_THRESHOLD - 1)
    _day(db_session, PROFILE_SON, 3, "done", YOUTH_THRESHOLD)

    streak = current_streak(db_session, PROFILE_SON)
    assert (streak.current, streak.best) == (2, 2)


def test_streak_stops_at_first_planned(db_session: DbSession) -> None:
    """The walk ends at the head of the queue; a later ``done`` row cannot be counted."""
    _profiles(db_session)
    _day(db_session, PROFILE_ME, 1, "done", ROW_COUNT)
    _day(db_session, PROFILE_ME, 2, "done", ROW_COUNT)
    _day(db_session, PROFILE_ME, 3, "planned", 0)
    _day(db_session, PROFILE_ME, 4, "done", ROW_COUNT)
    _day(db_session, PROFILE_ME, 5, "done", ROW_COUNT)

    streak = current_streak(db_session, PROFILE_ME)
    assert (streak.current, streak.best) == (2, 2)


def test_streak_first_week(db_session: DbSession) -> None:
    """A profile that has never trained gets zero and no exception."""
    _profiles(db_session)
    streak = current_streak(db_session, PROFILE_ME)
    assert (streak.current, streak.best) == (0, 0)

    card = weekly_scorecard(db_session, PROFILE_ME, date(2026, 9, 7))
    assert (card.current_streak, card.best_streak, card.total_sessions) == (0, 0, 0)


def test_streak_together_counts_once_per_profile(db_session: DbSession) -> None:
    """One joint workout is +1 for each of them, never +2 for either."""
    _profiles(db_session)
    group = str(uuid.uuid4())
    for profile_id, ticks in ((PROFILE_ME, ROW_COUNT), (PROFILE_SON, ROW_COUNT)):
        planned = _plan(db_session, profile_id, 1, "done")
        _session(db_session, planned, ticks, group=group)

    assert current_streak(db_session, PROFILE_ME).current == 1
    assert current_streak(db_session, PROFILE_SON).current == 1
    assert weekly_scorecard(db_session, PROFILE_ME, date(2026, 9, 7)).total_sessions == 1


# ------------------------------------------------------------------ 7-8, 10: the scorecard


def test_scorecard_counts_current_week(db_session: DbSession, tz: Callable[[str], None]) -> None:
    """The ISO week is the local one: a Sunday-evening session is not next week's.

    Both timestamps are within one hour of each other in UTC. In Paris (UTC+2 in September) the
    first is Sunday 23:30 and the second Monday 00:30, so exactly one of them is in the week of
    Monday 7 September.
    """
    tz("Europe/Paris")
    _profiles(db_session)
    _day(db_session, PROFILE_ME, 1, "done", ROW_COUNT, "2026-09-06T21:30:00+00:00")
    _day(db_session, PROFILE_ME, 2, "done", ROW_COUNT, "2026-09-06T22:30:00+00:00")

    monday = weekly_scorecard(db_session, PROFILE_ME, date(2026, 9, 7))
    sunday = weekly_scorecard(db_session, PROFILE_ME, date(2026, 9, 6))

    assert monday.done_this_week == 1
    assert monday.week_start == date(2026, 9, 7)
    assert sunday.done_this_week == 1
    assert sunday.week_start == date(2026, 8, 31)
    assert monday.total_sessions == 2


def test_scorecard_can_exceed_plan(db_session: DbSession) -> None:
    """Five sessions against a four-day plan render ``5 of 4``, never clamped down to the plan."""
    _profiles(db_session)
    for index in range(1, 6):
        _day(db_session, PROFILE_ME, index, "done", ROW_COUNT)

    card = weekly_scorecard(db_session, PROFILE_ME, date(2026, 9, 7))
    assert (card.done_this_week, card.planned_this_week) == (5, 4)
    assert card.headline == "5 of 4"
    assert sum(card.dots) == 5


def test_total_rows_ticked(db_session: DbSession) -> None:
    """Every ticked row across every finished session, and nothing from an unfinished one."""
    _profiles(db_session)
    _day(db_session, PROFILE_ME, 1, "done", 4)
    _day(db_session, PROFILE_ME, 2, "done", 2)
    open_plan = _plan(db_session, PROFILE_ME, 3, "planned")
    _session(db_session, open_plan, ROW_COUNT, finished_at=None)

    card = weekly_scorecard(db_session, PROFILE_ME, date(2026, 9, 7))
    assert card.total_rows_ticked == 6
    assert card.total_sessions == 2


# --------------------------------------------------------------------- 11-13: the sparkline


def _points(values: list[float], start: date = date(2026, 8, 8)) -> tuple[tuple[date, float], ...]:
    return tuple((start + timedelta(days=index * 7), value) for index, value in enumerate(values))


def test_sparkline_two_points() -> None:
    trend = TrendSeries(weight_kg=_points([85.2, 84.1]), body_fat_pct=_points([19.1, 18.2]))
    svg = sparkline(trend)

    assert svg is not None
    assert svg.count("<polyline") == 2
    assert f'viewBox="0 0 {WIDTH} {HEIGHT}"' in svg
    assert 'vector-effect="non-scaling-stroke"' in svg
    assert 'role="img"' in svg and "85.2 to 84.1 kilograms" in svg
    # No chart library, and no colour baked into the markup: the stylesheet owns the palette.
    assert "stroke=" not in svg and "trend-weight" in svg and "trend-fat" in svg


def test_sparkline_one_point_returns_none() -> None:
    """One point is not a line. The template's whole condition is this ``None``."""
    assert sparkline(TrendSeries(weight_kg=_points([85.2]), body_fat_pct=())) is None
    assert sparkline(TrendSeries(weight_kg=(), body_fat_pct=())) is None
    assert sparkline(None) is None


def test_sparkline_flat_series() -> None:
    """Identical values draw down the middle rather than dividing by zero."""
    svg = sparkline(TrendSeries(weight_kg=_points([84.0, 84.0, 84.0]), body_fat_pct=()))

    assert svg is not None
    assert svg.count("<polyline") == 1
    middle = f"{HEIGHT / 2:.2f}"
    drawn = svg.split('points="')[1].split('"')[0]
    assert [pair.split(",")[1] for pair in drawn.split(" ")] == [middle] * 3


def test_sparkline_one_series_short_draws_the_other() -> None:
    """A series with a single point is dropped; the card still renders the one that has data."""
    trend = TrendSeries(weight_kg=_points([85.2, 84.1, 83.9]), body_fat_pct=_points([19.1]))
    svg = sparkline(trend)

    assert svg is not None and svg.count("<polyline") == 1
    assert "trend-weight" in svg and "trend-fat" not in svg


# ------------------------------------------------------------------ the cached trend itself


def _cache(db: DbSession, profile_id: str, payload: dict, stale: bool = False, fetched: str | None = None) -> None:
    db.add(
        MetricsCache(
            profile_id=profile_id,
            fetched_at=fetched or datetime.now(UTC).isoformat(),
            payload_json=json.dumps(payload),
            stale=stale,
        )
    )
    db.commit()


def test_trend_reads_the_cache(db_session: DbSession) -> None:
    _profiles(db_session)
    _cache(
        db_session,
        PROFILE_ME,
        {
            "weight_kg": [["2026-08-08", 85.2], ["2026-09-05", 84.1]],
            "body_fat_pct": [["2026-08-08", 19.1], ["2026-09-05", 18.2]],
        },
    )
    trend = body_comp_trend(db_session, PROFILE_ME, today=date(2026, 9, 6))

    assert trend is not None and trend.renderable
    assert trend.latest_weight == 84.1
    assert trend.weight_label == "84.1 kg" and trend.body_fat_label == "18.2%"
    assert sparkline(trend) is not None


def test_trend_is_none_without_a_row(db_session: DbSession) -> None:
    _profiles(db_session)
    assert body_comp_trend(db_session, PROFILE_ME) is None


def test_trend_drops_a_grams_shaped_weight(db_session: DbSession) -> None:
    """VitalForge returns grams (contract 2.2). An unconverted value is dropped, never drawn."""
    _profiles(db_session)
    _cache(
        db_session,
        PROFILE_ME,
        {"weight_kg": [["2026-08-08", 85200], ["2026-09-05", 84.1]], "body_fat_pct": []},
    )
    trend = body_comp_trend(db_session, PROFILE_ME, today=date(2026, 9, 6))

    assert trend is not None
    assert trend.weight_kg == ((date(2026, 9, 5), 84.1),)
    assert sparkline(trend) is None


def test_trend_survives_a_broken_payload(db_session: DbSession) -> None:
    """Half a payload yields the points that parsed. A corrupt one yields no card, not a 500."""
    _profiles(db_session)
    _cache(
        db_session,
        PROFILE_ME,
        {"weight_kg": [["nonsense", 84.1], ["2026-09-05", "84.1"], ["2026-09-04", 84.4], "junk"], "body_fat_pct": 7},
    )
    trend = body_comp_trend(db_session, PROFILE_ME, today=date(2026, 9, 6))

    assert trend is not None
    assert trend.weight_kg == ((date(2026, 9, 4), 84.4),)
    assert trend.body_fat_pct == ()

    db_session.delete(db_session.get(MetricsCache, PROFILE_ME))
    db_session.commit()
    db_session.add(MetricsCache(profile_id=PROFILE_ME, fetched_at="", payload_json="{not json", stale=False))
    db_session.commit()
    assert body_comp_trend(db_session, PROFILE_ME) is None


def test_trend_drops_points_older_than_the_window(db_session: DbSession) -> None:
    _profiles(db_session)
    _cache(
        db_session,
        PROFILE_ME,
        {"weight_kg": [["2026-06-01", 90.0], ["2026-09-01", 84.5], ["2026-09-05", 84.1]], "body_fat_pct": []},
    )
    trend = body_comp_trend(db_session, PROFILE_ME, today=date(2026, 9, 6))

    assert trend is not None
    assert [day for day, _ in trend.weight_kg] == [date(2026, 9, 1), date(2026, 9, 5)]


def test_trend_stale_note(db_session: DbSession) -> None:
    _profiles(db_session)
    fetched = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    _cache(db_session, PROFILE_ME, {"weight_kg": [], "body_fat_pct": []}, stale=True, fetched=fetched)

    trend = body_comp_trend(db_session, PROFILE_ME)
    assert trend is not None
    assert trend.stale_note() == "Last updated 2 days ago"
    assert TrendSeries(weight_kg=(), body_fat_pct=(), stale=False).stale_note() is None
