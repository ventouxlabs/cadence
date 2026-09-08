"""The History screen and its one HTMX partial.

History is read-only: nothing here writes. Tapping a card expands it in place and a second tap
collapses it, both without leaving the page. With JavaScript off the same control is an ordinary
link back to ``/history?open=<id>``, which renders the card expanded — the floor PRP-02 set for
every other screen on this app.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlmodel import Session

from cadence.db import get_session
from cadence.historique.badges import badge_by_id, badges_for
from cadence.historique.detail import session_detail
from cadence.historique.page import HistoryView, build_page, resolve_profiles
from cadence.historique.queries import DEFAULT_LIMIT, session_summary
from cadence.seance.catalog import display_unit, load_settings
from cadence.seance.today import TodayError
from cadence.web.rendering import templates

router = APIRouter(tags=["history"])

SEE_OTHER = 303
HISTORY_TAB = "/history"


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request", "").lower() == "true"


def _error_page(request: Request, message: str, status: int) -> HTMLResponse:
    body = templates.get_template("message.html").render(request=request, message=message, profile_key="me", view=None)
    return HTMLResponse(body, status_code=status)


def _context(request: Request, db: Session, view: HistoryView, open_id: str | None) -> dict[str, Any]:
    """One card may arrive already expanded, from the no-JavaScript link. Only that one is read."""
    return {
        "request": request,
        "view": view,
        "profile_key": view.profile_key,
        "tab_base": HISTORY_TAB,
        "unit": display_unit(load_settings(db)),
        "open_id": open_id,
        "open_detail": session_detail(db, open_id) if open_id else None,
    }


@router.get("/history", response_class=HTMLResponse)
def history_page(
    request: Request,
    db: Annotated[Session, Depends(get_session)],
    profile: Annotated[str | None, Query()] = None,
    open: Annotated[str | None, Query()] = None,  # noqa: A002 - the query name is part of the URL
) -> Response:
    try:
        view = build_page(db, profile, DEFAULT_LIMIT)
    except TodayError as exc:
        return _error_page(request, str(exc), 404)
    return templates.TemplateResponse(request, "history.html", _context(request, db, view, open))


@router.get("/history/sessions/{session_id}", response_class=HTMLResponse)
def history_card(
    request: Request,
    session_id: str,
    db: Annotated[Session, Depends(get_session)],
    profile: Annotated[str | None, Query()] = None,
    expand: Annotated[str | None, Query()] = None,
) -> Response:
    """One card, expanded or collapsed. HTMX swaps it; a plain browser is sent back to the page."""
    summary = session_summary(db, session_id)
    if summary is None:
        return _error_page(request, f"no finished session {session_id!r}", 404)
    # A card can only be opened on its owner's tab. Together is the shared screen and carries
    # both, but ``?profile=son`` may not expand one of the parent's sessions: the answer is the
    # same 404 an absent id gets, so the reply says nothing about whose session it was.
    #
    # ``resolve_profiles`` rather than a literal whitelist, so the tab is normalised (case and
    # surrounding space) and read against the profiles that actually exist, exactly as the page
    # itself reads it. A second copy of the key list here is a second thing to keep in step.
    try:
        profile_key, profiles = resolve_profiles(db, profile or summary.profile_id)
    except TodayError:
        return _error_page(request, f"no finished session {session_id!r}", 404)
    if summary.profile_id not in {item.id for item in profiles}:
        return _error_page(request, f"no finished session {session_id!r}", 404)
    expanded = expand != "0"
    if not _is_htmx(request):
        target = f"/history?profile={profile_key}" + (f"&open={session_id}#hs-{session_id}" if expanded else "")
        return RedirectResponse(target, status_code=SEE_OTHER)

    body = templates.get_template("partials/session_row.html").render(
        request=request,
        item=summary,
        profile_key=profile_key,
        detail=session_detail(db, session_id) if expanded else None,
        expanded=expanded,
        unit=display_unit(load_settings(db)),
    )
    return HTMLResponse(body)


@router.get("/history/badges/{profile_id}/{badge_id}", response_class=HTMLResponse)
def badge_caption(
    request: Request,
    profile_id: str,
    badge_id: str,
    db: Annotated[Session, Depends(get_session)],
    profile: Annotated[str | None, Query()] = None,
) -> Response:
    """The caption for one badge. Swaps the line under the strip and never navigates.

    A route rather than JavaScript because History is not in the offline cache, so the round trip
    costs nothing the page has not already paid, and the names are computed server-side anyway —
    a youth profile reads a different word for the same badge.

    A caption can only be read on a tab that owns the profile, the same rule ``history_card``
    applies to a session card and for the same reason: Together carries both people, but
    ``?profile=son`` may not read the parent's strip. The refusal is the 404 an unknown badge id
    gets, so the reply says nothing about whose profile it was.

    The tab is read from ``?profile=`` **only**, never from the path (D-249): an earlier cut fell
    back to ``profile_id`` when the query was absent, which made the check answer its own
    question — every request without a query string resolved the tab from the very profile it was
    meant to be authorising, and so always passed. An omitted query is the parent's tab, which is
    what ``normalise_profile_key`` already defaults to everywhere else.
    """
    try:
        _, profiles = resolve_profiles(db, profile)
    except TodayError:
        return _error_page(request, f"no badge {badge_id!r}", 404)
    owner = next((item for item in profiles if item.id == profile_id), None)
    if owner is None:
        return _error_page(request, f"no badge {badge_id!r}", 404)
    badge = badge_by_id(badges_for(db, profile_id), badge_id)
    if badge is None:
        return _error_page(request, f"no badge {badge_id!r}", 404)
    body = templates.get_template("partials/badge_caption.html").render(
        request=request,
        column=SimpleNamespace(profile=owner),
        caption=badge.caption,
    )
    return HTMLResponse(body)
