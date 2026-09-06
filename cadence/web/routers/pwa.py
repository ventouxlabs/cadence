"""The front door and the PWA entry points.

``/sw.js`` is served from the **origin root**, not from ``/static/``. A worker registered under
``/static/`` has that as its scope and can never control ``/today``, so offline would silently do
nothing while every local test passed (PRP-02 risk 5).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from sqlmodel import Session

from cadence.db import get_session
from cadence.seance.catalog import load_settings, setup_complete
from cadence.web.rendering import STATIC_DIR, templates

router = APIRouter(tags=["pwa"])

SEE_OTHER = 303
JS_TYPE = "application/javascript"


@router.get("/")
def root(request: Request, db: Annotated[Session, Depends(get_session)]) -> Response:
    """Today once the app is set up, and a page saying how to set it up when it is not.

    PRP-03 owns ``/setup``. Redirecting there now would 404 the front door, so until that lands
    an unseeded install gets a page naming the command rather than a broken hop (D-072).
    """
    if setup_complete(db, load_settings(db)):
        return RedirectResponse("/today?profile=me", status_code=SEE_OTHER)
    body = templates.get_template("message.html").render(
        request=request,
        message="No profiles yet. Run `make seed` to load the library and build the first block.",
        profile_key="me",
        view=None,
    )
    return HTMLResponse(body, status_code=200)


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
