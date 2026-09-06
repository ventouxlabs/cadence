"""Domain enums.

Member values are exactly ``docs/exercise-principles.md`` section 11. Nothing else in the
codebase may spell an enum member as a bare string literal: this module is the only place
those values exist, so a rename here cannot leave a stale copy behind.
"""

from __future__ import annotations

from enum import Enum


class AgeBand(str, Enum):
    U10 = "u10"
    AGE_10_13 = "age_10_13"
    AGE_14_17 = "age_14_17"
    ADULT = "adult"


class YouthAllow(str, Enum):
    """The three states of the per-band columns in ``docs/exercise-principles.md`` section 9.

    Not in the section 11 enum list because section 9 writes them as ``Y`` / ``N`` / ``Y*``.
    """

    YES = "yes"
    NO = "no"
    CONDITIONAL = "conditional"


class Pattern(str, Enum):
    HINGE = "hinge"
    SQUAT = "squat"
    PUSH_H = "push_h"
    PUSH_V = "push_v"
    PULL_H = "pull_h"
    PULL_V = "pull_v"
    CARRY = "carry"
    BRACE = "brace"
    ROTATE_ANTI = "rotate_anti"
    MOBILITY = "mobility"
    LOCOMOTION = "locomotion"


class Region(str, Enum):
    UPPER = "upper"
    LOWER = "lower"
    CORE = "core"
    FULL = "full"


class LoadType(str, Enum):
    BODYWEIGHT = "bodyweight"
    DUMBBELL = "dumbbell"
    KETTLEBELL = "kettlebell"
    BENCH_ASSISTED = "bench_assisted"


class LoadUnit(str, Enum):
    PER_HAND = "per_hand"
    PER_IMPLEMENT = "per_implement"
    TOTAL = "total"
    BODYWEIGHT = "bodyweight"


class Measure(str, Enum):
    REPS = "reps"
    SECONDS = "seconds"
    METERS = "meters"
    STEPS = "steps"


class Equipment(str, Enum):
    """The whole whitelist (principles section 8.1). Nothing else is ever selectable."""

    BODYWEIGHT = "bodyweight"
    DUMBBELLS = "dumbbells"
    KETTLEBELLS = "kettlebells"
    BENCH = "bench"


class ExerciseTag(str, Enum):
    MAX_EFFORT = "max_effort"
    ONE_RM = "one_rm"
    TO_FAILURE = "to_failure"
    PLYOMETRIC_DEPTH = "plyometric_depth"
    LOADED_SPINAL_FLEXION = "loaded_spinal_flexion"
    OVERHEAD_LOADED = "overhead_loaded"
    LOADED_CARRY = "loaded_carry"
    REQUIRES_ANCHOR = "requires_anchor"
    UNILATERAL = "unilateral"
    ASSESSMENT_ONLY = "assessment_only"
    PLAY = "play"
    GRIP_LIMITED = "grip_limited"
    TEMPO = "tempo"


class ProgressionType(str, Enum):
    DOUBLE_PROGRESSION = "double_progression"
    LINEAR_LOAD = "linear_load"
    REP_PROGRESSION = "rep_progression"
    TIME_PROGRESSION = "time_progression"


class RegressTrigger(str, Enum):
    HARD = "hard"
    MISSED = "missed"
    LOW_READINESS = "low_readiness"


class AutoregOutcome(str, Enum):
    BUMP = "bump"
    HOLD = "hold"
    REGRESS = "regress"
    DELOAD = "deload"


class Felt(str, Enum):
    EASY = "easy"
    RIGHT = "right"
    HARD = "hard"


class Readiness(str, Enum):
    LOW = "low"
    OK = "ok"
    HIGH = "high"
    UNKNOWN = "unknown"


class SessionStatus(str, Enum):
    PENDING = "pending"
    PARTIAL = "partial"
    COMPLETE = "complete"
    SKIPPED = "skipped"


class GoalType(str, Enum):
    STRENGTH = "strength"
    POSTURE = "posture"
    MOVEMENT_QUALITY = "movement_quality"
    CONSISTENCY = "consistency"
    WEIGHT = "weight"
    BODY_FAT = "body_fat"
    APPEARANCE = "appearance"


class DayType(str, Enum):
    UPPER_A = "upper_a"
    LOWER_A = "lower_a"
    UPPER_B = "upper_b"
    LOWER_FULL_B = "lower_full_b"
    MOBILITY_CARRY = "mobility_carry"
    ASSESSMENT = "assessment"


class AssessmentId(str, Enum):
    PUSH_UP_MAX = "push_up_max"
    DEAD_HANG_S = "dead_hang_s"
    PLANK_S = "plank_s"
    WALL_ANGEL_REACH = "wall_angel_reach"
    GOBLET_SQUAT_QUALITY = "goblet_squat_quality"
    FARMER_CARRY_S = "farmer_carry_s"


class TargetTier(str, Enum):
    LOW = "low"
    OK = "ok"
    STRONG = "strong"


class GarminCategory(str, Enum):
    """Parent exercise categories confirmed present in ``garminconnect`` 0.3.11.

    There is deliberately **no** ``UNKNOWN`` member (D-018): the catalog has no such
    category and emitting it earns a Garmin 400. "Variant unknown" is ``garmin_category``
    set to a known parent with ``garmin_exercise = None``, or the field left ``None``.
    """

    PUSH_UP = "PUSH_UP"
    SQUAT = "SQUAT"
    DEADLIFT = "DEADLIFT"
    ROW = "ROW"
    PLANK = "PLANK"
    CARRY = "CARRY"
    LUNGE = "LUNGE"
    HIP_RAISE = "HIP_RAISE"
    CORE = "CORE"
    SHOULDER_PRESS = "SHOULDER_PRESS"
    BENCH_PRESS = "BENCH_PRESS"
    PULL_UP = "PULL_UP"
    CURL = "CURL"
    TRICEPS_EXTENSION = "TRICEPS_EXTENSION"
    TOTAL_BODY = "TOTAL_BODY"
    WARM_UP = "WARM_UP"
    FLYE = "FLYE"
    LATERAL_RAISE = "LATERAL_RAISE"
    SHRUG = "SHRUG"
    SIT_UP = "SIT_UP"
    LEG_RAISE = "LEG_RAISE"
    CALF_RAISE = "CALF_RAISE"
    HYPEREXTENSION = "HYPEREXTENSION"
    OLYMPIC_LIFT = "OLYMPIC_LIFT"
    PLYO = "PLYO"
    CHOP = "CHOP"
    CARDIO = "CARDIO"
