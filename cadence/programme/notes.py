"""The one "next time" line the Done screen shows, built from what the autoregulator just did.

Priority (PRP-07): a load bump, then a rep bump, then a time bump, then a regression, then a
deload, then a hold. One line, at most 80 characters, in the household's display unit.

The line is *formatted here and stored on the session* rather than recomputed on every render.
``next_time_note(session)`` is called again every time the Done screen reloads, and a note rebuilt
from the plan would drift the moment the next session were autoregulated a second time.
"""

from __future__ import annotations

from typing import Any

from cadence.programme.ladder import LB_TO_KG

MAX_NOTE_CHARS = 80
LB_STEP = 0.5

HOLD_NOTE = "Next time: same again — nail the tempo."
DELOAD_NOTE_ADULT = "Next time: deload week — 2 sets, same weight."
# Never the word "weight" on a youth profile (D-027, brief "Goals the program must serve").
DELOAD_NOTE_YOUTH = "Next time: deload week — 2 sets, same load."


def format_kg(value: float, unit: str = "kg") -> str:
    """``14 kg`` or ``31 lb``. Storage is always kilograms; this is the only conversion here."""
    shown = round(value / LB_TO_KG / LB_STEP) * LB_STEP if unit == "lb" else value
    text = f"{shown:.1f}".rstrip("0").rstrip(".")
    return f"{text} {unit}"


def _sets_reps(row: dict[str, Any]) -> str:
    sets = int(row.get("sets") or 1)
    if row.get("reps") is not None:
        return f"{sets}×{int(row['reps'])}"
    if row.get("seconds") is not None:
        return f"{sets}×{int(row['seconds'])} s"
    if row.get("meters") is not None:
        return f"{sets}×{float(row['meters']):g} m"
    return f"{sets} sets"


def _name(row: dict[str, Any]) -> str:
    return str(row.get("name") or row.get("exercise_id") or "the next lift").lower()


def _clip(line: str) -> str:
    """80 characters, never a word cut in half."""
    if len(line) <= MAX_NOTE_CHARS:
        return line
    return line[: MAX_NOTE_CHARS - 1].rsplit(" ", 1)[0] + "."


def _changed(before: dict[str, Any], after: dict[str, Any], key: str) -> bool:
    return before.get(key) != after.get(key)


def describe_change(
    before: dict[str, Any], after: dict[str, Any], outcome: str, unit: str, *, is_youth: bool
) -> str | None:
    """The line for one row, or ``None`` when this row is not worth a sentence."""
    if outcome == "deload":
        return DELOAD_NOTE_YOUTH if is_youth else DELOAD_NOTE_ADULT
    if outcome == "regress":
        return _clip(f"Next time: easing off a rung on the {_name(after)}.")
    if outcome != "bump":
        return None

    load = after.get("load_kg")
    if _changed(before, after, "load_kg") and isinstance(load, int | float):
        return _clip(f"Next time: {_name(after)} {_sets_reps(after)} @ {format_kg(float(load), unit)}")
    if _changed(before, after, "reps"):
        tail = f" @ {format_kg(float(load), unit)}" if isinstance(load, int | float) else ""
        return _clip(f"Next time: {_name(after)} {_sets_reps(after)}{tail}")
    if _changed(before, after, "seconds") or _changed(before, after, "meters"):
        return _clip(f"Next time: {_name(after)} {_sets_reps(after)}")
    if _changed(before, after, "exercise_id"):
        return _clip(f"Next time: {_name(after)} {_sets_reps(after)}")
    return None


# Which kind of change wins when several rows moved at once.
_PRIORITY: tuple[str, ...] = ("load", "reps", "time", "swap", "regress", "deload", "hold")


def _kind(before: dict[str, Any], after: dict[str, Any], outcome: str) -> str:
    if outcome == "deload":
        return "deload"
    if outcome == "regress":
        return "regress"
    if outcome != "bump":
        return "hold"
    if _changed(before, after, "load_kg"):
        return "load"
    if _changed(before, after, "reps"):
        return "reps"
    if _changed(before, after, "seconds") or _changed(before, after, "meters"):
        return "time"
    if _changed(before, after, "exercise_id"):
        return "swap"
    return "hold"


def best_note(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    outcome: str,
    unit: str = "kg",
    *,
    is_youth: bool = False,
) -> str:
    """The single highest-value change across every row that moved, as one line."""
    ranked: list[tuple[int, str]] = []
    for before, after in pairs:
        line = describe_change(before, after, outcome, unit, is_youth=is_youth)
        if line is None:
            continue
        ranked.append((_PRIORITY.index(_kind(before, after, outcome)), line))
    if not ranked:
        return HOLD_NOTE
    ranked.sort(key=lambda item: item[0])
    return ranked[0][1]


__all__ = [
    "DELOAD_NOTE_ADULT",
    "DELOAD_NOTE_YOUTH",
    "HOLD_NOTE",
    "MAX_NOTE_CHARS",
    "best_note",
    "describe_change",
    "format_kg",
]
