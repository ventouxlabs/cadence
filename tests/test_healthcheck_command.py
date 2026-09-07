"""Acceptance tests 3-4, executed rather than grepped.

``tests/test_deploy_files.py`` asserts the ``HEALTHCHECK`` line *mentions* ``/api/health`` and
``ok``. That is a string match, and a string match cannot tell the difference between a probe
that inspects the field and one that merely names it in a comment. The whole point of test 4 is
the case PRP-00 built deliberately: ``/api/health`` answers **200 with ``ok: false``** when the
database check fails, so a status-only probe calls a broken app healthy and the container never
restarts.

So the command is lifted out of the Dockerfile verbatim and run against three servers:

* nothing listening   -> non-zero (the container is still booting, or dead)
* ``{"ok": true}``    -> zero
* ``{"ok": false}``   -> non-zero, and this is the one nothing else in the suite proves

The last case is a stub, not the real app: making the real app answer ``ok: false`` needs a
broken database, and that is already ``test_smoke_script.py::test_smoke_fails_when_health_not_ok``.
What is under test here is the probe, not the endpoint.
"""

from __future__ import annotations

import json
import re
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DOCKERFILE = (REPO / "Dockerfile").read_text()


def _healthcheck_command() -> str:
    """The shell command from the Dockerfile's HEALTHCHECK, with its line continuations joined.

    Taken from the file rather than restated here: a test that carries its own copy of the
    command passes forever after someone edits the Dockerfile, which is the failure it exists
    to catch.
    """
    match = re.search(r"^HEALTHCHECK\b(.*?)\bCMD\s+(.*?)(?=\n[A-Z#]|\Z)", DOCKERFILE, re.DOTALL | re.MULTILINE)
    assert match, "no HEALTHCHECK ... CMD in the Dockerfile"
    return match.group(2).replace("\\\n", " ").strip()


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _run_probe(port: int) -> subprocess.CompletedProcess[str]:
    """Run the Dockerfile's probe against ``port`` instead of the image's fixed 8000."""
    command = _healthcheck_command().replace("localhost:8000", f"127.0.0.1:{port}")
    # `python` is on PATH inside the image; the test venv's interpreter is the equivalent here.
    command = command.replace("python -c", f"{sys.executable} -c", 1)
    return subprocess.run(command, shell=True, capture_output=True, text=True, check=False, timeout=30)  # noqa: S602


@pytest.fixture
def health_server() -> Iterator:
    """A one-route stub whose ``/api/health`` payload the test chooses."""
    payload: dict[str, object] = {"ok": True, "data": {"db": "ok"}, "error": None, "meta": {}}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
            body = json.dumps(payload).encode()
            # 200 in every case on purpose: the 200-with-ok:false shape is the thing under test.
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, payload
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_healthcheck_command_is_extractable_and_hits_api_health() -> None:
    """3. And the extraction itself works, so the two tests below are running something real."""
    command = _healthcheck_command()
    assert "/api/health" in command
    assert command.startswith("python -c")


def test_healthcheck_fails_before_the_app_listens() -> None:
    """A container that has not finished booting must not report healthy.

    This is what ``--start-period=20s`` is for; without a probe that actually fails here, the
    start period would be papering over nothing.
    """
    dead = _free_port()  # bound and released, so nothing is listening on it
    result = _run_probe(dead)
    assert result.returncode != 0, "the probe passed against a port with no server on it"


def test_healthcheck_passes_when_the_app_reports_ok(health_server: tuple) -> None:
    """The other half: a healthy app must not be restarted in a loop."""
    port, _ = health_server
    result = _run_probe(port)
    assert result.returncode == 0, f"a healthy app was called unhealthy: {result.stderr}"


def test_healthcheck_fails_on_200_with_ok_false(health_server: tuple) -> None:
    """4, for real. The case a status-only probe gets wrong.

    ``/api/health`` returns 200 with ``ok: false`` when the database check fails (PRP-00 §6).
    A probe that stopped at the status code would leave a Cadence that cannot open its database
    sitting there marked healthy, behind a proxy, answering every request with an error.
    """
    port, payload = health_server
    payload["ok"] = False
    payload["data"] = {"db": "error"}

    result = _run_probe(port)

    assert result.returncode != 0, "a 200 carrying ok:false was reported healthy"
