"""Rebuilding a block after a setting changes, without erasing what has been done.

PRP-03 risk 1. The obvious implementation deletes every planned session and calls
``build_program`` again, which throws away the block's history the first time someone changes
days-per-week. This one keeps history and re-plans only the queue ahead of it.

Three rules, in order of how much damage getting them wrong does:

1. A ``planned_session`` is only ever deleted when it is still ``planned`` and nobody has started
   working on it. D-024 creates the session on the first *render* of Today, so "has a session" is
   not the same as "has been touched": ``started_at`` moves on the first tick, and that is the line.
   A checklist with a tick on it survives a rebuild untouched; one that was only ever displayed is
   re-planned, and its empty session row goes with it. Keeping the displayed-but-untouched ones
   would freeze the son on yesterday's band the moment his age was corrected, which is the one
   change a rebuild most needs to land (D-093).
2. The surviving rows keep their place at the front of the queue and the new ones follow. The
   rebuilt plan already numbers its sessions ``week = i // days_per_week + 1``, so dropping as
   many of its leading entries as there are *spent* slots lands the next session in the slot after
   the last one, under the new days-per-week, with no arithmetic of our own. A **skipped** session
   survives as history but spends no slot: it is a day that did not happen, and charging the block
   for it shortens the plan as a punishment for missing a workout.
4. A rebuild always leaves something to do. If the spent slots already fill the block - ten done
   at four days a week, changed to two - the next block starts rather than the profile being left
   with an empty queue and a Today screen that says nothing is planned.
3. ``program.start_date`` is never touched (D-068d). A rebuild is not a new block.
"""

from __future__ import annotations

import logging
from datetime import date

from sqlmodel import Session, select

from cadence.bibliotheque.bundle import LibraryBundle
from cadence.profils.settings import ProgramSettings
from cadence.profils.tables import Profile
from cadence.programme.builder import build_program, program_id
from cadence.programme.tables import PLANNED, SKIPPED, PlannedSession, Program
from cadence.seance.tables import SessionRecord, SessionRowRecord

logger = logging.getLogger(__name__)

PROGRAM_FIELDS: tuple[str, ...] = ("template", "weeks", "days_per_week", "session_minutes", "status")


def _sessions_by_planned(session: Session, profile_id: str) -> dict[str, list[SessionRecord]]:
    """The ``session`` rows pointing at each of this profile's planned sessions."""
    rows = session.exec(select(SessionRecord).where(SessionRecord.profile_id == profile_id)).all()
    grouped: dict[str, list[SessionRecord]] = {}
    for row in rows:
        grouped.setdefault(row.planned_session_id, []).append(row)
    return grouped


def _started(record: SessionRecord) -> bool:
    """Whether anyone has actually worked on this session, as opposed to merely opening it."""
    return record.started_at is not None or record.finished_at is not None


def _discard(session: Session, records: list[SessionRecord]) -> None:
    """Drop an untouched session and its rows, so re-planning leaves nothing pointing at nothing."""
    for record in records:
        for row in session.exec(select(SessionRowRecord).where(SessionRowRecord.session_id == record.id)).all():
            session.delete(row)
        session.delete(record)


def _free_id(natural: str, taken: set[str]) -> str:
    """``natural`` if nothing holds it, else the same id with the first free ``-r`` suffix.

    Renumbering under a new days-per-week can land a fresh session on the ``w2-d0`` id a finished
    one already owns - four days a week with five sessions done, changed to six, is enough - and
    that is a primary-key collision, not a merge. The suffix is deterministic and only appears on
    the rows that would actually have clashed.
    """
    if natural not in taken:
        return natural
    revision = 2
    while f"{natural}-r{revision}" in taken:
        revision += 1
    return f"{natural}-r{revision}"


def _next_sessions(
    built: tuple[PlannedSession, ...],
    kept: list[PlannedSession],
) -> tuple[PlannedSession, ...]:
    """The part of the rebuilt plan that still has to be done.

    Spent slots are the surviving sessions that actually consumed a day: done, or open with work
    on them. Skipped ones are history too, but they consumed nothing, so they do not shorten the
    block. When the spent slots fill it outright the whole block comes back instead of nothing at
    all - the alternative is a profile whose Today says there is nothing planned, which is not a
    state any setting change should be able to produce.
    """
    spent = sum(1 for row in kept if row.status != SKIPPED)
    remaining = built[spent:]
    return remaining or built


def _start_date(existing: Program | None, today: date) -> date:
    if existing is None:
        return today
    try:
        return date.fromisoformat(existing.start_date)
    except ValueError:  # pragma: no cover - only a hand-edited database gets here
        logger.warning("program %s has an unreadable start_date %r; keeping today", existing.id, existing.start_date)
        return today


def rebuild_one(
    session: Session,
    profile: Profile,
    settings: ProgramSettings,
    library: LibraryBundle,
    today: date | None = None,
) -> int:
    """Re-plan the queue ahead of this profile's finished work. Returns the new session count.

    Writes into ``session`` and flushes; it does not commit. The caller owns the transaction, so a
    plan that will not build leaves neither a half-written block nor the setting that asked for it.
    """
    identifier = program_id(profile.id)
    existing_program = session.get(Program, identifier)
    plan = build_program(profile, settings, library, _start_date(existing_program, today or date.today()))

    if existing_program is None:
        session.add(plan.program)
    else:
        for field in PROGRAM_FIELDS:
            setattr(existing_program, field, getattr(plan.program, field))

    rows = session.exec(select(PlannedSession).where(PlannedSession.program_id == identifier)).all()
    attached = _sessions_by_planned(session, profile.id)
    kept = [row for row in rows if row.status != PLANNED or any(map(_started, attached.get(row.id, ())))]
    kept_ids = {row.id for row in kept}

    for row in rows:
        if row.id not in kept_ids:
            _discard(session, attached.get(row.id, []))
            session.delete(row)
    session.flush()

    taken = set(kept_ids)
    written = 0
    for planned in _next_sessions(plan.sessions, kept):
        # Assigning to an unsaved object the builder just minted, not to a loaded row: these have
        # never been near the database, and `model_copy` on a mapped instance would carry its
        # SQLAlchemy state across to the copy.
        planned.id = _free_id(planned.id, taken)
        taken.add(planned.id)
        session.add(planned)
        written += 1
    session.flush()
    return written


def rebuild_programs(
    session: Session,
    library: LibraryBundle,
    reason: str,
    profile_ids: tuple[str, ...] | None = None,
) -> list[str]:
    """Re-plan every profile's block (or just ``profile_ids``). Returns the ids rebuilt.

    Never writes a setting. A rebuild that changed one would call itself through
    ``update_settings`` for as long as the recursion limit allowed (PRP-03 risk 2).
    """
    profiles = list(session.exec(select(Profile).order_by(Profile.id)).all())
    if profile_ids is not None:
        wanted = set(profile_ids)
        profiles = [profile for profile in profiles if profile.id in wanted]
    settings = _current_settings(session)
    rebuilt: list[str] = []
    for profile in profiles:
        rebuild_one(session, profile, settings, library)
        rebuilt.append(profile.id)
    logger.info("rebuilt %s because %s", ", ".join(rebuilt) or "nothing", reason)
    return rebuilt


def _current_settings(session: Session) -> ProgramSettings:
    from cadence.seance.catalog import load_settings

    return load_settings(session)
