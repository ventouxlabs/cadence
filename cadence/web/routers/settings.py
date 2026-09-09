"""The Setup and Settings screens and their two HTMX partials.

One form template serves both screens, so a control cannot exist on one and not the other. Every
control is a plain input or radio: the segmented buttons are radios, the single Save is a submit,
and the only HTMX on the page is the live weights preview. With JavaScript off the form still
posts and still comes back with its errors in place.

Bad input never costs the user the page. A rejected save re-renders the form they were looking at,
with their values still in it and one line under each field that was wrong (PRP-03 wireframe).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlmodel import Session

from cadence.bibliotheque import adoption
from cadence.db import get_session
from cadence.profils import services
from cadence.profils.services import RebuildUnavailable
from cadence.profils.settings import SESSION_MINUTES_CHOICES, ProgramSettings
from cadence.profils.tables import PROFILE_ME, PROFILE_SON, Profile
from cadence.profils.validation import LEGAL_EQUIPMENT, REQUIRED_EQUIPMENT, ValidationFailed
from cadence.programme.bands import band_for_age
from cadence.schema.enums import AgeBand
from cadence.seance.catalog import library_bundle
from cadence.web.gate import require_setup
from cadence.web.rendering import templates
from cadence.web.routers import settings_ai

router = APIRouter(tags=["settings"])

SEE_OTHER = 303
UNPROCESSABLE = 422
UNAVAILABLE = 503

DAYS_CHOICES: tuple[int, ...] = (2, 3, 4, 5, 6)
UNIT_CHOICES: tuple[str, ...] = ("kg", "lb")
EQUIPMENT_LABELS: dict[str, str] = {
    "bodyweight": "Bodyweight",
    "dumbbells": "Dumbbells",
    "kettlebells": "Kettlebells",
    "bench": "Adjustable bench",
}
# The whitelist is not a suggestion: the picker is built from it, so an id that is not on it has
# no checkbox to arrive from and no branch that would accept it if it did.
EQUIPMENT_CHOICES: tuple[tuple[str, str], ...] = tuple((item, EQUIPMENT_LABELS[item]) for item in LEGAL_EQUIPMENT)
LOCKED_EQUIPMENT: frozenset[str] = frozenset({REQUIRED_EQUIPMENT})

AGE_HELP = "Enter an age between 3 and 19."

# The services address a *profile's* field (`son.vitalforge_person`); the form addresses an input
# (`person_son`). This is the one translation between them, so the template can key on the thing
# it actually renders and each input gets its own error line.
FORM_FIELD_FOR: dict[tuple[str, str], str] = {
    (PROFILE_ME, "vitalforge_person"): "person_me",
    (PROFILE_SON, "vitalforge_person"): "person_son",
    (PROFILE_SON, "age_years"): "age_years",
    (PROFILE_ME, "bodyweight_kg"): "bodyweight_kg",
}


def _form_errors(raw: dict[str, str]) -> dict[str, str]:
    """Re-key service errors onto the inputs that produced them.

    An unscoped key is a household setting and already names its own control. A scoped one names
    a profile, and without this the two person slugs would land on the same key and the screen
    would show one message for two boxes.
    """
    shown: dict[str, str] = {}
    for key, message in raw.items():
        scope, _, field = key.rpartition(".")
        shown[FORM_FIELD_FOR.get((scope, field), field)] = message
    return shown


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request", "").lower() == "true"


# A field the browser did not send at all, as distinct from one it sent empty. A radio group
# cannot be un-chosen in a browser, so an absent one means "this form did not ask", and the stored
# value stands. An *empty* one is a value the user cleared, and that is a different answer.
ABSENT = object()


def _number(text: str | None) -> Any:
    """An int when the text is one, ``ABSENT`` when the field was not sent, else the raw text.

    Handing the raw string back rather than ``None`` matters: ``None`` would read as "leave this
    setting alone" and quietly accept a form that asked for four days a week and got "banana".
    """
    if text is None:
        return ABSENT
    raw = text.strip()
    return int(raw) if raw.isdigit() else raw


def _decimal(text: Any) -> Any:
    """A float, ``None`` for a cleared box, ``ABSENT`` for a field the form did not send."""
    if text is ABSENT or text is None:
        return ABSENT
    raw = str(text).strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return raw


def _age(text: Any) -> Any:
    """Digits only. Anything else is named as an out-of-range age rather than a type error.

    ``ABSENT`` travels through untouched. A ``POST /settings`` that never mentions ``son_age``
    must leave the recorded age alone; reading a missing field as an empty one would clear the
    son's age, and with it drop his band to the strictest one, as a side effect of saving an
    unrelated setting.
    """
    if text is ABSENT or text is None:
        return ABSENT
    raw = str(text).strip()
    if not raw:
        return None
    return int(raw) if raw.isdigit() else raw


def _present(patch: dict[str, Any]) -> dict[str, Any]:
    """Drop the fields this form did not send, so a patch only names what it actually set."""
    return {key: value for key, value in patch.items() if value is not ABSENT}


class ProfilesMissing(RuntimeError):
    """The two seeded profiles are not there, so there is nothing to set up."""


def _profile(db: Session, profile_id: str) -> Profile:
    profile = services.get_profile(db, profile_id)
    if profile is None:
        # PRP-01 writes the two profiles; the screen cannot invent them (D-023).
        raise ProfilesMissing(profile_id)
    return profile


def _needs_seeding(request: Request) -> Response:
    """The page D-072 used to serve from ``/``, now served from the screen that needs it.

    Deleting D-072's front-door branch removed the only place that said which command to run, and
    an unseeded install then met a stack trace on the one screen it is sent to.
    """
    body = templates.get_template("message.html").render(
        request=request,
        message="No profiles yet. Run `make seed` to load the library and build the first block.",
        profile_key=PROFILE_ME,
        view=None,
        hide_tabs=True,
    )
    return HTMLResponse(body, status_code=200)


def _state_form(settings: ProgramSettings, me: Profile, son: Profile) -> dict[str, Any]:
    """The form as the stored state describes it."""
    return {
        "son_age": "" if son.age_years is None else str(son.age_years),
        "equipment": [item.value for item in settings.equipment],
        "weights_available": settings.weights_available,
        "days_per_week": settings.days_per_week,
        "session_minutes": settings.session_minutes,
        "has_overhead_anchor": me.has_overhead_anchor,
        "push_son_to_garmin": settings.push_son_to_garmin,
        "son_enabled": settings.son_enabled,
        "display_unit": settings.display_unit,
        "person_me": me.vitalforge_person,
        "person_son": son.vitalforge_person,
        "bodyweight_kg": "" if me.bodyweight_kg is None else f"{me.bodyweight_kg:g}",
    }


def _generation_configured(request: Request) -> bool:
    """Whether the Generate card is offered at all.

    Read off ``app.state``, which ``create_app`` sets from the settings the app was built with,
    rather than off the process-wide cache: the two differ in every test and would differ in any
    install that ever builds a second app.
    """
    config = getattr(request.app.state, "settings", None)
    return bool(config is not None and config.omniroute_configured)


def _ai_context(request: Request, me: Profile, son: Profile, gaps: tuple[tuple[str, str], ...]) -> dict[str, Any]:
    """What PRP-08's Import and Generate cards need. Static apart from the gap list.

    Kept in one function so the Settings screen has one place that knows the cards exist, and so
    the goal list a youth profile could be offered is filtered where it is chosen rather than
    only where it is checked (D-026, principles section 3.5).
    """
    kinds = {profile.id: profile.kind for profile in (me, son)}
    return {
        "profile_choices": tuple((profile.id, profile.display_name) for profile in (me, son)),
        "profile_kinds": kinds,
        # Keyed to the adult profile when there is one: the select serves both people, and the
        # son is protected by ``goal_is_allowed`` on the write path rather than by a shorter list.
        "goal_choices": settings_ai.goal_choices("adult" if "adult" in kinds.values() else "youth"),
        "gap_choices": gaps,
        "day_type_choices": adoption.day_type_choices(),
        "generate_enabled": _generation_configured(request),
    }


def _context(
    request: Request,
    mode: str,
    settings: ProgramSettings,
    me: Profile,
    son: Profile,
    form: dict[str, Any],
    errors: dict[str, str] | None = None,
    saved: bool = False,
    form_error: str | None = None,
    gaps: tuple[tuple[str, str], ...] = (),
) -> dict[str, Any]:
    return {
        **_ai_context(request, me, son, gaps),
        "request": request,
        "mode": mode,
        "action": "/setup" if mode == "setup" else "/settings",
        "settings": settings,
        "me": me,
        "son": son,
        "form": form,
        "errors": errors or {},
        "saved": saved,
        "form_error": form_error,
        "preview": services.weights_preview(str(form.get("weights_available", ""))),
        "equipment_choices": EQUIPMENT_CHOICES,
        "locked_equipment": LOCKED_EQUIPMENT,
        "days_choices": DAYS_CHOICES,
        "minutes_choices": SESSION_MINUTES_CHOICES,
        "unit_choices": UNIT_CHOICES,
        # Exactly the condition `age_band` uses, not a hardcoded 18: the note and the band it
        # explains cannot drift apart if the section 3.1 table ever gains a row.
        "son_past_bands": (
            son.kind == "youth" and son.age_years is not None and band_for_age(son.age_years) is AgeBand.ADULT
        ),
        "profile_key": PROFILE_ME,
        "hide_tabs": mode == "setup",
        "view": None,
    }


@router.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request, db: Annotated[Session, Depends(get_session)]) -> Response:
    """The first-run screen. Once it has been answered it is not a screen to land on by accident."""
    settings = services.get_settings(db)
    if settings.setup_complete:
        return RedirectResponse("/settings", status_code=SEE_OTHER)
    try:
        me, son = _profile(db, PROFILE_ME), _profile(db, PROFILE_SON)
    except ProfilesMissing:
        return _needs_seeding(request)
    context = _context(request, "setup", settings, me, son, _state_form(settings, me, son))
    return templates.TemplateResponse(request, "setup.html", context)


@router.get("/settings", response_class=HTMLResponse, dependencies=[Depends(require_setup)])
def settings_page(request: Request, db: Annotated[Session, Depends(get_session)]) -> Response:
    settings = services.get_settings(db)
    try:
        me, son = _profile(db, PROFILE_ME), _profile(db, PROFILE_SON)
    except ProfilesMissing:
        return _needs_seeding(request)
    context = _context(
        request,
        "settings",
        settings,
        me,
        son,
        _state_form(settings, me, son),
        gaps=settings_ai.gap_choices_for_household(db),
    )
    return templates.TemplateResponse(request, "settings.html", context)


def _submitted(
    equipment: list[str],
    weights_available: str | None,
    days_per_week: str | None,
    session_minutes: str | None,
    has_overhead_anchor: str | None,
    push_son_to_garmin: str | None,
    display_unit: str | None,
    son_age: str | None,
    person_me: str | None,
    person_son: str | None,
    bodyweight_kg: str | None,
    son_enabled: str | None = None,
) -> dict[str, Any]:
    """The values the browser posted, kept exactly as typed so a rejected form redisplays them."""
    return {
        "son_enabled": ABSENT if son_enabled is None else son_enabled == "1",
        "son_age": ABSENT if son_age is None else son_age.strip(),
        "equipment": list(equipment),
        "weights_available": ABSENT if weights_available is None else weights_available.strip(),
        "days_per_week": _number(days_per_week),
        "session_minutes": _number(session_minutes),
        "has_overhead_anchor": ABSENT if has_overhead_anchor is None else has_overhead_anchor == "1",
        "push_son_to_garmin": ABSENT if push_son_to_garmin is None else push_son_to_garmin == "1",
        "display_unit": ABSENT if display_unit is None else display_unit.strip(),
        "person_me": ABSENT if person_me is None else person_me.strip(),
        "person_son": ABSENT if person_son is None else person_son.strip(),
        "bodyweight_kg": ABSENT if bodyweight_kg is None else bodyweight_kg.strip(),
    }


def _patches(form: dict[str, Any], mode: str) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """The settings patch and the per-profile patches this form describes.

    ``has_overhead_anchor`` is written to *both* profiles from one control: a beam in the doorway
    is a fact about the room, not about a person, and asking twice would let the two answers
    disagree (D-095). ``push_to_garmin`` on the son is deliberately not written here - D-021 makes
    the household setting the single source of that, read at payload-build time.

    Hiding the son costs him nothing precisely because of ``_present``: with the toggle off the
    form does not render his age, his slug or his Garmin switch, the browser therefore posts none
    of them, and a field the form did not send is dropped from the patch rather than written as
    empty. Turning him back on finds every one of those values where he left it (D-261).
    """
    settings_patch = _present(
        {
            "equipment": form["equipment"],
            "weights_available": form["weights_available"],
            "days_per_week": form["days_per_week"],
            "session_minutes": form["session_minutes"],
            "push_son_to_garmin": form["push_son_to_garmin"],
            "display_unit": form["display_unit"],
            "son_enabled": form["son_enabled"],
        }
    )
    if mode == "setup":
        settings_patch["setup_complete"] = True

    anchor = form["has_overhead_anchor"]
    me_patch = _present({"vitalforge_person": form["person_me"], "has_overhead_anchor": anchor})
    if mode == "settings":
        me_patch.update(_present({"bodyweight_kg": _decimal(form["bodyweight_kg"])}))
    son_patch = _present(
        {
            "age_years": _age(form["son_age"]),
            "vitalforge_person": form["person_son"],
            "has_overhead_anchor": anchor,
        }
    )
    return settings_patch, {PROFILE_ME: me_patch, PROFILE_SON: son_patch}


def _age_required(form: dict[str, Any], mode: str) -> dict[str, str]:
    """Setup will not complete without the son's age: it is the whole point of the screen.

    ``ABSENT`` is a truthy object, so the sentinel is checked by identity. Testing it for
    truthiness would quietly turn the one required field on the screen into an optional one.
    """
    if mode == "setup" and (form["son_age"] is ABSENT or not form["son_age"]):
        return {"age_years": AGE_HELP}
    return {}


def _render_form(request: Request, context: dict[str, Any], status: int) -> Response:
    """The form alone for HTMX, the whole page for a plain post. Never a separate error page."""
    if _is_htmx(request):
        body = templates.get_template("partials/settings_form.html").render(**context)
        return HTMLResponse(body, status_code=status)
    template = "setup.html" if context["mode"] == "setup" else "settings.html"
    return templates.TemplateResponse(request, template, context, status_code=status)


def _redisplay(form: dict[str, Any], stored: dict[str, Any]) -> dict[str, Any]:
    """What a rejected form shows: what was typed, over what is stored for anything not sent.

    Without this the ``ABSENT`` sentinel reaches the template and renders as a repr, so a form
    rejected for one bad field would come back with ``<object object at 0x...>`` in the boxes the
    post happened not to carry.
    """
    return {**stored, **{key: value for key, value in form.items() if value is not ABSENT}}


def _save(request: Request, db: Session, mode: str, form: dict[str, Any]) -> Response:
    settings = services.get_settings(db)
    if mode == "setup" and settings.setup_complete:
        # The first-run form must not be replayable against a set-up household (PRP-03 review, finding 3).
        return RedirectResponse("/settings", status_code=SEE_OTHER)
    try:
        me, son = _profile(db, PROFILE_ME), _profile(db, PROFILE_SON)
    except ProfilesMissing:
        return _needs_seeding(request)
    errors = _age_required(form, mode)
    settings_patch, profile_patches = _patches(form, mode)
    shown = _redisplay(form, _state_form(settings, me, son))
    if not errors:
        try:
            services.apply_household(db, settings_patch, profile_patches, library_bundle())
        except ValidationFailed as exc:
            errors = _form_errors(exc.as_dict())
        except RebuildUnavailable as exc:
            context = _context(request, mode, settings, me, son, shown, form_error=str(exc))
            return _render_form(request, context, UNAVAILABLE)
    if errors:
        return _render_form(request, _context(request, mode, settings, me, son, shown, errors=errors), UNPROCESSABLE)
    if mode == "setup":
        return _to("/today?profile=me", request)
    fresh = services.get_settings(db)
    if fresh.son_enabled != settings.son_enabled:
        # This save changed the page's *chrome*, not just its form. The screen posts through HTMX
        # and swaps `#settings-form` alone, so the tab strip in the header is outside the fragment
        # that comes back: without a real navigation the header would keep offering a "Son" tab
        # that solo mode has just hidden, and go on offering it until the next page load. A
        # redirect re-fetches the whole document, which is the only thing that can repaint a part
        # of the page this form does not own (D-267). The visible reload is the confirmation here,
        # which is why it costs the transient "Saved." line and does not need it.
        return _to("/settings", request)
    me, son = _profile(db, PROFILE_ME), _profile(db, PROFILE_SON)
    context = _context(request, mode, fresh, me, son, _state_form(fresh, me, son), saved=True)
    return _render_form(request, context, 200)


def _to(target: str, request: Request) -> Response:
    """A real navigation, whether the form posted through HTMX or on its own."""
    if _is_htmx(request):
        return Response(status_code=204, headers={"HX-Redirect": target})
    return RedirectResponse(target, status_code=SEE_OTHER)


@router.post("/setup")
def setup_save(
    request: Request,
    db: Annotated[Session, Depends(get_session)],
    equipment: Annotated[list[str], Form()] = [],  # noqa: B006 - FastAPI reads the default, never mutates it
    weights_available: Annotated[str | None, Form()] = None,
    days_per_week: Annotated[str | None, Form()] = None,
    session_minutes: Annotated[str | None, Form()] = None,
    has_overhead_anchor: Annotated[str | None, Form()] = None,
    push_son_to_garmin: Annotated[str | None, Form()] = None,
    display_unit: Annotated[str | None, Form()] = None,
    son_age: Annotated[str | None, Form()] = None,
    person_me: Annotated[str | None, Form()] = None,
    person_son: Annotated[str | None, Form()] = None,
) -> Response:
    """Every field arrives as text on purpose (PRP-03).

    Typed ``int`` parameters would be caught by FastAPI's validation handler, which answers in the
    JSON envelope - a form that loses its page to a JSON error is exactly what the wireframe says
    must never happen.
    """
    if services.get_settings(db).setup_complete:
        # The same guard ``GET /setup`` has, and it belongs here more than there: a *GET* on a
        # finished setup is a wasted page, a POST is a silent rewrite of the whole household from
        # a form filled in before any of the current settings existed. A browser's "resend?"
        # prompt, a bookmarked POST or a back-button replay is enough to fire it, and it would
        # come back 303 to Today as though nothing had happened (D-116).
        return RedirectResponse("/settings", status_code=SEE_OTHER)
    form = _submitted(
        equipment,
        weights_available,
        days_per_week,
        session_minutes,
        has_overhead_anchor,
        push_son_to_garmin,
        display_unit,
        son_age,
        person_me,
        person_son,
        None,
    )
    return _save(request, db, "setup", form)


@router.post("/settings", dependencies=[Depends(require_setup)])
def settings_save(
    request: Request,
    db: Annotated[Session, Depends(get_session)],
    equipment: Annotated[list[str], Form()] = [],  # noqa: B006
    weights_available: Annotated[str | None, Form()] = None,
    days_per_week: Annotated[str | None, Form()] = None,
    session_minutes: Annotated[str | None, Form()] = None,
    has_overhead_anchor: Annotated[str | None, Form()] = None,
    push_son_to_garmin: Annotated[str | None, Form()] = None,
    display_unit: Annotated[str | None, Form()] = None,
    son_age: Annotated[str | None, Form()] = None,
    person_me: Annotated[str | None, Form()] = None,
    person_son: Annotated[str | None, Form()] = None,
    bodyweight_kg: Annotated[str | None, Form()] = None,
    son_enabled: Annotated[str | None, Form()] = None,
) -> Response:
    form = _submitted(
        equipment,
        weights_available,
        days_per_week,
        session_minutes,
        has_overhead_anchor,
        push_son_to_garmin,
        display_unit,
        son_age,
        person_me,
        person_son,
        bodyweight_kg,
        son_enabled,
    )
    return _save(request, db, "settings", form)


@router.post("/settings/profiles/{profile_id}/kind", dependencies=[Depends(require_setup)])
def settings_kind(
    request: Request,
    profile_id: str,
    db: Annotated[Session, Depends(get_session)],
    kind: Annotated[str | None, Form()] = None,
    confirm: Annotated[str | None, Form()] = None,
) -> Response:
    """The Settings control behind D-099's explicit, confirmed, reversible change of rule set.

    A plain post and a full re-render on purpose: no HTMX swap can drop the confirm, and it works
    with JavaScript off. On success it redirects, so a refresh cannot replay the change.
    """
    try:
        services.set_profile_kind(db, profile_id, str(kind or ""), confirm == "1", library_bundle())
    except (ValidationFailed, RebuildUnavailable) as exc:
        settings = services.get_settings(db)
        try:
            me, son = _profile(db, PROFILE_ME), _profile(db, PROFILE_SON)
        except ProfilesMissing:
            return _needs_seeding(request)
        failed = isinstance(exc, RebuildUnavailable)
        context = _context(
            request,
            "settings",
            settings,
            me,
            son,
            _state_form(settings, me, son),
            errors=None if failed else _form_errors(exc.as_dict()),
            form_error=str(exc) if failed else None,
        )
        status = UNAVAILABLE if failed else UNPROCESSABLE
        return templates.TemplateResponse(request, "settings.html", context, status_code=status)
    return RedirectResponse("/settings", status_code=SEE_OTHER)


@router.post("/settings/weights/preview", response_class=HTMLResponse)
def weights_preview(
    request: Request,
    weights_available: Annotated[str | None, Form()] = None,
) -> Response:
    """The live parse under the box. Not gated: the box is on the Setup screen too.

    Reads nothing and writes nothing - it hands the text straight to PRP-01's parser and renders
    what came back, so there is no path from this endpoint to the database at all.
    """
    preview = services.weights_preview((weights_available or "").strip())
    body = templates.get_template("partials/weights_preview.html").render(request=request, preview=preview)
    return HTMLResponse(body)
