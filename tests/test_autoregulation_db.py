"""Autoregulation against a database: the signals, the comeback, and the Done hook.

PRP-07 acceptance tests 14 and 15, plus the two things the task brief names that the pure table
cannot show: that an absent or unreadable ``metrics_cache`` reads as ``unknown`` and never as
``low`` (risk 6), and that a failure inside ``after_done`` cannot take the Done screen with it.

Kept apart from ``tests/test_autoregulation.py`` because everything here needs the seeded block and
the real library; the decision table and the arithmetic there need neither.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlmodel import select

from cadence.bibliotheque.loader import load_library
from cadence.bilan.rows import build_row
from cadence.profils.rebuild import rebuild_one
from cadence.profils.tables import PROFILE_ME, PROFILE_SON, Profile
from cadence.programme import autoregulation
from cadence.programme.autoregulation import restart_block, rewrite_rows
from cadence.programme.context import make_context
from cadence.programme.decision import Decision
from cadence.programme.materialise import materialise_rows
from cadence.programme.schemes import workout_id_for
from cadence.programme.signals import RESTART_AFTER_DAYS, RESTART_NOTE, needs_block_restart, readiness_for
from cadence.programme.tables import DONE, PLANNED, PlannedSession
from cadence.schema.enums import DayType
from cadence.seance import done as done_service
from cadence.seance import ticks as tick_service
from cadence.seance.catalog import load_settings
from cadence.seance.tables import SessionRecord
from cadence.seance.today import resolve_today
from cadence.vitalforge.metrics import PUSH_AT, STEADY_AT, ProfileMetrics
from cadence.vitalforge.metrics import Readiness as VitalForgeReadiness
from cadence.vitalforge.tables import MetricsCache

NOW = datetime(2026, 9, 6, 18, 0, tzinfo=UTC)
LIBRARY = Path(__file__).resolve().parents[1] / "library"


def _planned(db, profile_id: str = PROFILE_ME) -> list[PlannedSession]:
    statement = (
        select(PlannedSession)
        .where(PlannedSession.profile_id == profile_id)
        .order_by(PlannedSession.week, PlannedSession.day_index)
    )
    return list(db.exec(statement).all())


def _rows_json(db, profile_id: str | None = None) -> dict[str, str]:
    """Every planned session's stored rows, as the exact bytes on disk."""
    statement = select(PlannedSession)
    if profile_id is not None:
        statement = statement.where(PlannedSession.profile_id == profile_id)
    return {planned.id: planned.rows_json for planned in db.exec(statement).all()}


def _loads(rows_json: str) -> dict[int, float]:
    return {
        int(row["position"]): float(row["load_kg"])
        for row in json.loads(rows_json)
        if isinstance(row.get("load_kg"), int | float)
    }


def _context(db, library, profile_id: str = PROFILE_ME):
    profile = db.get(Profile, profile_id)
    return profile, make_context(profile, load_settings(db), library)


def _record_finished(db, planned: PlannedSession, when: datetime) -> SessionRecord:
    """A session finished at ``when``, and its planned row closed, as Done would leave them."""
    record = SessionRecord(
        id=str(uuid4()),
        profile_id=planned.profile_id,
        planned_session_id=planned.id,
        started_at=when.isoformat(),
        finished_at=when.isoformat(),
        duration_min=20,
        felt="right",
    )
    planned.status = DONE
    db.add(record)
    db.add(planned)
    db.commit()
    return record


def _finish_today(db, profile_id: str = PROFILE_ME, felt: str = "easy", *, tick_all: bool = True):
    """Tick this profile's checklist and press Done, the way the Today screen does."""
    view = resolve_today(db, profile_id).primary
    positions = [row.record.position for row in view.rows] if tick_all else [view.rows[0].record.position]
    for position in positions:
        tick_service.set_done(db, resolve_today(db, profile_id).primary, position, True)
    view = resolve_today(db, profile_id).primary
    return view, done_service.finish(db, view, felt)


# ------------------------------------------------------------------ readiness: never "low"


def test_readiness_is_unknown_without_a_cache(db_session, seeded) -> None:
    """An empty ``metrics_cache`` is nothing known, not a low reading (risk 6)."""
    assert readiness_for(db_session, PROFILE_ME) == "unknown"


@pytest.mark.parametrize(
    ("label", "payload"),
    [
        ("malformed", "{not json at all"),
        ("not an object", "[1, 2, 3]"),
        ("missing key", '{"weight_kg": [["2026-08-08", 85.2]]}'),
        ("null status", '{"readiness": {"score": null, "status": null}}'),
        ("unrecognised status", '{"readiness": {"status": "meh"}}'),
    ],
)
def test_readiness_unreadable_payloads_are_unknown(db_session, seeded, label: str, payload: str) -> None:
    """Unparseable, wrong-shaped and unrecognised all answer ``unknown`` - never ``low``."""
    db_session.add(MetricsCache(profile_id=PROFILE_ME, fetched_at=NOW.isoformat(), payload_json=payload))
    db_session.commit()
    assert readiness_for(db_session, PROFILE_ME) == "unknown", label


def test_readiness_reads_a_real_status(db_session, seeded) -> None:
    """The check above is not vacuous: a cache that does say ``low`` is read as ``low``."""
    db_session.add(
        MetricsCache(
            profile_id=PROFILE_ME,
            fetched_at=NOW.isoformat(),
            payload_json=json.dumps({"readiness": {"score": 21.0, "status": "low"}}),
        )
    )
    db_session.commit()
    assert readiness_for(db_session, PROFILE_ME) == "low"


@pytest.mark.parametrize(
    ("score", "expected"),
    [(PUSH_AT, "high"), (STEADY_AT, "ok"), (STEADY_AT - 1, "low")],
)
def test_readiness_round_trips_prp06s_own_payload(db_session, seeded, score: float, expected: str) -> None:
    """The cache is written by PRP-06 and read here, so both must agree on the key path (D-219).

    Every other readiness test hand-builds the payload, which would stay green if ``as_payload``
    moved ``readiness`` or renamed ``score``: ``_from_score`` would return ``None``, the fallback
    would read the son's permanent ``insufficient_data`` as ``unknown``, and R6 would be off with
    nothing failing. This one builds the payload with PRP-06's own writer instead.
    """
    metrics = ProfileMetrics(
        profile_id=PROFILE_ME,
        fetched_at=NOW,
        readiness=VitalForgeReadiness(score=score, status="insufficient_data"),
    )
    db_session.add(
        MetricsCache(
            profile_id=PROFILE_ME,
            fetched_at=NOW.isoformat(),
            payload_json=json.dumps(metrics.as_payload()),
        )
    )
    db_session.commit()
    assert readiness_for(db_session, PROFILE_ME) == expected


# --------------------------------------------------------- 14: the fourteen-day comeback


def test_block_restart_needs_a_prior_session(db_session, seeded) -> None:
    """14, risk 12. A fresh install has no gap to measure, so it never restarts."""
    assert needs_block_restart(db_session, PROFILE_ME, NOW) is False


def test_block_restart_after_14_days(db_session, seeded, library) -> None:
    """14. Loads x 0.90 re-rounded, the queue renumbered from week 1, one announcement line."""
    queue = _planned(db_session)
    _record_finished(db_session, queue[0], NOW - timedelta(days=RESTART_AFTER_DAYS + 6))

    assert needs_block_restart(db_session, PROFILE_ME, NOW - timedelta(days=RESTART_AFTER_DAYS - 1)) is False
    assert needs_block_restart(db_session, PROFILE_ME, NOW) is True

    before = _rows_json(db_session, PROFILE_ME)
    profile, ctx = _context(db_session, library)
    rungs = {rung for ladder in ctx.weights.ladders.values() for rung in ladder}

    note = restart_block(db_session, profile, ctx)
    db_session.commit()

    assert note == RESTART_NOTE
    assert "\n" not in note

    remaining = [planned for planned in _planned(db_session) if planned.status == PLANNED]
    assert [(planned.week, planned.day_index) for planned in remaining[:5]] == [
        (1, 0),
        (1, 1),
        (1, 2),
        (1, 3),
        (2, 0),
    ]

    carried_load = 0
    lightened = 0
    for planned in remaining:
        was, now = _loads(before[planned.id]), _loads(planned.rows_json)
        assert set(now) <= set(was), "the restart changes weights, never which rows are prescribed"
        for position, load in now.items():
            assert load <= was[position] + 1e-9
            assert load <= was[position] * 0.90 + 1e-9 or load == was[position]
            assert load in rungs, "a lightened load still has to be a rung the household owns"
        carried_load += bool(was)
        lightened += any(load < was[position] for position, load in now.items())
    assert carried_load > 0, "a block with no loaded row would pass this test vacuously"
    assert lightened == carried_load, "every remaining session is lightened, not just the first"


# ---------------------------------------------------- 15: assessment day changes nothing


def test_no_autoregulation_on_assessment_day(db_session, seeded) -> None:
    """15. Finishing the assessment leaves every planned session's rows byte-identical."""
    view = resolve_today(db_session, PROFILE_ME).primary
    assert view.planned.day_type == "assessment", "the seeded block opens on the assessment day"

    before = _rows_json(db_session)
    _, summaries = _finish_today(db_session)

    assert summaries, "Done still summarises the session it finished"
    assert _rows_json(db_session) == before
    assert db_session.get(SessionRecord, view.record.id).notes is None


def test_autoregulation_rewrites_an_ordinary_day(db_session, seeded) -> None:
    """15. The byte-identity check above is not vacuous: an ordinary day does rewrite."""
    _finish_today(db_session)  # clear the assessment day off the head of the queue.

    view = resolve_today(db_session, PROFILE_ME).primary
    assert view.planned.day_type != "assessment"
    before = _rows_json(db_session)

    _finish_today(db_session, felt="easy")

    changed = [key for key, value in _rows_json(db_session).items() if before[key] != value]
    assert len(changed) == 1, "one session of the same day type is rewritten, and only one"
    assert db_session.get(SessionRecord, view.record.id).notes


# ----------------------------------------------- a failing autoregulation never breaks Done


def test_after_done_failure_does_not_break_done(db_session, seeded, monkeypatch: pytest.MonkeyPatch) -> None:
    """Done is a redirect off a screen, not a transaction the plan may veto."""
    _finish_today(db_session)  # past the assessment day, so the hook reaches the library.

    view = resolve_today(db_session, PROFILE_ME).primary
    for row in view.rows:
        tick_service.set_done(db_session, resolve_today(db_session, PROFILE_ME).primary, row.record.position, True)
    view = resolve_today(db_session, PROFILE_ME).primary
    before = _rows_json(db_session)

    def _explode(*_: Any, **__: Any) -> None:
        raise RuntimeError("the library is on fire")

    # Bound at import time in ``autoregulation``: patching ``seance.catalog`` would patch a name
    # this call never reads, and the test would pass having proved nothing.
    monkeypatch.setattr(autoregulation, "library_bundle", _explode)

    summaries = done_service.finish(db_session, view, "easy")

    assert [summary.session_id for summary in summaries] == [view.record.id]
    assert summaries[0].completion == "complete"
    stored = db_session.get(SessionRecord, view.record.id)
    assert stored.finished_at is not None, "the rollback inside the hook must not un-finish it"
    assert stored.notes is None
    assert _rows_json(db_session) == before, "a failed autoregulation leaves the plan exactly as it was"

    # The hook now runs inside `finish`'s own transaction, on a savepoint, so its rollback must
    # not take PRP-06's write-back job with it: "finished" and "queued" are one fact (D-219).
    from cadence.vitalforge.tables import SyncJob

    queued = db_session.exec(select(SyncJob).where(SyncJob.session_id == view.record.id)).all()
    assert len(queued) == 1, "the savepoint rolled back more than the plan rewrite"


def test_week_four_carry_is_not_deloaded_twice(seeded: Any, db_session: Any) -> None:
    """R1 over a row the week scheme already shaped changes the RPE cap and nothing else.

    ``materialise_rows(template, 4, ...)`` builds a week-4 session at section 6.2's deload shape
    already, so running section 5.5's arithmetic over it again scales what is scaled. Sets and reps
    survive that (2 sets stay 2, ``rep_min`` is the week-1 value), but distance compounded: a 30 m
    carry came out at 18 m. Only a rewrite through the database path can show it (D-218).
    """
    library = load_library(LIBRARY)
    me = db_session.get(Profile, PROFILE_ME)
    settings = load_settings(db_session)
    ctx = make_context(me, settings, library)
    template = library.template(workout_id_for(DayType.LOWER_FULL_B, "adult"))

    week_four = materialise_rows(template, 4, me, settings, library)
    carries = [row for row in week_four if row.get("meters") is not None]
    assert carries, "this household's lower_full_b carries no distance row, so the check is vacuous"

    rewritten, _ = rewrite_rows(week_four, week_four, Decision("deload", "R1"), ctx, week=4)
    after = {row["position"]: row for row in rewritten}
    for row in carries:
        assert after[row["position"]]["meters"] == row["meters"], "the week scheme already deloaded this carry"
        assert after[row["position"]]["sets"] == row["sets"]


def test_a_week_one_target_exists_after_a_restart_renumbers_a_six_day_queue(seeded: Any, db_session: Any) -> None:
    """D-225, half one. ``next_of_day_type`` can return a session in the **same** week.

    At two to five days a week each day type appears once per week, so the next session of a day
    type is always a week later and a target is never week 1. Six is the exception: section 6.3
    fills the sixth slot with a second ``upper_a``. Even then a freshly built week 1 holds only
    one, because the assessment displaces the first slot (section 6.3) - so the branch needs
    ``restart_block``, which renumbers whatever is left of the queue from week 1 with no
    assessment in front of it. That combination is narrow, and it is reachable.
    """
    library = load_library(LIBRARY)
    me = db_session.get(Profile, PROFILE_ME)
    six = load_settings(db_session).with_changes(days_per_week=6)
    rebuild_one(db_session, me, six, library, date(2026, 9, 6))
    db_session.commit()

    queue = _planned(db_session)
    for planned in queue[:2]:
        _record_finished(db_session, planned, NOW - timedelta(days=RESTART_AFTER_DAYS + 2))
    restart_block(db_session, me, make_context(me, six, library))
    db_session.commit()

    week_one = [planned for planned in _planned(db_session) if planned.status == PLANNED and planned.week == 1]
    repeated = [
        day_type
        for day_type in {planned.day_type for planned in week_one}
        if sum(1 for planned in week_one if planned.day_type == day_type) > 1
    ]
    assert repeated, "no day type repeats inside the renumbered week 1, so the branch is dead"

    pair = sorted(
        (planned for planned in week_one if planned.day_type == repeated[0]),
        key=lambda planned: planned.day_index,
    )
    target = autoregulation.next_of_day_type(db_session, PROFILE_ME, pair[0])
    assert target is not None and target.week == 1, "the next session of that day type is not in week 1"


def test_a_pending_bump_is_consumed_when_the_target_is_week_one(seeded: Any, db_session: Any) -> None:
    """D-225, half two. The guard fires: a bump banked in a deload lands on a week-1 target.

    Together with the test above this is why ``pending_bump`` is **live**, not dead: a six-day
    week gives a week-1 target, and ``restart_block`` renumbers the remaining queue from week 1
    while ``lighten_row`` copies rows with ``{**row}``, so the flag survives to be read.
    """
    library = load_library(LIBRARY)
    me = db_session.get(Profile, PROFILE_ME)
    settings = load_settings(db_session)
    ctx = make_context(me, settings, library)
    template = library.template(workout_id_for(DayType.UPPER_A, "adult"))
    rows = materialise_rows(template, 1, me, settings, library)

    loaded = next((row for row in rows if isinstance(row.get("load_kg"), int | float) and row["load_kg"]), None)
    assert loaded is not None, "no loaded row in upper_a, so a bump has nothing to move"

    performed = [{**row, "pending_bump": True} for row in rows]
    held, _ = rewrite_rows(rows, rows, Decision("hold", "R11"), ctx, week=2)
    banked, _ = rewrite_rows(performed, rows, Decision("hold", "R11"), ctx, week=1)

    position = loaded["position"]
    by_position = ({row["position"]: row for row in held}, {row["position"]: row for row in banked})
    assert by_position[1][position] != by_position[0][position], (
        "the pending bump was not consumed at week 1, so the guard never fires"
    )


def test_a_youth_challenge_row_carries_the_youth_bump_order(aged_son: Any, db_session: Any) -> None:
    """Risk 2 held only by accident while a challenge row's progression was empty (D-218)."""
    library = load_library(LIBRARY)
    son = db_session.get(Profile, PROFILE_SON)
    ctx = make_context(son, load_settings(db_session), library)
    built = [
        row for row in (build_row(spec, 20.0, ctx) for spec in library.assessments.gap_rows.values()) if row is not None
    ]
    assert built, "no section 7.7 row is offerable to this band, so the check would be vacuous"
    for row in built:
        assert row["progression"]["allow_load_progression"] is False, row["exercise_id"]
