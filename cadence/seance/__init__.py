"""Today: resolving the session, ticking its rows, adjusting them, and finishing.

PRP-02 owns the ``session`` and ``session_row`` tables and the services over them (D-023);
PRP-04 only queries them.
"""

from __future__ import annotations

from cadence.seance.done import Summary, finish, set_felt, summarise
from cadence.seance.status import completion, good_enough_after, is_promoted, rows_done
from cadence.seance.tables import SessionRecord, SessionRowRecord
from cadence.seance.ticks import TickError, TickResult, adjust, apply_patch, set_done, toggle
from cadence.seance.today import (
    PROFILE_KEYS,
    TOGETHER,
    RowView,
    SessionView,
    TodayError,
    TodayView,
    resolve_today,
    view_for_session,
)

__all__ = [
    "PROFILE_KEYS",
    "TOGETHER",
    "RowView",
    "SessionRecord",
    "SessionRowRecord",
    "SessionView",
    "Summary",
    "TickError",
    "TickResult",
    "TodayError",
    "TodayView",
    "adjust",
    "apply_patch",
    "completion",
    "finish",
    "good_enough_after",
    "is_promoted",
    "resolve_today",
    "rows_done",
    "set_done",
    "set_felt",
    "summarise",
    "toggle",
    "view_for_session",
]
