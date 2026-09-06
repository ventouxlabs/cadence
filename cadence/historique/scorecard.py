"""The weekly count and the streak — the one place either is defined.

**The streak counts consecutive completed planned sessions in program order, not calendar days.**
The plan is a queue, not a calendar (D-011), so "days in a row" would read as broken after every
rest day. The rules, in full, and the only statement of them (PRP-04 risk 1):

- Walk ``planned_session`` for the profile in ``(week, day_index)`` order, oldest first.
- ``done`` and complete  -> +1.
- ``done`` and partial   -> holds. A short session is not a failure (principles section 3.7).
- ``skipped``            -> resets to 0.
- ``planned``            -> ends the walk; nothing after it counts.
- Together sessions count once per profile: each profile owns its own ``planned_session`` row.

``current_streak`` is the value at the end of the walk, ``best_streak`` the maximum reached during
it. PRP-07's missed-session logic and PRP-10's badges import this module rather than restating it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import text
from sqlmodel import Session

from cadence.historique.clock import same_iso_week, week_start
from cadence.historique.queries import finished_dates, total_rows_ticked
from cadence.profils.tables import Profile
from cadence.seance.catalog import band_rules, load_settings
from cadence.seance.status import good_enough_after

PLANNED = "planned"
DONE = "done"
SKIPPED = "skipped"

# How many dots the card draws, so a very high count does not wrap off a 390 px screen.
MAX_DOTS = 10


@dataclass(frozen=True, slots=True)
class Streak:
    current: int
    best: int


@dataclass(frozen=True, slots=True)
class Scorecard:
    """What the "This week" card shows, and what ``GET /api/scorecard`` returns."""

    profile_id: str
    week_start: date
    done_this_week: int
    planned_this_week: int
    days_per_week: int
    current_streak: int
    best_streak: int
    total_sessions: int
    total_rows_ticked: int

    @property
    def headline(self) -> str:
        """``3 of 4 sessions``. The count may exceed the plan and is never clamped."""
        return f"{self.done_this_week} of {self.planned_this_week}"

    @property
    def dots(self) -> list[bool]:
        """One dot per planned session, filled for each one done, capped for the layout."""
        width = min(max(self.planned_this_week, self.done_this_week), MAX_DOTS)
        return [index < self.done_this_week for index in range(width)]

    def as_dict(self) -> dict[str, object]:
        return {
            "profile": self.profile_id,
            "week_start": self.week_start.isoformat(),
            "done_this_week": self.done_this_week,
            "planned_this_week": self.planned_this_week,
            "days_per_week": self.days_per_week,
            "current_streak": self.current_streak,
            "best_streak": self.best_streak,
            "total_sessions": self.total_sessions,
            "total_rows_ticked": self.total_rows_ticked,
        }


_WALK_SQL = """
SELECT p.status AS status,
       COUNT(DISTINCT s.id) AS sessions,
       COALESCE(SUM(CASE WHEN r.done THEN 1 ELSE 0 END), 0) AS rows_done
FROM planned_session p
LEFT JOIN session s ON s.planned_session_id = p.id AND s.finished_at IS NOT NULL
LEFT JOIN session_row r ON r.session_id = s.id
WHERE p.profile_id = :profile_id
GROUP BY p.id
ORDER BY p.week, p.day_index
"""


def current_streak(db: Session, profile_id: str) -> Streak:
    """Walk the plan once and return the streak it ends on, and the best it reached.

    One statement for the whole walk: rebuilding a session view per planned row would put the
    scorecard's cost back on the page the session list just took it off (PRP-04 risk 7).

    **The walk is scoped to the profile, not to a program**, and rests on architecture section 3's
    "one active program per profile": ``bibliotheque.seed.program_id`` derives the id from the
    profile slug, so a rebuild reuses it and a profile never owns two blocks. Break that invariant
    and two blocks interleave by ``(week, day_index)``, a stale ``planned`` row ends the walk early,
    and every screen quoting the streak is quietly wrong. Anything that creates a second program
    per profile has to scope this query to the active one.
    """
    profile = db.get(Profile, profile_id)
    threshold = good_enough_after(band_rules(profile)) if profile is not None else 1
    rows = db.execute(text(_WALK_SQL), {"profile_id": profile_id}).all()

    current = 0
    best = 0
    for row in rows:
        mapping = row._mapping
        status = str(mapping["status"])
        if status == PLANNED:
            break
        if status == SKIPPED:
            current = 0
            continue
        if status != DONE:
            # An unknown status is not a failure anyone chose; it holds rather than resets.
            continue
        # A ``done`` row with no finished session cannot be shown to be complete, so it holds.
        complete = int(mapping["sessions"] or 0) > 0 and int(mapping["rows_done"] or 0) >= threshold
        if complete:
            current += 1
            best = max(best, current)
    return Streak(current=current, best=best)


def weekly_scorecard(db: Session, profile_id: str, today: date | None = None) -> Scorecard:
    """This week against the plan, plus the streak and the all-time totals.

    ``done_this_week`` counts sessions whose ``finished_at`` falls in ``today``'s ISO week, Monday
    start, in server local time. It can exceed ``days_per_week``; ``5 of 4`` is the honest answer.
    """
    day = today or date.today()
    settings = load_settings(db)
    days_per_week = int(settings.days_per_week)
    dates = finished_dates(db, profile_id)
    streak = current_streak(db, profile_id)
    return Scorecard(
        profile_id=profile_id,
        week_start=week_start(day),
        done_this_week=sum(1 for finished in dates if same_iso_week(finished, day)),
        planned_this_week=days_per_week,
        days_per_week=days_per_week,
        current_streak=streak.current,
        best_streak=streak.best,
        total_sessions=len(dates),
        total_rows_ticked=total_rows_ticked(db, profile_id),
    )
