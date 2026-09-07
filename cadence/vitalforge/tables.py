"""The ``metrics_cache`` table (``docs/architecture.md`` section 3).

PRP-06 owns filling it from VitalForge; PRP-04 only reads it, so this build creates it empty and
renders "No data yet." until something writes a row. The table lives here rather than in
``cadence/historique/`` because architecture section 2 puts the VitalForge cache in this module,
and PRP-06 should find it where it expects it (D-101).

``payload_json`` shape is fixed by D-101 and is exactly the ``trend`` object of PRP-04's
``GET /api/scorecard``::

    {"weight_kg": [["2026-08-08", 85.2], ...], "body_fat_pct": [["2026-08-08", 19.1], ...]}

Extra keys are ignored, so PRP-06 may cache readiness and the rest alongside these two series;
PRP-06 stores the scalars under ``latest`` rather than at the top level, so that ``weight_kg``
keeps meaning "the series" to the reader that already exists (D-120).

``sync_job`` is PRP-06's own, and lives here for the same reason: the write-back queue is part of
the VitalForge integration, and ``cadence/historique/`` only reads its ``status`` for the badge.
"""

from __future__ import annotations

from sqlmodel import Field, SQLModel

# ``sync_job.target``. One integration today; the column exists so a second one can be told apart.
VITALFORGE_TARGET = "vitalforge"

# ``sync_job.status``. See the table in PRP-06 section 3 for what each means alongside
# ``next_attempt_at``, which is what actually decides whether a job is terminal.
PENDING = "pending"
SENT = "sent"
FAILED = "failed"
SKIPPED = "skipped"
# Terminal, and the only status a *person* causes: the household turned the son's Garmin
# push off while this job was still queued to make one (D-139).
CANCELLED = "cancelled"
SYNC_STATUSES: tuple[str, ...] = (PENDING, SENT, FAILED, SKIPPED, CANCELLED)


class MetricsCache(SQLModel, table=True):
    __tablename__ = "metrics_cache"

    # One row per profile: the latest pull, not a history table. The series live in the payload.
    profile_id: str = Field(primary_key=True)
    fetched_at: str
    payload_json: str
    # True when the last pull failed and this is the previous answer being served on.
    stale: bool = Field(default=False)


class SyncJob(SQLModel, table=True):
    """One session's write-back to VitalForge (``docs/architecture.md`` section 3).

    ``session_id`` is unique, which is the database's backstop for :func:`cadence.vitalforge.sync.enqueue`
    being get-or-create: ``POST /api/sessions/{id}/done`` is idempotent and is the offline queue's
    replay target, so a blind insert would file the same session twice.

    ``next_attempt_at`` carries the whole retry decision. **NULL means terminal** - a 409, a 422,
    a success, or an exhausted attempt budget - and the drainer never looks at such a job again.
    """

    __tablename__ = "sync_job"

    id: str = Field(primary_key=True)
    session_id: str = Field(unique=True, index=True)
    target: str = Field(default=VITALFORGE_TARGET)
    status: str = Field(default=PENDING)
    attempts: int = Field(default=0)
    # NULL is terminal. Set means "retry at or after this UTC ISO instant".
    next_attempt_at: str | None = Field(default=None)
    # Operator-readable, and never the token: everything written here goes through
    # ``cadence.vitalforge.errors.sanitise`` first.
    last_error: str | None = Field(default=None)
    payload_json: str = Field(default="{}")
    remote_ref: str | None = Field(default=None)
    # The VitalForge person this job is addressed to, frozen when the job was queued. Retries
    # must not re-resolve it: a slug edited between a timed-out attempt and its retry would file
    # a finished session under a different human (D-139). Blank means "nobody was configured
    # yet", and that one case *is* resolved later, because a job queued before ``.env`` was
    # filled in has to be able to heal (D-138).
    target_slug: str = Field(default="")
    # When the last POST was *started*, written by the optimistic claim before the request goes
    # out. The manual-retry throttle reads it; ``next_attempt_at`` cannot answer "how long ago"
    # because a 404 or a 401 rewrites the schedule without burning an attempt.
    last_attempt_at: str | None = Field(default=None)
    # The last HTTP status, so a refusal can name its reason. A 422 is the one failure a person
    # tapping Retry can never fix, and the button must not offer to try it again.
    last_status: int | None = Field(default=None)
