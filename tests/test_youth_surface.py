"""What the son's screens are allowed to say, and what his Done button is allowed to do.

Two rules meet here. D-027 bans body-image language from anything a youth profile can read, and
principles section 3.7 P6 says the Done control is *always* tappable - the band threshold
promotes it, it never gates it. Both are easy to break by accident and neither shows up as a
failure anywhere else, so they are asserted over the rendered DOM rather than over the helpers
that feed it.

The banned vocabulary is imported from ``tests.test_web_today`` rather than restated: two copies
of a word list drift, and the copy that drifts is the one that stops catching anything.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from tests.test_web_today import BARE_WORDS, BODY_IMAGE_PHRASES

# The elements D-027 scopes the bare words to: metrics and challenge names, not cues.
METRIC_ELEMENTS = re.compile(
    r'class="(?:row-note|row-notice|session-sub|notice|row-line|summary-line|step-value)"[^>]*>([^<]*)<'
)


async def _son(client: httpx.AsyncClient) -> dict[str, Any]:
    response = await client.get("/api/today?profile=son")
    assert response.status_code == 200
    return response.json()["data"]


def _assert_clean(html: str, where: str) -> None:
    """No banned phrase anywhere, and no bare metric word in a metric element."""
    text = html.lower()
    for phrase in BODY_IMAGE_PHRASES:
        assert phrase not in text, f"{phrase!r} on {where}"

    scoped = METRIC_ELEMENTS.findall(text)
    assert scoped, f"no metric elements on {where}: the check would pass vacuously"
    for chunk in scoped:
        for pattern in BARE_WORDS:
            assert not pattern.search(chunk), f"{pattern.pattern!r} in {chunk!r} on {where}"


# --------------------------------------------------------------------------- D-027 on the DOM


async def test_the_sons_today_carries_no_banned_language(seeded_client: httpx.AsyncClient) -> None:
    """Every day of the son's block, not just whichever one happens to be first.

    The riskiest string is the youth clamp note, which D-063 puts on every loaded row, and a
    block that never reaches a loaded day would pass this vacuously.
    """
    days = 0
    for _ in range(8):
        page = await seeded_client.get("/today?profile=son")
        assert page.status_code == 200
        _assert_clean(page.text, f"the son's Today, day {days}")
        days += 1
        data = await _son(seeded_client)
        await seeded_client.post(f"/api/sessions/{data['session_id']}/done", json={})
    assert days == 8


async def test_the_sons_done_screen_carries_no_banned_language(seeded_client: httpx.AsyncClient) -> None:
    """The summary is a youth screen too, and nothing was checking it.

    It renders ``next_time_note``, which PRP-07 replaces with generated autoregulation text -
    so this is the assertion that catches the day a real prescription starts naming loads on the
    child's screen.
    """
    checked = 0
    for _ in range(6):
        data = await _son(seeded_client)
        session_id = data["session_id"]
        for row in data["rows"][:3]:
            await seeded_client.post(f"/api/sessions/{session_id}/rows/{row['position']}", json={"done": True})
        await seeded_client.post(f"/api/sessions/{session_id}/done", json={"felt": "hard"})

        page = await seeded_client.get(f"/done/{session_id}?profile=son")
        assert page.status_code == 200
        _assert_clean(page.text, f"the son's Done screen ({session_id})")
        checked += 1
    assert checked == 6


async def test_the_together_page_is_clean_for_the_son_too(seeded_client: httpx.AsyncClient) -> None:
    """Together puts the parent's rows on the same screen the son is reading."""
    page = await seeded_client.get("/today?profile=together")
    assert page.status_code == 200
    _assert_clean(page.text, "the Together page")


async def test_the_clamp_note_is_the_one_that_matters(aged_son_client: httpx.AsyncClient) -> None:
    """An aged son gets real loads, so his rows carry the clamp note on every loaded one.

    D-077 reworded that string from "weight" to "load" at the source. This is the regression
    test for the rewording surviving on the page rather than only in the helper.
    """
    notes = 0
    for _ in range(6):
        data = (await aged_son_client.get("/api/today?profile=son")).json()["data"]
        page = await aged_son_client.get("/today?profile=son")
        _assert_clean(page.text, "an aged son's Today")
        notes += sum(len(row["notes"]) for row in data["rows"])
        await aged_son_client.post(f"/api/sessions/{data['session_id']}/done", json={})
    assert notes, "no notes on the aged son's block: the check would pass vacuously"


# ------------------------------------------------------- P6: the Done control never disappears


async def test_the_done_control_is_on_the_sons_first_paint(seeded_client: httpx.AsyncClient) -> None:
    """Zero ticks, and the button is already there and says ``Done``.

    Risk 10: reading section 3.7 as "the exit appears after N exercises" is natural and wrong.
    A child who wants to stop after one movement must be able to.
    """
    data = await _son(seeded_client)
    assert data["good_enough_after"] >= 3
    assert all(row["done"] is False for row in data["rows"])

    page = (await seeded_client.get("/today?profile=son")).text

    assert 'class="done' in page
    assert "Good enough" not in page, "promoted before a single tick"
    assert "done-promoted" not in page


async def test_the_done_control_promotes_at_the_band_threshold(seeded_client: httpx.AsyncClient) -> None:
    """One tick below the threshold is still a plain Done; the threshold tick promotes it."""
    data = await _son(seeded_client)
    session_id = data["session_id"]
    threshold = data["good_enough_after"]

    for row in data["rows"][: threshold - 1]:
        await seeded_client.post(f"/api/sessions/{session_id}/rows/{row['position']}", json={"done": True})
    below = (await seeded_client.get("/today?profile=son")).text
    await seeded_client.post(
        f"/api/sessions/{session_id}/rows/{data['rows'][threshold - 1]['position']}", json={"done": True}
    )
    at = (await seeded_client.get("/today?profile=son")).text

    assert "Good enough" not in below, f"promoted at {threshold - 1} of {threshold} ticks"
    assert 'class="done' in below, "the plain Done vanished below the threshold"
    assert "Good enough" in at
    assert "done-promoted" in at


async def test_the_parents_done_never_says_good_enough(seeded_client: httpx.AsyncClient) -> None:
    """The adult threshold is 1, so the parent is "complete" after one tick - but the wording
    belongs to the youth exit and must not leak onto the grown-up's screen."""
    data = (await seeded_client.get("/api/today?profile=me")).json()["data"]
    assert data["good_enough_after"] == 1
    await seeded_client.post(f"/api/sessions/{data['session_id']}/rows/1", json={"done": True})

    page = (await seeded_client.get("/today?profile=me")).text

    assert "Good enough" not in page
    assert 'class="done' in page
