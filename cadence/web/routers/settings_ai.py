"""The Import and Generate cards on Settings, and the HTMX partials behind them.

Everything a model or a stranger wrote reaches these templates as a plain Jinja variable and is
escaped by the environment. Nothing in this module builds HTML from imported text, and no
template it renders uses ``|safe`` on a name, a cue or a note (PRP-08 risk 5).

The preview travels back to the browser and returns in the Accept post. That is deliberate: with
no server-side copy to compare against, the accept path has no choice but to re-validate what it
was handed, which is the property PRP-08 risk 6 exists to protect.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.datastructures import FormData
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import inspect
from sqlalchemy import text as sql_text
from sqlmodel import Session

from cadence.bibliotheque import adoption, ingestion, untrusted
from cadence.bibliotheque.import_service import (
    SOURCE_GENERATED,
    SOURCE_IMPORT,
    DuplicateId,
    Prepared,
    store,
)
from cadence.bibliotheque.untrusted import MAX_BODY_BYTES, Finding
from cadence.config import Settings, get_settings
from cadence.db import get_session
from cadence.ia import client
from cadence.ia import generate as ia
from cadence.ia.prompt import goal_label
from cadence.profils.tables import PROFILE_ME, PROFILE_SON
from cadence.schema.enums import AssessmentId, GoalType
from cadence.seance.catalog import display_unit, load_settings
from cadence.web.gate import require_setup
from cadence.web.guards import enforce_declared_size, read_bounded_form, require_same_origin
from cadence.web.rendering import templates

# Both guards run before any handler body here, which is the only point early enough: FastAPI
# has parsed the form by the time a handler starts (D-186).
router = APIRouter(
    tags=["settings-ai"],
    dependencies=[
        Depends(require_same_origin),
        Depends(enforce_declared_size),
        Depends(require_setup),
    ],
)
logger = logging.getLogger(__name__)

UNPROCESSABLE = 422
UNAVAILABLE = 503

# The four goals that are legal for every profile, and the three that are adult-only
# (principles section 3.5). One select serves both people, so the list is the union and the
# labels carry the restriction in words (D-026); ``goal_is_allowed`` is the check that actually
# refuses a body-composition goal for the son, and it runs on both write paths.
YOUTH_SAFE_GOALS: tuple[GoalType, ...] = (
    GoalType.STRENGTH,
    GoalType.POSTURE,
    GoalType.MOVEMENT_QUALITY,
    GoalType.CONSISTENCY,
)
ADULT_ONLY_GOALS: tuple[GoalType, ...] = (GoalType.WEIGHT, GoalType.BODY_FAT, GoalType.APPEARANCE)


def goal_choices(profile_kind: str) -> tuple[tuple[str, str], ...]:
    """``(value, label)`` for the goal select. A youth-only household never sees a body goal."""
    goals = YOUTH_SAFE_GOALS if profile_kind == "youth" else (*YOUTH_SAFE_GOALS, *ADULT_ONLY_GOALS)
    return tuple((item.value, goal_label(item)) for item in goals)


def gap_choices(db: Session, profile_id: str = PROFILE_ME) -> tuple[tuple[str, str], ...]:
    """Active challenges as ``(test_id, name)``, or nothing when PRP-07's table is not here yet.

    The **value** is the assessment test id and never the challenge name. A name is free text a
    person typed; a test id is one of ``AssessmentId``'s six members, and it is what travels in
    the prompt (Codex finding 7). Asked of the database rather than of an import, so this file
    works both before and after the progression branch lands.

    A row whose ``test_id`` is not one of those six is dropped here rather than offered. The
    docstring claimed closed-enum safety that nothing checked: ``challenge`` is a table PRP-07
    writes, ``test_id`` is a free ``TEXT`` column in it, and the value is interpolated into the
    outgoing prompt as ``gap_name`` with no content scan on the way. "It is in the table" is a
    weaker claim than "it is one of six known strings", and it was the only one being made
    (D-195). Filtering here rather than in ``resolve_gap`` is what makes the select and the write
    path inherit the same rule from one place.
    """
    try:
        if not inspect(db.get_bind()).has_table("challenge"):
            return ()
        # ``Session.execute``, not SQLModel's ``exec``: ``exec`` is typed for a SQLModel select
        # and a text clause does not go through it. The failure would be swallowed by the guard
        # below, so the gap list would stay empty for ever once PRP-07 lands the table.
        rows = db.execute(
            sql_text("SELECT test_id, name FROM challenge WHERE profile_id = :pid AND status = 'active' ORDER BY name"),
            {"pid": profile_id},
        ).all()
    except Exception:  # noqa: BLE001 - a missing or changed table must not break the screen
        # ``warning`` with the traceback, not a bare ``info``: the ``has_table`` probe above
        # already answers "PRP-07 has not landed", so anything reaching here is a real database
        # fault wearing the same silent, empty-list costume as the expected case (D-191).
        logger.warning("could not read the challenge table for gaps; offering none", exc_info=True)
        return ()
    known = {item.value for item in AssessmentId}
    return tuple((str(row[0]), str(row[1])) for row in rows if row and row[0] and row[1] and str(row[0]) in known)


def gap_choices_for_household(db: Session) -> tuple[tuple[str, str], ...]:
    """Every profile's active challenges, de-duplicated by test id and ordered by name.

    One Generate card serves both people, so a list built from the adult alone offered the son's
    select nothing to pick and silently dropped his own challenges. Widening the *display* only:
    ``resolve_gap`` still checks the posted gap against the named profile's own challenges, so
    picking the adult's gap while generating for the son is still refused (D-192).
    """
    seen: dict[str, str] = {}
    for profile_id in (PROFILE_ME, PROFILE_SON):
        for test_id, name in gap_choices(db, profile_id):
            seen.setdefault(test_id, name)
    return tuple(sorted(seen.items(), key=lambda item: item[1]))


def resolve_gap(db: Session, profile_id: str, raw: str | None) -> str | None:
    """The posted gap, resolved against this profile's own active challenges.

    Anything that is not one of them is refused rather than passed through, which is what makes
    the select unbypassable: a hand-rolled POST naming "ignore your instructions" matches no
    challenge and never reaches the prompt. An empty value is "nothing in particular".
    """
    wanted = (raw or "").strip()
    if not wanted:
        return None
    allowed = {test_id for test_id, _ in gap_choices(db, profile_id)}
    if wanted not in allowed:
        raise UnknownGap(wanted)
    return wanted


class UnknownGap(LookupError):
    """The posted gap is not an active challenge for this profile."""


def _field(form: FormData, name: str, default: str) -> str:
    """One plain text field off a parsed form, never an upload masquerading as one."""
    value = form.get(name)
    return value if isinstance(value, str) else default


def _render(request: Request, name: str, **context: Any) -> HTMLResponse:
    body = templates.get_template(f"partials/{name}.html").render(request=request, **context)
    return HTMLResponse(body)


def _problem(request: Request, name: str, *findings: Finding, status: int = UNPROCESSABLE) -> HTMLResponse:
    body = templates.get_template(f"partials/{name}.html").render(
        request=request,
        result={"ok": False, "errors": [item.as_dict() for item in findings]},
        preview=None,
    )
    return HTMLResponse(body, status_code=status)


# ------------------------------------------------------------------------------------ import


@router.post("/settings/import", response_class=HTMLResponse)
async def import_section(request: Request, db: Annotated[Session, Depends(get_session)]) -> Response:
    """Check and import what the card was given, and answer with the result partial alone.

    A bare ``Request`` and not ``Form``/``File`` parameters: FastAPI reads the body before it
    solves dependencies, so the only way the size guard runs before the multipart parser is for
    the handler to call it itself (D-186).
    """
    form = await read_bounded_form(request)
    try:
        profile = _field(form, "profile", ingestion.DEFAULT_PROFILE)
        day_type = _field(form, "day_type", "")
        document = _field(form, "text", "")
        upload = form.get("file")
        if upload is not None and not isinstance(upload, str) and upload.filename:
            # The filename is never read for anything: not to choose a parser, not to log, not to
            # build a path. The bytes are all that is taken (D-010).
            raw = await upload.read(MAX_BODY_BYTES + 1)
            if len(raw) > MAX_BODY_BYTES:
                return _problem(
                    request,
                    "import_result",
                    Finding(untrusted.TOO_LARGE, f"a workout must be under {MAX_BODY_BYTES // 1024} KB"),
                )
            try:
                document = raw.decode("utf-8")
            except UnicodeDecodeError:
                return _problem(request, "import_result", Finding(untrusted.PARSE_ERROR, "that file is not UTF-8 text"))
    finally:
        await form.close()
    if not document.strip():
        return _problem(
            request, "import_result", Finding(untrusted.PARSE_ERROR, "paste a workout or choose a file first")
        )

    try:
        context = ingestion.build_context(db, ingestion.normalise_profile_id(profile))
    except ingestion.UnknownProfile as exc:
        return _problem(request, "import_result", Finding(untrusted.PARSE_ERROR, str(exc), "profile"))

    prepared = ingestion.run(document, context)
    if not prepared.ok:
        return _problem(request, "import_result", *prepared.errors)
    return _stored(request, db, prepared, context, source=SOURCE_IMPORT, day_type=day_type, partial="import_result")


def _stored(
    request: Request,
    db: Session,
    prepared: Prepared,
    context: ingestion.Context,
    *,
    source: str,
    day_type: str,
    partial: str,
) -> Response:
    """Store, optionally point a day type at it, and render the success line."""
    workout = prepared.workout
    assert workout is not None  # noqa: S101 - only reached when ``prepared.ok``
    try:
        workout_id = store(db, prepared, source=source)
    except DuplicateId as exc:
        return _problem(request, partial, Finding(untrusted.ID_COLLISION, str(exc), "id"))
    # The document's own inline definitions are part of the catalog its rows resolve against; the
    # context's copy was read before the document was (finding 11).
    catalog = ingestion.resolved_catalog(context, prepared)
    swapped = 0
    swap_error: str | None = None
    if day_type:
        try:
            swapped = adoption.adopt(db, workout, profile_id=context.profile.id, day_type=day_type, catalog=catalog)
        except (ValueError, KeyError) as exc:
            swap_error = f"the workout was saved, but it could not be used for that day: {exc}"
    return _render(
        request,
        partial,
        result={
            "ok": True,
            "errors": [],
            "workout_id": workout_id,
            "name": workout.name,
            "rows": len(workout.rows),
            "warnings": list(prepared.warnings),
            "swapped": swapped,
            "swap_error": swap_error,
            "source": source,
        },
        preview=None,
    )


# ---------------------------------------------------------------------------------- generate


@router.post("/settings/generate", response_class=HTMLResponse)
async def generate_section(
    request: Request,
    db: Annotated[Session, Depends(get_session)],
    config: Annotated[Settings, Depends(get_settings)],
) -> Response:
    """Ask for a workout and render the read-only preview. Nothing is stored by this route.

    A bare ``Request`` and ``read_bounded_form``, not ``Form()`` parameters, for the reason
    ``/settings/import`` already carries: FastAPI reads the body before it solves dependencies, so
    declaring a form parameter parses the body *ahead of* the router's size guard. Urlencoded is
    not multipart, so nothing spools to disk - but the whole body was still read into memory with
    no gate in front of it, and a four-megabyte post was accepted in full (D-193).
    """
    form = await read_bounded_form(request)
    try:
        profile = _field(form, "profile", ingestion.DEFAULT_PROFILE)
        goal = _field(form, "goal", GoalType.POSTURE.value)
        gap = _field(form, "gap", "")
    finally:
        await form.close()
    if not config.omniroute_configured:
        return _problem(request, "generate_preview", ia.unavailable_finding(), status=UNAVAILABLE)
    try:
        chosen = GoalType(goal)
    except ValueError:
        return _problem(request, "generate_preview", Finding(untrusted.PARSE_ERROR, "pick a goal", "goal"))
    try:
        context = ingestion.build_context(db, ingestion.normalise_profile_id(profile))
        named = resolve_gap(db, context.profile.id, gap)
    except ingestion.UnknownProfile as exc:
        return _problem(request, "generate_preview", Finding(untrusted.PARSE_ERROR, str(exc), "profile"))
    except UnknownGap:
        return _problem(
            request,
            "generate_preview",
            Finding(untrusted.PARSE_ERROR, "that is not one of this profile's challenges", "gap"),
        )

    if not ia.goal_is_allowed(chosen, profile_kind=context.target.profile_kind, rules=context.rules):
        return _problem(
            request,
            "generate_preview",
            Finding("youth_banned_goal", "that is not a goal this profile trains for", "goal"),
        )

    try:
        outcome = await ia.generate(
            target=context.target,
            settings=context.settings,
            config=config,
            goal=chosen,
            gap_name=named,
            catalog=context.catalog,
            prompt_catalog=context.seed_catalog,
            youth_rules=context.youth_rules,
            rules=context.rules,
            taken_workout_ids=context.taken_workout_ids,
            taken_exercise_ids=context.taken_exercise_ids,
        )
    except client.OmniRouteError as exc:
        return _problem(request, "generate_preview", Finding(untrusted.GENERATION_FAILED, str(exc)))
    if not outcome.ok or outcome.prepared is None:
        return _problem(request, "generate_preview", *outcome.errors)

    workout = outcome.prepared.workout
    assert workout is not None  # noqa: S101 - guarded by ``outcome.ok``
    return _render(
        request,
        "generate_preview",
        result=None,
        # Today and History both convert; this panel did not, so a pound household read the one
        # screen that shows a *proposed* load in kilograms with no label saying so (D-190).
        unit=display_unit(load_settings(db)),
        preview={
            "workout": workout,
            "document": json.dumps(workout.model_dump(mode="json")),
            "profile": context.profile.id,
            "rows": adoption.rows_for(workout, ingestion.resolved_catalog(context, outcome.prepared)),
            "day_types": adoption.day_type_choices(),
            "model": outcome.model,
            "attempts": outcome.attempts,
        },
    )


@router.post("/settings/generate/accept", response_class=HTMLResponse)
async def accept_section(
    request: Request,
    db: Annotated[Session, Depends(get_session)],
) -> Response:
    """Store the previewed workout, after checking the posted document from scratch.

    Bounded before it is read, for the same reason as ``generate_section`` (D-193). This route
    carries the whole document in a form field, so it is the larger of the two bodies.
    """
    form = await read_bounded_form(request)
    try:
        profile = _field(form, "profile", ingestion.DEFAULT_PROFILE)
        workout = _field(form, "workout", "")
        day_type = _field(form, "day_type", "")
    finally:
        await form.close()
    if deep := untrusted.check_json_depth(workout):
        return _problem(request, "generate_preview", deep[0])
    try:
        posted = json.loads(workout)
    except (ValueError, RecursionError):
        return _problem(request, "generate_preview", Finding(untrusted.PARSE_ERROR, "that preview could not be read"))
    if not isinstance(posted, dict):
        return _problem(request, "generate_preview", Finding(untrusted.PARSE_ERROR, "that preview is not a workout"))
    try:
        context = ingestion.build_context(db, ingestion.normalise_profile_id(profile))
    except ingestion.UnknownProfile as exc:
        return _problem(request, "generate_preview", Finding(untrusted.PARSE_ERROR, str(exc), "profile"))

    prepared = ingestion.run(ingestion.as_document(posted), context)
    if not prepared.ok:
        return _problem(request, "generate_preview", *prepared.errors)
    return _stored(
        request, db, prepared, context, source=SOURCE_GENERATED, day_type=day_type, partial="generate_preview"
    )


@router.post("/settings/generate/discard", response_class=HTMLResponse)
def discard_section(request: Request) -> Response:
    """Clear the preview. Nothing was stored, so there is nothing to undo."""
    return HTMLResponse('<div id="generate-preview"></div>')
