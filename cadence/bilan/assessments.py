"""Recording and reading the six-test battery (principles section 7.1).

Two rules carry the weight here. A youth measurement is **capped on save**, not at render time:
the cap is the protocol, and storing 30 push-ups for a nine-year-old would leak the uncapped
number into every trend that reads the table afterwards (PRP-07 risk 11). And a test that could
not be run is stored as ``unavailable``, never as zero (D-019).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlmodel import Session, select

from cadence.bibliotheque.assessments import AssessmentLibrary
from cadence.bilan.tables import (
    RETEST_DAYS,
    UNIT_UNAVAILABLE,
    Assessment,
)
from cadence.profils.tables import Profile
from cadence.schema.enums import AgeBand, AssessmentId

#: Plausible ranges, by unit. Outside them the POST is a 422 rather than a stored absurdity.
RANGES: dict[str, tuple[float, float]] = {"reps": (0, 200), "s": (0, 600)}
SCORE_RANGES: dict[str, tuple[int, int]] = {"wall_angel_reach": (0, 3), "goblet_squat_quality": (1, 5)}

#: Fixed tie-break order for gap ranking (section 7.5.3), also the order the form renders in.
TEST_ORDER: tuple[str, ...] = (
    "dead_hang_s",
    "push_up_max",
    "plank_s",
    "wall_angel_reach",
    "goblet_squat_quality",
    "farmer_carry_s",
)
#: The order of principles section 10.10's assessment-day battery, grip tests separated.
FORM_ORDER: tuple[str, ...] = (
    "wall_angel_reach",
    "goblet_squat_quality",
    "push_up_max",
    "dead_hang_s",
    "plank_s",
    "farmer_carry_s",
)


class AssessmentError(ValueError):
    """A result that cannot be stored, with a message a person can act on."""


@dataclass(frozen=True, slots=True)
class Result:
    """One validated test result, ready to store."""

    test_id: str
    value: float | None
    unit: str
    self_rated: bool
    capped: bool = False


def unit_for(library: AssessmentLibrary, test_id: str) -> str:
    spec = library.spec(AssessmentId(test_id))
    return spec.unit if spec is not None else "reps"


def youth_cap(library: AssessmentLibrary, test_id: str) -> int | None:
    target = library.youth_targets.get(AssessmentId(test_id))
    return target.cap if target is not None else None


def parse_result(library: AssessmentLibrary, raw: Any, *, is_youth: bool) -> Result:
    """One item of the POST body, validated against section 7 and the plausible ranges."""
    if not isinstance(raw, dict):
        raise AssessmentError("each result must be an object with a test_id")
    test_id = str(raw.get("test_id") or "")
    if test_id not in {item.value for item in AssessmentId}:
        raise AssessmentError(f"{test_id!r} is not one of the six tests")
    spec = library.spec(AssessmentId(test_id))
    if spec is None:  # pragma: no cover - the library validator guarantees all six
        raise AssessmentError(f"{test_id!r} has no protocol in the library")

    if raw.get("unavailable"):
        return Result(test_id=test_id, value=None, unit=UNIT_UNAVAILABLE, self_rated=spec.self_rated)

    value = raw.get("value")
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise AssessmentError(f"{test_id} needs a number, or unavailable: true")

    if spec.self_rated:
        low, high = SCORE_RANGES.get(test_id, (0, 5))
        if float(value) != int(value):
            raise AssessmentError(f"{test_id} is a whole-number score between {low} and {high}")
        if not low <= int(value) <= high:
            raise AssessmentError(f"{test_id} must be between {low} and {high}")
    else:
        low_f, high_f = RANGES.get(spec.unit, (0, 600))
        if not low_f <= float(value) <= high_f:
            raise AssessmentError(f"{test_id} must be between {low_f:g} and {high_f:g} {spec.unit}")

    stored = float(value)
    capped = False
    cap = youth_cap(library, test_id) if is_youth else None
    if cap is not None and stored > cap:
        stored, capped = float(cap), True
    return Result(test_id=test_id, value=stored, unit=spec.unit, self_rated=spec.self_rated, capped=capped)


def record_results(db: Session, profile: Profile, results: list[Result], recorded_on: date) -> list[Assessment]:
    """Store one battery, replacing any already recorded on that date for that profile."""
    day = recorded_on.isoformat()
    existing = db.exec(
        select(Assessment).where(Assessment.profile_id == profile.id, Assessment.recorded_on == day)
    ).all()
    replacing = {row.test_id: row for row in existing}
    stored: list[Assessment] = []
    for result in results:
        row = replacing.pop(result.test_id, None)
        if row is None:
            row = Assessment(
                id=str(uuid.uuid4()),
                profile_id=profile.id,
                test_id=result.test_id,
                recorded_on=day,
            )
        row.value = result.value
        row.unit = result.unit
        row.self_rated = result.self_rated
        db.add(row)
        stored.append(row)
    for orphan in replacing.values():
        db.delete(orphan)
    # Flushed, not committed: ``save_battery`` owns the transaction (D-223).
    db.flush()
    return stored


def all_for(db: Session, profile_id: str) -> list[Assessment]:
    """Every stored result for this profile, oldest date first."""
    statement = (
        select(Assessment)
        .where(Assessment.profile_id == profile_id)
        .order_by(Assessment.recorded_on, Assessment.test_id)  # type: ignore[arg-type]
    )
    return list(db.exec(statement).all())


def latest_batch(db: Session, profile_id: str) -> list[Assessment]:
    """The most recent battery, or an empty list before the baseline."""
    rows = all_for(db, profile_id)
    if not rows:
        return []
    newest = max(row.recorded_on for row in rows)
    return [row for row in rows if row.recorded_on == newest]


def baseline_on(db: Session, profile_id: str) -> date | None:
    rows = all_for(db, profile_id)
    return date.fromisoformat(min(row.recorded_on for row in rows)) if rows else None


def latest_on(db: Session, profile_id: str) -> date | None:
    rows = all_for(db, profile_id)
    return date.fromisoformat(max(row.recorded_on for row in rows)) if rows else None


def next_due_on(db: Session, profile_id: str) -> date | None:
    """Twenty-eight days after the newest battery. ``None`` before the baseline."""
    latest = latest_on(db, profile_id)
    return latest + timedelta(days=RETEST_DAYS) if latest is not None else None


def is_due(db: Session, profile_id: str, today: date) -> bool:
    """Whether the Assessment-day card shows: no baseline yet, or the newest is 28 days old."""
    due = next_due_on(db, profile_id)
    return due is None or today >= due


def best_value(db: Session, profile_id: str, test_id: str) -> float | None:
    """The best measurement ever recorded for a test, used to size its challenge row."""
    values = [row.value for row in all_for(db, profile_id) if row.test_id == test_id and row.value is not None]
    return max(values) if values else None


def band_target(library: AssessmentLibrary, test_id: str, band: AgeBand) -> int | None:
    target = library.youth_targets.get(AssessmentId(test_id))
    return target.for_band(band) if target is not None else None


def ok_threshold(library: AssessmentLibrary, test_id: str) -> float | None:
    tier = library.adult_tiers.get(AssessmentId(test_id))
    return tier.ok_min if tier is not None else None


def tier_of(library: AssessmentLibrary, test_id: str, value: float | None) -> str | None:
    """``low``/``ok``/``strong`` for an adult reading. ``None`` for an unavailable test."""
    tier = library.adult_tiers.get(AssessmentId(test_id))
    if tier is None or value is None:
        return None
    if value < tier.ok_min:
        return "low"
    return "ok" if value <= tier.ok_max else "strong"


__all__ = [
    "FORM_ORDER",
    "RANGES",
    "SCORE_RANGES",
    "TEST_ORDER",
    "AssessmentError",
    "Result",
    "all_for",
    "band_target",
    "baseline_on",
    "best_value",
    "is_due",
    "latest_batch",
    "latest_on",
    "next_due_on",
    "ok_threshold",
    "parse_result",
    "record_results",
    "tier_of",
    "unit_for",
    "youth_cap",
]
