"""PRP-06 acceptance tests 46-47 and 49: the two read routes that must never touch the network."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import respx

from cadence.vitalforge.metrics import NUDGE_PUSH
from cadence.vitalforge.tables import MetricsCache


def _cache(db, profile_id: str, payload: dict) -> None:
    db.add(
        MetricsCache(profile_id=profile_id, fetched_at=datetime.now(UTC).isoformat(), payload_json=json.dumps(payload))
    )
    db.commit()


async def test_get_metrics_makes_no_network_call(live_db, vf_profiles, live_client) -> None:
    """Test 46. A refresh hidden here because the cache looked empty in dev is how a screen ends
    up waiting five seconds on a health service (risk 12)."""
    _cache(live_db, "me", {"latest": {"weight_kg": 84.1, "body_fat": 18.2}, "readiness": {"score": 72, "status": "ok"}})

    body = (await live_client.get("/api/metrics?profile=me")).json()

    assert respx.calls.call_count == 0
    assert body["ok"] is True
    assert body["data"]["weight_kg"] == 84.1
    assert body["data"]["nudge"] == NUDGE_PUSH
    assert body["meta"]["source"] == "cache"


async def test_get_metrics_unknown_profile_404(live_client) -> None:
    """Test 47, first half. The envelope, never FastAPI's ``{"detail": ...}``."""
    response = await live_client.get("/api/metrics?profile=nobody")

    assert response.status_code == 404
    assert response.json()["ok"] is False
    assert response.json()["error"]


async def test_cache_miss_returns_stale_true(live_db, vf_profiles, live_client) -> None:
    """Test 47, second half. Every metric absent, and ``stale`` says why the screen is empty."""
    body = (await live_client.get("/api/metrics?profile=me")).json()

    assert body["ok"] is True
    assert body["data"]["stale"] is True
    assert "weight_kg" not in body["data"]
    assert body["data"]["nudge"] == "Readiness not available"


async def test_the_youth_route_never_returns_body_composition(live_db, vf_profiles, live_client) -> None:
    """Architecture section 5, enforced past the template: absent, not null."""
    _cache(live_db, "son", {"latest": {"weight_kg": 40.0}, "readiness": {"score": None, "status": "insufficient_data"}})

    data = (await live_client.get("/api/metrics?profile=son")).json()["data"]

    assert "weight_kg" not in data
    assert "body_fat" not in data
    assert data["readiness"]["score"] is None


async def test_health_makes_no_vitalforge_call(live_client) -> None:
    """Test 49. Compose polls this every 30 s; a 5 s probe turns an outage into a restart loop."""
    body = (await live_client.get("/api/health")).json()

    assert respx.calls.call_count == 0
    assert body["data"]["vitalforge"] == {"configured": True, "mode": "live"}
    # Codex B: "is this deployment real" is one question with two halves.
    assert body["data"]["env"] == "test"


async def test_health_never_shows_the_token(live_client) -> None:
    """The health route reports ``configured``, and that is the whole of what it may say."""
    from tests.conftest import VF_TOKEN

    assert VF_TOKEN not in (await live_client.get("/api/health")).text
