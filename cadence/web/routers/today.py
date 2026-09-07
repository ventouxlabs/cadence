"""The Today screen and its HTMX partials.

Every write route answers twice over: an HTMX request gets the partial it asked for, and a plain
form POST gets a 303 back to Today. That is what keeps tick, adjust, felt and Done working with
JavaScript switched off, which is the floor a checklist for a kid has to clear.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlmodel import Session

from cadence.bilan.service import card_due, ensure_retest_queued
from cadence.config import Settings, get_settings
from cadence.db import get_session
from cadence.seance.catalog import display_unit, library_bundle, load_settings
from cadence.seance.done import finish, group_members, is_finished, set_felt, summarise
from cadence.seance.tables import SessionRecord
from cadence.seance.ticks import TickError, adjust, set_done
from cadence.seance.today import (
    SessionView,
    TodayError,
    TodayView,
    normalise_profile_key,
    resolve_today,
    view_for_session,
)
from cadence.vitalforge.client import VitalForgeClient
from cadence.vitalforge.metrics import read_cached
from cadence.vitalforge.schedule import REFUSALS
from cadence.vitalforge.sync import ForceRefused, drain
from cadence.vitalforge.writeback import line_for_session
from cadence.web.rendering import templates

router = APIRouter(tags=["today"])

SEE_OTHER = 303

# ``scope=solo`` on a Done finalises only that session (D-084); anything else finalises the group.
SOLO = "solo"


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request", "").lower() == "true"


def _today_context(
    request: Request,
    view: TodayView,
    confirming: bool = False,
    db: Session | None = None,
    assessment_due: bool = False,
) -> dict[str, Any]:
    """Promotion is per session (``item.promoted``), never per page: see ``SessionView``."""
    return {
        "request": request,
        "view": view,
        "profile_key": view.profile_key,
        "unit": display_unit(view.settings),
        "all_ticked": all(item.all_ticked for item in view.sessions),
        "confirming": confirming,
        "nudge": readiness_nudge(db, view) if db is not None else None,
        # PRP-07's Assessment-day card, for the primary session's profile only.
        "assessment_due": assessment_due,
    }


def readiness_nudge(db: Session, view: TodayView) -> str | None:
    """One line from the cache, for the adult only, and **never** a network call.

    Today is the hot path (``docs/architecture.md`` section 5): this reads the row the periodic
    task wrote and nothing else. The youth profile has no readiness of its own - one Garmin
    credential, one person - so showing him "not available" every day would be noise.

    Off on the Together tab as well (D-135), for the same reason D-105 keeps the body-composition
    card off it: that screen is the one the son is reading over his father's shoulder, and a
    number about the parent's body has no business on it.
    """
    if not view.settings.readiness_nudge_on or view.together:
        return None
    adult = next((item.profile for item in view.sessions if item.profile.kind != "youth"), None)
    if adult is None:
        return None
    cached = read_cached(db, adult)
    return cached.readiness.nudge if cached is not None else None


def _error_page(request: Request, message: str, status: int) -> HTMLResponse:
    body = templates.get_template("message.html").render(request=request, message=message, profile_key="me", view=None)
    return HTMLResponse(body, status_code=status)


def _back(profile_key: str, confirming: bool = False) -> RedirectResponse:
    """Back to Today, keeping the "Done anyway" state so a tick cannot close the felt strip."""
    suffix = "&confirm=1" if confirming else ""
    return RedirectResponse(f"/today?profile={profile_key}{suffix}", status_code=SEE_OTHER)


@router.get("/today", response_class=HTMLResponse)
def today_page(
    request: Request,
    db: Annotated[Session, Depends(get_session)],
    profile: Annotated[str | None, Query()] = None,
    confirm: Annotated[str | None, Query()] = None,
) -> Response:
    try:
        view = resolve_today(db, profile)
    except TodayError as exc:
        return _error_page(request, str(exc), 404)
    # Section 7's four-weekly retest. A write in a GET, deliberately and narrowly: the card is
    # gated on an assessment day being queued, and the only moment that can be known is when
    # somebody looks at Today on or after the due date. It is a no-op on every render but the
    # first one past that date (D-226). ``card_due`` itself stays a pure read.
    library = library_bundle()
    if library is not None and ensure_retest_queued(db, view.primary.profile, library, load_settings(db)) is not None:
        # The retest went in ahead of what was just resolved, so resolve again: the card and the
        # session rendered underneath it have to be the same day (D-218b).
        view = resolve_today(db, profile)
    context = _today_context(
        request, view, confirming=confirm == "1", db=db, assessment_due=card_due(db, view.primary.profile)
    )
    return templates.TemplateResponse(request, "today.html", context)


def _load_view(db: Session, session_id: str) -> SessionView | None:
    record = db.get(SessionRecord, session_id)
    return view_for_session(db, record) if record is not None else None


def _writable_view(request: Request, db: Session, session_id: str, profile_key: str) -> SessionView | Response:
    """The session this write is for, or the response that refuses the write.

    Refusing sends the browser to the summary rather than 404ing: the user is looking at a page
    for a session they already finished, and the summary is where they meant to be.
    """
    view = _load_view(db, session_id)
    if view is None:
        return _error_page(request, f"no session {session_id!r}", 404)
    if is_finished(view.record):
        return _to_done(request, session_id, profile_key)
    return view


def _row_response(
    request: Request,
    db: Session,
    view: SessionView,
    position: int,
    profile_key: str,
    *,
    expanded: bool = False,
    notice: str | None = None,
    confirming: bool = False,
) -> Response:
    """The row partial, plus the footer out of band so the felt block can appear on a tick.

    ``confirming`` rides in on the row post (``hx-include="#confirm-state"``): re-rendering the
    footer without it closed the felt strip under the user's thumb halfway through "Done anyway".
    """
    row = view.row(position)
    if row is None:
        return _error_page(request, f"row {position} is not on this session", 404)
    unit = display_unit(load_settings(db))
    name = "partials/row_adjust.html" if expanded else "partials/row.html"
    body = templates.get_template(name).render(
        request=request, view=view, row=row, profile_key=profile_key, unit=unit, expanded=expanded, notice=notice
    )
    today = resolve_today(db, profile_key)
    context = _today_context(request, today, confirming=confirming)
    footer = templates.get_template("partials/felt.html").render(**context, oob=True)
    return HTMLResponse(body + footer + _exits(context, today))


def _exits(context: dict[str, Any], today: TodayView) -> str:
    """The youth column exits, swapped out of band alongside the footer.

    They live in their own column rather than the footer, so a tick that only replaces
    ``#done-footer`` would leave the son's "Good enough - done!" showing yesterday's answer.
    """
    if not today.together:
        return ""
    template = templates.get_template("partials/exit.html")
    return "".join(template.render(**context, item=item, oob=True) for item in today.sessions if item.is_youth)


@router.post("/today/{session_id}/rows/{position}/tick", response_class=HTMLResponse)
def tick_row(
    request: Request,
    session_id: str,
    position: int,
    db: Annotated[Session, Depends(get_session)],
    profile: Annotated[str | None, Query()] = None,
    done: Annotated[str | None, Form()] = None,
    confirm: Annotated[str | None, Form()] = None,
) -> Response:
    profile_key = _safe_key(profile)
    view = _writable_view(request, db, session_id, profile_key)
    if isinstance(view, Response):
        return view
    try:
        # The posted checkbox is the truth, with JavaScript or without it: a form always sends the
        # state the user is looking at, and toggling on the server would fight a replayed POST.
        set_done(db, view, position, done is not None)
    except TickError as exc:
        return _error_page(request, str(exc), 404)
    if not _is_htmx(request):
        return _back(profile_key, confirm == "1")
    # No reload: the write went through the records this view already holds, so they are current.
    return _row_response(request, db, view, position, profile_key, confirming=confirm == "1")


@router.post("/today/{session_id}/rows/{position}/adjust", response_class=HTMLResponse)
def adjust_row(
    request: Request,
    session_id: str,
    position: int,
    db: Annotated[Session, Depends(get_session)],
    profile: Annotated[str | None, Query()] = None,
    expand: Annotated[str | None, Form()] = None,
    reps: Annotated[int | None, Form()] = None,
    load: Annotated[int | None, Form()] = None,
    confirm: Annotated[str | None, Form()] = None,
) -> Response:
    profile_key = _safe_key(profile)
    view = _writable_view(request, db, session_id, profile_key)
    if isinstance(view, Response):
        return view
    notice: str | None = None
    expanded = expand != "0"
    try:
        if reps:
            notice = adjust(db, view, position, "reps", reps, load_settings(db)).notice
        elif load:
            notice = adjust(db, view, position, "load", load, load_settings(db)).notice
    except TickError as exc:
        # Same split the JSON API makes: a row that is not there is a 404, a row that cannot be
        # adjusted is a 400.
        return _error_page(request, str(exc), 404 if "is not on this session" in str(exc) else 400)
    if not _is_htmx(request):
        return _back(profile_key, confirm == "1")
    return _row_response(
        request, db, view, position, profile_key, expanded=expanded, notice=notice, confirming=confirm == "1"
    )


@router.post("/today/{session_id}/felt", response_class=HTMLResponse)
def felt_route(
    request: Request,
    session_id: str,
    db: Annotated[Session, Depends(get_session)],
    profile: Annotated[str | None, Query()] = None,
    felt: Annotated[str | None, Form()] = None,
) -> Response:
    profile_key = _safe_key(profile)
    view = _writable_view(request, db, session_id, profile_key)
    if isinstance(view, Response):
        return view
    set_felt(db, view.record, felt)
    if not _is_htmx(request):
        return _back(profile_key, confirming=True)
    # The strip stays open after an answer: the user has to see which of the three they chose,
    # and it is the same control whether it appeared on a full checklist or on "Done anyway".
    return _footer_only(request, db, profile_key, confirming=True)


@router.post("/today/{session_id}/done")
def done_route(
    request: Request,
    session_id: str,
    db: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    profile: Annotated[str | None, Query()] = None,
    confirm: Annotated[str | None, Form()] = None,
    felt: Annotated[str | None, Form()] = None,
    scope: Annotated[str | None, Form()] = None,
) -> Response:
    profile_key = _safe_key(profile)
    view = _writable_view(request, db, session_id, profile_key)
    if isinstance(view, Response):
        return view

    # D-084: the youth exit finalises one checklist, so it is judged against one checklist.
    solo = scope == SOLO
    if confirm != "1" and not (view.all_ticked if solo else _group_all_ticked(db, view)):
        # "Done anyway": the same three-way appears above the button, and a second tap finishes.
        if not _is_htmx(request):
            return RedirectResponse(f"/today?profile={profile_key}&confirm=1", status_code=SEE_OTHER)
        return _footer_only(request, db, profile_key, confirming=True)

    finish(db, view, felt, group=not solo, config=settings)
    return _to_done(request, session_id, profile_key)


def _group_all_ticked(db: Session, view: SessionView) -> bool:
    """Whether every checklist this one Done would finalise is fully ticked.

    Together renders a single shared button over two sessions, so a confirm that read only the
    session whose id is on the button would finish the son's untouched checklist on the parent's
    first tap. The footer template already counts both (``_today_context``); this is the route
    agreeing with it.
    """
    members = (view_for_session(db, member) for member in group_members(db, view.record))
    return all(item.all_ticked for item in members if item is not None)


def _to_done(request: Request, session_id: str, profile_key: str) -> Response:
    """Send the browser to the summary, whichever way it asked.

    A 303 answering an HTMX POST is followed by HTMX itself and swapped into the target, so the
    page never moves. ``HX-Redirect`` is how a partial asks for a real navigation.
    """
    target = f"/done/{session_id}?profile={profile_key}"
    if _is_htmx(request):
        return Response(status_code=204, headers={"HX-Redirect": target})
    return RedirectResponse(target, status_code=SEE_OTHER)


@router.get("/done/{session_id}", response_class=HTMLResponse)
def done_page(
    request: Request,
    session_id: str,
    db: Annotated[Session, Depends(get_session)],
    profile: Annotated[str | None, Query()] = None,
    notice: Annotated[str | None, Query()] = None,
) -> Response:
    record = db.get(SessionRecord, session_id)
    view = view_for_session(db, record) if record is not None else None
    if view is None:
        return _error_page(request, f"no session {session_id!r}", 404)
    # Only what is actually finished: a youth exit leaves the parent's checklist open, and
    # summarising an unfinished session would report a session nobody has ended.
    members = [item for item in group_members(db, view.record) if is_finished(item)] or [view.record]
    summaries = [summarise(item) for item in (view_for_session(db, m) for m in members) if item is not None]
    summaries.sort(key=lambda item: item.session_id != view.record.id)
    context = {
        "request": request,
        "summaries": summaries,
        "profile_key": _safe_key(profile),
        "view": None,
        # A Done taken offline reaches this page from the queue before the server has the session.
        "pending": not is_finished(view.record),
        # The server-side write-back, which is a different thing from the phone's own outbox.
        # ``?notice=`` is how a refused plain-form Retry gets its reason across a redirect: a
        # code looked up in a fixed table, never a sentence off the URL, so an unknown value
        # renders nothing at all rather than whatever someone put there.
        "sync": line_for_session(db, view.record.id).with_notice(REFUSALS.get(notice or "")),
        "session_id": view.record.id,
    }
    return templates.TemplateResponse(request, "done.html", context)


@router.post("/done/{session_id}/retry", response_class=HTMLResponse)
async def retry_sync(
    request: Request,
    session_id: str,
    db: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    profile: Annotated[str | None, Query()] = None,
) -> Response:
    """The Retry button. Drains this one job, ignoring the backoff, and re-renders the line.

    A web route rather than ``POST /api/sync/retry`` for one reason: the button swaps HTML into
    the page, and the JSON endpoint answers the envelope. Both call the same ``drain``, and this
    one answers a plain form POST with a redirect so it works with JavaScript off.

    An unknown session id is a 404, the same answer ``GET /done/{session_id}`` gives it. Draining
    a queue that has never heard of the id and re-rendering "Stored locally." would tell a person
    their session is safe on a device that does not have it.
    """
    if db.get(SessionRecord, session_id) is None:
        return _error_page(request, f"no session {session_id!r}", 404)
    profile_key = _safe_key(profile)
    refusal: str | None = None
    try:
        await drain(db, VitalForgeClient(settings), force_session_id=session_id)
    except ForceRefused as declined:
        # The queue declined: this was tried a moment ago, or VitalForge rejected the session
        # itself and would reject it again. Both are answers, not errors — the page re-renders
        # with the reason rather than 500ing on a button somebody tapped twice.
        refusal = declined.code
    if _is_htmx(request):
        return HTMLResponse(_sync_line(request, db, session_id, profile_key, refusal))
    target = f"/done/{session_id}?profile={profile_key}"
    return RedirectResponse(f"{target}&notice={refusal}" if refusal else target, status_code=SEE_OTHER)


def _sync_line(request: Request, db: Session, session_id: str, profile_key: str, refusal: str | None = None) -> str:
    line = line_for_session(db, session_id).with_notice(REFUSALS.get(refusal or ""))
    return templates.get_template("partials/sync.html").render(
        request=request, sync=line, session_id=session_id, profile_key=profile_key
    )


def _footer_only(request: Request, db: Session, profile_key: str, confirming: bool = False) -> Response:
    today = resolve_today(db, profile_key)
    body = templates.get_template("partials/felt.html").render(
        **_today_context(request, today, confirming=confirming), oob=False
    )
    return HTMLResponse(body)


def _safe_key(raw: str | None) -> str:
    """A bad ``?profile=`` on a write must not 500 the write; it falls back to the parent tab."""
    try:
        return normalise_profile_key(raw)
    except TodayError:
        return "me"
