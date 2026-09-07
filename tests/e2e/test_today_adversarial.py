"""What only a real browser can answer about Today.

The unit suite can prove the server does the right thing. It cannot prove the service worker
controls the page, that the timer never touches the radio, that a stylesheet did not shrink a
tap target below a thumb, or that a Done taken with the phone in a lift reaches the server when
the lift doors open. Those are here.

Everything runs at 390x844 unless the test says otherwise, against the real uvicorn in
``conftest.py``. The server keeps one database for the whole run, so every test starts by asking
``fresh_session`` for an untouched checklist rather than assuming one.
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import PHONE, TABLET

MIN_TAP_PX = 56

# D-025, in gzip transfer bytes. The per-asset gates are response *bodies*: headers are counted
# only in the 60 KB total, which is the number architecture section 5 actually budgets.
HTML_BUDGET = 30 * 1024
CSS_BUDGET = 10 * 1024
JS_BUDGET = 25 * 1024
TOTAL_BUDGET = 60 * 1024

COUNTDOWN_MS = 3200
MAX_TAB_STOPS = 60


def _row(page: Page, profile: str, position: int):
    return page.locator(f"#row-{profile}-{position}")


def _await_service_worker(page: Page) -> None:
    """Block until a worker is active *and* controlling this page.

    ``ready`` only promises an activated worker. ``clients.claim()`` in ``activate`` is what
    makes it control the page that registered it, and until that has happened a navigation is
    still going straight to the network - so a cache test would pass for the wrong reason.
    """
    page.wait_for_function(
        "() => navigator.serviceWorker && navigator.serviceWorker.controller !== null", timeout=15000
    )


# ------------------------------------------------------------------- the timer touches nothing


def test_the_timer_issues_no_network_request(page: Page, fresh_session) -> None:
    """PRP-02: the timer is vanilla, its state lives in the DOM, and it never calls the server.

    The listener goes on *after* the page has settled, so the page's own assets are not counted;
    what is counted is everything the browser asks for across a three-second countdown.
    """
    data = fresh_session("me")
    timed = next(row for row in data["rows"] if row["seconds"] is not None or (row["rest_s"] or 0) > 0)
    page.goto("/today?profile=me", wait_until="networkidle")
    _await_service_worker(page)
    page.wait_for_timeout(300)

    requests: list[str] = []
    page.on("request", lambda request: requests.append(f"{request.method} {request.url}"))

    button = _row(page, "me", timed["position"]).locator("button.timer")
    expect(button).to_have_attribute("aria-pressed", "false")
    button.click()
    expect(button).to_have_attribute("aria-pressed", "true")
    first = button.locator("[data-clock]").inner_text()
    page.wait_for_timeout(COUNTDOWN_MS)

    assert requests == [], f"the timer went to the network: {requests}"
    assert button.locator("[data-clock]").inner_text() != first, "the countdown did not advance"


def test_the_timer_is_off_until_it_is_tapped(page: Page, fresh_session) -> None:
    """Off by default, one tap on, a second tap off, and only ever one clock running."""
    data = fresh_session("me")
    page.goto("/today?profile=me")
    expect(page.locator('button.timer[aria-pressed="true"]')).to_have_count(0)

    timed = [row for row in data["rows"] if row["seconds"] is not None or (row["rest_s"] or 0) > 0]
    assert len(timed) >= 2, "this block has too few timed rows to prove one clock at a time"

    first = _row(page, "me", timed[0]["position"]).locator("button.timer")
    second = _row(page, "me", timed[1]["position"]).locator("button.timer")
    first.click()
    second.click()

    expect(page.locator('button.timer[aria-pressed="true"]')).to_have_count(1)
    expect(second).to_have_attribute("aria-pressed", "true")
    second.click()
    expect(page.locator('button.timer[aria-pressed="true"]')).to_have_count(0)


# ------------------------------------------------------------------------------ the felt strip


def test_the_felt_strip_appears_only_on_the_last_tick(page: Page, fresh_session) -> None:
    """Hidden at zero, still hidden one row short, visible on the row that completes the list."""
    data = fresh_session("me")
    rows = data["rows"]
    assert len(rows) >= 2
    page.goto("/today?profile=me")
    expect(page.locator("fieldset.felt")).to_have_count(0)

    for row in rows[:-1]:
        _row(page, "me", row["position"]).locator(".tick").check()
        page.wait_for_timeout(80)
    expect(page.locator("fieldset.felt")).to_have_count(0)

    _row(page, "me", rows[-1]["position"]).locator(".tick").check()

    expect(page.locator("fieldset.felt")).to_be_visible()
    expect(page.locator('div.segments[role="radiogroup"]')).to_be_visible()
    for value in ("easy", "right", "hard"):
        expect(page.locator(f'button.segment[value="{value}"]')).to_be_visible()


def test_the_done_screen_reports_the_write_back(page: Page, fresh_session) -> None:
    """PRP-06's sync line. The server runs in mock mode, so the inline POST succeeds."""
    fresh_session("me")
    page.goto("/today?profile=me")
    _row(page, "me", 1).locator(".tick").check()
    page.wait_for_timeout(150)
    page.locator("button.done").click()
    page.locator("button.done").click()
    page.wait_for_url("**/done/**")

    expect(page.locator("#sync-status")).to_contain_text("synced")
    # Scoped to the summary. The line reports what the write-back actually did and nothing more:
    # no spinner, no promise about Garmin, whose outcome VitalForge owns and Cadence never sees.
    summary = page.locator("section.summary").inner_text().lower()
    for promise in ("syncing", "uploaded", "garmin"):
        assert promise not in summary, f"the summary promised {promise!r}, which this build cannot do"


# ------------------------------------------------------------------------------------- offline


def _finish_offline(page: Page, session_id: str) -> None:
    """Tap Done with the radio off and land on the worker's offline summary.

    The button no longer leaves the user on the checklist: it queues the Done and navigates to
    ``/done/{id}``, which the service worker answers from the precached shell because the server
    cannot be reached. Getting there is the confirmation that the tap did something.
    """
    page.context.set_offline(True)
    page.locator("button.done").click()
    page.wait_for_url("**/done/**", timeout=15000)
    expect(page.locator(".summary-head")).to_contain_text("Done")


def test_a_done_taken_offline_finalises_the_session_after_reconnect(
    page: Page, fresh_session, api: httpx.Client
) -> None:
    """The whole point of the PWA, end to end.

    Tick online so the session has a start, tap Done with the radio off, then walk back the way a
    person does - the offline summary's own "Back to today" link - and let the queue drain on
    load. Success is the *server* holding a finished session, checked by replaying the Done and
    being told it was already done.

    The return trip is load-bearing, not incidental. The offline summary is a static shell that
    does not load ``app.js``, so it carries no ``online`` listener and drains nothing by itself:
    reconnecting while sitting on that screen syncs nothing until the user navigates.
    """
    data = fresh_session("me")
    session_id = data["session_id"]
    page.goto("/today?profile=me", wait_until="networkidle")
    page.wait_for_function("() => window.cadence !== undefined")
    _await_service_worker(page)
    _row(page, "me", 1).locator(".tick").check()
    page.wait_for_timeout(250)

    _finish_offline(page, session_id)
    assert api.get("/api/today?profile=me").json()["data"]["session_id"] == session_id, (
        "the session finished on the server while the phone was offline"
    )

    page.context.set_offline(False)
    with page.expect_response(
        lambda response: response.url.endswith(f"/api/sessions/{session_id}/done") and response.status == 200
    ):
        page.goto("/today?profile=me")

    replay = api.post(f"/api/sessions/{session_id}/done", json={})
    assert replay.status_code == 200
    assert replay.json()["meta"]["replayed"] is True, "the offline Done never reached the server"
    assert replay.json()["data"]["rows_done"] >= 1
    page.wait_for_function("() => window.cadence.pending().then(n => n === 0)", timeout=15000)


def test_the_offline_done_screen_admits_it_is_only_on_the_phone(page: Page, fresh_session) -> None:
    """The Done taken offline has to look finished without claiming to be synced.

    This is the screen a person sees at the end of a workout in a basement gym. It must say the
    session is saved, must not say it reached anything, and must come from the worker rather than
    from Chromium's own network-error page.
    """
    data = fresh_session("me")
    page.goto("/today?profile=me", wait_until="networkidle")
    page.wait_for_function("() => window.cadence !== undefined")
    _await_service_worker(page)
    _row(page, "me", 1).locator(".tick").check()
    page.wait_for_timeout(250)

    _finish_offline(page, data["session_id"])

    body = page.locator("section.summary").inner_text().lower()
    assert "this phone" in body, f"the offline summary does not say where the session is: {body!r}"
    for promise in ("stored locally.", "synced", "uploaded", "garmin"):
        assert promise not in body, f"the offline summary claimed {promise!r}"
    expect(page.locator('a.action[href*="/today"]')).to_be_visible()
    page.context.set_offline(False)


def test_offline_reload_still_renders_today_from_the_cache(page: Page, fresh_session) -> None:
    """Risk 5 and 6 together: the worker has to control ``/today`` and have a copy of it.

    The sequence matters. A first visit registers the worker but is not yet controlled by it, so
    nothing has gone through ``networkFirst`` and nothing is cached. One reload while online is
    what puts the page in the cache; only then is going offline a real test.
    """
    fresh_session("me")
    page.goto("/today?profile=me", wait_until="networkidle")
    _await_service_worker(page)

    page.reload(wait_until="networkidle")
    _await_service_worker(page)
    page.wait_for_timeout(300)

    page.context.set_offline(True)
    page.reload(wait_until="domcontentloaded")

    # Chromium's network-error page loads perfectly happily, so assert the checklist itself.
    expect(page.locator("form.row").first).to_be_visible()
    assert page.locator("form.row").count() >= 1
    expect(page.locator("button.done")).to_be_visible()
    expect(page.locator("input.tick").first).to_be_visible()
    page.context.set_offline(False)


# ------------------------------------------------------------------------------- page weight


def test_page_weight_is_inside_every_budget(page: Page, fresh_session, base_url: str) -> None:
    """D-025 per asset, not just the 60 KB total the existing test checks.

    A regression that swapped a 2 KB stylesheet for a 40 KB one while HTMX shrank would keep the
    total under budget and still be the thing D-025 exists to stop.
    """
    fresh_session("me")
    context = page.context.browser.new_context(viewport=PHONE, base_url=base_url)
    cold = context.new_page()
    seen: list[tuple[str, int, int]] = []

    def record(response) -> None:
        try:
            sizes = response.request.sizes()
        except Exception:  # pragma: no cover - a redirect can be gone before sizes() resolves
            return
        seen.append(
            (response.url, max(sizes.get("responseBodySize", 0), 0), max(sizes.get("responseHeadersSize", 0), 0))
        )

    cold.on("response", record)
    cold.goto("/today?profile=me", wait_until="networkidle")
    cold.wait_for_timeout(400)
    context.close()

    assert seen, "no responses were measured"

    def body_of(fragment: str) -> int:
        return sum(body for url, body, _ in seen if fragment in url)

    html = body_of("/today?profile=me")
    css = body_of("/static/style.css")
    javascript = body_of("/static/app.js") + body_of("/static/htmx.min.js")
    total = sum(body + headers for _, body, headers in seen)

    assert html, "the document itself was not measured"
    assert css and javascript, "the stylesheet or the scripts were not measured"
    assert html < HTML_BUDGET, f"HTML {html} gzip bytes over the {HTML_BUDGET} budget"
    assert css < CSS_BUDGET, f"CSS {css} gzip bytes over the {CSS_BUDGET} budget"
    assert javascript < JS_BUDGET, f"JS {javascript} gzip bytes over the {JS_BUDGET} budget"
    assert total < TOTAL_BUDGET, f"{total} gzip bytes (bodies plus headers) over the {TOTAL_BUDGET} budget"


def test_everything_on_today_is_compressed(page: Page, fresh_session) -> None:
    """Risk 13: without ``GZipMiddleware`` the 60 KB budget silently becomes 110 KB.

    The middleware is a two-line shim this PRP added to PRP-00's file, which makes it exactly the
    kind of thing a later merge drops.
    """
    fresh_session("me")
    encodings: dict[str, str] = {}
    page.on(
        "response",
        lambda response: encodings.__setitem__(response.url, response.headers.get("content-encoding", "none")),
    )
    page.goto("/today?profile=me", wait_until="networkidle")

    for fragment in ("/today?profile=me", "/static/style.css", "/static/htmx.min.js", "/static/app.js"):
        matched = [value for url, value in encodings.items() if fragment in url]
        assert matched, f"{fragment} was not fetched"
        assert all(value == "gzip" for value in matched), f"{fragment} came back uncompressed: {matched}"


# ----------------------------------------------------------------------------- accessibility


@pytest.mark.parametrize("profile", ["me", "son"])
def test_every_checkbox_is_labelled_and_reports_its_state(page: Page, fresh_session, profile: str) -> None:
    """A checklist a ten-year-old drives with a screen reader on is still a checklist."""
    fresh_session(profile)
    page.goto(f"/today?profile={profile}")

    boxes = page.locator("form.row input.tick")
    count = boxes.count()
    assert count > 0, "no checkboxes on the page"
    for index in range(count):
        box = boxes.nth(index)
        assert box.get_attribute("aria-checked") in ("true", "false"), f"row {index} has no aria-checked"
        label = box.get_attribute("aria-label")
        assert label and label.strip(), f"row {index} has no accessible name"
        assert box.evaluate("node => node.closest('label') !== null"), f"row {index} is not wrapped in a label"

    first = boxes.first
    first.check()
    expect(first).to_have_attribute("aria-checked", "true")
    first.uncheck()
    expect(first).to_have_attribute("aria-checked", "false")


def test_the_tab_order_reaches_the_done_button(page: Page, fresh_session) -> None:
    """The Done button is last in the document and must be reachable from the top by keyboard."""
    fresh_session("me")
    page.goto("/today?profile=me")
    page.locator("a.skip").focus()

    stops: list[str] = []
    for _ in range(MAX_TAB_STOPS):
        page.keyboard.press("Tab")
        stops.append(page.evaluate("() => document.activeElement ? document.activeElement.className : ''"))
        if "done" in stops[-1].split():
            break
    else:
        raise AssertionError(f"Done was not reachable in {MAX_TAB_STOPS} tab stops; visited {len(stops)}: {stops}")

    assert any("tick" in entry.split() for entry in stops), "the checkboxes are not in the tab order"
    assert any("adjust" in entry.split() for entry in stops), "the adjust controls are not in the tab order"


@pytest.mark.parametrize("profile", ["me", "son"])
def test_every_row_is_a_thumb_sized_tap_target(page: Page, fresh_session, profile: str) -> None:
    """Not just the first row: a cue-less row is the short one, and it is the one that breaks."""
    fresh_session(profile)
    page.goto(f"/today?profile={profile}")

    rows = page.locator("form.row")
    count = rows.count()
    assert count > 0
    for index in range(count):
        box = rows.nth(index).bounding_box()
        name = rows.nth(index).locator(".row-name").inner_text()
        assert box["height"] >= MIN_TAP_PX, f"{name!r} is {box['height']}px tall"

    for tool in page.locator("form.row button.tool").all():
        box = tool.bounding_box()
        assert box["width"] >= 44 and box["height"] >= 44, f"a row tool is {box['width']}x{box['height']}"


def test_together_rows_are_thumb_sized_in_both_columns(page: Page, fresh_session) -> None:
    """Two columns at 1024 halve the width available to every row on the tablet."""
    fresh_session("me")
    fresh_session("son")
    page.goto("/today?profile=together")
    page.set_viewport_size(TABLET)
    page.wait_for_timeout(150)

    for profile in ("me", "son"):
        rows = page.locator(f'section.session[data-profile="{profile}"] form.row')
        assert rows.count() > 0, f"no rows for {profile}"
        for index in range(rows.count()):
            box = rows.nth(index).bounding_box()
            assert box["height"] >= MIN_TAP_PX, f"{profile} row {index} is {box['height']}px tall"

    # Scoped to the footer on purpose. PRP-02 wants "one shared Done at the bottom", which is a
    # statement about the footer; acceptance test 24 next door pins the count of *every* control
    # with this class, and that is the assertion that has to argue with any per-column exit.
    shared = page.locator("#done-footer button.done")
    expect(shared).to_have_count(1)
    assert shared.bounding_box()["height"] >= MIN_TAP_PX
