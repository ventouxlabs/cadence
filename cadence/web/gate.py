"""The setup gate: no HTML screen renders until the son's age has been asked for.

PRP-03 risk 7. Deep-linking ``/today`` before setup would hand the son a loaded checklist built
against the strictest default band rather than his own, so the gate is a dependency on the HTML
routers rather than a check inside one handler - a route added later is gated by being registered,
not by remembering.

It is a dependency and not middleware on purpose. ``/api/*`` must answer in the envelope, never in
a redirect (the service worker and the offline queue both read those), ``/sw.js`` and
``/manifest.json`` are fetched by the browser itself, and D-025 records GZip as the one middleware
this app has.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from fastapi.responses import RedirectResponse, Response
from sqlmodel import Session

from cadence.db import get_session
from cadence.seance.catalog import load_settings, setup_complete

SEE_OTHER = 303
SETUP_PATH = "/setup"


class SetupRequired(Exception):
    """Raised by the gate. Handled in ``create_app`` by a redirect to ``/setup``."""


def require_setup(db: Annotated[Session, Depends(get_session)]) -> None:
    """Let the request through once setup is complete; otherwise send it to ``/setup``."""
    if not setup_complete(load_settings(db)):
        raise SetupRequired


def setup_redirect(_request: Request, _exc: Exception) -> Response:
    """A 303 rather than a 307: the browser must re-issue an interrupted POST as a GET.

    Replaying the body against ``/setup`` would post a Today tick at the setup form, which is a
    422 on a screen the user has not seen yet instead of the screen itself.
    """
    return RedirectResponse(SETUP_PATH, status_code=SEE_OTHER)
