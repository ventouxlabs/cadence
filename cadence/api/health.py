"""``GET /api/health`` - the compose healthcheck.

This route makes no network call. Compose polls it every 30 s (PRP-09) and the VitalForge
field is a static config read, so it stays cheap and cannot hang on an unreachable dependency.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlmodel import Session

from cadence.api.envelope import ok
from cadence.config import Settings, get_settings
from cadence.db import get_session

router = APIRouter(prefix="/api", tags=["health"])

FALLBACK_VERSION = "0.0.0"


def app_version() -> str:
    """Installed package version, or a placeholder when running from a source tree."""
    try:
        return package_version("cadence")
    except PackageNotFoundError:  # pragma: no cover - only outside an installed venv
        return FALLBACK_VERSION


def _db_status(session: Session) -> str:
    try:
        session.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 - health must report, never raise
        return "error"
    return "ok"


@router.get("/health")
def health(
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    db = _db_status(session)
    payload = {
        "status": "ok" if db == "ok" else "degraded",
        "db": db,
        # ``env`` and ``mode`` together, because "is this real" is one question with two
        # halves: a prod deployment answering ``mode: mock`` would be reporting every session
        # synced while sending nothing (D-139). Settings validation refuses that pairing at
        # startup; this is how an operator sees it without reading the container's environment.
        "env": settings.env,
        "vitalforge": {"configured": settings.vitalforge_configured, "mode": settings.vitalforge_mode},
        "version": app_version(),
    }
    envelope = ok(payload)
    envelope["ok"] = db == "ok"
    return envelope
