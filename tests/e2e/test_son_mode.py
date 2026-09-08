"""The son's half of the app, in a real browser at 390x844.

Everything here is about what a nine-year-old sees: bigger type, an icon on the row, an exit
control with real weight, a tick that acknowledges itself in under a third of a second, and no
sentence anywhere about a body. The parent's screens are the control group in the same document —
that is the point of son mode being a CSS scope rather than a second template tree.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import PHONE
from tests.test_web_today import BODY_IMAGE_PHRASES

MAX_ANIMATION_MS = 300

# PRP-10's copy rules for son mode, which are about failure rather than about bodies: "never
# 'skipped', 'incomplete' or 'missed'". A separate tuple from D-027's, and joined to it below —
# the body-image list is imported rather than restated, because two copies drift and the copy
# that drifts is the one that stops catching anything (`tests/test_youth_surface.py` says so).
FAILURE_WORDS = ("incomplete", "you missed", "gave up", "did not finish")

# Stems, matched on a word boundary rather than as substrings (D-246). `skipped` as a substring
# missed the assessment card's `Skip` button — a guard calibrated to pass against the exact
# screen it exists to police. `\bskip` catches both, and the boundary keeps it off words that
# merely contain the letters. `test_the_failure_pattern_catches_the_word_it_was_added_for`
# below is what stops this being tuned to the page again.
FAILURE_PATTERNS = tuple(re.compile(stem) for stem in (r"\bskip", r"\bmiss(ed|ing)\b", r"\bfail"))

BANNED_PHRASES = BODY_IMAGE_PHRASES + FAILURE_WORDS


def _computed(page: Page, selector: str, prop: str) -> str:
    return page.eval_on_selector(selector, f"el => getComputedStyle(el).{prop}")


def _px(value: str) -> float:
    match = re.match(r"([\d.]+)", value)
    assert match, f"not a length: {value!r}"
    return float(match.group(1))


def _seconds(value: str) -> float:
    """`0s`, `200ms`, or a comma-separated list of them. The longest one is the duration.

    An empty string is rejected rather than read as zero. Chromium returns `""` for most computed
    properties on an element that is not currently rendered — which a row mid-HTMX-swap is — and
    a silent 0.0 there makes "the animation is disabled" true of a page whose animation is fine.
    """
    assert value.strip(), "computed style came back empty: the element was read before it settled"
    longest = 0.0
    for part in value.split(","):
        part = part.strip()
        if part.endswith("ms"):
            longest = max(longest, float(part[:-2]) / 1000)
        elif part.endswith("s"):
            longest = max(longest, float(part[:-1]))
    return longest


def _settled(page: Page, selector: str) -> None:
    """Wait for HTMX to finish swapping the row in, so computed style is readable.

    `wait_for_selector(".row-done")` returns while the class is on an element HTMX is still
    settling, and `getComputedStyle` on that element answers with empty strings.
    """
    page.wait_for_selector(f"{selector}:not(.htmx-swapping):not(.htmx-settling):not(.htmx-request)")


def test_son_mode_type_scale(page: Page, fresh_session) -> None:
    """The son's first row name is larger than the parent's, and the scope is on the body."""
    fresh_session("me")
    page.goto("/today?profile=me", wait_until="networkidle")
    parent = _px(_computed(page, ".row-name", "fontSize"))
    assert page.locator("body[data-profile-kind]").count() == 0, "the parent's body carries the youth scope"

    fresh_session("son")
    page.goto("/today?profile=son", wait_until="networkidle")
    expect(page.locator('body[data-profile-kind="youth"]')).to_have_count(1)
    son = _px(_computed(page, ".row-name", "fontSize"))
    assert son > parent, f"the son's rows are {son}px against the parent's {parent}px"


def test_son_mode_rows_are_taller(page: Page, fresh_session) -> None:
    fresh_session("son")
    page.goto("/today?profile=son", wait_until="networkidle")
    box = page.locator("form.row").first.bounding_box()
    assert box is not None and box["height"] >= 64, box


def test_son_mode_icons_present(page: Page, fresh_session) -> None:
    """At least one `<use>` resolves on a row, and no `<img>` is doing the job instead."""
    fresh_session("son")
    page.goto("/today?profile=son", wait_until="networkidle")

    uses = page.locator("form.row svg.row-icon use")
    assert uses.count() > 0, "no row carries an inline icon"
    symbol = uses.first.get_attribute("href")
    assert symbol and symbol.startswith("#i-"), symbol
    assert page.locator(f"symbol{symbol}").count() == 1, f"{symbol} has no <symbol> to resolve to"
    # The icon has to actually paint: a `<use>` pointing at nothing renders a zero-sized box.
    box = page.locator("form.row svg.row-icon").first.bounding_box()
    assert box is not None and box["width"] >= 24 and box["height"] >= 24, box
    assert page.locator("form.row img").count() == 0, "a row uses an <img> for its icon"


def test_the_parent_gets_no_icons(page: Page, fresh_session) -> None:
    """Only the son pays: the parent's rows carry no icon markup and cost no library lookup."""
    fresh_session("me")
    page.goto("/today?profile=me", wait_until="networkidle")
    expect(page.locator("form.row").first).to_be_visible()
    assert page.locator("svg.row-icon").count() == 0
    assert page.locator("svg.sprite").count() == 0, "the parent's page carries the icon sprite"


def test_together_gives_icons_to_the_son_alone(page: Page, fresh_session) -> None:
    """Two people in one document: the sprite is included once, and only his rows reference it."""
    fresh_session("me")
    fresh_session("son")
    page.goto("/today?profile=together", wait_until="networkidle")
    assert page.locator("svg.sprite").count() == 1, "the sprite is included twice or not at all"
    assert page.locator('section.session[data-profile="me"] svg.row-icon').count() == 0
    assert page.locator('section.session[data-profile="son"] svg.row-icon').count() > 0
    # The body stays neutral here, or the parent's column would render in the son's type scale.
    assert page.locator("body[data-profile-kind]").count() == 0


def test_together_scopes_the_type_size_to_the_son_alone(page: Page, fresh_session) -> None:
    """D-234, both directions: neither column inherits the other's type scale.

    `test_together_gives_icons_to_the_son_alone` proves the body carries no youth attribute; that
    is the mechanism. This is the outcome, which is what would actually be wrong on the screen —
    the parent's checklist rendered at a nine-year-old's size, or the son's shrunk to an adult's
    because the section hook was dropped when the body one was.
    """
    fresh_session("me")
    page.goto("/today?profile=me", wait_until="networkidle")
    solo_parent = _px(_computed(page, ".row-name", "fontSize"))

    fresh_session("son")
    page.goto("/today?profile=son", wait_until="networkidle")
    solo_son = _px(_computed(page, ".row-name", "fontSize"))
    assert solo_son > solo_parent, "son mode is not applying at all, so this proves nothing"

    page.goto("/today?profile=together", wait_until="networkidle")
    both_parent = _px(_computed(page, 'section.session[data-profile="me"] .row-name', "fontSize"))
    both_son = _px(_computed(page, 'section.session[data-profile="son"] .row-name', "fontSize"))

    assert both_parent == solo_parent, f"the parent's column is {both_parent}px in Together, {solo_parent}px alone"
    assert both_son == solo_son, f"the son's column is {both_son}px in Together, {solo_son}px alone"
    assert both_son > both_parent, (both_son, both_parent)


def test_son_mode_done_prominent(page: Page, fresh_session) -> None:
    """After the band threshold the Done control grows and its label changes."""
    data = fresh_session("son")
    page.goto("/today?profile=son", wait_until="networkidle")
    done = page.locator("#done-footer button.done")
    before = done.bounding_box()
    assert before is not None
    expect(done).not_to_contain_text("Good enough")

    for row in data["rows"]:
        page.locator(f"#row-son-{row['position']} .tick").check()
        page.wait_for_timeout(120)

    expect(done).to_have_text("Good enough — done!")
    after = done.bounding_box()
    assert after is not None and after["height"] > before["height"], (before, after)
    assert after["height"] >= 64, after
    assert before["height"] >= 44, "the exit was below the tap floor before promotion"


def test_tick_animation_under_300ms_and_shifts_nothing(page: Page, fresh_session) -> None:
    fresh_session("son")
    page.goto("/today?profile=son", wait_until="networkidle")
    row = page.locator("#row-son-1")
    before = row.bounding_box()

    row.locator(".tick").check()
    page.wait_for_selector("#row-son-1.row-done")
    _settled(page, "#row-son-1")
    duration = _seconds(_computed(page, "#row-son-1 .tick", "animationDuration"))
    assert 0 < duration <= MAX_ANIMATION_MS / 1000, f"{duration}s"

    # `transform` and `opacity` only, so the row cannot move while the check pops.
    assert _computed(page, "#row-son-1 .tick", "animationName") != "none"
    page.wait_for_timeout(MAX_ANIMATION_MS + 50)
    after = row.bounding_box()
    assert before is not None and after is not None
    assert abs(before["height"] - after["height"]) < 2, (before, after)
    assert abs(before["y"] - after["y"]) < 2, (before, after)


def test_reduced_motion_disables_the_animation(browser, base_url: str, fresh_session) -> None:
    """`prefers-reduced-motion: reduce` turns it off entirely, not merely down.

    The media query is checked first: this browser reports `no-preference` by default, so without
    that assertion a build where the emulation silently stopped working would still pass here —
    and so would one where the animation is off for everybody, which is the D-235 failure.
    """
    fresh_session("son")
    context = browser.new_context(base_url=base_url, viewport=PHONE, reduced_motion="reduce")
    try:
        page = context.new_page()
        page.goto("/today?profile=son", wait_until="networkidle")
        assert page.evaluate("matchMedia('(prefers-reduced-motion: reduce)').matches") is True

        page.locator("#row-son-1 .tick").check()
        page.wait_for_selector("#row-son-1.row-done")
        _settled(page, "#row-son-1")
        assert _seconds(_computed(page, "#row-son-1 .tick", "animationDuration")) == 0
        assert _computed(page, "#row-son-1 .tick", "animationName") == "none"
    finally:
        context.close()


def test_no_banned_words_anywhere_in_the_youth_ui(page: Page, fresh_session) -> None:
    """Today, Done and History, as the son actually reaches them."""
    data = fresh_session("son")
    page.goto("/today?profile=son", wait_until="networkidle")
    seen = [("Today", page.inner_text("body"))]

    for row in data["rows"]:
        page.locator(f"#row-son-{row['position']} .tick").check()
        page.wait_for_timeout(100)
    page.locator("#done-footer button.done").click()
    page.wait_for_url("**/done/**")
    seen.append(("Done", page.inner_text("body")))

    page.goto("/history?profile=son", wait_until="networkidle")
    seen.append(("History", page.inner_text("body")))

    assert BANNED_PHRASES and FAILURE_PATTERNS, "the word lists are empty: this would pass vacuously"
    for where, body in seen:
        assert body.strip(), f"{where} rendered nothing, so this check would pass vacuously"
        lowered = body.lower()
        for phrase in BANNED_PHRASES:
            assert phrase not in lowered, f"{phrase!r} on the son's {where}"
        for pattern in FAILURE_PATTERNS:
            found = pattern.search(lowered)
            assert not found, f"{pattern.pattern!r} matched {found.group(0)!r} on the son's {where}"


def test_the_failure_pattern_catches_the_word_it_was_added_for() -> None:
    """The guard fails against the markup that got past its predecessor (D-246).

    `"skipped" not in text` passed happily on a page whose button said `Skip`. Pinning the
    pattern against that exact string is what keeps the next edit from re-tuning the guard to
    whatever the page happens to say.
    """
    old_button = '<button class="action action-quiet" type="submit">skip</button>'
    assert not any(word in old_button for word in FAILURE_WORDS), "the old list would have caught it"
    assert any(pattern.search(old_button) for pattern in FAILURE_PATTERNS)
    # And it is not so broad that ordinary copy trips it.
    for innocent in ("three in a row!", "18 minutes", "5 of 5 things done", "bear crawl 2 x 12 m"):
        assert not any(pattern.search(innocent) for pattern in FAILURE_PATTERNS), innocent


def test_the_sons_assessment_card_never_says_skip(page: Page, api) -> None:
    """The card renders on his Today, so its control is his copy too."""
    card = api.get("/today?profile=son").text
    if 'data-role="assessment-card"' not in card:
        pytest.skip("no assessment is due for the son on this seeded block")
    page.goto("/today?profile=son", wait_until="networkidle")
    control = page.locator('[data-role="assessment-card"] button')
    expect(control).to_have_text("Not today")


def test_the_parent_keeps_the_plain_word(api) -> None:
    """Only the son's copy changes; the adult card is untouched."""
    body = api.get("/today?profile=me").text
    if 'data-role="assessment-card"' in body:
        assert ">Skip<" in body, "the parent's card lost its own wording"


def test_the_sons_done_screen_gets_the_son_mode_pass(page: Page, history_seed, fresh_session) -> None:
    """High 2: `done.html` shares none of Today's selectors, so it needed its own four.

    The adult baseline is read from the session `history_seed` has **already** finished, rather
    than by finishing another one. A planned day spent here is a planned day `test_history`'s
    hardcoded card count does not expect, and the seeded block holds sixteen (see
    `conftest.fresh_session`); the fixture is session-scoped, so this costs nothing new.
    """
    page.goto(f"/done/{history_seed['session_id']}?profile=me", wait_until="networkidle")
    adult_head = _px(_computed(page, ".summary-head", "fontSize"))
    adult_line = _px(_computed(page, '[data-role="rows"]', "fontSize"))
    expect(page.locator(".summary-head")).to_contain_text("Done.")

    data = fresh_session("son")
    page.goto("/today?profile=son", wait_until="networkidle")
    for row in data["rows"]:
        page.locator(f"#row-son-{row['position']} .tick").check()
        page.wait_for_timeout(100)
    page.locator("#done-footer button.done").click()
    page.wait_for_url("**/done/**")

    expect(page.locator('body[data-profile-kind="youth"]')).to_have_count(1)
    expect(page.locator(".summary-head")).to_contain_text("Nice one.")
    expect(page.locator('[data-role="rows"]')).to_contain_text("things done")
    assert _px(_computed(page, ".summary-head", "fontSize")) > adult_head
    assert _px(_computed(page, '[data-role="rows"]', "fontSize")) > adult_line
    # The first complete session earns one, and the line names it rather than counting them.
    expect(page.locator('[data-role="new-badge"]').first).to_contain_text("New badge:")


def test_no_banned_words_on_assess_or_the_together_son_column(page: Page, fresh_session) -> None:
    """The two youth surfaces the sweep above does not reach.

    Assess is where the son meets the six-test battery, and Together is where his column shares a
    document with the parent's body-composition trend — so the son's half is read on its own
    rather than through `body`, which would fail on the parent's card and prove nothing about his.
    """
    fresh_session("me")
    fresh_session("son")

    page.goto("/assess?profile=son", wait_until="networkidle")
    assess = page.inner_text("body")

    page.goto("/today?profile=together", wait_until="networkidle")
    column = page.locator('section.session[data-profile="son"]')
    expect(column).to_be_visible()
    together = column.inner_text()

    for where, body in (("Assess", assess), ("Together's son column", together)):
        assert body.strip(), f"{where} rendered nothing, so this check would pass vacuously"
        lowered = body.lower()
        for phrase in BANNED_PHRASES:
            assert phrase not in lowered, f"{phrase!r} on the son's {where}"


def test_no_body_image_language_on_setup(page: Page) -> None:
    """D-027 on the screen where the son's age is set.

    Only the body-image list here, not the failure words: Setup is a configuration screen and
    "incomplete" is a legitimate thing for it to say about setup itself, whereas the failure-word
    rule is about how the son is spoken to on his own screens. "Weights available" is equipment,
    which is why the bare-word list is not applied either.
    """
    page.goto("/setup", wait_until="networkidle")
    body = page.inner_text("body")
    assert body.strip(), "Setup rendered nothing, so this check would pass vacuously"

    lowered = body.lower()
    for phrase in BODY_IMAGE_PHRASES:
        assert phrase not in lowered, f"{phrase!r} on Setup"


def test_badges_render_on_history(page: Page, fresh_session) -> None:
    """Earned tiles are filled, unearned outlined, and tapping one moves only the caption."""
    data = fresh_session("son")
    page.goto("/today?profile=son", wait_until="networkidle")
    for row in data["rows"]:
        page.locator(f"#row-son-{row['position']} .tick").check()
        page.wait_for_timeout(100)
    page.locator("#done-footer button.done").click()
    page.wait_for_url("**/done/**")

    page.goto("/history?profile=son", wait_until="networkidle")
    strip = page.locator("#badges-son")
    expect(strip).to_be_visible()
    expect(strip.locator("button.badge")).to_have_count(7)
    assert strip.locator('button.badge[data-earned="true"]').count() >= 1, "one session earned nothing"
    assert strip.locator('button.badge[data-earned="false"]').count() >= 1

    earned = strip.locator("button.badge.badge-on").first
    outline = strip.locator("button.badge:not(.badge-on)").first
    assert strip.locator("button.badge").first.bounding_box()["width"] >= 56
    filled = page.eval_on_selector("#badges-son button.badge.badge-on", "el => getComputedStyle(el).backgroundColor")
    hollow = page.eval_on_selector(
        "#badges-son button.badge:not(.badge-on)", "el => getComputedStyle(el).backgroundColor"
    )
    assert filled != hollow, f"earned and unearned look identical: {filled}"

    caption = page.locator("#badge-caption-son")
    before = caption.inner_text()
    tiles_before = strip.locator("button.badge").count()
    earned.click()
    expect(caption).not_to_have_text(before)
    assert strip.locator("button.badge").count() == tiles_before, "tapping a badge re-rendered the strip"
    assert "/history" in page.url and "badges" not in page.url, "tapping a badge navigated"

    outline.click()
    expect(caption).to_contain_text("not yet")


def test_a_badge_caption_cannot_be_read_from_the_other_tab(page: Page, api) -> None:
    """The same ownership rule `history_card` applies to a session card (D-243).

    The son's tab asking for the parent's badge gets the 404 an unknown badge id gets, so the
    reply says nothing about whose profile it was. Together carries both people and may read both.
    """
    assert api.get("/history/badges/me/first-session?profile=me").status_code == 200
    assert api.get("/history/badges/me/first-session?profile=son").status_code == 404
    assert api.get("/history/badges/me/first-session?profile=together").status_code == 200
    assert api.get("/history/badges/son/first-session?profile=son").status_code == 200
    assert api.get("/history/badges/me/no-such-badge?profile=me").status_code == 404


def test_the_sons_card_on_the_shared_done_keeps_his_type_scale(page: Page, fresh_session) -> None:
    """Together renders both people in one document, so the scope travels with the card (D-255).

    `base.html` keys the body on `profile_key == "son"`, which is inert on `?profile=together` —
    so before this the son's summary card sat at the adult scale beside correct wording.
    """
    fresh_session("me")
    data = fresh_session("son")
    page.goto("/today?profile=together", wait_until="networkidle")
    for row in data["rows"]:
        page.locator(f"#row-son-{row['position']} .tick").check()
        page.wait_for_timeout(100)
    page.locator('section.session[data-profile="me"] form.row .tick').first.check()
    page.wait_for_timeout(150)
    page.locator("#done-footer button.done").click()
    page.locator("#done-footer button.done").click()
    page.wait_for_url("**/done/**")

    cards = page.locator(".summary-card")
    expect(cards).to_have_count(2)
    son_card = page.locator('.summary-card[data-profile-kind="youth"]')
    expect(son_card).to_have_count(1)
    # The body stays neutral: a page-level scope would put the parent in the son's type too.
    assert page.locator("body[data-profile-kind]").count() == 0

    son_line = _px(
        page.eval_on_selector(
            '.summary-card[data-profile-kind="youth"] [data-role="rows"]', "el => getComputedStyle(el).fontSize"
        )
    )
    parent_line = _px(
        page.eval_on_selector(
            '.summary-card:not([data-profile-kind]) [data-role="rows"]', "el => getComputedStyle(el).fontSize"
        )
    )
    assert son_line > parent_line, f"the son reads at {son_line}px, the parent at {parent_line}px"
    expect(son_card.locator('[data-role="rows"]')).to_contain_text("things done")
