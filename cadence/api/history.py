"""``GET /api/sessions`` and ``GET /api/scorecard`` — history as JSON.

Separate from ``cadence/api/sessions.py``, which owns the two write endpoints the offline queue
replays into: same prefix, different methods, and keeping the read here leaves that module about
one thing.

``?profile=together`` follows ``GET /api/today``: the primary profile's answer, with both under a
key alongside it (D-103). A together session is one ``session`` row per profile, so it appears once
on each list rather than twice on either.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlmodel import Session

from cadence.api.envelope import err, ok
from cadence.db import get_session
from cadence.historique.page import build_page, resolve_profiles
from cadence.historique.queries import DEFAULT_LIMIT, LimitOutOfRange, clamp_limit, recent_sessions
from cadence.seance.today import TodayError

router = APIRouter(prefix="/api", tags=["history"])


def _refuse(exc: Exception, status: int) -> JSONResponse:
    return JSONResponse(status_code=status, content=err(str(exc)))


@router.get("/sessions")
def api_sessions(
    db: Annotated[Session, Depends(get_session)],
    profile: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query()] = DEFAULT_LIMIT,
) -> Any:
    """The profile's finished sessions, newest first. ``limit`` is 1..200; anything else is 422."""
    try:
        size = clamp_limit(limit)
        _key, profiles = resolve_profiles(db, profile)
    except LimitOutOfRange as exc:
        return _refuse(exc, 422)
    except TodayError as exc:
        return _refuse(exc, 404)
    # Each profile's list already arrives newest-first from SQL; merging two of them for the
    # Together tab is the only reason to sort again, and it sorts on the instant rather than on
    # the local date, which ties every session finished on the same day.
    summaries = [summary for item in profiles for summary in recent_sessions(db, item.id, size)]
    summaries.sort(key=lambda summary: summary.sort_key, reverse=True)
    return ok(
        [summary.as_dict() for summary in summaries],
        count=len(summaries),
        limit=size,
        profiles=[item.id for item in profiles],
    )


@router.get("/scorecard")
def api_scorecard(
    db: Annotated[Session, Depends(get_session)],
    profile: Annotated[str | None, Query()] = None,
) -> Any:
    """This week against the plan, the streak, and — for an adult profile only — the trend."""
    try:
        view = build_page(db, profile, limit=1)
    except TodayError as exc:
        return _refuse(exc, 404)
    payload = view.primary.as_dict()
    if view.together:
        payload["profiles"] = [column.as_dict() for column in view.columns]
    return ok(payload)
