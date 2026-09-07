"""The ``assessment`` and ``challenge`` tables (``docs/architecture.md`` section 3).

A test that could not be run is stored with ``value = NULL`` and ``unit = "unavailable"`` - not
zero (D-019, principles section 7.5.2). Zero is a real measurement and would rank as the worst
possible gap; "unavailable" is the absence of one and is never a gap at all.
"""

from __future__ import annotations

from sqlalchemy import Index
from sqlmodel import Field, SQLModel

#: ``assessment.unit`` values. ``unavailable`` is a unit, not a value, so the column stays typed.
UNIT_REPS = "reps"
UNIT_SECONDS = "s"
UNIT_SCORE = "score"
UNIT_UNAVAILABLE = "unavailable"

#: ``challenge.status`` values.
ACTIVE = "active"
MET = "met"
EXPIRED = "expired"

#: Section 7.5.5, and the ceiling the challenge generator is held to.
MAX_ACTIVE_CHALLENGES = 3
#: Section 7: baseline on day one, then every four weeks.
RETEST_DAYS = 28


class Assessment(SQLModel, table=True):
    __tablename__ = "assessment"

    id: str = Field(primary_key=True)
    profile_id: str = Field(index=True)
    test_id: str = Field(index=True)
    value: float | None = Field(default=None)
    unit: str
    recorded_on: str = Field(index=True)
    self_rated: bool = Field(default=False)

    __table_args__ = (
        # One result per test per date. Re-posting a date replaces that date's battery rather
        # than appending a second one beside it, which would double every trend and every gap.
        Index("ux_assessment_day", "profile_id", "test_id", "recorded_on", unique=True),
    )


class Challenge(SQLModel, table=True):
    __tablename__ = "challenge"

    id: str = Field(primary_key=True)
    profile_id: str = Field(index=True)
    name: str
    test_id: str
    target_value: float
    unit: str = Field(default=UNIT_REPS)
    baseline_on: str
    due_on: str
    status: str = Field(default=ACTIVE)
    # The ``WorkoutRowSpec`` of section 7.7 in the shape ``materialise_rows`` produces, plus
    # ``{"frequency": n, "day_types": [...]}``. Null for ``body_comp``, which inserts no row.
    row_json: str | None = Field(default=None)
