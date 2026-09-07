"""``POST /api/sync/retry`` - drain the write-back queue now.

The periodic task runs every five minutes; this is the same drain on demand, and the Done
screen's Retry button is its one user-facing caller. With a ``session_id`` it ignores the backoff
entirely: an explicit human retry beats a schedule, including a job the schedule has given up on.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlmodel import Session

from cadence.api.envelope import err, ok
from cadence.config import Settings, get_settings
from cadence.db import get_session
from cadence.vitalforge.client import VitalForgeClient
from cadence.vitalforge.sync import ForceRefused, drain

router = APIRouter(prefix="/api/sync", tags=["sync"])

# 429 for both refusals. "Too soon" is literally a rate limit, and "VitalForge rejected the
# payload" is the queue declining to make a request it knows will fail the same way again.
TOO_MANY_REQUESTS = 429


class RetryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str | None = None


@router.post("/retry")
async def retry(
    db: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    body: RetryBody | None = None,
) -> Any:
    """``{"drained": n, "sent": n, "failed": n, "skipped": n}``, or a 429 with the reason.

    429 rather than a silent no-op: a person who taps Retry twice deserves to be told the second
    tap did nothing and why. The cooldown is a minute, which is also the shortest the automatic
    backoff ever waits, so a held button cannot outpace the schedule it is beating.
    """
    target = body.session_id if body is not None else None
    try:
        report = await drain(db, VitalForgeClient(settings), force_session_id=target)
    except ForceRefused as refusal:
        return JSONResponse(status_code=TOO_MANY_REQUESTS, content=err(str(refusal)))
    return ok(report.as_dict(), forced=bool(target))
