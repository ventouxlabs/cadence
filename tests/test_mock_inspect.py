"""``GET /api/_mock/activities`` - the mount guard, and what the route is willing to say (D-169).

The route exists for one assertion in ``scripts/smoke.sh``. Whether it *exists* is the part
worth a test: it reads the fake's recorder with no auth in front of it (D-009), so a change
that mounted it under live mode or under prod would be the regression to catch here.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from cadence.config import Settings
from cadence.db import init_db
from cadence.main import create_app
from cadence.vitalforge import mock

ROUTE = "/api/_mock/activities"


def _app_with(db_path: Path, **overrides: object) -> FastAPI:
    kwargs: dict[str, object] = {
        "CADENCE_DB_PATH": db_path,
        "CADENCE_VITALFORGE_MODE": "mock",
        "VITALFORGE_TOKEN": "",
        "OMNIROUTE_KEY": "",
        "_env_file": None,
    }
    kwargs.update(overrides)
    settings = Settings(**kwargs)  # type: ignore[arg-type]
    application = create_app(settings)
    init_db(settings)
    return application


async def _get(application: FastAPI) -> httpx.Response:
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.get(ROUTE)


@pytest.mark.parametrize(
    ("env", "vitalforge_mode"),
    [("dev", "live"), ("test", "live"), ("prod", "live")],
)
async def test_route_is_absent_outside_mock_mode(
    db_path: Path, clean_env: None, env: str, vitalforge_mode: str
) -> None:
    """The deployment shapes that must never carry it: anything not mocking VitalForge."""
    application = _app_with(db_path, CADENCE_ENV=env, CADENCE_VITALFORGE_MODE=vitalforge_mode)
    assert (await _get(application)).status_code == 404


async def test_route_is_present_at_dev_and_mock(db_path: Path, clean_env: None) -> None:
    """The one shape that mounts it, which is what `make dev-docker` boots."""
    application = _app_with(db_path, CADENCE_ENV="dev")
    response = await _get(application)
    assert response.status_code == 200
    assert response.json()["data"] == {"count": 0, "activities": []}


async def test_route_reports_identifiers_and_never_the_body(db_path: Path, clean_env: None) -> None:
    """Identifiers only: the payload carries a person's loads and reps, behind no auth."""
    application = _app_with(db_path, CADENCE_ENV="dev")
    mock.record("me", {"session_id": 7, "duration_s": 3600, "sets": [{"load_kg": 60}]})
    response = await _get(application)
    assert response.json()["data"] == {"count": 1, "activities": [{"session_id": "7", "slug": "me"}]}
    assert "load_kg" not in response.text
    assert "3600" not in response.text


async def test_route_is_present_at_test_and_mock(db_path: Path, clean_env: None) -> None:
    """The other shape that mounts it: the automated suite's own environment.

    ``dev`` and ``test`` are the two non-prod environments, and the guard is written as "not
    prod" rather than "is dev". Pinning both stops a later narrowing to ``dev`` from taking the
    route out from under a test environment that legitimately mocks VitalForge.
    """
    application = _app_with(db_path, CADENCE_ENV="test")
    assert (await _get(application)).status_code == 200


def test_prod_with_mock_is_refused_before_an_app_exists(db_path: Path, clean_env: None) -> None:
    """The prod+mock pairing never reaches the mount guard at all (D-139).

    The other parametrised cases assert a 404 for prod+live. prod+**mock** is the combination
    that would actually mount the route in production, and it is unreachable for a stronger
    reason than the guard: settings validation refuses a mocked integration under prod, so the
    process does not start. Asserting the 404 here would be asserting the weaker of the two
    guards and would quietly start passing for the wrong reason if validation were relaxed.
    """
    with pytest.raises(ValidationError) as caught:
        Settings(
            CADENCE_DB_PATH=db_path,
            CADENCE_ENV="prod",
            CADENCE_VITALFORGE_MODE="mock",
            VITALFORGE_TOKEN="",
            OMNIROUTE_KEY="",
            _env_file=None,
        )  # type: ignore[arg-type]
    assert "mock" in str(caught.value)
