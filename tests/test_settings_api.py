"""PRP-03 acceptance tests 1, 2, 5-8, 13 and 15: the settings API and the setup gate.

The negative tests are the point of this file. Every one of them is a value that must never reach
the database, and each is checked at the JSON API because that is the path a form cannot police:
the screen only offers legal choices, so the API is where an illegal one would arrive from.
"""

from __future__ import annotations

import json

import httpx
import pytest
from sqlmodel import Session as DbSession
from sqlmodel import select

from cadence.profils.settings import SETTING_KEYS
from cadence.profils.tables import Setting

UNPROCESSABLE = 422


def _set_setup_complete(db: DbSession, value: bool) -> None:
    row = db.get(Setting, "setup_complete")
    assert row is not None
    row.value_json = json.dumps(value)
    db.add(row)
    db.commit()


# ------------------------------------------------------------------- 1: the gate


async def test_root_redirects_to_setup_until_complete(seeded_client: httpx.AsyncClient, db_session: DbSession) -> None:
    """1. ``GET /`` gates on ``setup_complete``, and ``POST /setup`` opens it."""
    _set_setup_complete(db_session, False)
    before = await seeded_client.get("/")
    assert before.status_code == 303
    assert before.headers["location"] == "/setup"

    done = await seeded_client.post("/setup", data={"son_age": "12", "equipment": ["bodyweight"]})
    assert done.status_code == 303
    assert done.headers["location"] == "/today?profile=me"

    after = await seeded_client.get("/")
    assert after.headers["location"] == "/today?profile=me"


async def test_no_html_route_renders_before_setup(seeded_client: httpx.AsyncClient, db_session: DbSession) -> None:
    """Risk 7: deep-linking Today before setup must not hand the son a checklist."""
    _set_setup_complete(db_session, False)
    for path in ("/today?profile=son", "/today?profile=me", "/settings"):
        response = await seeded_client.get(path)
        assert response.status_code == 303, path
        assert response.headers["location"] == "/setup", path


async def test_the_gate_never_redirects_the_api_or_the_worker(
    seeded_client: httpx.AsyncClient, db_session: DbSession
) -> None:
    """The service worker and the offline queue read JSON; a redirect would wedge both."""
    _set_setup_complete(db_session, False)
    for path in ("/api/health", "/api/settings", "/sw.js", "/manifest.json"):
        assert (await seeded_client.get(path)).status_code == 200, path


async def test_setup_requires_son_age(seeded_client: httpx.AsyncClient, db_session: DbSession) -> None:
    """2. Setup without an age is refused in place, and setup stays incomplete."""
    _set_setup_complete(db_session, False)
    response = await seeded_client.post("/setup", data={"equipment": ["bodyweight"]})
    assert response.status_code == UNPROCESSABLE
    assert "Enter an age between 3 and 19." in response.text

    settings = (await seeded_client.get("/api/settings")).json()["data"]
    assert settings["setup_complete"] is False


async def test_setup_when_already_complete_goes_to_settings(seeded_client: httpx.AsyncClient) -> None:
    """The gate is not a screen to re-run by accident once it has been answered."""
    response = await seeded_client.get("/setup")
    assert response.status_code == 303
    assert response.headers["location"] == "/settings"


# --------------------------------------------------------- 5-8: the negative set


@pytest.mark.parametrize(("days", "expected"), [(1, UNPROCESSABLE), (7, UNPROCESSABLE), (2, 200), (6, 200)])
async def test_days_per_week_bounds(seeded_client: httpx.AsyncClient, days: int, expected: int) -> None:
    """5. Two to six inclusive; one and seven are not plans this app builds."""
    response = await seeded_client.put("/api/settings", json={"days_per_week": days})
    assert response.status_code == expected
    if expected == UNPROCESSABLE:
        assert "days per week is between 2 and 6" in response.json()["error"]


@pytest.mark.parametrize(("minutes", "expected"), [(50, UNPROCESSABLE), (20, UNPROCESSABLE), (15, 200), (45, 200)])
async def test_session_minutes_enum(seeded_client: httpx.AsyncClient, minutes: int, expected: int) -> None:
    """6. Fifteen, thirty or forty-five - the three the screen can actually show."""
    response = await seeded_client.put("/api/settings", json={"session_minutes": minutes})
    assert response.status_code == expected
    if expected == UNPROCESSABLE:
        assert "15, 30, 45" in response.json()["error"]


async def test_equipment_whitelist(seeded_client: httpx.AsyncClient) -> None:
    """7. Nothing outside the four ids, and bodyweight is not optional (D-068b)."""
    barbell = await seeded_client.put("/api/settings", json={"equipment": ["bodyweight", "barbell"]})
    assert barbell.status_code == UNPROCESSABLE
    message = barbell.json()["error"]
    for legal in ("bodyweight", "dumbbells", "kettlebells", "bench"):
        assert legal in message
    assert "barbell" in message

    without = await seeded_client.put("/api/settings", json={"equipment": ["dumbbells"]})
    assert without.status_code == UNPROCESSABLE
    assert "bodyweight cannot be turned off" in without.json()["error"]

    empty = await seeded_client.put("/api/settings", json={"equipment": []})
    assert empty.status_code == UNPROCESSABLE


async def test_unknown_setting_key_rejected(seeded_client: httpx.AsyncClient) -> None:
    """8. An unknown key is named, not dropped: a silently ignored setting is a lie."""
    response = await seeded_client.put("/api/settings", json={"turbo": True})
    assert response.status_code == UNPROCESSABLE
    assert "turbo" in response.json()["error"]


async def test_setup_complete_cannot_be_unset_by_a_bad_type(seeded_client: httpx.AsyncClient) -> None:
    """A string where a boolean belongs is a 422, never a truthy value quietly stored."""
    response = await seeded_client.put("/api/settings", json={"setup_complete": "yes"})
    assert response.status_code == UNPROCESSABLE


# ------------------------------------------------------- 13, 15: preview and I/O


async def test_weights_preview_parses_and_warns(seeded_client: httpx.AsyncClient) -> None:
    """13. Two ladders and no warning for good text; a named token and a 200 for bad."""
    good = await seeded_client.post(
        "/settings/weights/preview", data={"weights_available": "DB 5-52.5 lb adj step 2.5, KB 16/24 kg"}
    )
    assert good.status_code == 200
    assert "Dumbbells" in good.text
    assert "Kettlebells" in good.text
    assert "⚠" not in good.text

    bad = await seeded_client.post("/settings/weights/preview", data={"weights_available": "DB banana"})
    assert bad.status_code == 200
    assert "banana" in bad.text
    assert "⚠" in bad.text


async def test_an_over_heavy_token_warns_and_is_never_prescribed(seeded_client: httpx.AsyncClient) -> None:
    """D-068(e): 1000 kg is a typo, so it is named and ignored - not stored as a load."""
    response = await seeded_client.put("/api/settings", json={"weights_available": "DB 1000 kg"})
    assert response.status_code == 200
    warnings = response.json()["data"]["weights_warnings"]
    assert any("1000" in warning for warning in warnings)


async def test_settings_roundtrip(seeded_client: httpx.AsyncClient) -> None:
    """15. What a PUT writes is exactly what the next GET returns."""
    patch = {
        "equipment": ["bodyweight", "dumbbells"],
        "weights_available": "DB 5-30 lb adj",
        "days_per_week": 3,
        "session_minutes": 45,
        "push_son_to_garmin": True,
        "timers_default_on": True,
        "readiness_nudge_on": False,
        "display_unit": "lb",
    }
    written = await seeded_client.put("/api/settings", json=patch)
    assert written.status_code == 200

    read = (await seeded_client.get("/api/settings")).json()["data"]
    for key, value in patch.items():
        assert read[key] == value, key
    assert set(read) == set(SETTING_KEYS) | {"weights_warnings"}


async def test_a_patch_leaves_the_keys_it_does_not_name_alone(seeded_client: httpx.AsyncClient) -> None:
    before = (await seeded_client.get("/api/settings")).json()["data"]
    await seeded_client.put("/api/settings", json={"display_unit": "lb"})
    after = (await seeded_client.get("/api/settings")).json()["data"]
    for key in SETTING_KEYS:
        if key != "display_unit":
            assert after[key] == before[key], key


async def test_a_rejected_patch_writes_nothing(seeded_client: httpx.AsyncClient, db_session: DbSession) -> None:
    """A 422 must leave the stored rows untouched, not half of them."""
    before = {row.key: row.value_json for row in db_session.exec(select(Setting)).all()}
    response = await seeded_client.put("/api/settings", json={"days_per_week": 3, "session_minutes": 50})
    assert response.status_code == UNPROCESSABLE
    db_session.expire_all()
    after = {row.key: row.value_json for row in db_session.exec(select(Setting)).all()}
    assert after == before


# --------------------------------------------------- the screens, without a browser


def _full_form(**overrides: object) -> dict[str, object]:
    """Everything a real browser sends, so a test that changes one field changes only that one."""
    form: dict[str, object] = {
        "son_age": "12",
        "equipment": ["bodyweight", "dumbbells", "kettlebells", "bench"],
        "weights_available": "DB 5-52.5 lb adj step 2.5, KB 16/24 kg, BENCH adjustable",
        "days_per_week": "4",
        "session_minutes": "30",
        "has_overhead_anchor": "0",
        "push_son_to_garmin": "0",
        "display_unit": "kg",
        "person_me": "jd",
        "person_son": "son",
        "bodyweight_kg": "",
    }
    form.update(overrides)
    return form


async def test_setup_screen_offers_only_the_four_legal_ids(
    seeded_client: httpx.AsyncClient, db_session: DbSession
) -> None:
    """The picker is built from the whitelist, so there is no control an illegal id could use."""
    _set_setup_complete(db_session, False)
    body = (await seeded_client.get("/setup")).text
    for legal in ("bodyweight", "dumbbells", "kettlebells", "bench"):
        assert f'value="{legal}"' in body
    assert "barbell" not in body
    # Bodyweight is ticked, not tickable, and still posts (a disabled input sends nothing).
    assert 'type="hidden" name="equipment" value="bodyweight"' in body
    assert "checked disabled" in body


async def test_settings_screen_has_the_two_containers_prp_08_fills(seeded_client: httpx.AsyncClient) -> None:
    """23. The ids are the contract with PRP-08; they exist and they are empty."""
    body = (await seeded_client.get("/settings")).text
    assert 'id="import-result"' in body
    assert 'id="generate-preview"' in body
    assert 'id="weights-preview"' in body
    assert "Import workout" in body
    assert "Generate workout" in body


async def test_settings_save_round_trips_through_the_form(seeded_client: httpx.AsyncClient) -> None:
    response = await seeded_client.post("/settings", data=_full_form(days_per_week="3", display_unit="lb"))
    assert response.status_code == 200
    assert "Saved." in response.text

    stored = (await seeded_client.get("/api/settings")).json()["data"]
    assert stored["days_per_week"] == 3
    assert stored["display_unit"] == "lb"
    son = (await seeded_client.get("/api/profiles/son")).json()["data"]
    assert son["age_years"] == 12
    assert son["vitalforge_person"] == "son"


async def test_a_bad_age_renders_in_place_and_keeps_what_was_typed(seeded_client: httpx.AsyncClient) -> None:
    """The wireframe's rule: bad input never costs the user the page they filled in."""
    response = await seeded_client.post("/settings", data=_full_form(son_age="2", person_me="typed-this"))
    assert response.status_code == UNPROCESSABLE
    assert "Enter an age between 3 and 19." in response.text
    assert 'value="2"' in response.text
    assert "typed-this" in response.text
    # And nothing was written.
    assert (await seeded_client.get("/api/profiles/son")).json()["data"]["age_years"] is None


async def test_an_htmx_setup_asks_for_a_real_navigation(
    seeded_client: httpx.AsyncClient, db_session: DbSession
) -> None:
    """A 303 answering an HTMX post is swapped into the page; ``HX-Redirect`` actually moves it."""
    _set_setup_complete(db_session, False)
    response = await seeded_client.post("/setup", data=_full_form(), headers={"hx-request": "true"})
    assert response.status_code == 204
    assert response.headers["hx-redirect"] == "/today?profile=me"


async def test_an_htmx_save_returns_the_form_and_nothing_else(seeded_client: httpx.AsyncClient) -> None:
    response = await seeded_client.post("/settings", data=_full_form(), headers={"hx-request": "true"})
    assert response.status_code == 200
    assert response.text.lstrip().startswith("{#") or 'id="settings-form"' in response.text
    assert "<html" not in response.text


async def test_the_setup_screen_hides_the_profile_tabs(seeded_client: httpx.AsyncClient, db_session: DbSession) -> None:
    """Nothing on the first-run screen offers a way past it."""
    _set_setup_complete(db_session, False)
    body = (await seeded_client.get("/setup")).text
    assert 'href="/today?profile=son"' not in body


async def test_person_slugs_default_to_the_env_and_a_blank_stays_blank(
    seeded_client: httpx.AsyncClient, db_session: DbSession
) -> None:
    """D-017: Cadence never guesses a VitalForge slug from a display name."""
    _set_setup_complete(db_session, False)
    body = (await seeded_client.get("/setup")).text
    assert 'id="person_me" value=""' in body.replace('name="person_me" ', "")
    profile = (await seeded_client.get("/api/profiles/me")).json()["data"]
    assert profile["vitalforge_person"] == ""


async def test_a_settings_save_that_cannot_build_writes_nothing(seeded_client: httpx.AsyncClient) -> None:
    """Risk 8 in reverse: a household the builder refuses must not be stored (D-068b)."""
    before = (await seeded_client.get("/api/settings")).json()["data"]
    response = await seeded_client.put("/api/settings", json={"equipment": ["dumbbells", "bench"]})
    assert response.status_code == UNPROCESSABLE
    after = (await seeded_client.get("/api/settings")).json()["data"]
    assert after["equipment"] == before["equipment"]


async def test_the_gate_cannot_be_opened_by_one_put(seeded_client: httpx.AsyncClient, db_session: DbSession) -> None:
    """`setup_complete` is set by finishing setup, not by naming it in a patch.

    It is a legal key, so it reads back through GET and the setup form writes it - but a PUT that
    flipped it would skip the screen this whole PRP exists to build, and the son's age with it.
    """
    _set_setup_complete(db_session, False)
    response = await seeded_client.put("/api/settings", json={"setup_complete": True})
    assert response.status_code == UNPROCESSABLE
    assert "setup_complete" in response.json()["error"]

    assert (await seeded_client.get("/api/settings")).json()["data"]["setup_complete"] is False
    assert (await seeded_client.get("/")).headers["location"] == "/setup"


async def test_an_unseeded_install_is_told_which_command_to_run(client: httpx.AsyncClient) -> None:
    """Deleting D-072's front-door branch must not leave `/setup` as a stack trace."""
    response = await client.get("/setup")
    assert response.status_code == 200
    assert "make seed" in response.text


async def test_two_bad_slugs_get_two_messages(seeded_client: httpx.AsyncClient) -> None:
    """D-114. The form writes two profiles, so one message for both boxes is not an answer.

    ``as_dict`` used to key by field name alone, so a bad slug on each profile collapsed into a
    single ``vitalforge_person`` entry: the screen showed one line under a section with two inputs
    and the person had to guess which one it meant, or whether it meant both.
    """
    response = await seeded_client.post("/settings", data=_full_form(person_me="BAD ME", person_son="../son"))
    assert response.status_code == UNPROCESSABLE
    assert 'data-error-for="person_me"' in response.text
    assert 'data-error-for="person_son"' in response.text


async def test_a_bad_slug_names_only_the_box_it_is_in(seeded_client: httpx.AsyncClient) -> None:
    """The parent's field stays clean when only the son's is wrong, and the other way round."""
    son_bad = await seeded_client.post("/settings", data=_full_form(person_son="../son"))
    assert 'data-error-for="person_son"' in son_bad.text
    assert 'data-error-for="person_me"' not in son_bad.text

    me_bad = await seeded_client.post("/settings", data=_full_form(person_me="../me"))
    assert 'data-error-for="person_me"' in me_bad.text
    assert 'data-error-for="person_son"' not in me_bad.text


async def test_a_reserved_slug_is_refused(seeded_client: httpx.AsyncClient) -> None:
    """VitalForge's own reserved set: these shadow a real path segment under ``/p/{slug}/``."""
    for slug in ("api", "admin", "p"):
        response = await seeded_client.put("/api/profiles/me", json={"vitalforge_person": slug})
        assert response.status_code == UNPROCESSABLE, slug
        assert "reserved" in response.json()["error"]
