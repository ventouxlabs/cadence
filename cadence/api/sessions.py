"""The two write endpoints the offline queue replays into.

Both are idempotent. ``POST .../rows/{position}`` writes only the fields present in the body and
drops an operation older than the row's stored ``done_at``; ``POST .../done`` on a finished session
returns the same summary it returned the first time and changes nothing.

Errors return the envelope with ``error`` set, never FastAPI's ``{"detail": ...}``: the offline
client and the acceptance tests both read one shape.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictBool, StrictFloat, StrictInt
from sqlmodel import Session

from cadence.api.envelope import err, ok
from cadence.api.today import row_payload
from cadence.db import get_session
from cadence.seance.catalog import BandRulesUnavailable, load_settings
from cadence.seance.done import finish, is_finished, summarise
from cadence.seance.tables import SessionRecord
from cadence.seance.ticks import TickError, apply_patch
from cadence.seance.today import SessionView, view_for_session

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


class RowPatch(BaseModel):
    """Every field optional; only the ones sent are written."""

    model_config = ConfigDict(extra="forbid")

    done: StrictBool | None = None
    reps_done: StrictInt | None = None
    load_done_kg: StrictFloat | StrictInt | None = None
    ts: str | None = None


class DoneBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    felt: str | None = None
    ts: str | None = None
    # "solo" finalises only this session, which is what the youth exit queues (D-084).
    scope: Literal["solo", "group"] | None = None


def _load(db: Session, session_id: str) -> SessionView | JSONResponse:
    record = db.get(SessionRecord, session_id)
    if record is None:
        return JSONResponse(status_code=404, content=err(f"no session {session_id!r}"))
    view = view_for_session(db, record)
    if view is None:
        return JSONResponse(status_code=404, content=err(f"session {session_id!r} has no profile or plan"))
    return view


@router.post("/{session_id}/rows/{position}")
def write_row(
    session_id: str,
    position: int,
    patch: RowPatch,
    db: Annotated[Session, Depends(get_session)],
) -> Any:
    view = _load(db, session_id)
    if isinstance(view, JSONResponse):
        return view
    if is_finished(view.record):
        return JSONResponse(status_code=409, content=err("this session is already finished"))
    try:
        result = apply_patch(db, view, position, patch.model_dump(exclude_unset=True), load_settings(db))
    except BandRulesUnavailable as exc:
        # 503, not 400: the request is fine, the limit is missing. A retry once the library is
        # fixed is the right move, which is exactly what the status says.
        return JSONResponse(status_code=503, content=err(str(exc)))
    except TickError as exc:
        status = 404 if "is not on this session" in str(exc) else 400
        return JSONResponse(status_code=status, content=err(str(exc)))
    return ok(row_payload(result.row), changed=result.changed, notice=result.notice)


@router.post("/{session_id}/done")
def write_done(
    session_id: str,
    body: DoneBody,
    db: Annotated[Session, Depends(get_session)],
) -> Any:
    view = _load(db, session_id)
    if isinstance(view, JSONResponse):
        return view
    if is_finished(view.record):
        # Already done: the replay of a queued Done, or a double tap. Same body, nothing moved.
        return ok(summarise(view).as_dict(), replayed=True)
    summaries = finish(db, view, body.felt, body.ts, group=body.scope != "solo")
    payload = summaries[0].as_dict()
    if len(summaries) > 1:
        payload["group"] = [item.as_dict() for item in summaries]
    return ok(payload, replayed=False)
