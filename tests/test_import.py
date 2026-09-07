"""``POST /api/import`` - PRP-08 acceptance tests 1 to 20.

Every negative here is a case that must never reach the database. The assertions therefore come in
pairs: the response says no, *and* the ``workout`` table is unchanged.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlmodel import Session as DbSession
from sqlmodel import select

from cadence.db import WorkoutRecord, get_engine
from cadence.profils.tables import PROFILE_SON, Profile
from tests import documents as doc

LIBRARY_PATH = Path(__file__).resolve().parents[1] / "library"


def codes_of(payload: dict) -> set[str]:
    return {item["code"] for item in payload["data"]["errors"]}


def stored_ids(settings) -> set[str]:
    with DbSession(get_engine(settings)) as db:
        return {record.id for record in db.exec(select(WorkoutRecord)).all()}


def stored(settings, workout_id: str) -> WorkoutRecord | None:
    with DbSession(get_engine(settings)) as db:
        return db.get(WorkoutRecord, workout_id)


@pytest.fixture
def teen_son(db_session, seeded: FastAPI) -> FastAPI:
    """The son at fifteen and 40 kg: inside ``age_14_17``, below the kettlebell's bodyweight test."""
    son = db_session.get(Profile, PROFILE_SON)
    son.age_years = 15
    son.age_band = "age_14_17"
    son.bodyweight_kg = 40.0
    db_session.add(son)
    db_session.commit()
    return seeded


@pytest.fixture
async def teen_client(teen_son: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=teen_son)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
def unweighed_teen(db_session, teen_son: FastAPI) -> FastAPI:
    """The same teenager with no bodyweight on file, which V14 treats as a denial."""
    son = db_session.get(Profile, PROFILE_SON)
    son.bodyweight_kg = None
    db_session.add(son)
    db_session.commit()
    return teen_son


# ------------------------------------------------------------------------------- the happy path


async def test_import_valid_yaml_stores_workout(seeded_client: httpx.AsyncClient, settings) -> None:
    """Test 1."""
    response = await seeded_client.post("/api/import", json={"text": doc.ADULT_WORKOUT, "profile": "me"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["data"]["workout_id"] == "my-upper"
    assert body["data"]["rows"] == 3
    record = stored(settings, "my-upper")
    assert record is not None
    assert record.source == "import"
    assert record.target_profile_kind == "adult"


async def test_import_valid_json_same_path(seeded_client: httpx.AsyncClient, settings) -> None:
    """Test 2: JSON is a subset of YAML, so one parser serves both."""
    payload = json.dumps(doc.as_dict(doc.ADULT_WORKOUT))
    response = await seeded_client.post("/api/import", json={"text": payload, "profile": "me"})
    assert response.status_code == 200, response.text
    assert stored(settings, "my-upper") is not None


async def test_import_file_upload_ignores_the_filename(seeded_client: httpx.AsyncClient, settings) -> None:
    """A multipart upload takes the bytes and nothing else - not even for logging (D-010)."""
    files = {"file": ("../../../etc/passwd", doc.ADULT_WORKOUT.encode(), "application/x-yaml")}
    response = await seeded_client.post("/api/import", files=files, data={"profile": "me"})
    assert response.status_code == 200, response.text
    assert stored(settings, "my-upper") is not None


async def test_import_valid_youth_workout(aged_son_client: httpx.AsyncClient, settings) -> None:
    """The son's own path: the same pipeline against his band, his equipment and his bodyweight."""
    response = await aged_son_client.post("/api/import", json={"text": doc.YOUTH_WORKOUT, "profile": "son"})
    assert response.status_code == 200, response.text
    record = stored(settings, "son-fun")
    assert record is not None
    assert record.target_profile_kind == "youth"


# ------------------------------------------------------------------------------ the youth gates


async def test_import_barbell_rejected(seeded_client: httpx.AsyncClient, settings) -> None:
    """Test 3: an implement off the whitelist has no enum member to arrive as."""
    response = await seeded_client.post("/api/import", json={"text": doc.BARBELL_WORKOUT, "profile": "me"})
    assert response.status_code == 422
    assert "equipment_not_whitelisted" in codes_of(response.json())
    assert "barbell-day" not in stored_ids(settings)


async def test_import_youth_five_rep_sets_rejected(aged_son_client: httpx.AsyncClient, settings) -> None:
    """Test 4: loaded rows have a rep floor for every youth band."""
    response = await aged_son_client.post("/api/import", json={"text": doc.YOUTH_FIVE_REPS, "profile": "son"})
    assert response.status_code == 422
    assert "youth_rep_floor" in codes_of(response.json())
    assert "son-heavy" not in stored_ids(settings)


async def test_import_youth_kettlebell_rejected(aged_son_client: httpx.AsyncClient, settings) -> None:
    """Test 5: kettlebells are not an allowed load type below fourteen."""
    response = await aged_son_client.post("/api/import", json={"text": doc.YOUTH_KETTLEBELL, "profile": "son"})
    assert response.status_code == 422
    assert "youth_load_type_not_allowed" in codes_of(response.json())
    assert "son-kb" not in stored_ids(settings)


async def test_import_youth_allowlist_enforced(teen_client: httpx.AsyncClient, settings) -> None:
    """Test 5b, first half: ``kb-swing`` is refused for a 40 kg fourteen-to-seventeen.

    PRP-08 predicted ``youth_exercise_not_allowed`` here. PRP-00 splits the section 3.6 rule
    across two checks - V14 asks whether the *movement* is permitted at all for the band, V2
    whether the *load* is legal - so a 40 kg teenager is stopped by the percentage cap
    (0.35 x 40 = 14 kg against a 16 kg bell) and the code is ``youth_load_exceeded``. Both are
    refusals of the same kettlebell; the split is PRP-00's and is not re-litigated here (D-176).
    """
    response = await teen_client.post("/api/import", json={"text": doc.YOUTH_KB_SWING, "profile": "son"})
    assert response.status_code == 422
    assert "youth_load_exceeded" in codes_of(response.json())
    assert "son-swing" not in stored_ids(settings)


async def test_import_youth_allowlist_denies_unknown_bodyweight(unweighed_teen: FastAPI, settings) -> None:
    """Test 5b, second half: the route passes the *real* bodyweight, so an unknown one denies.

    This is the assertion that catches a ``validate_workout`` call left on its default arguments:
    with ``bodyweight_kg`` defaulted the conditional movement would be judged on the absolute cap
    alone, and 16 kg is exactly at that cap for this band (PRP-08 risk 10).
    """
    transport = httpx.ASGITransport(app=unweighed_teen)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/import", json={"text": doc.YOUTH_KB_SWING, "profile": "son"})
    assert response.status_code == 422
    assert codes_of(response.json()) == {"youth_exercise_not_allowed"}
    assert "son-swing" not in stored_ids(settings)


async def test_import_for_son_is_not_validated_as_an_adult(aged_son_client: httpx.AsyncClient) -> None:
    """PRP-08 risk 11: the same document, judged differently by the two profiles it names."""
    as_adult = await aged_son_client.post("/api/import", json={"text": doc.YOUTH_FIVE_REPS, "profile": "me"})
    as_son = await aged_son_client.post("/api/import", json={"text": doc.YOUTH_FIVE_REPS, "profile": "son"})
    assert as_adult.status_code == 422  # a youth-targeted document, so the kind mismatches
    assert "profile_kind_mismatch" in codes_of(as_adult.json())
    assert "youth_rep_floor" in codes_of(as_son.json())


async def test_unknown_profile_is_refused_never_defaulted(seeded_client: httpx.AsyncClient) -> None:
    """Defaulting a wrong profile to ``me`` is how the son's plan gets an adult workout."""
    response = await seeded_client.post("/api/import", json={"text": doc.ADULT_WORKOUT, "profile": "sonn"})
    assert response.status_code == 422
    assert "parse_error" in codes_of(response.json())


def test_a_warning_does_not_reject_the_document(library) -> None:
    """D-172: the gate is ``result.ok``, so a document carrying only warnings still imports.

    Driven at the service level because the route cannot produce a warning: ``age_band()`` never
    hands a youth profile the ``adult`` band (D-099), and ``youth_band_coerced`` is the only
    ``warn``-severity code this build has. The plumbing is real either way, and stays correct
    whichever way PRP-00 settles the disagreement between its severity table and its docstring.
    """
    from cadence.bibliotheque.import_service import ImportTarget, prepare
    from cadence.schema.enums import AgeBand, Equipment

    target = ImportTarget(
        profile_id="son",
        profile_kind="youth",
        age_band=AgeBand.ADULT,
        equipment=tuple(Equipment),
        bodyweight_kg=None,
        has_overhead_anchor=False,
    )
    prepared = prepare(
        doc.YOUTH_WORKOUT,
        target=target,
        catalog=library.exercises,
        youth_rules=library.youth_rules,
    )
    assert prepared.ok is True
    assert prepared.errors == ()
    assert len(prepared.warnings) == 1
    assert "strictest band" in prepared.warnings[0]
    assert prepared.workout is not None


def test_the_warning_reaches_the_import_payload(library) -> None:
    """And the warning is carried out to ``data.warnings`` rather than being dropped."""
    from cadence.bibliotheque.import_service import ImportTarget, prepare
    from cadence.bibliotheque.ingestion import Context, stored_payload
    from cadence.profils.settings import DEFAULT_SETTINGS
    from cadence.profils.tables import Profile
    from cadence.schema.enums import AgeBand, Equipment

    target = ImportTarget("son", "youth", AgeBand.ADULT, tuple(Equipment), None, False)
    prepared = prepare(doc.YOUTH_WORKOUT, target=target, catalog=library.exercises, youth_rules=library.youth_rules)
    context = Context(
        profile=Profile(id="son", display_name="Son", kind="youth"),
        target=target,
        settings=DEFAULT_SETTINGS,
        catalog=library.exercises,
        seed_catalog=library.exercises,
        youth_rules=library.youth_rules,
        rules=None,
        taken_workout_ids=frozenset(),
        taken_exercise_ids=frozenset(),
    )
    payload = stored_payload(prepared, "son-fun", context)
    assert payload["warnings"] == list(prepared.warnings)
