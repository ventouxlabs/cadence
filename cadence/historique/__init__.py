"""History: what happened, how this week compares to the plan, and the streak.

``scorecard.current_streak`` is the single definition of a streak in this build; PRP-07's
missed-session logic and PRP-10's badges import it rather than restating the rules.
"""

from __future__ import annotations

from cadence.historique.queries import SessionSummary, recent_sessions
from cadence.historique.scorecard import Scorecard, Streak, current_streak, weekly_scorecard
from cadence.historique.sparkline import sparkline
from cadence.historique.trend import TrendSeries, body_comp_trend

__all__ = [
    "Scorecard",
    "SessionSummary",
    "Streak",
    "TrendSeries",
    "body_comp_trend",
    "current_streak",
    "recent_sessions",
    "sparkline",
    "weekly_scorecard",
]
