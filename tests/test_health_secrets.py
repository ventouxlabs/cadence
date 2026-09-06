"""``GET /api/health`` must not leak a token, by any route out of the process.

Acceptance test 3 in ``tests/test_health.py`` passes the token as a constructor argument. This
file drives the same assertion through the **environment**, which is the path production uses:
``BaseSettings`` reads ``VITALFORGE_TOKEN`` from ``os.environ``, and a leak there would reach a
real deployment while the constructor test stayed green. Logs are checked as well as the body,
because the healthcheck is polled every thirty seconds and a token in a log line is a token on
disk forever.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from cadence.config import Settings, get_settings
from cadence.db import init_db
from cadence.main import create_app

TOKEN = "vf-live-DO-NOT-LEAK-8f3a9c2b"
KEY = "or-live-DO-NOT-LEAK-1d4e7f0a"


async def _health_response(db_path: Path) -> AsyncIterator[httpx.Response]:
    settings = Settings(CADENCE_DB_PATH=db_path)
    application = create_app(settings)
    init_db(settings)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield await client.get("/api/health")


@pytest.fixture
def token_env(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """A token in the environment, exactly as compose would supply one."""
    monkeypatch.setenv("VITALFORGE_TOKEN", TOKEN)
    monkeypatch.setenv("OMNIROUTE_KEY", KEY)
    monkeypatch.setenv("CADENCE_VITALFORGE_MODE", "mock")
    get_settings.cache_clear()


async def test_health_never_returns_a_token_set_in_the_environment(
    token_env: None, db_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The token is read, reported as a boolean, and never rendered."""
    with caplog.at_level(logging.DEBUG):
        async for response in _health_response(db_path):
            assert response.status_code == 200
            body = response.text
            payload = response.json()

    assert TOKEN not in body
    assert KEY not in body
    # Nothing token-shaped, even partially: a truncated secret is still a secret.
    assert "DO-NOT-LEAK" not in body
    assert TOKEN[:8] not in body

    # The one thing the route may say about the token is whether there is one.
    assert payload["data"]["vitalforge"] == {"configured": True, "mode": "mock"}
    assert payload["ok"] is True
    assert set(payload) == {"ok", "data", "error", "meta"}

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert TOKEN not in logged
    assert KEY not in logged
    assert "DO-NOT-LEAK" not in logged


async def test_the_whole_health_payload_carries_no_secret_shaped_string(token_env: None, db_path: Path) -> None:
    """Walk the serialised body rather than trusting the shape we expect it to have."""
    async for response in _health_response(db_path):
        payload = response.json()

    flattened = json.dumps(payload)
    for forbidden in (TOKEN, KEY, "vf-live", "or-live"):
        assert forbidden not in flattened
    assert "token" not in flattened.lower()
    assert "key" not in flattened.lower()
    assert "secret" not in flattened.lower()


async def test_health_reports_unconfigured_when_the_environment_token_is_blank(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, db_path: Path
) -> None:
    """A variable present but empty is not a configured token."""
    monkeypatch.setenv("VITALFORGE_TOKEN", "")
    monkeypatch.setenv("CADENCE_VITALFORGE_MODE", "live")
    get_settings.cache_clear()
    async for response in _health_response(db_path):
        assert response.json()["data"]["vitalforge"] == {"configured": False, "mode": "live"}


async def test_health_is_a_config_read_not_a_probe(token_env: None, db_path: Path) -> None:
    """With a token set it still opens no socket. Compose polls this every thirty seconds."""
    import socket

    real_connect = socket.socket.connect
    opened: list[object] = []

    def record(self: socket.socket, address: object) -> None:
        opened.append(address)
        return real_connect(self, address)

    original = socket.socket.connect
    socket.socket.connect = record  # type: ignore[method-assign]
    try:
        async for response in _health_response(db_path):
            assert response.status_code == 200
    finally:
        socket.socket.connect = original  # type: ignore[method-assign]

    assert opened == []


def test_the_settings_object_behind_the_route_still_hides_the_token(token_env: None) -> None:
    """Belt and braces: whatever a future route does with settings, repr must stay masked."""
    settings = get_settings()
    assert settings.vitalforge_configured is True
    assert TOKEN not in repr(settings)
    assert TOKEN not in str(settings.model_dump(mode="json"))
    assert settings.vitalforge_token.get_secret_value() == TOKEN
