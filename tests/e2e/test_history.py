"""PRP-04 acceptance tests 19-23, in a real browser at 390x844.

The end-to-end database is session-scoped and its four-week block holds sixteen planned days per
profile, which every ``Done`` in this suite spends. So exactly **one** session here is driven
through PRP-02's routes — that is the thing worth proving in a browser — and the extra cards the
list tests need are written straight into the database over synthetic ``done`` planned rows at
week 9, which no ``resolve_today`` will ever reach.

The seed itself lives in ``tests/e2e/conftest.py``. A session-scoped fixture imported into a
second test module is a *second* fixture as far as pytest is concerned, and it ran the whole
setup twice; the conftest is the one place both modules can share one.
"""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect

from tests.e2e.conftest import EXTRA_SESSIONS, MIN_NAV_PX
from tests.test_web_today import BARE_WORDS, BODY_IMAGE_PHRASES

MIN_TAP_PX = 56

METRIC_ELEMENTS = re.compile(
    r'class="(?:row-note|row-notice|session-sub|notice|row-line|summary-line|step-value)"[^>]*>([^<]*)<'
)


# ---------------------------------------------------------------- 19: the list renders


def test_history_renders_sessions(page: Page, history_seed: dict[str, str]) -> None:
    """Three cards, each saying how much of its checklist was ticked."""
    page.goto("/history?profile=me")

    cards = page.locator('[data-role="session"]')
    expect(cards).to_have_count(EXTRA_SESSIONS + 1)
    for index in range(EXTRA_SESSIONS + 1):
        card = cards.nth(index)
        expect(card).to_be_visible()
        expect(card.locator(".summary-line")).to_contain_text(re.compile(r"\d+ of \d+"))
        assert card.locator("a.hcard-tap").bounding_box()["height"] >= MIN_TAP_PX

    # Nothing may claim a sync before PRP-06 exists.
    expect(page.locator('[data-role="sync"]').first).to_contain_text("Stored locally")


def test_scorecard_numbers(page: Page, history_seed: dict[str, str]) -> None:
    """The card counts this week against the plan and never clamps down to it."""
    page.goto("/history?profile=me")

    score = page.locator('[data-role="score"]')
    expect(score).to_be_visible()
    expect(score).to_have_text(re.compile(r"^\d+ of \d+ sessions$"))
    done, planned = (int(part) for part in re.findall(r"\d+", score.inner_text())[:2])
    assert done >= EXTRA_SESSIONS + 1, score.inner_text()
    assert planned >= 2

    expect(page.locator('[data-role="streak"]')).to_contain_text("Streak")
    expect(page.locator('[data-role="streak"]')).to_contain_text("Best")
    expect(page.locator(".dot")).to_have_count(max(done, planned))
    expect(page.locator(".dot-on")).to_have_count(min(done, max(done, planned)))


def test_history_link_is_in_the_nav(page: Page, history_seed: dict[str, str]) -> None:
    """PRP-02 left room for it, and Today is still where the brand goes.

    Both header links are real tap targets. The brand is how you get back to Today from here, so
    a 24 px-tall word is a miss on a phone held one-handed, whatever the rest of the page does.
    """
    page.goto("/today?profile=me")
    link = page.locator('header a[href="/history?profile=me"]')
    expect(link).to_be_visible()
    assert link.bounding_box()["height"] >= MIN_NAV_PX, link.bounding_box()

    brand = page.locator("header a.brand")
    expect(brand).to_be_visible()
    assert brand.bounding_box()["height"] >= MIN_NAV_PX, brand.bounding_box()

    link.click()
    page.wait_for_url("**/history?profile=me")
    expect(page.locator('[data-role="score"]')).to_be_visible()

    page.locator("header a.brand").click()
    page.wait_for_url("**/today?profile=me")


# ------------------------------------------------------------------- 20-21: the sparkline


def test_sparkline_visible_for_parent(page: Page, history_seed: dict[str, str]) -> None:
    page.goto("/history?profile=me")

    svg = page.locator('svg[data-role="trend"]')
    expect(svg).to_be_visible()
    expect(svg.locator("polyline")).to_have_count(2)
    assert svg.bounding_box()["width"] > 100
    expect(page.locator(".trend-values")).to_contain_text("84.1 kg")


def test_no_trend_line_for_son(page: Page, history_seed: dict[str, str]) -> None:
    """Not on his own tab, and not on the tab the two of them share either (D-105)."""
    for path in ("/history?profile=son", "/history?profile=together"):
        page.goto(path)
        expect(page.locator('[data-role="score"]').first).to_be_visible()
        expect(page.locator('svg[data-role="trend"]')).to_have_count(0)
        expect(page.locator("polyline")).to_have_count(0)


# -------------------------------------------------------------- 22: expand in place


def test_session_row_expands_in_place(page: Page, history_seed: dict[str, str]) -> None:
    """Tapping a card reveals its exercises without leaving the page."""
    page.goto("/history?profile=me")
    card = page.locator('[data-role="session"]').first
    expect(card.locator('[data-role="detail"]')).to_have_count(0)

    # A real navigation would clear this; a swap keeps it.
    page.evaluate("window.__stayed = 'yes'")
    card.locator("a.hcard-tap").click()

    expect(page.locator('[data-role="session"]').first.locator('[data-role="detail"]')).to_be_visible()
    assert page.evaluate("window.__stayed") == "yes"
    expect(page.locator('[data-role="detail"] .hdetail-row').first).to_be_visible()

    page.locator('[data-role="session"]').first.locator("a.hcard-tap").click()
    expect(page.locator('[data-role="detail"]')).to_have_count(0)
    assert page.evaluate("window.__stayed") == "yes"


# ------------------------------------------------------- 23: D-027 on the son's History


def test_youth_history_has_no_body_words(page: Page, history_seed: dict[str, str]) -> None:
    page.goto("/history?profile=son")
    expect(page.locator('[data-role="score"]')).to_be_visible()
    html = page.content().lower()

    for phrase in BODY_IMAGE_PHRASES:
        assert phrase not in html, phrase

    scoped = METRIC_ELEMENTS.findall(html)
    assert scoped, "no metric elements on the son's History: the check would pass vacuously"
    for chunk in scoped:
        for pattern in BARE_WORDS:
            assert not pattern.search(chunk), f"{pattern.pattern!r} in {chunk!r}"
