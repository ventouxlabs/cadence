"""PRP-06 acceptance tests 9-19: the metrics cache, the unit traps and the readiness nudge.

Test 10 is the one this file exists for. Everyone remembers dividing weight by 1000 and forgets
muscle mass, and 34 500 kg of muscle renders on the screen without raising anything.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
import respx
from sqlmodel import Session as DbSession

from cadence.bilan.gaps import BODY_COMP, MIN_TREND_POINTS, body_comp_gap
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


def _dated(values: list[float], end: date) -> dict:
    """A metric body of dated points, one every two days, the last of them on ``end``."""
    days = [end - timedelta(days=2 * (len(values) - 1 - index)) for index in range(len(values))]
    return {"data": [{"date": day.isoformat(), "value": value} for day, value in zip(days, values, strict=True)]}


def _mock_trending_body_comp(end: date, points: int) -> None:
    """Every adult metric, with body fat rising and muscle mass falling over ``points`` days.

    Bodyweight is held flat so the derived percentage moves only because the mass does: a falling
    mass against a falling weight can yield a *rising* percentage, which would leave the gap
    silent for a reason that has nothing to do with the code under test.
    """
    trending = {
        "weight": [84_000.0] * points,
        "body_fat": [20.0 + 0.2 * index for index in range(points)],
        "muscle_mass": [34_500.0 - 100.0 * index for index in range(points)],
    }
    for name, value in VALUES.items():
        body = _dated(trending[name], end) if name in trending else _series(value)
        respx.get(f"{DASH}/p/jd/api/metrics/{name}").mock(return_value=httpx.Response(200, json=body))
    respx.get(f"{DASH}/p/jd/api/readiness").mock(return_value=httpx.Response(200, json={"score": 72, "status": "ok"}))


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


async def test_the_derived_muscle_pct_lets_the_body_comp_gap_fire(live_db, vf_profiles, live_settings) -> None:
    """D-220. Section 7.5.4's gap could not fire against a real cache until this series existed.

    Driven through ``refresh_metrics`` rather than a hand-written payload on purpose: every other
    body-composition test writes ``muscle_pct`` into the cache itself, so all of them stayed green
    for as long as nothing in the product ever wrote that key. This one fails if the derivation
    stops happening, which is the whole reason it is here rather than in ``tests/test_gaps.py``.
    """
    today = datetime.now(UTC).date()
    _mock_trending_body_comp(today, MIN_TREND_POINTS + 1)

    await _refresh(live_db, vf_profiles["me"], live_settings)

    payload = json.loads(live_db.get(MetricsCache, "me").payload_json)
    derived = payload["muscle_pct"]
    assert len(derived) == MIN_TREND_POINTS + 1
    assert derived[0][1] == 41.07, "muscle mass over bodyweight, as a percentage"
    assert derived[-1][1] < derived[0][1], "the percentage has to fall, or the gap is right to stay silent"

    gap = body_comp_gap(live_db, "me", today, is_youth=False)
    assert gap is not None and gap.test_id == BODY_COMP


async def test_a_day_missing_one_reading_is_skipped_not_interpolated(live_db, vf_profiles, live_settings) -> None:
    """A percentage needs both numbers from the same morning, so an unpaired day yields no point.

    Both directions, and by **date** rather than by count: a length that happens to come out right
    while the pairing is off by a day would pass a count check and still feed the least-squares
    slope a point taken from two different mornings (D-220).
    """
    today = datetime.now(UTC).date()
    _mock_trending_body_comp(today, MIN_TREND_POINTS + 1)
    # One extra weight reading, on a day the scale reported no muscle mass at all.
    respx.get(f"{DASH}/p/jd/api/metrics/weight").mock(
        return_value=httpx.Response(200, json=_dated([84_000.0] * (MIN_TREND_POINTS + 2), today))
    )

    await _refresh(live_db, vf_profiles["me"], live_settings)

    payload = json.loads(live_db.get(MetricsCache, "me").payload_json)
    assert len(payload["weight_kg"]) == MIN_TREND_POINTS + 2
    assert len(payload["muscle_pct"]) == MIN_TREND_POINTS + 1

    lonely = payload["weight_kg"][0][0]
    derived = {day for day, _ in payload["muscle_pct"]}
    assert lonely not in derived, "a day with a weight and no muscle mass was given a percentage anyway"
    assert derived == {day for day, _ in payload["weight_kg"][1:]}


async def test_a_muscle_reading_without_a_weight_is_skipped_too(live_db, vf_profiles, live_settings) -> None:
    """The other direction. Dividing by a bodyweight nobody measured is the worse of the two.

    ``_muscle_pct`` keys off the weight series, so this is the case that would fail loudly rather
    than silently - but only if something asks. Nothing did until now.
    """
    today = datetime.now(UTC).date()
    _mock_trending_body_comp(today, MIN_TREND_POINTS + 1)
    # One extra muscle-mass reading, on a day the scale reported no bodyweight at all.
    respx.get(f"{DASH}/p/jd/api/metrics/muscle_mass").mock(
        return_value=httpx.Response(
            200,
            json=_dated([34_500.0 - 100.0 * index for index in range(MIN_TREND_POINTS + 2)], today),
        )
    )

    await _refresh(live_db, vf_profiles["me"], live_settings)

    payload = json.loads(live_db.get(MetricsCache, "me").payload_json)
    assert len(payload["weight_kg"]) == MIN_TREND_POINTS + 1
    assert len(payload["muscle_pct"]) == MIN_TREND_POINTS + 1, "an unpaired muscle reading was kept"
    assert {day for day, _ in payload["muscle_pct"]} == {day for day, _ in payload["weight_kg"]}
    assert all(0.0 < value < 100.0 for _, value in payload["muscle_pct"]), "a percentage that is not one"


async def test_the_mass_series_is_never_cached(live_db, vf_profiles, live_settings) -> None:
    """D-220: the percentage is stored and the mass is not. ``latest`` still answers "how much"."""
    today = datetime.now(UTC).date()
    _mock_trending_body_comp(today, MIN_TREND_POINTS + 1)

    await _refresh(live_db, vf_profiles["me"], live_settings)

    payload = json.loads(live_db.get(MetricsCache, "me").payload_json)
    assert "muscle_mass_kg" not in payload, "a series nothing reads is a series that can go stale unnoticed"
    assert payload["latest"]["muscle_mass_kg"] > 0


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
    # The stale copy carries the series forward wholesale, the derived one included (D-220).
    assert metrics.series["muscle_pct"] == [["2026-09-05", 41.02], ["2026-09-06", 41.02]]


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
