"""The Jinja environment and the small amount of formatting the templates need.

Autoescape is on for every extension, not just the ones Jinja guesses: an exercise name or a cue
arriving from ``/api/import`` (PRP-08) is user input, and the checklist is where it lands.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.templating import Jinja2Templates

from cadence.programme.ladder import LB_TO_KG

# Re-exported: the day names are vocabulary two packages share, so they live beside the enum
# they name rather than in the module that owns the Jinja environment.
from cadence.schema.labels import DAY_TYPE_LABELS as DAY_TYPE_LABELS
from cadence.schema.labels import day_label as day_label
from cadence.seance.catalog import icon_id

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"

PROFILE_TABS: tuple[tuple[str, str], ...] = (("me", "Me"), ("son", "Son"), ("together", "Both"))


def is_youth(profile: Any) -> bool:
    """The youth guard, as one callable every template shares.

    A *function* and not the page-level ``youth`` flag the PRP sketches, because Together renders
    two profiles in one document: a single boolean would either hide the parent's numbers or show
    the son's, and PRP-02 already carries the right granularity on each column (D-094).
    """
    return bool(profile is not None and getattr(profile, "kind", None) == "youth")


# Pounds are shown to the nearest half pound. Finer than that is noise on a dumbbell rack, and
# coarser loses the 2.5 lb step the adjustable set actually has.
LB_STEP = 0.5


def format_load(value: float | None, unit: str = "kg") -> str:
    """``14 kg``, ``2.5 kg``, ``31 lb``, or nothing at all for a bodyweight row.

    The **only** place a stored kilogram becomes a displayed pound (PRP-03 risk 5). The value on
    the row is always kg; ``unit`` chooses how it is written, and no service ever sees the result.
    Registered as the ``load`` Jinja filter as well, so there is one conversion and not two: an
    earlier version labelled the number without converting it, which rendered 14 kg as "14 lb".
    """
    if value is None:
        return ""
    shown = round(value / LB_TO_KG / LB_STEP) * LB_STEP if unit == "lb" else value
    text = f"{shown:.1f}".rstrip("0").rstrip(".")
    return f"{text} {unit}"


def prescription(row: Any, unit: str = "kg") -> str:
    """The one-line "3 x 8 - 14 kg" under an exercise name.

    Reads the ``*_done`` values where the user has adjusted them, so the line always says what the
    row now claims rather than what it was planned as.
    """
    spec = row.spec
    sets = spec.get("sets") or 1
    parts: list[str] = []
    if row.reps is not None:
        per_side = " per side" if spec.get("per_side") else ""
        parts.append(f"{sets} x {row.reps}{per_side} reps" if sets > 1 else f"{row.reps}{per_side} reps")
    elif spec.get("seconds") is not None:
        parts.append(f"{sets} x {spec['seconds']} s" if sets > 1 else f"{spec['seconds']} s")
    elif spec.get("meters") is not None:
        metres = f"{float(spec['meters']):g}"
        parts.append(f"{sets} x {metres} m" if sets > 1 else f"{metres} m")
    elif spec.get("steps") is not None:
        parts.append(f"{sets} x {spec['steps']} steps" if sets > 1 else f"{spec['steps']} steps")
    load = format_load(row.load_kg, unit)
    if load:
        parts.append(load)
    return "  ·  ".join(parts)


templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
templates.env.autoescape = True
templates.env.trim_blocks = True
templates.env.lstrip_blocks = True
templates.env.globals["day_label"] = day_label
templates.env.globals["prescription"] = prescription
templates.env.globals["format_load"] = format_load
templates.env.filters["load"] = format_load
templates.env.globals["is_youth"] = is_youth
templates.env.globals["icon_id"] = icon_id
templates.env.globals["PROFILE_TABS"] = PROFILE_TABS
