"""The autoregulation inputs that have to be read from the database (principles section 5.3).

Readiness, missed sessions and the fourteen-day comeback. Kept apart from the decision table so
``decide`` stays a pure function of its inputs and these stay testable against a database without
a workout in sight.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlmodel import Session, select

from cadence.historique.clock import parse_utc
from cadence.programme.decision import Readiness
from cadence.seance.tables import SessionRecord
from cadence.vitalforge.tables import MetricsCache

logger = logging.getLogger(__name__)

WINDOW_DAYS = 7
RESTART_AFTER_DAYS = 14
RESTART_LOAD_FACTOR = 0.90
RESTART_NOTE = "Welcome back. Starting the block again, a little lighter."

_READINESS_VALUES: frozenset[str] = frozenset({"low", "ok", "high"})


def readiness_for(db: Session, profile_id: str) -> Readiness:
    """The cached VitalForge readiness status, or ``"unknown"``.

    Absent, unparseable and unrecognised all answer ``"unknown"`` and never ``"low"``. PRP-06 owns
    filling this cache; until it does, every household has an empty table, and reading that as a
    low readiness would hold every session forever (PRP-07 risk 6). D-101 fixes the payload shape,
    to which PRP-06 adds ``readiness: {"score": float | null, "status": str}``.
    """
    cached = db.get(MetricsCache, profile_id)
    if cached is None:
        return "unknown"
    try:
        payload = json.loads(cached.payload_json)
    except (TypeError, ValueError):
        logger.warning("metrics cache for %s will not parse; readiness is unknown", profile_id)
        return "unknown"
    if not isinstance(payload, dict):
        return "unknown"
    block = payload.get("readiness")
    if not isinstance(block, dict):
        return _from_status(block)
    return _from_score(block.get("score")) or _from_status(block.get("status"))


def _from_score(raw: Any) -> Readiness | None:
    """PRP-06's cached readiness score, banded on PRP-06's own nudge thresholds.

    The score is the real signal: ``status`` is whatever VitalForge chose to call it and is
    ``insufficient_data`` on the son's profile for ever. The cut points are imported rather than
    restated, so the line Today shows ("Good day to push") and the rule that bumps a load can
    never disagree about what a number means (D-219).
    """
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    from cadence.vitalforge.metrics import PUSH_AT, STEADY_AT

    if raw >= PUSH_AT:
        return "high"
    return "ok" if raw >= STEADY_AT else "low"


def _from_status(raw: Any) -> Readiness:
    """A status string, but only one the decision table has a rule for. Otherwise unknown."""
    if isinstance(raw, str) and raw.strip().lower() in _READINESS_VALUES:
        return raw.strip().lower()  # type: ignore[return-value]
    return "unknown"


def finished_at_values(db: Session, profile_id: str) -> list[datetime]:
    """Every finished session for this profile, oldest first. Unparseable stamps are dropped."""
    statement = select(SessionRecord.finished_at).where(
        SessionRecord.profile_id == profile_id,
        SessionRecord.finished_at.is_not(None),  # type: ignore[union-attr]
    )
    stamps = [parse_utc(value) for value in db.exec(statement).all()]
    return sorted(stamp for stamp in stamps if stamp is not None)


def missed_sessions_7d(db: Session, profile_id: str, days_per_week: int, now: datetime | None = None) -> int:
    """Scheduled slots in the trailing seven days with no completed session (section 5.7).

    The window starts at the profile's **first** completed session, never before it. A household
    that has trained once has not missed the three slots that notionally preceded it, and counting
    them would fire R3 on the second session of a brand-new program (PRP-07 risk 12's sibling).
    """
    if days_per_week < 1:
        return 0
    finished = finished_at_values(db, profile_id)
    if not finished:
        return 0
    reference = now or datetime.now(UTC)
    window_start = max(reference - timedelta(days=WINDOW_DAYS), finished[0])
    span_days = max((reference - window_start).total_seconds() / 86400.0, 0.0)
    slots = math.floor(days_per_week * span_days / WINDOW_DAYS + 1e-9)
    completed = sum(1 for stamp in finished if window_start <= stamp <= reference)
    return max(0, slots - completed)


def days_since_previous(db: Session, profile_id: str, before: datetime) -> int | None:
    """Whole days between ``before`` and the completed session that preceded it.

    ``None`` when nothing preceded it, which is what keeps the comeback rule off a fresh install
    (PRP-07 risk 12): a first ever session has no gap to measure.
    """
    earlier = [stamp for stamp in finished_at_values(db, profile_id) if stamp < before]
    if not earlier:
        return None
    return max(0, (before - earlier[-1]).days)


def needs_block_restart(db: Session, profile_id: str, finished: datetime) -> bool:
    """Whether more than fourteen days passed with zero completed sessions (section 5.7)."""
    gap = days_since_previous(db, profile_id, finished)
    return gap is not None and gap > RESTART_AFTER_DAYS


def lighten_row(row: dict[str, Any], ladder: list[float], factor: float = RESTART_LOAD_FACTOR) -> dict[str, Any]:
    """One row's load multiplied and re-rounded down to the ladder, then clamped (section 8.4)."""
    from cadence.programme.ladder import round_to_available

    load = row.get("load_kg")
    if not isinstance(load, int | float) or not load:
        return dict(row)
    progression = row.get("progression")
    cap = progression.get("cap_load_kg") if isinstance(progression, dict) else None
    cap_kg = float(cap) if isinstance(cap, int | float) else None
    landed = round_to_available(float(load) * factor, ladder, cap_kg)
    return dict(row) if landed is None else {**row, "load_kg": landed}


__all__ = [
    "RESTART_AFTER_DAYS",
    "RESTART_LOAD_FACTOR",
    "RESTART_NOTE",
    "WINDOW_DAYS",
    "days_since_previous",
    "finished_at_values",
    "lighten_row",
    "missed_sessions_7d",
    "needs_block_restart",
    "readiness_for",
]
