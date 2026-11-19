"""Loaded carry rows: the load adjusts, the distance never does (sf-2026-11-16#2)."""

from __future__ import annotations

import httpx
import pytest

from cadence.profils.settings import DEFAULT_SETTINGS
from cadence.profils.tables import PROFILE_SON
from cadence.seance import done as done_service
from cadence.seance import ticks as tick_service
from cadence.seance.today import resolve_today

MAX_SESSIONS = 14


def _walk(db, profile: str, predicate):
    """The first row of the profile's coming sessions that satisfies ``predicate``."""
    for _ in range(MAX_SESSIONS):
        view = resolve_today(db, profile).primary
        found = next((row for row in view.rows if predicate(row)), None)
        if found is not None:
            return view, found
        done_service.finish(db, view)
    raise AssertionError("no matching row in the next sessions")


def _loaded_carry(row) -> bool:
    return row.measure == "meters" and row.spec.get("load_kg") is not None


def _bodyweight_carry(row) -> bool:
    return row.measure == "meters" and row.spec.get("load_kg") is None


def test_a_loaded_carry_is_load_adjustable_but_not_reps_adjustable(db_session, seeded) -> None:
    _, row = _walk(db_session, "me", _loaded_carry)
    assert row.load_adjustable is True
    assert row.reps_adjustable is False
    assert row.adjustable is True


def test_a_bodyweight_carry_stays_tick_only(db_session, seeded) -> None:
    _, row = _walk(db_session, PROFILE_SON, _bodyweight_carry)
    assert (row.load_adjustable, row.reps_adjustable, row.adjustable) == (False, False, False)


def test_adjust_load_on_a_carry_row_steps_the_ladder(db_session, seeded) -> None:
    view, row = _walk(db_session, "me", _loaded_carry)
    before = row.load_kg
    result = tick_service.adjust(db_session, view, row.position, "load", 1, DEFAULT_SETTINGS)
    assert result.changed
    assert result.row.record.load_done_kg > before
    assert result.row.record.reps_done is None


def test_adjust_load_on_a_bodyweight_carry_row_is_refused(db_session, seeded) -> None:
    view, row = _walk(db_session, PROFILE_SON, _bodyweight_carry)
    with pytest.raises(tick_service.TickError):
        tick_service.adjust(db_session, view, row.position, "load", 1, DEFAULT_SETTINGS)
    assert row.record.load_done_kg is None


def test_adjust_reps_on_a_carry_row_is_refused(db_session, seeded) -> None:
    view, row = _walk(db_session, "me", _loaded_carry)
    with pytest.raises(tick_service.TickError):
        tick_service.adjust(db_session, view, row.position, "reps", 1, DEFAULT_SETTINGS)


def test_patch_seconds_or_reps_on_a_carry_row_is_refused(db_session, seeded) -> None:
    view, row = _walk(db_session, "me", _loaded_carry)
    with pytest.raises(tick_service.TickError):
        tick_service.apply_patch(db_session, view, row.position, {"reps_done": 5}, DEFAULT_SETTINGS)


def test_youth_carry_load_never_passes_the_band_cap(db_session, aged_son) -> None:
    view, row = _walk(db_session, PROFILE_SON, _loaded_carry)
    cap = tick_service.load_cap(view, row.spec)
    assert cap is not None
    last = None
    for _ in range(12):
        last = tick_service.adjust(
            db_session, resolve_today(db_session, PROFILE_SON).primary, row.position, "load", 1, DEFAULT_SETTINGS
        )
        if not last.changed:
            break
    final = resolve_today(db_session, PROFILE_SON).primary.row(row.position)
    assert final.load_kg <= cap + tick_service.LOAD_EPSILON
    assert last.notice and "weight" not in last.notice.lower()


async def _api_row(client: httpx.AsyncClient, db, predicate, profile: str = "me"):
    view, row = _walk(db, profile, predicate)
    return view.record.id, row


async def test_api_load_done_on_a_carry_row_is_accepted(seeded_client, db_session) -> None:
    session_id, row = await _api_row(seeded_client, db_session, _loaded_carry)
    response = await seeded_client.post(f"/api/sessions/{session_id}/rows/{row.position}", json={"load_done_kg": 24.0})
    assert response.status_code == 200
    assert response.json()["data"]["load_done_kg"] == 24.0


async def test_api_load_done_on_a_bodyweight_carry_is_refused(seeded_client, db_session) -> None:
    session_id, row = await _api_row(seeded_client, db_session, _bodyweight_carry, PROFILE_SON)
    response = await seeded_client.post(
        f"/api/sessions/{session_id}/rows/{row.position}?profile=son", json={"load_done_kg": 24.0}
    )
    assert response.status_code == 400


async def test_row_partial_shows_only_the_load_stepper_on_a_carry(seeded_client, db_session) -> None:
    session_id, row = await _api_row(seeded_client, db_session, _loaded_carry)
    response = await seeded_client.post(
        f"/today/{session_id}/rows/{row.position}/adjust?profile=me",
        data={"expand": "1"},
        headers={"HX-Request": "true"},
    )
    assert 'aria-label="Load used"' in response.text
    assert 'aria-label="Reps done"' not in response.text
