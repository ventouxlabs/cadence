"""``validate_workout`` - the single gate in front of seed load, import and generation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal

from pydantic import ValidationError as PydanticValidationError

from cadence.schema.enums import AgeBand, Equipment, GoalType
from cadence.schema.exercise import Exercise
from cadence.schema.workout import Workout
from cadence.schema.youth import YouthRuleSet
from cadence.validateur import codes, exercise_rules
from cadence.validateur import youth_rules as youth
from cadence.validateur.result import (
    ValidationError,
    ValidationResult,
    flatten_pydantic,
    make_error,
    result_from,
)


def _parse(doc: dict | Workout) -> tuple[Workout | None, list[ValidationError]]:
    # A model instance is re-validated from its own dump rather than trusted. An instance can be
    # built by other means than validation - model_construct, a pickle, a subclass - and the gate
    # has to hold for whatever actually arrives.
    payload = doc.model_dump() if isinstance(doc, Workout) else doc
    try:
        return Workout.model_validate(payload), []
    except PydanticValidationError as exc:
        return None, flatten_pydantic(exc)
    except (TypeError, ValueError, RecursionError) as exc:
        return None, [make_error("$", codes.SCHEMA, f"could not read the workout: {type(exc).__name__}")]


def _youth_ruleset(
    age_band: AgeBand,
    youth_rules: Mapping[AgeBand, YouthRuleSet] | None,
) -> tuple[YouthRuleSet | None, list[ValidationError]]:
    """Resolve the band's rule set, failing closed when the table is missing."""
    rules = youth_rules.get(age_band) if youth_rules is not None else None
    if rules is None:
        return None, [
            make_error(
                "$",
                codes.YOUTH_RULES_UNAVAILABLE,
                f"no youth rules loaded for band {age_band.value}; a youth workout cannot be checked without them",
            )
        ]
    return rules, []


def validate_workout(
    doc: dict | Workout,
    *,
    profile_kind: Literal["adult", "youth"],
    age_band: AgeBand,
    equipment: list[Equipment],
    bodyweight_kg: float | None = None,
    has_overhead_anchor: bool = False,
    exercises: Mapping[str, Exercise] | None = None,
    youth_rules: Mapping[AgeBand, YouthRuleSet] | None = None,
    goal_types: Sequence[GoalType] | None = None,
) -> ValidationResult:
    """Validate one workout for one profile. Never raises, and collects every finding.

    Order: schema, exercise resolution, equipment, measure, then youth V1-V14. Two inputs fail
    closed rather than open: an absent ``exercises`` catalog makes every row unresolvable, and an
    absent youth rule table makes a youth workout unvalidatable. Both are errors, because a gate
    that cannot check is not a gate that passes.

    Callers branch on ``result.ok``. ``youth_rep_ceiling`` and ``youth_rpe_exceeded`` are warnings
    and leave ``ok`` True.
    """
    workout, findings = _parse(doc)
    if workout is None:
        return result_from(findings)

    weight = youth.usable_bodyweight(bodyweight_kg)
    if bodyweight_kg is not None and weight is None:
        findings.append(
            make_error(
                "$.bodyweight_kg",
                codes.BODYWEIGHT_INVALID,
                f"{bodyweight_kg!r} is not a usable bodyweight; it is treated as unknown, "
                "which makes the percentage caps fall back to the absolute ones",
            )
        )
    bodyweight_kg = weight

    if workout.target_profile_kind not in ("both", profile_kind):
        findings.append(
            make_error(
                "target_profile_kind",
                codes.PROFILE_KIND_MISMATCH,
                f"this workout targets {workout.target_profile_kind} profiles, "
                f"but it is being validated for a {profile_kind} one",
            )
        )

    catalog: Mapping[str, Exercise] = exercises if exercises is not None else {}
    available = set(equipment or [])
    findings.extend(exercise_rules.check_available_equipment(equipment))

    resolved: list[Exercise | None] = []
    for index, row in enumerate(workout.rows):
        exercise, row_findings = exercise_rules.resolve_row(index, row, catalog)
        findings.extend(row_findings)
        resolved.append(exercise)
        if exercise is None:
            continue
        findings.extend(exercise_rules.check_row_equipment(index, exercise, available))
        findings.extend(exercise_rules.check_row_measure(index, row, exercise))
        findings.extend(exercise_rules.check_row_load_unit(index, row, exercise))

    band, coerced = youth.effective_band(profile_kind, age_band)
    if coerced:
        findings.append(
            make_error(
                "$.age_band",
                codes.YOUTH_BAND_COERCED,
                f"a youth profile carrying the adult band is evaluated as {band.value}, "
                "the strictest band, until an age is recorded",
            )
        )

    if profile_kind == "youth":
        rules, rule_findings = _youth_ruleset(band, youth_rules)
        findings.extend(rule_findings)
        if rules is not None:
            for index, row in enumerate(workout.rows):
                findings.extend(youth.check_row(index, row, resolved[index], rules, bodyweight_kg))
                exercise = resolved[index]
                if exercise is not None:
                    findings.extend(youth.check_allowlist(index, exercise, band, bodyweight_kg))
                    findings.extend(youth.check_banned_tags(index, exercise, rules, workout.day_type))
            findings.extend(youth.check_session(workout, rules, resolved))
            findings.extend(youth.check_goals(goal_types, rules))

    findings.extend(youth.check_anchor(workout, catalog, has_overhead_anchor))
    return result_from(findings)
