"""The ``session`` and ``session_row`` tables (``docs/architecture.md`` section 3).

Named ``SessionRecord`` / ``SessionRowRecord`` rather than ``Session`` / ``SessionRow``: ``db.py``
imports every table class beside ``sqlmodel.Session``, and a table named ``Session`` would shadow
the database session in the one module that opens them.

A session is created on the **first render** of Today, not on the first tick (D-024): the offline
queue needs a durable ``session_id`` before the first tap, or a tick taken offline has nothing to
address. ``started_at`` still moves on the first tick, so "when did the session begin" is unchanged.
"""

from __future__ import annotations

from sqlalchemy import Index, text
from sqlmodel import Field, SQLModel

FELT_VALUES: tuple[str, ...] = ("easy", "right", "hard")


class SessionRecord(SQLModel, table=True):
    __tablename__ = "session"

    # uuid4, and also the VitalForge ``session_id`` (PRP-06 posts this exact string).
    id: str = Field(primary_key=True)
    profile_id: str = Field(index=True)
    planned_session_id: str = Field(index=True)
    started_at: str | None = Field(default=None)
    finished_at: str | None = Field(default=None)
    duration_min: int | None = Field(default=None)
    felt: str | None = Field(default=None)
    readiness_at_start: float | None = Field(default=None)
    together_group_id: str | None = Field(default=None, index=True)
    notes: str | None = Field(default=None)

    __table_args__ = (
        # One unfinished session per planned session. Two taps on a cold page, or a browser
        # prefetch racing the navigation, otherwise create two sessions over the same checklist
        # and the second silently orphans the first one's ticks. Partial so a finished session
        # does not block the next block from planning over the same row.
        Index(
            "ux_session_open_planned",
            "planned_session_id",
            unique=True,
            sqlite_where=text("finished_at IS NULL"),
        ),
    )


class SessionRowRecord(SQLModel, table=True):
    __tablename__ = "session_row"

    id: str = Field(primary_key=True)
    session_id: str = Field(index=True)
    position: int
    exercise_id: str
    sets_planned: int | None = Field(default=None)
    reps_planned: int | None = Field(default=None)
    seconds_planned: int | None = Field(default=None)
    load_planned_kg: float | None = Field(default=None)
    sets_done: int | None = Field(default=None)
    reps_done: int | None = Field(default=None)
    seconds_done: int | None = Field(default=None)
    load_done_kg: float | None = Field(default=None)
    done: bool = Field(default=False)
    done_at: str | None = Field(default=None)
    is_challenge: bool = Field(default=False)

    __table_args__ = (Index("ux_session_row_position", "session_id", "position", unique=True),)
