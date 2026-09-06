"""History in a real browser at 1024x768, and the Together tab at both sizes.

``tests/e2e/test_history.py`` covers acceptance tests 19-23 on the 390x844 phone. This file is the
second viewport the PRP asks for plus the one screen the phone suite does not reach: Together,
where two columns share a page and D-105 says neither of them may carry a body metric.

``history_seed`` comes from ``tests/e2e/conftest.py`` rather than from the phone module. It is
session-scoped and spends a planned day from a block that holds sixteen, and a session fixture
imported into a second module is a second fixture: pytest ran the whole setup twice and the
second copy collided on the planned rows the first had already written.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.e2e.conftest import EXTRA_SESSIONS, PHONE, TABLET

MIN_TAP_PX = 56


def _no_horizontal_scroll(page: Page) -> None:
    """The page may never scroll sideways: every wide thing owns its own overflow."""
    overflow = page.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
    assert overflow <= 1, f"the page scrolls {overflow}px sideways"


def test_history_on_a_tablet(page: Page, history_seed: dict[str, str]) -> None:
    """The same three cards and the same scorecard, at 1024x768."""
    page.set_viewport_size(TABLET)
    page.goto("/history?profile=me")

    expect(page.locator('[data-role="score"]')).to_be_visible()
    cards = page.locator('[data-role="session"]')
    expect(cards).to_have_count(EXTRA_SESSIONS + 1)
    expect(cards.first).to_be_visible()
    assert cards.first.locator("a.hcard-tap").bounding_box()["height"] >= MIN_TAP_PX
    _no_horizontal_scroll(page)

    svg = page.locator('svg[data-role="trend"]')
    expect(svg).to_be_visible()
    expect(svg.locator("polyline")).to_have_count(2)
    box = svg.bounding_box()
    assert box["width"] > 200 and box["height"] > 20, box


def test_the_sons_history_on_a_tablet_has_no_trend(page: Page, history_seed: dict[str, str]) -> None:
    page.set_viewport_size(TABLET)
    page.goto("/history?profile=son")

    expect(page.locator('[data-role="score"]')).to_be_visible()
    expect(page.locator('svg[data-role="trend"]')).to_have_count(0)
    expect(page.locator("polyline")).to_have_count(0)
    expect(page.locator('[data-role="badges"]')).to_have_count(1)
    _no_horizontal_scroll(page)


def test_a_card_expands_in_place_on_a_tablet(page: Page, history_seed: dict[str, str]) -> None:
    """The expand is HTMX at both sizes; a wider screen must not turn it into a navigation."""
    page.set_viewport_size(TABLET)
    page.goto("/history?profile=me")

    card = page.locator('[data-role="session"]').first
    expect(card.locator('[data-role="detail"]')).to_have_count(0)
    page.evaluate("window.__stayed = 'tablet'")
    card.locator("a.hcard-tap").click()

    expect(page.locator('[data-role="session"]').first.locator('[data-role="detail"]')).to_be_visible()
    assert page.evaluate("window.__stayed") == "tablet"
    _no_horizontal_scroll(page)


def test_together_history_on_a_tablet(page: Page, history_seed: dict[str, str]) -> None:
    """Two named columns, one badges container each, and no body metric on either (D-105)."""
    page.set_viewport_size(TABLET)
    page.goto("/history?profile=together")

    columns = page.locator(".hcolumn")
    expect(columns).to_have_count(2)
    expect(columns.nth(0)).to_be_visible()
    expect(columns.nth(1)).to_be_visible()
    expect(page.locator("#badges-me")).to_have_count(1)
    expect(page.locator("#badges-son")).to_have_count(1)
    expect(page.locator('svg[data-role="trend"]')).to_have_count(0)

    # Side by side at this width: the two columns must not be stacked.
    first, second = columns.nth(0).bounding_box(), columns.nth(1).bounding_box()
    assert second["x"] > first["x"], (first, second)
    _no_horizontal_scroll(page)


def test_together_history_on_a_phone(page: Page, history_seed: dict[str, str]) -> None:
    """The same page at 390px: still both columns, still no trend, still no sideways scroll."""
    page.set_viewport_size(PHONE)
    page.goto("/history?profile=together")

    expect(page.locator(".hcolumn")).to_have_count(2)
    expect(page.locator('[data-role="score"]').first).to_be_visible()
    expect(page.locator('svg[data-role="trend"]')).to_have_count(0)
    expect(page.locator("#badges-me")).to_have_count(1)
    _no_horizontal_scroll(page)

    # Each profile's own sessions live under its own column, never merged into one list.
    mine = page.locator('.hcolumn[data-profile="me"] [data-role="session"]')
    assert mine.count() >= 1


def test_the_scorecard_numbers_match_the_api(page: Page, history_seed: dict[str, str], api) -> None:
    """What the card says is what ``GET /api/scorecard`` says, at the tablet width too."""
    page.set_viewport_size(TABLET)
    page.goto("/history?profile=me")

    data = api.get("/api/scorecard?profile=me").json()["data"]
    expect(page.locator('[data-role="score"]')).to_have_text(
        f"{data['done_this_week']} of {data['planned_this_week']} sessions"
    )
    expect(page.locator('[data-role="streak"]')).to_have_text(
        f"Streak {data['current_streak']} · Best {data['best_streak']}"
    )
