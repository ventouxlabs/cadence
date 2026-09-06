"""The workout-template layer - what ``library/workouts/*.yaml`` actually parses into.

PRP-00's ``Workout`` / ``WorkoutRow`` are concrete: ``load_kg`` is a number, there is no ``role``,
no ``per_side``, no per-profile column, and ``extra="forbid"`` rejects all of them. A seed workout
has no concrete load until a profile and a block week are applied, so it parses into these models
instead and ``cadence.programme.materialise`` compiles one into a ``Workout``.

Every key here is resolved at compile time and none of them reaches a ``WorkoutRow``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, StrictBool, StrictFloat, StrictInt, model_validator
from pydantic_core import PydanticCustomError

from cadence.schema.base import CadenceModel, Slug
from cadence.schema.enums import AssessmentId, DayType

LoadMode = Literal["bodyweight", "ladder", "fixed_kg"]
RowRole = Literal["main", "secondary", "finisher", "prelude", "assessment"]
ProfileKind = Literal["adult", "youth"]

MEASURE_KEYS: tuple[str, ...] = ("reps", "seconds", "meters", "steps")


class LoadRule(CadenceModel):
    """How a row's week-1 load is chosen before rounding and youth clamping."""

    mode: LoadMode
    # An absolute week-1 target, e.g. the 24 kg bell of principles section 10.5.
    kg: StrictFloat | None = Field(default=None, ge=0, le=500)
    # A fraction of the heaviest rung the household owns. Section 10 names no starting weights and
    # there is no previous block to read one from, so the seed scales to whatever is on the shelf
    # rather than hard-coding this household's dumbbells (D-055).
    pct_of_heaviest: StrictFloat | None = Field(default=None, gt=0, le=1)
    # A fraction of another row's resolved load ("match press load", "~60 % of bench load").
    relative_to: Slug | None = None
    pct: StrictFloat | None = Field(default=None, gt=0, le=3)
    rung_tolerance: StrictInt = Field(default=0, ge=0, le=5)
    fallback_exercise: Slug | None = None
    prefer: Literal["heaviest_rung"] | None = None
    # A hard ceiling applied before the youth cap, not instead of it.
    max_kg: StrictFloat | None = Field(default=None, ge=0, le=500)

    @model_validator(mode="after")
    def _coherent(self) -> LoadRule:
        if self.mode == "fixed_kg" and self.kg is None:
            raise PydanticCustomError("load_rule_needs_kg", "mode 'fixed_kg' needs a kg value")
        if self.pct is not None and self.relative_to is None:
            raise PydanticCustomError(
                "load_rule_pct_without_relative_to",
                "pct is a fraction of another row's load and needs relative_to; "
                "use pct_of_heaviest for a fraction of the ladder",
            )
        return self


BODYWEIGHT_RULE = LoadRule(mode="bodyweight")


class ColumnSpec(CadenceModel):
    """One profile kind's prescription for a shared row (principles section 10.9)."""

    sets: StrictInt = Field(ge=1, le=10)
    reps: StrictInt | None = Field(default=None, ge=1, le=100)
    seconds: StrictInt | None = Field(default=None, ge=1, le=3600)
    meters: StrictFloat | None = Field(default=None, ge=1, le=1000)
    steps: StrictInt | None = Field(default=None, ge=1, le=2000)
    per_side: StrictBool = False
    rest_s: StrictInt = Field(ge=0, le=3600)
    rpe_target: StrictInt | None = Field(default=None, ge=1, le=10)
    amrap: StrictBool = False
    cue_override: str | None = Field(default=None, max_length=120)
    load_rule: LoadRule = BODYWEIGHT_RULE
    u10_sub: Slug | None = None

    @model_validator(mode="after")
    def _exactly_one_measure(self) -> ColumnSpec:
        populated = [name for name in MEASURE_KEYS if getattr(self, name) is not None]
        if len(populated) != 1:
            raise PydanticCustomError(
                "measure_conflict",
                "exactly one of reps/seconds/meters/steps must be set, got {count}",
                {"count": len(populated)},
            )
        return self


class AnchorAlt(CadenceModel):
    """The row this one becomes when the profile does have an overhead anchor (section 10.4)."""

    exercise: Slug
    sets: StrictInt | None = Field(default=None, ge=1, le=10)
    reps: StrictInt | None = Field(default=None, ge=1, le=100)
    seconds: StrictInt | None = Field(default=None, ge=1, le=3600)
    meters: StrictFloat | None = Field(default=None, ge=1, le=1000)
    steps: StrictInt | None = Field(default=None, ge=1, le=2000)
    rest_s: StrictInt | None = Field(default=None, ge=0, le=3600)
    load_rule: LoadRule | None = None


class TemplateRow(CadenceModel):
    """One prescribed row, before a profile and a week make it concrete."""

    exercise: Slug
    role: RowRole
    sets: StrictInt | None = Field(default=None, ge=1, le=10)
    reps: StrictInt | None = Field(default=None, ge=1, le=100)
    seconds: StrictInt | None = Field(default=None, ge=1, le=3600)
    meters: StrictFloat | None = Field(default=None, ge=1, le=1000)
    steps: StrictInt | None = Field(default=None, ge=1, le=2000)
    per_side: StrictBool = False
    rest_s: StrictInt | None = Field(default=None, ge=0, le=3600)
    rpe_target: StrictInt | None = Field(default=None, ge=1, le=10)
    amrap: StrictBool = False
    cue_override: str | None = Field(default=None, max_length=120)
    load_rule: LoadRule | None = None
    u10_sub: Slug | None = None
    anchor_alt: AnchorAlt | None = None
    # Section 2's youth prelude drops dead-bug entirely; there is no prescription to shrink.
    youth_skip: StrictBool = False
    assessment_id: AssessmentId | None = None
    adult: ColumnSpec | None = None
    youth: ColumnSpec | None = None

    @property
    def _has_direct(self) -> bool:
        """Whether the row prescribes itself, rather than only through column blocks."""
        if self.sets is None or self.rest_s is None:
            return False
        return len([name for name in MEASURE_KEYS if getattr(self, name) is not None]) == 1

    @model_validator(mode="after")
    def _has_a_prescription(self) -> TemplateRow:
        """Every profile kind this row can serve must have exactly one readable prescription.

        Direct keys are the shared prescription; an ``adult:`` or ``youth:`` block overrides it for
        that kind. A row with neither, or with a half-written direct prescription, is a typo.
        """
        if self._has_direct:
            return self
        if self.sets is not None or any(getattr(self, key) is not None for key in MEASURE_KEYS):
            raise PydanticCustomError(
                "template_row_incomplete",
                "a row prescribing itself directly needs sets, rest_s and exactly one measure",
            )
        if self.adult is None and self.youth is None:
            raise PydanticCustomError("template_row_incomplete", "a row needs a prescription or a column block")
        return self

    def column(self, kind: ProfileKind) -> ColumnSpec:
        """This row's prescription for one profile kind. Never mutates the row."""
        block = self.youth if kind == "youth" else self.adult
        if block is not None:
            return block
        if not self._has_direct:
            other = self.adult if kind == "youth" else self.youth
            if other is not None:
                return other
        return ColumnSpec(
            sets=self.sets or 1,
            reps=self.reps,
            seconds=self.seconds,
            meters=self.meters,
            steps=self.steps,
            per_side=self.per_side,
            rest_s=self.rest_s or 0,
            rpe_target=self.rpe_target,
            amrap=self.amrap,
            cue_override=self.cue_override,
            load_rule=self.load_rule or BODYWEIGHT_RULE,
            u10_sub=self.u10_sub,
        )


class WorkoutTemplate(CadenceModel):
    """One ``library/workouts/*.yaml`` document."""

    version: StrictInt = Field(default=1, ge=1, le=1)
    kind: Literal["workout"] = "workout"
    id: Slug
    name: str = Field(min_length=1, max_length=100)
    day_type: DayType
    target_profile_kind: Literal["adult", "youth", "both"]
    variant: str | None = Field(default=None, max_length=64)
    prelude: Slug | None = None
    rows: tuple[TemplateRow, ...] = Field(min_length=1, max_length=60)
    extras: tuple[TemplateRow, ...] = Field(default_factory=tuple)
    notes: str | None = Field(default=None, max_length=500)

    @property
    def profile_kinds(self) -> tuple[ProfileKind, ...]:
        """The profile kinds this template claims to serve, in a fixed order."""
        if self.target_profile_kind == "both":
            return ("adult", "youth")
        return (self.target_profile_kind,)

    def serves(self, kind: ProfileKind) -> bool:
        return kind in self.profile_kinds

    def all_rows(self) -> tuple[TemplateRow, ...]:
        return self.rows + self.extras
