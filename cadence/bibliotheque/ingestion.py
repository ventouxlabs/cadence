"""Resolving the target profile, and running the import pipeline against it.

One module so that the JSON API and the Settings screen cannot disagree about what "import this
for the son" means. The single job here is to turn a profile id into an ``ImportTarget`` carrying
the profile's *real* band, equipment, bodyweight and anchor - the four facts PRP-08 risk 10 says
an incomplete validator call silently drops.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

from sqlmodel import Session

from cadence.bibliotheque import import_service
from cadence.bibliotheque.import_service import ImportTarget, Prepared
from cadence.profils.services import get_settings as stored_settings
from cadence.profils.settings import ProgramSettings
from cadence.profils.tables import PROFILE_ME, PROFILE_SON, Profile
from cadence.programme.bands import age_band
from cadence.schema.enums import AgeBand
from cadence.schema.exercise import Exercise
from cadence.schema.youth import YouthRuleSet
from cadence.seance.catalog import library_bundle

logger = logging.getLogger(__name__)

PROFILE_IDS: tuple[str, ...] = (PROFILE_ME, PROFILE_SON)
DEFAULT_PROFILE = PROFILE_ME


class UnknownProfile(LookupError):
    """The request named a profile that is not in this household."""


@dataclass(frozen=True, slots=True)
class Context:
    """Everything one import or generation needs from the database and the library."""

    profile: Profile
    target: ImportTarget
    settings: ProgramSettings
    catalog: Mapping[str, Exercise]
    # What the prompt may offer: seed exercises only, never an id an import chose (D-188).
    seed_catalog: Mapping[str, Exercise]
    youth_rules: Mapping[AgeBand, YouthRuleSet] | None
    rules: YouthRuleSet | None
    taken_workout_ids: frozenset[str]
    taken_exercise_ids: frozenset[str]


def normalise_profile_id(raw: object) -> str:
    """``me`` or ``son``. Anything else raises rather than defaulting.

    Defaulting a bad value to ``me`` is how a workout meant for the son gets validated against the
    adult rules (PRP-08 risk 11). An absent field defaults; a wrong one does not.
    """
    if raw is None:
        return DEFAULT_PROFILE
    key = str(raw).strip().lower()
    if key not in PROFILE_IDS:
        raise UnknownProfile(f"there is no profile {str(raw)[:32]!r}")
    return key


def build_context(session: Session, profile_id: str) -> Context:
    """Read the profile and the library once, so every check downstream sees the same facts."""
    profile = session.get(Profile, profile_id)
    if profile is None:
        raise UnknownProfile(f"there is no profile {profile_id!r}")
    settings = stored_settings(session)
    catalog = import_service.exercise_catalog(session)
    seed_ids = import_service.seed_exercise_ids(session)
    band = age_band(profile)
    bundle = library_bundle()
    youth_rules = bundle.youth_rules if bundle is not None else None
    target = ImportTarget(
        profile_id=profile.id,
        # Narrowed rather than trusted: the column is text, and the validator's own signature is
        # the only place the two legal values are named.
        profile_kind=cast(Literal["adult", "youth"], "youth" if profile.kind == "youth" else "adult"),
        age_band=band,
        equipment=tuple(settings.equipment),
        bodyweight_kg=profile.bodyweight_kg,
        has_overhead_anchor=bool(profile.has_overhead_anchor),
    )
    return Context(
        profile=profile,
        target=target,
        settings=settings,
        catalog=catalog,
        seed_catalog={key: value for key, value in catalog.items() if key in seed_ids},
        youth_rules=youth_rules,
        rules=youth_rules.get(band) if youth_rules is not None else None,
        taken_workout_ids=import_service.taken_workout_ids(session),
        taken_exercise_ids=import_service.taken_exercise_ids(session),
    )


def run(text: str, context: Context) -> Prepared:
    """The pipeline, against this profile's real facts. Writes nothing."""
    return import_service.prepare(
        text,
        target=context.target,
        catalog=context.catalog,
        youth_rules=context.youth_rules,
        taken_workout_ids=context.taken_workout_ids,
        taken_exercise_ids=context.taken_exercise_ids,
    )


def resolved_catalog(context: Context, prepared: Prepared) -> Mapping[str, Exercise]:
    """The catalog a *this* document's rows resolve against: the stored one plus its own inline
    definitions.

    ``Context.catalog`` is read before the document is, so it cannot contain an exercise the
    document brought with it. Rendering a preview or materialising a swap against it raises
    ``KeyError`` on the very row the inline definition existed for - which the Settings screen
    reported as "saved, but it could not be used for that day" (Codex finding 11).
    """
    if not prepared.inline_exercises:
        return context.catalog
    return {**context.catalog, **{item.id: item for item in prepared.inline_exercises}}


def stored_payload(prepared: Prepared, workout_id: str, context: Context) -> dict[str, Any]:
    """The success body of ``POST /api/import`` and ``POST /api/generate/accept``."""
    workout = prepared.workout
    assert workout is not None  # noqa: S101 - callers only reach this after ``prepared.ok``
    return {
        "workout_id": workout_id,
        "name": workout.name,
        "rows": len(workout.rows),
        "target_profile_kind": workout.target_profile_kind,
        "profile": context.profile.id,
        "warnings": list(prepared.warnings),
    }


def as_document(workout: Any) -> str:
    """Re-serialise a posted preview as text, so the accept path re-enters the same gate.

    **JSON, not YAML.** ``yaml.safe_dump`` emits ``&id001``/``*id001`` anchors whenever two fields
    happen to hold the same object, which Python's string interning makes common - and the very
    next gate rejects anchors. JSON is a subset of YAML, has no anchor syntax at all, and escapes
    a control character into something the post-parse scan still catches.
    """
    try:
        return json.dumps(workout)
    except (RecursionError, ValueError, TypeError):
        # Reached only if a depth guard upstream is ever removed. Answering with something the
        # pipeline will refuse is better than raising out of a route (Codex finding 9).
        return ""


def preview_payload(prepared: Prepared) -> dict[str, Any]:
    """The preview body: the document as it would be stored, and nothing stored."""
    workout = prepared.workout
    assert workout is not None  # noqa: S101 - callers only reach this after ``prepared.ok``
    return workout.model_dump(mode="json")


__all__ = [
    "DEFAULT_PROFILE",
    "PROFILE_IDS",
    "Context",
    "UnknownProfile",
    "as_document",
    "build_context",
    "resolved_catalog",
    "normalise_profile_id",
    "preview_payload",
    "run",
    "stored_payload",
]
