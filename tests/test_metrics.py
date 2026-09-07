"""PRP-06 acceptance tests 9-19: the metrics cache, the unit traps and the readiness nudge.

Test 10 is the one this file exists for. Everyone remembers dividing weight by 1000 and forgets
muscle mass, and 34 500 kg of muscle renders on the screen without raising anything.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from sqlmodel import Session as DbSession

from cadence.config import Settings
from cadence.profils.tables import Profile
from cadence.vitalforge.client import VitalForgeClient
from cadence.vitalforge.metrics import (
    ADULT_METRICS,
    NUDGE_EASY,
    NUDGE_NONE,
    NUDGE_PUSH,
    NUDGE_STEADY,
    nudge_for,
    read_cached,
    refresh_metrics,
)
from cadence.vitalforge.tables import MetricsCache

DASH = "http://dash.test"

VALUES: dict[str, float] = {
    "weight": 84100,
    "body_fat": 18.2,
    "muscle_mass": 34500,
    "resting_hr": 52,
    "sleep_score": 76,
    "body_battery": 61,
}


def _series(value: float) -> dict:
    return {"data": [{"date": "2026-09-05", "value": value}, {"date": "2026-09-06", "value": value}]}


def _mock_all(readiness: dict | None = None, failing: str | None = None) -> None:
    """Every adult metric plus readiness, with one optional 500."""
    for name, value in VALUES.items():
        response = httpx.Response(500, text="down") if name == failing else httpx.Response(200, json=_series(value))
        respx.get(f"{DASH}/p/jd/api/metrics/{name}").mock(return_value=response)
    body = readiness if readiness is not None else {"score": 72, "status": "ok"}
    respx.get(f"{DASH}/p/jd/api/readiness").mock(return_value=httpx.Response(200, json=body))


async def _refresh(db: DbSession, profile: Profile, settings: Settings):
    return await refresh_metrics(db, profile, VitalForgeClient(settings))


async def test_weight_divided_by_1000(live_db, vf_profiles, live_settings) -> None:
    """Test 9. ``metrics/weight`` reads ``weight_history.weight_grams`` (contract 2.2)."""
    _mock_all()
    metrics = await _refresh(live_db, vf_profiles["me"], live_settings)
    assert metrics.latest["weight_kg"] == 84.1


async def test_muscle_mass_divided_by_1000(live_db, vf_profiles, live_settings) -> None:
    """Test 10. The trap most likely to ship wrong: ``muscle_mass_g``, same as weight."""
    _mock_all()
    metrics = await _refresh(live_db, vf_profiles["me"], live_settings)
    assert metrics.latest["muscle_mass_kg"] == 34.5


async def test_body_fat_not_converted(live_db, vf_profiles, live_settings) -> None:
    """Test 11. Already a percentage. Dividing it would render 0.018 % and look plausible."""
    _mock_all()
    metrics = await _refresh(live_db, vf_profiles["me"], live_settings)
    assert metrics.latest["body_fat"] == 18.2
    assert metrics.latest["resting_hr"] == 52


async def test_the_cached_series_keep_d101s_shape(live_db, vf_profiles, live_settings) -> None:
    """PRP-04's trend reader parses ``weight_kg``/``body_fat_pct`` as ``[[date, value]]`` at the
    top level of the payload (D-101). The scalars live under ``latest`` (D-120)."""
    _mock_all()
    await _refresh(live_db, vf_profiles["me"], live_settings)

    payload = json.loads(live_db.get(MetricsCache, "me").payload_json)
    assert payload["weight_kg"] == [["2026-09-05", 84.1], ["2026-09-06", 84.1]]
    assert payload["body_fat_pct"][-1] == ["2026-09-06", 18.2]
    assert payload["latest"]["muscle_mass_kg"] == 34.5


async def test_partial_failure_keeps_other_metrics(live_db, vf_profiles, live_settings) -> None:
    """Test 12. One metric 500ing must not blank the other five."""
    _mock_all(failing="body_fat")

    metrics = await _refresh(live_db, vf_profiles["me"], live_settings)

    assert metrics.stale is False
    assert metrics.latest["weight_kg"] == 84.1
    assert "body_fat" not in metrics.latest


async def test_total_failure_marks_stale_and_keeps_last_payload(live_db, vf_profiles, live_settings) -> None:
    """Test 13. VitalForge rebooting must not empty a screen that had good numbers."""
    _mock_all()
    await _refresh(live_db, vf_profiles["me"], live_settings)

    respx.reset()
    for name in ADULT_METRICS:
        respx.get(f"{DASH}/p/jd/api/metrics/{name}").mock(side_effect=httpx.ConnectError("down"))
    respx.get(f"{DASH}/p/jd/api/readiness").mock(side_effect=httpx.ConnectError("down"))

    metrics = await _refresh(live_db, vf_profiles["me"], live_settings)

    assert metrics.stale is True
    assert metrics.latest["weight_kg"] == 84.1
    assert metrics.readiness.score == 72


async def test_cache_older_than_six_hours_is_stale(live_db, vf_profiles) -> None:
    """Test 14. Evaluated on read: six hours pass without anyone writing a row."""
    old = datetime.now(UTC) - timedelta(hours=6, minutes=1)
    live_db.add(
        MetricsCache(profile_id="me", fetched_at=old.isoformat(), payload_json=json.dumps({"latest": {}}), stale=False)
    )
    live_db.commit()

    assert read_cached(live_db, vf_profiles["me"]).stale is True


async def test_youth_refresh_calls_only_readiness(live_db, vf_profiles, live_settings) -> None:
    """Test 15. Not "asks and hides it": the son's body composition is never fetched."""
    respx.get(f"{DASH}/p/kid/api/readiness").mock(
        return_value=httpx.Response(200, json={"score": None, "status": "insufficient_data"})
    )

    await _refresh(live_db, vf_profiles["son"], live_settings)

    assert respx.calls.call_count == 1
    assert respx.calls.last.request.url.path == "/p/kid/api/readiness"


async def test_youth_payload_omits_body_comp_keys(live_db, vf_profiles, live_settings) -> None:
    """Test 16. Absent, not null, so a template rendering the dict blind cannot leak them."""
    respx.get(f"{DASH}/p/kid/api/readiness").mock(
        return_value=httpx.Response(200, json={"score": None, "status": "insufficient_data"})
    )
    await _refresh(live_db, vf_profiles["son"], live_settings)

    data = read_cached(live_db, vf_profiles["son"]).as_data()

    assert "weight_kg" not in data
    assert "body_fat" not in data
    assert "muscle_mass_kg" not in data
    assert data["nudge"] == NUDGE_NONE


async def test_a_youth_cache_written_with_body_comp_still_hides_it(live_db, vf_profiles) -> None:
    """Belt and braces: even a payload somebody else wrote is filtered on read."""
    live_db.add(
        MetricsCache(
            profile_id="son",
            fetched_at=datetime.now(UTC).isoformat(),
            payload_json=json.dumps({"latest": {"weight_kg": 40.0}, "weight_kg": [["2026-09-06", 40.0]]}),
        )
    )
    live_db.commit()

    data = read_cached(live_db, vf_profiles["son"]).as_data()

    assert "weight_kg" not in data


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (70, NUDGE_PUSH),
        (85, NUDGE_PUSH),
        (69, NUDGE_STEADY),
        (50, NUDGE_STEADY),
        (49, NUDGE_EASY),
        (0, NUDGE_EASY),
        (None, NUDGE_NONE),
    ],
)
def test_nudge_thresholds(score: float | None, expected: str) -> None:
    """Test 17. Boundaries included: 70 pushes, 69 is steady, 50 is steady, 49 is easy."""
    assert nudge_for(score) == expected


async def test_null_readiness_renders_not_available(live_db, vf_profiles, live_settings) -> None:
    """Test 18. The son's permanent answer - one Garmin credential, one person."""
    _mock_all(readiness={"score": None, "components": {"hrv": None}, "status": "insufficient_data"})

    metrics = await _refresh(live_db, vf_profiles["me"], live_settings)

    assert metrics.readiness.score is None
    assert metrics.readiness.status == "insufficient_data"
    assert metrics.readiness.nudge == NUDGE_NONE


async def test_readiness_500_is_not_coerced_to_zero(live_db, vf_profiles, live_settings) -> None:
    """Test 19. A zero would render "Easy day - hold loads" forever and look like advice."""
    for name, value in VALUES.items():
        respx.get(f"{DASH}/p/jd/api/metrics/{name}").mock(return_value=httpx.Response(200, json=_series(value)))
    respx.get(f"{DASH}/p/jd/api/readiness").mock(return_value=httpx.Response(500, text="scoring failed"))

    metrics = await _refresh(live_db, vf_profiles["me"], live_settings)

    assert metrics.readiness.score is None
    assert metrics.as_data()["nudge"] == NUDGE_NONE
    assert metrics.as_data()["readiness"]["score"] is None


async def test_a_blank_slug_leaves_the_cache_alone_and_never_calls(live_db, live_settings) -> None:
    """A profile nobody has given a slug to is a configuration gap, not a crash."""
    profile = Profile(id="me", display_name="Me", kind="adult", vitalforge_person="")
    live_db.add(profile)
    live_db.commit()

    metrics = await _refresh(live_db, profile, live_settings)

    assert respx.calls.call_count == 0
    assert metrics.stale is True
    assert metrics.latest == {}


# --------------------------------------------------- Codex F: a 200 is not automatically data


async def test_a_malformed_200_does_not_overwrite_a_good_cache(live_db, vf_profiles, live_settings) -> None:
    """A proxy error page with status 200 looked exactly like "this person has no readings".

    The refresh counted as a success, replaced good numbers with nothing, and marked the result
    fresh — so the History card went blank and said it was up to date.
    """
    _mock_all()
    await _refresh(live_db, vf_profiles["me"], live_settings)

    respx.reset()
    for name in ADULT_METRICS:
        respx.get(f"{DASH}/p/jd/api/metrics/{name}").mock(return_value=httpx.Response(200, html="<h1>Bad Gateway</h1>"))
    respx.get(f"{DASH}/p/jd/api/readiness").mock(return_value=httpx.Response(200, html="<h1>Bad Gateway</h1>"))

    metrics = await _refresh(live_db, vf_profiles["me"], live_settings)

    assert metrics.stale is True
    assert metrics.latest["weight_kg"] == 84.1, "a good cache was replaced by an error page"
    assert metrics.readiness.score == 72


async def test_a_200_with_the_wrong_envelope_is_not_data(live_db, vf_profiles, live_settings) -> None:
    """``{"data": {...}}`` instead of ``{"data": [...]}`` - a rewritten API, not an empty one."""
    for name in ADULT_METRICS:
        respx.get(f"{DASH}/p/jd/api/metrics/{name}").mock(return_value=httpx.Response(200, json={"data": {}}))
    respx.get(f"{DASH}/p/jd/api/readiness").mock(return_value=httpx.Response(200, json=[]))

    metrics = await _refresh(live_db, vf_profiles["me"], live_settings)

    assert metrics.stale is True
    assert metrics.latest == {}


async def test_a_genuinely_empty_series_is_still_a_successful_read(live_db, vf_profiles, live_settings) -> None:
    """A new person with no weigh-ins yet is not a failure, and must not read as stale forever."""
    for name in ADULT_METRICS:
        respx.get(f"{DASH}/p/jd/api/metrics/{name}").mock(
            return_value=httpx.Response(200, json={"metric": name, "days": 30, "count": 0, "data": []})
        )
    respx.get(f"{DASH}/p/jd/api/readiness").mock(
        return_value=httpx.Response(200, json={"score": None, "status": "insufficient_data"})
    )

    metrics = await _refresh(live_db, vf_profiles["me"], live_settings)

    assert metrics.stale is False
    assert metrics.latest == {}
    assert metrics.readiness.nudge == NUDGE_NONE


async def test_a_readiness_body_with_neither_key_is_not_a_reading(live_db, vf_profiles, live_settings) -> None:
    """``{}`` is not ``{"score": null}``: one is an answer, the other is a broken endpoint."""
    respx.get(f"{DASH}/p/kid/api/readiness").mock(return_value=httpx.Response(200, json={}))

    metrics = await _refresh(live_db, vf_profiles["son"], live_settings)

    assert metrics.stale is True
