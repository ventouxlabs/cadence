"""Session lifecycle: resolution, creation on first render, ticks, adjust, felt, Done.

PRP-02 acceptance tests 1-12, plus the HTML routes those tests reach through.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import select

from cadence.profils.settings import DEFAULT_SETTINGS
from cadence.profils.tables import PROFILE_ME, PROFILE_SON, Profile
from cadence.programme.tables import PlannedSession
from cadence.seance import done as done_service
from cadence.seance import ticks as tick_service
from cadence.seance.catalog import band_rules
from cadence.seance.status import completion, good_enough_after, rows_done
from cadence.seance.tables import SessionRecord, SessionRowRecord
from cadence.seance.today import TodayError, resolve_today, view_for_session

HX = {"HX-Request": "true"}


def _view(db, profile: str = PROFILE_ME):
    return resolve_today(db, profile).primary


def _sessions(db) -> list[SessionRecord]:
    return list(db.exec(select(SessionRecord)).all())


# ------------------------------------------------------------------ 1, 2: first render


def test_today_creates_session_on_first_render(db_session, seeded) -> None:
    view = _view(db_session)
    stored = db_session.exec(select(SessionRowRecord).where(SessionRowRecord.session_id == view.record.id)).all()
    assert len(_sessions(db_session)) == 1
    assert len(stored) == view.rows_total > 0
    assert view.record.started_at is None
    assert view.record.finished_at is None


def test_today_is_idempotent_on_reload(db_session, seeded) -> None:
    first = _view(db_session).record.id
    second = _view(db_session).record.id
    assert first == second
    assert len(_sessions(db_session)) == 1


def test_today_resolves_the_first_planned_session_in_order(db_session, seeded) -> None:
    view = _view(db_session)
    planned = db_session.exec(
        select(PlannedSession)
        .where(PlannedSession.profile_id == PROFILE_ME, PlannedSession.status == "planned")
        .order_by(PlannedSession.week, PlannedSession.day_index)
    ).first()
    assert view.planned.id == planned.id


def test_unknown_profile_is_an_error(db_session, seeded) -> None:
    with pytest.raises(TodayError):
        resolve_today(db_session, "nobody")


# ---------------------------------------------------------------------- 3, 4, 5: ticks


def test_first_tick_sets_started_at(db_session, seeded) -> None:
    view = _view(db_session)
    tick_service.set_done(db_session, view, 1, True)
    first = db_session.get(SessionRecord, view.record.id).started_at
    assert first is not None

    later = _view(db_session)
    tick_service.set_done(db_session, later, 2, True)
    assert db_session.get(SessionRecord, view.record.id).started_at == first


def test_tick_toggles_and_is_idempotent(db_session, seeded) -> None:
    view = _view(db_session)
    stamp = datetime.now(UTC).isoformat()
    first = tick_service.set_done(db_session, view, 1, True, stamp)
    assert first.changed and first.row.record.done

    replay = tick_service.set_done(db_session, _view(db_session), 1, True, stamp)
    assert not replay.changed
    assert replay.row.record.done is True
    assert replay.row.record.done_at == first.row.record.done_at

    untick = tick_service.toggle(db_session, _view(db_session), 1)
    assert untick.changed and untick.row.record.done is False
    assert untick.row.record.done_at is None


def test_stale_tick_ignored(db_session, seeded) -> None:
    view = _view(db_session)
    now = datetime.now(UTC)
    tick_service.set_done(db_session, view, 1, True, now.isoformat())
    stale = (now - timedelta(minutes=5)).isoformat()

    result = tick_service.set_done(db_session, _view(db_session), 1, False, stale)

    assert not result.changed
    assert result.row.record.done is True


def test_tick_on_missing_row_raises(db_session, seeded) -> None:
    with pytest.raises(tick_service.TickError):
        tick_service.set_done(db_session, _view(db_session), 99, True)


# ------------------------------------------------------------------- 6, 7, 8: adjust


def _first_reps_row(view):
    return next(row for row in view.rows if row.reps is not None and row.adjustable)


def test_adjust_writes_done_fields_only(db_session, seeded) -> None:
    view = _view(db_session)
    row = _first_reps_row(view)
    planned_reps = row.spec["reps"]

    result = tick_service.adjust(db_session, view, row.position, "reps", 1, DEFAULT_SETTINGS)

    assert result.changed
    assert result.row.record.reps_done == planned_reps + 1
    assert result.row.record.reps_planned == planned_reps
    assert result.row.record.done is False


def test_adjust_load_clamped_to_youth_cap(db_session, aged_son) -> None:
    """A son at ``age_10_13`` cannot step the goblet squat past the band's 8 kg cap."""
    view = None
    for _ in range(6):
        candidate = _view(db_session, PROFILE_SON)
        if any(row.spec["exercise_id"] == "goblet-squat" for row in candidate.rows):
            view = candidate
            break
        done_service.finish(db_session, candidate)
    assert view is not None, "no goblet squat row in the son's block"

    row = next(row for row in view.rows if row.spec["exercise_id"] == "goblet-squat")
    cap = tick_service.load_cap(view, row.spec)
    assert cap == 8.0

    seen = []
    for _ in range(8):
        result = tick_service.adjust(
            db_session, _view(db_session, PROFILE_SON), row.position, "load", 1, DEFAULT_SETTINGS
        )
        seen.append(result)
        if not result.changed:
            break
    final = _view(db_session, PROFILE_SON).row(row.position)
    assert final.load_kg <= cap
    assert seen[-1].notice
    assert "weight" not in seen[-1].notice.lower()


def test_adjust_rejected_on_carry_row(db_session, seeded) -> None:
    view = None
    for _ in range(6):
        candidate = _view(db_session)
        if any(row.measure == "meters" for row in candidate.rows):
            view = candidate
            break
        done_service.finish(db_session, candidate)
    assert view is not None, "no distance row in the parent's block"

    row = next(row for row in view.rows if row.measure == "meters")
    assert row.adjustable is False
    with pytest.raises(tick_service.TickError):
        tick_service.adjust(db_session, view, row.position, "reps", 1, DEFAULT_SETTINGS)


def test_adjust_reps_stops_at_zero(db_session, seeded) -> None:
    """Zero is where the stepper stops, and it is a real answer, not a floor to be clamped."""
    view = _view(db_session)
    row = _first_reps_row(view)
    for _ in range(row.spec["reps"] + 3):
        tick_service.adjust(db_session, _view(db_session), row.position, "reps", -1, DEFAULT_SETTINGS)
    assert _view(db_session).row(row.position).reps == tick_service.MIN_REPS


def test_adjust_rejects_an_unknown_field(db_session, seeded) -> None:
    view = _view(db_session)
    row = _first_reps_row(view)
    with pytest.raises(tick_service.TickError):
        tick_service.adjust(db_session, view, row.position, "sets", 1, DEFAULT_SETTINGS)  # type: ignore[arg-type]


# --------------------------------------------------------------- 9, 10, 11: finishing


def test_done_finalises(db_session, seeded) -> None:
    view = _view(db_session)
    for row in view.rows:
        tick_service.set_done(db_session, _view(db_session), row.position, True)

    summaries = done_service.finish(db_session, _view(db_session), "right")

    assert len(summaries) == 1
    summary = summaries[0]
    assert summary.completion == "complete"
    assert summary.rows_done == summary.rows_total == view.rows_total
    record = db_session.get(SessionRecord, view.record.id)
    assert record.finished_at is not None
    assert record.duration_min >= 1
    assert record.felt == "right"
    assert db_session.get(PlannedSession, view.planned.id).status == "done"


def test_done_is_idempotent(db_session, seeded) -> None:
    view = _view(db_session)
    tick_service.set_done(db_session, view, 1, True)
    first = done_service.finish(db_session, _view(db_session), "easy")[0]
    finished_at = db_session.get(SessionRecord, view.record.id).finished_at

    again = done_service.finish(db_session, view_for_session(db_session, view.record), "hard")[0]

    assert again.as_dict() == first.as_dict()
    assert db_session.get(SessionRecord, view.record.id).finished_at == finished_at
    assert db_session.get(SessionRecord, view.record.id).felt == "easy"


def test_partial_completion_for_youth(db_session, seeded) -> None:
    view = _view(db_session, PROFILE_SON)
    rules = band_rules(view.profile)
    threshold = good_enough_after(rules)
    assert threshold >= 3
    tick_service.set_done(db_session, view, view.rows[0].position, True)

    summary = done_service.finish(db_session, _view(db_session, PROFILE_SON))[0]

    assert summary.completion == "partial"
    assert summary.rows_done == 1


def test_completion_helpers_read_the_band(db_session, seeded) -> None:
    view = _view(db_session, PROFILE_SON)
    records = [row.record for row in view.rows]
    assert rows_done(records) == 0
    assert completion(records, view.rules) == "partial"
    assert good_enough_after(None) == 1


def test_duration_is_zero_without_a_tick(db_session, seeded) -> None:
    summary = done_service.finish(db_session, _view(db_session))[0]
    assert summary.duration_min == 0
    assert summary.completion == "partial"


def test_felt_only_accepts_the_three_values(db_session, seeded) -> None:
    view = _view(db_session)
    assert done_service.set_felt(db_session, view.record, "sideways") is None
    assert done_service.set_felt(db_session, view.record, "hard") == "hard"


# ----------------------------------------------------------------------- 12: together


def test_together_creates_two_linked_sessions(db_session, seeded) -> None:
    view = resolve_today(db_session, "together")
    assert len(view.sessions) == 2
    group = {item.record.together_group_id for item in view.sessions}
    assert len(group) == 1 and group != {None}
    assert {item.profile.id for item in view.sessions} == {PROFILE_ME, PROFILE_SON}

    summaries = done_service.finish(db_session, view.sessions[0])

    assert len(summaries) == 2
    for item in view.sessions:
        assert db_session.get(SessionRecord, item.record.id).finished_at is not None
        assert db_session.get(PlannedSession, item.planned.id).status == "done"


def test_together_felt_belongs_to_one_session(db_session, seeded) -> None:
    view = resolve_today(db_session, "together")
    done_service.finish(db_session, view.sessions[0], "hard")
    assert db_session.get(SessionRecord, view.sessions[0].record.id).felt == "hard"
    assert db_session.get(SessionRecord, view.sessions[1].record.id).felt is None


def test_together_group_id_is_stable_across_renders(db_session, seeded) -> None:
    first = {item.record.together_group_id for item in resolve_today(db_session, "together").sessions}
    second = {item.record.together_group_id for item in resolve_today(db_session, "together").sessions}
    assert first == second


def test_together_with_one_profile_names_the_other(db_session, seeded) -> None:
    son = db_session.get(Profile, PROFILE_SON)
    for planned in db_session.exec(select(PlannedSession).where(PlannedSession.profile_id == PROFILE_SON)).all():
        planned.status = "done"
        db_session.add(planned)
    db_session.commit()

    view = resolve_today(db_session, "together")

    assert len(view.sessions) == 1
    assert view.missing == (son.display_name,)


def test_nothing_planned_raises(db_session, seeded) -> None:
    for planned in db_session.exec(select(PlannedSession)).all():
        planned.status = "done"
        db_session.add(planned)
    db_session.commit()
    with pytest.raises(TodayError):
        resolve_today(db_session, PROFILE_ME)
