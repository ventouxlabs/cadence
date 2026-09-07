"""Everything the Settings write refuses, and the few odd things it must accept.

``tests/test_settings_api.py`` walks PRP-03's numbered acceptance tests. This file is the
adversarial pass over the same two doors: one value per shape a browser, a script or a typo can
actually produce, checked at ``PUT /api/settings`` **and** at the HTML form, because the form is
the path a person uses and the API is the path a person cannot.

The rule every test here shares: a rejected write leaves the stored rows exactly as they were.
A 422 that half-applied its patch is worse than a 500, because nothing says so.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from sqlmodel import Session as DbSession
from sqlmodel import select

from cadence.profils.settings import SETTING_KEYS
from cadence.profils.tables import PROFILE_ME, PROFILE_SON, Setting
from cadence.programme.ladder import MAX_RUNG_KG
from cadence.programme.tables import PlannedSession

UNPROCESSABLE = 422
LEGAL_IDS = ("bodyweight", "dumbbells", "kettlebells", "bench")


def _stored(db: DbSession) -> dict[str, str]:
    return {row.key: row.value_json for row in db.exec(select(Setting)).all()}


async def _settings(client: httpx.AsyncClient) -> dict[str, Any]:
    return (await client.get("/api/settings")).json()["data"]


# ------------------------------------------------------------------ days per week


@pytest.mark.parametrize(
    "value",
    [
        1,  # below the range
        7,  # above it
        0,
        -1,
        "4",  # the string a form field would send if the API took it raw
        4.0,  # a float that happens to be whole is still not an int
        3.5,
        True,  # bool is an int in Python, and `days_per_week=True` must not mean one day
        None,
        [4],
    ],
    ids=["one", "seven", "zero", "negative", "string", "float-whole", "float", "bool", "none", "list"],
)
async def test_days_per_week_refuses_everything_but_two_to_six(
    seeded_client: httpx.AsyncClient, db_session: DbSession, value: Any
) -> None:
    """5, adversarially. ``True`` is the one that would slip through an ``isinstance(int)``."""
    before = _stored(db_session)
    response = await seeded_client.put("/api/settings", json={"days_per_week": value})
    assert response.status_code == UNPROCESSABLE, value
    assert "days_per_week" in response.json()["meta"]["fields"]
    db_session.expire_all()
    assert _stored(db_session) == before, "a rejected patch wrote something"


@pytest.mark.parametrize("value", [2, 3, 4, 5, 6])
async def test_days_per_week_accepts_the_whole_range(seeded_client: httpx.AsyncClient, value: int) -> None:
    response = await seeded_client.put("/api/settings", json={"days_per_week": value})
    assert response.status_code == 200
    assert (await _settings(seeded_client))["days_per_week"] == value


# ---------------------------------------------------------------- session length


@pytest.mark.parametrize(
    "value",
    [14, 20, 46, 60, 0, -30, 30.5, "30", True, None],
    ids=["14", "20", "46", "60", "zero", "negative", "half", "string", "bool", "none"],
)
async def test_session_minutes_refuses_everything_but_the_three(
    seeded_client: httpx.AsyncClient, db_session: DbSession, value: Any
) -> None:
    """6, adversarially. The screen is three buttons, so a fourth value cannot round-trip."""
    before = _stored(db_session)
    response = await seeded_client.put("/api/settings", json={"session_minutes": value})
    assert response.status_code == UNPROCESSABLE, value
    db_session.expire_all()
    assert _stored(db_session) == before


@pytest.mark.parametrize("value", [15, 30, 45])
async def test_session_minutes_accepts_the_three(seeded_client: httpx.AsyncClient, value: int) -> None:
    assert (await seeded_client.put("/api/settings", json={"session_minutes": value})).status_code == 200


# -------------------------------------------------------------------- equipment


@pytest.mark.parametrize(
    ("value", "expected_words"),
    [
        ([], ("bodyweight",)),
        (["barbell"], ("barbell",) + LEGAL_IDS),
        (["bodyweight", "barbell"], ("barbell",) + LEGAL_IDS),
        (["Dumbbells"], ("Dumbbells",)),  # the whitelist is ids, and ids are lower case
        (["dumbbells"], ("bodyweight cannot be turned off",)),
        (["dumbbells", "dumbbells"], ("bodyweight cannot be turned off",)),
        (["kettlebells", "bench"], ("bodyweight cannot be turned off",)),
        ("bodyweight", ("list of ids",)),  # a bare string is not a one-item list
        ([1, 2], ("1",)),
        ([None], ("None",)),
    ],
    ids=[
        "empty",
        "barbell",
        "barbell-plus",
        "capitalised",
        "no-bw",
        "dupes-no-bw",
        "no-bw-2",
        "string",
        "ints",
        "none",
    ],
)
async def test_equipment_refuses_anything_off_the_whitelist(
    seeded_client: httpx.AsyncClient, db_session: DbSession, value: Any, expected_words: tuple[str, ...]
) -> None:
    """7, adversarially, including D-068(b): the household cannot untick its own floor."""
    before = _stored(db_session)
    response = await seeded_client.put("/api/settings", json={"equipment": value})
    assert response.status_code == UNPROCESSABLE, value
    message = response.json()["error"]
    for word in expected_words:
        assert word in message, f"{word!r} missing from {message!r}"
    db_session.expire_all()
    assert _stored(db_session) == before


async def test_a_duplicate_equipment_id_is_stored_once(seeded_client: httpx.AsyncClient) -> None:
    """A checkbox cannot be ticked twice, but a script can send it twice."""
    response = await seeded_client.put("/api/settings", json={"equipment": ["bodyweight", "dumbbells", "dumbbells"]})
    assert response.status_code == 200
    assert (await _settings(seeded_client))["equipment"] == ["bodyweight", "dumbbells"]


async def test_bodyweight_only_is_a_household_that_builds(
    seeded_client: httpx.AsyncClient, db_session: DbSession, library
) -> None:
    """D-068(a) and risk 8: the minimum legal household re-plans, and to bodyweight movements.

    ``["bodyweight"]`` is the configuration that used to raise ``ProgramBuildError`` for 45 of 480
    swept households. It has to be accepted *and* it has to leave a plan whose every exercise the
    household actually owns - a plan still naming a dumbbell row would be the equipment bypass
    risk 8 describes, arriving through the settings screen rather than around it.
    """
    response = await seeded_client.put("/api/settings", json={"equipment": ["bodyweight"]})
    assert response.status_code == 200
    assert sorted(response.json()["meta"]["rebuilt"]) == ["me", "son"]

    db_session.expire_all()
    rows = db_session.exec(select(PlannedSession)).all()
    assert rows, "the rebuild left no plan at all"
    seen = 0
    for planned in rows:
        for row in json.loads(planned.rows_json):
            exercise = library.exercise(row["exercise_id"])
            owned = {item.value for item in exercise.equipment} <= {"bodyweight"}
            assert owned, f"{row['exercise_id']} needs {exercise.equipment} in a bodyweight-only household"
            assert not row["load_kg"], f"{row['exercise_id']} carries {row['load_kg']} kg with nothing to load it with"
            seen += 1
    assert seen, "no rows to check: the assertion would pass vacuously"


# ------------------------------------------------------------------ unknown keys


async def test_every_unknown_key_is_named_at_once(seeded_client: httpx.AsyncClient) -> None:
    """8. A silently dropped setting is a lie about what the app is now doing."""
    response = await seeded_client.put("/api/settings", json={"turbo": True, "warp_factor": 9})
    assert response.status_code == UNPROCESSABLE
    message = response.json()["error"]
    assert "turbo" in message
    assert "warp_factor" in message


async def test_an_unknown_key_beside_a_legal_one_rejects_both(
    seeded_client: httpx.AsyncClient, db_session: DbSession
) -> None:
    """The legal half of a bad patch must not land: the person asked for one change, not half."""
    before = _stored(db_session)
    response = await seeded_client.put("/api/settings", json={"days_per_week": 3, "turbo": True})
    assert response.status_code == UNPROCESSABLE
    db_session.expire_all()
    assert _stored(db_session) == before


@pytest.mark.parametrize("key", ["kind", "age_years", "id", "profile", "__class__"])
async def test_a_profile_field_is_not_a_setting(seeded_client: httpx.AsyncClient, key: str) -> None:
    """The two endpoints do not share a namespace; naming a profile field here is an unknown key."""
    response = await seeded_client.put("/api/settings", json={key: "adult"})
    assert response.status_code == UNPROCESSABLE
    assert key in response.json()["error"]


# -------------------------------------------------------------- setup_complete


@pytest.mark.parametrize("value", [True, False])
async def test_setup_complete_is_never_writable_through_the_api(seeded_client: httpx.AsyncClient, value: bool) -> None:
    """D-098(a). Both directions: the gate is not openable *or* closable by a JSON patch."""
    response = await seeded_client.put("/api/settings", json={"setup_complete": value})
    assert response.status_code == UNPROCESSABLE
    assert "setup_complete" in response.json()["error"]
    assert (await _settings(seeded_client))["setup_complete"] is True


async def test_setup_complete_smuggled_beside_a_legal_key_takes_the_whole_patch_down(
    seeded_client: httpx.AsyncClient, db_session: DbSession
) -> None:
    """The interesting bypass is not the bare patch, it is the one hidden in a legitimate save."""
    before = _stored(db_session)
    response = await seeded_client.put("/api/settings", json={"days_per_week": 3, "setup_complete": True})
    assert response.status_code == UNPROCESSABLE
    db_session.expire_all()
    assert _stored(db_session) == before


# ------------------------------------------------------------- weights_available


async def test_a_three_hundred_character_rack_is_accepted(seeded_client: httpx.AsyncClient) -> None:
    """A real rack listing is long. 300 characters is under the 500-character ceiling."""
    text = ", ".join(f"DB {n} kg" for n in range(2, 80, 2))[:300]
    response = await seeded_client.put("/api/settings", json={"weights_available": text})
    assert response.status_code == 200
    assert (await _settings(seeded_client))["weights_available"] == text
    assert len(text) == 300


@pytest.mark.parametrize(
    ("value", "field"),
    [("x" * 501, "weights_available"), (5, "weights_available"), (None, "weights_available")],
    ids=["too-long", "int", "none"],
)
async def test_weights_available_refuses_what_is_not_a_short_string(
    seeded_client: httpx.AsyncClient, value: Any, field: str
) -> None:
    response = await seeded_client.put("/api/settings", json={field: value})
    assert response.status_code == UNPROCESSABLE


async def test_an_impossible_weight_is_warned_about_and_never_prescribed(
    seeded_client: httpx.AsyncClient, db_session: DbSession
) -> None:
    """D-068(e). ``DB 1000 kg`` is a typo; the plan built from it must carry no such load."""
    response = await seeded_client.put("/api/settings", json={"weights_available": "DB 1000 kg"})
    assert response.status_code == 200
    assert any("1000" in warning for warning in response.json()["data"]["weights_warnings"])

    db_session.expire_all()
    loads = [
        row["load_kg"]
        for planned in db_session.exec(select(PlannedSession)).all()
        for row in json.loads(planned.rows_json)
        if row["load_kg"]
    ]
    assert all(load <= MAX_RUNG_KG for load in loads), f"a rung above {MAX_RUNG_KG} kg reached the plan"


async def test_a_script_tag_is_stored_verbatim_and_never_rendered_as_markup(
    seeded_client: httpx.AsyncClient,
) -> None:
    """The one free-text setting is the one place markup can arrive from (risk 10).

    It is parsed, never executed, and the two places it is echoed back - the live preview and the
    form itself - must contain no live tag. Asserting the *escaped* form instead would pass
    vacuously the day the parser starts dropping the token rather than naming it.
    """
    payload = '<script>alert("xss")</script>'
    assert (await seeded_client.put("/api/settings", json={"weights_available": payload})).status_code == 200
    assert (await _settings(seeded_client))["weights_available"] == payload

    preview = await seeded_client.post("/settings/weights/preview", data={"weights_available": payload})
    assert preview.status_code == 200
    assert "<script>" not in preview.text
    assert "&lt;script&gt;" in preview.text

    page = await seeded_client.get("/settings")
    assert "<script>alert" not in page.text


async def test_unicode_survives_the_round_trip_unchanged(seeded_client: httpx.AsyncClient) -> None:
    """An en dash and an accent are what a phone keyboard produces; neither may be mangled."""
    payload = "DB 5–52.5 lb réglable, KB 16/24 kg ⚡"
    assert (await seeded_client.put("/api/settings", json={"weights_available": payload})).status_code == 200
    assert (await _settings(seeded_client))["weights_available"] == payload
    preview = await seeded_client.post("/settings/weights/preview", data={"weights_available": payload})
    assert "16" in preview.text


# ------------------------------------------------------------------ display_unit


@pytest.mark.parametrize("value", ["stone", "KG", "lbs", "", 1, None, True])
async def test_display_unit_is_one_of_two_strings(seeded_client: httpx.AsyncClient, value: Any) -> None:
    """Risk 5 starts here: an unrecognised unit that reached the filter would print nonsense."""
    response = await seeded_client.put("/api/settings", json={"display_unit": value})
    assert response.status_code == UNPROCESSABLE, value


# --------------------------------------------------------- the form, same rules


def _full_form(**overrides: object) -> dict[str, object]:
    form: dict[str, object] = {
        "son_age": "12",
        "equipment": list(LEGAL_IDS),
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


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("days_per_week", "7", "days per week is between 2 and 6"),
        ("days_per_week", "banana", "days per week is between 2 and 6"),
        ("days_per_week", "4.5", "days per week is between 2 and 6"),
        ("session_minutes", "20", "session length is one of"),
        ("session_minutes", "30.5", "session length is one of"),
        ("display_unit", "stone", "display_unit"),
        ("bodyweight_kg", "900", "bodyweight is between"),
        ("bodyweight_kg", "heavy", "bodyweight is a number"),
    ],
)
async def test_the_form_refuses_what_the_api_refuses_and_says_so_in_place(
    seeded_client: httpx.AsyncClient, db_session: DbSession, field: str, value: str, message: str
) -> None:
    """One validator behind both doors: the form must never accept a value the API rejects.

    And the rejection is rendered into the form the person is looking at, with what they typed
    still in it - the wireframe's rule that bad input never costs them the page.
    """
    before = _stored(db_session)
    response = await seeded_client.post("/settings", data=_full_form(**{field: value}))
    assert response.status_code == UNPROCESSABLE
    assert message in response.text
    assert "<html" in response.text, "the form came back as something other than the page"
    db_session.expire_all()
    assert _stored(db_session) == before


async def test_the_form_cannot_untick_bodyweight_even_with_the_checkbox_removed(
    seeded_client: httpx.AsyncClient, db_session: DbSession
) -> None:
    """The screen locks the control; this is the post that skips the screen (D-068b)."""
    before = _stored(db_session)
    response = await seeded_client.post("/settings", data=_full_form(equipment=["dumbbells", "kettlebells"]))
    assert response.status_code == UNPROCESSABLE
    assert "bodyweight cannot be turned off" in response.text
    db_session.expire_all()
    assert _stored(db_session) == before


async def test_an_equipment_id_the_screen_never_offers_is_refused_by_the_form(
    seeded_client: httpx.AsyncClient,
) -> None:
    """There is no barbell checkbox, so this post could only have been hand-made."""
    response = await seeded_client.post("/settings", data=_full_form(equipment=[*LEGAL_IDS, "barbell"]))
    assert response.status_code == UNPROCESSABLE
    assert "barbell" in response.text


async def test_the_form_reports_every_bad_field_at_once(seeded_client: httpx.AsyncClient) -> None:
    """D-092: one round trip per save, not one per mistake."""
    response = await seeded_client.post("/settings", data=_full_form(son_age="2", days_per_week="9"))
    assert response.status_code == UNPROCESSABLE
    assert "Enter an age between 3 and 19." in response.text
    assert "days per week is between 2 and 6" in response.text


async def test_the_son_is_not_aged_by_a_save_that_failed_elsewhere(
    seeded_client: httpx.AsyncClient, db_session: DbSession
) -> None:
    """D-092's whole point: a rejected days-per-week must not leave an accepted age behind."""
    from cadence.profils.tables import Profile

    response = await seeded_client.post("/settings", data=_full_form(son_age="14", days_per_week="9"))
    assert response.status_code == UNPROCESSABLE
    db_session.expire_all()
    assert db_session.get(Profile, PROFILE_SON).age_years is None


async def test_the_settings_payload_never_grows_a_key_by_accident(seeded_client: httpx.AsyncClient) -> None:
    """The nine keys plus the parser's warnings, and nothing else: PRP-08 reads this shape."""
    data = await _settings(seeded_client)
    assert set(data) == set(SETTING_KEYS) | {"weights_warnings"}
    assert len(SETTING_KEYS) == 9


# ------------------------------------------------------------- the person slugs


@pytest.mark.parametrize("slug", ["jd", "son", "a", "a-b-c9", "person42", "x" * 32, ""])
async def test_a_legal_person_slug_is_stored_as_typed(seeded_client: httpx.AsyncClient, slug: str) -> None:
    """D-017: blank stays blank, and a real slug is never rewritten on its way in."""
    response = await seeded_client.put("/api/profiles/me", json={"vitalforge_person": slug})
    assert response.status_code == 200
    assert response.json()["data"]["vitalforge_person"] == slug


@pytest.mark.parametrize(
    "slug",
    [
        "../../admin",
        "..",
        ".",
        "me/son",
        "me\\son",
        "my son",
        "jd ",
        " jd",
        "x" * 33,
        "-leading",
        "trailing-",
        "jd?admin=1",
        "jd#top",
        "jd%2e%2e",
        "jd\nson",
        "é",
        "JD",
        "jd_son",
        None,
        42,
        True,
    ],
    ids=[
        "traversal",
        "dotdot",
        "dot",
        "slash",
        "backslash",
        "space",
        "trailing-space",
        "leading-space",
        "too-long",
        "leading-dash",
        "trailing-dash",
        "query",
        "fragment",
        "encoded",
        "newline",
        "accent",
        "upper-case",
        "underscore",
        "none",
        "int",
        "bool",
    ],
)
async def test_a_person_slug_that_is_not_a_path_segment_is_refused(
    seeded_client: httpx.AsyncClient, db_session: DbSession, slug: Any
) -> None:
    """Every VitalForge route is ``/p/{slug}/api/...``, so this field is a path segment.

    PRP-06 interpolates it into a URL. ``..`` or a slash here is a request aimed somewhere nobody
    chose, and the cheapest place to stop it is the field it is typed into. The upper-case and
    underscore cases are not attacks - they are slugs VitalForge itself would refuse, and letting
    one be typed here only moves the 404 to PRP-06.
    """
    from cadence.profils.tables import Profile

    response = await seeded_client.put("/api/profiles/me", json={"vitalforge_person": slug})
    assert response.status_code == UNPROCESSABLE, slug
    assert "vitalforge_person" in response.json()["meta"]["fields"]
    db_session.expire_all()
    assert db_session.get(Profile, PROFILE_ME).vitalforge_person == ""


async def test_the_form_refuses_a_traversal_slug_and_says_so_under_the_field(
    seeded_client: httpx.AsyncClient,
) -> None:
    """And the screen shows it in place, like every other rejection on this form."""
    response = await seeded_client.post("/settings", data=_full_form(person_son="../me"))
    assert response.status_code == UNPROCESSABLE
    # Under the box that was wrong, not under the section: the error is keyed per input now, so
    # the parent's slug field stays clean when only the son's is bad (D-114).
    assert 'data-error-for="person_son"' in response.text
    assert 'data-error-for="person_me"' not in response.text
    assert "short slug" in response.text


async def test_a_bad_slug_on_one_profile_does_not_write_the_other(
    seeded_client: httpx.AsyncClient, db_session: DbSession
) -> None:
    """D-092 again: one save, one transaction, however many people it names."""
    from cadence.profils.tables import Profile

    response = await seeded_client.post("/settings", data=_full_form(person_me="jd", person_son="../me"))
    assert response.status_code == UNPROCESSABLE
    db_session.expire_all()
    assert db_session.get(Profile, PROFILE_ME).vitalforge_person == ""
