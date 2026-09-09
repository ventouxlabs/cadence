"""The check-in screen: six tests in, gaps and challenges out (principles section 7).

Every write answers twice over, exactly as PRP-02's Today does: an HTMX request gets the partial
it asked for, and a plain form POST gets a 303 back to the page. The steppers are real number
inputs, so with JavaScript switched off the buttons do nothing and the boxes still post.

Nothing here re-implements section 7. The router turns a form into ``bilan.Result`` objects and
hands them to ``bilan.service.save_battery``; validation, the youth cap and the challenge
generation all stay where they already live. A rejected value re-renders this form with one line
saying what was wrong, and never a 500 - the same floor the Setup screen holds itself to.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlmodel import Session, select

from cadence.bibliotheque.bundle import LibraryBundle
from cadence.bilan import assessments as bilan
from cadence.bilan import challenges as chal
from cadence.bilan import service
from cadence.bilan.gaps import ThresholdError
from cadence.db import get_session
from cadence.profils.tables import PROFILE_ME, Profile
from cadence.profils.visibility import is_hidden
from cadence.programme.bands import age_band
from cadence.programme.tables import PLANNED, SKIPPED, PlannedSession
from cadence.schema.enums import AssessmentId
from cadence.seance.catalog import library_bundle, load_settings
from cadence.web.rendering import templates
from cadence.web.solo import SOLO_HOME, ProfileHidden

router = APIRouter(tags=["assess"])

SEE_OTHER = 303
UNPROCESSABLE = 422
UNAVAILABLE = 503
ASSESS_TAB = "/assess"
TOGETHER = "together"
#: The tabs Today will serve. Anything else is not a redirect target (PRP-02's ``PROFILE_KEYS``).
TODAY_TABS: frozenset[str] = frozenset({PROFILE_ME, "son", TOGETHER})

LIBRARY_DOWN = "The exercise library is unavailable, so a check-in cannot be scored right now."

#: Section 7 as the wireframe renders it: seconds move by five, reps and scores by one.
SECONDS_STEP = 5
UNIT_STEP: dict[str, int] = {"s": SECONDS_STEP}
UNIT_LABEL: dict[str, str] = {"reps": "reps", "s": "s", "score": ""}
#: Where the box starts when nothing has been recorded and the library has no target to offer.
FALLBACK_DEFAULT: dict[str, int] = {"reps": 10, "s": 30, "score": 2}

# The section 7.2 rubric lives inside the spec's own ``protocol`` string, after "Self-rate ...:".
# Reading it from there rather than from a second copy here keeps one source for the wording; a
# protocol that stops matching renders no rubric rather than a stale one. Taking only the tail
# also drops the "8-16 kg" that opens the goblet protocol, which is a number no youth screen may
# carry (section 3.5).
RUBRIC_HEAD = re.compile(r"self-rate[^:]*:", re.IGNORECASE)
RUBRIC_ITEM = re.compile(r"^\s*(\d+)\s+(.+?)\.?\s*$")


@dataclass(frozen=True, slots=True)
class TestField:
    """One block of the form: what to call the test, and what control it gets."""

    test_id: str
    name: str
    unit_label: str
    self_rated: bool
    step: int
    default: int
    minimum: int
    maximum: int
    choices: tuple[int, ...]
    rubric: tuple[tuple[int, str], ...]
    cap: int | None
    unavailable: bool


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request", "").lower() == "true"


def _today() -> date:
    """The same clock ``cadence.api.assessments`` reads, so the two routes agree on the day."""
    return datetime.now(UTC).date()


def _error_page(request: Request, message: str, status: int) -> HTMLResponse:
    body = templates.get_template("message.html").render(
        request=request, message=message, profile_key=PROFILE_ME, view=None
    )
    return HTMLResponse(body, status_code=status)


def _rubric(protocol: str) -> tuple[tuple[int, str], ...]:
    """``((0, "wrists or low back leave the wall ..."), ...)`` from a self-rated protocol."""
    parts = RUBRIC_HEAD.split(protocol, maxsplit=1)
    if len(parts) != 2:
        return ()
    found: list[tuple[int, str]] = []
    for chunk in parts[1].split(";"):
        match = RUBRIC_ITEM.match(chunk)
        if match is not None:
            found.append((int(match.group(1)), match.group(2)))
    return tuple(found)


def _recent_value(db: Session, profile_id: str, test_id: str) -> float | None:
    """The newest number this person actually recorded for a test, skipping unavailable ones."""
    for row in reversed(bilan.all_for(db, profile_id)):
        if row.test_id == test_id and row.value is not None:
            return float(row.value)
    return None


def _bounds(test_id: str, unit: str, *, self_rated: bool) -> tuple[int, int]:
    if self_rated:
        low, high = bilan.SCORE_RANGES.get(test_id, (0, 5))
        return int(low), int(high)
    low_f, high_f = bilan.RANGES.get(unit, (0, 600))
    return int(low_f), int(high_f)


def _posted_default(posted: Any, test_id: str, low: int, high: int) -> int | None:
    """What the user typed into this box, for the form that is coming back rejected.

    Bad input must not cost the user the page (PRP-03's wireframe, and ``settings.py``'s
    ``_redisplay``): five correct entries are not thrown away because the sixth was wrong. The box
    that *was* wrong holds nothing parseable, so it alone falls back to its default.
    """
    if posted is None:
        return None
    raw = str(posted.get(f"value_{test_id}") or "").strip()
    if not raw:
        return None
    try:
        typed = int(round(float(raw)))
    except ValueError:
        return None
    return max(low, min(high, typed))


def _fields(db: Session, profile: Profile, library: LibraryBundle, posted: Any = None) -> tuple[TestField, ...]:
    """The six blocks in ``FORM_ORDER``, named for the profile's own vocabulary."""
    is_youth = profile.kind == "youth"
    band = age_band(profile)
    blocked = service.unavailable_tests(profile, library)
    fields: list[TestField] = []
    for test_id in bilan.FORM_ORDER:
        spec = library.assessments.spec(AssessmentId(test_id))
        if spec is None:  # pragma: no cover - the library validator guarantees all six
            continue
        entry = library.assessments.youth_targets.get(AssessmentId(test_id))
        low, high = _bounds(test_id, spec.unit, self_rated=spec.self_rated)
        stored = _recent_value(db, profile.id, test_id)
        target = chal.target_for(library.assessments, test_id, band, is_youth=is_youth)
        settled = stored if stored is not None else target
        if settled is None:
            settled = FALLBACK_DEFAULT.get(spec.unit, 10)
        typed = _posted_default(posted, test_id, low, high)
        fields.append(
            TestField(
                test_id=test_id,
                # Youth phrasing is the section 7.4 play string verbatim; adults get the test's name.
                name=(entry.phrasing if is_youth and entry is not None else spec.name),
                unit_label=UNIT_LABEL.get(spec.unit, spec.unit),
                self_rated=spec.self_rated,
                step=UNIT_STEP.get(spec.unit, 1),
                default=typed if typed is not None else int(round(settled)),
                minimum=low,
                maximum=high,
                choices=tuple(range(low, high + 1)) if spec.self_rated else (),
                rubric=_rubric(spec.protocol) if spec.self_rated else (),
                # A self-rated cap is its own rubric ceiling, so saying "stops at 3" adds nothing.
                cap=(entry.cap if is_youth and entry is not None and not spec.self_rated else None),
                unavailable=test_id in blocked,
            )
        )
    return tuple(fields)


def _context(
    request: Request,
    db: Session,
    profile: Profile,
    profile_key: str,
    library: LibraryBundle,
    *,
    error: str | None = None,
    posted: Any = None,
) -> dict[str, Any]:
    return {
        "request": request,
        "profile": profile,
        "profile_key": profile_key,
        "tab_base": ASSESS_TAB,
        "tests": _fields(db, profile, library, posted),
        "error": error,
        "result": None,
        "challenges": (),
        "capped_count": 0,
        "view": None,
    }


def _saved_context(request: Request, db: Session, profile: Profile, profile_key: str, capped: int) -> dict[str, Any]:
    return {
        "request": request,
        "profile": profile,
        "profile_key": profile_key,
        "tab_base": ASSESS_TAB,
        "tests": (),
        "error": None,
        "result": True,
        "challenges": chal.active_for(db, profile.id),
        "capped_count": capped,
        "view": None,
    }


def _resolve(request: Request, db: Session, raw: str | None) -> tuple[Profile, str] | Response:
    """One profile, by slug. Together is not a check-in subject: people are tested alone."""
    key = (raw or PROFILE_ME).strip().lower()
    if key == TOGETHER:
        return RedirectResponse(f"{ASSESS_TAB}?profile={PROFILE_ME}", status_code=SEE_OTHER)
    person = db.get(Profile, key)
    if person is None:
        return _error_page(request, f"There is no profile named {key!r}.", 404)
    return person, key


@router.get("/assess", response_class=HTMLResponse)
def assess_page(
    request: Request,
    db: Annotated[Session, Depends(get_session)],
    profile: Annotated[str | None, Query()] = None,
    saved: Annotated[str | None, Query()] = None,
    capped: Annotated[int, Query(ge=0, le=6)] = 0,
) -> Response:
    """The form, or - straight after a plain-form save - what that save produced."""
    resolved = _resolve(request, db, profile)
    if isinstance(resolved, Response):
        return resolved
    person, key = resolved
    if saved == "1":
        return templates.TemplateResponse(request, "assess.html", _saved_context(request, db, person, key, capped))
    library = library_bundle()
    if library is None:
        return _error_page(request, LIBRARY_DOWN, UNAVAILABLE)
    return templates.TemplateResponse(request, "assess.html", _context(request, db, person, key, library))


def _number(text: str, name: str) -> float:
    """The posted box as a number, or a line the form can show under it."""
    try:
        return float(text)
    except ValueError as exc:
        raise bilan.AssessmentError(f"{name} needs a number, not {text!r}.") from exc


def _one_result(library: LibraryBundle, field: TestField, form: Any, *, is_youth: bool) -> bilan.Result:
    """One posted test, validated by ``parse_result`` rather than by anything written here.

    A test this household cannot run is stored unavailable whatever the checkbox says: the block
    rendered without a stepper, so there is no number it could honestly have carried (D-019).
    """
    if field.unavailable or form.get(f"unavailable_{field.test_id}"):
        return service.unavailable_result(library, field.test_id)
    raw = str(form.get(f"value_{field.test_id}") or "").strip()
    if not raw:
        raise bilan.AssessmentError(f"{field.name} needs a number.")
    value = _number(raw, field.name)
    payload = {"test_id": field.test_id, "value": int(value) if value.is_integer() else value}
    return bilan.parse_result(library.assessments, payload, is_youth=is_youth)


def _hidden_here() -> ProfileHidden:
    """The bounce a hidden profile's check-in gets, once its write has already gone through.

    ``ProfileHidden`` rather than a ``RedirectResponse`` built here, so the one handler in
    ``create_app`` decides the shape: a 303 for a plain post, ``HX-Redirect`` for an HTMX one.
    This form posts both ways, and a 303 answering the HTMX post would swap the parent's whole
    check-in page into the result panel (D-269).
    """
    return ProfileHidden(f"{ASSESS_TAB}?profile={PROFILE_ME}")


def _render_form(request: Request, context: dict[str, Any], status: int) -> Response:
    """The form alone for HTMX, the whole page for a plain post. Never a separate error page."""
    if _is_htmx(request):
        return HTMLResponse(templates.get_template("partials/assess_form.html").render(**context), status_code=status)
    return templates.TemplateResponse(request, "assess.html", context, status_code=status)


@router.post("/assess", response_class=HTMLResponse)
async def assess_save(
    request: Request,
    db: Annotated[Session, Depends(get_session)],
    profile: Annotated[str | None, Query()] = None,
) -> Response:
    """Record one battery and show the challenges it just set.

    The field names are per test (``value_<test_id>``), so the body is read off the request rather
    than declared as parameters: FastAPI cannot name a form field it does not know at import time.
    """
    resolved = _resolve(request, db, profile)
    if isinstance(resolved, Response):
        return resolved
    person, key = resolved
    # Solo mode splits this route in two (D-276). The **write** goes through exactly as it always
    # did - D-264 and D-265 both say a hidden profile costs the screen and never the data - but
    # every answer that carries *his page* is replaced by a bounce to the parent's own. Without
    # this the route recorded the battery correctly and then handed back 7.7 KB of his screen,
    # youth scope and all, on a household that had hidden him.
    hidden = is_hidden(load_settings(db), key)
    library = library_bundle()
    if library is None:
        return _error_page(request, LIBRARY_DOWN, UNAVAILABLE)

    form = await request.form()
    fields = _fields(db, person, library)
    is_youth = person.kind == "youth"
    try:
        results = [_one_result(library, field, form, is_youth=is_youth) for field in fields]
        state = service.save_battery(db, person, results, library, load_settings(db), _today(), _today())
    except bilan.AssessmentError as exc:
        if hidden:
            # The redisplay is his form, with his tests named in his vocabulary, so it is the one
            # answer that cannot be given. Nothing is lost that a stale page had any right to.
            raise _hidden_here() from exc
        context = _context(request, db, person, key, library, error=str(exc), posted=form)
        return _render_form(request, context, UNPROCESSABLE)
    except ThresholdError as exc:
        # A zero ``ok`` threshold is a broken library, not a broken form: say so on its own page.
        # Safe while hidden - ``_error_page`` renders the generic message on the parent's tab.
        return _error_page(request, str(exc), 500)

    # Saved. The battery is his and it is recorded; the screen that would report it is not.
    if hidden:
        raise _hidden_here()
    capped = len(state.capped)
    if not _is_htmx(request):
        suffix = f"&capped={capped}" if capped else ""
        return RedirectResponse(f"{ASSESS_TAB}?profile={key}&saved=1{suffix}", status_code=SEE_OTHER)
    body = templates.get_template("partials/assess_result.html").render(
        request=request, profile_key=key, challenges=state.challenges, capped_count=capped
    )
    return HTMLResponse(body)


@router.post("/assess/skip")
def assess_skip(
    request: Request,
    db: Annotated[Session, Depends(get_session)],
    profile: Annotated[str | None, Query()] = None,
    tab: Annotated[str | None, Form()] = None,
) -> Response:
    """Skip the queued assessment day and move on. The card returns on the next session.

    Only the *planned* assessment at the head of the queue is marked: skipping is a statement
    about today, and a later block's assessment day is not today's to spend.
    """
    resolved = _resolve(request, db, profile)
    if isinstance(resolved, Response):
        return resolved
    person, key = resolved
    statement = (
        select(PlannedSession)
        .where(
            PlannedSession.profile_id == person.id,
            PlannedSession.status == PLANNED,
            PlannedSession.day_type == service.ASSESSMENT_DAY,
        )
        .order_by(PlannedSession.week, PlannedSession.day_index)  # type: ignore[arg-type]
    )
    planned = db.exec(statement).first()
    if planned is not None:
        planned.status = SKIPPED
        db.add(planned)
        db.commit()
    # The skip is his and it lands; where it sends the browser is not (D-276). This route leaks no
    # markup either way - it only ever redirects - but naming him in the ``Location`` costs a hop
    # through Today's own gate and puts a hidden profile in a header for no reason.
    if is_hidden(load_settings(db), key):
        raise ProfileHidden(SOLO_HOME)
    # The tab the card was tapped on, so skipping from Together comes back to Together. A tab
    # nobody has is not a redirect target: it would land the user on Today's 404 page.
    back = (tab or key).strip().lower()
    return RedirectResponse(f"/today?profile={back if back in TODAY_TABS else key}", status_code=SEE_OTHER)


__all__ = ["router"]
