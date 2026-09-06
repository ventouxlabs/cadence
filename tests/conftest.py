"""Shared fixtures.

Every test gets a fresh file-backed database under ``tmp_path``. Never ``:memory:``: WAL and
the connect-time pragma listener behave differently there, and PRP-06 needs concurrent
connections against the same file.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi import FastAPI

from cadence.config import Settings, get_settings
from cadence.db import init_db, reset_engines
from cadence.main import create_app
from cadence.schema import AgeBand, Exercise, YouthRules, YouthRuleSet

ENV_VARS = (
    "CADENCE_DB_PATH",
    "CADENCE_HOST",
    "CADENCE_PORT",
    "CADENCE_VITALFORGE_MODE",
    "VITALFORGE_WEIGHT_URL",
    "VITALFORGE_DASHBOARD_URL",
    "VITALFORGE_TOKEN",
    "VITALFORGE_PERSON_ME",
    "VITALFORGE_PERSON_SON",
    "OMNIROUTE_URL",
    "OMNIROUTE_KEY",
    "OMNIROUTE_MODEL_GENERATE",
    "OMNIROUTE_MODEL_SUMMARY",
    "TZ",
)

LIBRARY_PATH = Path(__file__).resolve().parents[1] / "library"
YOUTH_RULES_PATH = LIBRARY_PATH / "youth_rules.yaml"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "cadence.db"


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No ambient Cadence env vars, and no cached settings from another test."""
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
    reset_engines()


@pytest.fixture
def settings(db_path: Path, clean_env: None) -> Settings:
    return Settings(
        CADENCE_DB_PATH=db_path,
        CADENCE_VITALFORGE_MODE="mock",
        VITALFORGE_TOKEN="",
        OMNIROUTE_KEY="",
        _env_file=None,
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    application = create_app(settings)
    # httpx.ASGITransport does not run the lifespan, so the schema is created here.
    init_db(settings)
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture(scope="session")
def youth_rules() -> dict[AgeBand, YouthRuleSet]:
    """The shipped band table, parsed. Test 25 is driven off this file, not off constants."""
    parsed = YouthRules.model_validate(yaml.safe_load(YOUTH_RULES_PATH.read_text()))
    return dict(parsed.root)


@pytest.fixture
def exercise_catalog() -> dict[str, Exercise]:
    from tests.factories import CATALOG

    return dict(CATALOG)


@pytest.fixture(scope="session")
def library():
    """The real ``library/`` directory, loaded and validated once for the whole run."""
    from cadence.bibliotheque.loader import load_library

    return load_library(LIBRARY_PATH)


@pytest.fixture
def adult_profile():
    from cadence.profils.tables import Profile

    return Profile(id="me", display_name="Me", kind="adult", push_to_garmin=True)


@pytest.fixture
def youth_profile():
    """The seeded son: youth, no recorded age, so the strictest band (principles section 3.8)."""
    from cadence.profils.tables import Profile

    return Profile(id="son", display_name="Son", kind="youth", age_years=None)


@pytest.fixture
def program_settings():
    from cadence.profils.settings import DEFAULT_SETTINGS

    return DEFAULT_SETTINGS
