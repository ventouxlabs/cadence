"""Age bands, effective load caps and block-week arithmetic.

``effective_cap`` is re-exported from the validator rather than reimplemented: the program engine
clamps to exactly the number the validator would reject above, and two copies of that formula is
one copy too many.
"""

from __future__ import annotations

from cadence.profils.tables import Profile
from cadence.schema.enums import AgeBand, LoadUnit
from cadence.schema.youth import YouthRuleSet
from cadence.validateur.youth_rules import effective_cap as _validator_effective_cap
from cadence.validateur.youth_rules import usable_bodyweight

BLOCK_WEEKS = 4

YOUTH_BANDS: tuple[AgeBand, ...] = (AgeBand.U10, AgeBand.AGE_10_13, AgeBand.AGE_14_17)
STRICTEST_YOUTH_BAND = AgeBand.U10


def band_for_age(age_years: int | None) -> AgeBand:
    """Principles section 3.1, with section 3.8's strictest-band default for an unknown age."""
    if age_years is None:
        return AgeBand.U10
    if age_years < 10:
        return AgeBand.U10
    if age_years <= 13:
        return AgeBand.AGE_10_13
    if age_years <= 17:
        return AgeBand.AGE_14_17
    return AgeBand.ADULT


def age_band(profile: Profile) -> AgeBand:
    """The band this profile is evaluated against (D-023).

    The parent profile is always ``adult`` (section 3.1) whatever its age says. A youth profile
    with no recorded age is ``u10`` (section 3.8), and so is a youth profile whose age computes to
    ``adult`` - the same coercion the validator's ``effective_band`` makes for the same situation
    (D-037b). A profile marked youth never leaves the youth rules by arithmetic, and when the two
    facts on it disagree the strictest band wins rather than the loosest.
    """
    if profile.kind != "youth":
        return AgeBand.ADULT
    band = band_for_age(profile.age_years)
    return STRICTEST_YOUTH_BAND if band is AgeBand.ADULT else band


def effective_cap(rules: YouthRuleSet, load_unit: LoadUnit, bodyweight_kg: float | None) -> float | None:
    """Lower of the absolute and percentage caps for this unit (principles section 3.3).

    ``None`` means "this band does not cap this unit" - the adult rule set, or a unit the band
    table has no column for. It never means "unknown bodyweight": an unknown bodyweight falls
    back to the absolute cap, which is why the argument order puts the rules first.
    """
    return _validator_effective_cap(load_unit, rules, bodyweight_kg)


def week_of_block(completed_sessions_in_block: int, days_per_week: int) -> int:
    """Section 5.7: the week advances on completed sessions, not on dates. Capped at 4."""
    if days_per_week < 1:
        raise ValueError("days_per_week must be at least 1")
    completed = max(completed_sessions_in_block, 0)
    return min(completed // days_per_week + 1, BLOCK_WEEKS)


__all__ = [
    "BLOCK_WEEKS",
    "STRICTEST_YOUTH_BAND",
    "YOUTH_BANDS",
    "age_band",
    "band_for_age",
    "effective_cap",
    "usable_bodyweight",
    "week_of_block",
]
