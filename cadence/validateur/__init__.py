"""The validator - schema, equipment whitelist and youth rules, in one gate.

Nothing reaches the database or a screen without passing through here: the seed loader
(PRP-01), ``POST /api/import`` and ``POST /api/generate`` (PRP-08) all call it.
"""

from __future__ import annotations

from cadence.validateur import codes
from cadence.validateur.exercise_rules import validate_exercise
from cadence.validateur.result import ValidationError, ValidationResult
from cadence.validateur.workout import validate_workout
from cadence.validateur.youth_rules import (
    effective_band,
    effective_cap,
    usable_bodyweight,
    youth_allows,
)

__all__ = [
    "ValidationError",
    "ValidationResult",
    "codes",
    "effective_band",
    "effective_cap",
    "validate_exercise",
    "usable_bodyweight",
    "validate_workout",
    "youth_allows",
]
