"""The "next time" line on the Done screen.

PRP-07's real line, kept behind PRP-02's signature (D-071). The autoregulator formats one line
when it rewrites the next session and stores it on ``session.notes``; this reads it back.

Reading rather than recomputing is deliberate. The Done screen is re-summarised on every reload,
and a note rebuilt from the plan would say something different once that plan had been nudged a
second time. The three placeholder lines survive as the fallback for a session the autoregulator
never reached - an assessment day, a library that would not load, a queue with nothing after it.
"""

from __future__ import annotations

from typing import Any

PLACEHOLDER_EASY = "Next time: same session, one more rep on anything that felt easy."
PLACEHOLDER_HARD = "Next time: same session, hold the loads and take the longer rests."
PLACEHOLDER_DEFAULT = "Next time: same session, and we will nudge it once a few are logged."


def next_time_note(session: Any) -> str:
    """One line of advice for the session just finished.

    The line the autoregulator stored, or a neutral one keyed on ``felt`` when it stored nothing.
    """
    stored = getattr(session, "notes", None)
    if isinstance(stored, str) and stored.strip():
        return stored.strip()
    felt = getattr(session, "felt", None)
    if felt == "easy":
        return PLACEHOLDER_EASY
    if felt == "hard":
        return PLACEHOLDER_HARD
    return PLACEHOLDER_DEFAULT
