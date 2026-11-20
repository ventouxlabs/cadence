"""The web steppers for sets and held seconds write through the same clamp as the JSON API."""

from __future__ import annotations

import httpx
import pytest

from cadence.profils.settings import DEFAULT_SETTINGS
from cadence.seance import done as done_service
from cadence.seance import ticks as tick_service
from cadence.seance.today import resolve_today
from cadence.web.rendering import prescription

MAX_SESSIONS = 14


def _walk(db, predicate):
    for _ in range(MAX_SESSIONS):
        view = resolve_today(db, "me").primary
        found = next((row for row in view.rows if predicate(row)), None)
        if found is not None:
            return view, found
        done_service.finish(db, view)
    raise AssertionError("no matching row in the next sessions")


def _timed(row) -> bool:
    return row.spec.get("seconds") is not None and row.reps_adjustable


def _step(db, view, row, field, delta):
    return tick_service.adjust(db, view, row.position, field, delta, DEFAULT_SETTINGS)


def test_sets_step_by_one_and_persist(db_session, seeded) -> None:
    view, row = _walk(db_session, _timed)
    planned = row.sets
    assert _step(db_session, view, row, "sets", 1).changed
    assert resolve_today(db_session, "me").primary.row(row.position).record.sets_done == planned + 1


def test_sets_clamp_at_zero_and_max(db_session, seeded) -> None:
    view, row = _walk(db_session, _timed)
    row.record.sets_done = 0
    assert not _step(db_session, view, row, "sets", -1).changed
    row.record.sets_done = tick_service.MAX_SETS
    assert not _step(db_session, view, row, "sets", 1).changed


def test_seconds_step_by_five_and_persist(db_session, seeded) -> None:
    view, row = _walk(db_session, _timed)
    planned = row.seconds
    assert _step(db_session, view, row, "seconds", 1).changed
    assert resolve_today(db_session, "me").primary.row(row.position).record.seconds_done == planned + 5


def test_seconds_clamp(db_session, seeded) -> None:
    view, row = _walk(db_session, _timed)
    row.record.seconds_done = 2
    _step(db_session, view, row, "seconds", -1)
    assert row.record.seconds_done == 0
    row.record.seconds_done = tick_service.MAX_SECONDS - 2
    _step(db_session, view, row, "seconds", 1)
    assert row.record.seconds_done == tick_service.MAX_SECONDS


def test_prescription_shows_held_seconds_and_sets(db_session, seeded) -> None:
    view, row = _walk(db_session, lambda r: _timed(r) and r.reps is None)
    row.record.seconds_done = 41
    row.record.sets_done = 2
    assert prescription(row).startswith("2 x 41 s")


async def test_adjust_route_writes_sets_and_seconds_and_shows_steppers(seeded_client, db_session) -> None:
    view, row = _walk(db_session, _timed)
    url = f"/today/{view.record.id}/rows/{row.position}/adjust?profile=me"
    headers = {"HX-Request": "true"}
    opened = await seeded_client.post(url, data={"expand": "1"}, headers=headers)
    assert 'aria-label="Seconds held"' in opened.text
    assert 'aria-label="Sets done"' in opened.text
    await seeded_client.post(url, data={"seconds": "1"}, headers=headers)
    await seeded_client.post(url, data={"sets": "-1"}, headers=headers)
    stored = next(r for r in (await _today(seeded_client))["rows"] if r["position"] == row.position)
    assert stored["seconds_done"] == row.seconds + 5
    assert stored["sets_done"] == row.sets - 1


async def _today(client: httpx.AsyncClient) -> dict:
    return (await client.get("/api/today?profile=me")).json()["data"]


def test_unknown_field_still_refused(db_session, seeded) -> None:
    view, row = _walk(db_session, _timed)
    with pytest.raises(tick_service.TickError):
        _step(db_session, view, row, "bogus", 1)  # type: ignore[arg-type]
