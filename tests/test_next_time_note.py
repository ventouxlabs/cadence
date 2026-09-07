"""The "next time" line - PRP-07 acceptance test 16.

Split from ``test_autoregulation.py`` to keep both files under the 400-line ceiling; the note is
formatting over the arithmetic that file exercises, so the seam is a real one.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from cadence.programme.notes import (
    HOLD_NOTE,
    best_note,
)

# ---------------------------------------------------------------------- 16: the Done-screen line

# D-027's list, restated rather than imported: ``tests/e2e/test_setup.py`` pulls in playwright, and
# this file has to run without it. Keep the copies in step.
BANNED_PHRASES: tuple[str, ...] = (
    "body fat",
    "bodyfat",
    "body-fat",
    "body composition",
    "muscle %",
    "muscle percent",
    "lean mass",
    "body image",
)
BANNED_BARE_WORDS: tuple[str, ...] = ("weight", "fat", "lean", "abs", "calories")

NOTE_CASES: tuple[tuple[str, dict[str, Any], dict[str, Any], str, str], ...] = (
    (
        "load bump",
        {"name": "Goblet squat", "sets": 3, "reps": 12, "load_kg": 12.75},
        {"name": "Goblet squat", "sets": 3, "reps": 10, "load_kg": 14.0},
        "bump",
        "Next time: goblet squat 3×10 @ 14 kg",
    ),
    (
        "rep bump",
        {"name": "Push-up", "sets": 3, "reps": 10},
        {"name": "Push-up", "sets": 3, "reps": 11},
        "bump",
        "Next time: push-up 3×11",
    ),
    (
        "time bump",
        {"name": "Plank", "sets": 3, "seconds": 40},
        {"name": "Plank", "sets": 3, "seconds": 50},
        "bump",
        "Next time: plank 3×50 s",
    ),
    (
        "regress",
        {"name": "Bench press", "sets": 3, "reps": 8, "load_kg": 14.0},
        {"name": "Bench press", "sets": 3, "reps": 8, "load_kg": 12.75},
        "regress",
        "Next time: easing off a rung on the bench press.",
    ),
    (
        "deload",
        {"name": "Goblet squat", "sets": 3, "reps": 12, "load_kg": 14.0},
        {"name": "Goblet squat", "sets": 2, "reps": 8, "load_kg": 14.0},
        "deload",
        "Next time: deload week — 2 sets, same weight.",
    ),
    (
        "hold",
        {"name": "Goblet squat", "sets": 3, "reps": 10, "load_kg": 14.0},
        {"name": "Goblet squat", "sets": 3, "reps": 10, "load_kg": 14.0},
        "hold",
        HOLD_NOTE,
    ),
)


@pytest.mark.parametrize(
    ("before", "after", "outcome", "expected"),
    [case[1:] for case in NOTE_CASES],
    ids=[case[0] for case in NOTE_CASES],
)
def test_next_time_note_formats(before: dict, after: dict, outcome: str, expected: str) -> None:
    """16. The six shapes the PRP prints, each inside the 80-character budget."""
    line = best_note([(before, after)], outcome)
    assert line == expected
    assert len(line) <= 80

    youth = best_note([(before, after)], outcome, is_youth=True)
    assert len(youth) <= 80
    lowered = youth.lower()
    for phrase in BANNED_PHRASES:
        assert phrase not in lowered, f"{phrase!r} is in a line the son can see"
    for word in BANNED_BARE_WORDS:
        assert not re.search(rf"\b{word}\b", lowered), f"{word!r} is in a line the son can see"
