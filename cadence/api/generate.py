"""``POST /api/generate`` and ``POST /api/generate/accept``.

Generate returns a preview and stores nothing. Accept re-runs every gate on the bytes the client
posted, and stores only if they pass. The two endpoints share the import pipeline rather than a
cache, which is the whole of PRP-08 risk 6: there is no server-side preview to trust.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlmodel import Session

from cadence.api.envelope import err, ok
from cadence.api.import_ import UNPROCESSABLE, BodyTooLarge, failure, read_capped
from cadence.bibliotheque import ingestion, untrusted
from cadence.bibliotheque.import_service import SOURCE_GENERATED, DuplicateId, store
from cadence.bibliotheque.untrusted import MAX_BODY_BYTES, Finding
from cadence.config import Settings, get_settings
from cadence.db import get_session
from cadence.ia import client
from cadence.ia import generate as ia
from cadence.schema.enums import GoalType
from cadence.validateur import codes as vcodes
from cadence.web.guards import enforce_declared_size, require_same_origin
from cadence.web.routers import settings_ai

router = APIRouter(
    prefix="/api",
    tags=["generate"],
    dependencies=[Depends(require_same_origin), Depends(enforce_declared_size)],
)
logger = logging.getLogger(__name__)

UNAVAILABLE = 503


def _json_body(raw: bytes) -> tuple[dict[str, Any] | None, Finding | None]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, Finding(untrusted.PARSE_ERROR, "the request body is not JSON")
    # The bracket count runs before the recursive parser, never after it (Codex finding 9).
    if deep := untrusted.check_json_depth(text):
        return None, deep[0]
    try:
        payload = json.loads(text)
    except (ValueError, RecursionError):
        return None, Finding(untrusted.PARSE_ERROR, "the request body is not JSON")
    if not isinstance(payload, dict):
        return None, Finding(untrusted.PARSE_ERROR, "the request body is not an object")
    return payload, None


def _goal(raw: object) -> tuple[GoalType | None, Finding | None]:
    try:
        return GoalType(str(raw)), None
    except ValueError:
        allowed = ", ".join(item.value for item in GoalType)
        return None, Finding(untrusted.PARSE_ERROR, f"goal must be one of: {allowed}", "goal")


@router.post("/generate")
async def generate_workout(
    request: Request,
    db: Annotated[Session, Depends(get_session)],
    config: Annotated[Settings, Depends(get_settings)],
) -> Any:
    """Ask OmniRoute for a workout and answer with a validated preview. Stores nothing."""
    try:
        raw = await read_capped(request)
    except BodyTooLarge:
        return failure(Finding(untrusted.TOO_LARGE, "that request is too large"), error=untrusted.TOO_LARGE)
    payload, problem = _json_body(raw)
    if payload is None:
        return failure(problem or Finding(untrusted.PARSE_ERROR, "there was nothing to generate from"))

    if not config.omniroute_configured:
        finding = ia.unavailable_finding()
        return JSONResponse(
            status_code=UNAVAILABLE,
            content=err(untrusted.GENERATION_UNAVAILABLE, data={"errors": [finding.as_dict()]}),
        )

    goal, goal_problem = _goal(payload.get("goal"))
    if goal is None:
        return failure(goal_problem)  # type: ignore[arg-type]

    try:
        context = ingestion.build_context(db, ingestion.normalise_profile_id(payload.get("profile")))
        # Resolved against this profile's own active challenges, never taken as free text: the
        # gap is the one field a caller chooses that reaches the prompt (Codex finding 7).
        gap = settings_ai.resolve_gap(db, context.profile.id, payload.get("gap"))
    except ingestion.UnknownProfile as exc:
        return failure(Finding(untrusted.PARSE_ERROR, str(exc), "profile"))
    except settings_ai.UnknownGap:
        return failure(Finding(untrusted.PARSE_ERROR, "that is not one of this profile's challenges", "gap"))

    if not ia.goal_is_allowed(goal, profile_kind=context.target.profile_kind, rules=context.rules):
        return failure(
            Finding(
                vcodes.YOUTH_BANNED_GOAL,
                f"{context.profile.display_name} does not train for that; pick a goal about "
                "getting stronger, moving better or keeping the habit.",
                "goal",
            )
        )

    try:
        outcome = await ia.generate(
            target=context.target,
            settings=context.settings,
            config=config,
            goal=goal,
            gap_name=gap,
            catalog=context.catalog,
            prompt_catalog=context.seed_catalog,
            youth_rules=context.youth_rules,
            rules=context.rules,
            taken_workout_ids=context.taken_workout_ids,
            taken_exercise_ids=context.taken_exercise_ids,
        )
    except client.OmniRouteError as exc:
        return JSONResponse(
            status_code=UNPROCESSABLE,
            content=err(
                untrusted.GENERATION_FAILED,
                data={"errors": [Finding(untrusted.GENERATION_FAILED, str(exc)).as_dict()]},
            ),
        )

    if not outcome.ok or outcome.prepared is None:
        return JSONResponse(
            status_code=UNPROCESSABLE,
            content=err(
                untrusted.GENERATION_FAILED,
                data={"errors": outcome.as_errors()},
                model=outcome.model,
                attempts=outcome.attempts,
            ),
        )
    return ok(
        {"workout": ingestion.preview_payload(outcome.prepared), "errors": []},
        model=outcome.model,
        attempts=outcome.attempts,
    )


@router.post("/generate/accept")
async def accept_workout(request: Request, db: Annotated[Session, Depends(get_session)]) -> Any:
    """Store a preview, after re-running every check on the document the client actually sent."""
    try:
        raw = await read_capped(request)
    except BodyTooLarge:
        return failure(
            Finding(untrusted.TOO_LARGE, f"a workout must be under {MAX_BODY_BYTES // 1024} KB"),
            error=untrusted.TOO_LARGE,
        )
    payload, problem = _json_body(raw)
    if payload is None:
        return failure(problem or Finding(untrusted.PARSE_ERROR, "there was nothing to accept"))
    document = payload.get("workout")
    if not isinstance(document, dict):
        return failure(Finding(untrusted.PARSE_ERROR, "send the workout as an object", "workout"))

    try:
        context = ingestion.build_context(db, ingestion.normalise_profile_id(payload.get("profile")))
    except ingestion.UnknownProfile as exc:
        return failure(Finding(untrusted.PARSE_ERROR, str(exc), "profile"))

    prepared = ingestion.run(ingestion.as_document(document), context)
    if not prepared.ok:
        return failure(*prepared.errors, ignored_keys=list(prepared.ignored_keys))

    try:
        workout_id = store(db, prepared, source=SOURCE_GENERATED)
    except DuplicateId as exc:
        return failure(Finding(untrusted.ID_COLLISION, str(exc), "id"))
    return ok(
        ingestion.stored_payload(prepared, workout_id, context),
        source=SOURCE_GENERATED,
        ignored_keys=list(prepared.ignored_keys),
    )
