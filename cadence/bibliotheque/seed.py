"""``make seed`` - loads ``library/`` into the database and builds a block for each profile.

Idempotent: every write is an upsert keyed by a deterministic id, and nothing whose ``source`` is
not ``seed`` is ever touched, so an imported or generated workout survives a re-seed.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from sqlmodel import Session, select

from cadence.bibliotheque.loader import LIBRARY_DIR, LibraryError, load_library
from cadence.config import get_settings
from cadence.db import ExerciseRecord, WorkoutRecord, init_db
from cadence.profils.rebuild import rebuild_one
from cadence.profils.settings import DEFAULT_SETTINGS, ProgramSettings, settings_from_rows
from cadence.profils.tables import PROFILE_ME, PROFILE_SON, Profile, Setting
from cadence.profils.validation import check_person_slug
from cadence.programme.bands import age_band
from cadence.programme.errors import ProgramBuildError
from cadence.programme.tables import PlannedSession, Program

SEED_SOURCE = "seed"


class SeedError(RuntimeError):
    """A seed that will not run because the environment it was handed is wrong.

    Distinct from ``LibraryError``: the library is fine, the operator's ``.env`` is not. Both mean
    nothing was written and neither may exit 0.
    """


# D-091. Demo data is *set up* data: `make seed && make dev` has to open Today, and PRP-03's gate
# would otherwise send every seeded install to a Setup screen it has already answered for them.
# `--fresh` (or CADENCE_SEED_SETUP_COMPLETE=0) is the true first run, and it is the one setting a
# re-seed overwrites: a flag whose whole purpose is "give me the front door back" that declined to
# clear an existing `true` would do nothing at all on the second run.
SETUP_COMPLETE_ENV = "CADENCE_SEED_SETUP_COMPLETE"
FRESH_FLAG = "--fresh"


def _setup_complete_from_env(args: list[str]) -> bool:
    """Whether this seed marks setup done. Read from the process env, never from ``config``.

    Deliberately not a ``cadence.config`` field: settings there are cached process-wide and the
    test suite scrubs a fixed list of names, so a build flag added to it leaks between tests as
    ambient environment rather than staying an argument to one command.
    """
    if FRESH_FLAG in args:
        return False
    return os.environ.get(SETUP_COMPLETE_ENV, "1").strip().lower() not in {"0", "false", "no"}


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


def _ensure_settings(session: Session, stamp: str, setup_complete: bool = True) -> ProgramSettings:
    """Install the defaults for any setting not already stored; never overwrite a chosen one.

    ``setup_complete`` is the one exception, and only downwards: a ``--fresh`` seed rewrites it to
    false so the next request lands on ``/setup`` (D-091). Marking it *true* still only fills a
    gap, so an install part-way through setup is not quietly declared finished.
    """
    defaults = DEFAULT_SETTINGS.with_changes(setup_complete=setup_complete).as_rows()
    stored = {row.key: row.value_json for row in session.exec(select(Setting)).all()}
    for key, value in defaults.items():
        existing = session.get(Setting, key)
        forced = key == "setup_complete" and not setup_complete
        if existing is None:
            session.add(Setting(key=key, value_json=value, updated_at=stamp))
            stored[key] = value
        elif forced:
            existing.value_json = value
            existing.updated_at = stamp
            stored[key] = value
    return settings_from_rows(stored)


def _env_slug(name: str, value: str) -> str:
    """A person slug from the environment, checked the same way a typed one is.

    ``_ensure_profiles`` writes these straight onto the profile rows, which is the one entry point
    that does not pass through ``validate_profile_patch``, so a bad slug in ``.env`` reached the
    database however carefully the screen was validated. Operator input is still input: the slug
    ends up interpolated into ``/p/{slug}/api/...`` by PRP-06 exactly like a typed one (D-114).
    Blank stays legal and means "not set" (D-017).
    """
    problems = check_person_slug(value)
    if problems:
        raise SeedError(f"{name} is not a usable VitalForge person slug: {problems[0].message}")
    return value


def _ensure_profiles(session: Session) -> list[Profile]:
    """The two demo profiles. An existing profile keeps whatever the user has set on it."""
    config = get_settings()
    person_me = _env_slug("VITALFORGE_PERSON_ME", config.vitalforge_person_me)
    person_son = _env_slug("VITALFORGE_PERSON_SON", config.vitalforge_person_son)
    wanted = (
        Profile(
            id=PROFILE_ME,
            display_name="Me",
            kind="adult",
            age_years=None,
            vitalforge_person=person_me,
            push_to_garmin=True,
        ),
        Profile(
            id=PROFILE_SON,
            display_name="Son",
            kind="youth",
            # Section 3.8: until the age is set the son is evaluated against the strictest band.
            age_years=None,
            vitalforge_person=person_son,
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
            # Backfill a blank slug from the environment on every run, never overwrite a set one.
            # The two profiles are seeded before ``.env`` is filled in on a new install, so
            # without this the person slugs stay empty for the life of the database and every
            # write-back is skipped however correctly VITALFORGE_PERSON_* is set afterwards
            # (D-138). A slug the user has chosen in Settings is theirs and is left alone.
            wanted_slug = person_me if profile.id == PROFILE_ME else person_son
            if not existing.vitalforge_person and wanted_slug:
                existing.vitalforge_person = wanted_slug
            live.append(existing)
    return live


def _rebuild_program(session: Session, profile: Profile, settings: ProgramSettings, bundle, start: date) -> int:
    """Re-plan one profile's block. One implementation, shared with Settings (D-098d).

    This used to be a second rebuild with its own rules: it deleted on ``status == "planned"``
    alone, where PRP-03's rule also requires that nobody has started the session (D-093). Two
    rebuilds that disagree about what may be deleted is one too many, so ``make seed`` and the
    Settings screen now go through the same code and `start_date` is preserved in one place
    (D-068d).
    """
    return rebuild_one(session, profile, settings, bundle, start)


def seed(
    session: Session,
    path: Path = LIBRARY_DIR,
    reset: bool = False,
    setup_complete: bool = True,
) -> SeedReport:
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
    settings = _ensure_settings(session, stamp, setup_complete)
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
    complete = _setup_complete_from_env(args)
    engine = init_db()
    try:
        with Session(engine) as session:
            report = seed(session, LIBRARY_DIR, reset=reset, setup_complete=complete)
    except (LibraryError, ProgramBuildError, SeedError) as exc:
        # Both mean the same thing to the person running `make seed`: nothing was written, and
        # here is why. A library that will not parse and a plan that will not validate are equally
        # not a seeded database, so neither may exit 0.
        sys.stderr.write(f"{exc}\n")
        return 1
    sys.stdout.write(f"{report.summary()}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
