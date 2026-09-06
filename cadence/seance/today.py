"""Resolving "today" and creating the session it is about to render.

D-011: the plan is a rolling ordered list, so today is the first ``planned`` row for the profile in
``(week, day_index)`` order. A missed day stays at the head of the queue instead of piling up.

D-024: the ``session`` and its ``session_row``s are written on the first render, inside one
transaction, so the offline queue has a durable id before the first tap.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from cadence.profils.settings import ProgramSettings
from cadence.profils.tables import PROFILE_ME, PROFILE_SON, Profile
from cadence.programme.tables import PLANNED, PlannedSession
from cadence.schema.youth import YouthRuleSet
from cadence.seance.catalog import band_rules, load_settings
from cadence.seance.status import is_promoted
from cadence.seance.tables import SessionRecord, SessionRowRecord

TOGETHER = "together"
PROFILE_KEYS: tuple[str, ...] = (PROFILE_ME, PROFILE_SON, TOGETHER)
DEFAULT_PROFILE_KEY = PROFILE_ME


class TodayError(RuntimeError):
    """A Today request that cannot be served: an unknown profile, or nothing planned."""


@dataclass(frozen=True, slots=True)
class RowView:
    """One checklist row: what was prescribed, and what the user has done to it."""

    spec: dict[str, Any]
    record: SessionRowRecord

    @property
    def position(self) -> int:
        return self.record.position

    @property
    def measure(self) -> str:
        return str(self.spec.get("measure") or "reps")

    @property
    def is_prelude(self) -> bool:
        return bool(self.spec.get("is_prelude"))

    @property
    def adjustable(self) -> bool:
        """A distance row is tick-only: ``session_row`` has no metres column (PRP-02 data model)."""
        return self.measure != "meters"

    @property
    def timed(self) -> bool:
        """The clock control appears on rows with seconds or a real rest, and nowhere else."""
        return self.spec.get("seconds") is not None or int(self.spec.get("rest_s") or 0) > 0

    @property
    def reps(self) -> int | None:
        value = self.record.reps_done if self.record.reps_done is not None else self.spec.get("reps")
        return int(value) if value is not None else None

    @property
    def load_kg(self) -> float | None:
        value = self.record.load_done_kg if self.record.load_done_kg is not None else self.spec.get("load_kg")
        return float(value) if value is not None else None


@dataclass(frozen=True, slots=True)
class SessionView:
    """One profile's checklist for today."""

    profile: Profile
    planned: PlannedSession
    record: SessionRecord
    rows: tuple[RowView, ...]
    rules: YouthRuleSet | None

    @property
    def is_youth(self) -> bool:
        return self.profile.kind == "youth"

    @property
    def rows_total(self) -> int:
        return len(self.rows)

    @property
    def rows_ticked(self) -> int:
        return sum(1 for row in self.rows if row.record.done)

    @property
    def all_ticked(self) -> bool:
        return self.rows_total > 0 and self.rows_ticked == self.rows_total

    @property
    def promoted(self) -> bool:
        """Whether this session has passed its *own* band's ``good_enough_done`` threshold.

        Per session, never per page: on the Together tab the parent is an adult with a threshold
        of one, and reading promotion off him meant the son never saw his own exit however many
        rows he had ticked (principles section 3.7).
        """
        return is_promoted([row.record for row in self.rows], self.rules)

    def row(self, position: int) -> RowView | None:
        return next((row for row in self.rows if row.position == position), None)


@dataclass(frozen=True, slots=True)
class TodayView:
    """What ``GET /today`` renders: one session, or two in Together mode."""

    profile_key: str
    sessions: tuple[SessionView, ...]
    settings: ProgramSettings
    missing: tuple[str, ...] = ()

    @property
    def together(self) -> bool:
        return self.profile_key == TOGETHER

    @property
    def primary(self) -> SessionView:
        return self.sessions[0]


def normalise_profile_key(raw: str | None) -> str:
    """``me``, ``son`` or ``together``. Anything else is an error, never a silent default."""
    key = (raw or DEFAULT_PROFILE_KEY).strip().lower()
    if key not in PROFILE_KEYS:
        raise TodayError(f"unknown profile {raw!r}")
    return key


def _next_planned(db: Session, profile_id: str) -> PlannedSession | None:
    statement = (
        select(PlannedSession)
        .where(PlannedSession.profile_id == profile_id, PlannedSession.status == PLANNED)
        .order_by(PlannedSession.week, PlannedSession.day_index)
    )
    return db.exec(statement).first()


def _open_session(db: Session, planned_id: str) -> SessionRecord | None:
    statement = select(SessionRecord).where(
        SessionRecord.planned_session_id == planned_id,
        SessionRecord.finished_at.is_(None),  # type: ignore[union-attr]
    )
    return db.exec(statement).first()


def _row_records(session_id: str, specs: list[dict[str, Any]]) -> list[SessionRowRecord]:
    return [
        SessionRowRecord(
            id=f"{session_id}-{spec['position']}",
            session_id=session_id,
            position=int(spec["position"]),
            exercise_id=str(spec["exercise_id"]),
            sets_planned=spec.get("sets"),
            reps_planned=spec.get("reps"),
            seconds_planned=spec.get("seconds"),
            load_planned_kg=spec.get("load_kg"),
            is_challenge=bool(spec.get("is_challenge")),
        )
        for spec in specs
    ]


def parse_rows(planned: PlannedSession) -> list[dict[str, Any]]:
    """The materialised rows of this planned session, in position order.

    A ``rows_json`` that will not parse is a corrupt plan, not an empty session: it raises rather
    than rendering a checklist with nothing on it, which the user would tick their way through.
    """
    try:
        parsed = json.loads(planned.rows_json)
    except (TypeError, ValueError) as exc:
        raise TodayError(f"planned session {planned.id!r} has unreadable rows") from exc
    if not isinstance(parsed, list) or not parsed:
        raise TodayError(f"planned session {planned.id!r} has no rows")
    return sorted(parsed, key=lambda row: int(row["position"]))


def ensure_session(db: Session, planned: PlannedSession, together_group_id: str | None = None) -> SessionRecord:
    """The open session over this planned session, creating it and its rows if absent.

    Insert-or-read under one unique index: a double tap or a prefetch racing the navigation loses
    the insert and re-reads the winner rather than opening a second session over one checklist.
    """
    existing = _open_session(db, planned.id)
    if existing is not None:
        return _adopt_group(db, existing, together_group_id)

    specs = parse_rows(planned)
    record = SessionRecord(
        id=str(uuid.uuid4()),
        profile_id=planned.profile_id,
        planned_session_id=planned.id,
        together_group_id=together_group_id,
    )
    db.add(record)
    for row in _row_records(record.id, specs):
        db.add(row)
    try:
        db.commit()
    except IntegrityError:
        # Someone else won the race. Roll back first: an errored transaction poisons every
        # statement after it, so the re-read would fail too.
        db.rollback()
        winner = _open_session(db, planned.id)
        if winner is None:  # pragma: no cover - the index only fires when a winner exists
            raise
        return _adopt_group(db, winner, together_group_id)
    db.refresh(record)
    return record


def _adopt_group(db: Session, record: SessionRecord, together_group_id: str | None) -> SessionRecord:
    """Put an already-open session into a Together group, or take it out of one.

    Opening Today solo and then switching to Together must link the two sessions, not start a
    third: the shared id is what makes one Done finalise both (D-013). The reverse holds too --
    going back to the Me tab detaches, so a Done there finishes one person's session and not the
    other's. The tab the user is looking at is what says whether this is a joint session (D-075).
    """
    if record.together_group_id == together_group_id:
        return record
    record.together_group_id = together_group_id
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def load_rows(db: Session, record: SessionRecord, planned: PlannedSession) -> tuple[RowView, ...]:
    """Pair each stored row with the spec it was materialised from, in position order."""
    specs = {int(spec["position"]): spec for spec in parse_rows(planned)}
    statement = (
        select(SessionRowRecord).where(SessionRowRecord.session_id == record.id).order_by(SessionRowRecord.position)  # type: ignore[arg-type]
    )
    stored = db.exec(statement).all()
    return tuple(RowView(spec=specs[row.position], record=row) for row in stored if row.position in specs)


def session_view(db: Session, profile: Profile, planned: PlannedSession, group_id: str | None = None) -> SessionView:
    """Resolve, create if needed, and load one profile's checklist."""
    record = ensure_session(db, planned, group_id)
    return SessionView(
        profile=profile,
        planned=planned,
        record=record,
        rows=load_rows(db, record, planned),
        rules=band_rules(profile),
    )


def view_for_session(db: Session, record: SessionRecord) -> SessionView | None:
    """Rebuild a view around an existing session id, for the tick and Done routes."""
    profile = db.get(Profile, record.profile_id)
    planned = db.get(PlannedSession, record.planned_session_id)
    if profile is None or planned is None:
        return None
    return SessionView(
        profile=profile,
        planned=planned,
        record=record,
        rows=load_rows(db, record, planned),
        rules=band_rules(profile),
    )


def _profile_ids(profile_key: str) -> tuple[str, ...]:
    return (PROFILE_ME, PROFILE_SON) if profile_key == TOGETHER else (profile_key,)


def resolve_today(db: Session, raw_profile: str | None) -> TodayView:
    """Today for one profile, or both of them linked by a shared group id (D-013).

    Together with only one profile planned renders that one and names the other in a line, rather
    than refusing the whole page.
    """
    profile_key = normalise_profile_key(raw_profile)
    settings = load_settings(db)

    found: list[tuple[Profile, PlannedSession]] = []
    missing: list[str] = []
    for profile_id in _profile_ids(profile_key):
        profile = db.get(Profile, profile_id)
        planned = _next_planned(db, profile_id) if profile is not None else None
        if profile is None or planned is None:
            missing.append(profile.display_name if profile is not None else profile_id)
            continue
        found.append((profile, planned))

    if not found:
        raise TodayError(f"nothing is planned for {profile_key!r}")

    shared = _group_id(db, found) if profile_key == TOGETHER else None
    sessions = tuple(session_view(db, profile, planned, shared) for profile, planned in found)
    return TodayView(profile_key=profile_key, sessions=sessions, settings=settings, missing=tuple(missing))


def _group_id(db: Session, found: list[tuple[Profile, PlannedSession]]) -> str | None:
    """The Together group these two sessions share, reusing one already stamped.

    A fresh uuid on every render would re-link the pair each time the page loads, so a Done
    posted from a stale tab would iterate over a group that no longer exists. A group of one is
    left unstamped: it is a solo session that happens to be rendered on the Together tab, and a
    later Done must not go looking for a partner that was never there.
    """
    if len(found) < 2:
        return None
    for _, planned in found:
        existing = _open_session(db, planned.id)
        if existing is not None and existing.together_group_id:
            return existing.together_group_id
    return str(uuid.uuid4())
