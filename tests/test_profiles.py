"""PRP-03 acceptance tests 3, 4, 9-12, 14, 16 and 17: bands, profiles and the rebuild rule.

Test 11 is the one that matters most. The obvious rebuild deletes every planned session and
re-plans from week 1, which silently destroys the block's history the first time somebody changes
days-per-week; so it asserts the survivors by id, not a count.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import httpx
import pytest
from sqlmodel import Session as DbSession
from sqlmodel import select

from cadence.profils import services
from cadence.profils.tables import PROFILE_ME, PROFILE_SON, Profile
from cadence.programme.tables import PLANNED, PlannedSession
from cadence.schema.enums import AgeBand
from cadence.seance.tables import SessionRecord
from cadence.web.rendering import format_load

UNPROCESSABLE = 422


def _profile(kind: str, age: int | None) -> Profile:
    return Profile(id="x", display_name="X", kind=kind, age_years=age)


def _planned(db: DbSession, profile_id: str) -> list[PlannedSession]:
    rows = db.exec(select(PlannedSession).where(PlannedSession.profile_id == profile_id)).all()
    return sorted(rows, key=lambda row: (row.week, row.day_index))


def _finish(db: DbSession, planned: PlannedSession) -> str:
    """Mark one planned session done, with the ``session`` row a real Done would have left."""
    stamp = datetime.now(UTC).isoformat()
    record = SessionRecord(
        id=str(uuid.uuid4()),
        profile_id=planned.profile_id,
        planned_session_id=planned.id,
        started_at=stamp,
        finished_at=stamp,
        duration_min=30,
        felt="right",
    )
    db.add(record)
    planned.status = "done"
    db.add(planned)
    db.commit()
    return record.id


# --------------------------------------------------------------- 3, 4: the bands


@pytest.mark.parametrize(
    ("kind", "age", "expected"),
    [
        ("youth", 8, AgeBand.U10),
        ("youth", 10, AgeBand.AGE_10_13),
        ("youth", 13, AgeBand.AGE_10_13),
        ("youth", 14, AgeBand.AGE_14_17),
        ("youth", 17, AgeBand.AGE_14_17),
        # D-099: an age past the table is the *oldest youth* band. Still youth, still capped.
        # An unknown age is different and stays `u10` above: nothing known, nothing assumed.
        ("youth", 18, AgeBand.AGE_14_17),
        ("youth", 19, AgeBand.AGE_14_17),
        ("youth", 40, AgeBand.AGE_14_17),
        ("adult", 12, AgeBand.ADULT),
    ],
)
def test_age_band_mapping(kind: str, age: int, expected: AgeBand) -> None:
    """3. The section 3.1 table, including the 18+ youth clamp."""
    assert services.age_band(_profile(kind, age)) is expected


def test_strictest_band_when_age_unset() -> None:
    """4. Section 3.8: an unknown age is the strictest band, never the loosest."""
    assert services.age_band(_profile("youth", None)) is AgeBand.U10


def test_youth_ruleset_for(library) -> None:
    """16. The band table is read from ``library/``, never from constants in code."""
    son = Profile(id=PROFILE_SON, display_name="Son", kind="youth", age_years=12)
    rules = services.youth_ruleset_for(son, library)
    assert rules is not None
    assert rules.max_load_kg_per_hand == 5.0
    assert rules.good_enough_done_after_n_exercises == 3


# ------------------------------------------------------- 9, 10: the profile guard


async def test_profile_kind_immutable(seeded_client: httpx.AsyncClient) -> None:
    """9. Flipping ``kind`` would unlock kettlebells for a child; the API will not do it."""
    response = await seeded_client.put("/api/profiles/son", json={"kind": "adult"})
    assert response.status_code == UNPROCESSABLE
    assert "kind" in response.json()["error"]

    stored = (await seeded_client.get("/api/profiles/son")).json()["data"]
    assert stored["kind"] == "youth"


@pytest.mark.parametrize("age", [0, 2, 20, 120])
async def test_age_out_of_range(seeded_client: httpx.AsyncClient, age: int) -> None:
    """10. Below 3 or above 19 on a youth row is a typo, not a band."""
    response = await seeded_client.put("/api/profiles/son", json={"age_years": age})
    assert response.status_code == UNPROCESSABLE
    assert "between 3 and 19" in response.json()["error"]


@pytest.mark.parametrize("age", [3, 12, 17])
async def test_age_in_range_is_stored_with_its_date(seeded_client: httpx.AsyncClient, age: int) -> None:
    """``age_recorded_on`` is stamped so PRP-07's retest cadence has a date to count from."""
    response = await seeded_client.put("/api/profiles/son", json={"age_years": age})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["age_years"] == age
    assert data["age_recorded_on"] is not None
    assert data["age_band"] == services.age_band(_profile("youth", age)).value


@pytest.mark.parametrize("age", [18, 19])
async def test_an_adult_age_never_removes_the_youth_rules(seeded_client: httpx.AsyncClient, age: int) -> None:
    """3, as amended by D-099. An age is a fact about a person, not a decision about their rules.

    An earlier draft flipped ``kind`` to adult at 18. A mistyped digit then stripped a child's
    protections permanently, because the profile PUT refuses ``kind`` and there was no way back.
    The band moves to the loosest *youth* band and nothing else changes.
    """
    response = await seeded_client.put("/api/profiles/son", json={"age_years": age})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["kind"] == "youth"
    assert data["age_band"] == AgeBand.AGE_14_17.value
    assert data["age_years"] == age


async def test_a_mistyped_age_costs_only_a_corrected_number(seeded_client: httpx.AsyncClient) -> None:
    """The reviewer's case, pinned: typing 18 and fixing it leaves no trace on the rules."""
    await seeded_client.put("/api/profiles/son", json={"age_years": 18})
    corrected = (await seeded_client.put("/api/profiles/son", json={"age_years": 8})).json()["data"]
    assert corrected["kind"] == "youth"
    assert corrected["age_band"] == AgeBand.U10.value


async def test_kind_is_not_a_field_on_the_profile_endpoint(seeded_client: httpx.AsyncClient) -> None:
    """It has an endpoint of its own; naming it beside an age is still a 422."""
    assert (await seeded_client.put("/api/profiles/son", json={"kind": "adult"})).status_code == UNPROCESSABLE
    assert (await seeded_client.get("/api/profiles/son")).json()["data"]["kind"] == "youth"


# ------------------------------------------- the explicit, confirmed, reversible kind change


async def test_marking_a_profile_adult_needs_confirming(seeded_client: httpx.AsyncClient) -> None:
    """A control that changes which rules protect a child does not fire on a stray request."""
    refused = await seeded_client.put("/api/profiles/son/kind", json={"kind": "adult"})
    assert refused.status_code == UNPROCESSABLE
    assert "confirm" in refused.json()["error"]
    assert (await seeded_client.get("/api/profiles/son")).json()["data"]["kind"] == "youth"


async def test_marking_a_profile_adult_is_reversible(seeded_client: httpx.AsyncClient, db_session: DbSession) -> None:
    """Both directions, or the control is a trapdoor rather than a setting (D-099)."""
    up = await seeded_client.put("/api/profiles/son/kind", json={"kind": "adult", "confirm": True})
    assert up.status_code == 200
    assert up.json()["data"]["kind"] == "adult"
    assert up.json()["data"]["age_band"] == AgeBand.ADULT.value
    assert up.json()["meta"]["rebuilt"] == [PROFILE_SON]

    db_session.expire_all()
    adult_ids = {row.workout_id for row in _planned(db_session, PROFILE_SON)}
    assert not any(identifier.startswith("son-") for identifier in adult_ids)

    back = await seeded_client.put("/api/profiles/son/kind", json={"kind": "youth", "confirm": True})
    assert back.status_code == 200
    assert back.json()["data"]["kind"] == "youth"

    db_session.expire_all()
    youth_ids = {row.workout_id for row in _planned(db_session, PROFILE_SON)}
    assert any(identifier.startswith("son-") for identifier in youth_ids)


async def test_an_unknown_kind_is_refused(seeded_client: httpx.AsyncClient) -> None:
    response = await seeded_client.put("/api/profiles/son/kind", json={"kind": "robot", "confirm": True})
    assert response.status_code == UNPROCESSABLE
    assert (
        await seeded_client.put("/api/profiles/nobody/kind", json={"kind": "adult", "confirm": True})
    ).status_code == 404


async def test_unknown_profile_is_a_404(seeded_client: httpx.AsyncClient) -> None:
    assert (await seeded_client.get("/api/profiles/nobody")).status_code == 404
    assert (await seeded_client.put("/api/profiles/nobody", json={"age_years": 12})).status_code == 404


# ----------------------------------------------------- 11, 12, 17: the rebuild rule


async def test_rebuild_preserves_done_sessions(seeded_client: httpx.AsyncClient, db_session: DbSession) -> None:
    """11. Changing days-per-week re-plans the queue ahead and touches nothing behind it."""
    done = _planned(db_session, PROFILE_ME)[:3]
    done_ids = [row.id for row in done]
    session_ids = [_finish(db_session, row) for row in done]
    before_planned = {row.id for row in _planned(db_session, PROFILE_ME) if row.status == PLANNED}

    response = await seeded_client.put("/api/settings", json={"days_per_week": 3})
    assert response.status_code == 200
    assert PROFILE_ME in response.json()["meta"]["rebuilt"]

    db_session.expire_all()
    after = _planned(db_session, PROFILE_ME)
    survivors = {row.id: row for row in after}

    # Every finished planned session, and every session row, is still there.
    for identifier in done_ids:
        assert identifier in survivors, identifier
        assert survivors[identifier].status == "done"
    for identifier in session_ids:
        assert db_session.get(SessionRecord, identifier) is not None

    # The planned ones were replaced, not kept.
    still_planned = {row.id for row in after if row.status == PLANNED}
    assert still_planned != before_planned

    # And the new work follows the last completed one rather than restarting the block.
    fresh = [row for row in after if row.status == PLANNED]
    assert fresh, "a rebuild must leave something to do"
    assert min((row.week, row.day_index) for row in fresh) > max((row.week, row.day_index) for row in done)


async def test_rebuild_not_triggered_by_display_unit(seeded_client: httpx.AsyncClient, db_session: DbSession) -> None:
    """12. A rendering choice is not a programming change (risk 5)."""
    before = {row.id: row.rows_json for row in _planned(db_session, PROFILE_ME)}
    response = await seeded_client.put("/api/settings", json={"display_unit": "lb"})
    assert response.status_code == 200
    assert response.json()["meta"]["rebuilt"] == []

    db_session.expire_all()
    after = {row.id: row.rows_json for row in _planned(db_session, PROFILE_ME)}
    assert after == before


@pytest.mark.parametrize("key", ["timers_default_on", "readiness_nudge_on", "push_son_to_garmin"])
async def test_the_other_display_settings_never_rebuild(
    seeded_client: httpx.AsyncClient, db_session: DbSession, key: str
) -> None:
    before = {row.id for row in _planned(db_session, PROFILE_ME)}
    response = await seeded_client.put("/api/settings", json={key: True})
    assert response.json()["meta"]["rebuilt"] == []
    db_session.expire_all()
    assert {row.id for row in _planned(db_session, PROFILE_ME)} == before


async def test_has_overhead_anchor_rebuilds(seeded_client: httpx.AsyncClient, db_session: DbSession) -> None:
    """17. With a beam, ``upper-b`` slot 4 becomes the hang (``anchor_alt``, D-056)."""

    def slot_four() -> set[str]:
        found: set[str] = set()
        for row in _planned(db_session, PROFILE_ME):
            if row.day_type != "upper_b":
                continue
            body = [item for item in json.loads(row.rows_json) if item.get("role") != "prelude"]
            found.add(str(body[3]["exercise_id"]))
        return found

    assert slot_four() == {"db-floor-pullover"}

    response = await seeded_client.put("/api/profiles/me", json={"has_overhead_anchor": True})
    assert response.status_code == 200
    assert response.json()["meta"]["rebuilt"] == [PROFILE_ME]

    db_session.expire_all()
    assert slot_four() == {"scap-pull-hang"}


async def test_a_rebuild_keeps_the_block_start_date(seeded_client: httpx.AsyncClient, db_session: DbSession) -> None:
    """D-068(d): re-planning is not starting a new block, and PRP-04 counts from that date."""
    from cadence.programme.tables import Program

    before = db_session.get(Program, "me-block-1")
    assert before is not None
    started = before.start_date

    await seeded_client.put("/api/settings", json={"days_per_week": 5})
    db_session.expire_all()
    after = db_session.get(Program, "me-block-1")
    assert after is not None
    assert after.start_date == started
    assert after.days_per_week == 5


async def test_a_checklist_in_progress_is_not_deleted_by_a_rebuild(
    seeded_client: httpx.AsyncClient, db_session: DbSession
) -> None:
    """D-024 opens a session on the first *render*, so an open one is `planned` with ticks on it."""
    open_row = _planned(db_session, PROFILE_SON)[0]
    stamp = datetime.now(UTC).isoformat()
    db_session.add(
        SessionRecord(id=str(uuid.uuid4()), profile_id=PROFILE_SON, planned_session_id=open_row.id, started_at=stamp)
    )
    db_session.commit()

    await seeded_client.put("/api/settings", json={"days_per_week": 2})
    db_session.expire_all()
    assert db_session.get(PlannedSession, open_row.id) is not None


# ------------------------------------------------------------- 14: the load filter


@pytest.mark.parametrize(
    ("kilos", "unit", "expected"),
    [(14.0, "kg", "14 kg"), (14.0, "lb", "31 lb"), (2.5, "kg", "2.5 kg"), (16.0, "lb", "35.5 lb"), (None, "lb", "")],
)
def test_display_unit_rendering(kilos: float | None, unit: str, expected: str) -> None:
    """14. One conversion, in the filter, and storage is kilograms whatever it says."""
    assert format_load(kilos, unit) == expected


async def test_a_displayed_but_untouched_checklist_is_replanned(
    seeded_client: httpx.AsyncClient, db_session: DbSession
) -> None:
    """D-093: opening Today is not starting it, and a corrected age has to reach the rows.

    The son is rendered (which writes a ``session``, D-024) but never ticked. Changing his age must
    re-plan that day, not preserve it, or he keeps training on the band he has just left.
    """
    planned = _planned(db_session, PROFILE_SON)[0]
    db_session.add(SessionRecord(id=str(uuid.uuid4()), profile_id=PROFILE_SON, planned_session_id=planned.id))
    db_session.commit()
    before = planned.rows_json

    response = await seeded_client.put("/api/profiles/son", json={"age_years": 15})
    assert response.status_code == 200

    db_session.expire_all()
    after = _planned(db_session, PROFILE_SON)[0]
    assert after.rows_json != before, "the new band never reached the checklist"
    # And the empty session it replaced is gone rather than orphaned.
    orphans = db_session.exec(select(SessionRecord).where(SessionRecord.planned_session_id == planned.id)).all()
    assert [record for record in orphans if record.started_at is None] == []


def test_refresh_age_bands_recomputes_a_stale_cache(db_session: DbSession, seeded) -> None:
    """Risk 3: a son who has a birthday between two deploys must not keep last year's band."""
    son = db_session.get(Profile, PROFILE_SON)
    assert son is not None
    son.age_years = 15
    son.age_band = AgeBand.U10.value
    db_session.add(son)
    db_session.commit()

    assert services.refresh_age_bands(db_session) == 1
    db_session.expire_all()
    assert db_session.get(Profile, PROFILE_SON).age_band == AgeBand.AGE_14_17.value
    assert services.refresh_age_bands(db_session) == 0


async def test_the_display_unit_reaches_a_rendered_row(seeded_client: httpx.AsyncClient) -> None:
    """14, at the render layer. The filter is only right if the page actually uses it."""
    await seeded_client.put("/api/settings", json={"display_unit": "lb"})
    body = (await seeded_client.get("/today?profile=me")).text
    assert " lb" in body
    assert " kg" not in body


async def test_a_build_failure_rolls_the_setting_back(
    seeded_client: httpx.AsyncClient, db_session: DbSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard no legal configuration can currently trip, proven to do its job anyway.

    A sweep of 13,500 equipment / weights / days / length / kind / age / bodyweight / anchor
    combinations raises ``ProgramBuildError`` exactly nowhere, because D-068(a) made every
    household containing bodyweight buildable and bodyweight is no longer un-tickable. So this
    forces the failure: what matters is that the app never sits on a stored setting whose plan
    does not exist.
    """
    from cadence.profils import services
    from cadence.programme.errors import ProgramBuildError

    def _explode(*_args: object, **_kwargs: object) -> list[str]:
        raise ProgramBuildError("the plan for profile 'me' would not validate", ["invented"])

    monkeypatch.setattr(services, "rebuild_programs", _explode)
    before = (await seeded_client.get("/api/settings")).json()["data"]["days_per_week"]

    response = await seeded_client.put("/api/settings", json={"days_per_week": before + 1})
    assert response.status_code == UNPROCESSABLE
    assert "can be built" in response.json()["error"]

    after = (await seeded_client.get("/api/settings")).json()["data"]["days_per_week"]
    assert after == before, "a plan that will not build must not leave its setting behind"


# ------------------------------------- a rebuild that cannot run, and one that must leave work


async def test_a_change_needing_a_rebuild_fails_loudly_without_the_library(
    seeded_client: httpx.AsyncClient, db_session: DbSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """503, never 200 with an empty ``rebuilt``.

    The rows in ``rows_json`` were built for the previous band, equipment and ladder. Reporting
    success while leaving them there is how a child keeps being handed last month's loads under
    this month's settings, with the screen saying the change was made.
    """
    from cadence.api import profiles as api_profiles
    from cadence.api import settings as api_settings

    monkeypatch.setattr(api_settings, "library_bundle", lambda *a, **k: None)
    monkeypatch.setattr(api_profiles, "library_bundle", lambda *a, **k: None)
    before = {row.id: row.rows_json for row in _planned(db_session, PROFILE_SON)}

    refused = await seeded_client.put("/api/settings", json={"days_per_week": 3})
    assert refused.status_code == 503
    assert "library" in refused.json()["error"]

    aged = await seeded_client.put("/api/profiles/son", json={"age_years": 15})
    assert aged.status_code == 503

    db_session.expire_all()
    assert {row.id: row.rows_json for row in _planned(db_session, PROFILE_SON)} == before
    assert (await seeded_client.get("/api/profiles/son")).json()["data"]["age_years"] is None


async def test_a_change_needing_no_rebuild_still_works_without_the_library(
    seeded_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal is scoped to changes that re-plan; a display setting is not one."""
    from cadence.api import settings as api_settings

    monkeypatch.setattr(api_settings, "library_bundle", lambda *a, **k: None)
    response = await seeded_client.put("/api/settings", json={"display_unit": "lb"})
    assert response.status_code == 200
    assert response.json()["data"]["display_unit"] == "lb"


async def test_a_rebuild_always_leaves_something_to_do(seeded_client: httpx.AsyncClient, db_session: DbSession) -> None:
    """A four-day block with ten days done, dropped to two days a week, still has a Today.

    Ten spent slots overflow an eight-slot block, so the tail of the rebuilt plan is empty. The
    next block starts rather than the profile being left with nothing planned and a Today screen
    that says so (D-111).
    """
    for row in _planned(db_session, PROFILE_ME)[:10]:
        _finish(db_session, row)

    response = await seeded_client.put("/api/settings", json={"days_per_week": 2})
    assert response.status_code == 200

    db_session.expire_all()
    still_planned = [row for row in _planned(db_session, PROFILE_ME) if row.status == PLANNED]
    assert still_planned, "a rebuild must never leave a profile with an empty queue"
    assert len([row for row in _planned(db_session, PROFILE_ME) if row.status == "done"]) == 10

    today = await seeded_client.get("/api/today?profile=me")
    assert today.status_code == 200
    assert today.json()["data"] is not None


async def test_a_skipped_session_does_not_shorten_the_block(
    seeded_client: httpx.AsyncClient, db_session: DbSession
) -> None:
    """A day that did not happen is history, but it spends no slot (D-111)."""
    rows = _planned(db_session, PROFILE_ME)
    rows[0].status = "skipped"
    db_session.add(rows[0])
    db_session.commit()

    await seeded_client.put("/api/settings", json={"days_per_week": 2})
    db_session.expire_all()
    after = [row for row in _planned(db_session, PROFILE_ME) if row.status == PLANNED]
    # Two days a week over four weeks is eight slots, none of them spent by the skipped day.
    assert len(after) == 8
