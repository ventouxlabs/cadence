"""The ``Exercise`` model - one movement in the library."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, StrictInt, StringConstraints, field_validator, model_validator
from pydantic_core import PydanticCustomError

from cadence.schema.base import CadenceModel, FrozenMap, Slug
from cadence.schema.enums import (
    AgeBand,
    Equipment,
    ExerciseTag,
    GarminCategory,
    LoadType,
    LoadUnit,
    Measure,
    Pattern,
    Region,
    YouthAllow,
)

GarminExerciseName = Annotated[str, StringConstraints(pattern=r"^[A-Z0-9_]+$", min_length=1, max_length=64)]


class Exercise(CadenceModel):
    id: Slug
    name: str = Field(min_length=1, max_length=100)
    pattern: Pattern
    region: Region
    load_type: LoadType
    load_unit: LoadUnit
    measure: Measure
    equipment: tuple[Equipment, ...] = Field(min_length=1)
    tags: tuple[ExerciseTag, ...] = Field(default_factory=tuple)
    cue: str = Field(min_length=1, max_length=120)
    garmin_category: GarminCategory | None = None
    garmin_exercise: GarminExerciseName | None = None
    regression_of: Slug | None = None
    progression_of: Slug | None = None
    default_progression: Slug | None = None
    est_seconds_per_set: StrictInt | None = Field(default=None, ge=1, le=600)
    # Whether this movement belongs to the posture prelude (principles section 2). It lives on
    # the exercise, not the row: the youth exercise-count cap excludes prelude movements, and a
    # row-level flag would let a document exempt itself from the cap by declaring twelve
    # preludes.
    is_prelude: bool = False
    # The per-band columns of principles section 9, checked by the validator (V14) rather than
    # by a library helper: a check the import and generate paths must remember to call is a
    # bypass. A youth band absent from this dict is "no" - the allowlist fails closed.
    youth_ok_by_band: FrozenMap[AgeBand, YouthAllow] = Field(default_factory=dict, validate_default=True)

    @field_validator("equipment")
    @classmethod
    def _dedupe_equipment(cls, value: tuple[Equipment, ...]) -> tuple[Equipment, ...]:
        seen: list[Equipment] = []
        for item in value:
            if item not in seen:
                seen.append(item)
        return tuple(seen)

    @field_validator("cue")
    @classmethod
    def _cue_is_one_line(cls, value: str) -> str:
        if "\n" in value or "\r" in value:
            raise ValueError("cue must be a single line")
        return value

    @model_validator(mode="after")
    def _garmin_exercise_needs_category(self) -> Exercise:
        if self.garmin_exercise is not None and self.garmin_category is None:
            raise PydanticCustomError(
                "garmin_exercise_without_category",
                "garmin_exercise {name!r} needs a garmin_category: a sub-category cannot travel without its parent",
                {"name": self.garmin_exercise},
            )
        return self
