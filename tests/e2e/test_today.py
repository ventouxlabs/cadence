"""PRP-02 acceptance tests 17-26, in a real browser at 390x844.

The point of these is what a stylesheet, a service worker or a queue does that a unit test cannot
see: tap-target height in pixels, a tick surviving a reload, two taps to change a rep, and an
offline tick reaching the server after the radio comes back.
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import TABLET

MIN_TAP_PX = 56
BUDGET_BYTES = 60 * 1024


def _row(page: Page, profile: str, position: int):
    return page.locator(f"#row-{profile}-{position}")


# ------------------------------------------------------------------------------ 17: layout


def test_today_renders_checklist(page: Page, fresh_session) -> None:
    fresh_session("me")
    page.goto("/today?profile=me")

    first = page.locator("form.row").first
    expect(first).to_be_visible()
    box = first.bounding_box()
    assert box["height"] >= MIN_TAP_PX, box

    done = page.locator("button.done")
    expect(done).to_be_in_viewport()
    assert done.bounding_box()["height"] >= MIN_TAP_PX

    for handle in page.locator("form.row .tick").all():
        assert handle.bounding_box() is not None


# -------------------------------------------------------------------- 18: tick persistence


def test_tick_persists_across_reload(page: Page, fresh_session, api: httpx.Client) -> None:
    data = fresh_session("me")
    page.goto("/today?profile=me")

    box = _row(page, "me", 2).locator(".tick")
    box.check()
    expect(box).to_have_attribute("aria-checked", "true")

    page.reload()

    reloaded = _row(page, "me", 2).locator(".tick")
    expect(reloaded).to_be_checked()
    expect(reloaded).to_have_attribute("aria-checked", "true")
    stored = api.get("/api/today?profile=me").json()["data"]
    assert stored["session_id"] == data["session_id"]
    assert next(row for row in stored["rows"] if row["position"] == 2)["done"] is True


# ------------------------------------------------------------------------- 19: two-tap adjust


def test_adjust_reps_in_two_taps(page: Page, fresh_session, api: httpx.Client) -> None:
    data = fresh_session("me")
    target = next(row for row in data["rows"] if row["reps"] is not None and row["adjustable"])
    page.goto("/today?profile=me")
    row = _row(page, "me", target["position"])

    row.locator("button.adjust").click()  # tap 1
    row.locator('button[aria-label="One rep more"]').click()  # tap 2

    expect(row.locator("[data-reps]")).to_have_text(str(target["reps"] + 1))
    stored = api.get("/api/today?profile=me").json()["data"]
    assert next(r for r in stored["rows"] if r["position"] == target["position"])["reps_done"] == target["reps"] + 1


# ------------------------------------------------------------------------------- 20: felt


def test_felt_appears_after_all_ticks(page: Page, fresh_session) -> None:
    data = fresh_session("me")
    page.goto("/today?profile=me")
    expect(page.locator("fieldset.felt")).to_have_count(0)

    for row in data["rows"]:
        _row(page, "me", row["position"]).locator(".tick").check()
        page.wait_for_timeout(60)

    expect(page.locator("fieldset.felt")).to_be_visible()
    expect(page.locator('button.segment[value="right"]')).to_be_visible()


# ------------------------------------------------------------------------------- 21: done


def test_done_shows_three_line_summary(page: Page, fresh_session) -> None:
    data = fresh_session("me")
    page.goto("/today?profile=me")
    _row(page, "me", 1).locator(".tick").check()
    page.wait_for_timeout(120)

    page.locator("button.done").click()  # "Done anyway": the felt strip appears
    page.locator("button.done").click()
    page.wait_for_url("**/done/**")

    lines = page.locator(".summary-line")
    expect(lines).to_have_count(3)
    expect(lines.nth(0)).to_contain_text("minute")
    expect(lines.nth(1)).to_contain_text(f"of {len(data['rows'])} exercises")
    expect(lines.nth(2)).to_contain_text("Next time")
    expect(page.locator("#sync-status")).to_contain_text("synced")


# ------------------------------------------------------------------------- 22: youth exit


def test_good_enough_done_visible_for_youth(page: Page, fresh_session, api: httpx.Client) -> None:
    data = fresh_session("son")
    threshold = data["good_enough_after"]
    assert threshold >= 3
    page.goto("/today?profile=son")

    done = page.locator("button.done")
    expect(done).to_be_visible()
    expect(done).to_have_text("Done")

    for row in data["rows"][:threshold]:
        _row(page, "son", row["position"]).locator(".tick").check()
        page.wait_for_timeout(80)

    expect(done).to_contain_text("Good enough")
    assert "done-promoted" in (done.get_attribute("class") or "")


# ---------------------------------------------------------------------------- 23: offline


def test_offline_tick_replays(page: Page, fresh_session, api: httpx.Client) -> None:
    data = fresh_session("me")
    session_id = data["session_id"]
    page.goto("/today?profile=me")
    page.wait_for_function("() => window.cadence !== undefined")

    page.context.set_offline(True)
    box = _row(page, "me", 1).locator(".tick")
    box.check()
    expect(box).to_be_checked()
    page.wait_for_function("() => window.cadence.pending().then(n => n > 0)")
    assert api.get("/api/today?profile=me").json()["data"]["rows"][0]["done"] is False

    with page.expect_response(
        lambda response: f"/api/sessions/{session_id}/rows/" in response.url and response.status == 200
    ):
        page.context.set_offline(False)
        page.evaluate("() => window.dispatchEvent(new Event('online'))")

    stored = api.get("/api/today?profile=me").json()["data"]
    assert next(row for row in stored["rows"] if row["position"] == 1)["done"] is True


# --------------------------------------------------------------------------- 24: together


def test_together_stacked_then_side_by_side(page: Page, fresh_session) -> None:
    fresh_session("me")
    fresh_session("son")
    page.goto("/today?profile=together")

    parent = page.locator('section.session[data-profile="me"]')
    child = page.locator('section.session[data-profile="son"]')
    expect(parent).to_be_visible()
    expect(child).to_be_visible()

    last_parent_row = parent.locator("form.row").last.bounding_box()
    child_head = child.locator(".session-head").bounding_box()
    assert child_head["y"] >= last_parent_row["y"] + last_parent_row["height"] - 1

    page.set_viewport_size(TABLET)
    page.wait_for_timeout(120)
    assert (
        abs(parent.locator(".session-head").bounding_box()["y"] - child.locator(".session-head").bounding_box()["y"])
        < 4
    )
    # PRP-02 lines 143/157/161: one shared Done. D-084 keeps that literally true -- exactly one
    # element is named "Done" - while the son keeps the always-visible exit of principles 3.7 P6
    # in his own column, which finalises only his checklist and never wears that name.
    expect(page.get_by_role("button", name="Done", exact=True)).to_have_count(1)
    expect(page.locator("#done-footer button.done")).to_have_count(1)
    expect(page.locator('section.session[data-profile="son"] button.exit')).to_have_count(1)
    expect(page.locator('section.session[data-profile="me"] button.exit')).to_have_count(0)


# ------------------------------------------------------------------------------ 25: timer


def test_timer_off_by_default(page: Page, fresh_session, api: httpx.Client) -> None:
    data = fresh_session("me")
    page.goto("/today?profile=me")

    expect(page.locator('button.timer[aria-pressed="true"]')).to_have_count(0)
    for row in data["rows"]:
        expected = 1 if (row["seconds"] is not None or (row["rest_s"] or 0) > 0) else 0
        expect(_row(page, "me", row["position"]).locator("button.timer")).to_have_count(expected)

    timed = next(row for row in data["rows"] if row["seconds"] is not None or (row["rest_s"] or 0) > 0)
    button = _row(page, "me", timed["position"]).locator("button.timer")
    button.click()
    expect(button).to_have_attribute("aria-pressed", "true")
    first = button.locator("[data-clock]").inner_text()
    page.wait_for_timeout(1400)
    assert button.locator("[data-clock]").inner_text() != first


# ------------------------------------------------------------------------ 26: page weight


def test_page_weight_under_budget(page: Page, fresh_session, base_url: str) -> None:
    """A cold load, in a context with nothing cached and no worker controlling it yet."""
    fresh_session("me")
    context = page.context.browser.new_context(viewport={"width": 390, "height": 844}, base_url=base_url)
    cold = context.new_page()
    transferred: list[int] = []

    def record(response) -> None:
        try:
            sizes = response.request.sizes()
        except Exception:  # pragma: no cover - a redirect can be gone before sizes() resolves
            return
        transferred.append(max(sizes.get("responseBodySize", 0), 0) + max(sizes.get("responseHeadersSize", 0), 0))

    cold.on("response", record)
    cold.goto("/today?profile=me", wait_until="networkidle")
    cold.wait_for_timeout(300)
    total = sum(transferred)
    context.close()

    assert transferred, "no responses were measured"
    assert total < BUDGET_BYTES, f"{total} bytes transferred on a cold /today"


# ------------------------------------------------------------------- no-JavaScript fallback


def test_tick_and_done_work_without_javascript(fresh_session, api: httpx.Client, browser, base_url: str) -> None:
    fresh_session("me")
    context = browser.new_context(java_script_enabled=False, viewport={"width": 390, "height": 844}, base_url=base_url)
    page = context.new_page()
    page.goto("/today?profile=me")

    row = page.locator("#row-me-1")
    row.locator(".tick").check()
    row.locator("button.noscript-go").click()
    page.wait_for_url("**/today**")

    assert api.get("/api/today?profile=me").json()["data"]["rows"][0]["done"] is True

    page.locator("button.done").click()
    page.wait_for_url("**/today**")
    page.locator("button.done").click()
    page.wait_for_url("**/done/**")
    expect(page.locator(".summary-line")).to_have_count(3)
    context.close()


# ---------------------------------------------------------------- the worker and the shell


def test_service_worker_registers_at_the_root(page: Page, fresh_session) -> None:
    fresh_session("me")
    page.goto("/today?profile=me")
    scope = page.evaluate(
        "() => navigator.serviceWorker.ready.then(reg => reg.scope)",
    )
    assert scope.endswith("/")


@pytest.mark.parametrize("profile", ["me", "son", "together"])
def test_no_external_requests(page: Page, fresh_session, profile: str) -> None:
    fresh_session("me")
    fresh_session("son")
    external: list[str] = []
    page.on("request", lambda request: external.append(request.url) if "127.0.0.1" not in request.url else None)
    page.goto(f"/today?profile={profile}", wait_until="networkidle")
    assert external == []


# ------------------------------------- the offline queue, after the review (D-083)


def test_offline_done_navigates_and_says_it_will_sync(page: Page, fresh_session, api: httpx.Client) -> None:
    """Medium 3: offline, Done used to swallow the tap and leave the user on the checklist."""
    data = fresh_session("me")
    session_id = data["session_id"]
    page.goto("/today?profile=me")
    page.wait_for_function("() => window.cadence !== undefined")
    # The worker has to be in charge before the navigation, or nothing serves the fallback.
    page.evaluate("() => navigator.serviceWorker.ready")
    page.reload()
    page.wait_for_function("() => navigator.serviceWorker.controller !== null")

    _row(page, "me", 1).locator(".tick").check()
    page.wait_for_timeout(200)
    page.context.set_offline(True)
    page.locator("button.done").click()

    page.wait_for_url(f"**/done/{session_id}")
    expect(page.locator("#sync-status")).to_contain_text("will sync")
    assert api.get("/api/today?profile=me").json()["data"]["session_id"] == session_id

    page.context.set_offline(False)


def test_offline_felt_rides_along_with_the_queued_done(page: Page, fresh_session, api: httpx.Client) -> None:
    """Medium 3: `felt` had no offline path at all, so the answer was simply lost."""
    data = fresh_session("me")
    session_id = data["session_id"]
    page.goto("/today?profile=me")
    page.wait_for_function("() => window.cadence !== undefined")
    for row in data["rows"]:
        _row(page, "me", row["position"]).locator(".tick").check()
        page.wait_for_timeout(50)

    page.context.set_offline(True)
    page.locator('button.segment[value="hard"]').click()
    page.wait_for_function(f"() => window.cadence.feltFor('{session_id}').then(v => v === 'hard')")

    with page.expect_response(lambda r: "/felt" in r.url and r.status == 200):
        page.context.set_offline(False)
        page.evaluate("() => window.dispatchEvent(new Event('online'))")

    assert api.get("/api/today?profile=me").json()["data"]["felt"] == "hard"


def test_a_tick_that_fails_online_is_not_lost(page: Page, fresh_session, api: httpx.Client) -> None:
    """Medium 6: the queue used to record a tick only when `navigator.onLine` was false."""
    fresh_session("me")
    page.goto("/today?profile=me")
    page.wait_for_function("() => window.cadence !== undefined")
    # Online, but every write fails. The tick must survive to be replayed.
    page.route("**/today/*/rows/*/tick*", lambda route: route.fulfill(status=503, body="nope"))

    _row(page, "me", 3).locator(".tick").check()
    page.wait_for_function("() => window.cadence.pending().then(n => n > 0)")
    assert api.get("/api/today?profile=me").json()["data"]["rows"][2]["done"] is False

    page.unroute("**/today/*/rows/*/tick*")
    with page.expect_response(lambda r: "/api/sessions/" in r.url and r.status == 200):
        page.evaluate("() => window.cadence.replay()")

    stored = api.get("/api/today?profile=me").json()["data"]
    assert next(row for row in stored["rows"] if row["position"] == 3)["done"] is True


def test_a_refused_operation_is_kept_and_reported(page: Page, fresh_session) -> None:
    """Medium 5: a 4xx used to count as success and the entry vanished without a word."""
    fresh_session("me")
    page.goto("/today?profile=me")
    page.wait_for_function("() => window.cadence !== undefined")

    page.evaluate(
        """() => fetch('/api/sessions/no-such-session/rows/1', {
             method: 'POST', headers: {'Content-Type': 'application/json'},
             body: JSON.stringify({done: true})
           })"""
    )
    # Queue an operation the server will always refuse, then drain.
    page.evaluate(
        """() => new Promise(resolve => {
             const open = indexedDB.open('cadence-queue', 1);
             open.onsuccess = () => {
               const tx = open.result.transaction('ops', 'readwrite');
               tx.objectStore('ops').add({
                 session_id: 'no-such-session', kind: 'row', position: 1,
                 patch: {done: true, ts: new Date().toISOString()},
                 ts: Date.now() - 10000, attempts: 0, blocked: false, last_error: null
               });
               tx.oncomplete = resolve;
             };
           })"""
    )
    for _ in range(6):
        page.evaluate("() => window.cadence.replay()")
        page.wait_for_timeout(150)

    state = page.evaluate("() => window.cadence.counts()")
    assert state["total"] >= 1, state
    assert state["blocked"] >= 1, state
    expect(page.locator("#offline-banner")).to_be_visible()


# ------------------------------------------- the last review batch (D-083 h-l)


def test_the_son_gets_his_own_exit_on_together(page: Page, fresh_session, api: httpx.Client) -> None:
    """D-083(j): promotion is per session, so the parent's progress cannot hide the son's exit."""
    fresh_session("me")
    child = fresh_session("son")
    threshold = child["good_enough_after"]
    assert threshold >= 3
    page.goto("/today?profile=together")

    column = page.locator('section.session[data-profile="son"]')
    exit_button = column.locator("button.exit")
    expect(exit_button).to_be_visible()
    # Always tappable, never named "Done": that name belongs to the one shared button (D-084).
    expect(exit_button).to_contain_text("I'm finished for today")
    expect(page.get_by_role("button", name="Done", exact=True)).to_have_count(1)
    # The parent has ticked nothing, and it makes no difference to the son's own control.
    expect(page.locator("#done-footer button.done")).not_to_contain_text("Good enough")

    for row in child["rows"][:threshold]:
        page.locator(f"#row-son-{row['position']} .tick").check()
        page.wait_for_timeout(90)

    expect(exit_button).to_contain_text("Good enough — I'm done!")
    assert "exit-promoted" in (exit_button.get_attribute("class") or "")
    expect(page.locator("#done-footer button.done")).not_to_contain_text("Good enough")
    expect(page.get_by_role("button", name="Done", exact=True)).to_have_count(1)


def test_the_together_footer_does_not_cover_the_last_row(page: Page, fresh_session) -> None:
    """D-083(k): two felt strips are taller than one, and the padding is measured, not guessed."""
    parent = fresh_session("me")
    child = fresh_session("son")
    page.goto("/today?profile=together")
    for row in parent["rows"]:
        page.locator(f"#row-me-{row['position']} .tick").check()
        page.wait_for_timeout(50)
    for row in child["rows"]:
        page.locator(f"#row-son-{row['position']} .tick").check()
        page.wait_for_timeout(50)

    expect(page.locator("fieldset.felt")).to_have_count(2)
    page.keyboard.press("End")
    page.wait_for_timeout(300)

    footer_top = page.locator("#done-footer").bounding_box()["y"]
    last_row = page.locator('section.session[data-profile="son"] form.row').last.bounding_box()
    assert last_row["y"] + last_row["height"] <= footer_top + 1, (last_row, footer_top)


def test_a_tick_keeps_the_done_anyway_strip_open(page: Page, fresh_session) -> None:
    """D-083(l): the out-of-band footer used to re-render with the confirm state dropped."""
    data = fresh_session("me")
    page.goto("/today?profile=today" if False else "/today?profile=me")

    page.locator("button.done").click()  # "Done anyway": the strip opens
    expect(page.locator("fieldset.felt")).to_be_visible()

    page.locator(f"#row-me-{data['rows'][0]['position']} .tick").check()
    page.wait_for_timeout(250)

    expect(page.locator("fieldset.felt")).to_be_visible()
    expect(page.locator(".hint")).to_contain_text("Tap Done again")


def test_offline_done_takes_only_its_own_felt(page: Page, fresh_session) -> None:
    """D-083(i): the son's answer was being stamped onto the parent's queued Done.

    The Done tapped is the son's own column exit, and the parent's strip sits *above* his in the
    document. An unscoped ``querySelector`` therefore returns the parent's answer, so the two
    profiles picking different answers is what makes the assertion able to fail.
    """
    parent = fresh_session("me")
    child = fresh_session("son")
    page.goto("/today?profile=together")
    page.wait_for_function("() => window.cadence !== undefined")
    for row in parent["rows"]:
        page.locator(f"#row-me-{row['position']} .tick").check()
        page.wait_for_timeout(50)
    for row in child["rows"]:
        page.locator(f"#row-son-{row['position']} .tick").check()
        page.wait_for_timeout(50)

    page.locator(f'button.segment[data-session="{parent["session_id"]}"][value="easy"]').click()
    page.wait_for_timeout(200)

    page.context.set_offline(True)
    page.locator(f'button.segment[data-session="{child["session_id"]}"][value="hard"]').click()
    page.wait_for_timeout(150)
    page.locator('section.session[data-profile="son"] button.exit').click()
    # Offline the Done navigates to the worker's summary, which does not load app.js, so the
    # queue is read straight out of IndexedDB rather than through window.cadence.
    page.wait_for_url(f"**/done/{child['session_id']}")

    queued = page.evaluate(
        "() => new Promise(resolve => {"
        "  const open = indexedDB.open('cadence-queue', 1);"
        "  open.onsuccess = () => {"
        "    const tx = open.result.transaction('ops', 'readonly');"
        "    tx.objectStore('ops').getAll().onsuccess = e => resolve("
        "      e.target.result.map(op => [op.kind, op.session_id, (op.patch || {}).felt || null]));"
        "  };"
        "})"
    )
    done_ops = [item for item in queued if item[0] == "done"]
    assert done_ops, f"no Done was queued: {queued}"
    for _, session_id, value in done_ops:
        assert session_id == child["session_id"], f"the son's exit queued someone else's Done: {queued}"
        assert value == "hard", f"the son's Done took the parent's answer: {queued}"
    assert ["felt", child["session_id"], "hard"] in [list(item) for item in queued], queued

    page.context.set_offline(False)


def test_the_youth_exit_offline_queues_only_his_session(page: Page, fresh_session, api: httpx.Client) -> None:
    """D-084 offline: the son's exit queues a solo Done for himself and lands on his summary."""
    parent = fresh_session("me")
    child = fresh_session("son")
    page.goto("/today?profile=together")
    page.wait_for_function("() => window.cadence !== undefined")
    for row in child["rows"]:
        page.locator(f"#row-son-{row['position']} .tick").check()
        page.wait_for_timeout(50)

    page.context.set_offline(True)
    page.locator('section.session[data-profile="son"] button.exit').click()
    page.wait_for_url(f"**/done/{child['session_id']}")

    queued = page.evaluate(
        "() => new Promise(resolve => {"
        "  const open = indexedDB.open('cadence-queue', 1);"
        "  open.onsuccess = () => {"
        "    const tx = open.result.transaction('ops', 'readonly');"
        "    tx.objectStore('ops').getAll().onsuccess = e => resolve("
        "      e.target.result.filter(op => op.kind === 'done')"
        "        .map(op => [op.session_id, (op.patch || {}).scope || null]));"
        "  };"
        "})"
    )
    assert queued == [[child["session_id"], "solo"]], queued

    page.context.set_offline(False)
    # Wait on the Done actually landing, not on the queue reporting itself empty: the drain runs
    # oldest-first and the Done is the last record in it.
    with page.expect_response(
        lambda r: r.url.endswith(f"/api/sessions/{child['session_id']}/done") and r.status == 200
    ):
        page.goto("/today?profile=together")

    assert api.get("/api/today?profile=son").json()["data"]["session_id"] != child["session_id"]
    assert api.get("/api/today?profile=me").json()["data"]["session_id"] == parent["session_id"]
