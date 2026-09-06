"""The per-exercise rows behind one history card, for the tap-to-expand partial.

History is read-only, so this is a query and nothing else. Names come from the planned session's
materialised ``rows_json`` — the same specs Today rendered from — paired with the stored row by
position, exactly as ``seance.today.load_rows`` pairs them.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlmodel import Session

logger = logging.getLogger(__name__)

DASH = "—"

_DETAIL_SQL = """
SELECT s.profile_id AS profile_id,
       COALESCE(p.rows_json, '') AS rows_json,
       r.position   AS position,
       r.exercise_id AS exercise_id,
       r.done       AS done,
       r.reps_done  AS reps_done,
       r.load_done_kg AS load_done_kg
FROM session s
LEFT JOIN planned_session p ON p.id = s.planned_session_id
LEFT JOIN session_row r ON r.session_id = s.id
WHERE s.id = :session_id
ORDER BY r.position
"""


@dataclass(frozen=True, slots=True)
class DetailRow:
    """One exercise as it was finished: what it asked for, and what was ticked."""

    position: int
    name: str
    done: bool
    reps: int | None
    seconds: int | None
    load_kg: float | None
    is_prelude: bool

    @property
    def measure_label(self) -> str:
        """``8 reps``, ``30 s``, or a dash for a row with neither."""
        if self.reps is not None:
            return f"{self.reps} reps"
        if self.seconds is not None:
            return f"{self.seconds} s"
        return DASH


@dataclass(frozen=True, slots=True)
class SessionDetail:
    session_id: str
    profile_id: str
    rows: tuple[DetailRow, ...]


def _spec_index(raw: str, session_id: str) -> dict[int, dict[str, Any]]:
    """The planned specs by position. A plan that will not parse yields no names, not an error.

    An empty string is the ordinary case for a session whose planned row has been deleted, so it
    is not worth a line in the log; anything else that will not parse is a corrupt plan and is.
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        logger.warning("the plan behind session %r will not parse, so its rows lose their names: %s", session_id, exc)
        return {}
    if not isinstance(parsed, list):
        logger.warning("the plan behind session %r is a %s, not a list of rows", session_id, type(parsed).__name__)
        return {}
    return {int(spec["position"]): spec for spec in parsed if isinstance(spec, dict) and "position" in spec}


def _row(mapping: Any, specs: dict[int, dict[str, Any]]) -> DetailRow:
    position = int(mapping["position"])
    spec = specs.get(position, {})
    reps = mapping["reps_done"] if mapping["reps_done"] is not None else spec.get("reps")
    load = mapping["load_done_kg"] if mapping["load_done_kg"] is not None else spec.get("load_kg")
    return DetailRow(
        position=position,
        name=str(spec.get("name") or mapping["exercise_id"]),
        done=bool(mapping["done"]),
        reps=int(reps) if reps is not None else None,
        seconds=int(spec["seconds"]) if spec.get("seconds") is not None else None,
        load_kg=float(load) if load is not None else None,
        is_prelude=bool(spec.get("is_prelude")),
    )


def session_detail(db: Session, session_id: str) -> SessionDetail | None:
    """Every row of one session, or ``None`` when there is no such session."""
    rows = db.execute(text(_DETAIL_SQL), {"session_id": session_id}).all()
    if not rows:
        return None
    first = rows[0]._mapping
    specs = _spec_index(str(first["rows_json"] or ""), session_id)
    return SessionDetail(
        session_id=session_id,
        profile_id=str(first["profile_id"]),
        # A session with no rows at all joins to one null row; it has a position of ``None``.
        rows=tuple(_row(row._mapping, specs) for row in rows if row._mapping["position"] is not None),
    )
