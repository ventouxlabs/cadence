"""Acceptance tests 17-20: ``scripts/smoke.sh`` against a real server.

A real uvicorn on an ephemeral port, over a freshly seeded temporary database, the way
``tests/e2e/conftest.py`` does it: the script drives the app over HTTP with curl and jq, so an
ASGI transport would not exercise the thing being tested.
"""

from __future__ import annotations

import http.server
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "smoke.sh"
BOOT_TIMEOUT_S = 60
POLL_S = 0.2

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("curl") is None, reason="smoke.sh needs curl and jq"
)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait_for_health(base: str, process: subprocess.Popen, log: Path) -> None:
    """Block until the server answers, or fail naming what it printed.

    ``urlopen`` rather than httpx: PRP-06 added an autouse respx fixture that refuses any
    unmocked httpx request, and these tests talk to a real subprocess over a real socket.
    respx patches httpx alone, so the standard library goes straight through.
    """
    deadline = time.monotonic() + BOOT_TIMEOUT_S
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail(f"the app exited before it listened:\n{log.read_text()}")
        try:
            with urlopen(f"{base}/api/health", timeout=1.0) as response:  # noqa: S310 - localhost
                if response.status == 200:
                    return
        except (URLError, OSError):
            time.sleep(POLL_S)
    pytest.fail(f"the app did not start in time:\n{log.read_text()}")  # pragma: no cover


def _run_smoke(base: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT)],
        env={**os.environ, "BASE": base, "SMOKE_BOOT_TIMEOUT_S": "10", **env},
        capture_output=True,
        text=True,
        check=False,
        cwd=str(REPO),
        timeout=180,
    )


@pytest.fixture(scope="module")
def vitalforge_trap() -> Iterator[socket.socket]:
    """A listening socket standing in for VitalForge.

    Nothing accepts on it. A connection the app opened would still be queued in the backlog
    afterwards, which is what test 19 checks: the assertion is about bytes leaving the process,
    not about a mock the app was told to use.
    """
    trap = socket.socket()
    trap.bind(("127.0.0.1", 0))
    trap.listen(8)
    trap.settimeout(0)
    yield trap
    trap.close()


@pytest.fixture(scope="module")
def seeded_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("smoke") / "cadence.db"
    result = subprocess.run(
        [sys.executable, "-m", "cadence.bibliotheque.seed"],
        cwd=str(REPO),
        env={**os.environ, "CADENCE_DB_PATH": str(path)},
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"seeding failed:\n{result.stdout}\n{result.stderr}")
    return path


def _boot(db_path: Path, log: Path, **env: str) -> tuple[subprocess.Popen, str]:
    port = _free_port()
    handle = log.open("w")
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "cadence.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(REPO),
        env={**os.environ, "CADENCE_VITALFORGE_MODE": "mock", "CADENCE_DB_PATH": str(db_path), **env},
        stdout=handle,
        stderr=subprocess.STDOUT,
    )
    process._log_handle = handle  # type: ignore[attr-defined]
    return process, f"http://127.0.0.1:{port}"


@pytest.fixture(scope="module")
def running_app(seeded_db: Path, vitalforge_trap: socket.socket) -> Iterator[str]:
    """The app in mock mode, with both VitalForge URLs pointed at the trap."""
    trap_url = f"http://127.0.0.1:{vitalforge_trap.getsockname()[1]}"
    log = seeded_db.parent / "uvicorn.log"
    process, base = _boot(
        seeded_db,
        log,
        VITALFORGE_WEIGHT_URL=trap_url,
        VITALFORGE_DASHBOARD_URL=trap_url,
        VITALFORGE_TOKEN="x",
    )
    try:
        _wait_for_health(base, process, log)
        yield base
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            process.kill()
        process._log_handle.close()  # type: ignore[attr-defined]


@pytest.fixture(scope="module")
def smoke_result(running_app: str) -> subprocess.CompletedProcess[str]:
    """One run, shared: it finishes a real session, so a second run would consume another day."""
    return _run_smoke(running_app)


def test_smoke_passes_against_mock_mode(smoke_result: subprocess.CompletedProcess[str]) -> None:
    """17."""
    assert smoke_result.returncode == 0, f"{smoke_result.stdout}\n{smoke_result.stderr}"
    for step in range(1, 9):
        assert f"step {step}" in smoke_result.stdout, f"step {step} never ran"
    assert "vitalforge mode = mock" in smoke_result.stdout


def test_smoke_tick_is_idempotent(smoke_result: subprocess.CompletedProcess[str]) -> None:
    """20. Step 5 re-POSTs the same tick, which is what the PWA replays out of IndexedDB."""
    assert "step 5 - POST the same tick again (idempotency)" in smoke_result.stdout
    assert smoke_result.returncode == 0


def test_smoke_makes_no_real_vitalforge_call(
    smoke_result: subprocess.CompletedProcess[str], vitalforge_trap: socket.socket
) -> None:
    """19. Nothing left the process for VitalForge while the whole flow ran.

    Both URLs point at a socket that never accepts, so a connection Cadence opened would still
    be queued in its backlog. In mock mode none is opened at all - the client short-circuits
    before the transport - and this is what proves it rather than trusting the switch.
    """
    assert smoke_result.returncode == 0
    with pytest.raises((BlockingIOError, socket.timeout, OSError)):
        vitalforge_trap.accept()
    assert "grepon.cc" not in smoke_result.stdout


def test_smoke_asserts_the_mock_recorded_one_activity(smoke_result: subprocess.CompletedProcess[str]) -> None:
    """The write-back actually happened, and happened once.

    The Done screen's "synced ✓" renders a ``sync_job`` row, so it reads identically whether the
    client handed VitalForge one activity, none, or two. Step 7 asks the mock's own recorder.
    """
    assert "mock recorded exactly 1 activity" in smoke_result.stdout, smoke_result.stdout
    assert "sync line = 'synced ✓'" in smoke_result.stdout, smoke_result.stdout


def test_smoke_metrics_step_no_longer_skips(smoke_result: subprocess.CompletedProcess[str]) -> None:
    """Step 8 asserts /api/metrics for real now that PRP-06 has built it."""
    assert "SKIPPED" not in smoke_result.stdout, smoke_result.stdout


def test_smoke_refuses_live_mode(seeded_db: Path, tmp_path: Path) -> None:
    """Not numbered, but it is the reason step 1 reads the mode back off /api/health: a smoke
    run against a live-mode server would write a real activity into a real Garmin account.

    A second server on its own port, in live mode. It must be refused at step 1, before step 4
    ticks anything - so this costs no planned day out of the seeded block.
    """
    log = tmp_path / "uvicorn-live.log"
    process, base = _boot(seeded_db, log, CADENCE_VITALFORGE_MODE="live")
    try:
        _wait_for_health(base, process, log)
        result = _run_smoke(base)
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            process.kill()
        process._log_handle.close()  # type: ignore[attr-defined]

    assert result.returncode != 0
    assert "not 'mock'" in result.stderr, result.stderr
    assert "step 4" not in result.stdout, "the run ticked a row before refusing"


def test_smoke_fails_when_health_not_ok(tmp_path: Path) -> None:
    """18. An unwritable database exits non-zero naming step 1.

    The wait loop is inside step 1 for exactly this case: the app may answer ``ok: false`` or
    may never listen at all, and from the operator's chair those are the same failure.

    The path is an existing *directory*. A merely absent one proves nothing: ``init_db`` calls
    ``parent.mkdir(parents=True)``, so pointing at a missing directory creates it and the app
    comes up perfectly healthy - which is how this test first passed step 1 and failed at
    step 2 instead.
    """
    unwritable = tmp_path / "cadence.db"
    unwritable.mkdir()
    log = tmp_path / "uvicorn.log"
    process, base = _boot(unwritable, log)
    try:
        result = _run_smoke(base)
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            process.kill()
        process._log_handle.close()  # type: ignore[attr-defined]

    assert result.returncode != 0
    assert "step 1" in result.stderr, result.stderr


def test_smoke_mock_recorder_check_is_conditional_on_mock_mode() -> None:
    """`make smoke BASE=https://cadence.grepon.cc` is a PRP-09 §9 exit criterion.

    `/api/_mock/activities` is mounted only when the mode is mock and CADENCE_ENV is not prod,
    and docker-compose.yml pins CADENCE_ENV=prod in `environment:` - which beats `env_file:`.
    So on VM-201 the route answers 404 in every configuration, and an unconditional assertion on
    it made that exit criterion unreachable. The check must sit behind the mode.
    """
    text = SCRIPT.read_text()
    guard = 'if [[ "$MODE" == "mock" ]]; then'
    assert guard in text, "the mock recorder check is not behind a mode guard"
    assert text.index(guard) < text.index("request GET /api/_mock/activities"), (
        "the mode guard does not precede the /api/_mock/activities request"
    )


# --------------------------------------------------------------- step 7 outside mock mode
#
# `make smoke BASE=https://cadence.grepon.cc` runs against the real VitalForge, and PRP-05's
# activity endpoint is deliberately unpushed (D-005), so the honest states there are not "sent".
# These pin what step 7 accepts and what it still refuses (D-207). Each seeds its own database:
# the run finishes a real session and consumes a planned day.


def _seed_fresh(directory: Path, *, person: str = "me") -> Path:
    """Seed, then give every profile a VitalForge person slug.

    The slug lives on the profile row, not in the environment (D-017), and the seed leaves it
    blank - which is the *shipped* state and makes the client report "not configured" before it
    ever opens a socket. A live-mode test that skipped for that reason would be asserting
    nothing about the write-back.
    """
    path = directory / "cadence.db"
    result = subprocess.run(
        [sys.executable, "-m", "cadence.bibliotheque.seed"],
        cwd=str(REPO),
        env={**os.environ, "CADENCE_DB_PATH": str(path)},
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:  # pragma: no cover
        pytest.fail(f"seeding failed:\n{result.stdout}\n{result.stderr}")
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE profile SET vitalforge_person = ?", (person,))
        connection.commit()
    return path


def _smoke_against_live(tmp_path: Path, **env: str) -> subprocess.CompletedProcess[str]:
    """One live-mode run with SMOKE_ALLOW_LIVE=1, against whatever VitalForge `env` points at."""
    db = _seed_fresh(tmp_path)
    log = tmp_path / "uvicorn-live-step7.log"
    process, base = _boot(
        db,
        log,
        CADENCE_VITALFORGE_MODE="live",
        VITALFORGE_TOKEN="x",
        VITALFORGE_PERSON_ME="me",
        VITALFORGE_PERSON_SON="son",
        **env,
    )
    try:
        _wait_for_health(base, process, log)
        return _run_smoke(base, SMOKE_ALLOW_LIVE="1")
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            process.kill()
        process._log_handle.close()  # type: ignore[attr-defined]


class _Stub(http.server.BaseHTTPRequestHandler):
    """Answers every request with the class's ``status``. Stands in for VitalForge."""

    status = 404

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"detail":"stub"}')

    do_GET = do_POST  # noqa: N815

    def log_message(self, *_args: object) -> None:
        return


@contextmanager
def _stub_vitalforge(status: int) -> Iterator[str]:
    handler = type("Handler", (_Stub,), {"status": status})
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)  # type: ignore[arg-type]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def test_step_7_accepts_will_sync_when_the_activity_endpoint_is_missing(tmp_path: Path) -> None:
    """The state on VM-201 today, and the whole reason the check is not "sent" everywhere.

    A 404 is saved FAILED with ``attempts`` untouched (outcomes.py, D-137), which ``line_for``
    maps to "will sync" - not to "sync failed (retrying)". A check that accepted only the two
    failure wordings would reject exactly the deployment this relaxation exists for.
    """
    with _stub_vitalforge(404) as url:
        result = _smoke_against_live(tmp_path, VITALFORGE_WEIGHT_URL=url, VITALFORGE_DASHBOARD_URL=url)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "will sync" in result.stdout, result.stdout
    assert "all steps passed" in result.stdout, result.stdout


def test_step_7_accepts_a_retrying_write_back(tmp_path: Path, vitalforge_trap: socket.socket) -> None:
    """VitalForge listening but never accepting: a transport failure that will be retried."""
    trap_url = f"http://127.0.0.1:{vitalforge_trap.getsockname()[1]}"
    result = _smoke_against_live(tmp_path, VITALFORGE_WEIGHT_URL=trap_url, VITALFORGE_DASHBOARD_URL=trap_url)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "sync failed (retrying)" in result.stdout, result.stdout


def test_step_7_refuses_an_unconfigured_server(tmp_path: Path) -> None:
    """No token in prod is a misconfigured deploy that otherwise looks like a working one.

    It is the one sync state step 7 must still fail on: every other line means the session
    reached the queue, while this one means it never will until somebody edits the VM's .env.
    """
    db = _seed_fresh(tmp_path)
    log = tmp_path / "uvicorn-unconfigured.log"
    process, base = _boot(db, log, CADENCE_VITALFORGE_MODE="live", VITALFORGE_TOKEN="")
    try:
        _wait_for_health(base, process, log)
        result = _run_smoke(base, SMOKE_ALLOW_LIVE="1")
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            process.kill()
        process._log_handle.close()  # type: ignore[attr-defined]

    assert result.returncode != 0, result.stdout
    assert "step 7" in result.stdout + result.stderr
    assert "VITALFORGE_TOKEN" in result.stdout + result.stderr


def test_step_7_never_accepts_a_terminal_failure_by_prefix() -> None:
    """ "sync failed" is a strict prefix of "sync failed (retrying)".

    A substring match on the shorter string would accept the terminal 409/422 case, which has
    given up. The script must match the full retrying sentence.
    """
    body = SCRIPT.read_text()
    assert 'grep -qF "$LINE_RETRYING"' in body
    assert 'LINE_RETRYING="sync failed (retrying)"' in body
