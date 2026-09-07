"""Gap detection - principles section 7.5, in order.

A gap is a test below the ``ok`` tier for an adult, or below the band's fun target for a youth.
An ``unavailable`` test is never a gap (D-019). Gaps rank by how far short they fell, ties break by
the fixed order of section 7.5.3, and the adult-only ``body_comp`` gap is appended last. Top three.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlmodel import Session

from cadence.bibliotheque.assessments import AssessmentLibrary
from cadence.bilan.assessments import TEST_ORDER, band_target, ok_threshold
from cadence.bilan.tables import UNIT_UNAVAILABLE, Assessment
from cadence.historique.clock import parse_date
from cadence.schema.enums import AgeBand
from cadence.vitalforge.tables import MetricsCache

logger = logging.getLogger(__name__)

BODY_COMP = "body_comp"
#: Section 7.5.4 as the PRP fixes it: a slope inside this band, in points per week, is "flat".
FLAT_SLOPE_PER_WEEK = 0.05
#: Below this many cached points the trend is noise, and no ``body_comp`` gap is emitted.
MIN_TREND_POINTS = 10
TREND_WINDOW_DAYS = 28
#: ``body_comp`` always ranks last (section 7.5.4), whatever its severity would compute to.
BODY_COMP_SEVERITY = -1.0


class ThresholdError(ValueError):
    """An ``ok`` threshold that is zero or negative, which severity cannot divide by."""


@dataclass(frozen=True, slots=True)
class Gap:
    """One shortfall, ranked."""

    test_id: str
    severity: float
    rank: int = 0
    measured: float | None = None
    threshold: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"test_id": self.test_id, "severity": round(self.severity, 4), "rank": self.rank}


def threshold_for(library: AssessmentLibrary, test_id: str, band: AgeBand, *, is_youth: bool) -> float | None:
    """The number this test has to reach: the band's fun target, or the adult ``ok`` tier."""
    value = band_target(library, test_id, band) if is_youth else ok_threshold(library, test_id)
    if value is None:
        return None
    if value <= 0:
        # Risk 10: severity divides by this. Every shipped threshold is positive; a future edit
        # that broke that would otherwise surface as a ZeroDivisionError inside a Done request.
        raise ThresholdError(f"the target for {test_id} must be positive, not {value!r}")
    return float(value)


def _measured(rows: list[Assessment]) -> dict[str, float]:
    """Test id to value, skipping anything recorded ``unavailable`` (section 7.5.2)."""
    return {row.test_id: float(row.value) for row in rows if row.value is not None and row.unit != UNIT_UNAVAILABLE}


def _slope_per_week(points: list[tuple[date, float]]) -> float | None:
    """Least-squares slope in units per week, or ``None`` when the series cannot support one."""
    if len(points) < 2:
        return None
    origin = min(day for day, _ in points)
    xs = [(day - origin).days / 7.0 for day, _ in points]
    ys = [value for _, value in points]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    variance = sum((x - mean_x) ** 2 for x in xs)
    if variance <= 1e-9:
        return None
    return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / variance


def _series(payload: dict[str, Any], *keys: str) -> list[tuple[date, float]]:
    """One ``[[iso_date, value], ...]`` series from the cached payload (D-101), cleaned."""
    for key in keys:
        raw = payload.get(key)
        if not isinstance(raw, list):
            continue
        points: list[tuple[date, float]] = []
        for item in raw:
            if not isinstance(item, list | tuple) or len(item) < 2:
                continue
            day, value = parse_date(item[0]), item[1]
            if day is not None and isinstance(value, int | float) and not isinstance(value, bool):
                points.append((day, float(value)))
        if points:
            return sorted(points)
    return []


def body_comp_gap(db: Session, profile_id: str, today: date, *, is_youth: bool) -> Gap | None:
    """Section 7.5.4: body fat flat-or-rising while muscle percent is flat-or-falling.

    Never for a youth profile (section 3.5): body composition is a banned goal type, so the gap
    that would name it is not computed at all rather than computed and then filtered.
    """
    if is_youth:
        return None
    cached = db.get(MetricsCache, profile_id)
    if cached is None:
        return None
    try:
        payload = json.loads(cached.payload_json)
    except (TypeError, ValueError):
        logger.warning("metrics cache for %s will not parse; no body-composition gap", profile_id)
        return None
    if not isinstance(payload, dict):
        return None

    window_start = today - timedelta(days=TREND_WINDOW_DAYS)
    fat = [point for point in _series(payload, "body_fat_pct") if point[0] >= window_start]
    muscle = [point for point in _series(payload, "muscle_pct", "muscle_percent") if point[0] >= window_start]
    if len(fat) < MIN_TREND_POINTS or len(muscle) < MIN_TREND_POINTS:
        return None
    fat_slope, muscle_slope = _slope_per_week(fat), _slope_per_week(muscle)
    if fat_slope is None or muscle_slope is None:
        return None
    if fat_slope >= -FLAT_SLOPE_PER_WEEK and muscle_slope <= FLAT_SLOPE_PER_WEEK:
        return Gap(test_id=BODY_COMP, severity=BODY_COMP_SEVERITY)
    return None


def detect(
    db: Session,
    profile_id: str,
    rows: list[Assessment],
    library: AssessmentLibrary,
    band: AgeBand,
    *,
    is_youth: bool,
    today: date,
    limit: int = 3,
) -> list[Gap]:
    """The ranked gaps of one battery, at most ``limit`` of them (section 7.5)."""
    measured = _measured(rows)
    found: list[Gap] = []
    for test_id, value in measured.items():
        threshold = threshold_for(library, test_id, band, is_youth=is_youth)
        if threshold is None or value >= threshold:
            continue
        found.append(
            Gap(test_id=test_id, severity=(threshold - value) / threshold, measured=value, threshold=threshold)
        )

    order = {test_id: index for index, test_id in enumerate(TEST_ORDER)}
    found.sort(key=lambda gap: (-gap.severity, order.get(gap.test_id, len(order))))

    body = body_comp_gap(db, profile_id, today, is_youth=is_youth)
    if body is not None:
        found.append(body)
    return [
        Gap(test_id=gap.test_id, severity=gap.severity, rank=index, measured=gap.measured, threshold=gap.threshold)
        for index, gap in enumerate(found[:limit], start=1)
    ]


__all__ = [
    "BODY_COMP",
    "FLAT_SLOPE_PER_WEEK",
    "MIN_TREND_POINTS",
    "Gap",
    "ThresholdError",
    "body_comp_gap",
    "detect",
    "threshold_for",
]
