"""A row can record the sets actually done and the seconds actually held (not just reps and load)."""

from __future__ import annotations

import httpx
import pytest


async def _today(client: httpx.AsyncClient) -> dict:
    response = await client.get("/api/today?profile=me")
    assert response.status_code == 200
    return response.json()["data"]


async def _adjustable_row(client: httpx.AsyncClient) -> tuple[str, dict]:
    data = await _today(client)
    row = next(r for r in data["rows"] if r["adjustable"])
    return data["session_id"], row


async def test_sets_done_and_seconds_done_are_recorded(seeded_client: httpx.AsyncClient) -> None:
    session_id, row = await _adjustable_row(seeded_client)
    response = await seeded_client.post(
        f"/api/sessions/{session_id}/rows/{row['position']}", json={"sets_done": 2, "seconds_done": 41}
    )
    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["sets_done"] == 2
    assert payload["seconds_done"] == 41
    stored = next(r for r in (await _today(seeded_client))["rows"] if r["position"] == row["position"])
    assert (stored["sets_done"], stored["seconds_done"]) == (2, 41)


@pytest.mark.parametrize(("sent", "stored"), [(-3, 0), (9999, 100)])
async def test_sets_done_is_clamped(seeded_client: httpx.AsyncClient, sent: int, stored: int) -> None:
    session_id, row = await _adjustable_row(seeded_client)
    response = await seeded_client.post(f"/api/sessions/{session_id}/rows/{row['position']}", json={"sets_done": sent})
    assert response.json()["data"]["sets_done"] == stored


@pytest.mark.parametrize(("sent", "stored"), [(-3, 0), (99999, 3600)])
async def test_seconds_done_is_clamped(seeded_client: httpx.AsyncClient, sent: int, stored: int) -> None:
    session_id, row = await _adjustable_row(seeded_client)
    response = await seeded_client.post(
        f"/api/sessions/{session_id}/rows/{row['position']}", json={"seconds_done": sent}
    )
    assert response.json()["data"]["seconds_done"] == stored


@pytest.mark.parametrize("body", ['{"sets_done": "2"}', '{"seconds_done": 4.5}', '{"sets_done": true}'])
async def test_type_violations_are_422(seeded_client: httpx.AsyncClient, body: str) -> None:
    session_id, row = await _adjustable_row(seeded_client)
    response = await seeded_client.post(
        f"/api/sessions/{session_id}/rows/{row['position']}",
        content=body,
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 422
