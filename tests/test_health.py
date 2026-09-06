"""GET /api/health - acceptance tests 1-4."""

from __future__ import annotations

import socket
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from cadence.config import Settings
from cadence.db import init_db
from cadence.main import create_app


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


async def _health(application: FastAPI) -> httpx.Response:
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.get("/api/health")


async def test_health_ok(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"]["db"] == "ok"
    assert body["data"]["status"] == "ok"
    assert body["error"] is None


async def test_health_reports_vitalforge_unconfigured(client: httpx.AsyncClient) -> None:
    body = (await client.get("/api/health")).json()
    assert body["data"]["vitalforge"] == {"configured": False, "mode": "mock"}


async def test_health_never_leaks_secrets(db_path: Path, clean_env: None) -> None:
    application = _app_with(db_path, VITALFORGE_TOKEN="sekret", OMNIROUTE_KEY="sekret")
    response = await _health(application)
    assert "sekret" not in response.text
    assert response.json()["data"]["vitalforge"]["configured"] is True


async def test_health_makes_no_network_call(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Compose polls this every 30 s; an outbound call would make it hang on a dead dependency."""

    def no_sockets(*args: object, **kwargs: object) -> None:
        raise AssertionError("/api/health opened a network connection")

    real_request = httpx.AsyncClient.request

    async def guarded_request(self: httpx.AsyncClient, method: str, url: object, **kwargs: object):
        host = httpx.URL(str(url)).host
        if host not in ("", "test"):
            raise AssertionError(f"/api/health called out to {url}")
        return await real_request(self, method, url, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", no_sockets)
    monkeypatch.setattr(httpx.AsyncClient, "request", guarded_request)

    response = await client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True
