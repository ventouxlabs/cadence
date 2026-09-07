"""The two write endpoints the offline queue replays into.

Both are idempotent. ``POST .../rows/{position}`` writes only the fields present in the body and
drops an operation older than the row's stored ``done_at``; ``POST .../done`` on a finished session
returns the same summary it returned the first time and changes nothing.

Errors return the envelope with ``error`` set, never FastAPI's ``{"detail": ...}``: the offline
client and the acceptance tests both read one shape.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictBool, StrictFloat, StrictInt, field_validator
from sqlmodel import Session

from cadence.api.envelope import err, ok
from cadence.api.today import row_payload
from cadence.config import Settings, get_settings
from cadence.db import get_session
from cadence.seance.catalog import BandRulesUnavailable, load_settings
from cadence.seance.done import finish, is_finished, summarise
from cadence.seance.tables import SessionRecord
from cadence.seance.ticks import TickError, apply_patch
from cadence.seance.today import SessionView, view_for_session
from cadence.vitalforge.writeback import sync_status

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


# A client timestamp Cadence will accept. ``ts`` comes from the phone so an offline tick can
# replay with the time it actually happened, which means a wrong device clock is ordinary input,
# not an attack. Rejecting it here is the cheap half of the fix: the payload builder clamps as
# well (D-138a), because a row already stored cannot be rejected retrospectively.
TS_FUTURE_TOLERANCE = timedelta(seconds=60)
# Before Cadence existed. A ts this old is a broken clock, not a late replay.
TS_FLOOR = datetime(2020, 1, 1, tzinfo=UTC)


def _valid_ts(value: str | None) -> str | None:
    """An ISO timestamp with an offset, in the plausible past. Anything else is a 422.

    Naive is refused rather than assumed to be UTC: the three plausible readings — the phone's
    zone, the server's, and UTC — differ by hours, and guessing wrong silently misfiles a Garmin
    activity by that much. A client that cannot say which zone it meant has not said a time.
    """
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError("ts must be an ISO-8601 timestamp") from None
    if parsed.tzinfo is None:
        raise ValueError("ts must carry a UTC offset")
    if parsed > datetime.now(UTC) + TS_FUTURE_TOLERANCE:
        raise ValueError("ts is in the future; check the device clock")
    if parsed < TS_FLOOR:
        raise ValueError("ts is implausibly old; check the device clock")
    return value


class RowPatch(BaseModel):
    """Every field optional; only the ones sent are written."""

    model_config = ConfigDict(extra="forbid")

    done: StrictBool | None = None
    reps_done: StrictInt | None = None
    load_done_kg: StrictFloat | StrictInt | None = None
    ts: str | None = None

    _check_ts = field_validator("ts")(staticmethod(_valid_ts))


class DoneBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    felt: str | None = None
    ts: str | None = None
    # "solo" finalises only this session, which is what the youth exit queues (D-084).
    scope: Literal["solo", "group"] | None = None

    _check_ts = field_validator("ts")(staticmethod(_valid_ts))


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
    settings: Annotated[Settings, Depends(get_settings)],
) -> Any:
    view = _load(db, session_id)
    if isinstance(view, JSONResponse):
        return view
    if is_finished(view.record):
        # Already done: the replay of a queued Done, or a double tap. Same body, nothing moved -
        # including the write-back, whose job is get-or-create on this same session id.
        return ok(summarise(view, sync_status(db, session_id)).as_dict(), replayed=True)
    summaries = finish(db, view, body.felt, body.ts, group=body.scope != "solo", config=settings)
    payload = summaries[0].as_dict()
    if len(summaries) > 1:
        payload["group"] = [item.as_dict() for item in summaries]
    return ok(payload, replayed=False)
