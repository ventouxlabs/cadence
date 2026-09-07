"""Shared fixtures.

Every test gets a fresh file-backed database under ``tmp_path``. Never ``:memory:``: WAL and
the connect-time pragma listener behave differently there, and PRP-06 needs concurrent
connections against the same file.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import AsyncIterator, Callable, Iterator
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
    "CADENCE_ENV",
    "CADENCE_DB_PATH",
    "CADENCE_PERIODIC_SYNC",
    "CADENCE_HOST",
    "CADENCE_PORT",
    "CADENCE_VITALFORGE_MODE",
    "VITALFORGE_WEIGHT_URL",
    "VITALFORGE_DASHBOARD_URL",
    "VITALFORGE_TOKEN",
    "VITALFORGE_PERSON_ME",
    "VITALFORGE_PERSON_SON",
    "CADENCE_OMNIROUTE_MODE",
    "OMNIROUTE_URL",
    "OMNIROUTE_KEY",
    "OMNIROUTE_MODEL_GENERATE",
    "OMNIROUTE_MODEL_SUMMARY",
    "TZ",
)

LIBRARY_PATH = Path(__file__).resolve().parents[1] / "library"
YOUTH_RULES_PATH = LIBRARY_PATH / "youth_rules.yaml"

# A fake token, and the only one this suite ever sees. Tests assert it never reaches a log.
VF_TOKEN = "vf-test-token-abc123"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "cadence.db"


@pytest.fixture
def tz() -> Iterator[Callable[[str], None]]:
    """Set the process time zone for one test and put it back afterwards.

    ``monkeypatch.setenv("TZ", ...)`` alone is not enough: it restores the variable at teardown
    but never re-runs ``tzset()``, so the C-level zone stays whatever the last test chose for
    every test after it in the process. Every zone-sensitive assertion downstream then depends on
    collection order. This fixture owns both halves.
    """

    def _set(name: str) -> None:
        os.environ["TZ"] = name
        time.tzset()

    yield _set
    os.environ.pop("TZ", None)
    time.tzset()


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
        CADENCE_ENV="test",
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


@pytest.fixture
def db_session(settings: Settings, app: FastAPI):
    """A plain database session against the same file the app uses."""
    from sqlmodel import Session as DbSession

    from cadence.db import get_engine

    with DbSession(get_engine(settings)) as session:
        yield session


@pytest.fixture
def seeded(db_session, app: FastAPI) -> FastAPI:
    """The app with the real library loaded and a four-week block per profile."""
    from cadence.bibliotheque.seed import seed

    seed(db_session, LIBRARY_PATH)
    return app


@pytest.fixture
async def seeded_client(seeded: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=seeded)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
def aged_son(db_session, seeded: FastAPI) -> FastAPI:
    """The son at eleven, so his block carries loaded rows under the ``age_10_13`` caps."""
    from datetime import date

    from cadence.bibliotheque.loader import load_library
    from cadence.bibliotheque.seed import _rebuild_program
    from cadence.profils.settings import DEFAULT_SETTINGS
    from cadence.profils.tables import PROFILE_SON, Profile

    son = db_session.get(Profile, PROFILE_SON)
    son.age_years = 11
    son.age_band = "age_10_13"
    db_session.add(son)
    db_session.commit()
    _rebuild_program(db_session, son, DEFAULT_SETTINGS, load_library(LIBRARY_PATH), date(2026, 9, 6))
    db_session.commit()
    return seeded


@pytest.fixture
async def aged_son_client(aged_son: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """A client over a database where the son is eleven, so his rows carry real loads."""
    transport = httpx.ASGITransport(app=aged_son)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


class OmniRouteGateway:
    """A stand-in for the OmniRoute gateway (PRP-08).

    Replies are queued and consumed in order; the last one repeats, so "always fails" is one
    ``reply`` call. Every request is captured whole, which is what the prompt-contents assertions
    read - a test that only counted calls would not notice a bodyweight travelling.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.bodies: list[dict] = []
        self._replies: list[tuple[int, object]] = []

    def reply(self, text: str, *, status: int = 200) -> OmniRouteGateway:
        """Queue one completion, as an OpenAI-compatible body."""
        self._replies.append((status, {"choices": [{"message": {"role": "assistant", "content": text}}]}))
        return self

    def reply_raw(self, payload: object, *, status: int = 200) -> OmniRouteGateway:
        self._replies.append((status, payload))
        return self

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.bodies.append(json.loads(request.content.decode()))
        if not self._replies:
            return httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})
        status, payload = self._replies[min(len(self.requests) - 1, len(self._replies) - 1)]
        return httpx.Response(status, json=payload)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    @property
    def prompts(self) -> list[str]:
        """The user message of every request, which is the whole outgoing prompt."""
        return [body["messages"][0]["content"] for body in self.bodies]


@pytest.fixture
def omniroute_gateway() -> OmniRouteGateway:
    """The mocked OmniRoute gateway. No test in this repo needs a key or a network."""
    return OmniRouteGateway()


# --------------------------------------------------------------- PRP-06: no real network, ever
#
# PRP-06 section 5.6: an unmocked host would otherwise hang CI for the client's full five-second
# timeout on every call. ``assert_all_mocked`` turns that into an immediate, named failure.
# ``httpx.ASGITransport`` is untouched by respx, so the in-process app clients still work.


@pytest.fixture(autouse=True)
def forget_mock_payloads() -> Iterator[None]:
    """Empty the mock VitalForge's recorder between tests.

    ``mock.POSTED`` is module-level, because the fake stands in for a remote service and "what
    did we send it" is a question about that service rather than about a client instance. That
    makes it shared state across the suite, so a test asserting on the last payload would
    otherwise depend on which tests ran before it.
    """
    from cadence.vitalforge import mock

    mock.reset()
    yield
    mock.reset()


@pytest.fixture(autouse=True)
def no_real_network() -> Iterator[None]:
    import respx

    # The *global* router, not a fresh one: module-level ``respx.get(...)`` in a test registers
    # there, and its ``assert_all_mocked`` default is what turns an unmocked host into a named
    # failure instead of a five-second hang.
    with respx.mock:
        yield


@pytest.fixture
def live_settings(db_path: Path, clean_env: None) -> Settings:
    """Live mode against two throwaway hosts, with a token respx can see on the wire."""
    return Settings(
        CADENCE_DB_PATH=db_path,
        CADENCE_ENV="test",
        CADENCE_VITALFORGE_MODE="live",
        VITALFORGE_WEIGHT_URL="http://weight.test",
        VITALFORGE_DASHBOARD_URL="http://dash.test",
        VITALFORGE_TOKEN=VF_TOKEN,
        VITALFORGE_PERSON_ME="jd",
        VITALFORGE_PERSON_SON="kid",
        OMNIROUTE_KEY="",
        _env_file=None,
    )


@pytest.fixture
def live_app(live_settings: Settings) -> FastAPI:
    application = create_app(live_settings)
    init_db(live_settings)
    return application


@pytest.fixture
async def live_client(live_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=live_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def live_db(live_settings: Settings, live_app: FastAPI):
    from sqlmodel import Session as DbSession

    from cadence.db import get_engine

    with DbSession(get_engine(live_settings)) as session:
        yield session


def complete_setup(db) -> None:
    """Write the ``setting`` rows a finished Setup leaves behind.

    PRP-03 gates every HTML screen on ``setup_complete`` (``cadence/web/gate.py``), so a database
    without these rows answers ``/today`` and ``/done/...`` with a 303 to ``/setup``. ``merge``
    rather than ``add``: a test that has already written one of these keys keeps its value.
    """
    from datetime import UTC, datetime

    from cadence.profils.settings import DEFAULT_SETTINGS
    from cadence.profils.tables import Setting

    stamp = datetime.now(UTC).isoformat()
    for key, value in DEFAULT_SETTINGS.with_changes(setup_complete=True).as_rows().items():
        db.merge(Setting(key=key, value_json=value, updated_at=stamp))
    db.commit()


@pytest.fixture
def vf_profiles(live_db):
    """The two profiles with their VitalForge slugs, as ``make seed`` would leave them (D-017)."""
    from cadence.profils.tables import Profile

    me = Profile(id="me", display_name="Me", kind="adult", push_to_garmin=True, vitalforge_person="jd")
    son = Profile(id="son", display_name="Son", kind="youth", vitalforge_person="kid")
    live_db.add(me)
    live_db.add(son)
    live_db.commit()
    complete_setup(live_db)
    live_db.refresh(me)
    live_db.refresh(son)
    return {"me": me, "son": son}
