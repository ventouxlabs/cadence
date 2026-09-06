"""The ``Workout`` and ``WorkoutRow`` models - one prescribed session."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, StrictBool, StrictFloat, StrictInt, model_validator
from pydantic_core import PydanticCustomError

from cadence.schema.base import CadenceModel, Slug
from cadence.schema.enums import DayType, LoadUnit

MEASURE_FIELDS: tuple[str, ...] = ("reps", "seconds", "meters", "steps")


class WorkoutRow(CadenceModel):
    exercise_id: Slug
    sets: StrictInt = Field(ge=1, le=10)
    reps: StrictInt | None = Field(default=None, ge=1, le=100)
    seconds: StrictInt | None = Field(default=None, ge=1, le=3600)
    meters: StrictFloat | None = Field(default=None, ge=1, le=1000)
    steps: StrictInt | None = Field(default=None, ge=1, le=2000)
    load_kg: StrictFloat | None = Field(default=None, ge=0, le=500)
    load_unit: LoadUnit
    rest_s: StrictInt = Field(ge=0, le=3600)
    rpe_target: StrictInt | None = Field(default=None, ge=1, le=10)
    amrap: StrictBool = False
    # Display hint only. The youth exercise-count cap reads Exercise.is_prelude, because a
    # document must not be able to exempt itself from a safety cap by asserting a flag.
    is_prelude: StrictBool = False
    is_challenge: StrictBool = False
    cue_override: str | None = Field(default=None, max_length=120)
    progression_id: Slug | None = None

    @property
    def populated_measure(self) -> str | None:
        """Name of the single populated measure field, or ``None`` if there is not exactly one."""
        populated = [name for name in MEASURE_FIELDS if getattr(self, name) is not None]
        return populated[0] if len(populated) == 1 else None

    @model_validator(mode="after")
    def _exactly_one_measure(self) -> WorkoutRow:
        populated = [name for name in MEASURE_FIELDS if getattr(self, name) is not None]
        if len(populated) != 1:
            raise PydanticCustomError(
                "measure_conflict",
                "exactly one of reps/seconds/meters/steps must be set, got {count} ({names})",
                {"count": len(populated), "names": ", ".join(populated) or "none"},
            )
        return self

    @model_validator(mode="after")
    def _bodyweight_unit_carries_no_load(self) -> WorkoutRow:
        """``bodyweight`` means no external load (principles section 1.4).

        A row claiming both is a contradiction, and a permissive one: ``is_loaded()`` reads the
        unit rather than the number, so a bodyweight-unit row carrying kilos would skip the rep
        floor, the rest floor and the load cap alike.
        """
        if self.load_unit is LoadUnit.BODYWEIGHT and self.load_kg:
            raise PydanticCustomError(
                "bodyweight_row_carries_load",
                "load_unit 'bodyweight' means no external load, but load_kg is {load_kg}",
                {"load_kg": self.load_kg},
            )
        return self


class Workout(CadenceModel):
    id: Slug
    name: str = Field(min_length=1, max_length=100)
    day_type: DayType
    target_profile_kind: Literal["adult", "youth", "both"]
    variant: str | None = Field(default=None, max_length=64)
    rows: tuple[WorkoutRow, ...] = Field(min_length=1, max_length=60)
    estimated_minutes: StrictInt = Field(ge=1, le=120)
    notes: str | None = Field(default=None, max_length=500)
