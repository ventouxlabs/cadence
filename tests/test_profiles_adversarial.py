"""The profile endpoint, the band it derives, and what a rebuild is allowed to leave behind.

``tests/test_profiles.py`` walks PRP-03's numbered acceptance tests. This file is the adversarial
pass: one value per shape an age can arrive in, the D-099 grow-up flip pinned in both directions,
the anchor written to both profiles from one question (D-095), and - the one that matters most -
a sweep asserting that after a rebuild **every** row of the son's block is inside his band's caps.

The cap sweep reads its numbers from ``library/youth_rules.yaml`` through the ``library`` fixture,
never from constants here: the band table is domain data (principles section 3) and a test that
restated it would keep passing after the table changed.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
import pytest
from sqlmodel import Session as DbSession
from sqlmodel import select

from cadence.profils.tables import PROFILE_ME, PROFILE_SON, Profile
from cadence.programme.bands import effective_cap
from cadence.programme.tables import PlannedSession
from cadence.schema.enums import AgeBand, LoadUnit
from tests.test_web_today import BARE_WORDS, BODY_IMAGE_PHRASES

UNPROCESSABLE = 422
TOLERANCE = 1e-9

# The elements D-027 scopes the bare words to. Cues are deliberately not among them: "refuse to
# lean" is a legal suitcase-carry cue and banning it would push someone to edit seed content.
METRIC_ELEMENTS = re.compile(
    r'class="(?:row-note|row-notice|session-sub|notice|row-line|summary-line|step-value)"[^>]*>([^<]*)<'
)


def _rows(db: DbSession, profile_id: str) -> list[dict[str, Any]]:
    """Every materialised row of every planned session this profile owns."""
    planned = db.exec(select(PlannedSession).where(PlannedSession.profile_id == profile_id)).all()
    return [row for item in planned for row in json.loads(item.rows_json)]


def _plan_fingerprint(db: DbSession, profile_id: str) -> dict[str, str]:
    planned = db.exec(select(PlannedSession).where(PlannedSession.profile_id == profile_id)).all()
    return {item.id: item.rows_json for item in planned}


# ------------------------------------------------------------------- the age field


@pytest.mark.parametrize(
    "value",
    [0, -1, 2, 20, 21, 120, 999, "nine", "12", 9.5, 12.0, True, [12], {"years": 12}],
    ids=[
        "zero",
        "negative",
        "two",
        "twenty",
        "twenty-one",
        "hundred-twenty",
        "absurd",
        "word",
        "digits-as-text",
        "half",
        "float-whole",
        "bool",
        "list",
        "object",
    ],
)
async def test_an_age_that_is_not_three_to_nineteen_whole_years_is_refused(
    seeded_client: httpx.AsyncClient, db_session: DbSession, value: Any
) -> None:
    """10, adversarially. Below 3 or above 19 is a typo, and so is anything not a whole number."""
    response = await seeded_client.put("/api/profiles/son", json={"age_years": value})
    assert response.status_code == UNPROCESSABLE, value
    assert "age_years" in response.json()["meta"]["fields"]
    db_session.expire_all()
    assert db_session.get(Profile, PROFILE_SON).age_years is None, "a refused age was written anyway"


@pytest.mark.parametrize(
    ("age", "band"),
    [
        (3, AgeBand.U10),
        (4, AgeBand.U10),
        (9, AgeBand.U10),
        (10, AgeBand.AGE_10_13),
        (13, AgeBand.AGE_10_13),
        (14, AgeBand.AGE_14_17),
        (17, AgeBand.AGE_14_17),
    ],
)
async def test_a_legal_age_pins_its_band_and_keeps_him_a_youth(
    seeded_client: httpx.AsyncClient, age: int, band: AgeBand
) -> None:
    """3. The section 3.1 table, through the endpoint rather than through the helper.

    Age 4 is legal: PRP-03's wireframe shows it producing the out-of-range error, which its own
    bounds and acceptance test 10 contradict; D-096(b) resolves that in favour of the bounds.
    """
    response = await seeded_client.put("/api/profiles/son", json={"age_years": age})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["age_band"] == band.value
    assert data["kind"] == "youth"


# --------------------------------------------- D-099: an age past the band table


@pytest.mark.parametrize("age", [18, 19])
async def test_an_age_past_the_band_table_stays_a_capped_youth(
    seeded_client: httpx.AsyncClient, db_session: DbSession, age: int
) -> None:
    """D-099. Section 3.1 stops at 17, so 18 and 19 land on the loosest *youth* band.

    Two unknowns that are not the same unknown: an absent age is a question nobody has answered
    and stays ``u10`` (section 3.8), while 19 is an answer the table does not extend to. Neither
    reading lets the profile out of the youth rules - only a deliberate change of ``kind`` does.
    """
    response = await seeded_client.put("/api/profiles/son", json={"age_years": age})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["kind"] == "youth"
    assert data["age_band"] == AgeBand.AGE_14_17.value
    assert data["age_years"] == age

    db_session.expire_all()
    assert db_session.get(Profile, PROFILE_SON).kind == "youth"


async def test_seventeen_is_still_a_youth_profile(seeded_client: httpx.AsyncClient) -> None:
    """The boundary from below, so the test above is not passing for the wrong reason."""
    data = (await seeded_client.put("/api/profiles/son", json={"age_years": 17})).json()["data"]
    assert data["kind"] == "youth"
    assert data["age_band"] == AgeBand.AGE_14_17.value


async def _heaviest(client: httpx.AsyncClient, db: DbSession, age: int) -> float:
    """The heaviest load anywhere in the son's block after his age is set to ``age``."""
    assert (await client.put("/api/profiles/son", json={"age_years": age})).status_code == 200
    db.expire_all()
    loads = [row["load_kg"] for row in _rows(db, PROFILE_SON) if row["load_kg"]]
    return max(loads) if loads else 0.0


async def test_no_age_a_person_can_type_ever_lifts_the_sons_loads(
    seeded_client: httpx.AsyncClient, db_session: DbSession
) -> None:
    """The invariant this whole PRP exists to hold, stated as one assertion.

    The son's age is a number a parent types on a phone, and 18 for 8 is the ordinary slip. No
    value in the legal range may answer that slip by handing him heavier work than the range's
    own ceiling allows: an age is a fact about a child, never a key that unlocks the adult rules.
    Only the confirmed ``kind`` endpoint does that, and it says so out loud.
    """
    ceiling = await _heaviest(seeded_client, db_session, 17)
    assert ceiling > 0, "the oldest youth band carries no load: the invariant would be vacuous"

    for age in (18, 19, 3, 9, 12):
        heaviest = await _heaviest(seeded_client, db_session, age)
        assert heaviest <= ceiling + TOLERANCE, f"age {age} raised the son's heaviest load to {heaviest} kg"
        stored = (await seeded_client.get("/api/profiles/son")).json()["data"]
        assert stored["kind"] == "youth", f"age {age} took him out of the youth rules"


async def test_the_settings_form_does_not_grow_him_up_either(seeded_client: httpx.AsyncClient) -> None:
    """The screen is the path a person actually types on, so the same invariant is pinned there."""
    from tests.test_settings_adversarial import _full_form

    response = await seeded_client.post("/settings", data=_full_form(son_age="18"))
    assert response.status_code == 200
    stored = (await seeded_client.get("/api/profiles/son")).json()["data"]
    assert stored["kind"] == "youth"
    assert stored["age_band"] == AgeBand.AGE_14_17.value


async def test_the_ordinary_profile_put_still_refuses_kind(seeded_client: httpx.AsyncClient) -> None:
    """Changing which rules protect a child is never something that happens while doing something else."""
    response = await seeded_client.put("/api/profiles/son", json={"kind": "adult", "age_years": 12})
    assert response.status_code == UNPROCESSABLE
    assert (await seeded_client.get("/api/profiles/son")).json()["data"]["kind"] == "youth"


async def test_moving_a_child_to_the_adult_rules_takes_a_confirmation(seeded_client: httpx.AsyncClient) -> None:
    """The one door out of the youth rules is deliberate, named, and reversible."""
    unconfirmed = await seeded_client.put("/api/profiles/son/kind", json={"kind": "adult"})
    assert unconfirmed.status_code == UNPROCESSABLE
    assert "confirm" in unconfirmed.json()["meta"]["fields"]
    assert (await seeded_client.get("/api/profiles/son")).json()["data"]["kind"] == "youth"

    nonsense = await seeded_client.put("/api/profiles/son/kind", json={"kind": "grown", "confirm": True})
    assert nonsense.status_code == UNPROCESSABLE

    moved = await seeded_client.put("/api/profiles/son/kind", json={"kind": "adult", "confirm": True})
    assert moved.status_code == 200
    assert moved.json()["data"]["kind"] == "adult"
    assert moved.json()["data"]["age_band"] == AgeBand.ADULT.value

    # And back, because a control that only travels the way that removes protections is a trapdoor.
    back = await seeded_client.put("/api/profiles/son/kind", json={"kind": "youth", "confirm": True})
    assert back.status_code == 200
    assert back.json()["data"]["kind"] == "youth"


# ------------------------------------------------- the caps, after the rebuild


@pytest.mark.parametrize(
    ("age", "band"),
    [(9, AgeBand.U10), (12, AgeBand.AGE_10_13), (15, AgeBand.AGE_14_17)],
)
async def test_every_row_of_the_rebuilt_block_is_inside_the_new_band(
    seeded_client: httpx.AsyncClient, db_session: DbSession, library, age: int, band: AgeBand
) -> None:
    """Risk 3 and section 3.3, on the stored plan rather than on the helper that made it.

    Correcting the son's age re-plans his block; this asserts the whole block, every session and
    every row, against the caps of the band he has just landed in. A load above the cap here is a
    row the validator would reject being handed to a child by the engine that built it.
    """
    stored = (await seeded_client.put("/api/profiles/son", json={"age_years": age})).json()["data"]
    assert stored["age_band"] == band.value
    rules = library.youth_rules[band]
    allowed = set(rules.allowed_load_types)

    db_session.expire_all()
    rows = _rows(db_session, PROFILE_SON)
    assert rows, "the son has no plan: the sweep would pass vacuously"

    loaded = 0
    for row in rows:
        exercise = library.exercise(row["exercise_id"])
        assert exercise.load_type in allowed, f"{exercise.id} is a {exercise.load_type} at {band.value}"
        load = row["load_kg"]
        if not load:
            continue
        loaded += 1
        cap = effective_cap(rules, LoadUnit(row["load_unit"]), stored["bodyweight_kg"])
        assert cap is not None, f"{exercise.id} carries {load} kg under a unit the band does not cap"
        assert load <= cap + TOLERANCE, f"{exercise.id} at {load} kg is over the {cap} kg cap for {band.value}"

    if band is AgeBand.U10:
        assert loaded == 0, "the under-tens carry no external load at all (section 3.2)"
    else:
        assert loaded, f"no loaded row anywhere in the {band.value} block: the cap sweep proves nothing"


@pytest.mark.parametrize("age", [9, 12, 15])
async def test_the_sons_screens_say_nothing_about_his_body_at_any_age(
    seeded_client: httpx.AsyncClient, age: int
) -> None:
    """21 and D-027, at all three bands, over the page **and** the JSON the page is built from."""
    assert (await seeded_client.put("/api/profiles/son", json={"age_years": age})).status_code == 200

    page = await seeded_client.get("/today?profile=son")
    assert page.status_code == 200
    text = page.text.lower()
    for phrase in BODY_IMAGE_PHRASES:
        assert phrase not in text, f"{phrase!r} on the son's Today at {age}"

    scoped = METRIC_ELEMENTS.findall(text)
    assert scoped, f"no metric elements at age {age}: the check would pass vacuously"
    for chunk in scoped:
        for pattern in BARE_WORDS:
            assert not pattern.search(chunk), f"{pattern.pattern!r} in {chunk!r} at age {age}"

    data = (await seeded_client.get("/api/today?profile=son")).json()["data"]
    assert data["rows"], "an empty checklist would pass this vacuously"
    for row in data["rows"]:
        for note in row["notes"]:
            for pattern in BARE_WORDS:
                assert not pattern.search(note.lower()), f"{note!r} at age {age}"


# --------------------------------------------------- D-095: one question, two rows


@pytest.mark.parametrize("answer", ["1", "0"])
async def test_the_anchor_question_is_answered_for_both_profiles(seeded_client: httpx.AsyncClient, answer: str) -> None:
    """D-095. A beam in a doorway is a fact about the room, so two controls could disagree."""
    from tests.test_settings_adversarial import _full_form

    response = await seeded_client.post("/settings", data=_full_form(has_overhead_anchor=answer))
    assert response.status_code == 200

    expected = answer == "1"
    for profile_id in (PROFILE_ME, PROFILE_SON):
        stored = (await seeded_client.get(f"/api/profiles/{profile_id}")).json()["data"]
        assert stored["has_overhead_anchor"] is expected, profile_id


async def test_the_screen_never_writes_the_sons_garmin_flag(seeded_client: httpx.AsyncClient) -> None:
    """D-021 and D-069: the household setting is the single source, read at payload-build time."""
    from tests.test_settings_adversarial import _full_form

    await seeded_client.post("/settings", data=_full_form(push_son_to_garmin="1"))
    son = (await seeded_client.get("/api/profiles/son")).json()["data"]
    assert son["push_to_garmin"] is False, "a second source of truth for the son's Garmin push"
    assert (await seeded_client.get("/api/settings")).json()["data"]["push_son_to_garmin"] is True


# ------------------------------------------------------- what does and does not re-plan


@pytest.mark.parametrize(
    "patch",
    [
        {"days_per_week": 3},
        {"session_minutes": 45},
        {"equipment": ["bodyweight", "dumbbells"]},
        {"weights_available": "DB 5-20 kg adj"},
    ],
    ids=["days", "minutes", "equipment", "weights"],
)
async def test_a_programming_setting_re_plans_both_blocks(
    seeded_client: httpx.AsyncClient, patch: dict[str, Any]
) -> None:
    response = await seeded_client.put("/api/settings", json=patch)
    assert response.status_code == 200
    assert sorted(response.json()["meta"]["rebuilt"]) == [PROFILE_ME, PROFILE_SON], patch


@pytest.mark.parametrize(
    "patch",
    [
        {"display_unit": "lb"},
        {"timers_default_on": True},
        {"readiness_nudge_on": False},
        {"push_son_to_garmin": True},
    ],
    ids=["unit", "timers", "nudge", "garmin"],
)
async def test_a_rendering_setting_never_touches_the_plan(
    seeded_client: httpx.AsyncClient, db_session: DbSession, patch: dict[str, Any]
) -> None:
    """12, widened. Not just the session ids: the materialised rows must be byte-identical."""
    before = {pid: _plan_fingerprint(db_session, pid) for pid in (PROFILE_ME, PROFILE_SON)}
    response = await seeded_client.put("/api/settings", json=patch)
    assert response.status_code == 200
    assert response.json()["meta"]["rebuilt"] == [], patch
    db_session.expire_all()
    assert {pid: _plan_fingerprint(db_session, pid) for pid in (PROFILE_ME, PROFILE_SON)} == before


@pytest.mark.parametrize(
    "patch",
    [{"display_name": "Dad"}, {"vitalforge_person": "jd"}, {"push_to_garmin": True}],
    ids=["name", "person", "garmin"],
)
async def test_a_profile_field_that_changes_no_prescription_never_re_plans(
    seeded_client: httpx.AsyncClient, db_session: DbSession, patch: dict[str, Any]
) -> None:
    before = _plan_fingerprint(db_session, PROFILE_ME)
    response = await seeded_client.put("/api/profiles/me", json=patch)
    assert response.status_code == 200
    assert response.json()["meta"]["rebuilt"] == [], patch
    db_session.expire_all()
    assert _plan_fingerprint(db_session, PROFILE_ME) == before


@pytest.mark.parametrize(
    "patch",
    [{"age_years": 15}, {"bodyweight_kg": 41.5}, {"has_overhead_anchor": True}],
    ids=["age", "bodyweight", "anchor"],
)
async def test_a_profile_fact_the_caps_are_computed_from_re_plans_only_that_profile(
    seeded_client: httpx.AsyncClient, db_session: DbSession, patch: dict[str, Any]
) -> None:
    """A change to the son is not a reason to re-plan the parent's block."""
    parent = _plan_fingerprint(db_session, PROFILE_ME)
    response = await seeded_client.put("/api/profiles/son", json=patch)
    assert response.status_code == 200
    assert response.json()["meta"]["rebuilt"] == [PROFILE_SON], patch
    db_session.expire_all()
    assert _plan_fingerprint(db_session, PROFILE_ME) == parent


async def test_a_rebuild_keeps_the_start_date_and_the_finished_work_of_both_profiles(
    seeded_client: httpx.AsyncClient, db_session: DbSession
) -> None:
    """11 and D-068(d), asserted for the son as well as the parent."""
    from cadence.programme.tables import Program

    dates = {pid: db_session.get(Program, f"{pid}-block-1").start_date for pid in (PROFILE_ME, PROFILE_SON)}
    assert (await seeded_client.put("/api/settings", json={"days_per_week": 6})).status_code == 200
    db_session.expire_all()
    for pid, started in dates.items():
        program = db_session.get(Program, f"{pid}-block-1")
        assert program.start_date == started, pid
        assert program.days_per_week == 6, pid
