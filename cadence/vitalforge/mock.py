"""The in-process fake behind ``CADENCE_VITALFORGE_MODE=mock``.

No socket is ever opened in this mode: the client short-circuits here and returns canned
responses in the exact shape ``docs/vitalforge-contract.md`` section 2 and section 4.4 describe.
PRP-09's deploy smoke test runs the whole app this way, which is why mock mode does **not**
require a token - there is no service to authenticate against, and demanding one would make the
smoke test assert "stored locally" forever.

Posted payloads are recorded so an in-process test can assert the exact body Cadence built.
The recorder is a module-level list on purpose: the fake stands in for a remote service, and a
test asking "what did we send it" is asking about that service, not about a client instance.
"""

from __future__ import annotations

from hashlib import sha256
from typing import Any

# Values chosen to exercise the unit conversions: grams in, kilograms out.
WEIGHT_GRAMS = 84100
MUSCLE_MASS_GRAMS = 34500
BODY_FAT_PCT = 18.2
RESTING_HR = 52
SLEEP_SCORE = 76
BODY_BATTERY = 61
READINESS_SCORE = 72

_SERIES_DAYS = ("2026-09-04", "2026-09-05", "2026-09-06")

_LATEST: dict[str, float] = {
    "weight": float(WEIGHT_GRAMS),
    "muscle_mass": float(MUSCLE_MASS_GRAMS),
    "body_fat": BODY_FAT_PCT,
    "resting_hr": float(RESTING_HR),
    "sleep_score": float(SLEEP_SCORE),
    "body_battery": float(BODY_BATTERY),
}

# Every payload Cadence has POSTed to the fake in this process, oldest first.
POSTED: list[dict[str, Any]] = []


def reset() -> None:
    """Forget every recorded payload. Test support, and the one way to clear the recorder."""
    POSTED.clear()


def record(slug: str, payload: dict[str, Any]) -> None:
    POSTED.append({"slug": slug, "payload": payload})


def metric(name: str) -> dict[str, Any]:
    """``GET /p/{slug}/api/metrics/{name}`` - three points, oldest first, like the real one."""
    latest = _LATEST.get(name, 1.0)
    # A gently descending series, so a trend line drawn from it is not a flat bar.
    values = [round(latest * factor, 2) for factor in (1.012, 1.006, 1.0)]
    data = [
        {"date": day, "value": value, "moving_avg_7d": value} for day, value in zip(_SERIES_DAYS, values, strict=True)
    ]
    return {"metric": name, "days": len(data), "count": len(data), "data": data}


def readiness() -> dict[str, Any]:
    return {
        "score": READINESS_SCORE,
        "components": {"hrv": 68, "rhr": 74, "sleep_score": 76},
        "status": "ok",
    }


def recent_weight() -> list[dict[str, Any]]:
    """No body composition here, exactly like the real endpoint (contract section 2.1)."""
    return [
        {
            "id": 42,
            "weight_lbs": 185.4,
            "weight_kg": 84.1,
            "timestamp": "2026-09-06T07:31:00+00:00",
            "synced_to_garmin": True,
        }
    ]


def remote_id(session_id: str) -> str:
    """A stable stand-in for VitalForge's row id, derived from the session it belongs to.

    Derived rather than counted: ``len(POSTED)`` made the id depend on how many payloads this
    process had already recorded, so the same session got a different ``remote_ref`` depending on
    test order, and two different sessions could share one. A hash of the session id is stable
    across runs, unique per session, and obviously not a real VitalForge id.
    """
    return f"mock-{sha256(session_id.encode()).hexdigest()[:12]}"


def activity(session_id: str) -> dict[str, Any]:
    """A fresh-insert 202 body, mirroring ``post_weight``'s shape (contract section 4.4)."""
    return {
        "success": True,
        "id": remote_id(session_id),
        "session_id": session_id,
        "garmin_status": "synced",
        "garmin_activity_id": "mock-activity",
        "garmin_sets_status": "skipped",
    }
