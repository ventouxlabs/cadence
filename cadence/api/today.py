"""``GET /api/today`` - the payload the service worker caches and the offline page replays from.

No network call and no clock beyond ``generated_at``: this is the same resolution ``GET /today``
does, serialised (architecture section 5).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlmodel import Session

from cadence.api.envelope import err, ok
from cadence.db import get_session
from cadence.seance.catalog import display_unit
from cadence.seance.status import good_enough_after
from cadence.seance.today import RowView, SessionView, TodayError, TodayView, resolve_today

router = APIRouter(prefix="/api", tags=["today"])


def row_payload(row: RowView) -> dict[str, Any]:
    """One checklist row, planned values plus whatever the user has changed."""
    spec = row.spec
    return {
        "position": row.position,
        "exercise_id": spec.get("exercise_id"),
        "name": spec.get("name"),
        "role": spec.get("role"),
        "sets": spec.get("sets"),
        "reps": spec.get("reps"),
        "seconds": spec.get("seconds"),
        "meters": spec.get("meters"),
        "per_side": bool(spec.get("per_side")),
        "rest_s": spec.get("rest_s"),
        "load_kg": spec.get("load_kg"),
        "measure": row.measure,
        "done": bool(row.record.done),
        "done_at": row.record.done_at,
        "reps_done": row.record.reps_done,
        "load_done_kg": row.record.load_done_kg,
        "adjustable": row.adjustable,
        "timed": row.timed,
        "cue": spec.get("cue"),
        "is_prelude": row.is_prelude,
        "is_challenge": bool(spec.get("is_challenge")),
        "notes": list(spec.get("notes") or []),
    }


def session_payload(view: SessionView, unit: str) -> dict[str, Any]:
    return {
        "session_id": view.record.id,
        "profile": view.profile.id,
        "display_name": view.profile.display_name,
        "day_type": view.planned.day_type,
        "week": view.planned.week,
        "display_unit": unit,
        "good_enough_after": good_enough_after(view.rules),
        "together_group_id": view.record.together_group_id,
        "started_at": view.record.started_at,
        "finished_at": view.record.finished_at,
        "felt": view.record.felt,
        "rows": [row_payload(row) for row in view.rows],
    }


def today_payload(view: TodayView) -> dict[str, Any]:
    """One session's payload, with the Together partner alongside it when there is one."""
    unit = display_unit(view.settings)
    primary = session_payload(view.primary, unit)
    if view.together:
        primary["sessions"] = [session_payload(item, unit) for item in view.sessions]
    if view.missing:
        primary["missing"] = list(view.missing)
    return primary


@router.get("/today")
def api_today(
    db: Annotated[Session, Depends(get_session)],
    profile: Annotated[str | None, Query()] = None,
) -> Any:
    try:
        view = resolve_today(db, profile)
    except TodayError as exc:
        return JSONResponse(status_code=404, content=err(str(exc)))
    return ok(today_payload(view), generated_at=datetime.now(UTC).isoformat())
