"""Full-page screenshots of every Today surface, for a human to look at.

These are not assertions about pixels - the layout tests next door do that. They exist so a
reviewer can see the six screens this PRP ships without running the app: the two solo
checklists, Together on a phone and on a tablet, an open adjust panel, and the Done summary.

Output goes to ``$CADENCE_SHOTS_DIR`` when it is set, otherwise ``.shots/`` in the repo, which
is git-ignored. Each test still asserts that the screen it is photographing is the right one,
so a broken page produces a failure rather than a picture of an error card.
"""

from __future__ import annotations

import os
from pathlib import Path

from playwright.sync_api import Page, expect

from tests.e2e.conftest import PHONE, TABLET

REPO = Path(__file__).resolve().parents[2]
SHOTS = Path(os.environ.get("CADENCE_SHOTS_DIR") or (REPO / ".shots"))


def _shoot(page: Page, name: str) -> Path:
    SHOTS.mkdir(parents=True, exist_ok=True)
    target = SHOTS / name
    page.screenshot(path=str(target), full_page=True)
    assert target.exists() and target.stat().st_size > 0, f"{name} was not written"
    return target


def test_shot_today_me(page: Page, fresh_session) -> None:
    fresh_session("me")
    page.goto("/today?profile=me", wait_until="networkidle")
    expect(page.locator("form.row").first).to_be_visible()
    expect(page.locator("button.done")).to_be_visible()
    _shoot(page, "today-me-390.png")


def test_shot_today_son(page: Page, fresh_session) -> None:
    """The son's checklist at zero ticks: the Done control is there and says ``Done``."""
    fresh_session("son")
    page.goto("/today?profile=son", wait_until="networkidle")
    expect(page.locator("form.row").first).to_be_visible()
    expect(page.locator("button.done")).to_have_text("Done")
    _shoot(page, "today-son-390.png")


def test_shot_today_adjust_open(page: Page, fresh_session) -> None:
    """The two-tap adjust, photographed after the first tap: the stepper panel is open."""
    data = fresh_session("me")
    target = next(row for row in data["rows"] if row["reps"] is not None and row["adjustable"])
    page.goto("/today?profile=me", wait_until="networkidle")

    row = page.locator(f"#row-me-{target['position']}")
    row.locator("button.adjust").click()
    expect(row.locator(".stepper-panel")).to_be_visible()
    expect(row.locator("[data-reps]")).to_be_visible()

    _shoot(page, "today-adjust-open-390.png")


def test_shot_together_phone(page: Page, fresh_session) -> None:
    """Stacked: the son's section sits under the parent's, one shared Done at the foot."""
    fresh_session("me")
    fresh_session("son")
    page.goto("/today?profile=together", wait_until="networkidle")
    expect(page.locator('section.session[data-profile="me"]')).to_be_visible()
    expect(page.locator('section.session[data-profile="son"]')).to_be_visible()
    expect(page.locator("#done-footer button.done")).to_have_count(1)
    _shoot(page, "together-390.png")


def test_shot_together_tablet(page: Page, fresh_session) -> None:
    """Side by side at 1024x768, with the two headings on the same line."""
    fresh_session("me")
    fresh_session("son")
    page.set_viewport_size(TABLET)
    page.goto("/today?profile=together", wait_until="networkidle")

    parent = page.locator('section.session[data-profile="me"] .session-head').bounding_box()
    child = page.locator('section.session[data-profile="son"] .session-head').bounding_box()
    assert abs(parent["y"] - child["y"]) < 4, f"the columns are not level: {parent['y']} vs {child['y']}"

    _shoot(page, "together-1024.png")
    page.set_viewport_size(PHONE)


def test_shot_done(page: Page, fresh_session) -> None:
    """The summary, reached the way a person reaches it rather than by URL."""
    fresh_session("me")
    page.goto("/today?profile=me", wait_until="networkidle")
    page.locator("#row-me-1 .tick").check()
    page.wait_for_timeout(150)
    page.locator("button.done").click()
    page.locator("button.done").click()
    page.wait_for_url("**/done/**")

    expect(page.locator(".summary-line")).to_have_count(3)
    expect(page.locator("#sync-status")).to_contain_text("synced")
    _shoot(page, "done-390.png")
