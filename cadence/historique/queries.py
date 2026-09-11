"""Reading finished sessions back out, in one statement each.

Thirty sessions must cost the same number of round trips as one (PRP-04 risk 7): the row counts
come from a grouped ``COUNT``, never from a query per session, and the limit is applied in SQL.

``sync_job`` belongs to PRP-06 and may not exist yet. The join is added only when the table is
there; without it every badge reads "Stored locally", which is the truth on a build that has
nothing to sync with (PRP-04 risk 9).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.inspection import inspect
from sqlmodel import Session

from cadence.historique.clock import format_day, local_date, parse_utc
from cadence.profils.tables import Profile
from cadence.schema.labels import day_label
from cadence.seance.catalog import band_rules
from cadence.seance.status import Completion, good_enough_after

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 30
MIN_LIMIT = 1
MAX_LIMIT = 200

# Sorts before every real timestamp, for a session whose ``finished_at`` will not parse.
EPOCH = datetime.min.replace(tzinfo=UTC)

# The badge shown when ``sync_job`` does not exist, or carries no row for this session.
SYNC_LOCAL = "local"

SYNC_LABELS: dict[str, tuple[str, str]] = {
    "sent": ("✓", "Synced"),
    "pending": ("⟳", "Will sync"),
    "failed": ("!", "Sync failed"),
    "skipped": ("·", "Not sent"),
    # PRP-06: a person switched the son's Garmin push off while this one was queued. Not a
    # failure, and the badge must not read like one.
    "cancelled": ("·", "Cancelled"),
    SYNC_LOCAL: ("·", "Stored locally"),
}


# ``right`` reads as "just right" on screen; the stored value stays the three-way of PRP-02.
FELT_LABELS: dict[str, str] = {"easy": "easy", "right": "just right", "hard": "hard"}


class LimitOutOfRange(ValueError):
    """A ``?limit=`` outside 1..200."""


def clamp_limit(raw: int) -> int:
    """Validate ``?limit=``; out of range is an error, never a silent clamp."""
    if raw < MIN_LIMIT or raw > MAX_LIMIT:
        raise LimitOutOfRange(f"limit must be between {MIN_LIMIT} and {MAX_LIMIT}, not {raw}")
    return raw


@dataclass(frozen=True, slots=True)
class SessionSummary:
    """One finished session, as the history list and ``GET /api/sessions`` show it."""

    id: str
    profile_id: str
    # The instant, kept alongside the local date so two profiles' lists can be merged in true
    # order: a local date alone ties every session finished on the same day.
    finished: datetime | None
    day: date | None
    day_type: str
    day_name: str
    rows_done: int
    rows_total: int
    felt: str | None
    duration_min: int
    completion: Completion
    together: bool
    sync_status: str

    @property
    def sort_key(self) -> tuple[datetime, str]:
        """Newest first, with the id breaking a tie so the order is stable across calls."""
        return (self.finished or EPOCH, self.id)

    @property
    def date_label(self) -> str:
        return format_day(self.day)

    @property
    def counts(self) -> str:
        return f"{self.rows_done} of {self.rows_total}"

    @property
    def felt_label(self) -> str:
        """How it felt, in words. Empty when the session was finished without an answer."""
        return FELT_LABELS.get(self.felt or "", "")

    @property
    def sync_glyph(self) -> str:
        return SYNC_LABELS.get(self.sync_status, SYNC_LABELS[SYNC_LOCAL])[0]

    @property
    def sync_label(self) -> str:
        return SYNC_LABELS.get(self.sync_status, SYNC_LABELS[SYNC_LOCAL])[1]

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "profile": self.profile_id,
            "date": self.day.isoformat() if self.day else None,
            "day_type": self.day_type,
            "day_name": self.day_name,
            "rows_done": self.rows_done,
            "rows_total": self.rows_total,
            "felt": self.felt,
            "duration_min": self.duration_min,
            "completion": self.completion,
            "together": self.together,
            "sync_status": self.sync_status,
        }


def has_table(db: Session, name: str) -> bool:
    """Whether a table PRP-06 owns has landed yet. Never raises on a database that is mid-build."""
    try:
        return bool(inspect(db.get_bind()).has_table(name))
    except SQLAlchemyError:  # pragma: no cover - only on a connection already broken
        logger.warning("could not check whether the %r table exists; treating it as absent", name, exc_info=True)
        return False


def has_column(db: Session, table: str, column: str) -> bool:
    """Whether a column added after a database was created is present in it.

    The same shape as ``has_table`` and for the same reason: there is no migration runner, so a
    column this build declares is created by ``SQLModel.metadata.create_all`` on a fresh database
    and simply absent on one that predates it. A reader that asks first degrades to "no value"
    instead of raising ``no such column`` at the deployed install (D-232).
    """
    try:
        inspector = inspect(db.get_bind())
        if not inspector.has_table(table):
            return False
        return any(entry["name"] == column for entry in inspector.get_columns(table))
    except SQLAlchemyError:  # pragma: no cover - only on a connection already broken
        logger.warning(
            "could not check whether %r.%r exists; treating it as absent", table, column, exc_info=True
        )
        return False


# ``datetime()`` rather than the bare column: SQLite normalises an ISO offset to UTC, and a
# plain string sort would put "2026-09-07T00:30:00+02:00" after "2026-09-06T22:30:00+00:00"
# although they are the same instant. Everything this build writes is UTC; a replayed Done from
# some other client need not be. ``s.id`` breaks a tie so the order is stable across calls.
_LIST_SQL = """
SELECT s.id            AS id,
       s.profile_id    AS profile_id,
       s.finished_at   AS finished_at,
       s.duration_min  AS duration_min,
       s.felt          AS felt,
       s.together_group_id AS together_group_id,
       COALESCE(p.day_type, '') AS day_type,
       COUNT(r.id)     AS rows_total,
       COALESCE(SUM(CASE WHEN r.done THEN 1 ELSE 0 END), 0) AS rows_done{sync_select}
FROM session s
LEFT JOIN planned_session p ON p.id = s.planned_session_id
LEFT JOIN session_row r ON r.session_id = s.id
WHERE {where}
GROUP BY s.id
ORDER BY datetime(s.finished_at) DESC, s.id DESC
{tail}
"""

_BY_PROFILE = "s.profile_id = :profile_id AND s.finished_at IS NOT NULL"
_BY_ID = "s.id = :session_id AND s.finished_at IS NOT NULL"

# A correlated subquery rather than a join and an aggregate. ``MAX(j.status)`` picked the badge
# **alphabetically** -- "sent" beats "pending" beats "failed" -- which is the one ordering that
# reports the most reassuring answer rather than the current one. ``sync_job`` is idempotent on
# ``session_id`` (architecture section 3) so there is normally one row; ordering by id keeps the
# pick deterministic if that ever stops being true. Still one statement: no N+1.
_SYNC_SELECT = """,
       (SELECT j.status FROM sync_job j
         WHERE j.session_id = s.id AND j.target = 'vitalforge'
         ORDER BY j.id DESC LIMIT 1) AS sync_status"""


def _statement(with_sync: bool, where: str, tail: str = "") -> str:
    """The one list query, with the ``sync_job`` lookup spliced in only when the table is there.

    Every value is bound; the substitutions are module constants, never anything a request can
    reach.
    """
    return _LIST_SQL.format(sync_select=_SYNC_SELECT if with_sync else "", where=where, tail=tail)


def completion_threshold(db: Session, profile_id: str) -> int:
    """The band threshold ``completion`` is judged against, for a profile that may be missing."""
    profile = db.get(Profile, profile_id)
    return good_enough_after(band_rules(profile)) if profile is not None else 1


def recent_sessions(db: Session, profile_id: str, limit: int = DEFAULT_LIMIT) -> list[SessionSummary]:
    """The profile's finished sessions, newest first, capped in SQL.

    Together sessions appear once here: each profile owns its own ``session`` row, so a joint
    workout is one row on each list rather than two on either (PRP-04 risk 10).
    """
    size = clamp_limit(limit)
    threshold = completion_threshold(db, profile_id)
    with_sync = has_table(db, "sync_job")
    statement = _statement(with_sync, _BY_PROFILE, "LIMIT :limit")
    rows = db.execute(text(statement), {"profile_id": profile_id, "limit": size}).all()
    return [_summary(row._mapping, threshold, with_sync) for row in rows]


def session_summary(db: Session, session_id: str) -> SessionSummary | None:
    """One finished session by id, for the expand-in-place partial."""
    with_sync = has_table(db, "sync_job")
    row = db.execute(text(_statement(with_sync, _BY_ID)), {"session_id": session_id}).first()
    if row is None:
        return None
    return _summary(row._mapping, completion_threshold(db, str(row._mapping["profile_id"])), with_sync)


def _summary(row: dict, threshold: int, with_sync: bool) -> SessionSummary:
    done = int(row["rows_done"] or 0)
    finished = parse_utc(row["finished_at"])
    status = (row["sync_status"] or SYNC_LOCAL) if with_sync else SYNC_LOCAL
    day_type = str(row["day_type"])
    return SessionSummary(
        id=str(row["id"]),
        profile_id=str(row["profile_id"]),
        finished=finished,
        day=finished.astimezone().date() if finished is not None else None,
        day_type=day_type,
        day_name=day_label(day_type),
        rows_done=done,
        rows_total=int(row["rows_total"] or 0),
        felt=row["felt"],
        duration_min=int(row["duration_min"] or 0),
        # The same threshold ``seance.status.completion`` applies, asked of a counted row rather
        # than of loaded records: one definition, no second copy to drift (PRP-04 risk 1).
        completion="complete" if done >= threshold else "partial",
        together=bool(row["together_group_id"]),
        sync_status=str(status),
    )


_FINISHED_SQL = """
SELECT s.finished_at AS finished_at
FROM session s
WHERE s.profile_id = :profile_id AND s.finished_at IS NOT NULL
"""

_TICKS_SQL = """
SELECT COALESCE(SUM(CASE WHEN r.done THEN 1 ELSE 0 END), 0) AS ticked
FROM session s
JOIN session_row r ON r.session_id = s.id
WHERE s.profile_id = :profile_id AND s.finished_at IS NOT NULL
"""


def finished_dates(db: Session, profile_id: str) -> list[date]:
    """The local date of every finished session, for the weekly count and the total.

    One statement for the whole history: a household's is small, and filtering by a UTC range in
    SQL would put a Sunday-evening session in the wrong local week.
    """
    rows = db.execute(text(_FINISHED_SQL), {"profile_id": profile_id}).all()
    return [day for day in (local_date(row._mapping["finished_at"]) for row in rows) if day is not None]


def total_rows_ticked(db: Session, profile_id: str) -> int:
    """Every ticked row across every finished session for this profile."""
    row = db.execute(text(_TICKS_SQL), {"profile_id": profile_id}).first()
    return int(row._mapping["ticked"] or 0) if row is not None else 0
