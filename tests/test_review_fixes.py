"""Regressions for the PRP-02 devil's-advocate findings (D-083).

Each test names the hole it closes and fails against the code as reviewed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlmodel import select

from cadence.profils.settings import DEFAULT_SETTINGS
from cadence.profils.tables import PROFILE_SON
from cadence.seance import ticks as tick_service
from cadence.seance.catalog import BandRulesUnavailable, require_band_rules
from cadence.seance.tables import SessionRecord, SessionRowRecord
from cadence.seance.today import resolve_today

HX = {"HX-Request": "true"}


async def _today(client: httpx.AsyncClient, profile: str = "me") -> dict:
    response = await client.get(f"/api/today?profile={profile}")
    assert response.status_code == 200
    return response.json()["data"]


def _open_sessions(db) -> list[SessionRecord]:
    return list(db.exec(select(SessionRecord).where(SessionRecord.finished_at.is_(None))).all())


# ------------------------------------------- High 1: no writing into a finished session


@pytest.mark.parametrize(
    ("suffix", "form"),
    [
        ("rows/1/tick", {"done": "1"}),
        ("rows/1/adjust", {"reps": "1"}),
        ("felt", {"felt": "easy"}),
    ],
)
async def test_html_writes_refuse_a_finished_session(
    seeded_client: httpx.AsyncClient, db_session, suffix: str, form: dict
) -> None:
    """A stale tab or a back-button POST must not reopen a session that is already summarised."""
    data = await _today(seeded_client)
    session_id = data["session_id"]
    await seeded_client.post(f"/api/sessions/{session_id}/rows/1", json={"done": True})
    await seeded_client.post(f"/api/sessions/{session_id}/done", json={})
    before = (await seeded_client.get("/api/today?profile=me")).json()["data"]["session_id"]
    assert before != session_id, "Done should have moved Today on to the next planned session"

    response = await seeded_client.post(f"/today/{session_id}/{suffix}?profile=me", data=form, headers=HX)

    assert response.status_code in (204, 303)
    target = response.headers.get("hx-redirect") or response.headers.get("location")
    assert target.startswith(f"/done/{session_id}")


async def test_a_finished_session_keeps_its_rows_and_leaves_one_open_session(
    seeded_client: httpx.AsyncClient, db_session
) -> None:
    data = await _today(seeded_client)
    session_id = data["session_id"]
    await seeded_client.post(f"/api/sessions/{session_id}/rows/1", json={"done": True})
    await seeded_client.post(f"/api/sessions/{session_id}/done", json={})
    await _today(seeded_client)  # opens the next planned session

    for suffix, form in (("rows/2/tick", {"done": "1"}), ("rows/1/adjust", {"reps": "1"})):
        await seeded_client.post(f"/today/{session_id}/{suffix}?profile=me", data=form, headers=HX)

    db_session.expire_all()
    finished = db_session.get(SessionRecord, session_id)
    assert finished.finished_at is not None
    # Row 2 was never ticked and row 1's reps were never stepped: the writes were refused, not
    # applied to whichever session happened to be open.
    stored = {
        row.position: row
        for row in db_session.exec(select(SessionRowRecord).where(SessionRowRecord.session_id == session_id)).all()
    }
    assert stored[2].done is False
    assert stored[1].reps_done is None
    assert len(_open_sessions(db_session)) == 1


# ----------------------------------- High 2: one shared Done never finalises an untouched partner


async def test_together_done_needs_confirm_when_the_partner_has_no_ticks(
    seeded_client: httpx.AsyncClient, db_session
) -> None:
    """Parent 10/10 and son 0/9 is not "all ticked": the son gets the explicit Done-anyway step."""
    data = await _today(seeded_client, "together")
    parent, child = data["sessions"][0], data["sessions"][1]
    for row in parent["rows"]:
        await seeded_client.post(f"/api/sessions/{parent['session_id']}/rows/{row['position']}", json={"done": True})

    first = await seeded_client.post(
        f"/today/{parent['session_id']}/done?profile=together", data={"confirm": "0"}, headers=HX
    )

    assert first.status_code == 200
    assert "How did" in first.text
    db_session.expire_all()
    assert db_session.get(SessionRecord, child["session_id"]).finished_at is None

    second = await seeded_client.post(
        f"/today/{parent['session_id']}/done?profile=together", data={"confirm": "1"}, headers=HX
    )

    assert second.status_code == 204
    db_session.expire_all()
    assert db_session.get(SessionRecord, child["session_id"]).finished_at is not None


async def test_together_done_goes_straight_through_when_both_are_ticked(
    seeded_client: httpx.AsyncClient, db_session
) -> None:
    data = await _today(seeded_client, "together")
    for item in data["sessions"]:
        for row in item["rows"]:
            await seeded_client.post(f"/api/sessions/{item['session_id']}/rows/{row['position']}", json={"done": True})

    response = await seeded_client.post(
        f"/today/{data['session_id']}/done?profile=together", data={"confirm": "0"}, headers=HX
    )

    assert response.status_code == 204
    db_session.expire_all()
    for item in data["sessions"]:
        assert db_session.get(SessionRecord, item["session_id"]).finished_at is not None


async def test_a_youth_session_with_no_ticks_is_never_finalised_on_one_tap(
    seeded_client: httpx.AsyncClient, db_session
) -> None:
    data = await _today(seeded_client, PROFILE_SON)

    response = await seeded_client.post(
        f"/today/{data['session_id']}/done?profile=son", data={"confirm": "0"}, headers=HX
    )

    assert response.status_code == 200
    db_session.expire_all()
    assert db_session.get(SessionRecord, data["session_id"]).finished_at is None


# --------------------------------------------- Medium 4: the client timestamp orders every field


async def test_stale_adjust_replay_is_ignored(seeded_client: httpx.AsyncClient) -> None:
    """A queue drained after a reconnect must not roll reps back to an older number."""
    data = await _today(seeded_client)
    session_id = data["session_id"]
    target = next(row for row in data["rows"] if row["reps"] is not None and row["adjustable"])
    now = datetime.now(UTC)

    await seeded_client.post(
        f"/api/sessions/{session_id}/rows/{target['position']}",
        json={"done": True, "reps_done": target["reps"] + 2, "ts": now.isoformat()},
    )
    stale = (now - timedelta(minutes=10)).isoformat()
    response = await seeded_client.post(
        f"/api/sessions/{session_id}/rows/{target['position']}",
        json={"reps_done": target["reps"] - 1, "ts": stale},
    )

    assert response.json()["data"]["reps_done"] == target["reps"] + 2
    assert response.json()["meta"]["changed"] is False


async def test_stale_load_replay_is_ignored(aged_son_client: httpx.AsyncClient) -> None:
    for _ in range(8):
        data = (await aged_son_client.get("/api/today?profile=son")).json()["data"]
        loaded = next((row for row in data["rows"] if row["load_kg"] is not None), None)
        if loaded is not None:
            break
        await aged_son_client.post(f"/api/sessions/{data['session_id']}/done", json={})
    assert loaded is not None
    now = datetime.now(UTC)
    session_id = data["session_id"]

    await aged_son_client.post(
        f"/api/sessions/{session_id}/rows/{loaded['position']}",
        json={"done": True, "load_done_kg": 5.0, "ts": now.isoformat()},
    )
    response = await aged_son_client.post(
        f"/api/sessions/{session_id}/rows/{loaded['position']}",
        json={"load_done_kg": 2.5, "ts": (now - timedelta(hours=1)).isoformat()},
    )

    assert response.json()["data"]["load_done_kg"] == 5.0


# ------------------------------------- Medium 7: a cap that cannot be computed is not a cap of None


def test_require_band_rules_refuses_a_youth_profile_without_rules(youth_profile, adult_profile) -> None:
    assert require_band_rules(adult_profile, None) is None
    with pytest.raises(BandRulesUnavailable):
        require_band_rules(youth_profile, None)


def test_youth_load_adjust_refuses_when_the_band_table_is_missing(db_session, aged_son, monkeypatch) -> None:
    monkeypatch.setattr("cadence.seance.today.band_rules", lambda profile, bundle=None: None)
    for _ in range(8):
        view = resolve_today(db_session, PROFILE_SON).primary
        loaded = next((row for row in view.rows if row.load_kg is not None), None)
        if loaded is not None:
            break
        from cadence.seance.done import finish

        finish(db_session, view)
    assert loaded is not None

    with pytest.raises(BandRulesUnavailable):
        tick_service.adjust(db_session, view, loaded.position, "load", 1, DEFAULT_SETTINGS)


async def test_the_api_answers_503_when_a_youth_cap_cannot_be_computed(
    aged_son_client: httpx.AsyncClient, monkeypatch
) -> None:
    for _ in range(8):
        data = (await aged_son_client.get("/api/today?profile=son")).json()["data"]
        loaded = next((row for row in data["rows"] if row["load_kg"] is not None), None)
        if loaded is not None:
            break
        await aged_son_client.post(f"/api/sessions/{data['session_id']}/done", json={})
    assert loaded is not None
    monkeypatch.setattr("cadence.seance.today.band_rules", lambda profile, bundle=None: None)

    response = await aged_son_client.post(
        f"/api/sessions/{data['session_id']}/rows/{loaded['position']}", json={"load_done_kg": 4.0}
    )

    assert response.status_code == 503
    assert "unavailable" in response.json()["error"]
    stored = (await aged_son_client.get("/api/today?profile=son")).json()["data"]
    assert next(r for r in stored["rows"] if r["position"] == loaded["position"])["load_done_kg"] is None


def test_an_adult_is_never_refused(db_session, seeded, adult_profile) -> None:
    """The refusal is about a *missing* answer for a youth, not about every ``None``."""
    # The shipped table has an adult rule set (D-029), and it caps nothing.
    view = resolve_today(db_session, "me").primary
    assert require_band_rules(view.profile, view.rules) is view.rules
    assert tick_service.load_cap(view, view.rows[0].spec) is None

    # And an adult with no rule set at all is still an answer, not a refusal.
    assert require_band_rules(adult_profile, None) is None


# ------------------------------------------ Low 8/9: one transaction, one view load per request


async def test_apply_patch_is_a_single_transaction(seeded_client: httpx.AsyncClient, monkeypatch) -> None:
    """Three columns used to mean three commits, so a crash left a row half-applied."""
    from sqlmodel.orm.session import Session as OrmSession

    data = await _today(seeded_client)
    session_id = data["session_id"]
    target = next(row for row in data["rows"] if row["reps"] is not None and row["adjustable"])
    commits: list[int] = []
    real = OrmSession.commit
    monkeypatch.setattr(OrmSession, "commit", lambda self: (commits.append(1), real(self))[1])

    response = await seeded_client.post(
        f"/api/sessions/{session_id}/rows/{target['position']}",
        json={"done": True, "reps_done": target["reps"] + 1},
    )

    assert response.status_code == 200
    assert response.json()["data"]["done"] is True
    assert response.json()["data"]["reps_done"] == target["reps"] + 1
    assert len(commits) == 1, f"{len(commits)} commits for one patch"


def test_a_no_op_patch_commits_nothing(db_session, seeded) -> None:
    from cadence.seance.ticks import apply_patch

    view = resolve_today(db_session, "me").primary
    result = apply_patch(db_session, view, view.rows[0].position, {"done": False}, DEFAULT_SETTINGS)
    assert result.changed is False
    assert db_session.get(SessionRecord, view.record.id).started_at is None


# ------------------------------------------------ D-082 addendum: zero reps is an honest answer


async def test_zero_reps_done_is_recorded_not_clamped(seeded_client: httpx.AsyncClient) -> None:
    """A row attempted and not completed records 0, and can still be ticked."""
    data = await _today(seeded_client)
    session_id = data["session_id"]
    target = next(row for row in data["rows"] if row["reps"] is not None and row["adjustable"])

    response = await seeded_client.post(
        f"/api/sessions/{session_id}/rows/{target['position']}", json={"reps_done": 0, "done": True}
    )

    payload = response.json()["data"]
    assert payload["reps_done"] == 0
    assert payload["done"] is True
    stored = (await _today(seeded_client))["rows"]
    assert next(r for r in stored if r["position"] == target["position"])["reps_done"] == 0


async def test_negative_reps_still_clamps_to_zero(seeded_client: httpx.AsyncClient) -> None:
    data = await _today(seeded_client)
    target = next(row for row in data["rows"] if row["reps"] is not None and row["adjustable"])
    response = await seeded_client.post(
        f"/api/sessions/{data['session_id']}/rows/{target['position']}", json={"reps_done": -4}
    )
    assert response.json()["data"]["reps_done"] == 0


@pytest.mark.parametrize("body", ['{"reps_done": "8"}', '{"reps_done": 8.5}', '{"reps_done": true}'])
async def test_a_reps_type_violation_is_still_422(seeded_client: httpx.AsyncClient, body: str) -> None:
    """Widening the range must not widen the type: StrictInt still rejects these."""
    data = await _today(seeded_client)
    target = next(row for row in data["rows"] if row["reps"] is not None and row["adjustable"])
    response = await seeded_client.post(
        f"/api/sessions/{data['session_id']}/rows/{target['position']}",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]


def test_the_stepper_can_reach_zero(db_session, seeded) -> None:
    from cadence.seance.ticks import MIN_REPS, adjust

    view = resolve_today(db_session, "me").primary
    row = next(item for item in view.rows if item.reps is not None and item.adjustable)
    assert MIN_REPS == 0
    for _ in range(row.reps + 3):
        adjust(db_session, resolve_today(db_session, "me").primary, row.position, "reps", -1, DEFAULT_SETTINGS)
    assert resolve_today(db_session, "me").primary.row(row.position).reps == 0


# ------------------------------- D-084: the youth exit finishes one checklist, not the group


async def test_the_youth_exit_finalises_only_his_session(seeded_client: httpx.AsyncClient, db_session) -> None:
    data = await _today(seeded_client, "together")
    parent, child = data["sessions"][0], data["sessions"][1]
    for row in child["rows"]:
        await seeded_client.post(f"/api/sessions/{child['session_id']}/rows/{row['position']}", json={"done": True})

    response = await seeded_client.post(
        f"/today/{child['session_id']}/done?profile=together",
        data={"scope": "solo", "confirm": "0", "felt": "easy"},
        headers=HX,
    )

    assert response.status_code == 204
    db_session.expire_all()
    assert db_session.get(SessionRecord, child["session_id"]).finished_at is not None
    assert db_session.get(SessionRecord, child["session_id"]).felt == "easy"
    # The parent's checklist is untouched: that is the whole difference from the shared Done.
    assert db_session.get(SessionRecord, parent["session_id"]).finished_at is None
    assert db_session.get(SessionRecord, parent["session_id"]).felt is None


async def test_the_youth_exit_confirms_against_his_own_rows(seeded_client: httpx.AsyncClient, db_session) -> None:
    """A solo exit is judged against one checklist, so the parent's progress cannot skip it."""
    data = await _today(seeded_client, "together")
    child = data["sessions"][1]

    first = await seeded_client.post(
        f"/today/{child['session_id']}/done?profile=together", data={"scope": "solo", "confirm": "0"}, headers=HX
    )

    assert first.status_code == 200
    db_session.expire_all()
    assert db_session.get(SessionRecord, child["session_id"]).finished_at is None

    second = await seeded_client.post(
        f"/today/{child['session_id']}/done?profile=together", data={"scope": "solo", "confirm": "1"}, headers=HX
    )
    assert second.status_code == 204
    db_session.expire_all()
    assert db_session.get(SessionRecord, child["session_id"]).finished_at is not None


async def test_the_json_api_honours_the_solo_scope(seeded_client: httpx.AsyncClient, db_session) -> None:
    """The offline queue replays the same scope the tap carried."""
    data = await _today(seeded_client, "together")
    parent, child = data["sessions"][0], data["sessions"][1]

    body = (await seeded_client.post(f"/api/sessions/{child['session_id']}/done", json={"scope": "solo"})).json()

    assert "group" not in body["data"]
    db_session.expire_all()
    assert db_session.get(SessionRecord, child["session_id"]).finished_at is not None
    assert db_session.get(SessionRecord, parent["session_id"]).finished_at is None


async def test_the_shared_done_still_finalises_both(seeded_client: httpx.AsyncClient, db_session) -> None:
    """D-013 is unchanged for anything that is not the youth exit."""
    data = await _today(seeded_client, "together")
    body = (await seeded_client.post(f"/api/sessions/{data['session_id']}/done", json={})).json()

    assert len(body["data"]["group"]) == 2
    db_session.expire_all()
    for item in data["sessions"]:
        assert db_session.get(SessionRecord, item["session_id"]).finished_at is not None


async def test_the_done_page_omits_an_unfinished_partner(seeded_client: httpx.AsyncClient) -> None:
    data = await _today(seeded_client, "together")
    child = data["sessions"][1]
    await seeded_client.post(f"/api/sessions/{child['session_id']}/done", json={"scope": "solo"})

    page = await seeded_client.get(f"/done/{child['session_id']}?profile=together")

    assert page.status_code == 200
    assert page.text.count('class="summary-card"') == 1
