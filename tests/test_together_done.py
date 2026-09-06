"""Together mode: two checklists, one button, and the ways that goes wrong.

The shared Done is the only control in the app that finalises something the person tapping it
is not looking at. Everything here is about that: the confirm has to count both checklists, one
tap has to finish both sessions, and each session has to keep its own row in the database with
the shared ``together_group_id`` on it.

Rendering the Together tab is what attaches the group (D-075), so none of these tests may ask
for ``?profile=me`` in between: that detaches, on purpose, and would quietly turn a group test
into a solo one.
"""

from __future__ import annotations

from typing import Any

import httpx
from sqlmodel import select

from cadence.profils.tables import PROFILE_ME, PROFILE_SON
from cadence.seance.tables import SessionRecord, SessionRowRecord

HX = {"HX-Request": "true"}


async def _together(client: httpx.AsyncClient) -> dict[str, Any]:
    response = await client.get("/api/today?profile=together")
    assert response.status_code == 200
    data = response.json()["data"]
    assert len(data["sessions"]) == 2, "the fixture did not plan a day for both profiles"
    return data


async def _tick_all(client: httpx.AsyncClient, session: dict[str, Any]) -> None:
    for row in session["rows"]:
        reply = await client.post(f"/api/sessions/{session['session_id']}/rows/{row['position']}", json={"done": True})
        assert reply.status_code == 200


# ------------------------------------------------------------------ the "Done anyway" confirm


async def test_the_done_confirm_counts_both_checklists(seeded_client: httpx.AsyncClient) -> None:
    """A full parent checklist and an untouched son's must still ask before finishing.

    The shared button carries the *primary* session's id, so a route that reads
    ``view.all_ticked`` off that one session sees a complete checklist and finalises both on the
    first tap - ending the son's session at zero ticks with no confirm and no felt strip. The
    footer template already counted both; this is the route agreeing with it.
    """
    data = await _together(seeded_client)
    parent, child = data["sessions"][0], data["sessions"][1]
    assert parent["profile"] == PROFILE_ME and child["profile"] == PROFILE_SON
    await _tick_all(seeded_client, parent)
    assert all(row["done"] is False for row in (await _together(seeded_client))["sessions"][1]["rows"])

    response = await seeded_client.post(
        f"/today/{parent['session_id']}/done?profile=together", data={"confirm": "0"}, headers=HX
    )

    assert response.status_code == 200, "the first tap finished the session instead of asking"
    assert "hx-redirect" not in {key.lower() for key in response.headers}
    assert "How did" in response.text
    still_open = await _together(seeded_client)
    assert {item["session_id"] for item in still_open["sessions"]} == {parent["session_id"], child["session_id"]}
    assert all(item["finished_at"] is None for item in still_open["sessions"]), (
        "a checklist was finished behind the ask"
    )


async def test_the_second_tap_finishes_both(seeded_client: httpx.AsyncClient) -> None:
    """And the confirm is answered once, not once per person."""
    data = await _together(seeded_client)
    parent, child = data["sessions"]
    await _tick_all(seeded_client, parent)

    asked = await seeded_client.post(
        f"/today/{parent['session_id']}/done?profile=together", data={"confirm": "0"}, headers=HX
    )
    finished = await seeded_client.post(
        f"/today/{parent['session_id']}/done?profile=together", data={"confirm": "1"}, headers=HX
    )

    assert "How did" in asked.text
    assert finished.status_code == 204
    assert finished.headers["hx-redirect"].startswith(f"/done/{parent['session_id']}")
    summary = (await seeded_client.post(f"/api/sessions/{child['session_id']}/done", json={})).json()
    assert summary["meta"]["replayed"] is True, "the son's session was left open"


async def test_no_confirm_is_asked_when_both_checklists_are_full(seeded_client: httpx.AsyncClient) -> None:
    """The complement: nothing outstanding anywhere, so one tap is the whole interaction."""
    data = await _together(seeded_client)
    for session in data["sessions"]:
        await _tick_all(seeded_client, session)

    response = await seeded_client.post(
        f"/today/{data['sessions'][0]['session_id']}/done?profile=together", data={"confirm": "0"}, headers=HX
    )

    assert response.status_code == 204
    assert response.headers["hx-redirect"].startswith(f"/done/{data['sessions'][0]['session_id']}")


async def test_a_solo_session_still_asks_on_an_outstanding_row(seeded_client: httpx.AsyncClient) -> None:
    """The group-aware confirm must not break the one-person path it also serves."""
    solo = (await seeded_client.get("/api/today?profile=me")).json()["data"]

    response = await seeded_client.post(
        f"/today/{solo['session_id']}/done?profile=me", data={"confirm": "0"}, headers=HX
    )

    assert response.status_code == 200
    assert "How did" in response.text


# --------------------------------------------------------------- one Done, two stored sessions


async def test_one_done_finalises_both_and_each_keeps_its_own_row(seeded_client: httpx.AsyncClient, db_session) -> None:
    """Two ``session`` rows, one ``together_group_id``, and one tap that ends both.

    Each keeps its own everything: profile, planned day, rows, duration and ``felt``. The group
    id is what makes the Done iterate, and it is the same string on both rows rather than two
    ids that happen to have been minted together.
    """
    data = await _together(seeded_client)
    parent, child = data["sessions"]
    await _tick_all(seeded_client, parent)
    await seeded_client.post(
        f"/api/sessions/{child['session_id']}/rows/{child['rows'][0]['position']}", json={"done": True}
    )

    body = (await seeded_client.post(f"/api/sessions/{parent['session_id']}/done", json={"felt": "hard"})).json()

    assert len(body["data"]["group"]) == 2
    records = {
        record.id: record
        for record in db_session.exec(select(SessionRecord)).all()
        if record.id in {parent["session_id"], child["session_id"]}
    }
    assert len(records) == 2, "one Done collapsed the pair into a single session row"
    for record in records.values():
        db_session.refresh(record)
        assert record.finished_at is not None, f"{record.profile_id} was left open"
    group_ids = {record.together_group_id for record in records.values()}
    assert len(group_ids) == 1 and group_ids != {None}, f"the group id is not shared: {group_ids}"
    assert {record.profile_id for record in records.values()} == {PROFILE_ME, PROFILE_SON}
    assert records[parent["session_id"]].felt == "hard"
    assert records[child["session_id"]].felt is None, "the parent answered for the son"

    # Separate row records throughout: the positions collide, the session ids do not.
    for session_id, session in ((parent["session_id"], parent), (child["session_id"], child)):
        stored = db_session.exec(select(SessionRowRecord).where(SessionRowRecord.session_id == session_id)).all()
        assert len(stored) == len(session["rows"]) > 0

    summaries = {item["profile"]: item for item in body["data"]["group"]}
    assert summaries[PROFILE_ME]["rows_done"] == len(parent["rows"])
    assert summaries[PROFILE_SON]["rows_done"] == 1


async def test_a_done_tapped_on_the_son_side_finalises_both(seeded_client: httpx.AsyncClient) -> None:
    """The button carries the primary's id, but the group is symmetric and the API is too."""
    data = await _together(seeded_client)
    parent, child = data["sessions"]

    body = (await seeded_client.post(f"/api/sessions/{child['session_id']}/done", json={})).json()

    assert body["data"]["session_id"] == child["session_id"], "the summary should lead with the tapped session"
    assert {item["session_id"] for item in body["data"]["group"]} == {parent["session_id"], child["session_id"]}
    replay = await seeded_client.post(f"/api/sessions/{parent['session_id']}/done", json={})
    assert replay.json()["meta"]["replayed"] is True


async def test_the_done_screen_shows_a_card_for_each_person(seeded_client: httpx.AsyncClient) -> None:
    """One Done, two summaries, both named: the parent must be able to see the son's number."""
    data = await _together(seeded_client)
    parent, child = data["sessions"]
    await seeded_client.post(f"/api/sessions/{parent['session_id']}/done", json={})

    page = (await seeded_client.get(f"/done/{parent['session_id']}?profile=together")).text

    assert page.count("summary-card") == 2
    assert page.count("summary-line") == 6, "three lines per person"
    for session in (parent, child):
        assert session["display_name"] in page
