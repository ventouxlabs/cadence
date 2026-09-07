"""The browser half of PRP-06's finish criterion: what the Done screen actually says.

The exact ``/api/activity`` body is asserted in ``tests/test_session_to_activity.py`` - respx
patches ``httpx`` in this process and the end-to-end server is a subprocess, so the wire
assertion cannot live here. What can live here is the thing a person sees.

The server runs with ``CADENCE_VITALFORGE_MODE=mock`` (``tests/e2e/conftest.py``), so the inline
write-back succeeds against the in-process fake and no socket is opened.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from playwright.sync_api import Page, expect


def _row(page: Page, profile: str, position: int):
    return page.locator(f"#row-{profile}-{position}")


def test_the_done_screen_reports_a_successful_write_back(page: Page, fresh_session) -> None:
    """One session, one Done, one line: "synced ✓" and no Retry button."""
    data = fresh_session("me")
    page.goto("/today?profile=me")
    for row in data["rows"]:
        _row(page, "me", row["position"]).locator(".tick").check()
        page.wait_for_timeout(60)
    page.locator("button.done").click()
    page.wait_for_url("**/done/**")

    line = page.locator("#sync-status")
    expect(line).to_have_text("synced ✓")
    expect(line).to_have_attribute("data-sync", "sent")
    expect(page.locator(".sync-retry")).to_have_count(0)


def test_the_sync_line_survives_a_reload(page: Page, api: httpx.Client, fresh_session) -> None:
    """Static HTML, read from the job: no spinner, no polling, same answer on the way back."""
    data = fresh_session("me")
    session_id = data["session_id"]
    api.post(f"/api/sessions/{session_id}/rows/1", json={"done": True})
    api.post(f"/api/sessions/{session_id}/done", json={"felt": "right"})

    page.goto(f"/done/{session_id}?profile=me")

    expect(page.locator("#sync-status")).to_have_text("synced ✓")


def test_the_retry_endpoint_answers_the_envelope(api: httpx.Client) -> None:
    """``POST /api/sync/retry`` is the JSON half of the same drain the button runs."""
    body = api.post("/api/sync/retry", json={}).json()

    assert body["ok"] is True
    assert set(body["data"]) == {"drained", "sent", "failed", "skipped"}


def test_today_never_calls_vitalforge_on_the_request_path(page: Page, fresh_session) -> None:
    """Architecture section 5. The nudge comes from the cache; Today opens a connection to
    nothing but its own origin."""
    external: list[str] = []
    page.on("request", lambda request: external.append(request.url) if "127.0.0.1" not in request.url else None)

    fresh_session("me")
    page.goto("/today?profile=me")
    page.wait_for_load_state("networkidle")

    assert external == []


# --------------------------------------------------------------- the other two deployments
#
# The sync line reports the state of a *deployment*, and the three states a person can actually
# land in are decided by environment variables read at startup: mocked (above), live against a
# VitalForge that is not answering, and live with no token pasted in yet. Each needs its own
# server, which is what ``failing_url`` and ``tokenless_url`` in the conftest are.


def _finish_a_session(url: str, profile: str = "me") -> str:
    """One full session through the real routes on the server at ``url``."""
    with httpx.Client(base_url=url, timeout=15.0) as api:
        data = api.get(f"/api/today?profile={profile}").json()["data"]
        session_id = data["session_id"]
        for row in data["rows"]:
            api.post(f"/api/sessions/{session_id}/rows/{row['position']}", json={"done": True})
        assert api.post(f"/api/sessions/{session_id}/done", json={"felt": "right"}).status_code == 200
    return session_id


def test_an_unreachable_vitalforge_reads_as_retrying_and_offers_no_button(page: Page, failing_url: str) -> None:
    """PRP-06 section 4.3, the ``failed`` + scheduled row.

    No Retry button here, and that is the spec rather than an omission: the job already has a
    ``next_attempt_at``, the drain will pick it up, and a button inviting a person to hammer a
    dead service is the tight loop D-016 exists to prevent. The button belongs to the terminal
    state, which the next test drives.
    """
    session_id = _finish_a_session(failing_url)

    page.goto(f"{failing_url}/done/{session_id}?profile=me")

    line = page.locator("#sync-status")
    expect(line).to_contain_text("sync failed (retrying)")
    expect(line).to_have_attribute("data-sync", "failed")
    expect(page.locator(".sync-retry")).to_have_count(0)


def _exhaust(db_path: Path, session_id: str) -> None:
    """Put one job in the terminal shape PRP-06 section 3 defines: failed, and no schedule.

    Written straight onto the row rather than driven there by eight real retries. The route that
    would drive it is the same one the button uses, so a throttle or an attempt accounting change
    on that path would silently stop this test from reaching the state it is named after - it
    would pass by testing the retrying line instead. ``next_attempt_at IS NULL`` is the whole
    definition of terminal, and it is stable whatever the drainer does to get there.
    """
    db = sqlite3.connect(db_path, timeout=10)
    try:
        columns = {row[1] for row in db.execute("PRAGMA table_info(sync_job)")}
        db.execute(
            "UPDATE sync_job SET status = 'failed', attempts = 8, next_attempt_at = NULL WHERE session_id = ?",
            (session_id,),
        )
        if "last_attempt_at" in columns:
            # Old enough that any cooldown on the manual retry has passed.
            db.execute(
                "UPDATE sync_job SET last_attempt_at = ? WHERE session_id = ?",
                ((datetime.now(UTC) - timedelta(days=1)).isoformat(), session_id),
            )
        assert db.total_changes, f"no sync_job row for {session_id}"
        db.commit()
    finally:
        db.close()


def test_the_retry_button_appears_on_a_terminal_job_and_posts_back(
    page: Page, failing_url: str, failing_db: Path
) -> None:
    """The terminal state: the line, the button, and the button reaching the server.

    VitalForge is still unreachable when the button is clicked, so the line comes back terminal.
    What is asserted is that the form posted to a route that answered with the line again, rather
    than 404ing or leaving a dead form on the page.
    """
    session_id = _finish_a_session(failing_url)
    _exhaust(failing_db, session_id)

    page.goto(f"{failing_url}/done/{session_id}?profile=me")
    line = page.locator("#sync-status")
    expect(line).to_contain_text("sync failed")
    expect(page.locator(".sync-retry")).to_have_count(1)

    page.locator(".sync-retry button").click()

    expect(page.locator("#sync-status")).to_contain_text("sync failed")


def test_a_deployment_with_no_token_says_the_session_is_stored_locally(page: Page, tokenless_url: str) -> None:
    """The fresh-install state. No token, no request, and a line that promises nothing.

    Deliberately different wording from PRP-02's offline banner ("Saved on this phone — will
    sync"): that one is about the browser's own outbox and this one is about the server's queue,
    and both can be on screen at once.
    """
    session_id = _finish_a_session(tokenless_url)

    page.goto(f"{tokenless_url}/done/{session_id}?profile=me")

    line = page.locator("#sync-status")
    expect(line).to_contain_text("stored locally")
    expect(line).to_contain_text("VitalForge not configured")
    expect(line).to_have_attribute("data-sync", "skipped")
    # Whether this state also carries a Retry button is not asserted here. PRP-06 section 8 says
    # "Retry only on the terminal one" and ``writeback.line_for`` currently offers one on
    # ``skipped`` as well, deliberately. That disagreement is reported rather than settled by a
    # browser test, and the line itself - which is what a person reads - is the same either way.
