"""Whether a session counts as finished, and how much of it was done.

Completion is **derived, not stored**: ``planned_session.status`` keeps architecture's
``planned | done | skipped`` and nothing carries a fourth value. The judgment lives here so
PRP-04's streak and PRP-07's R7 exclusion ask one function rather than two.

Principles section 3.7: the Done control is always tappable. ``good_enough_done_after_n_exercises``
is the threshold at which a session counts as *complete* and the control is promoted in the UI --
never the point at which it appears.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Literal, Protocol

from cadence.seance.tables import SessionRowRecord

Completion = Literal["complete", "partial"]

# The adult threshold of principles section 3.2: one tick makes an adult session complete.
DEFAULT_GOOD_ENOUGH_AFTER = 1


class _HasGoodEnough(Protocol):
    good_enough_done_after_n_exercises: int


def rows_done(rows: Iterable[SessionRowRecord]) -> int:
    """How many rows carry a tick."""
    return sum(1 for row in rows if row.done)


def good_enough_after(band_rules: _HasGoodEnough | None) -> int:
    """The band's promotion threshold, at least 1.

    ``None`` means the band table was unreadable; the adult threshold is the safe fallback,
    because a session the user finished must never be recorded as partial for want of a rule file.
    """
    if band_rules is None:
        return DEFAULT_GOOD_ENOUGH_AFTER
    return max(int(band_rules.good_enough_done_after_n_exercises), 1)


def completion(rows: Sequence[SessionRowRecord], band_rules: _HasGoodEnough | None) -> Completion:
    """``complete`` iff enough rows are ticked for the profile's band, else ``partial``."""
    return "complete" if rows_done(rows) >= good_enough_after(band_rules) else "partial"


def is_promoted(rows: Sequence[SessionRowRecord], band_rules: _HasGoodEnough | None) -> bool:
    """Whether the Done control should show its "Good enough - done!" face."""
    return completion(rows, band_rules) == "complete"
