"""The ``program`` and ``planned_session`` tables (``docs/architecture.md`` section 3).

The plan is a rolling ordered list, not a calendar (D-011): "today" is the first ``planned`` row
in order, and a missed day stays at the head of the queue rather than piling up as guilt.
"""

from __future__ import annotations

from sqlmodel import Field, SQLModel

# The three states of `planned_session.status` (architecture section 3). Constants rather than
# literals so the rebuild rule, which turns on the difference between them, cannot be written
# against a typo.
PLANNED = "planned"
DONE = "done"
SKIPPED = "skipped"

ACTIVE = "active"


class Program(SQLModel, table=True):
    __tablename__ = "program"

    id: str = Field(primary_key=True)
    profile_id: str = Field(index=True)
    template: str
    start_date: str
    weeks: int = Field(default=4)
    days_per_week: int
    session_minutes: int
    status: str = Field(default=ACTIVE)


class PlannedSession(SQLModel, table=True):
    __tablename__ = "planned_session"

    id: str = Field(primary_key=True)
    program_id: str = Field(index=True)
    profile_id: str = Field(index=True)
    week: int
    day_index: int
    day_type: str
    workout_id: str
    # The materialised rows, with concrete loads. See cadence.programme.materialise.
    rows_json: str
    status: str = Field(default=PLANNED)
