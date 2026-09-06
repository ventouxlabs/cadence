"""The per-profile build context shared by the compiler's stages.

Separated from ``materialise`` so the prescription stage can take a context without importing the
module that calls it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cadence.bibliotheque.bundle import LibraryBundle
from cadence.bibliotheque.template import ColumnSpec, RowRole
from cadence.profils.settings import ProgramSettings
from cadence.profils.tables import Profile
from cadence.programme import substitution as sub
from cadence.programme.bands import age_band
from cadence.programme.ladder import WeightsAvailable, parse_weights_available
from cadence.schema.enums import Measure
from cadence.schema.exercise import Exercise
from cadence.schema.youth import YouthRuleSet

MEASURE_FIELD: dict[Measure, str] = {
    Measure.REPS: "reps",
    Measure.SECONDS: "seconds",
    Measure.METERS: "meters",
    Measure.STEPS: "steps",
}


@dataclass(frozen=True, slots=True)
class BuildContext:
    """One profile's view of the library, settings and band rules."""

    profile: Profile
    settings: ProgramSettings
    library: LibraryBundle
    band: sub.BandContext
    weights: WeightsAvailable
    rules: YouthRuleSet | None
    effective_minutes: int

    @property
    def is_youth(self) -> bool:
        return self.band.is_youth


@dataclass(frozen=True, slots=True)
class Candidate:
    """A template row that survived substitution, with its weight already settled.

    ``index`` is the row's place in the template's own priority order, which is what section 6.4's
    truncation and every tie-break read.
    """

    index: int
    draft: Draft
    load_kg: float | None
    cap_kg: float | None


@dataclass(frozen=True, slots=True)
class Draft:
    """A row after substitution, before the week scheme and the load are applied."""

    role: RowRole
    origin_id: str
    exercise: Exercise
    column: ColumnSpec
    notes: tuple[str, ...]
    assessment_id: Any = None


def make_context(profile: Profile, settings: ProgramSettings, library: LibraryBundle) -> BuildContext:
    """Everything the compiler needs about one profile, resolved once."""
    band = age_band(profile)
    kind: Any = "youth" if profile.kind == "youth" else "adult"
    rules = library.youth_rules.get(band) if kind == "youth" else None
    minutes = settings.session_minutes
    if rules is not None:
        # A household with a u10 son and a 45-minute setting is legal; his sessions run at 20.
        minutes = min(minutes, rules.max_session_minutes)
    return BuildContext(
        profile=profile,
        settings=settings,
        library=library,
        band=sub.BandContext(
            kind=kind,
            band=band,
            rules=rules,
            equipment=frozenset(settings.equipment),
            has_overhead_anchor=bool(profile.has_overhead_anchor),
            bodyweight_kg=profile.bodyweight_kg,
            library=library,
        ),
        weights=parse_weights_available(settings.weights_available),
        rules=rules,
        effective_minutes=minutes,
    )
