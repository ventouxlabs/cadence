"""The ``AssessmentSpec`` model - one baseline/retest test."""

from __future__ import annotations

from pydantic import Field

from cadence.schema.base import CadenceModel
from cadence.schema.enums import AssessmentId, Measure


class AssessmentSpec(CadenceModel):
    id: AssessmentId
    name: str = Field(min_length=1, max_length=100)
    unit: str = Field(min_length=1, max_length=20)
    measure: Measure
    self_rated: bool = False
    higher_is_better: bool = True
    protocol: str = Field(min_length=1, max_length=500)
    retest_days: int = Field(default=28, ge=1, le=365)
    youth_cap: int | None = Field(default=None, ge=1)
