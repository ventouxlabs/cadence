"""``GET /api/_mock/activities`` - what the in-process VitalForge fake has recorded.

Mounted **only** when ``CADENCE_VITALFORGE_MODE=mock`` and ``CADENCE_ENV`` is not ``prod``
(``cadence/main.py``). In a real deployment the route does not exist and answers 404, which is
why it carries no auth of its own in an app that has none (D-009).

It exists for one assertion in ``scripts/smoke.sh``: a session that reports "synced" on the Done
screen must correspond to exactly one activity actually handed to VitalForge. The Done line
alone cannot prove that - it reads a ``sync_job`` row, so a write-back that recorded nothing and
a write-back that recorded twice both render the same words.

It returns identifiers only, never the recorded bodies: the caller already knows the session it
just finished, and the payload carries a person's loads and reps.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from cadence.api.envelope import ok
from cadence.vitalforge import mock

router = APIRouter(prefix="/api/_mock", tags=["mock"])


@router.get("/activities")
def activities() -> dict[str, Any]:
    """Every activity POSTed to the fake in this process, oldest first."""
    recorded = [
        {"session_id": str(item["payload"].get("session_id", "")), "slug": item["slug"]} for item in mock.POSTED
    ]
    return ok({"count": len(recorded), "activities": recorded})
