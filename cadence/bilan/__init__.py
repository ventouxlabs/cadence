"""Assessments, gaps and challenges - principles section 7.

Six tests at baseline and every 28 days; the shortfalls they expose become at most three named
challenges; each challenge puts one extra row into the sessions that suit it.
"""

from __future__ import annotations

from cadence.bilan.assessments import AssessmentError, Result, is_due, latest_batch, next_due_on
from cadence.bilan.gaps import Gap, detect
from cadence.bilan.service import BilanState, save_battery, state_for
from cadence.bilan.tables import Assessment, Challenge

__all__ = [
    "Assessment",
    "AssessmentError",
    "BilanState",
    "Challenge",
    "Gap",
    "Result",
    "detect",
    "is_due",
    "latest_batch",
    "next_due_on",
    "save_battery",
    "state_for",
]
