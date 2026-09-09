"""Solo mode on the HTML screens: the tab strip, and the redirect off a hidden profile (D-260).

Built the way ``gate.py`` is built, and for the same reason. One dependency, registered on the
HTML routers in ``main.py`` rather than repeated inside each handler, so a screen added later is
covered by being registered rather than by somebody remembering (D-262). It does two jobs on the
one settings read it already has to make:

- it puts the tabs this household gets on ``request.state``, which is what turned ``PROFILE_TABS``
  from a frozen Jinja global into something derived per request (D-263);
- it raises ``ProfileHidden`` for a **GET** naming a profile the household is not showing, which
  ``create_app`` answers with a 303 to the same screen on ``?profile=me``.

**GET only, and that is not a detail.** ``/today``'s tick, adjust, felt and Done routes are
addressed by session id and carry ``?profile=`` only as a hint for where to send the browser
afterwards. Redirecting one of those would throw away a write - including a write the offline
queue is replaying for a session started before the toggle was touched. A hidden profile costs
you the *screen*, never the work (D-264).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from fastapi.responses import RedirectResponse, Response
from sqlmodel import Session

from cadence.db import get_session
from cadence.profils.tables import PROFILE_ME
from cadence.profils.visibility import is_hidden, visible_profile_tabs
from cadence.seance.catalog import load_settings

SEE_OTHER = 303
PROFILE_QUERY = "profile"

#: Where somebody lands when the screen they asked for cannot be rewritten onto the parent's tab.
SOLO_HOME = f"/today?{PROFILE_QUERY}={PROFILE_ME}"

#: Routes that answer with a *piece* of a page rather than a page, and the screen each belongs to
#: (D-272). Rewriting ``?profile=`` on one of these lands on the same fragment under a tab that
#: does not own it, which is that fragment's own 404 - a hard landing on the very path D-260 says
#: must be soft. They go to their parent screen instead, which is where the person can actually
#: stand. Keyed by prefix because both carry an id in the path.
FRAGMENT_PARENTS: tuple[tuple[str, str], ...] = (
    ("/history/sessions/", "/history"),
    ("/history/badges/", "/history"),
)


class ProfileHidden(Exception):
    """Raised on a screen solo mode will not show. Carries where to send the browser instead.

    ``target`` is explicit rather than derived in the handler because the two callers know
    different things. The dependency knows which *tab* was asked for and can usually keep the
    path; ``done_page`` knows the session's **owner** is hidden, and keeping that path would
    redirect to the same hidden person's screen for ever (D-271).
    """

    def __init__(self, target: str) -> None:
        super().__init__(target)
        self.target = target


def _tab_target(path: str) -> str:
    """The parent screen for a fragment, or the same path on the parent's tab for a screen."""
    for prefix, parent in FRAGMENT_PARENTS:
        if path.startswith(prefix):
            return f"{parent}?{PROFILE_QUERY}={PROFILE_ME}"
    return f"{path}?{PROFILE_QUERY}={PROFILE_ME}"


def require_visible_profile(request: Request, db: Annotated[Session, Depends(get_session)]) -> None:
    """Build this request's tab strip, and bounce a GET that names somebody who is hidden."""
    settings = load_settings(db)
    request.state.profile_tabs = visible_profile_tabs(settings)
    if request.method == "GET" and is_hidden(settings, request.query_params.get(PROFILE_QUERY)):
        raise ProfileHidden(_tab_target(request.url.path))


def refuse_hidden_owner(db: Session, profile_id: str) -> None:
    """Raise if this **session** belongs to somebody the household is not showing (D-271).

    The tab check cannot answer this one. ``/done/{id}`` is addressed by session id, and the tab
    on it is decoration: ``app.js`` navigates to ``/done/{id}`` with **no** ``?profile=`` at all
    when it lands a queued Done, so a check keyed on the query would have let the son's summary
    render on every offline finish - and a check that *required* the query would have broken that
    same flow for a household with nothing hidden. Asking who owns the session answers both.

    The target is Today rather than this path on another tab, because this path *is* the hidden
    person's screen however the query reads: rewriting the query would bounce here again.
    """
    if is_hidden(load_settings(db), profile_id):
        raise ProfileHidden(SOLO_HOME)


def hidden_redirect(request: Request, exc: Exception) -> Response:
    """The same screen, on the parent's tab. A gentle 303, never a 404.

    A stale bookmark, a tab left open on the son's screen, or a page the service worker cached
    before the toggle was turned off is not a mistake worth an error page: the person holding the
    phone asked for a screen that still exists, and the only thing that has changed is whose
    column it shows. Where "instead" is depends on what was asked for, so the raiser names it:
    usually the same path on the parent's tab, a fragment's parent screen, or Today.

    Every other query parameter is dropped on purpose. They address the hidden profile's own
    things (``?open=`` is one of his History cards), so carrying them onto the parent's tab would
    turn a soft landing into the 404 this redirect exists to avoid.

    An HTMX request is answered with ``HX-Redirect`` rather than the 303 (D-269). HTMX follows a
    redirect itself and swaps whatever comes back into the target, and several of these routes
    are *fragments* - a badge caption, one History card. The redirected request renders a whole
    ``<!DOCTYPE html>`` document, so the 303 would have posted an entire page into a one-line
    caption slot. ``HX-Redirect`` is how a partial asks for a real navigation, which is what this
    is; ``today.py`` and ``settings.py`` already answer this way for the same reason.
    """
    target = getattr(exc, "target", None) or _tab_target(request.url.path)
    if request.headers.get("hx-request", "").lower() == "true":
        return Response(status_code=204, headers={"HX-Redirect": target})
    return RedirectResponse(target, status_code=SEE_OTHER)


__all__ = [
    "SOLO_HOME",
    "ProfileHidden",
    "hidden_redirect",
    "refuse_hidden_owner",
    "require_visible_profile",
]
