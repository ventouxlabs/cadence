"""Playwright end-to-end tests land in PRP-02 with the Today screen.

This placeholder keeps ``make e2e`` and the CI e2e job honest: they run, they report a skip,
and they exit 0 rather than failing on "no tests collected".
"""

from __future__ import annotations

import pytest


@pytest.mark.skip(reason="PRP-02 adds the Today screen and the first end-to-end tests")
def test_today_checklist_placeholder() -> None:  # pragma: no cover
    raise AssertionError("unreachable")
