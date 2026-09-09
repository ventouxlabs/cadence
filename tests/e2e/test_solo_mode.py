"""Solo mode in a real browser at 390x844 (D-260).

Named ``test_solo_mode`` and not ``test_son_mode``: ``tests/e2e/test_son_mode.py`` already exists
and means something else entirely — the *youth* CSS scope (D-234), which is about what a
nine-year-old sees when he is looking at the app. This file is about whether he appears in it at
all. The two are unrelated and the names have to stay apart.

The end-to-end server is session-scoped and shared by every module in this suite, so a test that
turned the son off and walked away would silently put every test after it into solo mode - tabs
gone, ``?profile=son`` redirecting - and the failures would read as unrelated regressions.
``solo`` therefore restores the setting in a ``finally``, whatever the test does.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, expect

TABS = "nav.tabs"


def _set_son_enabled(api: httpx.Client, value: bool) -> None:
    response = api.put("/api/settings", json={"son_enabled": value})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["son_enabled"] is value


@pytest.fixture
def solo(api: httpx.Client) -> Iterator[None]:
    """The son hidden for the duration of one test, and shown again however it ends."""
    _set_son_enabled(api, False)
    try:
        yield
    finally:
        _set_son_enabled(api, True)


def test_the_three_tabs_are_there_before_anything_is_hidden(page: Page) -> None:
    """The control group, so the assertions below are about the setting and not about the markup."""
    page.goto("/today?profile=me")
    expect(page.locator(f"{TABS} a.tab")).to_have_count(4)  # Me, Son, Both, and the gear
    for label in ("Me", "Son", "Both"):
        expect(page.locator(f"{TABS} a.tab", has_text=label).first).to_be_visible()


def test_only_me_is_offered_when_the_son_is_hidden(page: Page, solo: None) -> None:
    """One profile tab and the gear. "Son" and "Both" are not rendered, not merely disabled."""
    page.goto("/today?profile=me")

    expect(page.locator(f'{TABS} a[href*="profile=me"]')).to_be_visible()
    expect(page.locator(f'{TABS} a[href*="profile=son"]')).to_have_count(0)
    expect(page.locator(f'{TABS} a[href*="profile=together"]')).to_have_count(0)
    # The gear survives - it is the way back to the toggle that turned this on.
    expect(page.locator(f"{TABS} a.tab-gear")).to_be_visible()


def test_history_loses_the_tabs_too(page: Page, solo: None) -> None:
    """The strip is built per request, so every screen that renders it agrees (D-263)."""
    page.goto("/history?profile=me")
    expect(page.locator(f'{TABS} a[href*="profile=son"]')).to_have_count(0)
    expect(page.locator(f"{TABS} a.tab-gear")).to_be_visible()


def test_a_stale_link_to_the_sons_screen_lands_on_the_parents(page: Page, solo: None) -> None:
    """A bookmark or a page the service worker cached is a soft landing, never an error."""
    page.goto("/today?profile=son")

    assert page.url.endswith("/today?profile=me")
    expect(page.locator("main")).to_be_visible()
    # Not an error page: the 404 template is what a redirect would have been mistaken for.
    assert "no session" not in page.locator("main").inner_text().lower()


def test_the_shared_tab_lands_there_as_well(page: Page, solo: None) -> None:
    page.goto("/history?profile=together")
    assert page.url.endswith("/history?profile=me")


def test_the_toggle_round_trips_on_the_settings_screen(page: Page, api: httpx.Client) -> None:
    """Off, and then on again, driven the way JD would drive it.

    The son's own controls go with him, which is the visible half of "nothing here is deleted":
    his age box disappearing while his stored age does not is exactly the promise the copy makes.
    """
    page.goto("/settings")
    expect(page.locator("#son_age")).to_be_visible()

    # His stored profile, read rather than typed. An earlier cut compared the age box before and
    # after and would have asserted ``"" == ""``: this suite's seed records no age, so the box is
    # empty and the comparison could not fail. Setting one here would re-plan his block on a
    # server every other module in this suite shares, so the round trip is proved against what is
    # already stored instead of against something this test writes.
    before = api.get("/api/profiles/son").json()["data"]
    assert before["id"] == "son"

    try:
        # The label, not the radio. The segmented control hides its input behind a span, so a
        # `.check()` on the input itself is intercepted - `test_setup.py` drives them the same way.
        page.click('.segmented label.seg:has(input[name="son_enabled"][value="0"])')
        page.locator("#settings-form button.save").click()
        expect(page.locator("[data-son-note]")).to_be_visible()

        # His controls are gone from the form, and the tab strip has dropped him.
        expect(page.locator("#son_age")).to_have_count(0)
        expect(page.locator('input[name="push_son_to_garmin"]')).to_have_count(0)
        expect(page.locator('input[name="person_son"]')).to_have_count(0)
        expect(page.locator(f'{TABS} a[href*="profile=son"]')).to_have_count(0)
        assert api.get("/api/settings").json()["data"]["son_enabled"] is False
        # Hidden from the API too, which is the other half of D-260.
        assert api.get("/api/profiles/son").status_code == 404

        # Back on, and every one of his stored fields is exactly what it was.
        page.click('.segmented label.seg:has(input[name="son_enabled"][value="1"])')
        page.locator("#settings-form button.save").click()
        expect(page.locator("#son_age")).to_be_visible()
        expect(page.locator(f'{TABS} a[href*="profile=son"]').first).to_be_visible()
        assert api.get("/api/profiles/son").json()["data"] == before
    finally:
        _set_son_enabled(api, True)
