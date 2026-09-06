"""Exercise-level checks: parsing, equipment availability, measure agreement."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from pydantic import ValidationError as PydanticValidationError

from cadence.schema.enums import Equipment
from cadence.schema.equipment import is_whitelisted
from cadence.schema.exercise import Exercise
from cadence.schema.workout import WorkoutRow
from cadence.validateur import codes
from cadence.validateur.result import (
    ValidationError,
    ValidationResult,
    flatten_pydantic,
    make_error,
    result_from,
)


def validate_exercise(doc: dict | Exercise) -> ValidationResult:
    """Parse and check one exercise document. Never raises.

    A model instance is re-validated from its own dump rather than trusted, for the same reason
    ``validate_workout`` does it: an instance is not proof that validation ever ran.
    """
    payload = doc.model_dump() if isinstance(doc, Exercise) else doc
    try:
        Exercise.model_validate(payload)
    except PydanticValidationError as exc:
        return result_from(flatten_pydantic(exc))
    except (TypeError, ValueError) as exc:
        return result_from([make_error("$", codes.SCHEMA, f"could not read the exercise: {exc}")])
    return result_from([])


def check_available_equipment(available: Sequence[Equipment] | None) -> list[ValidationError]:
    """The caller's own equipment list must itself be on the whitelist."""
    if not available:
        return []
    return [
        make_error(
            f"equipment[{index}]",
            codes.EQUIPMENT_NOT_WHITELISTED,
            f"{item!r} is not on the equipment whitelist",
        )
        for index, item in enumerate(available)
        if not is_whitelisted(item)
    ]


def check_row_equipment(index: int, exercise: Exercise, available: set[Equipment]) -> list[ValidationError]:
    """The exercise needs equipment the profile has not enabled."""
    missing = [item for item in exercise.equipment if item not in available]
    if not missing:
        return []
    names = ", ".join(item.value for item in missing)
    return [
        make_error(
            f"rows[{index}].exercise_id",
            codes.EQUIPMENT_NOT_AVAILABLE,
            f"{exercise.id} needs {names}, which this profile does not have",
        )
    ]


def check_row_measure(index: int, row: WorkoutRow, exercise: Exercise) -> list[ValidationError]:
    """The row must be prescribed in the measure the exercise is defined in."""
    populated = row.populated_measure
    if populated is None or populated == exercise.measure.value:
        return []
    return [
        make_error(
            f"rows[{index}].{populated}",
            codes.MEASURE_MISMATCH,
            f"{exercise.id} is measured in {exercise.measure.value}, but the row prescribes {populated}",
        )
    ]


def check_row_load_unit(index: int, row: WorkoutRow, exercise: Exercise) -> list[ValidationError]:
    """The row must count its load the way the exercise is held (principles section 1.4).

    ``load_unit`` is required on the exercise *and* on the row precisely so a cap can be checked
    against the right column. Without this check a per-hand dumbbell row relabelled ``total``
    reaches the youth cap with a unit PRP-00 section 5.2 does not cap, and 100 kg passes for a
    fourteen-year-old.
    """
    if row.load_unit is exercise.load_unit:
        return []
    return [
        make_error(
            f"rows[{index}].load_unit",
            codes.LOAD_UNIT_MISMATCH,
            f"{exercise.id} is loaded {exercise.load_unit.value}, but the row counts it {row.load_unit.value}",
        )
    ]


def resolve_row(
    index: int, row: WorkoutRow, exercises: Mapping[str, Exercise]
) -> tuple[Exercise | None, list[ValidationError]]:
    """Look the row's exercise up in the catalog."""
    exercise = exercises.get(row.exercise_id)
    if exercise is None:
        return None, [
            make_error(
                f"rows[{index}].exercise_id",
                codes.UNKNOWN_EXERCISE,
                f"{row.exercise_id!r} is not an exercise in the library",
            )
        ]
    return exercise, []
