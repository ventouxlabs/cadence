"""PRP-04 acceptance tests 9 and 14-18: the two JSON endpoints and the History page.

The fixtures come from ``tests.test_historique`` rather than a second copy: two builders for the
same three tables drift, and the copy that drifts is the one that stops catching anything.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import Engine, event, text
from sqlmodel import Session as DbSession

from cadence.config import Settings
from cadence.db import get_engine
from cadence.profils.tables import PROFILE_ME, PROFILE_SON
from cadence.vitalforge.tables import MetricsCache
from tests.test_historique import ROW_COUNT, _plan, _profiles, _session

DROP_SYNC_JOB = "DROP TABLE IF EXISTS sync_job"

SYNC_JOB_DDL = """
CREATE TABLE sync_job (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL UNIQUE,
    target TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    last_error TEXT,
    payload_json TEXT,
    remote_ref TEXT
)
"""


def _finished(db: DbSession, profile_id: str, count: int, ticks: int = ROW_COUNT, first_day: int = 1) -> list[str]:
    """``count`` finished sessions a few seconds apart, so they all land in the current ISO week.

    The route asks the real clock, which is the behaviour under test; seconds keep every session
    inside one local day rather than walking backwards over a week boundary.
    """
    now = datetime.now(UTC)
    ids: list[str] = []
    for index in range(count):
        planned = _plan(db, profile_id, first_day + index, "done")
        stamp = (now - timedelta(seconds=index)).isoformat()
        ids.append(_session(db, planned, ticks, finished_at=stamp).id)
    return ids


@pytest.fixture
def history_db(db_session: DbSession) -> DbSession:
    _profiles(db_session)
    return db_session


# ------------------------------------------------------------------------- 9: the youth guard


async def test_scorecard_youth_omits_trend(history_db: DbSession, client: httpx.AsyncClient) -> None:
    """``trend`` is absent from the son's payload, not null. A null key is still a leak waiting."""
    _finished(history_db, PROFILE_SON, 1)
    history_db.add(
        MetricsCache(
            profile_id=PROFILE_SON,
            fetched_at=datetime.now(UTC).isoformat(),
            payload_json=json.dumps({"weight_kg": [["2026-09-01", 40.0], ["2026-09-05", 40.5]]}),
        )
    )
    history_db.commit()

    body = (await client.get("/api/scorecard?profile=son")).json()
    assert body["ok"] is True
    assert "trend" not in body["data"]

    parent = (await client.get("/api/scorecard?profile=me")).json()
    assert "trend" in parent["data"]


async def test_youth_history_page_has_no_trend_line(history_db: DbSession, client: httpx.AsyncClient) -> None:
    _finished(history_db, PROFILE_SON, 1)
    page = await client.get("/history?profile=son")

    assert page.status_code == 200
    assert 'data-role="trend"' not in page.text
    assert "Body composition" not in page.text
    assert "exercises ticked all-time" in page.text


# ------------------------------------------------------ 14-15: degrading without PRP-06's tables


async def test_history_without_metrics_cache(history_db: DbSession, client: httpx.AsyncClient) -> None:
    """The table exists and is empty, which is what PRP-04 ships. The card says so."""
    _finished(history_db, PROFILE_ME, 1)
    page = await client.get("/history?profile=me")

    assert page.status_code == 200
    assert "No data yet." in page.text
    assert 'data-role="trend"' not in page.text


async def test_history_without_the_metrics_cache_table(
    history_db: DbSession, client: httpx.AsyncClient, settings: Settings
) -> None:
    """A database built before PRP-04 has no table at all. Still a page, still not a 500."""
    _finished(history_db, PROFILE_ME, 1)
    with get_engine(settings).begin() as connection:
        connection.execute(text("DROP TABLE metrics_cache"))

    page = await client.get("/history?profile=me")
    assert page.status_code == 200
    assert "No data yet." in page.text


async def test_history_without_sync_job_table(
    history_db: DbSession, client: httpx.AsyncClient, settings: Settings
) -> None:
    """A database built before PRP-06 has no ``sync_job``. Nothing may claim a session was synced."""
    _finished(history_db, PROFILE_ME, 1)
    with get_engine(settings).begin() as connection:
        connection.execute(text(DROP_SYNC_JOB))
    page = await client.get("/history?profile=me")

    assert page.status_code == 200
    assert "Stored locally" in page.text
    assert "Synced" not in page.text

    body = (await client.get("/api/sessions?profile=me")).json()
    assert body["data"][0]["sync_status"] == "local"


async def test_sync_badge_reads_the_table_once_it_exists(
    history_db: DbSession, client: httpx.AsyncClient, settings: Settings
) -> None:
    """The badge is PRP-06's status when there is one, and this profile's own row only."""
    ids = _finished(history_db, PROFILE_ME, 2)
    with get_engine(settings).begin() as connection:
        connection.execute(text(DROP_SYNC_JOB))
        connection.execute(text(SYNC_JOB_DDL))
        connection.execute(
            text("INSERT INTO sync_job (id, session_id, target, status) VALUES (:i, :s, 'vitalforge', 'sent')"),
            {"i": str(uuid.uuid4()), "s": ids[0]},
        )

    body = (await client.get("/api/sessions?profile=me")).json()
    statuses = {row["id"]: row["sync_status"] for row in body["data"]}
    assert statuses[ids[0]] == "sent"
    assert statuses[ids[1]] == "local"

    page = await client.get("/history?profile=me")
    assert "Synced" in page.text and "Stored locally" in page.text


# ------------------------------------------------------------------- 16-17: the refusals


@pytest.mark.parametrize("limit", [0, -1, 201, 500])
async def test_limit_out_of_range(history_db: DbSession, client: httpx.AsyncClient, limit: int) -> None:
    response = await client.get(f"/api/sessions?profile=me&limit={limit}")

    assert response.status_code == 422
    body = response.json()
    assert body["ok"] is False and body["error"]
    assert body["data"] is None


@pytest.mark.parametrize("limit", [1, 30, 200])
async def test_limit_in_range(history_db: DbSession, client: httpx.AsyncClient, limit: int) -> None:
    response = await client.get(f"/api/sessions?profile=me&limit={limit}")
    assert response.status_code == 200
    assert response.json()["meta"]["limit"] == limit


async def test_limit_is_applied(history_db: DbSession, client: httpx.AsyncClient) -> None:
    _finished(history_db, PROFILE_ME, 5)
    body = (await client.get("/api/sessions?profile=me&limit=2")).json()
    assert len(body["data"]) == 2 and body["meta"]["count"] == 2


async def test_unknown_profile(history_db: DbSession, client: httpx.AsyncClient) -> None:
    for path in ("/api/sessions?profile=nobody", "/api/scorecard?profile=nobody"):
        response = await client.get(path)
        assert response.status_code == 404, path
        assert response.json()["error"], path

    page = await client.get("/history?profile=nobody")
    assert page.status_code == 404


# --------------------------------------------------------------------------- 18: no N+1


class _Counter:
    """Counts statements on the app's engine for the duration of one request."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.count = 0

    def _seen(self, *_args: object, **_kwargs: object) -> None:
        self.count += 1

    def __enter__(self) -> _Counter:
        event.listen(self.engine, "before_cursor_execute", self._seen)
        return self

    def __exit__(self, *_exc: object) -> None:
        event.remove(self.engine, "before_cursor_execute", self._seen)


async def test_no_n_plus_one(history_db: DbSession, client: httpx.AsyncClient, settings: Settings) -> None:
    """Thirty sessions cost the same number of statements as one.

    Never a literal number: the ``sync_job`` table check and the profile lookup make an absolute
    count brittle. What matters is that the count does not grow with the list.
    """
    _finished(history_db, PROFILE_ME, 1)
    engine = get_engine(settings)

    with _Counter(engine) as one:
        small = await client.get("/api/sessions?profile=me&limit=200")

    _finished(history_db, PROFILE_ME, 29, first_day=2)
    with _Counter(engine) as many:
        large = await client.get("/api/sessions?profile=me&limit=200")

    assert len(small.json()["data"]) == 1
    assert len(large.json()["data"]) == 30
    assert many.count == one.count, f"{many.count} statements for 30 sessions, {one.count} for 1"


async def test_history_page_is_not_n_plus_one(
    history_db: DbSession, client: httpx.AsyncClient, settings: Settings
) -> None:
    """The scorecard walks the plan in one statement too, and it renders on the same page."""
    _finished(history_db, PROFILE_ME, 1)
    engine = get_engine(settings)

    with _Counter(engine) as one:
        await client.get("/history?profile=me")

    _finished(history_db, PROFILE_ME, 29, first_day=2)
    with _Counter(engine) as many:
        await client.get("/history?profile=me")

    assert many.count == one.count, f"{many.count} statements for 30 sessions, {one.count} for 1"


# --------------------------------------------------------------------- the rest of the surface


async def test_sessions_payload_shape(history_db: DbSession, client: httpx.AsyncClient) -> None:
    _finished(history_db, PROFILE_ME, 1)
    row = (await client.get("/api/sessions?profile=me&limit=10")).json()["data"][0]

    assert set(row) == {
        "id",
        "profile",
        "date",
        "day_type",
        "day_name",
        "rows_done",
        "rows_total",
        "felt",
        "duration_min",
        "completion",
        "together",
        "sync_status",
    }
    assert row["rows_done"] == ROW_COUNT and row["rows_total"] == ROW_COUNT
    assert row["completion"] == "complete" and row["together"] is False


async def test_history_page_renders_the_scorecard_and_the_list(
    history_db: DbSession, client: httpx.AsyncClient
) -> None:
    _finished(history_db, PROFILE_ME, 3)
    page = await client.get("/history?profile=me")

    assert page.status_code == 200
    assert page.text.count('data-role="session"') == 3
    assert "3 of 4 sessions" in page.text
    assert "Streak 3" in page.text and "Best 3" in page.text
    assert 'id="badges-me"' in page.text
    assert "history.css" in page.text


async def test_history_page_is_empty_but_fine(history_db: DbSession, client: httpx.AsyncClient) -> None:
    page = await client.get("/history?profile=me")
    assert page.status_code == 200
    assert "No sessions yet" in page.text
    assert "0 of 4 sessions" in page.text


async def test_together_renders_two_lists_and_no_trend(history_db: DbSession, client: httpx.AsyncClient) -> None:
    """Two independent lists, never a merged one, and no body metric on a screen the son shares."""
    group = str(uuid.uuid4())
    for profile_id in (PROFILE_ME, PROFILE_SON):
        planned = _plan(history_db, profile_id, 1, "done")
        _session(history_db, planned, ROW_COUNT, group=group)

    page = await client.get("/history?profile=together")
    assert page.status_code == 200
    assert page.text.count('data-role="session"') == 2
    assert page.text.count("together") >= 2
    assert 'data-role="trend"' not in page.text
    assert "Body composition" not in page.text
    assert 'id="badges-me"' in page.text and 'id="badges-son"' in page.text


async def test_card_expands_and_collapses(history_db: DbSession, client: httpx.AsyncClient) -> None:
    session_id = _finished(history_db, PROFILE_ME, 1, ticks=2)[0]
    hx = {"HX-Request": "true"}

    opened = await client.get(f"/history/sessions/{session_id}?profile=me&expand=1", headers=hx)
    assert opened.status_code == 200
    assert 'data-role="detail"' in opened.text
    assert opened.text.count("Goblet squat") == ROW_COUNT
    assert 'aria-expanded="true"' in opened.text

    closed = await client.get(f"/history/sessions/{session_id}?profile=me&expand=0", headers=hx)
    assert 'data-role="detail"' not in closed.text
    assert 'aria-expanded="false"' in closed.text


async def test_card_expands_without_javascript(history_db: DbSession, client: httpx.AsyncClient) -> None:
    """The same control is an ordinary link when HTMX is not there to intercept it."""
    session_id = _finished(history_db, PROFILE_ME, 1)[0]

    redirect = await client.get(f"/history/sessions/{session_id}?profile=me&expand=1")
    assert redirect.status_code == 303
    assert redirect.headers["location"].startswith(f"/history?profile=me&open={session_id}")

    page = await client.get(f"/history?profile=me&open={session_id}")
    assert 'data-role="detail"' in page.text


async def test_card_for_an_unknown_session(history_db: DbSession, client: httpx.AsyncClient) -> None:
    response = await client.get("/history/sessions/nope?profile=me", headers={"HX-Request": "true"})
    assert response.status_code == 404


async def test_nav_carries_history_and_keeps_today_the_landing(
    seeded_client: httpx.AsyncClient, seeded: FastAPI
) -> None:
    """PRP-02 left room for the link; Today stays what ``/`` lands on."""
    today = await seeded_client.get("/today?profile=me")
    assert "/history?profile=me" in today.text
    assert 'href="/today?profile=me"' in today.text
    assert "history.css" not in today.text

    history = await seeded_client.get("/history?profile=son")
    assert 'href="/history?profile=son"' in history.text
    assert 'href="/today?profile=son"' in history.text

    root = await seeded_client.get("/", follow_redirects=False)
    assert root.headers.get("location", "").startswith("/today")
