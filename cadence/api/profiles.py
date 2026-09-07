"""``GET|PUT /api/profiles/{id}`` and ``PUT /api/profiles/{id}/kind``.

``kind`` is not writable through the ordinary profile PUT. It is the authority for the youth rules
(D-066), so a profile that could be flipped to ``adult`` by naming a field alongside an age is a
youth-rule bypass with an HTTP verb in front of it.

It has its own endpoint instead, which asks for the change in as many words and will not act
without ``confirm``. That endpoint goes both ways (D-099): whichever direction removes protections
must be as reversible as the one that restores them, or a mistyped age becomes permanent.
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

router = APIRouter(prefix="/api", tags=["profiles"])

NOT_FOUND = 404
UNPROCESSABLE = 422
UNAVAILABLE = 503


@router.get("/profiles/{profile_id}")
def read_profile(profile_id: str, db: Annotated[Session, Depends(get_session)]) -> Any:
    profile = services.get_profile(db, profile_id)
    if profile is None:
        return JSONResponse(status_code=NOT_FOUND, content=err(f"there is no profile {profile_id!r}"))
    return ok(services.profile_payload(profile))


@router.put("/profiles/{profile_id}")
def write_profile(
    profile_id: str,
    db: Annotated[Session, Depends(get_session)],
    patch: Annotated[dict[str, Any], Body()],
) -> Any:
    try:
        result = services.update_profile(db, profile_id, patch, library_bundle())
    except ValidationFailed as exc:
        status = NOT_FOUND if any(item.field == "id" for item in exc.errors) else UNPROCESSABLE
        return JSONResponse(status_code=status, content=err(str(exc), fields=exc.as_dict()))
    except RebuildUnavailable as exc:
        return JSONResponse(status_code=UNAVAILABLE, content=err(str(exc)))
    return ok(services.profile_payload(result.profile), rebuilt=list(result.rebuilt))


@router.put("/profiles/{profile_id}/kind")
def write_profile_kind(
    profile_id: str,
    db: Annotated[Session, Depends(get_session)],
    body: Annotated[dict[str, Any], Body()],
) -> Any:
    """Move a profile between the youth and adult rule sets. ``{"kind": ..., "confirm": true}``.

    Separate from the profile PUT so that changing which rules protect a child is never something
    that happens *while* doing something else, and never something a patch can do by accident.
    """
    try:
        result = services.set_profile_kind(
            db, profile_id, str(body.get("kind", "")), bool(body.get("confirm")), library_bundle()
        )
    except ValidationFailed as exc:
        status = NOT_FOUND if any(item.field == "id" for item in exc.errors) else UNPROCESSABLE
        return JSONResponse(status_code=status, content=err(str(exc), fields=exc.as_dict()))
    except RebuildUnavailable as exc:
        return JSONResponse(status_code=UNAVAILABLE, content=err(str(exc)))
    return ok(services.profile_payload(result.profile), rebuilt=list(result.rebuilt))
