"""The page body must never scroll sideways, whatever fonts the device ships (CI found a 22px
overflow on Together history under Linux fallback fonts). Widening every glyph reproduces that."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

from tests.e2e.conftest import PHONE

PAGES = ("/today?profile=me", "/today?profile=together", "/history?profile=me", "/history?profile=together")


@pytest.mark.parametrize("url", PAGES)
def test_no_sideways_scroll_even_with_wide_glyphs(page: Page, history_seed: dict[str, str], url: str) -> None:
    page.set_viewport_size(PHONE)
    page.goto(url)
    page.add_style_tag(content="* { letter-spacing: 0.08em !important; }")
    page.wait_for_timeout(150)
    overflow = page.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
    assert overflow <= 1, f"{url} scrolls {overflow}px sideways"
