"""``make seed`` - loads ``library/`` into the database and builds a block for each profile.

Idempotent: every write is an upsert keyed by a deterministic id, and nothing whose ``source`` is
not ``seed`` is ever touched, so an imported or generated workout survives a re-seed.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from sqlmodel import Session, select

from cadence.bibliotheque.loader import LIBRARY_DIR, LibraryError, load_library
from cadence.config import get_settings
from cadence.db import ExerciseRecord, WorkoutRecord, init_db
from cadence.profils.settings import DEFAULT_SETTINGS, ProgramSettings, settings_from_rows
from cadence.profils.tables import PROFILE_ME, PROFILE_SON, Profile, Setting
from cadence.programme.bands import age_band
from cadence.programme.builder import build_program, program_id
from cadence.programme.errors import ProgramBuildError
from cadence.programme.tables import PLANNED, PlannedSession, Program

SEED_SOURCE = "seed"


@dataclass(frozen=True, slots=True)
class SeedReport:
    """What one seed run wrote. ``sessions`` is keyed by profile id."""

    exercises: int
    workouts: int
    profiles: tuple[str, ...]
    sessions: dict[str, int]

    def summary(self) -> str:
        planned = ", ".join(f"{key} {value}" for key, value in sorted(self.sessions.items()))
        return (
            f"seeded {self.exercises} exercises, {self.workouts} workouts, "
            f"{len(self.profiles)} profiles ({', '.join(self.profiles)}), planned sessions: {planned}"
        )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _upsert_exercises(session: Session, bundle_exercises: dict, stamp: str) -> int:
    for exercise in bundle_exercises.values():
        payload = json.dumps(exercise.model_dump(mode="json"), sort_keys=True)
        existing = session.get(ExerciseRecord, exercise.id)
        if existing is None:
            session.add(ExerciseRecord(id=exercise.id, doc_json=payload, source=SEED_SOURCE, created_at=stamp))
        elif existing.source == SEED_SOURCE:
            existing.doc_json = payload
    return len(bundle_exercises)


def _upsert_workouts(session: Session, templates: dict, stamp: str) -> int:
    """Seed workouts store the *template*, not a concrete ``Workout`` (D-064).

    A seed workout has no load until a profile and a week are applied; PRP-08's imported and
    generated documents are concrete and store themselves.
    """
    for template in templates.values():
        payload = json.dumps(template.model_dump(mode="json"), sort_keys=True)
        existing = session.get(WorkoutRecord, template.id)
        if existing is None:
            session.add(
                WorkoutRecord(
                    id=template.id,
                    doc_json=payload,
                    source=SEED_SOURCE,
                    target_profile_kind=template.target_profile_kind,
                    created_at=stamp,
                )
            )
        elif existing.source == SEED_SOURCE:
            existing.doc_json = payload
            existing.target_profile_kind = template.target_profile_kind
    return len(templates)


def _ensure_settings(session: Session, stamp: str) -> ProgramSettings:
    """Install the defaults for any setting not already stored; never overwrite a chosen one."""
    stored = {row.key: row.value_json for row in session.exec(select(Setting)).all()}
    for key, value in DEFAULT_SETTINGS.as_rows().items():
        if key not in stored:
            session.add(Setting(key=key, value_json=value, updated_at=stamp))
            stored[key] = value
    return settings_from_rows(stored)


def _ensure_profiles(session: Session) -> list[Profile]:
    """The two demo profiles. An existing profile keeps whatever the user has set on it."""
    config = get_settings()
    wanted = (
        Profile(
            id=PROFILE_ME,
            display_name="Me",
            kind="adult",
            age_years=None,
            vitalforge_person=config.vitalforge_person_me,
            push_to_garmin=True,
        ),
        Profile(
            id=PROFILE_SON,
            display_name="Son",
            kind="youth",
            # Section 3.8: until the age is set the son is evaluated against the strictest band.
            age_years=None,
            vitalforge_person=config.vitalforge_person_son,
            push_to_garmin=False,
        ),
    )
    live: list[Profile] = []
    for profile in wanted:
        existing = session.get(Profile, profile.id)
        if existing is None:
            profile.age_band = age_band(profile).value
            session.add(profile)
            live.append(profile)
        else:
            existing.age_band = age_band(existing).value
            live.append(existing)
    return live


def _rebuild_program(session: Session, profile: Profile, settings: ProgramSettings, bundle, start: date) -> int:
    plan = build_program(profile, settings, bundle, start)
    identifier = program_id(profile.id)

    existing_program = session.get(Program, identifier)
    if existing_program is None:
        session.add(plan.program)
    else:
        # `start_date` is the one field a rebuild must not touch: it is when this block began, and
        # re-seeding is not starting over. Overwriting it with today made every re-seed look like
        # a fresh block and would misdate anything PRP-04 counts from it.
        for field in ("template", "weeks", "days_per_week", "session_minutes", "status"):
            setattr(existing_program, field, getattr(plan.program, field))

    # Only rows still waiting to be done are the plan's to remove. A session that was finished or
    # deliberately skipped is history, not a slot: changing days-per-week reshapes the queue ahead
    # of the user, and it must not erase what they already did behind them.
    keep = {row.id for row in plan.sessions}
    existing = session.exec(select(PlannedSession).where(PlannedSession.program_id == identifier)).all()
    for row in existing:
        if row.id not in keep and row.status == PLANNED:
            session.delete(row)
    for planned in plan.sessions:
        current = session.get(PlannedSession, planned.id)
        if current is None:
            session.add(planned)
        elif current.status == PLANNED:
            current.rows_json = planned.rows_json
            current.workout_id = planned.workout_id
            current.day_type = planned.day_type
    return plan.session_count


def seed(session: Session, path: Path = LIBRARY_DIR, reset: bool = False) -> SeedReport:
    """Load the library and build one block per profile. Raises ``LibraryError`` on a bad library."""
    bundle = load_library(path)
    stamp = _now()

    if reset:
        for record in session.exec(select(ExerciseRecord).where(ExerciseRecord.source == SEED_SOURCE)).all():
            session.delete(record)
        for record in session.exec(select(WorkoutRecord).where(WorkoutRecord.source == SEED_SOURCE)).all():
            session.delete(record)
        for planned in session.exec(select(PlannedSession)).all():
            session.delete(planned)
        for program in session.exec(select(Program)).all():
            session.delete(program)
        session.flush()

    exercises = _upsert_exercises(session, dict(bundle.exercises), stamp)
    workouts = _upsert_workouts(session, dict(bundle.templates), stamp)
    settings = _ensure_settings(session, stamp)
    profiles = _ensure_profiles(session)
    session.flush()

    start = date.today()
    counts = {profile.id: _rebuild_program(session, profile, settings, bundle, start) for profile in profiles}
    session.commit()
    return SeedReport(
        exercises=exercises,
        workouts=workouts,
        profiles=tuple(profile.id for profile in profiles),
        sessions=counts,
    )


def main(argv: list[str] | None = None) -> int:
    """``python -m cadence.bibliotheque.seed``. One line out, non-zero on a bad library."""
    args = argv if argv is not None else sys.argv[1:]
    reset = "--reset" in args
    engine = init_db()
    try:
        with Session(engine) as session:
            report = seed(session, LIBRARY_DIR, reset=reset)
    except (LibraryError, ProgramBuildError) as exc:
        # Both mean the same thing to the person running `make seed`: nothing was written, and
        # here is why. A library that will not parse and a plan that will not validate are equally
        # not a seeded database, so neither may exit 0.
        sys.stderr.write(f"{exc}\n")
        return 1
    sys.stdout.write(f"{report.summary()}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
