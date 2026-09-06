"""The "next time" line on the Done screen.

A **placeholder**. PRP-07 owns autoregulation and replaces the body of ``next_time_note`` with the
real decision (principles section 5.4), keeping this signature: the Done screen, the JSON summary
and PRP-04's history all call it and none of them should change when the maths arrives.

Deliberately says nothing numeric. A made-up "goblet squat 3x10 @ 14 kg" would be indistinguishable
from a real prescription, and a person following it would be following an invention.
"""

from __future__ import annotations

from typing import Any

PLACEHOLDER_EASY = "Next time: same session, one more rep on anything that felt easy."
PLACEHOLDER_HARD = "Next time: same session, hold the loads and take the longer rests."
PLACEHOLDER_DEFAULT = "Next time: same session, and we will nudge it once a few are logged."


def next_time_note(session: Any) -> str:
    """One line of advice for the session just finished.

    Reads only ``felt``, which is the one autoregulation input that already exists in PRP-02
    (principles section 5.3). Anything absent gets the neutral line.
    """
    felt = getattr(session, "felt", None)
    if felt == "easy":
        return PLACEHOLDER_EASY
    if felt == "hard":
        return PLACEHOLDER_HARD
    return PLACEHOLDER_DEFAULT
