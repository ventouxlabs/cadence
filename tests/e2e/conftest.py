"""A real uvicorn server on a free port, over a seeded temporary database.

The end-to-end tests exercise the service worker and the IndexedDB queue, and neither exists
under an ASGI transport: they need an origin a browser will treat as secure, which ``127.0.0.1``
is. Port comes from ``CADENCE_PORT`` when set, otherwise from the kernel (8000 is taken on JD's
workstation and 8090 is the ``make dev`` default, so neither is hard-coded here).
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
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


@pytest.fixture(scope="session")
def server_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A database seeded exactly the way ``make seed`` seeds the real one."""
    path = tmp_path_factory.mktemp("e2e") / "cadence.db"
    env = {**os.environ, "CADENCE_DB_PATH": str(path)}
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


@pytest.fixture(scope="session")
def base_url(server_db: Path) -> Iterator[str]:
    """The running app. ``page.goto('/today')`` resolves against this."""
    port = _free_port()
    env = {**os.environ, "CADENCE_DB_PATH": str(server_db), "CADENCE_VITALFORGE_MODE": "mock"}
    # Straight to a file, never a pipe nobody reads: uvicorn's access log fills a 64 KB pipe
    # buffer partway through the suite and the server blocks on its own stdout forever.
    log = server_db.parent / "uvicorn.log"
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
