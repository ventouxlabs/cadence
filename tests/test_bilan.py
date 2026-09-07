"""PRP-07 acceptance tests 17-28: assessments, gaps and challenges as domain objects.

The HTTP half lives in ``tests/test_assessments_api.py``. Everything here drives ``cadence.bilan``
directly, because the rules under test - the section 7.5 ranking and its tie-break, the section 7.4
youth vocabulary, and the section 7.7 row insertion - are decisions the routes only relay.

Several of these are written so they can *fail*: the tie-break is fed in the wrong order, the
youth-name check asserts the phrase is really there before it asserts what is not, and the
"unavailable" test proves a recorded zero would have been a gap. A negative test that passes
because nothing was computed at all is worse than no test.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from datetime import date, timedelta
from typing import Any

from sqlmodel import Session as DbSession
from sqlmodel import select

from cadence.bilan import assessments as bilan
from cadence.bilan import challenges as chal
from cadence.bilan import gaps as gap_rules
from cadence.bilan.assessments import RETEST_DAYS
from cadence.bilan.gaps import Gap
from cadence.bilan.rows import parse_rows
from cadence.bilan.service import card_due, ensure_retest_queued, save_battery
from cadence.bilan.tables import ACTIVE, EXPIRED, MET, UNIT_UNAVAILABLE, Assessment, Challenge
from cadence.profils.rebuild import rebuild_one
from cadence.profils.settings import DEFAULT_SETTINGS
from cadence.profils.tables import PROFILE_ME, PROFILE_SON, Profile
from cadence.programme.context import make_context
from cadence.programme.selection import counted_rows
from cadence.programme.tables import PLANNED, SKIPPED, PlannedSession
from cadence.schema.enums import AgeBand, AssessmentId, Pattern
from cadence.vitalforge.tables import MetricsCache
from tests.e2e.test_setup import BANNED_BARE_WORDS, BANNED_PHRASES

BASELINE = date(2026, 9, 6)
YOUTH_BANDS: tuple[AgeBand, ...] = (AgeBand.U10, AgeBand.AGE_10_13, AgeBand.AGE_14_17)

#: Six adult readings that are all at or above their section 7.3 ``ok`` tier, so a test that wants
#: exactly one gap can name exactly one number and leave the rest alone.
ADULT_PASS: dict[str, float] = {
    "push_up_max": 20.0,
    "dead_hang_s": 45.0,
    "plank_s": 90.0,
    "wall_angel_reach": 3.0,
    "goblet_squat_quality": 4.0,
    "farmer_carry_s": 45.0,
}
#: The same for a ``u10`` son against his section 7.4 fun targets.
YOUTH_PASS: dict[str, float] = {
    "push_up_max": 8.0,
    "dead_hang_s": 20.0,
    "plank_s": 30.0,
    "wall_angel_reach": 2.0,
    "goblet_squat_quality": 3.0,
    "farmer_carry_s": 20.0,
}


def _rows(pairs: Iterable[tuple[str, float | None]], profile_id: str = PROFILE_ME) -> list[Assessment]:
    """One battery as unsaved rows, in the order given - which is what the tie-break is fed."""
    return [
        Assessment(
            id=f"{profile_id}-{test_id}",
            profile_id=profile_id,
            test_id=test_id,
            value=value,
            unit=UNIT_UNAVAILABLE if value is None else "reps",
            recorded_on=BASELINE.isoformat(),
        )
        for test_id, value in pairs
    ]


def _detect(
    db: DbSession,
    rows: list[Assessment],
    library: Any,
    *,
    is_youth: bool = False,
    band: AgeBand = AgeBand.ADULT,
    profile_id: str = PROFILE_ME,
    today: date = BASELINE,
    limit: int = 3,
) -> list[Gap]:
    return gap_rules.detect(
        db, profile_id, rows, library.assessments, band, is_youth=is_youth, today=today, limit=limit
    )


def _battery(
    library: Any, values: Mapping[str, float], *, is_youth: bool, unavailable: Iterable[str] = ()
) -> list[Any]:
    skip = set(unavailable)
    items: list[dict[str, Any]] = [
        {"test_id": test_id, "value": value} for test_id, value in values.items() if test_id not in skip
    ]
    items += [{"test_id": test_id, "unavailable": True} for test_id in unavailable]
    return [bilan.parse_result(library.assessments, item, is_youth=is_youth) for item in items]


def _save(
    db: DbSession,
    profile: Profile,
    library: Any,
    values: Mapping[str, float],
    *,
    unavailable: Iterable[str] = (),
    on: date = BASELINE,
    today: date | None = None,
    settings: Any = DEFAULT_SETTINGS,
) -> Any:
    results = _battery(library, values, is_youth=profile.kind == "youth", unavailable=unavailable)
    return save_battery(db, profile, results, library, settings, on, today or on)


def _planned(db: DbSession, profile_id: str) -> list[PlannedSession]:
    statement = (
        select(PlannedSession)
        .where(PlannedSession.profile_id == profile_id, PlannedSession.status == PLANNED)
        .order_by(PlannedSession.week, PlannedSession.day_index)  # type: ignore[arg-type]
    )
    return list(db.exec(statement).all())


def _trend(db: DbSession, profile_id: str, points: int) -> None:
    """A cached body-composition series: fat rising, muscle falling, both well off the flat band."""
    days = [BASELINE - timedelta(days=2 * (points - 1 - index)) for index in range(points)]
    payload = {
        "body_fat_pct": [[day.isoformat(), 20.0 + 0.2 * index] for index, day in enumerate(days)],
        "muscle_pct": [[day.isoformat(), 40.0 - 0.2 * index] for index, day in enumerate(days)],
    }
    db.merge(MetricsCache(profile_id=profile_id, fetched_at=BASELINE.isoformat(), payload_json=json.dumps(payload)))
    db.commit()


# -------------------------------------------------------------- 22: the youth challenge vocabulary


def test_challenge_names_youth_banned_words(library: Any) -> None:
    """22. Every test at every youth band, checked for what it says and for what it must not."""
    checked = 0
    for band in YOUTH_BANDS:
        for test_id in (item.value for item in AssessmentId):
            built = chal.build(
                library.assessments, PROFILE_SON, Gap(test_id=test_id, severity=0.5), band, BASELINE, is_youth=True
            )
            assert built is not None, f"{test_id} at {band.value} produced no challenge"
            name = built.name
            phrase = library.assessments.youth_targets[AssessmentId(test_id)].phrasing
            # Checked first: an empty or generic name would sail through every ban below.
            assert phrase in name, f"{name!r} does not carry the section 7.4 phrase for {test_id}"

            lowered = name.lower()
            for banned in BANNED_PHRASES:
                assert banned not in lowered, f"{banned!r} is in {name!r} ({band.value})"
            for word in BANNED_BARE_WORDS:
                assert not re.search(rf"\b{word}\b", lowered), f"{word!r} is in {name!r} ({band.value})"
            assert not re.search(r"\d\s*(?:kg|lb)", lowered), f"{name!r} names a load ({band.value})"
            checked += 1
    assert checked == 18, "all six tests at all three youth bands"


# ---------------------------------------------------------- 23-25: the row a challenge inserts


def test_challenge_row_appears_in_today(seeded: Any, db_session: DbSession, library: Any) -> None:
    """23. A ``push_up_max`` gap puts ``push-up`` last in the next ``upper_a`` session."""
    me = db_session.get(Profile, PROFILE_ME)
    assert me is not None
    state = _save(db_session, me, library, {**ADULT_PASS, "push_up_max": 6.0})
    assert [gap.test_id for gap in state.gaps] == ["push_up_max"]

    upper_a = next(item for item in _planned(db_session, PROFILE_ME) if item.day_type == "upper_a")
    last = parse_rows(upper_a)[-1]
    assert last["exercise_id"] == "push-up"
    assert last["is_challenge"] is True
    assert last["is_prelude"] is False


def test_a_prelude_challenge_does_not_erase_the_body_challenge_rows(
    seeded: Any, db_session: DbSession, library: Any
) -> None:
    """Two gaps, two challenges, and only one of them reaches the plan."""
    me = db_session.get(Profile, PROFILE_ME)
    assert me is not None
    state = _save(db_session, me, library, {**ADULT_PASS, "push_up_max": 6.0, "wall_angel_reach": 1.0})
    assert {item.test_id for item in state.challenges} == {"push_up_max", "wall_angel_reach"}

    inserted = {
        row["exercise_id"]
        for planned in _planned(db_session, PROFILE_ME)
        for row in parse_rows(planned)
        if row.get("is_challenge")
    }
    assert inserted == {"push-up", "wall-angel"}


def test_two_challenges_sharing_day_types_both_reach_the_plan(seeded: Any, db_session: DbSession, library: Any) -> None:
    """D-221. ``plank_s`` and ``goblet_squat_quality`` both want ``[lower_a, lower_full_b]``.

    ``target_sessions`` used to take the earliest matching sessions with no idea what was already
    in them, and ``insert_into`` evicts any other body challenge row, so the second challenge
    filled exactly the two sessions the first had just been put in and the first ended up in none
    of them - while the screen went on listing it as active. A four-week block has four sessions
    of each day type, so both fit with room to spare.
    """
    me = db_session.get(Profile, PROFILE_ME)
    assert me is not None
    state = _save(db_session, me, library, {**ADULT_PASS, "plank_s": 20.0, "goblet_squat_quality": 1.0})
    assert {item.test_id for item in state.challenges} == {"plank_s", "goblet_squat_quality"}

    carrying: dict[str, set[str]] = {"plank": set(), "box-squat-bench": set()}
    for planned in _planned(db_session, PROFILE_ME):
        for row in parse_rows(planned):
            if row.get("is_challenge") and row["exercise_id"] in carrying:
                carrying[row["exercise_id"]].add(planned.id)

    assert len(carrying["plank"]) == 2, "the plank challenge reached no session, or too many"
    assert len(carrying["box-squat-bench"]) == 2
    assert not carrying["plank"] & carrying["box-squat-bench"], "both landed in the same sessions"
    assert {chal.as_dict(item)["placed"] for item in state.challenges} == {2}


def test_the_extra_carry_set_is_added_once_however_often_the_battery_is_saved(
    seeded: Any, db_session: DbSession, library: Any
) -> None:
    """D-222. Re-posting a battery for the same date is permitted and must not stack sets.

    ``add_carry_set`` adds one set to the first carry row and inserts no row of its own, so
    ``clear_challenge_rows`` - which only removed ``is_challenge`` rows - could not undo it and
    every save laid another set on top. Three saves used to mean three extra sets.
    """
    me = db_session.get(Profile, PROFILE_ME)
    assert me is not None
    ctx = make_context(me, DEFAULT_SETTINGS, library)

    def carry_sets() -> dict[str, list[int]]:
        """Carry-row set counts per planned session, so sessions that come and go do not confuse
        the comparison: saving a battery also closes the assessment day (D-218b)."""
        found: dict[str, list[int]] = {}
        for planned in _planned(db_session, PROFILE_ME):
            for row in parse_rows(planned):
                exercise = ctx.library.exercises.get(str(row.get("exercise_id") or ""))
                if exercise is not None and exercise.pattern is Pattern.CARRY:
                    found.setdefault(planned.id, []).append(int(row["sets"]))
        return found

    before = carry_sets()
    _trend(db_session, PROFILE_ME, 11)
    state = _save(db_session, me, library, ADULT_PASS)
    assert "body_comp" in {item.test_id for item in state.challenges}, "no body-comp challenge to test"

    once = carry_sets()
    shared = set(before) & set(once)
    assert shared, "no session survived the save, so nothing here is being compared"
    # One extra set on the first carry row of each `lower_full_b` session, and nowhere else.
    assert sum(sum(once[key]) - sum(before[key]) for key in shared) == len(
        [key for key in shared if sum(once[key]) != sum(before[key])]
    )
    assert any(sum(once[key]) == sum(before[key]) + 1 for key in shared), "no extra carry set was added at all"

    _save(db_session, me, library, ADULT_PASS)
    _save(db_session, me, library, ADULT_PASS)
    assert carry_sets() == once, "saving the same battery again stacked another set on the carry"


def test_challenge_row_respects_p5(seeded: Any, db_session: DbSession, library: Any) -> None:
    """24. Three rows plus one at 15 minutes, and never past a ``u10`` exercise cap."""
    short = DEFAULT_SETTINGS.with_changes(session_minutes=15)
    me = db_session.get(Profile, PROFILE_ME)
    assert me is not None
    rebuild_one(db_session, me, short, library, BASELINE)
    db_session.commit()
    ctx = make_context(me, short, library)

    upper_a = next(item for item in _planned(db_session, PROFILE_ME) if item.day_type == "upper_a")
    assert len(counted_rows(parse_rows(upper_a), ctx)) == 3, "section 6.4: a 15-minute session is three rows"

    _save(db_session, me, library, {**ADULT_PASS, "push_up_max": 6.0}, settings=short)
    db_session.refresh(upper_a)
    assert len(counted_rows(parse_rows(upper_a), ctx)) == 4, "P5 allows exactly one row over the count"

    son = db_session.get(Profile, PROFILE_SON)
    assert son is not None
    _save(db_session, son, library, {**YOUTH_PASS, "plank_s": 10.0}, unavailable=["dead_hang_s"])
    son_ctx = make_context(son, DEFAULT_SETTINGS, library)
    cap = library.youth_rules[AgeBand.U10].max_exercises_per_session
    placed = 0
    for planned in _planned(db_session, PROFILE_SON):
        rows = parse_rows(planned)
        assert len(counted_rows(rows, son_ctx)) <= cap, f"{planned.day_type} is over the u10 cap of {cap}"
        placed += sum(1 for row in rows if row.get("is_challenge"))
    assert placed > 0, "no challenge row was inserted at all, so the cap was never under pressure"


def test_wall_angel_challenge_appends_to_prelude(seeded: Any, db_session: DbSession, library: Any) -> None:
    """25. The prelude grows by one, the main rows do not, and assessment day gets nothing."""
    me = db_session.get(Profile, PROFILE_ME)
    assert me is not None
    ctx = make_context(me, DEFAULT_SETTINGS, library)
    before = {
        planned.id: (
            sum(1 for row in parse_rows(planned) if row["is_prelude"]),
            len(counted_rows(parse_rows(planned), ctx)),
        )
        for planned in _planned(db_session, PROFILE_ME)
    }

    state = _save(db_session, me, library, {**ADULT_PASS, "wall_angel_reach": 1.0})
    assert [item.test_id for item in state.challenges] == ["wall_angel_reach"]

    grown = 0
    for planned in _planned(db_session, PROFILE_ME):
        rows = parse_rows(planned)
        extra = [row for row in rows if row.get("is_challenge")]
        prelude, main = before[planned.id]
        if planned.day_type == "assessment":
            assert extra == [], "the appendix would pre-fatigue the wall-angel test (section 7.7)"
            continue
        if not extra:
            continue
        assert extra[0]["exercise_id"] == "wall-angel"
        assert extra[0]["is_prelude"] is True
        assert sum(1 for row in rows if row["is_prelude"]) == prelude + 1
        assert len(counted_rows(rows, ctx)) == main, "a prelude appendix is not a main row"
        grown += 1
    assert grown > 0, "the challenge reached no session, so nothing above was checked"


# ------------------------------------------------------------------- 26-28: the challenge lifecycle


def test_challenge_met_on_retest(seeded: Any, db_session: DbSession, library: Any) -> None:
    """26. A retest that reaches the target closes the challenge before the due date can expire it."""
    me = db_session.get(Profile, PROFILE_ME)
    assert me is not None
    opened = _save(db_session, me, library, {**ADULT_PASS, "push_up_max": 6.0}).challenges
    assert [(item.test_id, item.target_value, item.status) for item in opened] == [("push_up_max", 15.0, ACTIVE)]

    retest = BASELINE + timedelta(days=28)
    _save(db_session, me, library, {**ADULT_PASS, "push_up_max": 15.0}, on=retest, today=retest)

    stored = chal.all_for(db_session, PROFILE_ME)
    assert [(item.test_id, item.status) for item in stored] == [("push_up_max", MET)]


def test_challenge_expires(seeded: Any, db_session: DbSession, library: Any) -> None:
    """27. Past the due date and still short: the old one expires, a fresh one is derived."""
    me = db_session.get(Profile, PROFILE_ME)
    assert me is not None
    opened = _save(db_session, me, library, {**ADULT_PASS, "push_up_max": 6.0, "dead_hang_s": 10.0}).challenges
    assert {item.test_id: item.due_on for item in opened} == {
        "push_up_max": "2026-10-04",
        "dead_hang_s": "2026-10-04",
    }

    late = date(2026, 10, 5)
    _save(db_session, me, library, {**ADULT_PASS, "push_up_max": 6.0}, unavailable=["dead_hang_s"], on=late, today=late)

    stored = {item.test_id: item for item in chal.all_for(db_session, PROFILE_ME)}
    assert len(stored) == 2, "a re-derived challenge reuses its natural key rather than minting a second row"
    assert stored["dead_hang_s"].status == EXPIRED
    assert stored["push_up_max"].status == ACTIVE
    assert stored["push_up_max"].baseline_on == late.isoformat()
    assert stored["push_up_max"].due_on == "2026-11-02"


def test_at_most_three_active_challenges(db_session: DbSession, library: Any) -> None:
    """28. Five candidates in, three stored; a later fourth is refused while those three stand."""
    wanted = ["push_up_max", "dead_hang_s", "plank_s", "wall_angel_reach", "goblet_squat_quality"]
    candidates = [
        chal.build(
            library.assessments, PROFILE_ME, Gap(test_id=test_id, severity=0.5), AgeBand.ADULT, BASELINE, is_youth=False
        )
        for test_id in wanted
    ]
    assert all(item is not None for item in candidates)

    # `store` answers with the open set, ordered by due date and id, so the three that survived are
    # compared as a set; what matters is that they are the first three offered and not the last.
    active = chal.store(db_session, PROFILE_ME, [item for item in candidates if item is not None])
    assert {item.test_id for item in active} == set(wanted[:3])

    fifth = chal.build(
        library.assessments,
        PROFILE_ME,
        Gap(test_id="farmer_carry_s", severity=0.9),
        AgeBand.ADULT,
        BASELINE,
        is_youth=False,
    )
    assert fifth is not None
    after = chal.store(db_session, PROFILE_ME, [fifth])
    assert {item.test_id for item in after} == set(wanted[:3])
    assert db_session.get(Challenge, fifth.id) is None, "the fourth was stored anyway, just not counted"


def test_a_skipped_retest_comes_back_the_next_day(seeded: Any, db_session: DbSession, library: Any) -> None:
    """D-227. Skipping is a statement about today, not about the retest for ever.

    The queued row keeps its id after a skip and ``next_due_on`` does not move until a new battery
    is recorded, so a due-date-stamped id matched the skipped row on every later render and
    ``ensure_retest_queued`` returned ``None`` for good: one skip and the four-weekly reminder was
    gone permanently. ``assess_skip``'s own docstring promises "the card returns on the next
    session", which is what this asserts.
    """
    me = db_session.get(Profile, PROFILE_ME)
    assert me is not None
    _save(db_session, me, library, ADULT_PASS)
    overdue = BASELINE + timedelta(days=RETEST_DAYS)

    assert ensure_retest_queued(db_session, me, library, DEFAULT_SETTINGS, overdue) is not None
    assert card_due(db_session, me, overdue) is True
    # Still idempotent within the day: a second render must not queue a second retest.
    assert ensure_retest_queued(db_session, me, library, DEFAULT_SETTINGS, overdue) is None

    queued = next(item for item in _planned(db_session, PROFILE_ME) if item.day_type == "assessment")
    queued.status = SKIPPED
    db_session.add(queued)
    db_session.commit()
    assert card_due(db_session, me, overdue) is False, "a skipped day should not still show the card"

    tomorrow = overdue + timedelta(days=1)
    assert ensure_retest_queued(db_session, me, library, DEFAULT_SETTINGS, tomorrow) is not None, (
        "the skipped row blocked the next retest for ever"
    )
    assert card_due(db_session, me, tomorrow) is True
