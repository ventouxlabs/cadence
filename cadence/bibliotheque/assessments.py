"""``library/assessments.yaml`` - the four blocks of principles section 7.

``tests`` parses into PRP-00's ``AssessmentSpec``. The other three have no PRP-00 model and are
this PRP's own; PRP-07 reads all four, PRP-01 only loads and validates them.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, StrictBool, StrictFloat, StrictInt, model_validator
from pydantic_core import PydanticCustomError

from cadence.schema.assessment import AssessmentSpec
from cadence.schema.base import CadenceModel, FrozenMap, Slug
from cadence.schema.enums import AgeBand, AssessmentId, DayType

YOUTH_BANDS: tuple[AgeBand, ...] = (AgeBand.U10, AgeBand.AGE_10_13, AgeBand.AGE_14_17)


class AdultTier(CadenceModel):
    """Section 7.3. A measurement below ``ok_min`` is a gap; ``ok_min`` is the challenge target."""

    low_max: StrictFloat
    ok_min: StrictFloat
    ok_max: StrictFloat
    strong_min: StrictFloat

    @model_validator(mode="after")
    def _ordered(self) -> AdultTier:
        if not self.low_max < self.ok_min <= self.ok_max < self.strong_min:
            raise PydanticCustomError("tier_order", "tiers must read low_max < ok_min <= ok_max < strong_min")
        return self


class YouthTarget(CadenceModel):
    """Section 7.4. No "low" tier for a youth profile - only "not yet" and "got it"."""

    targets: FrozenMap[AgeBand, StrictInt]
    cap: StrictInt = Field(ge=1)
    # Never mentions body, weight or appearance (section 3.5).
    phrasing: str = Field(min_length=1, max_length=120)

    @model_validator(mode="after")
    def _every_youth_band(self) -> YouthTarget:
        missing = [band.value for band in YOUTH_BANDS if band not in self.targets]
        if missing:
            raise PydanticCustomError(
                "youth_target_band_missing",
                "youth targets must name every youth band; missing {missing}",
                {"missing": ", ".join(missing)},
            )
        return self

    def for_band(self, band: AgeBand) -> int | None:
        return self.targets.get(band)


class GapRow(CadenceModel):
    """Section 7.7 - the row a gap inserts into the program."""

    exercise: Slug | None = None
    sets: StrictInt | None = Field(default=None, ge=1, le=10)
    reps: StrictInt | None = Field(default=None, ge=1, le=100)
    seconds: StrictInt | None = Field(default=None, ge=1, le=3600)
    reps_pct: StrictFloat | None = Field(default=None, gt=0, le=1)
    seconds_offset: StrictInt | None = Field(default=None, ge=-120, le=120)
    frequency: StrictInt = Field(ge=1, le=7)
    day_types: tuple[DayType, ...] = Field(default_factory=tuple)
    append_to_prelude: StrictBool = False
    extra_carry_set: StrictBool = False


GapId = Literal[
    "push_up_max",
    "dead_hang_s",
    "plank_s",
    "wall_angel_reach",
    "goblet_squat_quality",
    "farmer_carry_s",
    "body_comp",
]


class AssessmentLibrary(CadenceModel):
    """The parsed ``library/assessments.yaml``."""

    version: StrictInt = Field(default=1, ge=1, le=1)
    kind: Literal["assessments"] = "assessments"
    tests: tuple[AssessmentSpec, ...] = Field(min_length=1)
    adult_tiers: FrozenMap[AssessmentId, AdultTier]
    youth_targets: FrozenMap[AssessmentId, YouthTarget]
    gap_rows: FrozenMap[GapId, GapRow]

    @model_validator(mode="after")
    def _covers_every_test(self) -> AssessmentLibrary:
        ids = {spec.id for spec in self.tests}
        expected = set(AssessmentId)
        if ids != expected:
            missing = ", ".join(sorted(t.value for t in expected - ids)) or "none"
            extra = ", ".join(sorted(t.value for t in ids - expected)) or "none"
            raise PydanticCustomError(
                "assessment_test_set",
                "tests must be exactly the six of principles section 7.1; missing {missing}, unexpected {extra}",
                {"missing": missing, "extra": extra},
            )
        for block, label in ((self.adult_tiers, "adult_tiers"), (self.youth_targets, "youth_targets")):
            absent = sorted(t.value for t in expected if t not in block)
            if absent:
                raise PydanticCustomError(
                    "assessment_block_incomplete",
                    "{label} must cover every test; missing {absent}",
                    {"label": label, "absent": ", ".join(absent)},
                )
        return self

    def spec(self, test_id: AssessmentId) -> AssessmentSpec | None:
        return next((spec for spec in self.tests if spec.id is test_id), None)

    def youth_prescription(self, test_id: AssessmentId, band: AgeBand) -> int | None:
        """The band's fun target for a measured test, or ``None`` for a self-rated one.

        Section 7.4's cap column exceeds ``u10``'s bodyweight rep ceiling for ``push_up_max``,
        so the band target is what a youth row is prescribed at, never the cap (D-062).
        """
        spec = self.spec(test_id)
        if spec is None or spec.self_rated:
            return None
        target = self.youth_targets[test_id]
        return target.for_band(band)
