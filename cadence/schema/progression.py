"""The ``Progression`` model - progression-as-data.

Field-for-field as ``docs/exercise-principles.md`` section 5.1. One shape serves two lives: the
default stored on a seed exercise, and the per-user mutable state copied onto each workout row.
Do not fork it into two models.
"""

from __future__ import annotations

from pydantic import Field, StrictBool, StrictFloat, StrictInt

from cadence.schema.base import CadenceModel, Slug
from cadence.schema.enums import ProgressionType, RegressTrigger


class Progression(CadenceModel):
    # ``id`` is an addition to principles section 5.1: progressions get their own
    # library/progressions/*.yaml (architecture section 1), which needs a key.
    id: Slug
    type: ProgressionType
    rep_min: StrictInt | None = Field(default=None, ge=1, le=100)
    rep_max: StrictInt | None = Field(default=None, ge=1, le=100)
    load_step_kg: StrictFloat | None = Field(default=None, ge=0, le=20)
    load_step_pct: StrictFloat | None = Field(default=None, ge=0, le=1)
    time_step_s: StrictInt | None = Field(default=None, ge=0, le=120)
    distance_step_m: StrictInt | None = Field(default=None, ge=0, le=200)
    regress_on: tuple[RegressTrigger, ...] = Field(default_factory=tuple)
    regress_step: StrictFloat = Field(default=0.10, ge=0, le=1)
    regress_reps: StrictInt = Field(default=2, ge=0, le=20)
    # 0.60, not 0.40. Set arithmetic hides the difference, but carry distance scales by it
    # directly (principles section 5.5), so a wrong default shortens every deload-week carry.
    deload_pct: StrictFloat = Field(default=0.60, ge=0, le=1)
    allow_load_progression: StrictBool = True
    # Injected by the validator from principles section 3.3, always via model_copy(update=...).
    cap_load_kg: StrictFloat | None = Field(default=None, ge=0, le=500)
