"""``GET /api/metrics?profile=`` - the cached VitalForge numbers and the readiness nudge.

**This route makes no network call.** Today is the hot path (``docs/architecture.md`` section 5),
this is the endpoint its nudge comes from, and a refresh hidden here because the cache looked
empty in dev is exactly how a screen ends up waiting five seconds on a health service (risk 12).
The periodic task in ``cadence/main.py`` is the only thing that fills the cache.

A youth profile's body composition is absent, not null: ``metrics.py`` never fetches it and never
stores it, so no template can render it by accident.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlmodel import Session

from cadence.api.envelope import err, ok
from cadence.api.gates import refuse_hidden
from cadence.db import get_session
from cadence.profils.tables import Profile
from cadence.vitalforge.metrics import ProfileMetrics, read_cached

router = APIRouter(prefix="/api", tags=["metrics"])


@router.get("/metrics")
def metrics(
    db: Annotated[Session, Depends(get_session)],
    profile: Annotated[str, Query()] = "me",
) -> Any:
    """The cached payload, or an honest empty one flagged stale."""
    # The same sentence line 41 gives a profile that is not there: hidden and absent must not be
    # tellable apart from outside (D-273).
    hidden = refuse_hidden(db, profile, f"no profile {profile!r}")
    if hidden is not None:
        return hidden
    record = db.get(Profile, profile.strip().lower())
    if record is None:
        return JSONResponse(status_code=404, content=err(f"no profile {profile!r}"))
    cached = read_cached(db, record)
    if cached is None:
        # Nothing has ever been pulled. Every metric absent, and ``stale`` says why the screen is
        # empty rather than pretending the numbers are current.
        cached = ProfileMetrics(profile_id=record.id, fetched_at=datetime.now(UTC), stale=True)
        return ok(cached.as_data(), source="empty")
    return ok(cached.as_data(), source="cache")
