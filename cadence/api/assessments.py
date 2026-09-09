"""``GET`` and ``POST /api/assessments`` - the six-test battery as JSON.

The POST is strict on purpose (PRP-07): an unknown ``test_id``, a value outside the test's
plausible range, a body-composition metric on a youth profile, or a self-rated score that is not a
whole number all answer 422 naming what was wrong. A youth measurement above the section 7.4 cap is
**not** an error - it is stored at the cap and the response says so, because the cap is the
protocol and the child really did do the reps.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import JSONResponse
from sqlmodel import Session

from cadence.api.envelope import err, ok
from cadence.api.gates import refuse_hidden
from cadence.bilan import assessments as bilan
from cadence.bilan import service
from cadence.bilan.gaps import BODY_COMP, ThresholdError
from cadence.db import get_session
from cadence.profils.tables import Profile
from cadence.seance.catalog import library_bundle, load_settings

router = APIRouter(prefix="/api", tags=["assessments"])

BODY_COMP_KEYS: frozenset[str] = frozenset({BODY_COMP, "body_fat_pct", "muscle_pct", "weight_kg"})


def _refuse(message: str, status: int = 422) -> JSONResponse:
    return JSONResponse(status_code=status, content=err(message))


def resolve_profile(db: Session, raw: str | None) -> Profile | None:
    """One profile, by slug. ``together`` is not an assessment subject: people are tested alone."""
    key = (raw or "me").strip().lower()
    return db.get(Profile, key)


def today_utc() -> date:
    return datetime.now(UTC).date()


@router.get("/assessments")
def get_assessments(
    db: Annotated[Session, Depends(get_session)],
    profile: Annotated[str | None, Query()] = None,
) -> Any:
    """The latest battery, the gaps it exposes and the challenges standing against them.

    Solo mode gates the **read** and not the ``POST`` below it (D-265). Recording a battery is a
    write, no screen can reach it while the son is hidden, and the offline client never queues one
    - so refusing it would buy nothing and put a toggle in front of somebody's data.
    """
    hidden = refuse_hidden(db, profile)
    if hidden is not None:
        return hidden
    person = resolve_profile(db, profile)
    if person is None:
        return _refuse(f"unknown profile {profile!r}", 404)
    library = library_bundle()
    if library is None:
        return _refuse("the exercise library is unavailable, so assessments cannot be scored", 503)
    try:
        state = service.state_for(db, person, library, today_utc())
    except ThresholdError as exc:
        return _refuse(str(exc), 500)
    return ok(state.as_dict(library), **state.meta())


def _parse_body(body: Any) -> tuple[str | None, str | None, list[Any], str | None]:
    """``(profile, recorded_on, results, error)``. Never raises on a hostile body."""
    if not isinstance(body, dict):
        return None, None, [], "the body must be a JSON object"
    results = body.get("results")
    if not isinstance(results, list) or not results:
        return None, None, [], "results must be a non-empty list"
    raw_profile = body.get("profile")
    if raw_profile is not None and not isinstance(raw_profile, str):
        return None, None, [], "profile must be a string"
    raw_date = body.get("recorded_on")
    if raw_date is not None and not isinstance(raw_date, str):
        return None, None, [], "recorded_on must be an ISO date"
    return raw_profile, raw_date, results, None


@router.post("/assessments")
def post_assessments(
    db: Annotated[Session, Depends(get_session)],
    body: Annotated[Any, Body()] = None,
    profile: Annotated[str | None, Query()] = None,
) -> Any:
    """Record one battery and re-derive this profile's challenges from it."""
    raw_profile, raw_date, results, problem = _parse_body(body)
    if problem is not None:
        return _refuse(problem)
    person = resolve_profile(db, raw_profile or profile)
    if person is None:
        return _refuse(f"unknown profile {raw_profile or profile!r}", 404)
    library = library_bundle()
    if library is None:
        return _refuse("the exercise library is unavailable, so assessments cannot be scored", 503)

    try:
        recorded_on = date.fromisoformat(raw_date) if raw_date else today_utc()
    except ValueError:
        return _refuse(f"{raw_date!r} is not an ISO date")

    is_youth = person.kind == "youth"
    # D-019: a household with no overhead anchor cannot run the dead hang, so a value for it is
    # recorded as "unavailable" rather than stored. The HTML form already does this; the JSON
    # route did not, so a posted dead-hang value became a gap and then a challenge for a profile
    # that has nowhere to hang (D-224). Coerced before parsing: the parser is not the place to
    # decide that a number the caller sent is not a number this household can have.
    blocked = service.unavailable_tests(person, library)
    parsed: list[bilan.Result] = []
    for item in results:
        if is_youth and isinstance(item, dict) and str(item.get("test_id") or "") in BODY_COMP_KEYS:
            # Section 3.5: body composition is a banned goal type for every youth band, so it is
            # refused at the boundary rather than stored and filtered out later.
            return _refuse(
                f"{item.get('test_id')!r} is a body-composition metric and is never recorded for a youth profile"
            )
        if isinstance(item, dict) and str(item.get("test_id") or "") in blocked:
            parsed.append(service.unavailable_result(library, str(item.get("test_id"))))
            continue
        try:
            parsed.append(bilan.parse_result(library.assessments, item, is_youth=is_youth))
        except bilan.AssessmentError as exc:
            return _refuse(str(exc))

    seen = {result.test_id for result in parsed}
    if len(seen) != len(parsed):
        return _refuse("each test may appear only once in a battery")

    try:
        state = service.save_battery(db, person, parsed, library, load_settings(db), recorded_on, today_utc())
    except ThresholdError as exc:
        return _refuse(str(exc), 500)
    return ok(state.as_dict(library), **state.meta())


__all__ = ["router"]
