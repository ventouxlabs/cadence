"""The Cadence library DSL - Pydantic v2 models are the single source of truth."""

from __future__ import annotations

from cadence.schema.assessment import AssessmentSpec
from cadence.schema.base import SLUG_PATTERN, CadenceModel, Slug
from cadence.schema.enums import (
    AgeBand,
    AssessmentId,
    AutoregOutcome,
    DayType,
    Equipment,
    ExerciseTag,
    Felt,
    GarminCategory,
    GoalType,
    LoadType,
    LoadUnit,
    Measure,
    Pattern,
    ProgressionType,
    Readiness,
    Region,
    RegressTrigger,
    SessionStatus,
    TargetTier,
    YouthAllow,
)
from cadence.schema.equipment import EQUIPMENT_WHITELIST, is_whitelisted
from cadence.schema.exercise import Exercise
from cadence.schema.progression import Progression
from cadence.schema.workout import MEASURE_FIELDS, Workout, WorkoutRow
from cadence.schema.youth import YouthRules, YouthRuleSet

__all__ = [
    "EQUIPMENT_WHITELIST",
    "MEASURE_FIELDS",
    "SLUG_PATTERN",
    "AgeBand",
    "AssessmentId",
    "AssessmentSpec",
    "AutoregOutcome",
    "CadenceModel",
    "DayType",
    "Equipment",
    "Exercise",
    "ExerciseTag",
    "Felt",
    "GarminCategory",
    "GoalType",
    "LoadType",
    "LoadUnit",
    "Measure",
    "Pattern",
    "Progression",
    "ProgressionType",
    "Readiness",
    "Region",
    "RegressTrigger",
    "SessionStatus",
    "Slug",
    "TargetTier",
    "Workout",
    "WorkoutRow",
    "YouthAllow",
    "YouthRuleSet",
    "YouthRules",
    "is_whitelisted",
]
