"""``build_program`` - one rolling four-week block for one profile.

Pure by design: it takes the start date rather than reading a clock, uses no randomness, adds
nothing to a database session, and returns unsaved objects so PRP-03 can splice a rebuild into an
existing plan. Two calls with equal inputs produce byte-identical ``rows_json``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Literal

from cadence.bibliotheque.bundle import LibraryBundle
from cadence.bibliotheque.template import WorkoutTemplate
from cadence.profils.settings import ProgramSettings
from cadence.profils.tables import Profile
from cadence.programme.bands import BLOCK_WEEKS, age_band
from cadence.programme.errors import ProgramBuildError
from cadence.programme.materialise import materialise_rows, workout_from_rows
from cadence.programme.schemes import week_day_types, workout_id_for
from cadence.programme.tables import ACTIVE, PLANNED, PlannedSession, Program
from cadence.schema.enums import AgeBand, DayType
from cadence.validateur import validate_workout

DEFAULT_TEMPLATE = "rolling-4-week"


@dataclass(frozen=True, slots=True)
class ProgramPlan:
    """A program and its planned sessions, none of them saved yet."""

    program: Program
    sessions: tuple[PlannedSession, ...]

    @property
    def session_count(self) -> int:
        return len(self.sessions)


def program_id(profile_id: str) -> str:
    """Deterministic, so re-seeding updates the one block rather than adding another."""
    return f"{profile_id}-block-1"


def _gate(
    template: WorkoutTemplate,
    rows: list[dict],
    profile: Profile,
    settings: ProgramSettings,
    library: LibraryBundle,
    kind: Literal["adult", "youth"],
    band: AgeBand,
    where: str,
) -> list[str]:
    """Run the validator over the rows this session will actually store.

    ``docs/architecture.md`` section 5 makes the validator the gate in front of everything that
    reaches the database or a screen, and until now the program engine was the one path that
    wrote rows without passing through it - the loader checked *templates* at week 1 against a
    profile with no bodyweight and no anchor, which is not the household. This checks the concrete
    rows, at the week they are built for, with this profile's own facts.
    """
    result = validate_workout(
        workout_from_rows(template, rows, is_youth=kind == "youth"),
        profile_kind=kind,
        age_band=band,
        equipment=list(settings.equipment),
        bodyweight_kg=profile.bodyweight_kg,
        has_overhead_anchor=profile.has_overhead_anchor,
        exercises=library.exercises,
        youth_rules=library.youth_rules,
    )
    if result.ok:
        return []
    return [
        f"{where}: {error.path} {error.code}: {error.message}" for error in result.errors if error.severity == "error"
    ]


def build_program(
    profile: Profile,
    settings: ProgramSettings,
    library: LibraryBundle,
    start_date: date,
) -> ProgramPlan:
    """The four-week block for this profile, as unsaved rows.

    ``assessment`` replaces the first slot of week 1 (section 6.3). The displaced day type is
    skipped for the block and the rotation index still advances, because the rotation is a
    function of the week and the slot, not of what was scheduled before it - so a four-day block
    is 16 sessions: one assessment and fifteen training days.
    """
    is_youth = profile.kind == "youth"
    kind: Literal["adult", "youth"] = "youth" if is_youth else "adult"
    identifier = program_id(profile.id)
    band = age_band(profile)
    sessions: list[PlannedSession] = []
    problems: list[str] = []

    for week in range(1, BLOCK_WEEKS + 1):
        for day_index, rotated in enumerate(week_day_types(week, settings.days_per_week, is_youth=is_youth)):
            day_type = DayType.ASSESSMENT if (week == 1 and day_index == 0) else rotated
            workout_id = workout_id_for(day_type, kind)
            template = library.template(workout_id)
            rows = materialise_rows(template, week, profile, settings, library)
            problems += _gate(template, rows, profile, settings, library, kind, band, f"{workout_id} w{week}")
            sessions.append(
                PlannedSession(
                    id=f"{identifier}-w{week}-d{day_index}",
                    program_id=identifier,
                    profile_id=profile.id,
                    week=week,
                    day_index=day_index,
                    day_type=day_type.value,
                    workout_id=workout_id,
                    rows_json=json.dumps(rows, sort_keys=True, separators=(",", ":")),
                    status=PLANNED,
                )
            )

    if problems:
        raise ProgramBuildError(f"the plan for profile {profile.id!r} would not validate", problems)

    program = Program(
        id=identifier,
        profile_id=profile.id,
        template=DEFAULT_TEMPLATE,
        start_date=start_date.isoformat(),
        weeks=BLOCK_WEEKS,
        days_per_week=settings.days_per_week,
        session_minutes=settings.session_minutes,
        status=ACTIVE,
    )
    return ProgramPlan(program=program, sessions=tuple(sessions))
