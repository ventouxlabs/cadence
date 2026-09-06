"""The streak walk, table-driven — PRP-04 acceptance tests 1-6 as one exhaustive suite.

Every other screen in the build will quote this number (PRP-07's missed-session logic, PRP-10's
badges), and PRP-04 risk 1 is that it ends up defined three different ways. The six rules are
therefore pinned as a **table of sequences** rather than as one test per rule: a rule that only
ever appears at the head or the tail of a walk is a rule whose interaction with the others is
untested, and every regression this catches will be an interaction.

The table runs on the son because the strictest youth band promotes at three ticked exercises, so
a ``partial`` session is expressible there. On an adult one tick is enough (principles section
3.2), which is asserted rather than assumed, and the adult table is the same sequences with the
partials removed.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from sqlmodel import Session as DbSession
from sqlmodel import select

from cadence.historique.queries import recent_sessions
from cadence.historique.scorecard import current_streak, weekly_scorecard
from cadence.profils.tables import PROFILE_ME, PROFILE_SON, Profile
from cadence.programme.tables import ACTIVE, PlannedSession, Program
from cadence.seance.catalog import band_rules
from cadence.seance.status import completion, good_enough_after
from cadence.seance.tables import SessionRowRecord
from tests.test_historique import ROW_COUNT, WEEK_MONDAY, _plan, _profiles, _session

# The six outcomes a planned row can present to the walk.
COMPLETE = "complete"
PARTIAL = "partial"
SKIPPED = "skipped"
PLANNED = "planned"
# ``done`` with no session behind it: not reachable through the UI, since ``finish()`` writes both.
GHOST = "ghost"
# A status no writer in this build produces, on a hand-edited or half-restored database.
UNKNOWN = "unknown"

PARTIAL_FREE = (COMPLETE, SKIPPED, PLANNED, GHOST, UNKNOWN)

# (name, sequence, current, best). Read a sequence oldest-first, exactly as the walk does.
TABLE: tuple[tuple[str, tuple[str, ...], int, int], ...] = (
    ("empty history", (), 0, 0),
    ("one session", (COMPLETE,), 1, 1),
    ("five in a row", (COMPLETE,) * 5, 5, 5),
    ("skip resets, best remembers", (COMPLETE, COMPLETE, SKIPPED, COMPLETE), 1, 2),
    ("partial holds", (COMPLETE, PARTIAL, COMPLETE), 2, 2),
    ("planned ends the walk", (COMPLETE, COMPLETE, PLANNED, COMPLETE, COMPLETE), 2, 2),
    ("planned first", (PLANNED, COMPLETE, COMPLETE), 0, 0),
    ("nothing but skips", (SKIPPED, SKIPPED), 0, 0),
    ("skipped first", (SKIPPED, COMPLETE, COMPLETE), 2, 2),
    ("two skips in the middle", (COMPLETE, SKIPPED, SKIPPED, COMPLETE, COMPLETE), 2, 2),
    ("nothing but partials", (PARTIAL, PARTIAL, PARTIAL), 0, 0),
    ("partial before the first complete", (PARTIAL, COMPLETE), 1, 1),
    ("ghost holds (D-106)", (COMPLETE, GHOST, COMPLETE), 2, 2),
    ("nothing but a ghost", (GHOST,), 0, 0),
    ("unknown status holds", (COMPLETE, UNKNOWN, COMPLETE), 2, 2),
    ("ends on a skip", (COMPLETE, COMPLETE, COMPLETE, SKIPPED), 0, 3),
    ("partial and skip together", (COMPLETE, PARTIAL, SKIPPED, COMPLETE, PARTIAL), 1, 1),
    ("a skip after the head is never seen", (COMPLETE, PLANNED, SKIPPED), 1, 1),
    ("ghost then skip", (COMPLETE, COMPLETE, GHOST, SKIPPED, COMPLETE), 1, 2),
)

ADULT_TABLE = tuple(case for case in TABLE if all(step in PARTIAL_FREE for step in case[1]))


def _threshold(db: DbSession, profile_id: str) -> int:
    return good_enough_after(band_rules(db.get(Profile, profile_id)))


def _walk(db: DbSession, profile_id: str, sequence: tuple[str, ...]) -> None:
    """Write one planned row per step, in walk order, with the session each step implies."""
    threshold = _threshold(db, profile_id)
    for index, step in enumerate(sequence, start=1):
        if step in {SKIPPED, PLANNED}:
            _plan(db, profile_id, index, step)
            continue
        if step == UNKNOWN:
            _plan(db, profile_id, index, "abandoned")
            continue
        planned = _plan(db, profile_id, index, "done")
        if step == GHOST:
            continue
        _session(db, planned, threshold if step == COMPLETE else max(threshold - 1, 0))


@pytest.mark.parametrize(("name", "sequence", "current", "best"), TABLE, ids=[case[0] for case in TABLE])
def test_streak_table_youth(
    db_session: DbSession, name: str, sequence: tuple[str, ...], current: int, best: int
) -> None:
    _profiles(db_session)
    assert _threshold(db_session, PROFILE_SON) > 1, "a partial is not expressible at a threshold of one"
    _walk(db_session, PROFILE_SON, sequence)

    streak = current_streak(db_session, PROFILE_SON)
    assert (streak.current, streak.best) == (current, best), name


@pytest.mark.parametrize(("name", "sequence", "current", "best"), ADULT_TABLE, ids=[case[0] for case in ADULT_TABLE])
def test_streak_table_adult(
    db_session: DbSession, name: str, sequence: tuple[str, ...], current: int, best: int
) -> None:
    """The same sequences on the parent, whose every finished session is complete."""
    _profiles(db_session)
    assert _threshold(db_session, PROFILE_ME) == 1, "principles section 3.2: one tick finishes an adult session"
    _walk(db_session, PROFILE_ME, sequence)

    streak = current_streak(db_session, PROFILE_ME)
    assert (streak.current, streak.best) == (current, best), name


def test_the_walk_is_scoped_to_one_profile(db_session: DbSession) -> None:
    """The son's skipped day may not reset the parent's streak, or the table proves nothing."""
    _profiles(db_session)
    _walk(db_session, PROFILE_ME, (COMPLETE, COMPLETE, COMPLETE))
    _walk(db_session, PROFILE_SON, (SKIPPED, SKIPPED, SKIPPED))

    assert current_streak(db_session, PROFILE_ME).current == 3
    assert current_streak(db_session, PROFILE_SON).current == 0


# ------------------------------------------------------------------ 6: together, once each


def test_together_pairs_count_once_per_profile(db_session: DbSession) -> None:
    """Three joint workouts are three for each of them, never six for either (PRP-04 risk 10)."""
    _profiles(db_session)
    for index in range(1, 4):
        group = str(uuid.uuid4())
        for profile_id in (PROFILE_ME, PROFILE_SON):
            planned = _plan(db_session, profile_id, index, "done")
            _session(db_session, planned, ROW_COUNT, group=group)

    for profile_id in (PROFILE_ME, PROFILE_SON):
        assert current_streak(db_session, profile_id).current == 3, profile_id
        card = weekly_scorecard(db_session, profile_id, WEEK_MONDAY)
        assert card.total_sessions == 3, profile_id
        assert card.done_this_week == 3, profile_id
        sessions = recent_sessions(db_session, profile_id)
        assert len(sessions) == 3 and all(item.together for item in sessions), profile_id


async def test_together_list_holds_one_row_per_profile(db_session: DbSession, client: httpx.AsyncClient) -> None:
    """The merged JSON list is three plus three, not three counted twice (D-103)."""
    _profiles(db_session)
    for index in range(1, 4):
        group = str(uuid.uuid4())
        for profile_id in (PROFILE_ME, PROFILE_SON):
            _session(db_session, _plan(db_session, profile_id, index, "done"), ROW_COUNT, group=group)

    body = (await client.get("/api/sessions?profile=together")).json()
    assert body["meta"]["count"] == 6
    assert sorted(row["profile"] for row in body["data"]) == [PROFILE_ME] * 3 + [PROFILE_SON] * 3
    assert len({row["id"] for row in body["data"]}) == 6


# ----------------------------------------------------- the invariant the walk rests on


def test_two_programs_for_one_profile_interleave(db_session: DbSession) -> None:
    """**Known limitation, documented rather than asserted away.**

    ``current_streak`` scopes its walk to the *profile*, not to the active program
    (``cadence/historique/scorecard.py:104-109``), and rests on architecture section 3's "one
    active program per profile". Nothing in this build breaks that: ``bibliotheque.seed`` derives
    the program id from the profile slug, so a rebuild reuses the row rather than adding one.

    On a database that does hold two, the two blocks interleave by ``(week, day_index)`` and a
    stale ``planned`` row in the retired one ends the walk early. This test writes that database
    by hand and pins what actually happens, so that whoever adds a second program per profile sees
    the number change here first.
    """
    _profiles(db_session)
    for index in range(1, 4):
        _session(db_session, _plan(db_session, PROFILE_ME, index, "done"), ROW_COUNT)
    assert current_streak(db_session, PROFILE_ME).current == 3

    retired = Program(
        id="prog-me-old",
        profile_id=PROFILE_ME,
        template="strength-4",
        start_date="2026-08-01",
        weeks=4,
        days_per_week=4,
        session_minutes=30,
        status="archived",
    )
    db_session.add(retired)
    db_session.add(
        PlannedSession(
            id="prog-me-old-1-0",
            program_id=retired.id,
            profile_id=PROFILE_ME,
            week=1,
            day_index=0,
            day_type="upper_a",
            workout_id="upper-a",
            rows_json="[]",
            status="planned",
        )
    )
    db_session.commit()

    # Scoped to the active program this would still be 3. Scoped to the profile, the retired
    # block's ``planned`` row sorts first by (week, day_index) and ends the walk before it starts.
    # A day index of zero is chosen so the ordering is decided by the data rather than by which
    # row SQLite happens to return first among ties.
    assert current_streak(db_session, PROFILE_ME).current == 0
    assert current_streak(db_session, PROFILE_ME).best == 0
    assert [program.status for program in db_session.exec(select(Program)).all()] == ["archived"]
    assert retired.status != ACTIVE


# --------------------------------------------------- one definition of "complete", not two


@pytest.mark.parametrize(
    ("profile_id", "ticks"),
    [(PROFILE_ME, 0), (PROFILE_ME, 1), (PROFILE_ME, ROW_COUNT), (PROFILE_SON, 0), (PROFILE_SON, 2), (PROFILE_SON, 3)],
)
def test_the_list_and_the_status_module_agree_on_completion(db_session: DbSession, profile_id: str, ticks: int) -> None:
    """PRP-04 risk 1, as a test.

    ``queries._summary`` re-derives completion from a grouped SQL count, while
    ``seance.status.completion`` derives it from loaded rows. The comment at
    ``cadence/historique/queries.py:221`` claims the two are one definition; nothing proved it
    until here. Both are asked about the same session, at and either side of the band threshold.
    """
    _profiles(db_session)
    record = _session(db_session, _plan(db_session, profile_id, 1, "done"), ticks)

    rows = db_session.exec(select(SessionRowRecord).where(SessionRowRecord.session_id == record.id)).all()
    expected = completion(list(rows), band_rules(db_session.get(Profile, profile_id)))

    summary = recent_sessions(db_session, profile_id)[0]
    assert summary.completion == expected
    assert summary.rows_done == ticks and summary.rows_total == ROW_COUNT
