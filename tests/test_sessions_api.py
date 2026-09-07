"""The JSON API and the HTML routes: envelope shape, idempotency, and the offline replay targets.

PRP-02 acceptance tests 13-16, plus the web routes the Playwright suite drives.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from cadence.api.envelope import ENVELOPE_KEYS
from cadence.programme.next_time import PLACEHOLDER_DEFAULT

HX = {"HX-Request": "true"}


async def _today(client: httpx.AsyncClient, profile: str = "me") -> dict:
    response = await client.get(f"/api/today?profile={profile}")
    assert response.status_code == 200
    return response.json()["data"]


# -------------------------------------------------------------------------- 13, 14: shape


async def test_api_today_shape(seeded_client: httpx.AsyncClient) -> None:
    response = await seeded_client.get("/api/today?profile=me")
    body = response.json()

    assert set(body) == set(ENVELOPE_KEYS)
    assert body["ok"] is True and body["error"] is None
    assert body["meta"]["generated_at"]
    data = body["data"]
    assert data["display_unit"] == "kg"
    assert data["good_enough_after"] == 1
    assert data["together_group_id"] is None
    assert data["week"] >= 1
    row = data["rows"][0]
    for key in ("position", "exercise_id", "name", "role", "sets", "measure", "done", "cue", "notes"):
        assert key in row
    assert row["done"] is False


async def test_api_today_together_carries_both_sessions(seeded_client: httpx.AsyncClient) -> None:
    data = await _today(seeded_client, "together")
    assert len(data["sessions"]) == 2
    assert data["sessions"][1]["good_enough_after"] >= 3
    assert data["together_group_id"]


async def test_api_today_rejects_an_unknown_profile(seeded_client: httpx.AsyncClient) -> None:
    response = await seeded_client.get("/api/today?profile=cat")
    assert response.status_code == 404
    assert response.json()["error"]
    assert response.json()["ok"] is False


async def test_no_network_call_on_today(seeded_client: httpx.AsyncClient, monkeypatch) -> None:
    """Today is the hot path: nothing on it may reach the network (architecture section 5).

    The block is at the socket, not at one client class. Patching ``httpx.Client`` alone proves
    very little here - the test client is an ``AsyncClient`` over an in-process transport, so it
    never touches the sync class, and a VitalForge call made with ``requests``, ``urllib`` or an
    async client would sail straight past. Nothing in this path opens a socket at all: the
    database is a file and the transport is a function call.
    """
    import socket

    def explode(*args, **kwargs):  # pragma: no cover - the assertion is that it never runs
        raise AssertionError("Today reached the network")

    monkeypatch.setattr(socket.socket, "connect", explode)
    monkeypatch.setattr(socket.socket, "connect_ex", explode)
    monkeypatch.setattr(httpx.Client, "request", explode)
    monkeypatch.setattr(httpx.Client, "send", explode)
    # Not ``httpx.AsyncClient``: that is the test client's own class, and patching it would
    # only ever prove that this test can break itself. The socket is the honest boundary.

    # The guard has to be able to fire, or the rest of this test is a comment. A patch aimed at
    # the wrong layer catches nothing and passes just as quietly as a page that made no call.
    with pytest.raises(AssertionError, match="reached the network"):
        httpx.get("http://127.0.0.1:1/")

    for path in ("/today?profile=me", "/today?profile=son", "/today?profile=together"):
        assert (await seeded_client.get(path)).status_code == 200
    for path in ("/api/today?profile=me", "/api/today?profile=son", "/api/today?profile=together"):
        assert (await seeded_client.get(path)).status_code == 200


async def test_no_network_call_when_finishing_a_session(seeded_client: httpx.AsyncClient, monkeypatch) -> None:
    """The write path is offline too, until PRP-06 adds the VitalForge push behind a queue.

    A tick or a Done that blocked on a socket would strand the person mid-workout on the exact
    connection this app is built to survive losing.
    """
    import socket

    session_id = (await _today(seeded_client))["session_id"]

    def explode(*args, **kwargs):  # pragma: no cover - the assertion is that it never runs
        raise AssertionError("finishing a session reached the network")

    monkeypatch.setattr(socket.socket, "connect", explode)
    monkeypatch.setattr(socket.socket, "connect_ex", explode)

    assert (await seeded_client.post(f"/api/sessions/{session_id}/rows/1", json={"done": True})).status_code == 200
    assert (await seeded_client.post(f"/api/sessions/{session_id}/done", json={"felt": "easy"})).status_code == 200
    assert (await seeded_client.get(f"/done/{session_id}?profile=me")).status_code == 200


# ------------------------------------------------------------------ row writes and replays


async def test_row_write_is_idempotent(seeded_client: httpx.AsyncClient) -> None:
    data = await _today(seeded_client)
    sid = data["session_id"]
    stamp = datetime.now(UTC).isoformat()
    body = {"done": True, "ts": stamp}

    first = await seeded_client.post(f"/api/sessions/{sid}/rows/1", json=body)
    second = await seeded_client.post(f"/api/sessions/{sid}/rows/1", json=body)

    assert first.status_code == second.status_code == 200
    assert first.json()["data"]["done"] is True
    assert second.json()["data"]["done"] is True
    assert second.json()["data"]["done_at"] == first.json()["data"]["done_at"]
    assert second.json()["meta"]["changed"] is False


async def test_row_write_ignores_a_stale_timestamp(seeded_client: httpx.AsyncClient) -> None:
    data = await _today(seeded_client)
    sid = data["session_id"]
    now = datetime.now(UTC)
    await seeded_client.post(f"/api/sessions/{sid}/rows/1", json={"done": True, "ts": now.isoformat()})

    stale = (now - timedelta(minutes=5)).isoformat()
    response = await seeded_client.post(f"/api/sessions/{sid}/rows/1", json={"done": False, "ts": stale})

    assert response.json()["data"]["done"] is True


async def test_row_write_touches_only_the_fields_sent(seeded_client: httpx.AsyncClient) -> None:
    data = await _today(seeded_client)
    sid = data["session_id"]
    target = next(row for row in data["rows"] if row["reps"] is not None and row["adjustable"])

    response = await seeded_client.post(
        f"/api/sessions/{sid}/rows/{target['position']}", json={"reps_done": target["reps"] + 1}
    )

    payload = response.json()["data"]
    assert payload["reps_done"] == target["reps"] + 1
    assert payload["done"] is False
    assert payload["reps"] == target["reps"]


async def test_row_position_out_of_range(seeded_client: httpx.AsyncClient) -> None:
    sid = (await _today(seeded_client))["session_id"]
    response = await seeded_client.post(f"/api/sessions/{sid}/rows/99", json={"done": True})
    assert response.status_code == 404
    assert response.json()["error"]
    assert response.json()["data"] is None


async def test_unknown_session_id(seeded_client: httpx.AsyncClient) -> None:
    response = await seeded_client.post("/api/sessions/not-a-session/rows/1", json={"done": True})
    assert response.status_code == 404
    assert response.json()["error"] and "Traceback" not in response.text
    assert set(response.json()) == set(ENVELOPE_KEYS)


async def test_adjust_on_a_distance_row_is_rejected(seeded_client: httpx.AsyncClient) -> None:
    for _ in range(6):
        data = await _today(seeded_client)
        carry = next((row for row in data["rows"] if row["measure"] == "meters"), None)
        if carry is not None:
            break
        await seeded_client.post(f"/api/sessions/{data['session_id']}/done", json={})
    assert carry is not None, "no distance row in the parent's block"

    response = await seeded_client.post(
        f"/api/sessions/{data['session_id']}/rows/{carry['position']}", json={"reps_done": 5}
    )

    assert response.status_code == 400
    assert response.json()["error"]


async def test_row_write_rejects_an_unknown_field(seeded_client: httpx.AsyncClient) -> None:
    sid = (await _today(seeded_client))["session_id"]
    response = await seeded_client.post(f"/api/sessions/{sid}/rows/1", json={"sets_done": 9})
    assert response.status_code == 422


async def test_row_write_on_a_finished_session_is_refused(seeded_client: httpx.AsyncClient) -> None:
    sid = (await _today(seeded_client))["session_id"]
    await seeded_client.post(f"/api/sessions/{sid}/done", json={})
    response = await seeded_client.post(f"/api/sessions/{sid}/rows/1", json={"done": True})
    assert response.status_code == 409
    assert response.json()["error"]


# ------------------------------------------------------------------------------- done API


async def test_done_api_returns_the_summary(seeded_client: httpx.AsyncClient) -> None:
    data = await _today(seeded_client)
    sid = data["session_id"]
    for row in data["rows"]:
        await seeded_client.post(f"/api/sessions/{sid}/rows/{row['position']}", json={"done": True})

    response = await seeded_client.post(f"/api/sessions/{sid}/done", json={"felt": "right"})

    payload = response.json()["data"]
    assert payload["completion"] == "complete"
    assert payload["rows_done"] == payload["rows_total"] == len(data["rows"])
    # PRP-06: the fixtures run in mock mode, so the inline write-back succeeds and the summary
    # reports the job rather than the "nothing has queued this" default.
    assert payload["sync"] == "sent"
    assert payload["next_time_note"]
    assert payload["felt"] == "right"


async def test_done_api_is_idempotent(seeded_client: httpx.AsyncClient) -> None:
    sid = (await _today(seeded_client))["session_id"]
    first = await seeded_client.post(f"/api/sessions/{sid}/done", json={"felt": "easy"})
    second = await seeded_client.post(f"/api/sessions/{sid}/done", json={"felt": "hard"})

    assert first.status_code == second.status_code == 200
    assert second.json()["data"] == first.json()["data"]
    assert second.json()["meta"]["replayed"] is True


async def test_done_api_finalises_a_together_group(seeded_client: httpx.AsyncClient) -> None:
    data = await _today(seeded_client, "together")
    partner = data["sessions"][1]["session_id"]

    response = await seeded_client.post(f"/api/sessions/{data['session_id']}/done", json={})

    assert len(response.json()["data"]["group"]) == 2
    replay = await seeded_client.post(f"/api/sessions/{partner}/done", json={})
    assert replay.json()["meta"]["replayed"] is True


async def test_next_time_note_is_the_placeholder(seeded_client: httpx.AsyncClient) -> None:
    sid = (await _today(seeded_client))["session_id"]
    body = (await seeded_client.post(f"/api/sessions/{sid}/done", json={})).json()
    assert body["data"]["next_time_note"] == PLACEHOLDER_DEFAULT


async def test_done_on_an_unknown_session(seeded_client: httpx.AsyncClient) -> None:
    response = await seeded_client.post("/api/sessions/nope/done", json={})
    assert response.status_code == 404
    assert response.json()["error"]


# ---------------------------------------------------------- the clamp on the replay target


async def _son_row(client: httpx.AsyncClient, predicate) -> tuple[str, dict]:
    """Walk the son's block until a row matching ``predicate`` shows up on Today."""
    for _ in range(8):
        data = (await client.get("/api/today?profile=son")).json()["data"]
        found = next((row for row in data["rows"] if predicate(row)), None)
        if found is not None:
            return data["session_id"], found
        await client.post(f"/api/sessions/{data['session_id']}/done", json={})
    raise AssertionError("no matching row in the son's block")


async def test_api_load_write_is_clamped_to_the_youth_cap(aged_son_client: httpx.AsyncClient) -> None:
    """Risk 11 on the path an offline client uses: an absolute load, not a stepper press."""
    session_id, row = await _son_row(aged_son_client, lambda row: row["load_kg"] is not None)

    response = await aged_son_client.post(
        f"/api/sessions/{session_id}/rows/{row['position']}", json={"load_done_kg": 250.0}
    )

    payload = response.json()
    assert response.status_code == 200
    assert payload["data"]["load_done_kg"] <= 8.0
    assert payload["meta"]["notice"]
    stored = (await aged_son_client.get("/api/today?profile=son")).json()["data"]
    assert next(item for item in stored["rows"] if item["position"] == row["position"])["load_done_kg"] <= 8.0


async def test_api_refuses_a_load_on_a_bodyweight_row(seeded_client: httpx.AsyncClient) -> None:
    """A row the plan gave no load has no cappable unit, so it takes no load at all."""
    session_id, row = await _son_row(seeded_client, lambda row: row["load_kg"] is None and row["measure"] == "reps")

    response = await seeded_client.post(
        f"/api/sessions/{session_id}/rows/{row['position']}", json={"load_done_kg": 500.0}
    )

    assert response.status_code == 400
    assert "bodyweight" in response.json()["error"]
    stored = (await seeded_client.get("/api/today?profile=son")).json()["data"]
    assert next(item for item in stored["rows"] if item["position"] == row["position"])["load_done_kg"] is None


@pytest.mark.parametrize("body", ['{"load_done_kg": -5.0}', '{"load_done_kg": Infinity}', '{"load_done_kg": NaN}'])
async def test_api_refuses_a_load_that_is_not_a_real_weight(aged_son_client: httpx.AsyncClient, body: str) -> None:
    """Sent as raw text: a strict JSON encoder will not produce these, and a hand-rolled one will."""
    session_id, row = await _son_row(aged_son_client, lambda row: row["load_kg"] is not None)

    response = await aged_son_client.post(
        f"/api/sessions/{session_id}/rows/{row['position']}",
        content=body,
        headers={"Content-Type": "application/json"},
    )

    # Exactly 400, from the ``isfinite`` guard: all three parse as floats, so they reach the
    # service rather than being turned away by the schema. Pinned rather than hedged, because
    # which of the two rejections fires is the difference between a guarded write and a lucky one.
    assert response.status_code == 400
    assert response.json()["error"]
    stored = (await aged_son_client.get("/api/today?profile=son")).json()["data"]
    assert next(item for item in stored["rows"] if item["position"] == row["position"])["load_done_kg"] is None


# ------------------------------------------------- Codex E: the client clock is ordinary input


async def test_a_naive_timestamp_is_refused(seeded_client: httpx.AsyncClient) -> None:
    """The three plausible readings of a naive ts differ by hours, and guessing wrong misfiles a
    Garmin activity by that much. A client that cannot say which zone it meant has not said one."""
    data = await _today(seeded_client)
    sid = data["session_id"]

    response = await seeded_client.post(f"/api/sessions/{sid}/rows/1", json={"done": True, "ts": "2026-09-06T08:00:00"})

    assert response.status_code == 422
    assert response.json()["ok"] is False
    assert "ts" in response.json()["error"]


async def test_a_future_timestamp_is_refused_at_the_boundary(seeded_client: httpx.AsyncClient) -> None:
    """Pairs with the payload clamp: a row already stored cannot be rejected retrospectively, so
    the boundary refuses what it can and the payload builder clamps the rest."""
    from datetime import UTC, datetime, timedelta

    data = await _today(seeded_client)
    sid = data["session_id"]
    ahead = (datetime.now(UTC) + timedelta(hours=1)).isoformat()

    row = await seeded_client.post(f"/api/sessions/{sid}/rows/1", json={"done": True, "ts": ahead})
    done = await seeded_client.post(f"/api/sessions/{sid}/done", json={"ts": ahead})

    assert row.status_code == 422
    assert done.status_code == 422


async def test_an_implausibly_old_timestamp_is_refused(seeded_client: httpx.AsyncClient) -> None:
    data = await _today(seeded_client)

    response = await seeded_client.post(
        f"/api/sessions/{data['session_id']}/rows/1", json={"done": True, "ts": "1970-01-01T00:00:00+00:00"}
    )

    assert response.status_code == 422


async def test_a_slightly_skewed_clock_is_still_accepted(seeded_client: httpx.AsyncClient) -> None:
    """Sixty seconds of tolerance, the same as VitalForge's: a phone is not an atomic clock and a
    tick taken a moment ago must not be refused for arriving a moment ahead."""
    from datetime import UTC, datetime, timedelta

    data = await _today(seeded_client)
    ts = (datetime.now(UTC) + timedelta(seconds=30)).isoformat()

    response = await seeded_client.post(f"/api/sessions/{data['session_id']}/rows/1", json={"done": True, "ts": ts})

    assert response.status_code == 200
