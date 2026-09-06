"""The program engine: bands, ladders, week schemes and the four-week block builder."""

from __future__ import annotations

from cadence.programme.bands import age_band, band_for_age, effective_cap, week_of_block
from cadence.programme.builder import ProgramPlan, build_program, program_id
from cadence.programme.ladder import (
    WeightsAvailable,
    parse_weights_available,
    round_to_available,
)
from cadence.programme.materialise import compile_workout, materialise_rows
from cadence.programme.next_time import next_time_note
from cadence.programme.prescription import estimated_minutes
from cadence.programme.schemes import row_budget, week_day_types, workout_id_for
from cadence.programme.tables import PlannedSession, Program

__all__ = [
    "PlannedSession",
    "Program",
    "ProgramPlan",
    "WeightsAvailable",
    "age_band",
    "band_for_age",
    "build_program",
    "compile_workout",
    "effective_cap",
    "estimated_minutes",
    "materialise_rows",
    "next_time_note",
    "parse_weights_available",
    "program_id",
    "round_to_available",
    "row_budget",
    "week_day_types",
    "week_of_block",
    "workout_id_for",
]
