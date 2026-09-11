"""Turning gaps into named challenges - principles section 7.6, and their lifecycle.

At most three are active per profile. A challenge closes ``met`` when a later assessment reaches
its target and ``expired`` at its due date, and an expired one is re-derived from the next
battery.

Youth names are the fixed play phrases of section 7.4 plus a count or a duration, and nothing
else. No weight, no body, no appearance, ever (section 3.5, D-027): the phrase comes from the
library and the only number that may join it is one the child can count or time.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, timedelta
from typing import Any

from sqlmodel import Session, select

from cadence.bibliotheque.assessments import AssessmentLibrary
from cadence.bilan.assessments import band_target, ok_threshold, unit_for
from cadence.bilan.gaps import BODY_COMP, Gap
from cadence.bilan.tables import (
    ACTIVE,
    EXPIRED,
    MAX_ACTIVE_CHALLENGES,
    MET,
    RETEST_DAYS,
    UNIT_SECONDS,
    Assessment,
    Challenge,
)
from cadence.schema.enums import AgeBand, AssessmentId

logger = logging.getLogger(__name__)

NAME_DATE_FORMAT = "%-d %b"
BODY_COMP_NAME = "One more carry set on every full-body day"


def _slug(text: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", text.lower())).strip("-")


def challenge_id(test_id: str, target: float, unit: str) -> str:
    """Kebab-case and stable: ``dead-hang-60s``, ``push-up-max-15``."""
    suffix = "s" if unit == UNIT_SECONDS else ""
    return _slug(f"{test_id.replace('_', '-')}-{target:g}{suffix}")


def adult_name(library: AssessmentLibrary, test_id: str, target: float, due: date) -> str:
    """``Dead hang 60 s by 15 Oct`` (section 7.6)."""
    spec = library.spec(AssessmentId(test_id))
    display = spec.name if spec is not None else test_id
    unit = f" {UNIT_SECONDS}" if unit_for(library, test_id) == UNIT_SECONDS else ""
    return f"{display} {target:g}{unit} by {due.strftime(NAME_DATE_FORMAT)}"


def youth_name(library: AssessmentLibrary, test_id: str, target: float) -> str:
    """The section 7.4 play phrase, with a count or a duration and no other number."""
    entry = library.youth_targets.get(AssessmentId(test_id))
    phrase = entry.phrasing if entry is not None else test_id.replace("_", " ")
    spec = library.spec(AssessmentId(test_id))
    if spec is not None and spec.self_rated:
        # A self-rated score is neither a count nor a duration, so it gets no number at all.
        return phrase
    if unit_for(library, test_id) == UNIT_SECONDS:
        return f"{phrase} for {int(target)} seconds"
    return f"{phrase} {int(target)} times"


def target_for(library: AssessmentLibrary, test_id: str, band: AgeBand, *, is_youth: bool) -> float | None:
    value = band_target(library, test_id, band) if is_youth else ok_threshold(library, test_id)
    return float(value) if value is not None else None


def active_for(db: Session, profile_id: str) -> list[Challenge]:
    statement = (
        select(Challenge)
        .where(Challenge.profile_id == profile_id, Challenge.status == ACTIVE)
        .order_by(Challenge.due_on, Challenge.id)  # type: ignore[arg-type]
    )
    return list(db.exec(statement).all())


def all_for(db: Session, profile_id: str) -> list[Challenge]:
    statement = (
        select(Challenge).where(Challenge.profile_id == profile_id).order_by(Challenge.due_on, Challenge.id)  # type: ignore[arg-type]
    )
    return list(db.exec(statement).all())


def close_met(db: Session, profile_id: str, results: list[Assessment]) -> list[Challenge]:
    """Mark every active challenge a retest has reached (section 7.7).

    ``met_on`` is the ``recorded_on`` of the measurement that reached the target, not today
    (D-232). A battery backdated to the day it was actually performed closes its challenges on
    that day too, which is the same reading ``recorded_on`` already gets everywhere else - and
    the alternative, stamping the day the row happened to be written, is the guess D-232 was
    holding out for a real answer rather than accept.
    """
    reached = {row.test_id: (float(row.value), row.recorded_on) for row in results if row.value is not None}
    closed: list[Challenge] = []
    for challenge in active_for(db, profile_id):
        measurement = reached.get(challenge.test_id)
        if measurement is not None and measurement[0] >= challenge.target_value:
            challenge.status = MET
            challenge.met_on = measurement[1]
            db.add(challenge)
            closed.append(challenge)
    return closed


def expire_due(db: Session, profile_id: str, today: date) -> list[Challenge]:
    """Mark every active challenge whose due date has passed (section 7.6)."""
    expired: list[Challenge] = []
    for challenge in active_for(db, profile_id):
        if date.fromisoformat(challenge.due_on) <= today:
            challenge.status = EXPIRED
            db.add(challenge)
            expired.append(challenge)
    return expired


def build(
    library: AssessmentLibrary,
    profile_id: str,
    gap: Gap,
    band: AgeBand,
    baseline: date,
    *,
    is_youth: bool,
    row: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> Challenge | None:
    """One challenge from one gap, named for the profile's own vocabulary."""
    due = baseline + timedelta(days=RETEST_DAYS)
    if gap.test_id == BODY_COMP:
        if is_youth:  # pragma: no cover - gaps.detect never emits body_comp for a youth profile
            return None
        return Challenge(
            id=f"{profile_id}-body-comp-{due.isoformat()}",
            profile_id=profile_id,
            name=BODY_COMP_NAME,
            test_id=BODY_COMP,
            target_value=1.0,
            unit="set",
            baseline_on=baseline.isoformat(),
            due_on=due.isoformat(),
            status=ACTIVE,
            row_json=json.dumps(extra or {}, sort_keys=True) if extra else None,
        )

    target = target_for(library, gap.test_id, band, is_youth=is_youth)
    if target is None:
        return None
    name = youth_name(library, gap.test_id, target) if is_youth else adult_name(library, gap.test_id, target, due)
    payload = None
    if row is not None:
        payload = json.dumps({**row, **(extra or {})}, sort_keys=True, separators=(",", ":"))
    return Challenge(
        id=f"{profile_id}-{challenge_id(gap.test_id, target, unit_for(library, gap.test_id))}",
        profile_id=profile_id,
        name=name,
        test_id=gap.test_id,
        target_value=target,
        unit=unit_for(library, gap.test_id),
        baseline_on=baseline.isoformat(),
        due_on=due.isoformat(),
        status=ACTIVE,
        row_json=payload,
    )


def store(db: Session, profile_id: str, candidates: list[Challenge]) -> list[Challenge]:
    """Persist up to three active challenges, keeping any already open (section 7.5.5)."""
    open_now = {row.id: row for row in active_for(db, profile_id)}
    kept: list[Challenge] = list(open_now.values())
    for candidate in candidates:
        if len(kept) >= MAX_ACTIVE_CHALLENGES:
            logger.info("challenge %r dropped: %s already has three active", candidate.id, profile_id)
            break
        existing = db.get(Challenge, candidate.id)
        if existing is not None:
            if existing.status == ACTIVE:
                continue
            # A challenge that expired and is earned again by the next battery reopens rather
            # than minting a second row under the same natural key.
            existing.status = ACTIVE
            existing.name = candidate.name
            existing.baseline_on = candidate.baseline_on
            existing.due_on = candidate.due_on
            existing.row_json = candidate.row_json
            db.add(existing)
            kept.append(existing)
            continue
        db.add(candidate)
        kept.append(candidate)
    # Flushed, not committed: ``save_battery`` owns the transaction so a battery and the
    # challenges derived from it land together or not at all (D-223).
    db.flush()
    return active_for(db, profile_id)


def as_dict(challenge: Challenge) -> dict[str, Any]:
    return {
        "id": challenge.id,
        "name": challenge.name,
        "metric": challenge.test_id,
        "target_value": challenge.target_value,
        "unit": challenge.unit,
        "due_on": challenge.due_on,
        "status": challenge.status,
        # How many planned sessions actually carry this challenge's row. ``None`` where the
        # challenge inserts no row of its own or has never been woven (D-221).
        "placed": _placed(challenge),
    }


def _placed(challenge: Challenge) -> int | None:
    if not challenge.row_json:
        return None
    try:
        row = json.loads(challenge.row_json)
    except (TypeError, ValueError):
        return None
    placed = row.get("placed") if isinstance(row, dict) else None
    return placed if isinstance(placed, int) and not isinstance(placed, bool) else None


__all__ = [
    "BODY_COMP_NAME",
    "active_for",
    "adult_name",
    "all_for",
    "as_dict",
    "build",
    "challenge_id",
    "close_met",
    "expire_due",
    "store",
    "target_for",
    "youth_name",
]
