"""``GET|PUT /api/settings`` (``docs/architecture.md`` section 4).

The same validation the HTML form runs, answered in the envelope. A ``PUT`` takes a partial patch:
keys it does not name are left where they are, and a key this build has never heard of is a 422
naming it rather than a silently dropped field.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends
from fastapi.responses import JSONResponse
from sqlmodel import Session

from cadence.api.envelope import err, ok
from cadence.db import get_session
from cadence.profils import services
from cadence.profils.services import RebuildUnavailable
from cadence.profils.validation import ValidationFailed
from cadence.seance.catalog import library_bundle

router = APIRouter(prefix="/api", tags=["settings"])

UNPROCESSABLE = 422
UNAVAILABLE = 503

# The PRP's data model is explicit: `setup_complete` is "set true only by a successful POST
# /setup". It is a legal setting key, so it round-trips through GET and through the form that
# completes setup - but writing it here would skip the one screen this PRP exists to build, and
# with it the son's age, in a single request.
SETUP_ONLY: frozenset[str] = frozenset({"setup_complete"})


@router.get("/settings")
def read_settings(db: Annotated[Session, Depends(get_session)]) -> dict[str, Any]:
    return ok(services.settings_payload(services.get_settings(db)))


@router.put("/settings")
def write_settings(
    db: Annotated[Session, Depends(get_session)],
    patch: Annotated[dict[str, Any], Body()],
) -> Any:
    """A partial patch. Answers with the full settings, plus what a rebuild touched."""
    blocked = sorted(set(patch) & SETUP_ONLY)
    if blocked:
        names = ", ".join(blocked)
        message = f"{names} is set by completing setup, not by this endpoint"
        return JSONResponse(status_code=UNPROCESSABLE, content=err(message, fields={names: message}))
    try:
        result = services.update_settings(db, patch, library_bundle())
    except ValidationFailed as exc:
        return JSONResponse(status_code=UNPROCESSABLE, content=err(str(exc), fields=exc.as_dict()))
    except RebuildUnavailable as exc:
        # Not a 200 with an empty `rebuilt`: the caller would read that as "saved, nothing needed
        # re-planning", which is the opposite of what happened.
        return JSONResponse(status_code=UNAVAILABLE, content=err(str(exc)))
    payload = services.settings_payload(result.settings)
    return ok(payload, rebuilt=list(result.rebuilt))
