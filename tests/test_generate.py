"""``POST /api/generate`` and ``/api/generate/accept`` - PRP-08 acceptance tests 21 to 34.

The gateway is always ``httpx.MockTransport``. No test here needs a key, a network, or respx.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlmodel import Session as DbSession
from sqlmodel import select

from cadence.config import Settings
from cadence.db import WorkoutRecord, get_engine
from cadence.ia import client, prompt
from cadence.ia import generate as ia
from cadence.profils.tables import PROFILE_ME, PROFILE_SON, Profile
from tests import documents as doc
from tests.conftest import OmniRouteGateway

KEY = "or-live-DO-NOT-LEAK-1d4e7f0a"


def codes_of(payload: dict) -> set[str]:
    return {item["code"] for item in payload["data"]["errors"]}


def generated_ids(settings: Settings) -> set[str]:
    with DbSession(get_engine(settings)) as db:
        return {row.id for row in db.exec(select(WorkoutRecord)).all() if row.source == "generated"}


@pytest.fixture
def keyed_app(seeded: FastAPI, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """The seeded app with an OmniRoute key present, so the 503 path is not the default."""
    from cadence.config import get_settings as config_settings

    keyed = settings.model_copy(update={"omniroute_key": SecretStr(KEY)})
    seeded.dependency_overrides[config_settings] = lambda: keyed
    seeded.state.settings = keyed
    return seeded


@pytest.fixture
async def ai_client(keyed_app: FastAPI, omniroute_gateway: OmniRouteGateway, monkeypatch) -> AsyncIterator:
    """A client whose OmniRoute calls land on the mock gateway."""
    monkeypatch.setattr(client, "TRANSPORT", omniroute_gateway.transport)
    transport = httpx.ASGITransport(app=keyed_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


@pytest.fixture
def described_family(db_session, seeded: FastAPI) -> FastAPI:
    """A household with facts worth leaking: a name, a slug, an age and a bodyweight."""
    son = db_session.get(Profile, PROFILE_SON)
    son.age_years = 12
    son.age_band = "age_10_13"
    son.bodyweight_kg = 41.0
    son.display_name = "Theodore"
    son.vitalforge_person = "theo-slug"
    me = db_session.get(Profile, PROFILE_ME)
    me.display_name = "Jean-Dominique"
    me.vitalforge_person = "jd-slug"
    db_session.add(son)
    db_session.add(me)
    db_session.commit()
    return seeded


# ------------------------------------------------------------------------------------- previews


async def test_generate_valid_yaml(ai_client, omniroute_gateway: OmniRouteGateway, settings) -> None:
    """Test 21: a good reply becomes a preview, and nothing is stored."""
    omniroute_gateway.reply(doc.GENERATED_ADULT)
    response = await ai_client.post("/api/generate", json={"profile": "me", "goal": "posture"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["data"]["workout"]["id"] == "generated-posture"
    assert body["data"]["errors"] == []
    assert body["meta"]["attempts"] == 1
    assert generated_ids(settings) == set()


async def test_generate_strips_a_code_fence(ai_client, omniroute_gateway: OmniRouteGateway) -> None:
    """A fenced reply is repaired defensively rather than failing on the backticks."""
    omniroute_gateway.reply(f"```yaml\n{doc.GENERATED_ADULT}```")
    response = await ai_client.post("/api/generate", json={"profile": "me", "goal": "posture"})
    assert response.status_code == 200, response.text


async def test_generate_barbell_rejected(ai_client, omniroute_gateway: OmniRouteGateway, settings) -> None:
    """Test 22: the model's output goes through exactly the gate an import does."""
    omniroute_gateway.reply(doc.BARBELL_WORKOUT)
    response = await ai_client.post("/api/generate", json={"profile": "me", "goal": "strength"})
    assert response.status_code == 422
    assert "equipment_not_whitelisted" in codes_of(response.json())
    assert generated_ids(settings) == set()


async def test_generate_youth_five_rep_rejected(ai_client, omniroute_gateway: OmniRouteGateway, aged_son) -> None:
    """Test 23: a youth workout with loaded five-rep sets never becomes a preview."""
    omniroute_gateway.reply(doc.YOUTH_FIVE_REPS)
    response = await ai_client.post("/api/generate", json={"profile": "son", "goal": "strength"})
    assert response.status_code == 422
    assert "youth_rep_floor" in codes_of(response.json())


async def test_generate_malformed_text(ai_client, omniroute_gateway: OmniRouteGateway) -> None:
    """Test 24: prose earns one retry, then a refusal."""
    omniroute_gateway.reply("Sure! Here is a great workout for you: do some push-ups.")
    response = await ai_client.post("/api/generate", json={"profile": "me", "goal": "posture"})
    assert response.status_code == 422
    assert response.json()["error"] == "generation_failed"
    assert len(omniroute_gateway.requests) == 2


async def test_generate_retry_includes_errors(ai_client, omniroute_gateway: OmniRouteGateway) -> None:
    """Test 25: the second prompt carries the first attempt's findings."""
    omniroute_gateway.reply(doc.BARBELL_WORKOUT).reply(doc.GENERATED_ADULT)
    response = await ai_client.post("/api/generate", json={"profile": "me", "goal": "posture"})
    assert response.status_code == 200, response.text
    assert response.json()["meta"]["attempts"] == 2
    first, second = omniroute_gateway.prompts
    assert prompt.RETRY_HEADING not in first
    assert prompt.RETRY_HEADING in second
    # The findings, as codes and static guidance: a retry that drops them asks the same question
    # again, and a retry that forwards the validator's messages sends the numbers in them (D-184).
    assert "equipment_not_whitelisted" in second
    assert "use one of: bodyweight, dumbbells, kettlebells, bench" in second


async def test_generate_only_one_retry(ai_client, omniroute_gateway: OmniRouteGateway) -> None:
    """Test 26: a gateway that always fails is called exactly twice, never more."""
    omniroute_gateway.reply(doc.BARBELL_WORKOUT)
    response = await ai_client.post("/api/generate", json={"profile": "me", "goal": "posture"})
    assert response.status_code == 422
    assert len(omniroute_gateway.requests) == ia.MAX_ATTEMPTS == 2


async def test_generate_injection_in_cue_rejected(ai_client, omniroute_gateway: OmniRouteGateway, settings) -> None:
    """Test 27: a model's own output is untrusted content and meets the same content scan."""
    poisoned = doc.GENERATED_ADULT.replace(
        "    rest_s: 45\n  - exercise_id: prone-ytw-raise",
        "    rest_s: 45\n    cue_override: '<script>alert(1)</script>'\n  - exercise_id: prone-ytw-raise",
    )
    omniroute_gateway.reply(poisoned)
    response = await ai_client.post("/api/generate", json={"profile": "me", "goal": "posture"})
    assert response.status_code == 422
    assert codes_of(response.json()) == {"suspicious_content"}
    assert generated_ids(settings) == set()


async def test_no_streaming_requested(ai_client, omniroute_gateway: OmniRouteGateway) -> None:
    """Test 34: the brief's gateway gotcha, asserted on the wire."""
    omniroute_gateway.reply(doc.GENERATED_ADULT)
    await ai_client.post("/api/generate", json={"profile": "me", "goal": "posture"})
    body = omniroute_gateway.bodies[0]
    assert body["stream"] is False
    assert body["temperature"] == client.TEMPERATURE
    assert body["model"] == "code-plan"


async def test_generate_appearance_goal_rejected_for_youth(ai_client, omniroute_gateway, aged_son) -> None:
    """Test 30: a body-image goal is refused before a single byte leaves the machine."""
    response = await ai_client.post("/api/generate", json={"profile": "son", "goal": "appearance"})
    assert response.status_code == 422
    assert "youth_banned_goal" in codes_of(response.json())
    assert omniroute_gateway.requests == []


async def test_generate_without_key(seeded_client, settings) -> None:
    """Test 31: a 503, a readable line, and no variable name or value anywhere in the body."""
    response = await seeded_client.post("/api/generate", json={"profile": "me", "goal": "posture"})
    assert response.status_code == 503
    assert response.json()["error"] == "generation_unavailable"
    assert "OMNIROUTE" not in response.text
    assert KEY not in response.text


async def test_import_still_works_without_a_key(seeded_client) -> None:
    """Test k: generation is optional; importing a workout is not."""
    response = await seeded_client.post("/api/import", json={"text": doc.ADULT_WORKOUT, "profile": "me"})
    assert response.status_code == 200, response.text


async def test_gateway_timeout_is_not_a_500(ai_client, monkeypatch) -> None:
    """Risk 12: a slow gateway is a refusal in the envelope, never a traceback."""

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    monkeypatch.setattr(client, "TRANSPORT", httpx.MockTransport(timeout))
    response = await ai_client.post("/api/generate", json={"profile": "me", "goal": "posture"})
    assert response.status_code == 422
    assert response.json()["error"] == "generation_failed"


async def test_gateway_401_never_echoes_the_key(ai_client, omniroute_gateway: OmniRouteGateway, caplog) -> None:
    """Risk 9: an auth failure from a proxy is reported by status alone."""
    omniroute_gateway.reply_raw({"error": f"invalid api key {KEY}"}, status=401)
    with caplog.at_level(logging.DEBUG):
        response = await ai_client.post("/api/generate", json={"profile": "me", "goal": "posture"})
    assert response.status_code == 422
    assert KEY not in response.text
    assert "Bearer" not in response.text
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert KEY not in logged
    assert "Bearer" not in logged


# ----------------------------------------------------------------------------------- accepting


async def test_accept_stores_generated(ai_client, omniroute_gateway: OmniRouteGateway, settings) -> None:
    """Test 33."""
    omniroute_gateway.reply(doc.GENERATED_ADULT)
    preview = await ai_client.post("/api/generate", json={"profile": "me", "goal": "posture"})
    workout = preview.json()["data"]["workout"]
    response = await ai_client.post("/api/generate/accept", json={"profile": "me", "workout": workout})
    assert response.status_code == 200, response.text
    assert generated_ids(settings) == {"generated-posture"}
    with DbSession(get_engine(settings)) as db:
        assert db.get(WorkoutRecord, "generated-posture").source == "generated"


async def test_accept_revalidates(ai_client, omniroute_gateway: OmniRouteGateway, settings) -> None:
    """Test 32: a preview with a barbell row swapped in after the fact is refused, not stored."""
    omniroute_gateway.reply(doc.GENERATED_ADULT)
    preview = await ai_client.post("/api/generate", json={"profile": "me", "goal": "posture"})
    workout = preview.json()["data"]["workout"]
    workout["rows"][0]["exercise_id"] = "barbell-back-squat"
    response = await ai_client.post("/api/generate/accept", json={"profile": "me", "workout": workout})
    assert response.status_code == 422
    assert "unknown_exercise" in codes_of(response.json())
    assert generated_ids(settings) == set()


async def test_accept_revalidates_a_load_above_the_band_cap(
    ai_client, omniroute_gateway: OmniRouteGateway, aged_son, settings
) -> None:
    """The same tampering against the son, where the band cap is the check that catches it."""
    omniroute_gateway.reply(doc.GENERATED_YOUTH)
    preview = await ai_client.post("/api/generate", json={"profile": "son", "goal": "posture"})
    workout = preview.json()["data"]["workout"]
    workout["rows"][0] = {
        "exercise_id": "db-bent-row",
        "sets": 2,
        "reps": 10,
        "seconds": None,
        "meters": None,
        "steps": None,
        "load_kg": 40.0,
        "load_unit": "per_hand",
        "rest_s": 60,
        "rpe_target": None,
        "amrap": False,
        "is_prelude": False,
        "is_challenge": False,
        "cue_override": None,
        "progression_id": None,
    }
    response = await ai_client.post("/api/generate/accept", json={"profile": "son", "workout": workout})
    assert response.status_code == 422
    assert "youth_load_exceeded" in codes_of(response.json())
    assert generated_ids(settings) == set()


async def test_accept_re_runs_the_content_scan(ai_client, settings) -> None:
    """The accept path is not a shortcut past the scan: a tampered cue is caught there too."""
    workout = doc.as_dict(doc.GENERATED_ADULT)
    workout["rows"][0]["cue_override"] = "{{ 7*7 }}"
    response = await ai_client.post("/api/generate/accept", json={"profile": "me", "workout": workout})
    assert response.status_code == 422
    assert codes_of(response.json()) == {"suspicious_content"}
    assert generated_ids(settings) == set()


async def test_accept_cannot_overwrite_a_seed_workout(ai_client, settings) -> None:
    """Risk 7, on the generate side."""
    workout = doc.as_dict(doc.GENERATED_ADULT)
    workout["id"] = "upper-a"
    response = await ai_client.post("/api/generate/accept", json={"profile": "me", "workout": workout})
    assert response.status_code == 422
    assert codes_of(response.json()) == {"id_collision"}
    with DbSession(get_engine(settings)) as db:
        assert db.get(WorkoutRecord, "upper-a").source == "seed"


async def test_a_generated_id_that_collides_is_renamed(ai_client, omniroute_gateway: OmniRouteGateway) -> None:
    """A model that picks a taken slug gets a suffix rather than overwriting anything."""
    omniroute_gateway.reply(doc.GENERATED_ADULT.replace("id: generated-posture", "id: upper-a"))
    response = await ai_client.post("/api/generate", json={"profile": "me", "goal": "posture"})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["workout"]["id"] == "upper-a-2"


async def test_accept_refuses_a_body_that_is_not_a_workout(ai_client) -> None:
    response = await ai_client.post("/api/generate/accept", json={"profile": "me", "workout": "not a mapping"})
    assert response.status_code == 422


# --------------------------------------------------------------------------------- mock mode


async def test_mock_mode_needs_no_key_and_still_validates(db_session, settings, monkeypatch) -> None:
    """The deploy smoke test's switch: no network, no key, and every gate still runs."""
    from pathlib import Path

    from cadence.bibliotheque.seed import seed
    from cadence.main import create_app

    mocked = settings.model_copy(update={"omniroute_mode": "mock"})
    app = create_app(mocked)
    seed(db_session, Path(__file__).resolve().parents[1] / "library")

    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError("mock mode made a network call")

    monkeypatch.setattr(client, "TRANSPORT", httpx.MockTransport(refuse))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.post("/api/generate", json={"profile": "me", "goal": "posture"})
        assert response.status_code == 200, response.text
        workout = response.json()["data"]["workout"]
        accepted = await http.post("/api/generate/accept", json={"profile": "me", "workout": workout})
    assert accepted.status_code == 200, accepted.text
    assert json.loads(accepted.text)["meta"]["source"] == "generated"


async def test_mock_mode_generates_for_the_son_too(db_session, settings, monkeypatch) -> None:
    """The son is the profile the mock is most likely to be pointed at, and was never covered.

    The canned reply is one document serving both profiles, so it declares ``both``. Declaring
    ``adult`` made every mock generation for a youth profile fail ``profile_kind_mismatch`` twice
    and then answer "could not produce a workout" - on the deploy smoke test, on a box with no
    key, which is exactly where nobody would think to look for a validator bug (D-189).
    """
    from pathlib import Path

    from cadence.bibliotheque.seed import seed
    from cadence.main import create_app

    mocked = settings.model_copy(update={"omniroute_mode": "mock"})
    app = create_app(mocked)
    seed(db_session, Path(__file__).resolve().parents[1] / "library")

    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError("mock mode made a network call")

    monkeypatch.setattr(client, "TRANSPORT", httpx.MockTransport(refuse))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.post("/api/generate", json={"profile": PROFILE_SON, "goal": "posture"})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["data"]["errors"] == [], body["data"]["errors"]
        workout = body["data"]["workout"]
        accepted = await http.post("/api/generate/accept", json={"profile": PROFILE_SON, "workout": workout})
    assert accepted.status_code == 200, accepted.text
    assert json.loads(accepted.text)["meta"]["source"] == "generated"
