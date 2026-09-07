"""Trying to get a bad document *stored* or *adopted*, PRP-08 tester pass.

Every test here is an attack that would matter if it worked: a document that grades its own
safety, a shape that costs the process more than it costs the attacker, or a body that reaches
the store under a profile it was never checked against. The assertions come in pairs - the
response says no, *and* the tables are unchanged - because a 422 over a written row is worse than
no gate at all.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from sqlmodel import Session as DbSession
from sqlmodel import select

from cadence.db import ExerciseRecord, WorkoutRecord, get_engine
from cadence.profils.tables import PROFILE_SON, Profile
from tests import documents as doc


def codes_of(payload: dict) -> set[str]:
    return {item["code"] for item in payload["data"]["errors"]}


def stored_ids(settings) -> set[str]:
    with DbSession(get_engine(settings)) as db:
        return {record.id for record in db.exec(select(WorkoutRecord)).all()}


def stored_exercise(settings, exercise_id: str) -> dict | None:
    with DbSession(get_engine(settings)) as db:
        record = db.get(ExerciseRecord, exercise_id)
        return json.loads(record.doc_json) if record is not None else None


@pytest.fixture
def young_son(db_session, seeded: FastAPI) -> FastAPI:
    """The son at eleven and 32 kg, so every youth cap in ``age_10_13`` is live."""
    son = db_session.get(Profile, PROFILE_SON)
    son.age_years = 11
    son.age_band = "age_10_13"
    son.bodyweight_kg = 32.0
    db_session.add(son)
    db_session.commit()
    return seeded


@pytest.fixture
async def son_client(young_son: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=young_son)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# ------------------------------------------------------------------ "both" and the wrong profile

BOTH_ILLEGAL_FOR_YOUTH = """id: both-heavy
name: Both heavy
day_type: upper_a
target_profile_kind: both
estimated_minutes: 15
rows:
  - exercise_id: db-bent-row
    sets: 3
    reps: 5
    load_kg: 6.0
    load_unit: per_hand
    rest_s: 60
"""

BOTH_LEGAL_FOR_YOUTH = """id: both-easy
name: Both easy
day_type: upper_a
target_profile_kind: both
estimated_minutes: 15
rows:
  - exercise_id: push-up
    sets: 2
    reps: 10
    load_unit: bodyweight
    rest_s: 45
"""


async def test_target_profile_kind_both_does_not_buy_a_youth_pass(son_client, settings) -> None:
    """``both`` satisfies the kind check and must not satisfy anything else.

    ``profile_kind_mismatch`` (D-037) passes ``both`` through by design, which is exactly the
    shape a document would choose to be judged as an adult on the son's plan.
    """
    response = await son_client.post("/api/import", json={"text": BOTH_ILLEGAL_FOR_YOUTH, "profile": "son"})
    assert response.status_code == 422
    assert "youth_rep_floor" in codes_of(response.json())
    assert "both-heavy" not in stored_ids(settings)


async def test_target_profile_kind_both_is_not_refused_outright(son_client, settings) -> None:
    """The non-vacuity half: a legal ``both`` document does import for the son."""
    response = await son_client.post("/api/import", json={"text": BOTH_LEGAL_FOR_YOUTH, "profile": "son"})
    assert response.status_code == 200, response.text
    assert "both-easy" in stored_ids(settings)


async def test_an_adult_document_cannot_be_accepted_for_the_son(son_client, settings) -> None:
    """Accept re-reads ``profile`` from the posted body, so it is an attack surface of its own."""
    body = {"profile": "son", "workout": doc.as_dict(doc.GENERATED_ADULT)}
    response = await son_client.post("/api/generate/accept", json=body)
    assert response.status_code == 422
    assert "profile_kind_mismatch" in codes_of(response.json())
    assert "generated-posture" not in stored_ids(settings)


async def test_an_injected_ok_flag_does_not_short_circuit_the_accept(son_client, settings) -> None:
    """``ok: true`` in the posted body is an unknown key on a document that is refused anyway."""
    document = doc.as_dict(doc.YOUTH_FIVE_REPS)
    document["ok"] = True
    document["errors"] = []
    response = await son_client.post("/api/generate/accept", json={"profile": "son", "workout": document})
    assert response.status_code == 422
    assert "youth_rep_floor" in codes_of(response.json())
    assert "son-heavy" not in stored_ids(settings)


async def test_an_adult_workout_is_neither_stored_nor_adopted_for_the_son(son_client, settings) -> None:
    """The Settings card adopts what it just stored, so a refusal there is a refusal to adopt."""
    response = await son_client.post(
        "/settings/import",
        data={"profile": "son", "text": doc.ADULT_WORKOUT, "day_type": "upper_a"},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 422
    assert "my-upper" not in stored_ids(settings)


# ------------------------------------------------------------------------- inline safety metadata

SELF_CERTIFYING_INLINE = """id: self-certified
name: Self certified
day_type: upper_a
target_profile_kind: adult
estimated_minutes: 20
rows:
  - exercise_id: cheat-row
    sets: 3
    reps: 12
    load_unit: bodyweight
    rest_s: 60
exercises:
  - id: cheat-row
    name: Cheat row
    pattern: pull_h
    region: upper
    load_type: bodyweight
    load_unit: bodyweight
    measure: reps
    equipment: [bodyweight]
    cue: Elbows past the ribs.
    est_seconds_per_set: 1
    is_prelude: true
    tags: [assessment_only, play]
    youth_ok_by_band:
      u10: yes
      age_10_13: yes
      age_14_17: yes
"""


async def test_an_inline_exercise_is_stripped_of_every_claim_it_made(seeded_client, settings) -> None:
    """D-183 for an adult target: the definition is kept, its safety claims are not.

    The row that matters most is ``youth_ok_by_band``. An imported exercise outlives its document
    in the ``exercise`` table, so a later workout for the son would be judged on this dict.
    """
    response = await seeded_client.post("/api/import", json={"text": SELF_CERTIFYING_INLINE, "profile": "me"})
    assert response.status_code == 200, response.text
    stored = stored_exercise(settings, "cheat-row")
    assert stored is not None
    assert set(stored["youth_ok_by_band"].values()) == {"no"}
    assert stored["is_prelude"] is False
    assert "assessment_only" not in stored["tags"]
    assert "play" not in stored["tags"]
    assert stored["est_seconds_per_set"] >= 20


async def test_a_self_certifying_inline_exercise_is_refused_for_the_son(son_client, settings) -> None:
    """D-183 for a youth target: a document may not bring its own movements at all."""
    response = await son_client.post("/api/import", json={"text": SELF_CERTIFYING_INLINE, "profile": "son"})
    assert response.status_code == 422
    assert "inline_exercise_not_allowed_for_youth" in codes_of(response.json())
    assert stored_exercise(settings, "cheat-row") is None


async def test_thirty_one_inline_exercises_are_refused(seeded_client) -> None:
    """The row cap and the inline cap are the same cap, and both are checked before the walk."""
    entries = "\n".join(
        f"  - id: made-up-{index}\n    name: Made up {index}\n    pattern: pull_h\n    region: upper\n"
        "    load_type: bodyweight\n    load_unit: bodyweight\n    measure: reps\n"
        "    equipment: [bodyweight]\n    cue: Pull.\n"
        for index in range(31)
    )
    text = (
        "id: many-inline\nname: Many inline\nday_type: upper_a\ntarget_profile_kind: adult\n"
        "estimated_minutes: 20\nrows:\n  - exercise_id: push-up\n    sets: 2\n    reps: 10\n"
        f"    load_unit: bodyweight\n    rest_s: 45\nexercises:\n{entries}"
    )
    response = await seeded_client.post("/api/import", json={"text": text, "profile": "me"})
    assert response.status_code == 422
    assert "too_many_exercises" in codes_of(response.json())


# ---------------------------------------------------------------------------------- hostile shapes


async def test_a_hundred_deep_json_body_is_a_422(seeded_client) -> None:
    """``json.loads`` is recursive, so the bracket count has to run in front of it."""
    body = "{" + '"a":{' * 100 + '"b":1' + "}" * 100 + "}"
    response = await seeded_client.post(
        "/api/import", content=body.encode(), headers={"content-type": "application/json"}
    )
    assert response.status_code == 422
    assert "RecursionError" not in response.text
    assert "Traceback" not in response.text


async def test_a_hundred_deep_json_accept_body_is_a_422(seeded_client) -> None:
    """The same gate on the other body a client controls."""
    nested: object = {"deep": 1}
    for _ in range(100):
        nested = {"a": nested}
    response = await seeded_client.post("/api/generate/accept", json={"profile": "me", "workout": nested})
    assert response.status_code == 422
    assert "RecursionError" not in response.text


@pytest.mark.parametrize("levels", [100, 2000])
async def test_a_deeply_nested_yaml_document_is_a_422(seeded_client, levels: int) -> None:
    """A YAML flow collection nests through ``safe_load``, which composes recursively.

    Two hundred is past the depth cap and two thousand is past CPython's stack, so this covers
    both the rule and the crash the rule exists to prevent.
    """
    text = "id: deep\nname: Deep\nnested: " + "[" * levels + "]" * levels + "\n"
    response = await seeded_client.post("/api/import", json={"text": text, "profile": "me"})
    assert response.status_code == 422
    assert "parse_error" in codes_of(response.json())
    assert "RecursionError" not in response.text
    assert "Traceback" not in response.text


@pytest.mark.parametrize("literal", [".nan", ".inf", "-.inf"])
async def test_a_non_finite_load_is_refused(seeded_client, settings, literal: str) -> None:
    """``nan`` compares False against every cap, which is how a load cap evaporates (D-037f)."""
    text = doc.ADULT_WORKOUT.replace("load_kg: 9.0", f"load_kg: {literal}").replace("id: my-upper", "id: nan-upper")
    response = await seeded_client.post("/api/import", json={"text": text, "profile": "me"})
    assert response.status_code == 422
    assert "nan-upper" not in stored_ids(settings)


async def test_a_non_finite_session_length_is_refused(seeded_client, settings) -> None:
    text = doc.ADULT_WORKOUT.replace("estimated_minutes: 30", "estimated_minutes: .inf").replace(
        "id: my-upper", "id: inf-upper"
    )
    response = await seeded_client.post("/api/import", json={"text": text, "profile": "me"})
    assert response.status_code == 422
    assert "inf-upper" not in stored_ids(settings)


@pytest.mark.parametrize(
    ("field", "value"),
    [("sets: 3", 'sets: "3"'), ("reps: 10", "reps: true"), ("rest_s: 60", 'rest_s: "60"')],
)
async def test_a_numeric_string_or_a_bool_is_not_a_number(seeded_client, settings, field: str, value: str) -> None:
    """Strict numerics (D-037i): YAML ``true`` is not one and ``"12"`` is not twelve."""
    text = doc.ADULT_WORKOUT.replace(field, value, 1).replace("id: my-upper", "id: loose-upper")
    response = await seeded_client.post("/api/import", json={"text": text, "profile": "me"})
    assert response.status_code == 422
    assert "loose-upper" not in stored_ids(settings)


async def test_a_homoglyph_exercise_id_does_not_resolve(seeded_client, settings) -> None:
    """A Cyrillic ``р`` looks like a ``p`` and is not one; the catalog lookup must agree."""
    text = doc.ADULT_WORKOUT.replace("exercise_id: push-up", "exercise_id: \u0440ush-up", 1).replace(
        "id: my-upper", "id: homoglyph-upper"
    )
    response = await seeded_client.post("/api/import", json={"text": text, "profile": "me"})
    assert response.status_code == 422
    assert "homoglyph-upper" not in stored_ids(settings)


async def test_a_homoglyph_equipment_name_does_not_resolve(seeded_client, settings) -> None:
    """The same for an inline definition's equipment list, which is what V1 reads."""
    text = doc.INLINE_EXERCISE_WORKOUT.replace("equipment: [bodyweight]", "equipment: [b\u043edyweight]").replace(
        "id: inline-day", "id: homoglyph-kit"
    )
    response = await seeded_client.post("/api/import", json={"text": text, "profile": "me"})
    assert response.status_code == 422
    assert "homoglyph-kit" not in stored_ids(settings)


@pytest.mark.parametrize(
    ("where", "text"),
    [
        ("row", doc.ADULT_WORKOUT.replace("    rest_s: 60", "    rest_s: 60\n    sneaky: 1", 1)),
        ("inline", doc.INLINE_EXERCISE_WORKOUT.replace("    cue: Elbows", "    sneaky: 1\n    cue: Elbows", 1)),
    ],
)
async def test_an_extra_key_below_the_top_level_is_refused(seeded_client, settings, where: str, text: str) -> None:
    """Only *top-level* keys are dropped. Deeper ones are ``extra="forbid"`` and must fail."""
    response = await seeded_client.post("/api/import", json={"text": text, "profile": "me"})
    assert response.status_code == 422, where
    assert stored_ids(settings) & {"my-upper", "inline-day"} == set()


# -------------------------------------------------------------------------------- suspicious text

TOKENS = ["{{ 7*7 }}", "{% raw %}", "{# note #}", "http://evil.example", "<script>alert(1)</script>"]


@pytest.mark.parametrize("token", TOKENS)
@pytest.mark.parametrize("field", ["name", "notes", "cue_override"])
async def test_a_hostile_token_is_refused_in_every_text_field(seeded_client, settings, field: str, token: str) -> None:
    """The scan is on strings, not on a list of fields, and this is what proves it."""
    document = doc.as_dict(doc.ADULT_WORKOUT)
    document["id"] = "token-upper"
    if field == "cue_override":
        document["rows"][0]["cue_override"] = f"Keep it tight {token}"
    else:
        document[field] = f"Upper {token}"
    response = await seeded_client.post("/api/import", json={"text": json.dumps(document), "profile": "me"})
    assert response.status_code == 422
    assert "suspicious_content" in codes_of(response.json())
    assert "token-upper" not in stored_ids(settings)


async def test_a_hostile_token_in_a_mapping_key_is_refused(seeded_client) -> None:
    """Mapping keys are walked too: a payload hidden in a key is still in the document."""
    document = doc.as_dict(doc.ADULT_WORKOUT)
    document["{{ 7*7 }}"] = "ignored"
    response = await seeded_client.post("/api/import", json={"text": json.dumps(document), "profile": "me"})
    assert response.status_code == 422
    assert "suspicious_content" in codes_of(response.json())


async def test_a_control_character_survives_the_json_round_trip_and_is_caught(seeded_client) -> None:
    """D-179: JSON escapes a control character, and the post-parse scan still sees it."""
    document = doc.as_dict(doc.ADULT_WORKOUT)
    document["name"] = "Upper\u0007bell"
    response = await seeded_client.post("/api/generate/accept", json={"profile": "me", "workout": document})
    assert response.status_code == 422
    assert "suspicious_content" in codes_of(response.json())


@pytest.mark.parametrize(
    ("field", "length"),
    [("name", 101), ("notes", 501), ("cue_override", 121)],
)
async def test_an_over_long_string_is_refused(seeded_client, field: str, length: int) -> None:
    """PRP-00's model bounds, restated as pre-parse caps so the message is actionable."""
    document = doc.as_dict(doc.ADULT_WORKOUT)
    document["id"] = "long-upper"
    filler = "a" * length
    if field == "cue_override":
        document["rows"][0]["cue_override"] = filler
    else:
        document[field] = filler
    response = await seeded_client.post("/api/import", json={"text": json.dumps(document), "profile": "me"})
    assert response.status_code == 422
    assert "string_too_long" in codes_of(response.json())


async def test_a_cue_of_exactly_the_limit_still_imports(seeded_client, settings) -> None:
    """The non-vacuity half of the length caps: 120 characters is legal."""
    document = doc.as_dict(doc.ADULT_WORKOUT)
    document["id"] = "edge-upper"
    document["rows"][0]["cue_override"] = "a" * 120
    response = await seeded_client.post("/api/import", json={"text": json.dumps(document), "profile": "me"})
    assert response.status_code == 200, response.text
    assert "edge-upper" in stored_ids(settings)


# ------------------------------------------------------------------------------------- collisions


async def test_the_same_document_twice_is_an_id_collision(seeded_client, settings) -> None:
    """D-178: an import keeps the id it was given, so the second one is refused."""
    first = await seeded_client.post("/api/import", json={"text": doc.ADULT_WORKOUT, "profile": "me"})
    second = await seeded_client.post("/api/import", json={"text": doc.ADULT_WORKOUT, "profile": "me"})
    assert first.status_code == 200, first.text
    assert second.status_code == 422
    assert "id_collision" in codes_of(second.json())
    with DbSession(get_engine(settings)) as db:
        held = [record for record in db.exec(select(WorkoutRecord)).all() if record.id == "my-upper"]
    assert len(held) == 1
    assert held[0].source == "import"


async def test_two_concurrent_imports_of_one_document_answer_once(seeded_client, settings) -> None:
    """The id check reads the table a moment before the write, so the primary key is the backstop.

    Whichever way the race falls, exactly one row exists and neither caller sees a 500.
    """
    posts = [seeded_client.post("/api/import", json={"text": doc.ADULT_WORKOUT, "profile": "me"}) for _ in range(2)]
    answers = await asyncio.gather(*posts)
    assert {response.status_code for response in answers} <= {200, 422}
    assert sorted(response.status_code for response in answers) == [200, 422]
    with DbSession(get_engine(settings)) as db:
        assert len([r for r in db.exec(select(WorkoutRecord)).all() if r.id == "my-upper"]) == 1


async def test_an_import_never_replaces_a_seed_exercise(seeded_client, settings) -> None:
    """An inline definition may not shadow a seed movement, and the seed row is untouched."""
    before = stored_exercise(settings, "push-up")
    text = doc.INLINE_EXERCISE_WORKOUT.replace("towel-row", "push-up").replace("id: inline-day", "id: shadow-day")
    response = await seeded_client.post("/api/import", json={"text": text, "profile": "me"})
    assert response.status_code == 422
    assert "id_collision" in codes_of(response.json())
    assert stored_exercise(settings, "push-up") == before


async def test_the_stored_kind_is_the_one_that_was_checked(seeded_client, settings) -> None:
    """A ``both`` document imported for the adult is stored as ``adult`` (D-194).

    It was judged under adult rules alone — no youth rule ran — so recording ``both`` would file
    a youth clearance nothing ever granted. Nothing reads the column today; PRP-10 plans to, and
    at that point the document's own claim would decide whether the son may be given it.
    """
    response = await seeded_client.post("/api/import", json={"text": BOTH_LEGAL_FOR_YOUTH, "profile": "me"})
    assert response.status_code == 200, response.text
    with DbSession(get_engine(settings)) as db:
        stored = db.get(WorkoutRecord, "both-easy")
    assert stored is not None
    assert stored.target_profile_kind == "adult"
    # Non-vacuity: the document really did claim something else.
    assert "target_profile_kind: both" in BOTH_LEGAL_FOR_YOUTH
