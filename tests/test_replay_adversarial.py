"""The offline replay target under abuse.

``POST /api/sessions/{id}/rows/{position}`` and ``.../done`` are what a phone drains its
IndexedDB queue into, so they are reachable by a client nobody is watching: a replay of an
operation the user has since undone, a body a hand-rolled encoder produced, the same payload
three times because the radio dropped between the write and the acknowledgement.

These tests pin what each of those *actually* does, including the two places where the answer
is "accepted on purpose" (D-073, D-082). A future refactor that tightens either has to change
a test that says so, rather than one that quietly passes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlmodel import select

from cadence.api.envelope import ENVELOPE_KEYS
from cadence.seance.tables import SessionRecord, SessionRowRecord

JSON = {"Content-Type": "application/json"}


async def _today(client: httpx.AsyncClient, profile: str = "me") -> dict[str, Any]:
    response = await client.get(f"/api/today?profile={profile}")
    assert response.status_code == 200
    return response.json()["data"]


async def _row_now(client: httpx.AsyncClient, position: int, profile: str = "me") -> dict[str, Any]:
    data = await _today(client, profile)
    return next(row for row in data["rows"] if row["position"] == position)


async def _walk_to(client: httpx.AsyncClient, predicate, profile: str = "me") -> tuple[str, dict[str, Any]]:
    """Finish sessions until a row matching ``predicate`` is on Today, and return it."""
    for _ in range(8):
        data = await _today(client, profile)
        found = next((row for row in data["rows"] if predicate(row)), None)
        if found is not None:
            return data["session_id"], found
        await client.post(f"/api/sessions/{data['session_id']}/done", json={})
    raise AssertionError(f"no matching row in the {profile} block")


# --------------------------------------------------------------------------- replaying a tick


async def test_replaying_one_tick_three_times_leaves_one_done_row(seeded_client: httpx.AsyncClient, db_session) -> None:
    """A queue that retries the same payload must not accumulate anything at all.

    Three identical POSTs: the first writes, the second and third are no-ops that return the
    row as it stands. One session, one row at that position, one ``done_at``.
    """
    session_id = (await _today(seeded_client))["session_id"]
    payload = {"done": True, "ts": datetime.now(UTC).isoformat()}

    replies = [await seeded_client.post(f"/api/sessions/{session_id}/rows/1", json=payload) for _ in range(3)]

    assert [reply.status_code for reply in replies] == [200, 200, 200]
    assert [reply.json()["meta"]["changed"] for reply in replies] == [True, False, False]
    stamps = {reply.json()["data"]["done_at"] for reply in replies}
    assert len(stamps) == 1, f"the replays moved done_at: {stamps}"
    assert all(reply.json()["data"]["done"] is True for reply in replies)

    rows = db_session.exec(
        select(SessionRowRecord).where(SessionRowRecord.session_id == session_id, SessionRowRecord.position == 1)
    ).all()
    assert len(rows) == 1
    assert len(db_session.exec(select(SessionRecord)).all()) == 1


async def test_a_stale_tick_after_an_untick_is_accepted(seeded_client: httpx.AsyncClient) -> None:
    """D-073's named residual, pinned exactly rather than left to the reader.

    Ordering is last-write-wins against the *stored* ``done_at`` only. An untick clears that
    column, so a tick queued before the untick and drained after it has nothing to lose to and
    is applied - and it writes its own older timestamp. Closing this needs a ``last_op_at``
    column architecture section 3 does not have.

    If a later PRP adds that column, this test fails on purpose. Rewrite it then; do not delete
    it, because the assertion that the residual is *gone* is the same three facts inverted.
    """
    session_id = (await _today(seeded_client))["session_id"]
    early = datetime.now(UTC) - timedelta(minutes=5)
    late = datetime.now(UTC)

    await seeded_client.post(f"/api/sessions/{session_id}/rows/1", json={"done": True, "ts": late.isoformat()})
    await seeded_client.post(f"/api/sessions/{session_id}/rows/1", json={"done": False, "ts": late.isoformat()})
    assert (await _row_now(seeded_client, 1))["done"] is False

    replayed = await seeded_client.post(
        f"/api/sessions/{session_id}/rows/1", json={"done": True, "ts": early.isoformat()}
    )

    # Fact 1: the stale tick is accepted, not dropped.
    assert replayed.status_code == 200
    assert replayed.json()["meta"]["changed"] is True
    # Fact 2: the row is done again.
    assert replayed.json()["data"]["done"] is True
    # Fact 3: and it carries the *older* timestamp, which is what makes this a residual.
    assert replayed.json()["data"]["done_at"] == early.isoformat()

    stored = await _row_now(seeded_client, 1)
    assert stored["done"] is True and stored["done_at"] == early.isoformat()


async def test_a_stale_tick_against_a_live_done_at_is_dropped(seeded_client: httpx.AsyncClient) -> None:
    """The half of D-073 that does work: an untick older than the stored tick loses."""
    session_id = (await _today(seeded_client))["session_id"]
    now = datetime.now(UTC)
    await seeded_client.post(f"/api/sessions/{session_id}/rows/1", json={"done": True, "ts": now.isoformat()})

    stale = await seeded_client.post(
        f"/api/sessions/{session_id}/rows/1",
        json={"done": False, "ts": (now - timedelta(hours=1)).isoformat()},
    )

    assert stale.json()["meta"]["changed"] is False
    assert (await _row_now(seeded_client, 1))["done"] is True


# ------------------------------------------------------------------------ reps: clamped, typed


@pytest.mark.parametrize(("sent", "stored"), [(-5, 0), (0, 0), (1, 1), (99999, 200), (200, 200)])
async def test_reps_out_of_range_are_clamped_not_refused(
    seeded_client: httpx.AsyncClient, sent: int, stored: int
) -> None:
    """D-082: ``reps_done`` is bounded by 0 and 200, and the bound clamps rather than rejects.

    The brief for this pass expected 422 here. It is 200 with the value clamped, which is the
    designed contract: a queue drained days later must not lose a whole session to one silly
    number. Pinned so that turning it into a refusal is a deliberate change.

    The floor was 1 until the D-082 addendum: zero is a real answer -- the row was attempted and
    none of it completed -- so clamping it up rewrote an honest number. A type violation is still
    422; only the range widened.
    """
    session_id, row = await _walk_to(seeded_client, lambda row: row["reps"] is not None and row["adjustable"])

    response = await seeded_client.post(f"/api/sessions/{session_id}/rows/{row['position']}", json={"reps_done": sent})

    assert response.status_code == 200
    assert response.json()["data"]["reps_done"] == stored
    assert (await _row_now(seeded_client, row["position"]))["reps_done"] == stored


@pytest.mark.parametrize("value", [1.5, True, False, "8", None, [8], {"n": 8}])
async def test_reps_of_the_wrong_type_are_refused_and_write_nothing(
    seeded_client: httpx.AsyncClient, value: Any
) -> None:
    """``StrictInt``: a bool is not a rep count, and neither is the string a form would send.

    ``None`` is the exception - the schema allows it, and ``apply_patch`` treats an explicit
    null as "this field was not sent", so it is a 200 that writes nothing.
    """
    session_id, row = await _walk_to(seeded_client, lambda row: row["reps"] is not None and row["adjustable"])
    before = await _row_now(seeded_client, row["position"])

    response = await seeded_client.post(f"/api/sessions/{session_id}/rows/{row['position']}", json={"reps_done": value})

    assert response.status_code == (200 if value is None else 422)
    after = await _row_now(seeded_client, row["position"])
    assert after["reps_done"] == before["reps_done"]
    assert after["done"] == before["done"]


@pytest.mark.parametrize("raw", ['{"reps_done": NaN}', '{"reps_done": Infinity}', '{"done": NaN}'])
async def test_a_body_no_encoder_should_produce_answers_in_the_envelope(
    seeded_client: httpx.AsyncClient, raw: str
) -> None:
    """A ``NaN`` in the body must not 500 the replay target.

    ``JSON.stringify`` emits ``null`` for these, but the queue is drained by code a later PRP
    may rewrite, and FastAPI's default validation handler echoes the offending input back -
    which no JSON encoder will serialise. That answered 500 with a plain-text body, and
    ``app.js`` retries on 5xx and stops on the first failure, so one such record wedged the
    whole queue for good. It is a 422 in the envelope now.
    """
    session_id = (await _today(seeded_client))["session_id"]

    response = await seeded_client.post(f"/api/sessions/{session_id}/rows/1", content=raw, headers=JSON)

    assert response.status_code == 422
    assert set(response.json()) == set(ENVELOPE_KEYS)
    assert response.json()["error"] and response.json()["ok"] is False
    assert "Traceback" not in response.text
    assert (await _row_now(seeded_client, 1))["done"] is False


async def test_youth_reps_are_not_capped_by_the_band(aged_son_client: httpx.AsyncClient) -> None:
    """D-082's other half: a rep count above the band's ``rep_max_*`` is recorded as given.

    Load is a safety limit and is clamped (``test_api_load_write_is_clamped_to_the_youth_cap``);
    reps are a record of what the child actually did, and rewriting an honest number would make
    the history a lie. This is the test that stops a tidying refactor from adding the symmetry.
    """
    session_id, row = await _walk_to(
        aged_son_client, lambda row: row["reps"] is not None and row["adjustable"], profile="son"
    )
    assert row["reps"] < 60, "the planned prescription is already clamped at build time (D-063)"

    response = await aged_son_client.post(f"/api/sessions/{session_id}/rows/{row['position']}", json={"reps_done": 150})

    assert response.status_code == 200
    assert response.json()["data"]["reps_done"] == 150
    assert (await _row_now(aged_son_client, row["position"], "son"))["reps_done"] == 150


# ------------------------------------------------------------------------ load: capped, finite


@pytest.mark.parametrize("raw", ['{"load_done_kg": -0.5}', '{"load_done_kg": NaN}', '{"load_done_kg": Infinity}'])
async def test_a_load_that_is_not_a_real_number_is_refused_with_400(
    aged_son_client: httpx.AsyncClient, raw: str
) -> None:
    """Sent as raw text, because a strict encoder will not produce these and a loose one will.

    Exactly 400, from the ``isfinite`` guard in ``_write_load``: these parse as floats, so they
    reach the service rather than being caught by the schema.
    """
    session_id, row = await _walk_to(aged_son_client, lambda row: row["load_kg"] is not None, profile="son")

    response = await aged_son_client.post(
        f"/api/sessions/{session_id}/rows/{row['position']}", content=raw, headers=JSON
    )

    assert response.status_code == 400
    assert response.json()["error"] and set(response.json()) == set(ENVELOPE_KEYS)
    assert (await _row_now(aged_son_client, row["position"], "son"))["load_done_kg"] is None


async def test_a_youth_load_above_the_cap_is_clamped_while_its_reps_are_not(
    aged_son_client: httpx.AsyncClient,
) -> None:
    """One request carrying both: the asymmetry of D-082 in a single write."""
    session_id, row = await _walk_to(aged_son_client, lambda row: row["load_kg"] is not None, profile="son")

    response = await aged_son_client.post(
        f"/api/sessions/{session_id}/rows/{row['position']}", json={"load_done_kg": 250.0, "reps_done": 199}
    )

    payload = response.json()["data"]
    assert payload["load_done_kg"] <= 8.0, "the band cap is a safety limit and holds"
    assert payload["reps_done"] == 199, "the rep count is a record and is taken as given"
    assert response.json()["meta"]["notice"], "a clamped load tells the user, in the row's own words"
    assert "weight" not in response.json()["meta"]["notice"].lower()


# ----------------------------------------------------------------- writes after the final whistle


@pytest.mark.parametrize(
    "patch", [{"done": True}, {"done": False}, {"reps_done": 5}, {"load_done_kg": 4.0}, {"ts": "2026-09-06T08:00:00Z"}]
)
async def test_every_row_write_on_a_finished_session_is_refused(
    seeded_client: httpx.AsyncClient, db_session, patch: dict[str, Any]
) -> None:
    """D-078: a queue drained after a Done must not reopen a session already summarised.

    409 for every field, not just ``done``: from PRP-06 the session has been posted to
    VitalForge by the time a late record arrives, and a reps edit would desynchronise it just
    as thoroughly as a tick. The row is read back from the database, because once the session
    is finished Today has already moved on to the next planned day.
    """
    session_id = (await _today(seeded_client))["session_id"]
    await seeded_client.post(f"/api/sessions/{session_id}/rows/1", json={"done": True, "reps_done": 7})
    await seeded_client.post(f"/api/sessions/{session_id}/done", json={})

    response = await seeded_client.post(f"/api/sessions/{session_id}/rows/1", json=patch)

    assert response.status_code == 409
    assert response.json()["error"] and response.json()["ok"] is False
    stored = db_session.exec(
        select(SessionRowRecord).where(SessionRowRecord.session_id == session_id, SessionRowRecord.position == 1)
    ).one()
    db_session.refresh(stored)
    assert stored.done is True and stored.reps_done == 7, "a refused write still moved the row"


async def test_done_twice_does_not_finalise_twice(seeded_client: httpx.AsyncClient) -> None:
    """A double tap, or a queued Done drained after the user already finished on another tab."""
    data = await _today(seeded_client)
    session_id = data["session_id"]
    await seeded_client.post(f"/api/sessions/{session_id}/rows/1", json={"done": True})

    first = await seeded_client.post(f"/api/sessions/{session_id}/done", json={"felt": "easy"})
    summary_page = (await seeded_client.get(f"/done/{session_id}?profile=me")).text
    second = await seeded_client.post(f"/api/sessions/{session_id}/done", json={"felt": "hard"})

    assert first.status_code == second.status_code == 200
    assert second.json()["data"] == first.json()["data"], "the second Done returned a different summary"
    assert first.json()["meta"]["replayed"] is False
    assert second.json()["meta"]["replayed"] is True
    assert second.json()["data"]["felt"] == "easy", "the replay overwrote how it felt"
    assert (await seeded_client.get(f"/done/{session_id}?profile=me")).text == summary_page

    # And no second session was opened behind it: Today has moved to the next planned day.
    assert (await _today(seeded_client))["session_id"] != session_id


async def test_done_does_not_reopen_the_planned_day(seeded_client: httpx.AsyncClient) -> None:
    """The finished session stays finished however many times the queue asks."""
    session_id = (await _today(seeded_client))["session_id"]
    await seeded_client.post(f"/api/sessions/{session_id}/done", json={})
    stamp = (await seeded_client.post(f"/api/sessions/{session_id}/done", json={})).json()["data"]

    for _ in range(3):
        again = await seeded_client.post(f"/api/sessions/{session_id}/done", json={"felt": "hard"})
        assert again.json()["data"] == stamp
