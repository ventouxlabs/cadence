"""Youth rule sets - the machine-readable form of ``docs/exercise-principles.md`` section 3.

Values live in ``library/youth_rules.yaml``, never in code: the band table is domain data and
must be editable without a deploy.
"""

from __future__ import annotations

from pydantic import ConfigDict, Field, RootModel, StrictBool, StrictFloat, StrictInt

from cadence.schema.base import CadenceModel, FrozenMap
from cadence.schema.enums import AgeBand, AssessmentId, ExerciseTag, GoalType, LoadType


class YouthRuleSet(CadenceModel):
    band: AgeBand
    allowed_load_types: tuple[LoadType, ...] = Field(min_length=1)
    max_load_kg_per_hand: StrictFloat | None = Field(default=None, ge=0, le=500)
    max_load_kg_per_implement: StrictFloat | None = Field(default=None, ge=0, le=500)
    # Fractions, not percents: principles section 3.2 writes 15% but the value stored is 0.15.
    max_load_pct_bw_per_hand: StrictFloat | None = Field(default=None, ge=0, le=1)
    max_load_pct_bw_per_implement: StrictFloat | None = Field(default=None, ge=0, le=1)
    rep_min_loaded: StrictInt | None = Field(default=None, ge=1, le=100)
    rep_max_loaded: StrictInt | None = Field(default=None, ge=1, le=100)
    rep_min_bodyweight: StrictInt = Field(ge=1, le=100)
    rep_max_bodyweight: StrictInt = Field(ge=1, le=100)
    min_rest_s_loaded: StrictInt = Field(ge=0, le=3600)
    max_session_minutes: StrictInt = Field(ge=1, le=120)
    max_exercises_per_session: StrictInt = Field(ge=1, le=60)
    max_sets_per_exercise: StrictInt = Field(ge=1, le=10)
    rpe_cap: StrictInt = Field(ge=1, le=10)
    allow_amrap: StrictBool
    allow_max_test: StrictBool
    banned_tags: tuple[ExerciseTag, ...] = Field(default_factory=tuple)
    banned_goal_types: tuple[GoalType, ...] = Field(default_factory=tuple)
    good_enough_done_after_n_exercises: StrictInt = Field(ge=1, le=60)
    assessment_caps: FrozenMap[AssessmentId, StrictInt] = Field(default_factory=dict, validate_default=True)


class YouthRules(RootModel[FrozenMap[AgeBand, YouthRuleSet]]):
    """Parsed shape of ``library/youth_rules.yaml``: one rule set per age band."""

    model_config = ConfigDict(frozen=True)

    def __getitem__(self, band: AgeBand) -> YouthRuleSet:
        return self.root[band]

    def __contains__(self, band: object) -> bool:
        return band in self.root

    def get(self, band: AgeBand) -> YouthRuleSet | None:
        return self.root.get(band)
