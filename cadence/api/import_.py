"""``POST /api/import`` - a pasted or uploaded workout, validated and stored.

The handler takes a raw ``Request`` rather than a declared body model on purpose. A Pydantic body
would be parsed by FastAPI *before* this function runs, so the size cap would be checked after the
whole payload had already been read and built into objects - which is exactly the property PRP-08
risk 3 asks for and exactly the sort of regression that keeps passing its own test.

Nothing here opens a file. A multipart upload is read into memory under a hard cap and handed on
as a ``str``; the filename is never read, never logged and never used for anything (D-010).
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlmodel import Session

from cadence.api.envelope import err, ok
from cadence.bibliotheque import ingestion, untrusted
from cadence.bibliotheque.import_service import SOURCE_IMPORT, DuplicateId, store
from cadence.bibliotheque.untrusted import MAX_BODY_BYTES, Finding
from cadence.db import get_session
from cadence.web.guards import enforce_declared_size, require_same_origin

router = APIRouter(
    prefix="/api",
    tags=["import"],
    dependencies=[Depends(require_same_origin), Depends(enforce_declared_size)],
)
logger = logging.getLogger(__name__)

UNPROCESSABLE = 422
MULTIPART = "multipart/form-data"


class BodyTooLarge(Exception):
    """The request body passed the cap. Raised before anything parses it."""


def failure(*findings: Finding, error: str = untrusted.VALIDATION_FAILED, **meta: Any) -> JSONResponse:
    """Every failure of this endpoint: 422, the envelope, and the full list of findings.

    One status for every rejection so a client has one branch. A body that is too large is not a
    different kind of answer to a body that is malformed - both are "this did not import", and the
    code in the list is what says which (D-171).
    """
    return JSONResponse(
        status_code=UNPROCESSABLE,
        content=err(error, data={"errors": [item.as_dict() for item in findings]}, **meta),
    )


async def read_capped(request: Request) -> bytes:
    """The body, refused before it is read whole if it is or claims to be too big."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise BodyTooLarge
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise BodyTooLarge
        chunks.append(chunk)
    return b"".join(chunks)


def _decode(raw: bytes) -> str | None:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


async def _from_multipart(request: Request) -> tuple[str | None, str, Finding | None]:
    """The uploaded text and the target profile. The filename is read by nobody."""
    form = await request.form(max_part_size=MAX_BODY_BYTES)
    try:
        upload = form.get("file")
        profile_raw = form.get("profile")
        if upload is None or isinstance(upload, str):
            return None, ingestion.DEFAULT_PROFILE, Finding(untrusted.PARSE_ERROR, "no file was attached")
        raw = await upload.read()
        if len(raw) > MAX_BODY_BYTES:
            raise BodyTooLarge
        text = _decode(raw)
        if text is None:
            return None, ingestion.DEFAULT_PROFILE, Finding(untrusted.PARSE_ERROR, "the file is not UTF-8 text")
        return text, str(profile_raw) if profile_raw is not None else ingestion.DEFAULT_PROFILE, None
    finally:
        await form.close()


def _from_json(raw: bytes) -> tuple[str | None, str, Finding | None]:
    text = _decode(raw)
    if text is None:
        return None, ingestion.DEFAULT_PROFILE, Finding(untrusted.PARSE_ERROR, "the body is not UTF-8 text")
    # Before ``json.loads``, which is recursive: ten thousand open brackets is a RecursionError
    # from inside the standard library, and that is a 500 on a request that should be a 422.
    if deep := untrusted.check_json_depth(text):
        return None, ingestion.DEFAULT_PROFILE, deep[0]
    try:
        payload = json.loads(text)
    except (ValueError, RecursionError):
        return None, ingestion.DEFAULT_PROFILE, Finding(untrusted.PARSE_ERROR, "the request body is not JSON")
    if not isinstance(payload, dict):
        return None, ingestion.DEFAULT_PROFILE, Finding(untrusted.PARSE_ERROR, "the request body is not an object")
    document = payload.get("text")
    if not isinstance(document, str):
        return None, ingestion.DEFAULT_PROFILE, Finding(untrusted.PARSE_ERROR, "send the workout as a 'text' string")
    return document, payload.get("profile", ingestion.DEFAULT_PROFILE), None


@router.post("/import")
async def import_workout(request: Request, db: Annotated[Session, Depends(get_session)]) -> Any:
    """Validate a pasted or uploaded workout against one profile, and store it if it passes."""
    try:
        if request.headers.get("content-type", "").startswith(MULTIPART):
            declared = request.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
                raise BodyTooLarge
            text, profile_raw, problem = await _from_multipart(request)
        else:
            text, profile_raw, problem = _from_json(await read_capped(request))
    except BodyTooLarge:
        return failure(
            Finding(untrusted.TOO_LARGE, f"a workout must be under {MAX_BODY_BYTES // 1024} KB"),
            error=untrusted.TOO_LARGE,
        )
    if problem is not None or text is None:
        return failure(problem or Finding(untrusted.PARSE_ERROR, "there was nothing to import"))

    try:
        profile_id = ingestion.normalise_profile_id(profile_raw)
        context = ingestion.build_context(db, profile_id)
    except ingestion.UnknownProfile as exc:
        return failure(Finding(untrusted.PARSE_ERROR, str(exc), "profile"))

    prepared = ingestion.run(text, context)
    if not prepared.ok:
        return failure(*prepared.errors, ignored_keys=list(prepared.ignored_keys))

    try:
        workout_id = store(db, prepared, source=SOURCE_IMPORT)
    except DuplicateId as exc:
        return failure(Finding(untrusted.ID_COLLISION, str(exc), "id"))
    return ok(
        ingestion.stored_payload(prepared, workout_id, context),
        source=SOURCE_IMPORT,
        ignored_keys=list(prepared.ignored_keys),
    )
