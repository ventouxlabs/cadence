"""What History refuses, and what it must never show.

Three things live here that the happy-path suites cannot reach: the refusals on the JSON
endpoints, the youth guard asserted with PRP-03's own banned-word check rather than with a
hand-written substring, and the degenerate series the sparkline has to survive.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from sqlalchemy import text
from sqlmodel import Session as DbSession

from cadence.config import Settings
from cadence.db import get_engine
from cadence.historique.sparkline import HEIGHT, WIDTH, sparkline
from cadence.historique.trend import BODY_FAT_RANGE, MIN_POINTS, TrendSeries, body_comp_trend
from cadence.profils.tables import PROFILE_ME, PROFILE_SON
from cadence.vitalforge.tables import MetricsCache
from tests.test_historique import ROW_COUNT, _plan, _profiles, _session
from tests.test_history_api import DROP_SYNC_JOB, SYNC_JOB_DDL, _finished
from tests.test_web_today import BARE_WORDS, BODY_IMAGE_PHRASES

# The same scoping PRP-03's own check uses, widened by the classes History adds.
METRIC_ELEMENTS = (
    "row-note",
    "row-notice",
    "session-sub",
    "notice",
    "row-line",
    "summary-line",
    "score-head",
    "group",
    "hcard-day",
    "trend-values",
)


@pytest.fixture
def history_db(db_session: DbSession) -> DbSession:
    _profiles(db_session)
    return db_session


# ------------------------------------------------------------------- 16-17: the refusals


@pytest.mark.parametrize("limit", [0, -1, -100, 201, 10000, 2**31])
async def test_limit_out_of_range_is_never_capped_silently(
    history_db: DbSession, client: httpx.AsyncClient, limit: int
) -> None:
    """Out of range is an error. A silent clamp would answer a question nobody asked."""
    _finished(history_db, PROFILE_ME, 2)
    response = await client.get(f"/api/sessions?profile=me&limit={limit}")

    assert response.status_code == 422, limit
    body = response.json()
    assert body["ok"] is False and body["data"] is None
    assert "limit" in body["error"]


@pytest.mark.parametrize("limit", ["abc", "", "1.5", "1e3", "null"])
async def test_a_limit_that_is_not_a_number_still_answers_in_the_envelope(
    history_db: DbSession, client: httpx.AsyncClient, limit: str
) -> None:
    """FastAPI validates before the route runs, so this is the handler in ``main`` answering.

    It matters because ``app.js`` reads ``ok`` and would treat FastAPI's default ``detail`` body
    as a success with no data.
    """
    response = await client.get(f"/api/sessions?profile=me&limit={limit}")

    assert response.status_code == 422, limit
    body = response.json()
    assert set(body) == {"ok", "data", "error", "meta"}
    assert body["ok"] is False and body["error"]


async def test_the_limit_is_per_profile_on_the_together_tab(history_db: DbSession, client: httpx.AsyncClient) -> None:
    """D-103: ``limit`` is each person's, so halving it would give each an arbitrary share."""
    for profile_id in (PROFILE_ME, PROFILE_SON):
        _finished(history_db, profile_id, 3)

    body = (await client.get("/api/sessions?profile=together&limit=1")).json()
    assert body["meta"]["limit"] == 1
    assert body["meta"]["count"] == 2
    assert sorted(row["profile"] for row in body["data"]) == [PROFILE_ME, PROFILE_SON]


@pytest.mark.parametrize("profile", ["nobody", "../me", "%2e%2e", "together2", "0", "me;son", "'"])
async def test_an_unknown_profile_is_a_404_with_a_reason(
    history_db: DbSession, client: httpx.AsyncClient, profile: str
) -> None:
    for path in (f"/api/sessions?profile={profile}", f"/api/scorecard?profile={profile}"):
        response = await client.get(path)
        assert response.status_code == 404, path
        body = response.json()
        assert body["ok"] is False and body["error"] and body["data"] is None, path

    page = await client.get(f"/history?profile={profile}")
    assert page.status_code == 404


@pytest.mark.parametrize("profile", ["ME", "me ", " Son", "TOGETHER"])
async def test_case_and_whitespace_are_tolerated_but_nothing_else_is(
    history_db: DbSession, client: httpx.AsyncClient, profile: str
) -> None:
    """``normalise_profile_key`` strips and lowercases (PRP-02), and History inherits that.

    Pinned rather than assumed: the refusal test above is only meaningful next to the list of what
    is deliberately accepted.
    """
    response = await client.get(f"/api/scorecard?profile={profile}")
    assert response.status_code == 200, profile
    assert response.json()["data"]["profile"] in {PROFILE_ME, PROFILE_SON}


# ------------------------------------------------- one profile may not expand another's session


async def test_a_card_cannot_be_expanded_from_the_other_profiles_tab(
    history_db: DbSession, client: httpx.AsyncClient
) -> None:
    """The son's tab asking for one of the parent's sessions gets the same 404 an unknown id gets.

    The ids are random, so this is not a hole anyone walks into. It is the guard that keeps the
    expand route from being the one place on this app where a profile boundary is decided by the
    id in the URL rather than by the tab the request came from.
    """
    mine = _finished(history_db, PROFILE_ME, 1)[0]
    his = _finished(history_db, PROFILE_SON, 1)[0]
    hx = {"HX-Request": "true"}

    crossed = await client.get(f"/history/sessions/{mine}?profile=son&expand=1", headers=hx)
    assert crossed.status_code == 404
    assert "Goblet squat" not in crossed.text

    other_way = await client.get(f"/history/sessions/{his}?profile=me&expand=1", headers=hx)
    assert other_way.status_code == 404

    # The owner's own tab, and the shared tab, still open it.
    for profile in ("me", "together"):
        opened = await client.get(f"/history/sessions/{mine}?profile={profile}&expand=1", headers=hx)
        assert opened.status_code == 200, profile
        assert 'data-role="detail"' in opened.text, profile


async def test_the_no_javascript_route_is_guarded_too(history_db: DbSession, client: httpx.AsyncClient) -> None:
    """The redirect branch runs before the partial is rendered, so it needs the same check."""
    mine = _finished(history_db, PROFILE_ME, 1)[0]

    crossed = await client.get(f"/history/sessions/{mine}?profile=son&expand=1", follow_redirects=False)
    assert crossed.status_code == 404

    ok = await client.get(f"/history/sessions/{mine}?profile=me&expand=1", follow_redirects=False)
    assert ok.status_code == 303


@pytest.mark.parametrize("profile", ["nobody", "../me", "0"])
async def test_expanding_a_card_from_a_profile_that_does_not_exist(
    history_db: DbSession, client: httpx.AsyncClient, profile: str
) -> None:
    """A tab that is not a profile cannot open a card either, however real the id is."""
    mine = _finished(history_db, PROFILE_ME, 1)[0]

    response = await client.get(f"/history/sessions/{mine}?profile={profile}&expand=1", headers={"HX-Request": "true"})
    assert response.status_code == 404, profile
    assert "Goblet squat" not in response.text


async def test_two_sync_rows_never_report_the_more_reassuring_one(
    history_db: DbSession, client: httpx.AsyncClient, settings: Settings
) -> None:
    """A session with a retried job must read as its latest attempt, not its friendliest.

    ``MAX(status)`` would pick alphabetically, and "sent" beats "failed" in every alphabet. PRP-04
    risk 9 is exactly this: nothing may claim a sync that did not happen.

    The table is created without the unique constraint the rest of the suite gives it, because a
    second row per session is precisely the shape this guard exists for. PRP-06 owns the real
    table and may or may not keep that constraint; the badge has to be right either way.
    """
    ids = _finished(history_db, PROFILE_ME, 1)
    with get_engine(settings).begin() as connection:
        connection.execute(text(DROP_SYNC_JOB))
        connection.execute(text(SYNC_JOB_DDL.replace(" UNIQUE", "")))
        for job_id, status in (("job-a", "sent"), ("job-b", "failed")):
            connection.execute(
                text("INSERT INTO sync_job (id, session_id, target, status) VALUES (:i, :s, 'vitalforge', :st)"),
                {"i": job_id, "s": ids[0], "st": status},
            )

    body = (await client.get("/api/sessions?profile=me")).json()
    assert body["data"][0]["sync_status"] == "failed"

    page = await client.get("/history?profile=me")
    assert "Sync failed" in page.text and "Synced" not in page.text


async def test_open_on_the_page_shows_only_the_open_card(history_db: DbSession, client: httpx.AsyncClient) -> None:
    """``?open=`` is read once, for the card it names, and never expands the whole list."""
    ids = _finished(history_db, PROFILE_ME, 3)

    page = await client.get(f"/history?profile=me&open={ids[1]}")
    assert page.status_code == 200
    assert page.text.count('data-role="detail"') == 1
    assert page.text.count('aria-expanded="true"') == 1


# ------------------------------------------------------------- 9, 23: D-027 on the son's screens


def _scoped_chunks(html: str) -> list[str]:
    """The text of every metric-ish element, which is where a bare word would be a body metric."""
    import re

    pattern = re.compile(rf'class="(?:{"|".join(METRIC_ELEMENTS)})"[^>]*>([^<]*)<')
    return pattern.findall(html)


async def test_the_sons_history_carries_no_body_words(history_db: DbSession, client: httpx.AsyncClient) -> None:
    """PRP-03's own check (D-027), run server-side against ``/history?profile=son``.

    The end-to-end suite runs the same check in a browser; this one runs before Playwright and
    fails in the suite a change to a template is most likely to be made next to.
    """
    _finished(history_db, PROFILE_SON, 2)
    history_db.add(
        MetricsCache(
            profile_id=PROFILE_SON,
            fetched_at=datetime.now(UTC).isoformat(),
            payload_json=json.dumps({"weight_kg": [["2026-09-01", 40.0], ["2026-09-05", 40.5]]}),
        )
    )
    history_db.commit()

    html = (await client.get("/history?profile=son")).text.lower()
    for phrase in BODY_IMAGE_PHRASES:
        assert phrase not in html, phrase

    chunks = _scoped_chunks(html)
    assert chunks, "no metric elements on the son's History: the check would pass vacuously"
    for chunk in chunks:
        for pattern in BARE_WORDS:
            assert not pattern.search(chunk), f"{pattern.pattern!r} in {chunk!r}"

    assert 'data-role="trend"' not in html and "polyline" not in html


async def test_the_together_tab_carries_no_body_words_either(history_db: DbSession, client: httpx.AsyncClient) -> None:
    """D-105: the son is standing at the same phone, so the shared tab is a youth screen."""
    for profile_id in (PROFILE_ME, PROFILE_SON):
        _finished(history_db, profile_id, 1)
    history_db.add(
        MetricsCache(
            profile_id=PROFILE_ME,
            fetched_at=datetime.now(UTC).isoformat(),
            payload_json=json.dumps(
                {
                    "weight_kg": [["2026-09-01", 85.2], ["2026-09-05", 84.1]],
                    "body_fat_pct": [["2026-09-01", 19.1], ["2026-09-05", 18.2]],
                }
            ),
        )
    )
    history_db.commit()

    html = (await client.get("/history?profile=together")).text.lower()
    for phrase in BODY_IMAGE_PHRASES:
        assert phrase not in html, phrase
    assert "84.1 kg" not in html and 'data-role="trend"' not in html

    # And the parent's own tab, over the same cache, does show it: without this the check above
    # would pass on a page that simply has no data.
    mine = (await client.get("/history?profile=me")).text
    assert 'data-role="trend"' in mine and "84.1 kg" in mine


async def test_the_sons_scorecard_json_carries_no_body_words(history_db: DbSession, client: httpx.AsyncClient) -> None:
    """The JSON is a surface too: PRP-07 and PRP-10 will read it to draw his screens."""
    _finished(history_db, PROFILE_SON, 1)
    body = (await client.get("/api/scorecard?profile=son")).json()

    assert "trend" not in body["data"]
    text = json.dumps(body).lower()
    for phrase in BODY_IMAGE_PHRASES:
        assert phrase not in text, phrase
    for pattern in BARE_WORDS:
        assert not pattern.search(text), pattern.pattern


# --------------------------------------------------------------- the stylesheet stays on its page


async def test_the_history_stylesheet_is_only_loaded_on_history(
    history_db: DbSession, client: httpx.AsyncClient
) -> None:
    """One extra request on every Today render is a page-weight regression nobody would notice."""
    _finished(history_db, PROFILE_ME, 1)
    assert "history.css" in (await client.get("/history?profile=me")).text
    assert "history.css" in (await client.get("/history?profile=son")).text

    for path in ("/today?profile=me", "/today?profile=son", "/today?profile=together"):
        assert "history.css" not in (await client.get(path)).text, path

    assert (await client.get("/static/history.css")).status_code == 200


# ------------------------------------------------------------------- 15: the sync badge


async def test_a_sync_status_nobody_recognises_reads_as_stored_locally(
    history_db: DbSession, client: httpx.AsyncClient, settings: Settings
) -> None:
    """PRP-06 may add a status this build has never heard of. It may not read as "Synced"."""
    ids = _finished(history_db, PROFILE_ME, 1)
    with get_engine(settings).begin() as connection:
        connection.execute(text(DROP_SYNC_JOB))
        connection.execute(text(SYNC_JOB_DDL))
        connection.execute(
            text("INSERT INTO sync_job (id, session_id, target, status) VALUES (:i, :s, 'vitalforge', 'quantum')"),
            {"i": str(uuid.uuid4()), "s": ids[0]},
        )

    body = (await client.get("/api/sessions?profile=me")).json()
    assert body["data"][0]["sync_status"] == "quantum"

    page = await client.get("/history?profile=me")
    assert "Stored locally" in page.text
    assert "Synced" not in page.text


@pytest.mark.parametrize(
    ("status", "label"),
    [("sent", "Synced"), ("pending", "Will sync"), ("failed", "Sync failed"), ("skipped", "Not sent")],
)
async def test_each_sync_status_has_its_own_badge(
    history_db: DbSession, client: httpx.AsyncClient, settings: Settings, status: str, label: str
) -> None:
    ids = _finished(history_db, PROFILE_ME, 1)
    with get_engine(settings).begin() as connection:
        connection.execute(text(DROP_SYNC_JOB))
        connection.execute(text(SYNC_JOB_DDL))
        connection.execute(
            text("INSERT INTO sync_job (id, session_id, target, status) VALUES (:i, :s, 'vitalforge', :st)"),
            {"i": str(uuid.uuid4()), "s": ids[0], "st": status},
        )

    page = await client.get("/history?profile=me")
    assert label in page.text


# --------------------------------------------------------- 12: one point hides the whole card


async def test_a_single_cached_point_hides_the_card(history_db: DbSession, client: httpx.AsyncClient) -> None:
    """Acceptance test 12's second half: the function returns ``None`` *and* the template omits it."""
    _finished(history_db, PROFILE_ME, 1)
    history_db.add(
        MetricsCache(
            profile_id=PROFILE_ME,
            fetched_at=datetime.now(UTC).isoformat(),
            payload_json=json.dumps({"weight_kg": [[date.today().isoformat(), 84.1]], "body_fat_pct": []}),
        )
    )
    history_db.commit()

    page = await client.get("/history?profile=me")
    assert page.status_code == 200
    assert "No data yet." in page.text
    assert 'data-role="trend"' not in page.text and "<polyline" not in page.text

    data = (await client.get("/api/scorecard?profile=me")).json()["data"]
    assert data["trend"]["weight_kg"] == [[date.today().isoformat(), 84.1]]


# ------------------------------------------------------------- 11-13: the series, at every size


def _run(count: int, start: date = date(2026, 8, 1)) -> tuple[tuple[date, float], ...]:
    """``count`` daily points on a gentle downward slope."""
    return tuple((start + timedelta(days=index), 85.0 - index * 0.05) for index in range(count))


@pytest.mark.parametrize("count", [0, 1, 2, 3, 30, 31, 60])
def test_the_sparkline_at_every_length(count: int) -> None:
    """Below two points there is no line; at and above it there is exactly one point each."""
    svg = sparkline(TrendSeries(weight_kg=_run(count), body_fat_pct=()))

    if count < MIN_POINTS:
        assert svg is None
        return
    assert svg is not None and svg.count("<polyline") == 1
    drawn = svg.split('points="')[1].split('"')[0].split(" ")
    assert len(drawn) == count
    xs = [float(pair.split(",")[0]) for pair in drawn]
    ys = [float(pair.split(",")[1]) for pair in drawn]
    assert xs == sorted(xs) and xs[0] == 0.0 and xs[-1] == float(WIDTH)
    assert all(0 <= value <= HEIGHT for value in ys)


def test_thirty_one_points_all_reach_the_svg(history_db: DbSession) -> None:
    """A month of daily readings is the real shape of this card, and none of it is dropped.

    The window is thirty days back from today, inclusive of both ends, so it holds thirty-one
    daily readings. A thirty-second, one day older, is dropped — and asking for a wider window
    brings it back, which is what says the boundary is the window rather than the parser.
    """
    today = date.today()
    payload = {
        "weight_kg": [[(today - timedelta(days=31 - i)).isoformat(), 85.0 - i * 0.05] for i in range(32)],
        "body_fat_pct": [],
    }
    history_db.add(
        MetricsCache(profile_id=PROFILE_ME, fetched_at=datetime.now(UTC).isoformat(), payload_json=json.dumps(payload))
    )
    history_db.commit()

    narrow = body_comp_trend(history_db, PROFILE_ME, today=today)
    assert narrow is not None and len(narrow.weight_kg) == 31
    assert narrow.weight_kg[0][0] == today - timedelta(days=30)
    svg = sparkline(narrow)
    assert svg is not None
    assert len(svg.split('points="')[1].split('"')[0].split(" ")) == 31

    wide = body_comp_trend(history_db, PROFILE_ME, days=60, today=today)
    assert wide is not None and len(wide.weight_kg) == 32


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), None, "84.1", True, [], {}])
def test_a_value_that_is_not_a_reading_is_dropped(history_db: DbSession, value: object) -> None:
    """``NaN`` survives a JSON round trip in Python and would plot as a hole in the line."""
    payload = {
        "weight_kg": [["2026-09-01", value], ["2026-09-02", 84.1], ["2026-09-03", 84.0]],
        "body_fat_pct": [],
    }
    history_db.add(
        MetricsCache(
            profile_id=PROFILE_ME,
            fetched_at=datetime.now(UTC).isoformat(),
            payload_json=json.dumps(payload),
        )
    )
    history_db.commit()

    trend = body_comp_trend(history_db, PROFILE_ME, today=date(2026, 9, 4))
    assert trend is not None
    assert trend.weight_kg == ((date(2026, 9, 2), 84.1), (date(2026, 9, 3), 84.0))

    svg = sparkline(trend)
    assert svg is not None
    assert "nan" not in svg.lower() and "inf" not in svg.lower()


@pytest.mark.parametrize("value", [0.18, 0.0, 1.0, 70.0, 70.1, 100.0, -5.0])
def test_body_fat_outside_the_plausible_range_is_dropped(history_db: DbSession, value: float) -> None:
    """D-102 both ways: a fraction where a percent was promised is as wrong as grams for kilos."""
    low, high = BODY_FAT_RANGE
    payload = {"weight_kg": [], "body_fat_pct": [["2026-09-01", value], ["2026-09-02", 18.2]]}
    history_db.add(
        MetricsCache(
            profile_id=PROFILE_ME,
            fetched_at=datetime.now(UTC).isoformat(),
            payload_json=json.dumps(payload),
        )
    )
    history_db.commit()

    trend = body_comp_trend(history_db, PROFILE_ME, today=date(2026, 9, 3))
    assert trend is not None
    kept = [point for point in trend.body_fat_pct if point[0] == date(2026, 9, 1)]
    assert bool(kept) is (low <= value <= high), value


async def test_a_grams_shaped_weight_never_reaches_the_page(history_db: DbSession, client: httpx.AsyncClient) -> None:
    """D-102 end to end: "85200.0 kg" on the parent's screen is worse than no card at all."""
    _finished(history_db, PROFILE_ME, 1)
    today = date.today()
    history_db.add(
        MetricsCache(
            profile_id=PROFILE_ME,
            fetched_at=datetime.now(UTC).isoformat(),
            payload_json=json.dumps(
                {
                    "weight_kg": [
                        [(today - timedelta(days=7)).isoformat(), 85200],
                        [today.isoformat(), 84100.0],
                    ],
                    "body_fat_pct": [],
                }
            ),
        )
    )
    history_db.commit()

    page = await client.get("/history?profile=me")
    assert page.status_code == 200
    assert "85200" not in page.text and "84100" not in page.text
    assert "No data yet." in page.text

    data = (await client.get("/api/scorecard?profile=me")).json()["data"]
    assert data["trend"]["weight_kg"] == []


def test_a_session_is_still_a_session_without_rows(history_db: DbSession) -> None:
    """A finished session whose rows never landed reads ``0 of 0``, not a crash."""
    from cadence.historique.detail import session_detail
    from cadence.historique.queries import recent_sessions

    planned = _plan(history_db, PROFILE_ME, 1, "done")
    record = _session(history_db, planned, ROW_COUNT)
    history_db.execute(text("DELETE FROM session_row WHERE session_id = :s"), {"s": record.id})
    history_db.commit()

    summary = recent_sessions(history_db, PROFILE_ME)[0]
    assert (summary.rows_done, summary.rows_total) == (0, 0)
    assert summary.counts == "0 of 0" and summary.completion == "partial"

    detail = session_detail(history_db, record.id)
    assert detail is not None and detail.rows == ()
