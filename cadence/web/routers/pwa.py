"""The front door and the PWA entry points.

``/sw.js`` is served from the **origin root**, not from ``/static/``. A worker registered under
``/static/`` has that as its scope and can never control ``/today``, so offline would silently do
nothing while every local test passed (PRP-02 risk 5).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse, RedirectResponse, Response
from sqlmodel import Session

from cadence.db import get_session
from cadence.seance.catalog import load_settings, setup_complete
from cadence.web.rendering import STATIC_DIR

router = APIRouter(tags=["pwa"])

SEE_OTHER = 303
JS_TYPE = "application/javascript"


@router.get("/")
def root(db: Annotated[Session, Depends(get_session)]) -> Response:
    """Setup until the son's age has been asked for, Today once it has (architecture section 4)."""
    if setup_complete(load_settings(db)):
        return RedirectResponse("/today?profile=me", status_code=SEE_OTHER)
    return RedirectResponse("/setup", status_code=SEE_OTHER)


@router.get("/sw.js", include_in_schema=False)
def service_worker() -> Response:
    """The worker, at the root, with the header that lets it claim the whole origin."""
    return FileResponse(
        STATIC_DIR / "sw.js",
        media_type=JS_TYPE,
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )


@router.get("/manifest.json", include_in_schema=False)
def manifest_at_root() -> Response:
    """A convenience alias. The document links ``/static/manifest.json``, which the worker caches."""
    return FileResponse(STATIC_DIR / "manifest.json", media_type="application/manifest+json")
