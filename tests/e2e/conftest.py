"""A real uvicorn server on a free port, over a seeded temporary database.

The end-to-end tests exercise the service worker and the IndexedDB queue, and neither exists
under an ASGI transport: they need an origin a browser will treat as secure, which ``127.0.0.1``
is. Port comes from ``CADENCE_PORT`` when set, otherwise from the kernel (8000 is taken on JD's
workstation and 8090 is the ``make dev`` default, so neither is hard-coded here).
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

REPO = Path(__file__).resolve().parents[2]
BOOT_TIMEOUT_S = 40
POLL_S = 0.2
PHONE = {"width": 390, "height": 844}
TABLET = {"width": 1024, "height": 768}


def _free_port() -> int:
    configured = os.environ.get("CADENCE_PORT")
    if configured:
        return int(configured)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _seed(path: Path, **env_overrides: str) -> Path:
    """A database seeded exactly the way ``make seed`` seeds the real one.

    ``env_overrides`` matters for more than the seeder's own settings: ``VITALFORGE_PERSON_ME``
    and ``VITALFORGE_PERSON_SON`` are written onto the **profile rows** here (D-017, D-114), and
    ``sync`` reads the slug off the profile rather than off the settings. A database seeded
    without them makes every write-back ``skipped`` for a missing slug, whatever the server's
    token says.
    """
    env = {**os.environ, "CADENCE_DB_PATH": str(path), **env_overrides}
    result = subprocess.run(
        [sys.executable, "-m", "cadence.bibliotheque.seed"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"seeding the end-to-end database failed:\n{result.stdout}\n{result.stderr}")
    return path


@contextmanager
def serve(db: Path, log_name: str, **env_overrides: str) -> Iterator[str]:
    """One uvicorn on a free port against ``db``, torn down on the way out.

    Factored out of ``base_url`` so PRP-06 can run a second and a third app beside the mock-mode
    one: the sync line the Done screen shows depends on the *deployment* - mocked, live against
    something unreachable, live with no token - and that is an environment variable read at
    startup, not something a test can change from inside the browser.
    """
    port = _free_port()
    # Periodic sync off by default (D-140): the five-minute loop would drain and refresh
    # underneath a running test, and the trend card reads a ``metrics_cache`` row
    # ``history_seed`` writes by hand. A deployment that wants the loop passes it back on.
    env = {
        **os.environ,
        "CADENCE_DB_PATH": str(db),
        "CADENCE_PERIODIC_SYNC": "0",
        **env_overrides,
    }
    # Straight to a file, never a pipe nobody reads: uvicorn's access log fills a 64 KB pipe
    # buffer partway through the suite and the server blocks on its own stdout forever.
    log = db.parent / log_name
    handle = log.open("w")
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "cadence.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=REPO,
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
        text=True,
    )
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + BOOT_TIMEOUT_S
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail(f"the app exited before it listened:\n{log.read_text()}")
            try:
                if httpx.get(f"{url}/api/health", timeout=1.0).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(POLL_S)
        else:  # pragma: no cover - only on a machine too slow to boot uvicorn in 40 s
            pytest.fail("the app did not start in time")
        yield url
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            process.kill()
        finally:
            handle.close()


@pytest.fixture(scope="session")
def server_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _seed(tmp_path_factory.mktemp("e2e") / "cadence.db")


@pytest.fixture(scope="session")
def base_url(server_db: Path) -> Iterator[str]:
    """The running app. ``page.goto('/today')`` resolves against this."""
    with serve(server_db, "uvicorn.log", CADENCE_VITALFORGE_MODE="mock") as url:
        yield url


# ``127.0.0.1:1`` is closed on every machine, so a POST there is refused immediately rather than
# spending the client's five-second timeout. That is the "VitalForge is down" deployment.
DEAD_HOST = "http://127.0.0.1:1"

# Written onto the profile rows by the seeder, and frozen onto each job by ``sync.enqueue``.
VF_PERSONS = {"VITALFORGE_PERSON_ME": "jd", "VITALFORGE_PERSON_SON": "kid"}


@pytest.fixture(scope="session")
def failing_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _seed(tmp_path_factory.mktemp("e2e-failing") / "cadence.db", **VF_PERSONS)


@pytest.fixture(scope="session")
def failing_url(failing_db: Path) -> Iterator[str]:
    """A live deployment whose VitalForge is unreachable.

    Both a token and the person slugs are set. Without the slugs the client raises
    ``VitalForgeNotConfigured`` before any socket is opened and the job lands ``skipped``, which
    is the *other* screen entirely - "stored locally" rather than "sync failed (retrying)".
    """
    with serve(
        failing_db,
        "uvicorn-failing.log",
        CADENCE_VITALFORGE_MODE="live",
        VITALFORGE_WEIGHT_URL=DEAD_HOST,
        VITALFORGE_DASHBOARD_URL=DEAD_HOST,
        VITALFORGE_TOKEN="e2e-not-a-real-token",
        VITALFORGE_PERSON_ME="jd",
        VITALFORGE_PERSON_SON="kid",
    ) as url:
        yield url


@pytest.fixture(scope="session")
def tokenless_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Slugs and all: the only thing missing on this deployment must be the token."""
    return _seed(tmp_path_factory.mktemp("e2e-tokenless") / "cadence.db", **VF_PERSONS)


@pytest.fixture(scope="session")
def tokenless_url(tokenless_db: Path) -> Iterator[str]:
    """A live deployment with no token: the state a fresh install is in before JD pastes one.

    The URLs still point at the dead host, so a client that decided to try anyway would fail
    loudly here instead of reaching the real ``weight.grepon.cc`` default.
    """
    with serve(
        tokenless_db,
        "uvicorn-tokenless.log",
        CADENCE_VITALFORGE_MODE="live",
        VITALFORGE_WEIGHT_URL=DEAD_HOST,
        VITALFORGE_DASHBOARD_URL=DEAD_HOST,
        VITALFORGE_TOKEN="",
        VITALFORGE_PERSON_ME="jd",
        VITALFORGE_PERSON_SON="kid",
    ) as url:
        yield url


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args: dict, base_url: str) -> dict:
    """Every context is a 390x844 phone unless a test says otherwise."""
    return {**browser_context_args, "base_url": base_url, "viewport": PHONE}


@pytest.fixture
def api(base_url: str) -> Iterator[httpx.Client]:
    """A plain HTTP client against the same server, for asserting what actually landed."""
    with httpx.Client(base_url=base_url, timeout=5.0) as client:
        yield client


@pytest.fixture
def fresh_session(api: httpx.Client):
    """Clear whatever a previous test left ticked, so this one gets an untouched checklist.

    The server is one process for the whole session, so tests would otherwise inherit each
    other's ticks.

    It unticks rather than finishing. Finishing was the obvious way to get a clean slate, but it
    burns a planned day per call, and the block holds sixteen: once the suite grew past that the
    plan ran out, ``/api/today`` answered 404, and every test after it died on ``data`` being
    ``None`` rather than on anything it was testing. Unticking leaves the rolling plan where it
    was (D-011), so the fixture costs nothing and the suite is order-independent. Days are still
    consumed by the tests that genuinely finish a session, which is what they are for.
    """

    def _today(profile: str) -> dict:
        body = api.get(f"/api/today?profile={profile}").json()
        data = body.get("data")
        assert data is not None, (
            f"no session available for {profile!r}: {body.get('error')!r}. The seeded block is "
            "exhausted - a test is finishing more planned days than it needs to."
        )
        return data

    def _fresh(profile: str = "me") -> dict:
        data = _today(profile)
        for _ in range(3):
            ticked = [row for row in data["rows"] if row["done"]]
            if not ticked:
                return data
            for row in ticked:
                api.post(f"/api/sessions/{data['session_id']}/rows/{row['position']}", json={"done": False})
            data = _today(profile)
        raise AssertionError(f"could not clear the open session for {profile!r}")

    return _fresh


# ------------------------------------------------------------------ the History seed (PRP-04)
#
# Here rather than in a test module because it is session-scoped and two modules use it: pytest
# treats a session fixture imported into a second module as a second fixture and runs the whole
# setup twice, which collides on the planned rows the first copy already wrote.

# The nav floor. Lower than a checklist row's, because these are chrome rather than the work.
MIN_NAV_PX = 44
EXTRA_SESSIONS = 2
SYNTHETIC_WEEK = 9
ROWS = 5


def _rows_json() -> str:
    return json.dumps(
        [
            {"position": index, "exercise_id": "goblet-squat", "name": "Goblet squat", "reps": 8, "sets": 3}
            for index in range(1, ROWS + 1)
        ]
    )


def _write_session(db: sqlite3.Connection, profile_id: str, day_index: int, finished_at: str) -> None:
    """One finished session over a planned row Today will never surface."""
    planned_id = f"e2e-{profile_id}-{day_index}"
    session_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO planned_session (id, program_id, profile_id, week, day_index, day_type, workout_id,"
        " rows_json, status) VALUES (?, ?, ?, ?, ?, 'upper_a', 'upper-a', ?, 'done')",
        (planned_id, f"e2e-{profile_id}", profile_id, SYNTHETIC_WEEK, day_index, _rows_json()),
    )
    db.execute(
        "INSERT INTO session (id, profile_id, planned_session_id, started_at, finished_at, duration_min, felt)"
        " VALUES (?, ?, ?, ?, ?, 26, 'right')",
        (session_id, profile_id, planned_id, finished_at, finished_at),
    )
    for position in range(1, ROWS + 1):
        db.execute(
            "INSERT INTO session_row (id, session_id, position, exercise_id, done, done_at, is_challenge)"
            " VALUES (?, ?, ?, 'goblet-squat', 1, ?, 0)",
            (f"{session_id}-{position}", session_id, position, finished_at),
        )


@pytest.fixture(scope="session")
def history_seed(server_db: Path, base_url: str) -> dict[str, str]:
    """One real Done through PRP-02's routes, two written rows, and two cached trend points."""
    with httpx.Client(base_url=base_url, timeout=10.0) as api:
        data = api.get("/api/today?profile=me").json()["data"]
        session_id = data["session_id"]
        for row in data["rows"]:
            api.post(f"/api/sessions/{session_id}/rows/{row['position']}", json={"done": True})
        assert api.post(f"/api/sessions/{session_id}/done", json={"felt": "right"}).status_code == 200

    now = datetime.now(UTC)
    today = now.astimezone().date()
    db = sqlite3.connect(server_db, timeout=10)
    try:
        for index in range(EXTRA_SESSIONS):
            _write_session(db, "me", index + 1, (now - timedelta(minutes=index + 1)).isoformat())
        db.execute(
            "INSERT OR REPLACE INTO metrics_cache (profile_id, fetched_at, payload_json, stale) VALUES ('me', ?, ?, 0)",
            (
                now.isoformat(),
                json.dumps(
                    {
                        "weight_kg": [
                            [(today - timedelta(days=28)).isoformat(), 85.2],
                            [(today - timedelta(days=14)).isoformat(), 84.6],
                            [today.isoformat(), 84.1],
                        ],
                        "body_fat_pct": [
                            [(today - timedelta(days=28)).isoformat(), 19.1],
                            [(today - timedelta(days=14)).isoformat(), 18.6],
                            [today.isoformat(), 18.2],
                        ],
                    }
                ),
            ),
        )
        db.commit()
    finally:
        db.close()
    return {"session_id": session_id}


@pytest.fixture(autouse=True)
def no_real_network() -> Iterator[None]:
    """Overrides the unit suite's respx guard, which has no business here.

    ``tests/conftest.py`` blocks every real socket so an unmocked host cannot hang CI for the
    client's five-second timeout. These tests are the opposite case: a real uvicorn on 127.0.0.1,
    reached by a real browser. VitalForge is still never touched - the server runs in mock mode
    (``base_url`` above), so the write-back answers itself in process.
    """
    yield
