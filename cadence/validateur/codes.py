"""Error codes and their fixed severity/remedy (PRP-00 section 5.1, principles section 3.10).

Two codes are warnings. Callers therefore gate on ``ValidationResult.ok``, never on
``len(result.errors)``: a perfectly acceptable document can carry warn entries.
"""

from __future__ import annotations

from typing import Literal

Severity = Literal["error", "warn"]

SCHEMA = "schema"
UNKNOWN_EXERCISE = "unknown_exercise"
EQUIPMENT_NOT_WHITELISTED = "equipment_not_whitelisted"
EQUIPMENT_NOT_AVAILABLE = "equipment_not_available"
MEASURE_MISMATCH = "measure_mismatch"
MEASURE_CONFLICT = "measure_conflict"
LOAD_UNIT_MISMATCH = "load_unit_mismatch"
PROFILE_KIND_MISMATCH = "profile_kind_mismatch"
YOUTH_RULES_UNAVAILABLE = "youth_rules_unavailable"
YOUTH_LOAD_TYPE_NOT_ALLOWED = "youth_load_type_not_allowed"
YOUTH_LOAD_EXCEEDED = "youth_load_exceeded"
YOUTH_LOAD_UNCAPPABLE = "youth_load_uncappable"
YOUTH_BAND_COERCED = "youth_band_coerced"
BODYWEIGHT_INVALID = "bodyweight_invalid"
YOUTH_REP_FLOOR = "youth_rep_floor"
YOUTH_REP_CEILING = "youth_rep_ceiling"
YOUTH_REST_FLOOR = "youth_rest_floor"
YOUTH_EXERCISE_COUNT = "youth_exercise_count"
YOUTH_SET_COUNT = "youth_set_count"
YOUTH_BANNED_TAG = "youth_banned_tag"
YOUTH_SESSION_LENGTH = "youth_session_length"
YOUTH_AMRAP_NOT_ALLOWED = "youth_amrap_not_allowed"
YOUTH_BANNED_GOAL = "youth_banned_goal"
YOUTH_RPE_EXCEEDED = "youth_rpe_exceeded"
REQUIRES_ANCHOR_UNAVAILABLE = "requires_anchor_unavailable"
YOUTH_EXERCISE_NOT_ALLOWED = "youth_exercise_not_allowed"

SEVERITY: dict[str, Severity] = {
    SCHEMA: "error",
    UNKNOWN_EXERCISE: "error",
    EQUIPMENT_NOT_WHITELISTED: "error",
    EQUIPMENT_NOT_AVAILABLE: "error",
    MEASURE_MISMATCH: "error",
    MEASURE_CONFLICT: "error",
    LOAD_UNIT_MISMATCH: "error",
    PROFILE_KIND_MISMATCH: "error",
    YOUTH_RULES_UNAVAILABLE: "error",
    YOUTH_LOAD_TYPE_NOT_ALLOWED: "error",
    YOUTH_LOAD_EXCEEDED: "error",
    YOUTH_LOAD_UNCAPPABLE: "error",
    YOUTH_BAND_COERCED: "warn",
    BODYWEIGHT_INVALID: "error",
    YOUTH_REP_FLOOR: "error",
    YOUTH_REP_CEILING: "error",
    YOUTH_REST_FLOOR: "error",
    YOUTH_EXERCISE_COUNT: "error",
    YOUTH_SET_COUNT: "error",
    YOUTH_BANNED_TAG: "error",
    YOUTH_SESSION_LENGTH: "error",
    YOUTH_AMRAP_NOT_ALLOWED: "error",
    YOUTH_BANNED_GOAL: "error",
    YOUTH_RPE_EXCEEDED: "error",
    REQUIRES_ANCHOR_UNAVAILABLE: "error",
    YOUTH_EXERCISE_NOT_ALLOWED: "error",
}

REMEDY: dict[str, str] = {
    SCHEMA: "fix the document so it matches the schema",
    UNKNOWN_EXERCISE: "use an exercise id that exists in the library",
    EQUIPMENT_NOT_WHITELISTED: "use one of: bodyweight, dumbbells, kettlebells, bench",
    EQUIPMENT_NOT_AVAILABLE: "substitute an exercise that uses the equipment you have",
    MEASURE_MISMATCH: "prescribe the measure the exercise is defined in",
    MEASURE_CONFLICT: "set exactly one of reps, seconds, meters, steps",
    LOAD_UNIT_MISMATCH: "prescribe the load in the unit the exercise is held in",
    PROFILE_KIND_MISMATCH: "serve this workout to the profile kind it targets",
    YOUTH_RULES_UNAVAILABLE: "load library/youth_rules.yaml before validating a youth workout",
    YOUTH_LOAD_TYPE_NOT_ALLOWED: "substitute via regression_of",
    YOUTH_LOAD_EXCEEDED: "clamp to cap",
    YOUTH_LOAD_UNCAPPABLE: "prescribe the load per hand or per implement so a cap applies",
    YOUTH_BAND_COERCED: "set the age so the right band applies",
    BODYWEIGHT_INVALID: "record a real bodyweight in kilograms, or leave it unset",
    YOUTH_REP_FLOOR: "raise reps to the floor, reduce load",
    YOUTH_REP_CEILING: "cap reps",
    YOUTH_REST_FLOOR: "raise rest",
    YOUTH_EXERCISE_COUNT: "drop lowest-priority rows",
    YOUTH_SET_COUNT: "drop sets",
    YOUTH_BANNED_TAG: "substitute or drop",
    YOUTH_SESSION_LENGTH: "drop lowest-priority rows",
    YOUTH_AMRAP_NOT_ALLOWED: "convert to fixed reps at rep_max",
    YOUTH_BANNED_GOAL: "reject profile save",
    YOUTH_RPE_EXCEEDED: "lower target",
    REQUIRES_ANCHOR_UNAVAILABLE: "substitute per principles section 1.7",
    YOUTH_EXERCISE_NOT_ALLOWED: "substitute via regression_of",
}

ALL_CODES: frozenset[str] = frozenset(SEVERITY)
