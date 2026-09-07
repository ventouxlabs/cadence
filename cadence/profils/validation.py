"""Validation for the Setup and Settings writes.

Every rejection in PRP-03 starts here, so the JSON API and the HTML form refuse exactly the same
values: one list of legal equipment ids, one age range, one set of session lengths. The API
answers 422 with the message and the form renders the same message under the field, which is why
errors carry the field name rather than only prose.

Nothing here writes. ``services`` decides what to do with a valid patch; this module only decides
whether the patch is one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from cadence.profils.settings import SESSION_MINUTES_CHOICES, SETTING_KEYS, ProgramSettings
from cadence.profils.tables import Profile
from cadence.schema.enums import Equipment

# The four ids of the whitelist, in the order the Setup screen ticks them.
LEGAL_EQUIPMENT: tuple[str, ...] = tuple(member.value for member in Equipment)
REQUIRED_EQUIPMENT = Equipment.BODYWEIGHT.value

# Principles section 3.1 bands start at "under 10" and stop at 17; 18 and 19 are accepted and
# clamp to `age_14_17` (D-066), so the youth range is 3..19. Below 3 or above 19 on a youth row is
# a typo, not a band. An adult's age is recorded but drives nothing, so it only has to be a
# plausible human age.
# A youth profile is still a youth profile at 18 and 19; ``age_band`` puts it in the loosest
# youth band and only an explicit change of ``kind`` moves it out of the youth rules (D-099). The
# range stays 3..19 because that is what the screen asks for, and PRP-03 acceptance test 10 pins
# 20 as a typo: recording a genuinely older person means marking the profile adult first, which is
# a deliberate act rather than a consequence of a number.
MIN_YOUTH_AGE = 3
MAX_YOUTH_AGE = 19
MAX_ADULT_AGE = 110

# A bodyweight outside this is a unit mix-up (pounds typed into a kilogram field, or a decimal
# point lost), and it feeds the percentage caps of principles section 3.3.
MIN_BODYWEIGHT_KG = 10.0
MAX_BODYWEIGHT_KG = 300.0

# A VitalForge person slug is a path segment - every route in `docs/vitalforge-contract.md` is
# `/p/{slug}/api/...` - so it is validated as one rather than as free text. The rule is VitalForge's
# own, copied from the contract (section 1, `shared/slugs.py`) rather than invented here, so a slug
# this app accepts is one that side will too. Blank is legal and means "not set" (D-017).
MAX_PERSON_SLUG = 32
_PERSON_SLUG = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?")

# Also VitalForge's own, from the same file: these shadow a real path segment under `/p/{slug}/`
# or collide with the sentinels its auth layer reserves. A slug that matches the pattern but names
# one of these routes a write at somebody else's endpoint rather than at a person (D-112).
RESERVED_PERSON_SLUGS: frozenset[str] = frozenset(
    {"api", "auth", "static", "health", "p", "new", "admin", "persons", "anonymous", "api-token"}
)

# `kind` is the authority for the youth rules (D-066), so it is not editable through any API:
# flipping it is how a youth profile would unlock kettlebells without ageing a day.
IMMUTABLE_PROFILE_FIELDS: frozenset[str] = frozenset({"id", "kind", "age_band", "age_recorded_on"})
PROFILE_FIELDS: tuple[str, ...] = (
    "display_name",
    "age_years",
    "vitalforge_person",
    "push_to_garmin",
    "bodyweight_kg",
    "has_overhead_anchor",
)


@dataclass(frozen=True, slots=True)
class FieldError:
    """One rejection, addressed to the field that caused it.

    ``scope`` names *whose* field it is. The Setup and Settings screens write both profiles from
    one form, so ``vitalforge_person`` genuinely appears twice on the page: without a scope, two
    bad slugs collapse to one message and the person is shown a single line for a section with two
    inputs, one of which may be fine.
    """

    field: str
    message: str
    scope: str = ""

    @property
    def key(self) -> str:
        """``vitalforge_person`` on its own, or ``son.vitalforge_person`` when it belongs to one."""
        return f"{self.scope}.{self.field}" if self.scope else self.field

    def within(self, scope: str) -> FieldError:
        """The same error, said to belong to ``scope``. Returns a new one; never writes through."""
        return FieldError(field=self.field, message=self.message, scope=scope)


class ValidationFailed(ValueError):
    """A patch that will not be written. Carries one entry per offending field."""

    def __init__(self, errors: tuple[FieldError, ...]) -> None:
        self.errors = errors
        super().__init__("; ".join(f"{item.key}: {item.message}" for item in errors))

    @classmethod
    def one(cls, field: str, message: str) -> ValidationFailed:
        return cls((FieldError(field, message),))

    def as_dict(self) -> dict[str, str]:
        """Error key to message, for rendering the form again with the errors in place."""
        return {item.key: item.message for item in self.errors}


def _unknown_keys(patch: dict[str, Any], legal: tuple[str, ...]) -> list[FieldError]:
    unknown = [key for key in patch if key not in legal]
    if not unknown:
        return []
    names = ", ".join(sorted(unknown))
    return [FieldError(names, f"not a setting this app has: {names}")]


def _check_equipment(value: Any) -> list[FieldError]:
    """The whitelist is membership, and bodyweight is not optional.

    D-068(b): a household without bodyweight loses the prelude and every substitute, so the plan
    it asks for has no rows at all and the builder refuses it. Refusing here instead means the
    person sees "bodyweight cannot be turned off" rather than a build error about a template.
    """
    if not isinstance(value, list | tuple):
        return [FieldError("equipment", "equipment must be a list of ids")]
    legal = ", ".join(LEGAL_EQUIPMENT)
    bad = [str(item) for item in value if str(item) not in LEGAL_EQUIPMENT]
    if bad:
        return [FieldError("equipment", f"{', '.join(sorted(bad))} is not equipment this app has; choose from {legal}")]
    if not value:
        return [FieldError("equipment", f"choose at least bodyweight; the four options are {legal}")]
    if REQUIRED_EQUIPMENT not in [str(item) for item in value]:
        return [FieldError("equipment", "bodyweight cannot be turned off: without it there is nothing to fall back to")]
    return []


def _check_days(value: Any) -> list[FieldError]:
    if not isinstance(value, int) or isinstance(value, bool) or not 2 <= value <= 6:
        return [FieldError("days_per_week", "days per week is between 2 and 6")]
    return []


def _check_minutes(value: Any) -> list[FieldError]:
    if not isinstance(value, int) or isinstance(value, bool) or value not in SESSION_MINUTES_CHOICES:
        choices = ", ".join(str(item) for item in SESSION_MINUTES_CHOICES)
        return [FieldError("session_minutes", f"session length is one of {choices} minutes")]
    return []


_SETTING_CHECKS = {
    "equipment": _check_equipment,
    "days_per_week": _check_days,
    "session_minutes": _check_minutes,
}


def validate_settings_patch(current: ProgramSettings, patch: dict[str, Any]) -> ProgramSettings:
    """The settings that ``patch`` would produce, or ``ValidationFailed`` naming every bad field.

    The hand-written checks run first so the message says "days per week is between 2 and 6"
    rather than pydantic's field prose; ``with_changes`` then re-validates the whole object, which
    is what catches a type nobody thought to check by hand.
    """
    errors = _unknown_keys(patch, SETTING_KEYS)
    for key, check in _SETTING_CHECKS.items():
        if key in patch:
            errors += check(patch[key])
    if errors:
        raise ValidationFailed(tuple(errors))
    try:
        return current.with_changes(**patch)
    except ValidationError as exc:
        raise ValidationFailed(tuple(_from_pydantic(exc))) from exc


def _from_pydantic(exc: ValidationError) -> list[FieldError]:
    """Pydantic's own rejections, addressed to a field so the form can put them under one."""
    errors: list[FieldError] = []
    for error in exc.errors():
        field = str(error["loc"][0]) if error["loc"] else "settings"
        errors.append(FieldError(field, str(error["msg"]).removeprefix("Value error, ")))
    return errors


def _check_age(profile: Profile, value: Any) -> list[FieldError]:
    if value is None:
        return []
    if not isinstance(value, int) or isinstance(value, bool):
        return [FieldError("age_years", "Enter the age in whole years, digits only.")]
    low, high = (MIN_YOUTH_AGE, MAX_YOUTH_AGE) if profile.kind == "youth" else (MIN_YOUTH_AGE, MAX_ADULT_AGE)
    if not low <= value <= high:
        return [FieldError("age_years", f"Enter an age between {low} and {high}.")]
    return []


def _check_bodyweight(value: Any) -> list[FieldError]:
    if value is None:
        return []
    if isinstance(value, bool) or not isinstance(value, int | float):
        return [FieldError("bodyweight_kg", "bodyweight is a number of kilograms")]
    if not MIN_BODYWEIGHT_KG <= float(value) <= MAX_BODYWEIGHT_KG:
        return [
            FieldError("bodyweight_kg", f"bodyweight is between {MIN_BODYWEIGHT_KG:g} and {MAX_BODYWEIGHT_KG:g} kg")
        ]
    return []


def check_person_slug(value: Any) -> list[FieldError]:
    """A VitalForge person slug, or blank.

    The slug is interpolated straight into a path - the contract's routes are all
    ``/p/{slug}/api/...`` - so ``..``, a slash or a space in this field is a request aimed at a
    URL nobody meant to call, and PRP-06 is where it would be sent. Blank stays legal and blank
    (D-017): Cadence never guesses a slug from a display name, so "not set yet" has to be
    expressible.
    """
    if not isinstance(value, str):
        return [FieldError("vitalforge_person", "vitalforge_person is text")]
    if value == "":
        return []
    if value in RESERVED_PERSON_SLUGS:
        return [FieldError("vitalforge_person", f"{value!r} is reserved by VitalForge; pick another slug")]
    if _PERSON_SLUG.fullmatch(value):
        return []
    return [
        FieldError(
            "vitalforge_person",
            "a VitalForge person is a short slug: lower-case letters, digits and hyphens, "
            f"up to {MAX_PERSON_SLUG} characters",
        )
    ]


def validate_profile_patch(profile: Profile, patch: dict[str, Any]) -> dict[str, Any]:
    """The fields of ``patch`` that may be written to ``profile``, or ``ValidationFailed``."""
    errors: list[FieldError] = []
    blocked = sorted(set(patch) & IMMUTABLE_PROFILE_FIELDS)
    if blocked:
        names = ", ".join(blocked)
        errors.append(FieldError(names, f"{names} cannot be changed through this API"))
    errors += _unknown_keys({k: v for k, v in patch.items() if k not in IMMUTABLE_PROFILE_FIELDS}, PROFILE_FIELDS)
    if "age_years" in patch:
        errors += _check_age(profile, patch["age_years"])
    if "bodyweight_kg" in patch:
        errors += _check_bodyweight(patch["bodyweight_kg"])
    for flag in ("push_to_garmin", "has_overhead_anchor"):
        if flag in patch and not isinstance(patch[flag], bool):
            errors.append(FieldError(flag, f"{flag} is on or off"))
    if "display_name" in patch and not isinstance(patch["display_name"], str):
        errors.append(FieldError("display_name", "display_name is text"))
    if "vitalforge_person" in patch:
        errors += check_person_slug(patch["vitalforge_person"])
    if errors:
        raise ValidationFailed(tuple(errors))
    return dict(patch)
