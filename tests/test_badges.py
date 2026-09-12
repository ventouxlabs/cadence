"""The seven badges: each rule, both negatives on the hard one, and the derived-not-stored claim.

The fixtures write ``planned_session`` / ``session`` / ``session_row`` rows directly rather than
driving Today, because these tests need histories a seeded block cannot reach in one test — ten
sessions, twenty-five, four consecutive ISO weeks, and a fifth week with a hole in it.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime, time, timedelta

import pytest
from sqlalchemy import text
from sqlmodel import Session

from cadence.bilan.tables import MET, Challenge
from cadence.historique import badges
from cadence.historique.badges import badges_for
from cadence.historique.page import build_column
from cadence.historique.queries import MAX_LIMIT, recent_sessions
from cadence.profils.tables import Profile
from cadence.programme.tables import PlannedSession
from cadence.seance.tables import SessionRecord, SessionRowRecord

ROWS = 5
PRELUDE_ROWS = 2
# The seeded profiles' first week starts far enough back that nothing here collides with the
# block `seeded` already planned.
BASE_WEEK = 40
#: Planned rows are ordered by ``(week, day_index)`` and their ids must not collide, so both are
#: derived from the day itself rather than from a per-call counter.
EPOCH = date(2026, 1, 1)


def _rows_json(prelude: int = PRELUDE_ROWS) -> str:
    return json.dumps(
        [
            {
                "position": index,
                "exercise_id": "goblet-squat",
                "name": "Goblet squat",
                "reps": 8,
                "sets": 3,
                "is_prelude": index <= prelude,
            }
            for index in range(1, ROWS + 1)
        ]
    )


def _at(day: date) -> str:
    return datetime.combine(day, time(18, 0), tzinfo=UTC).isoformat()


def write_session(
    db: Session,
    profile_id: str,
    day: date,
    index: int,
    *,
    done_rows: int = ROWS,
    prelude: int = PRELUDE_ROWS,
    prelude_done: bool = True,
    status: str = "done",
) -> str:
    """One finished session on ``day``, with ``done_rows`` ticked from the top of the list."""
    planned_id = f"badge-{profile_id}-{index}"
    session_id = str(uuid.uuid4())
    db.add(
        PlannedSession(
            id=planned_id,
            program_id=f"badge-{profile_id}",
            profile_id=profile_id,
            week=BASE_WEEK + index,
            day_index=index,
            day_type="upper_a",
            workout_id="upper-a",
            rows_json=_rows_json(prelude),
            status=status,
        )
    )
    db.add(
        SessionRecord(
            id=session_id,
            profile_id=profile_id,
            planned_session_id=planned_id,
            started_at=_at(day),
            finished_at=_at(day),
            duration_min=26,
            felt="right",
        )
    )
    for position in range(1, ROWS + 1):
        is_prelude = position <= prelude
        done = (position <= done_rows) and (prelude_done or not is_prelude)
        db.add(
            SessionRowRecord(
                id=f"{session_id}-{position}",
                session_id=session_id,
                position=position,
                exercise_id="goblet-squat",
                done=done,
                done_at=_at(day) if done else None,
            )
        )
    db.commit()
    return session_id


@pytest.fixture
def profile_id(db_session: Session) -> str:
    """A clean adult profile with no history of its own, so counts start at zero."""
    db_session.add(Profile(id="badger", display_name="Badger", kind="adult", age_years=40))
    db_session.commit()
    return "badger"


def ids(db: Session, profile: str) -> dict[str, bool]:
    return {badge.id: badge.earned for badge in badges_for(db, profile)}


def _run(db: Session, profile: str, count: int, *, start: date | None = None, **kwargs) -> list[date]:
    """``count`` sessions on consecutive days, oldest first."""
    first = start or date(2026, 3, 2)
    days = [first + timedelta(days=index) for index in range(count)]
    for day in days:
        write_session(db, profile, day, (day - EPOCH).days, **kwargs)
    return days


# ------------------------------------------------------------------------------ the seven rules


def test_first_session_badge(db_session: Session, profile_id: str) -> None:
    """One complete session earns it; a history of partials alone does not."""
    assert ids(db_session, profile_id)["first-session"] is False
    write_session(db_session, profile_id, date(2026, 3, 2), 1, done_rows=0)
    assert ids(db_session, profile_id)["first-session"] is False, "a partial-only history earned it"
    write_session(db_session, profile_id, date(2026, 3, 3), 2)
    assert ids(db_session, profile_id)["first-session"] is True


def test_streak_badges(db_session: Session, profile_id: str) -> None:
    """Three in a row earns `streak-3` and not `streak-7`; seven earns both."""
    _run(db_session, profile_id, 3)
    earned = ids(db_session, profile_id)
    assert earned["streak-3"] is True
    assert earned["streak-7"] is False

    _run(db_session, profile_id, 4, start=date(2026, 3, 5))
    earned = ids(db_session, profile_id)
    assert earned["streak-3"] is True
    assert earned["streak-7"] is True


def test_session_count_badges(db_session: Session, profile_id: str) -> None:
    """Ten and twenty-five, counting complete sessions only."""
    _run(db_session, profile_id, 9)
    assert ids(db_session, profile_id)["sessions-10"] is False
    _run(db_session, profile_id, 1, start=date(2026, 3, 20))
    assert ids(db_session, profile_id)["sessions-10"] is True
    assert ids(db_session, profile_id)["sessions-25"] is False


def test_partial_sessions_do_not_count_toward_ten(db_session: Session, profile_id: str) -> None:
    """Twelve sessions of which four are partial is eight, and eight is not ten."""
    _run(db_session, profile_id, 8)
    _run(db_session, profile_id, 4, start=date(2026, 4, 1), done_rows=0)
    assert ids(db_session, profile_id)["sessions-10"] is False


# ---------------------------------------------------------------------------- exact boundaries
#
# Each threshold is checked at N-1 and at N. A rule written with `>` instead of `>=` — or one
# counting the sessions rather than the run of them — passes every test above and fails here.


def test_streak_three_is_off_at_two_and_on_at_three(db_session: Session, profile_id: str) -> None:
    _run(db_session, profile_id, 2)
    assert ids(db_session, profile_id)["streak-3"] is False, "two in a row earned the three badge"
    _run(db_session, profile_id, 1, start=date(2026, 3, 4))
    assert ids(db_session, profile_id)["streak-3"] is True


def test_streak_seven_is_off_at_six_and_on_at_seven(db_session: Session, profile_id: str) -> None:
    _run(db_session, profile_id, 6)
    earned = ids(db_session, profile_id)
    assert earned["streak-3"] is True
    assert earned["streak-7"] is False, "six in a row earned the seven badge"
    _run(db_session, profile_id, 1, start=date(2026, 3, 8))
    assert ids(db_session, profile_id)["streak-7"] is True


def test_twenty_five_is_off_at_twenty_four_and_on_at_twenty_five(db_session: Session, profile_id: str) -> None:
    _run(db_session, profile_id, 24)
    earned = ids(db_session, profile_id)
    assert earned["sessions-10"] is True
    assert earned["sessions-25"] is False, "twenty-four sessions earned the twenty-five badge"
    _run(db_session, profile_id, 1, start=date(2026, 4, 20))
    assert ids(db_session, profile_id)["sessions-25"] is True


def test_the_prelude_window_slides_past_a_bad_first_week(db_session: Session, profile_id: str) -> None:
    """Six weeks with only the first week's warm-up skipped still earns it, from weeks two to five.

    The discriminating case for `prelude_streak_day`: a checker that only tested the window
    starting at the earliest week would call this unearned. The date must be the last qualifying
    day of week five, not of week six.
    """
    days = []
    for index in range(6):
        day = date(2026, 3, 2) + timedelta(weeks=index)
        days.append(day)
        write_session(db_session, profile_id, day, (day - EPOCH).days, prelude_done=index != 0)

    badges = {badge.id: badge for badge in badges_for(db_session, profile_id)}
    assert badges["prelude-4-weeks"].earned is True, "the window never slid off the bad first week"
    assert badges["prelude-4-weeks"].earned_on == days[4], badges["prelude-4-weeks"].earned_on


def test_prelude_four_weeks(db_session: Session, profile_id: str) -> None:
    """Four consecutive ISO weeks, one session each, every prelude row ticked."""
    for index in range(4):
        day = date(2026, 3, 2) + timedelta(weeks=index)
        write_session(db_session, profile_id, day, (day - EPOCH).days)
    assert ids(db_session, profile_id)["prelude-4-weeks"] is True


def test_prelude_four_weeks_gap(db_session: Session, profile_id: str) -> None:
    """A missing week breaks it, even with qualifying weeks on both sides of the hole."""
    offsets = [0, 1, 2, 4, 5]  # week 3 is missing
    for offset in offsets:
        day = date(2026, 3, 2) + timedelta(weeks=offset)
        write_session(db_session, profile_id, day, (day - EPOCH).days)
    assert ids(db_session, profile_id)["prelude-4-weeks"] is False


def test_prelude_four_weeks_one_missed_prelude_row(db_session: Session, profile_id: str) -> None:
    """One unticked prelude row in one session of the window breaks the whole window."""
    for index in range(4):
        day = date(2026, 3, 2) + timedelta(weeks=index)
        # The third week's session is complete on the band threshold but skips its warm-up.
        write_session(db_session, profile_id, day, (day - EPOCH).days, prelude_done=index != 2)
    assert ids(db_session, profile_id)["prelude-4-weeks"] is False


def test_prelude_four_weeks_needs_a_prelude_at_all(db_session: Session, profile_id: str) -> None:
    """D-231: a session carrying no prelude row does not qualify vacuously."""
    for index in range(4):
        day = date(2026, 3, 2) + timedelta(weeks=index)
        write_session(db_session, profile_id, day, (day - EPOCH).days, prelude=0)
    assert ids(db_session, profile_id)["prelude-4-weeks"] is False


def test_challenge_met_badge(db_session: Session, profile_id: str) -> None:
    db_session.add(
        Challenge(
            id="ch-1",
            profile_id=profile_id,
            name="Dead hang 60 s",
            test_id="dead_hang_s",
            target_value=60.0,
            unit="s",
            baseline_on="2026-03-01",
            due_on="2026-04-01",
            status=MET,
        )
    )
    db_session.commit()
    assert ids(db_session, profile_id)["challenge-met"] is True


def test_challenge_met_without_met_on_still_names_no_date(db_session: Session, profile_id: str) -> None:
    """D-232 the other way round: a row closed before `met_on` existed still invents nothing.

    This is what every already-met challenge on the deployed install looks like — `status = met`,
    `met_on` null — and it has to keep reading "earned" with no day rather than guessing one or
    dropping the badge. The caption is asserted because it is the thing a person reads.
    """
    db_session.add(
        Challenge(
            id="ch-3",
            profile_id=profile_id,
            name="Dead hang 60 s",
            test_id="dead_hang_s",
            target_value=60.0,
            unit="s",
            baseline_on="2026-03-01",
            due_on="2026-04-01",
            status=MET,
        )
    )
    db_session.commit()

    badge = next(item for item in badges_for(db_session, profile_id) if item.id == "challenge-met")
    assert badge.earned is True
    assert badge.earned_on is None, f"a met-on date was invented: {badge.earned_on}"
    assert badge.caption == "Challenge met · earned", badge.caption
    assert "earned " not in badge.caption, f"the caption names a day it cannot know: {badge.caption}"


def test_challenge_met_names_the_day_it_was_met(db_session: Session, profile_id: str) -> None:
    """D-232 resolved: `met_on` is set, so the badge reads it instead of shrugging."""
    db_session.add(
        Challenge(
            id="ch-4",
            profile_id=profile_id,
            name="Dead hang 60 s",
            test_id="dead_hang_s",
            target_value=60.0,
            unit="s",
            baseline_on="2026-03-01",
            due_on="2026-04-01",
            status=MET,
            met_on="2026-03-22",
        )
    )
    db_session.commit()

    badge = next(item for item in badges_for(db_session, profile_id) if item.id == "challenge-met")
    assert badge.earned is True
    assert badge.earned_on == date(2026, 3, 22)
    assert badge.caption == "Challenge met · earned 22 Mar", badge.caption


def test_challenge_met_takes_the_first_day_of_several(db_session: Session, profile_id: str) -> None:
    """The qualifying day is when the badge *started* being true, as it is for the other six.

    A dateless row beside a dated one must not win: `MIN` skips nulls, so the mixed history the
    deployed install will actually have — old met challenges plus new ones — still names a day.
    """
    for index, met_on in enumerate((None, "2026-05-04", "2026-03-22"), start=5):
        db_session.add(
            Challenge(
                id=f"ch-{index}",
                profile_id=profile_id,
                name="Dead hang 60 s",
                test_id="dead_hang_s",
                target_value=60.0,
                unit="s",
                baseline_on="2026-03-01",
                due_on="2026-04-01",
                status=MET,
                met_on=met_on,
            )
        )
    db_session.commit()

    badge = next(item for item in badges_for(db_session, profile_id) if item.id == "challenge-met")
    assert badge.earned_on == date(2026, 3, 22)


def test_challenge_met_survives_an_unparseable_met_on(db_session: Session, profile_id: str) -> None:
    """A day this build cannot read is no day — never an unearned badge, never a crash."""
    db_session.add(
        Challenge(
            id="ch-8",
            profile_id=profile_id,
            name="Dead hang 60 s",
            test_id="dead_hang_s",
            target_value=60.0,
            unit="s",
            baseline_on="2026-03-01",
            due_on="2026-04-01",
            status=MET,
            met_on="last Tuesday",
        )
    )
    db_session.commit()

    badge = next(item for item in badges_for(db_session, profile_id) if item.id == "challenge-met")
    assert badge.earned is True
    assert badge.earned_on is None
    assert badge.caption == "Challenge met · earned"


def test_an_unparseable_met_on_does_not_outvote_a_real_one(db_session: Session, profile_id: str) -> None:
    """The reason the earliest day is picked in Python and not by SQL's `MIN`.

    `MIN(met_on)` compares strings, so a junk value that happens to sort below a real ISO date —
    `"0000-not-a-day"` does — would win the aggregate and cost the badge a date it genuinely has.
    Parsing first and taking the minimum of what survives makes the junk row unable to vote.
    """
    for index, met_on in enumerate(("0000-not-a-day", "2026-03-22"), start=10):
        db_session.add(
            Challenge(
                id=f"ch-{index}",
                profile_id=profile_id,
                name="Dead hang 60 s",
                test_id="dead_hang_s",
                target_value=60.0,
                unit="s",
                baseline_on="2026-03-01",
                due_on="2026-04-01",
                status=MET,
                met_on=met_on,
            )
        )
    db_session.commit()

    badge = next(item for item in badges_for(db_session, profile_id) if item.id == "challenge-met")
    assert badge.earned_on == date(2026, 3, 22), "a junk met_on sorted below the real one and won"


def test_challenge_met_tolerates_a_database_without_the_column(db_session: Session, profile_id: str) -> None:
    """A `challenge` table with no `met_on`, which `init_db` no longer leaves behind (D-283).

    **This test is not the deployed-database guarantee, and reading it as one is what let a P1
    through.** It exercises the badge's raw SQL only. `Challenge` is ORM-mapped, so
    `select(Challenge)` names every mapped field and the real failure was `active_for` — on the
    assessment path, nowhere near a badge. `tests/test_db.py` owns that case now; `has_column`
    survives here because raw SQL that names a column still has to cope with it being absent,
    for instance mid-migration or against a database this build did not open.
    """
    db_session.add(
        Challenge(
            id="ch-9",
            profile_id=profile_id,
            name="Dead hang 60 s",
            test_id="dead_hang_s",
            target_value=60.0,
            unit="s",
            baseline_on="2026-03-01",
            due_on="2026-04-01",
            status=MET,
            met_on="2026-03-22",
        )
    )
    db_session.commit()
    db_session.execute(text("ALTER TABLE challenge DROP COLUMN met_on"))
    db_session.commit()

    badge = next(item for item in badges_for(db_session, profile_id) if item.id == "challenge-met")
    assert badge.earned is True, "the badge was lost with its date"
    assert badge.earned_on is None
    assert badge.caption == "Challenge met · earned"


def test_an_active_challenge_earns_nothing(db_session: Session, profile_id: str) -> None:
    db_session.add(
        Challenge(
            id="ch-2",
            profile_id=profile_id,
            name="Dead hang 60 s",
            test_id="dead_hang_s",
            target_value=60.0,
            unit="s",
            baseline_on="2026-03-01",
            due_on="2026-04-01",
        )
    )
    db_session.commit()
    assert ids(db_session, profile_id)["challenge-met"] is False


# ------------------------------------------------------------------------- shape and safety


def test_badges_are_derived_not_stored(db_session: Session, profile_id: str) -> None:
    """No badge table, and two reads agree without either of them writing anything."""
    tables = {
        str(row[0]) for row in db_session.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).all()
    }
    assert not any("badge" in name.lower() for name in tables), f"a badge table exists: {tables}"

    _run(db_session, profile_id, 3)
    before = db_session.execute(text("SELECT COUNT(*) FROM session_row")).scalar_one()
    first = badges_for(db_session, profile_id)
    second = badges_for(db_session, profile_id)
    assert first == second
    assert db_session.execute(text("SELECT COUNT(*) FROM session_row")).scalar_one() == before


def test_badges_youth_safe(seeded, db_session: Session) -> None:
    """Every name, at every youth band, passes the D-027 banned-phrase check."""
    from tests.test_web_today import BARE_WORDS, BODY_IMAGE_PHRASES

    checked = 0
    for band, age in (("u10", 8), ("age_10_13", 12), ("age_14_17", 16)):
        son = db_session.get(Profile, "son")
        assert son is not None, "the seeded fixture has no son profile"
        son.age_years = age
        son.age_band = band
        db_session.add(son)
        db_session.commit()
        for badge in badges_for(db_session, "son"):
            assert badge.youth_safe is True
            text_ = f"{badge.name} {badge.caption}".lower()
            for phrase in BODY_IMAGE_PHRASES:
                assert phrase not in text_, f"{phrase!r} in {badge.id} at {band}"
            for pattern in BARE_WORDS:
                assert not pattern.search(text_), f"{pattern.pattern!r} in {badge.id} at {band}"
            checked += 1
    assert checked == 21, f"expected seven badges at three bands, checked {checked}"


def test_a_youth_profile_is_only_shown_youth_safe_badges(db_session: Session, seeded) -> None:
    """The son's strip may hold nothing flagged unsafe — and the filter is real (D-254).

    All seven shipped rules are safe, so asserting only over them would pass whether or not a
    filter existed. An unsafe rule is injected for the duration of the test, which is the only
    way to watch the filter actually drop one.
    """
    son = db_session.get(Profile, "son")
    assert son is not None and son.kind == "youth"
    shown = build_column(db_session, son).badges
    assert shown, "the son's strip is empty, so this would pass vacuously"
    assert [badge.id for badge in shown if not badge.youth_safe] == []
    assert len(shown) == len(badges._RULES)


def test_an_unsafe_badge_never_reaches_the_sons_column(
    db_session: Session, seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The filter drops it for the son and keeps it for the parent (D-254).

    The injected rule is the kind PRP-10's youth filter exists to stop — a body-composition badge
    that would be true of the parent and must never be named on a child's screen.
    """
    unsafe = badges._Rule("body-comp", "Body composition", "Body composition", "bc", youth_safe=False)
    monkeypatch.setattr(badges, "_RULES", (*badges._RULES, unsafe))

    son = db_session.get(Profile, "son")
    parent = db_session.get(Profile, "me")
    assert son is not None and parent is not None

    son_ids = [badge.id for badge in build_column(db_session, son).badges]
    parent_ids = [badge.id for badge in build_column(db_session, parent).badges]

    assert "body-comp" not in son_ids, "an unsafe badge reached the son's strip"
    assert "body-comp" in parent_ids, "the filter dropped it for the parent too"
    assert len(son_ids) == len(parent_ids) - 1
    # A rule with no condition computed for it is unearned, not a crash.
    unearned = next(badge for badge in badges.badges_for(db_session, "me") if badge.id == "body-comp")
    assert unearned.earned is False and unearned.earned_on is None


def test_the_gate_is_in_badges_for_so_every_caller_inherits_it(
    db_session: Session, seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D-256: the strip is one of three youth surfaces, and it was the only one filtered.

    `badges_earned_on` (the son's Done screen) and `badge_by_id` (the caption route) both call
    `badges_for` directly, so a filter that lived on `build_column` was not a gate at all — it
    was a gate on one door of three. This asserts the source, not a surface, because that is the
    claim: an unsafe rule does not become a `Badge` for a youth profile anywhere.
    """
    unsafe = badges._Rule("body-comp", "Body composition", "Body composition", "bc", youth_safe=False)
    monkeypatch.setattr(badges, "_RULES", (*badges._RULES, unsafe))

    assert "body-comp" not in [badge.id for badge in badges.badges_for(db_session, "son")]
    assert "body-comp" in [badge.id for badge in badges.badges_for(db_session, "me")]
    # The caption route resolves by id off the same list, so an unsafe id is simply not there.
    assert badges.badge_by_id(badges.badges_for(db_session, "son"), "body-comp") is None
    assert badges.badge_by_id(badges.badges_for(db_session, "me"), "body-comp") is not None


def test_an_unsafe_badge_cannot_reach_the_sons_done_screen(
    db_session: Session, seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The "New badge" line reads `badges_earned_on`, which is `badges_for` filtered by date."""
    day = date(2026, 3, 2)
    write_session(db_session, "son", day, (day - EPOCH).days)
    unsafe = badges._Rule("body-comp", "Body composition", "Body composition", "bc", youth_safe=False)
    monkeypatch.setattr(badges, "_RULES", (*badges._RULES, unsafe))

    earned_today = badges.badges_earned_on(db_session, "son", day)
    assert earned_today, "the son earned nothing that day, so this would pass vacuously"
    assert "body-comp" not in [badge.id for badge in earned_today]


def test_every_shipped_rule_declares_its_own_safety(db_session: Session) -> None:
    """No rule may inherit the flag by accident: it is a field, set per rule."""
    assert all(isinstance(rule.youth_safe, bool) for rule in badges._RULES)
    assert all(rule.youth_safe for rule in badges._RULES), "a shipped rule is flagged unsafe"
    assert {badge.youth_safe for badge in badges.badges_for(db_session, "me")} == {True}


def test_youth_and_adult_names_differ(db_session: Session, profile_id: str, seeded) -> None:
    adult = {badge.id: badge.name for badge in badges_for(db_session, profile_id)}
    youth = {badge.id: badge.name for badge in badges_for(db_session, "son")}
    assert adult["first-session"] == "First session"
    assert youth["first-session"] == "You started!"
    assert set(adult) == set(youth)


def test_badge_earned_on_date(db_session: Session, profile_id: str) -> None:
    """The qualifying session's date, not today."""
    days = _run(db_session, profile_id, 3)
    earned = {badge.id: badge.earned_on for badge in badges_for(db_session, profile_id)}
    assert earned["first-session"] == days[0]
    assert earned["streak-3"] == days[2]
    assert earned["first-session"] != date.today()


def test_unearned_badges_carry_no_date(db_session: Session, profile_id: str) -> None:
    for badge in badges_for(db_session, profile_id):
        assert badge.earned is False
        assert badge.earned_on is None


def test_the_history_column_carries_badges(db_session: Session, seeded) -> None:
    """PRP-04's column grows a ``badges`` field; ``as_dict`` is untouched (it is an API contract)."""
    son = db_session.get(Profile, "son")
    assert son is not None
    column = build_column(db_session, son)
    assert len(column.badges) == 7
    assert "badges" not in column.as_dict()


def test_the_session_count_badge_agrees_with_the_history_list(db_session: Session, profile_id: str) -> None:
    """The badge and the card next to it count the same sessions (Low 12).

    Both are supposed to read one definition of "complete" — ``queries.completion_threshold`` —
    but nothing pinned that they actually agree, so a future edit could give the History list one
    threshold and the badge another and no test would notice. The fixture mixes complete and
    partial sessions deliberately: two counts that both said "every finished session" would agree
    with each other while both being wrong.
    """
    _run(db_session, profile_id, 10)
    _run(db_session, profile_id, 4, start=date(2026, 5, 1), done_rows=0)

    listed = recent_sessions(db_session, profile_id, MAX_LIMIT)
    complete = sum(1 for item in listed if item.completion == "complete")
    partial = sum(1 for item in listed if item.completion == "partial")

    assert len(listed) == 14, "the fixture did not land: this would compare two empty counts"
    assert (complete, partial) == (10, 4)
    earned = ids(db_session, profile_id)
    assert earned["sessions-10"] is (complete >= 10)
    assert earned["sessions-25"] is (complete >= 25)


def test_the_two_counts_move_together_across_the_threshold(db_session: Session, profile_id: str) -> None:
    """Nine agrees with nine, ten agrees with ten — the badge flips exactly where the list does."""
    _run(db_session, profile_id, 9)
    listed = sum(1 for item in recent_sessions(db_session, profile_id, MAX_LIMIT) if item.completion == "complete")
    assert listed == 9
    assert ids(db_session, profile_id)["sessions-10"] is False

    _run(db_session, profile_id, 1, start=date(2026, 6, 1))
    listed = sum(1 for item in recent_sessions(db_session, profile_id, MAX_LIMIT) if item.completion == "complete")
    assert listed == 10
    assert ids(db_session, profile_id)["sessions-10"] is True
