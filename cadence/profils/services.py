"""The services behind Setup, Settings and the two profile endpoints (D-023).

PRP-01 owns the ``profile`` and ``setting`` tables and ``age_band()``; this module owns reading and
writing them. Everything here is immutable in the sense the architecture asks for: an update builds
a new ``ProgramSettings`` and returns it rather than writing through the one it was handed.

The rebuild rule lives in ``rebuild.py``; what is *worth* rebuilding lives here, in
``REBUILD_ON_SETTING`` and ``REBUILD_ON_PROFILE``. Those two tuples are the whole answer to "why
did my plan change", and a key not in them can never trigger one - which is what keeps changing
the display unit from re-planning the block (PRP-03 acceptance test 12).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlmodel import Session, select

from cadence.bibliotheque.bundle import LibraryBundle
from cadence.profils.rebuild import rebuild_programs
from cadence.profils.settings import DEFAULT_SETTINGS, SETTING_KEYS, ProgramSettings, settings_from_rows
from cadence.profils.tables import Profile, Setting
from cadence.profils.validation import (
    FieldError,
    ValidationFailed,
    validate_profile_patch,
    validate_settings_patch,
)
from cadence.programme.bands import age_band
from cadence.programme.errors import ProgramBuildError
from cadence.programme.ladder import parse_weights_available
from cadence.schema.enums import AgeBand
from cadence.schema.youth import YouthRuleSet

# Changing one of these changes what the plan prescribes, so the queue ahead is re-planned.
REBUILD_ON_SETTING: frozenset[str] = frozenset({"days_per_week", "session_minutes", "equipment", "weights_available"})
# And these are the facts on a profile that the load caps and substitutions are computed from.
REBUILD_ON_PROFILE: frozenset[str] = frozenset({"age_years", "bodyweight_kg", "has_overhead_anchor"})

logger = logging.getLogger(__name__)

PROFILE_KINDS: tuple[str, ...] = ("adult", "youth")


class RebuildUnavailable(RuntimeError):
    """A change needs the block re-planned and the exercise library will not load.

    Answered as a 503, never as a 200 with an empty ``rebuilt`` list. The rows already in
    ``rows_json`` were built for the *previous* band, equipment and ladder; reporting success
    while leaving them there is how a child keeps being handed last month's loads under this
    month's settings, and the screen would say the change had been made.
    """


@dataclass(frozen=True, slots=True)
class SettingsUpdate:
    """What a write produced: the new settings, and which profiles were re-planned.

    ``update_settings`` returns this rather than the bare ``ProgramSettings`` of the PRP's sketch,
    because ``PUT /api/settings`` has to answer with ``{"rebuilt": [...]}`` and the rebuild happens
    inside the same transaction as the write (D-092).
    """

    settings: ProgramSettings
    rebuilt: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class HouseholdUpdate:
    """One Setup or Settings save: the settings, the profiles it touched, and the re-plan it caused."""

    settings: ProgramSettings
    profiles: tuple[Profile, ...] = ()
    rebuilt: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProfileUpdate:
    """The stored profile after a write, and which profiles were re-planned because of it."""

    profile: Profile
    rebuilt: tuple[str, ...] = ()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def get_settings(session: Session) -> ProgramSettings:
    """The household settings, or the shipped defaults when nothing is stored yet."""
    rows = session.exec(select(Setting)).all()
    if not rows:
        return DEFAULT_SETTINGS
    return settings_from_rows({row.key: row.value_json for row in rows})


def _write_settings(session: Session, settings: ProgramSettings) -> None:
    stamp = _now()
    for key, value in settings.as_rows().items():
        row = session.get(Setting, key)
        if row is None:
            session.add(Setting(key=key, value_json=value, updated_at=stamp))
        elif row.value_json != value:
            row.value_json = value
            row.updated_at = stamp
    session.flush()


def _changed_keys(before: ProgramSettings, after: ProgramSettings) -> set[str]:
    old, new = before.model_dump(mode="json"), after.model_dump(mode="json")
    return {key for key in SETTING_KEYS if old.get(key) != new.get(key)}


def update_settings(
    session: Session,
    patch: dict[str, Any],
    library: LibraryBundle | None = None,
) -> SettingsUpdate:
    """Validate, write, and re-plan if the change was one that changes the plan.

    One transaction: if the new settings describe a household whose plan will not build, the
    settings are rolled back with it, so the app never sits on a configuration whose Today screen
    is a stack trace.
    """
    before = get_settings(session)
    candidate = validate_settings_patch(before, patch)
    changed = _changed_keys(before, candidate)
    _write_settings(session, candidate)

    rebuilt: tuple[str, ...] = ()
    if changed & REBUILD_ON_SETTING:
        rebuilt = _rebuild_or_refuse(session, library, f"settings changed: {', '.join(sorted(changed))}")
    if "push_son_to_garmin" in changed and not candidate.push_son_to_garmin:
        # Switching the son's Garmin push off has to reach the queue, not just the next payload.
        # A job queued while it was on carries ``push_to_garmin: true`` in its frozen body, so a
        # session that timed out on Monday would still file itself under the parent's account on
        # Wednesday — after the household had said no. Same transaction as the setting, so the
        # answer and the queue can never disagree (D-139).
        _withdraw_queued_youth_pushes(session)
    session.commit()
    return SettingsUpdate(settings=candidate, rebuilt=rebuilt)


def _withdraw_queued_youth_pushes(session: Session) -> None:
    """Cancel queued Garmin pushes for the son. Imported here to keep the dependency one-way."""
    from cadence.vitalforge.writeback import cancel_youth_pushes

    cancel_youth_pushes(session)


def _build_message(exc: ProgramBuildError) -> str:
    """A build failure said in the words of the setting that caused it, never as a stack trace."""
    return f"these settings do not describe a plan that can be built: {exc}"


def get_profile(session: Session, profile_id: str) -> Profile | None:
    return session.get(Profile, profile_id)


def update_profile(
    session: Session,
    profile_id: str,
    patch: dict[str, Any],
    library: LibraryBundle | None = None,
) -> ProfileUpdate:
    """Write the fields of ``patch`` this API allows, recompute the band, and re-plan if needed.

    ``age_recorded_on`` is stamped on every age change so PRP-07's 28-day retest cadence and any
    future birthday roll have a date to work from, and so "he was 12 when we set this up" stays
    answerable a year later.
    """
    profile = session.get(Profile, profile_id)
    if profile is None:
        raise ValidationFailed.one("id", f"there is no profile {profile_id!r}")
    changed = _write_profile(session, profile, validate_profile_patch(profile, patch))
    session.flush()

    rebuilt: tuple[str, ...] = ()
    if changed & REBUILD_ON_PROFILE:
        reason = f"{profile_id} changed: {', '.join(sorted(changed & REBUILD_ON_PROFILE))}"
        rebuilt = _rebuild_or_refuse(session, library, reason, (profile_id,))
    session.commit()
    session.refresh(profile)
    return ProfileUpdate(profile=profile, rebuilt=rebuilt)


def apply_household(
    session: Session,
    settings_patch: dict[str, Any],
    profile_patches: dict[str, dict[str, Any]],
    library: LibraryBundle | None = None,
) -> HouseholdUpdate:
    """One screen, one save: settings and both profiles written together, or neither written.

    The form asks about the household and about two people at once, so validating and writing them
    in three separate transactions would leave a rejected age sitting next to an accepted
    days-per-week. Everything is checked first and every failure is collected, so the form comes
    back with all its errors at once rather than one per round trip.
    """
    errors: list[FieldError] = []
    before = get_settings(session)
    candidate = before
    try:
        candidate = validate_settings_patch(before, settings_patch)
    except ValidationFailed as exc:
        errors += list(exc.errors)

    accepted: list[tuple[Profile, dict[str, Any]]] = []
    for profile_id, patch in profile_patches.items():
        profile = session.get(Profile, profile_id)
        if profile is None:
            errors.append(FieldError("id", f"there is no profile {profile_id!r}"))
            continue
        try:
            accepted.append((profile, validate_profile_patch(profile, patch)))
        except ValidationFailed as exc:
            # Scoped to the profile it came from: this screen writes two of them, so an unscoped
            # `vitalforge_person` would put one message under a section holding two inputs.
            errors += [item.within(profile_id) for item in exc.errors]
    if errors:
        raise ValidationFailed(tuple(errors))

    changed = _changed_keys(before, candidate)
    _write_settings(session, candidate)
    touched: list[Profile] = []
    replan: set[str] = set()
    for profile, fields in accepted:
        if _write_profile(session, profile, fields) & REBUILD_ON_PROFILE:
            replan.add(profile.id)
        touched.append(profile)
    session.flush()

    rebuilt: tuple[str, ...] = ()
    wanted = None if changed & REBUILD_ON_SETTING else tuple(sorted(replan))
    if changed & REBUILD_ON_SETTING or replan:
        rebuilt = _rebuild_or_refuse(session, library, "the settings screen was saved", wanted)
    session.commit()
    return HouseholdUpdate(settings=candidate, profiles=tuple(touched), rebuilt=rebuilt)


def _write_profile(session: Session, profile: Profile, fields: dict[str, Any]) -> set[str]:
    """Write ``fields`` onto ``profile`` and restamp the band. ``kind`` is never touched here.

    An age is a fact about a person, not a decision about which rules protect them (D-099). A
    mistyped 18 on the son's profile must cost nothing more than a corrected number, so nothing in
    this function can move a profile out of the youth rules; only ``set_profile_kind`` does that,
    and only when asked to in as many words.
    """
    changed = {key for key, value in fields.items() if getattr(profile, key) != value}
    for key, value in fields.items():
        setattr(profile, key, value)
    if "age_years" in changed:
        profile.age_recorded_on = date.today().isoformat()
    profile.age_band = age_band(profile).value
    session.add(profile)
    return changed


def set_profile_kind(
    session: Session,
    profile_id: str,
    kind: str,
    confirm: bool,
    library: LibraryBundle | None = None,
) -> ProfileUpdate:
    """Move a profile between the youth and adult rule sets. Deliberate, confirmed, reversible.

    This is the only way ``kind`` ever changes, and it is why the ordinary profile PUT refuses the
    field. It goes both ways on purpose: a parent who marks a profile adult by mistake has to be
    able to put it back, and a control that only travels in the direction that removes protections
    is not a control, it is a trapdoor.
    """
    if kind not in PROFILE_KINDS:
        raise ValidationFailed.one("kind", f"kind is one of {', '.join(PROFILE_KINDS)}")
    profile = session.get(Profile, profile_id)
    if profile is None:
        raise ValidationFailed.one("id", f"there is no profile {profile_id!r}")
    if not confirm:
        raise ValidationFailed.one(
            "confirm",
            f"changing which rules apply to {profile.display_name} has to be confirmed",
        )
    if profile.kind == kind:
        return ProfileUpdate(profile=profile)

    logger.info("profile %s moves from the %s rules to the %s rules", profile_id, profile.kind, kind)
    profile.kind = kind
    profile.age_band = age_band(profile).value
    session.add(profile)
    session.flush()
    # `kind` picks the templates a block is built from, so the plan is not the same plan any more.
    rebuilt = _rebuild_or_refuse(session, library, f"{profile_id} moved to the {kind} rules", (profile_id,))
    session.commit()
    session.refresh(profile)
    return ProfileUpdate(profile=profile, rebuilt=rebuilt)


def _rebuild_or_refuse(
    session: Session,
    library: LibraryBundle | None,
    reason: str,
    profile_ids: tuple[str, ...] | None = None,
) -> tuple[str, ...]:
    """Re-plan, or refuse the whole change because the library cannot be read.

    Never returns an empty list to mean "the library is missing". A caller that cannot tell those
    two apart reports success for a change that did not reach a single row.
    """
    if library is None:
        session.rollback()
        raise RebuildUnavailable(
            "the exercise library will not load, so the plan cannot be rebuilt; "
            "this change was not saved rather than leaving the previous plan in place"
        )
    try:
        return tuple(rebuild_programs(session, library, reason, profile_ids=profile_ids))
    except ProgramBuildError as exc:
        session.rollback()
        raise ValidationFailed.one("weights_available", _build_message(exc)) from exc


def refresh_age_bands(session: Session) -> int:
    """Recompute every profile's cached band. Run at startup (PRP-03 risk 3).

    A son who turns fourteen between two deploys would otherwise keep ``age_10_13`` until somebody
    happened to save his profile, and the band is what the load caps are read from.
    """
    changed = 0
    for profile in session.exec(select(Profile)).all():
        band = age_band(profile).value
        if profile.age_band != band:
            profile.age_band = band
            session.add(profile)
            changed += 1
    if changed:
        session.commit()
    return changed


def youth_ruleset_for(profile: Profile, library: LibraryBundle) -> YouthRuleSet | None:
    """The rule set this profile is judged against, or ``None`` when the table has no such band."""
    return library.youth_rules.get(age_band(profile))


def has_overhead_anchor(profile: Profile) -> bool:
    """Whether a pull-up bar or beam is available to this profile (principles section 1.7).

    A profile setting and never an equipment id: it is a property of the room, not of the
    household's kit, and it is why the dead hang records ``unavailable`` rather than a gap (D-019).
    """
    return bool(profile.has_overhead_anchor)


def bodyweight_kg(profile: Profile) -> float | None:
    """The bodyweight the percentage caps are computed from, or ``None`` (D-050)."""
    return profile.bodyweight_kg


def display_unit(settings: ProgramSettings) -> str:
    """``kg`` or ``lb``. Rendering only; storage is kilograms everywhere (D-090)."""
    return settings.display_unit


def weights_preview(text: str) -> dict[str, Any]:
    """What the live preview under the ``weights_available`` box shows.

    The parser is PRP-01's and is never given a second implementation here: an unreadable token is
    ignored with a warning naming it, never guessed at, and never fatal (principles section 8.2).
    """
    parsed = parse_weights_available(text)
    ladders = [
        {
            "load_type": load_type.value,
            "rungs": len(rungs),
            "min_kg": min(rungs) if rungs else None,
            "max_kg": max(rungs) if rungs else None,
        }
        for load_type, rungs in sorted(parsed.ladders.items(), key=lambda item: item[0].value)
    ]
    return {"ladders": ladders, "bench": parsed.bench, "warnings": list(parsed.warnings)}


def settings_payload(settings: ProgramSettings) -> dict[str, Any]:
    """The ``GET /api/settings`` body: every stored key plus the parser's warnings."""
    payload = json.loads(json.dumps(settings.model_dump(mode="json")))
    payload["weights_warnings"] = weights_preview(settings.weights_available)["warnings"]
    return payload


def profile_payload(profile: Profile) -> dict[str, Any]:
    """The stored profile, including the derived band."""
    return {
        "id": profile.id,
        "display_name": profile.display_name,
        "kind": profile.kind,
        "age_years": profile.age_years,
        "age_recorded_on": profile.age_recorded_on,
        "age_band": age_band(profile).value,
        "vitalforge_person": profile.vitalforge_person,
        "push_to_garmin": profile.push_to_garmin,
        "bodyweight_kg": profile.bodyweight_kg,
        "has_overhead_anchor": profile.has_overhead_anchor,
    }


__all__ = [
    "AgeBand",
    "HouseholdUpdate",
    "ProfileUpdate",
    "RebuildUnavailable",
    "apply_household",
    "SettingsUpdate",
    "age_band",
    "bodyweight_kg",
    "display_unit",
    "get_profile",
    "get_settings",
    "has_overhead_anchor",
    "profile_payload",
    "refresh_age_bands",
    "set_profile_kind",
    "settings_payload",
    "update_profile",
    "update_settings",
    "weights_preview",
    "youth_ruleset_for",
]
