"""The HTTP client. Every VitalForge call Cadence makes goes through here.

Two base URLs, not one: the weight service (:8085) hosts ``weight/recent`` and ``activity``, the
dashboard (:8086) hosts ``metrics/{name}`` and ``readiness`` (``docs/architecture.md`` section 1).
Sending an activity to the dashboard is a 404 that looks exactly like "PRP-05 is not deployed
yet", so the split is worth a test of its own.

Nothing ``httpx`` raises escapes this module (``errors.py``), and the token is unwrapped at
exactly one point - inside :meth:`VitalForgeClient._headers` - so no header dict, no request
``repr`` and no exception message can carry it into a log file.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from cadence.config import Settings
from cadence.vitalforge import mock
from cadence.vitalforge.errors import (
    VitalForgeHTTPError,
    VitalForgeNotConfigured,
    VitalForgeUnavailable,
    register_secret,
    sanitise,
)

logger = logging.getLogger(__name__)

# One timeout for every call. Done attempts the write-back inline, so this is also the longest a
# user can wait on a slow VitalForge before the summary renders anyway (PRP-06 risk 10).
TIMEOUT_S = 5.0

MOCK_MODE = "mock"

# 202 fresh insert, 200 idempotent repeat. Both mean VitalForge has the session.
ACCEPTED = 202
OK = 200


@dataclass(frozen=True, slots=True)
class ActivityResult:
    """What ``POST /api/activity`` answered, reduced to what the sync queue decides on."""

    status_code: int
    deduplicated: bool = False
    # skipped | pending | synced | failed | unknown - VitalForge's view of the Garmin side.
    garmin_status: str | None = None
    remote_id: str | None = None
    error: str | None = None

    @property
    def stored(self) -> bool:
        return self.status_code in (ACCEPTED, OK)


class VitalForgeClient:
    """Typed calls against one VitalForge deployment.

    ``transport`` is for tests that want a hand-built transport; the suite uses ``respx``, which
    patches ``httpx`` underneath and needs nothing passed here.
    """

    def __init__(self, settings: Settings, transport: httpx.AsyncTransport | None = None) -> None:
        self._settings = settings
        self._transport = transport
        # Before any request: whatever this client is about to send must never come back out of
        # an error message, and ``sanitise`` can only redact a value it has been told about.
        register_secret(settings.vitalforge_token)
        self.timeout = httpx.Timeout(TIMEOUT_S)
        self.mode = settings.vitalforge_mode

    @property
    def configured(self) -> bool:
        """Whether a call would reach anything. Mock mode always would, without a token."""
        return self.mock_mode or self._settings.vitalforge_configured

    @property
    def mock_mode(self) -> bool:
        return self.mode == MOCK_MODE

    def can_reach(self, slug: str) -> bool:
        """Whether a call for ``slug`` would reach anything at all.

        Asked *before* fanning out, so a missing token or an unset person slug costs one INFO
        line rather than seven identical warnings about requests that were never made.
        """
        return self.mock_mode or (self._settings.vitalforge_configured and bool(slug.strip()))

    def _headers(self) -> dict[str, str]:
        """The one place the token is unwrapped. Never logged, never returned, never stored."""
        return {"Authorization": f"Bearer {self._settings.vitalforge_token.get_secret_value()}"}

    def _url(self, base: object, slug: str, path: str) -> str:
        if not slug.strip():
            # Never ``/p//api/...``: an empty slug is a configuration gap, and asking VitalForge
            # about it just turns it into a 404 that reads like a missing deployment.
            raise VitalForgeNotConfigured("no VitalForge person slug is configured for this profile")
        if not self._settings.vitalforge_configured:
            raise VitalForgeNotConfigured("no VitalForge token is configured")
        return f"{str(base).rstrip('/')}/p/{slug.strip()}/api/{path.lstrip('/')}"

    async def _request(self, method: str, url: str, payload: dict[str, Any] | None = None) -> Any:
        """One request, with every failure mode turned into one of this package's errors."""
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self._transport) as client:
                response = await client.request(method, url, json=payload, headers=self._headers())
        except httpx.HTTPError as exc:
            # ``exc`` itself is never logged or re-raised: its repr carries the request, and the
            # request carries the Authorization header.
            raise VitalForgeUnavailable(f"{type(exc).__name__} talking to VitalForge") from None
        if response.status_code >= 400:
            raise VitalForgeHTTPError(response.status_code, _body_text(response))
        return _body_json(response)

    async def get_recent_weight(self, slug: str) -> list[dict[str, Any]]:
        """Last ten weigh-ins, newest first. **No body composition** (contract section 2.1)."""
        if self.mock_mode:
            return mock.recent_weight()
        body = await self._request("GET", self._url(self._settings.vitalforge_weight_url, slug, "weight/recent"))
        return body if isinstance(body, list) else []

    async def get_metric(self, slug: str, name: str, days: int = 30) -> dict[str, Any]:
        """One body-composition or Garmin-derived series. Values are raw: ``metrics.py`` converts."""
        if self.mock_mode:
            return mock.metric(name)
        url = self._url(self._settings.vitalforge_dashboard_url, slug, f"metrics/{name}")
        body = await self._request("GET", f"{url}?days={int(days)}")
        return body if isinstance(body, dict) else {}

    async def get_readiness(self, slug: str) -> dict[str, Any]:
        """``{score, components, status}``. ``score`` is permanently ``null`` for the son."""
        if self.mock_mode:
            return mock.readiness()
        body = await self._request("GET", self._url(self._settings.vitalforge_dashboard_url, slug, "readiness"))
        return body if isinstance(body, dict) else {}

    async def post_activity(self, slug: str, payload: dict[str, Any]) -> ActivityResult:
        """File one finished session. Never raises on a 4xx/5xx - the status decides the retry."""
        if self.mock_mode:
            mock.record(slug, payload)
            return _result(ACCEPTED, mock.activity(str(payload.get("session_id", ""))))
        url = self._url(self._settings.vitalforge_weight_url, slug, "activity")
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self._transport) as client:
                response = await client.post(url, json=payload, headers=self._headers())
        except httpx.HTTPError as exc:
            raise VitalForgeUnavailable(f"{type(exc).__name__} talking to VitalForge") from None
        if response.status_code >= 400:
            return ActivityResult(status_code=response.status_code, error=_detail(response))
        body = _body_json(response)
        return _result(response.status_code, body if isinstance(body, dict) else {})


def _result(status_code: int, body: dict[str, Any]) -> ActivityResult:
    remote = body.get("id")
    return ActivityResult(
        status_code=status_code,
        deduplicated=bool(body.get("deduplicated")),
        garmin_status=_text_or_none(body.get("garmin_status")),
        remote_id=None if remote is None else str(remote),
        error=_text_or_none(body.get("garmin_error")),
    )


def _text_or_none(value: object) -> str | None:
    return None if value is None else sanitise(value)


def _body_text(response: httpx.Response) -> str:
    try:
        return response.text
    except Exception:  # noqa: BLE001 - a body that will not decode must not mask the status code
        return ""


def _body_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        # A 200 with an unparseable body is a broken deployment, not a transport failure. The
        # caller gets an empty answer and the cache stays stale rather than filling with nothing.
        logger.warning("VitalForge answered %s with a body that is not JSON", response.status_code)
        return None


def _detail(response: httpx.Response) -> str:
    """FastAPI's ``{"detail": ...}`` when there is one, else the raw body. Always sanitised."""
    body = _body_json(response)
    if isinstance(body, dict) and body.get("detail") is not None:
        return sanitise(body["detail"])
    return sanitise(_body_text(response))
