"""PRP-03 review finding 3: the first-run form cannot be replayed against a completed setup."""

from __future__ import annotations

import httpx


async def test_setup_post_after_completion_is_refused_and_writes_nothing(
    seeded_client: httpx.AsyncClient,
) -> None:
    before = (await seeded_client.get("/api/settings")).json()["data"]
    assert before["setup_complete"] is True

    response = await seeded_client.post(
        "/setup",
        data={
            "equipment": ["bodyweight"],
            "days_per_week": "6",
            "session_minutes": "45",
            "display_unit": "lb",
            "son_age": "9",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/settings"
    after = (await seeded_client.get("/api/settings")).json()["data"]
    assert after == before
