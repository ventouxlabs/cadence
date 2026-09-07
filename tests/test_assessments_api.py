"""PRP-07 acceptance tests 29-32 and the two guards the task brief names, at the JSON boundary.

The negative tests are the point of this file. A form only offers legal choices, so an illegal one
arrives here or nowhere; and the two positive tests both read the **database** rather than the
response, because "capped on save" and "replaces rather than appends" are claims about what is
stored, which a response echoing its own input cannot support.

The ranking and naming rules these routes relay live in ``tests/test_bilan.py``.
"""

from __future__ import annotations

import httpx
from sqlmodel import Session as DbSession
from sqlmodel import select

from cadence.bilan.tables import Assessment
from cadence.profils.tables import PROFILE_ME, PROFILE_SON, Profile

UNPROCESSABLE = 422
DAY = "2026-09-06"

#: One legal six-test battery for the son, entirely inside his ``u10`` caps.
SON_BATTERY: list[dict[str, object]] = [
    {"test_id": "push_up_max", "value": 6},
    {"test_id": "dead_hang_s", "unavailable": True},
    {"test_id": "plank_s", "value": 25},
    {"test_id": "wall_angel_reach", "value": 2},
    {"test_id": "goblet_squat_quality", "value": 3},
    {"test_id": "farmer_carry_s", "value": 18},
]


def _adult_battery(*, seconds: int, score: int) -> list[dict[str, object]]:
    """All six tests for an adult, with one number for the timed tests and one for the scores."""
    return [
        {"test_id": "push_up_max", "value": seconds},
        {"test_id": "dead_hang_s", "value": seconds},
        {"test_id": "plank_s", "value": seconds},
        {"test_id": "farmer_carry_s", "value": seconds},
        {"test_id": "wall_angel_reach", "value": min(score, 3)},
        {"test_id": "goblet_squat_quality", "value": max(score, 1)},
    ]


def _stored(db: DbSession, profile_id: str) -> list[Assessment]:
    statement = select(Assessment).where(Assessment.profile_id == profile_id)
    return list(db.exec(statement).all())


async def _post(client: httpx.AsyncClient, profile: str, results: list[dict[str, object]]) -> httpx.Response:
    return await client.post("/api/assessments", json={"profile": profile, "recorded_on": DAY, "results": results})


async def test_a_dead_hang_value_is_unavailable_without_an_anchor(
    seeded_client: httpx.AsyncClient, db_session: DbSession
) -> None:
    """D-224. D-019: no overhead anchor, no dead hang - at the JSON boundary as well as the form.

    The HTML route has always coerced this; the JSON route stored whatever it was handed, so a
    posted value became a result, then a gap, then a challenge telling a household with nowhere
    to hang to go and hang for longer. The value is dropped rather than the request refused: the
    other five tests in the battery are perfectly good and should still be recorded.
    """
    me = db_session.get(Profile, PROFILE_ME)
    assert me is not None and not me.has_overhead_anchor, "this test needs an anchorless profile"

    assert (await _post(seeded_client, PROFILE_ME, _adult_battery(seconds=5, score=1))).status_code == 200

    body = (await seeded_client.get("/api/assessments?profile=me")).json()
    by_id = {row["test_id"]: row for row in body["data"]["latest"]}
    assert (by_id["dead_hang_s"]["value"], by_id["dead_hang_s"]["unit"]) == (None, "unavailable")
    assert "dead_hang_s" not in {gap["test_id"] for gap in body["data"]["gaps"]}
    assert "dead_hang_s" not in {item["metric"] for item in body["data"]["challenges"]}
    # The rest of the battery is untouched: five seconds of plank is a real, and poor, result.
    assert by_id["plank_s"]["value"] == 5


# ------------------------------------------------------------------- 29-30: what is refused


async def test_assessment_value_out_of_range(seeded_client: httpx.AsyncClient, db_session: DbSession) -> None:
    """29. **Negative.** Five hundred push-ups is not a measurement, it is a typo."""
    response = await _post(seeded_client, PROFILE_ME, [{"test_id": "push_up_max", "value": 500}])

    assert response.status_code == UNPROCESSABLE
    body = response.json()
    assert body["ok"] is False
    assert "push_up_max" in body["error"]
    assert "200" in body["error"], "the message should name the range it broke"
    assert _stored(db_session, PROFILE_ME) == [], "a refused battery must leave nothing behind"


async def test_unknown_test_id(seeded_client: httpx.AsyncClient, db_session: DbSession) -> None:
    """30. **Negative.** An id outside the six of section 7.1 is refused, and named."""
    response = await _post(seeded_client, PROFILE_ME, [{"test_id": "burpee_max", "value": 12}])

    assert response.status_code == UNPROCESSABLE
    assert "burpee_max" in response.json()["error"]
    assert _stored(db_session, PROFILE_ME) == []


async def test_body_comp_metric_refused_for_youth(seeded_client: httpx.AsyncClient, db_session: DbSession) -> None:
    """**Negative.** Section 3.5: body composition is a banned goal type for every youth band."""
    for metric in ("body_comp", "body_fat_pct", "muscle_pct", "weight_kg"):
        response = await _post(seeded_client, PROFILE_SON, [{"test_id": metric, "value": 20}])
        assert response.status_code == UNPROCESSABLE, metric
        assert metric in response.json()["error"], metric
    assert _stored(db_session, PROFILE_SON) == []

    # Not vacuous: the same route accepts a legal battery for the same profile.
    assert (await _post(seeded_client, PROFILE_SON, SON_BATTERY)).status_code == 200


# ------------------------------------------------------------- 31-32: what is stored, and once


async def test_youth_value_capped_on_save(seeded_client: httpx.AsyncClient, db_session: DbSession) -> None:
    """31. Thirty push-ups from the son are stored as the section 7.4 cap of 20, and he is told."""
    results = [dict(item) for item in SON_BATTERY]
    results[0] = {"test_id": "push_up_max", "value": 30}
    response = await _post(seeded_client, PROFILE_SON, results)

    assert response.status_code == 200
    body = response.json()
    assert body["meta"]["capped"] == ["push_up_max"], "the response has to say the count was capped"

    rows = {row.test_id: row for row in _stored(db_session, PROFILE_SON)}
    assert rows["push_up_max"].value == 20.0, "the cap is the protocol, so the raw 30 never reaches the table"
    assert rows["plank_s"].value == 25.0, "an in-range value is stored untouched"


async def test_reposting_same_date_replaces(seeded_client: httpx.AsyncClient, db_session: DbSession) -> None:
    """32. Two batteries for one day leave six rows, and the second one is the one that stands."""
    assert (await _post(seeded_client, PROFILE_ME, _adult_battery(seconds=40, score=1))).status_code == 200
    assert len(_stored(db_session, PROFILE_ME)) == 6

    assert (await _post(seeded_client, PROFILE_ME, _adult_battery(seconds=50, score=3))).status_code == 200

    rows = _stored(db_session, PROFILE_ME)
    assert len(rows) == 6, "a re-post replaces that date's battery rather than appending a second one"
    assert {row.recorded_on for row in rows} == {DAY}
    stored = {row.test_id: row.value for row in rows}
    assert stored["plank_s"] == 50.0, "the second post has to win, not be silently dropped"
    assert stored["wall_angel_reach"] == 3.0


# ------------------------------------------------------------------------ the documented GET


async def test_get_assessments_envelope(seeded_client: httpx.AsyncClient, db_session: DbSession) -> None:
    """The shape the PRP documents: ``meta`` dates, and ``latest``/``gaps``/``challenges``."""
    empty = (await seeded_client.get("/api/assessments?profile=me")).json()
    assert empty["ok"] is True
    assert empty["meta"]["baseline_on"] is None, "before the baseline there is no date to report"
    assert empty["data"] == {"latest": [], "gaps": [], "challenges": []}

    low = [
        {"test_id": "push_up_max", "value": 6},
        {"test_id": "dead_hang_s", "unavailable": True},
        {"test_id": "plank_s", "value": 90},
        {"test_id": "wall_angel_reach", "value": 3},
        {"test_id": "goblet_squat_quality", "value": 4},
        {"test_id": "farmer_carry_s", "value": 45},
    ]
    assert (await _post(seeded_client, PROFILE_ME, low)).status_code == 200

    body = (await seeded_client.get("/api/assessments?profile=me")).json()
    assert body["ok"] is True
    assert body["error"] is None
    assert body["meta"]["baseline_on"] == DAY
    assert body["meta"]["next_due_on"] == "2026-10-04", "28 days after the newest battery"

    data = body["data"]
    assert set(data) == {"latest", "gaps", "challenges"}
    assert len(data["latest"]) == 6
    by_id = {row["test_id"]: row for row in data["latest"]}
    assert set(by_id["push_up_max"]) == {"test_id", "value", "unit", "recorded_on", "self_rated", "tier"}
    assert by_id["push_up_max"]["tier"] == "low"
    assert (by_id["dead_hang_s"]["value"], by_id["dead_hang_s"]["unit"]) == (None, "unavailable")

    assert [gap["test_id"] for gap in data["gaps"]] == ["push_up_max"]
    assert set(data["gaps"][0]) == {"test_id", "severity", "rank"}
    assert len(data["challenges"]) == 1
    assert set(data["challenges"][0]) == {
        "id",
        "name",
        "metric",
        "target_value",
        "unit",
        "due_on",
        "status",
        "placed",
    }
    assert data["challenges"][0]["name"] == "Push-up max 15 by 4 Oct"
    assert data["challenges"][0]["status"] == "active"
    # D-221: how many planned sessions actually carry the row, not merely that one was derived.
    assert data["challenges"][0]["placed"] == 2


async def test_saving_a_battery_takes_the_assessment_day_off_the_queue(
    seeded_client: httpx.AsyncClient, db_session: DbSession
) -> None:
    """The queue advances on save, not only on Skip.

    Found by review after the first cut shipped: saving hid the Today card (``is_due`` goes false)
    while ``resolve_today`` still returned the assessment session, so Today rendered the six-test
    checklist with no card and nothing to move past it (D-218).
    """
    from cadence.programme.tables import PLANNED, PlannedSession
    from cadence.seance.today import resolve_today

    before = resolve_today(db_session, PROFILE_ME)
    assert before.primary.planned.day_type == "assessment"

    response = await seeded_client.post(
        "/api/assessments",
        json={"profile": PROFILE_ME, "recorded_on": DAY, "results": _adult_battery(seconds=20, score=1)},
    )
    assert response.status_code == 200

    db_session.expire_all()
    remaining = db_session.exec(
        select(PlannedSession).where(
            PlannedSession.profile_id == PROFILE_ME,
            PlannedSession.status == PLANNED,
            PlannedSession.day_type == "assessment",
        )
    ).all()
    assert remaining == []
    assert resolve_today(db_session, PROFILE_ME).primary.planned.day_type != "assessment"


async def test_assess_page_renders_and_a_bad_post_never_500s(seeded_client: httpx.AsyncClient) -> None:
    """The HTML surface answers, and a rejected value comes back as a message on the form."""
    page = await seeded_client.get(f"/assess?profile={PROFILE_ME}")
    assert page.status_code == 200
    assert "Push-up max" in page.text

    son = await seeded_client.get(f"/assess?profile={PROFILE_SON}")
    assert son.status_code == 200
    assert "Hang like a monkey" in son.text
    assert "Push-up max" not in son.text

    rejected = await seeded_client.post(
        "/assess",
        data={"profile": PROFILE_ME, "value_push_up_max": "500"},
        headers={"hx-request": "true"},
    )
    assert rejected.status_code < 500
