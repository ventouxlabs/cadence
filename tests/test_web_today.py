"""The HTML surface: Today, its HTMX partials, the Done screen and the PWA entry points.

The Playwright suite drives the same routes in a browser; these keep them covered by ``make test``,
which never runs ``tests/e2e``.
"""

from __future__ import annotations

import re

import httpx
import pytest

HX = {"HX-Request": "true"}


async def _today(client: httpx.AsyncClient, profile: str = "me") -> dict:
    response = await client.get(f"/api/today?profile={profile}")
    assert response.status_code == 200
    return response.json()["data"]


async def test_today_page_renders_rows_and_the_done_button(seeded_client: httpx.AsyncClient) -> None:
    response = await seeded_client.get("/today?profile=me")
    assert response.status_code == 200
    assert 'class="row' in response.text
    assert 'id="done-footer"' in response.text
    assert 'type="checkbox"' in response.text
    assert "htmx.min.js" in response.text
    assert "http://" not in response.text.replace("http://test", "")


async def test_today_page_makes_no_external_request(seeded_client: httpx.AsyncClient) -> None:
    for path in ("/today?profile=me", "/today?profile=son", "/today?profile=together"):
        text = (await seeded_client.get(path)).text
        assert "https://" not in text
        assert "//cdn" not in text


async def test_tick_route_returns_the_row_and_the_footer(seeded_client: httpx.AsyncClient) -> None:
    sid = (await _today(seeded_client))["session_id"]
    response = await seeded_client.post(f"/today/{sid}/rows/1/tick?profile=me", data={"done": "1"}, headers=HX)

    assert response.status_code == 200
    assert 'aria-checked="true"' in response.text
    assert 'hx-swap-oob="true"' in response.text


async def test_tick_route_without_javascript_redirects(seeded_client: httpx.AsyncClient) -> None:
    sid = (await _today(seeded_client))["session_id"]
    response = await seeded_client.post(f"/today/{sid}/rows/1/tick?profile=me", data={"done": "1"})
    assert response.status_code == 303
    assert response.headers["location"] == "/today?profile=me"


async def test_adjust_route_expands_then_steps(seeded_client: httpx.AsyncClient) -> None:
    data = await _today(seeded_client)
    sid = data["session_id"]
    target = next(row for row in data["rows"] if row["reps"] is not None and row["adjustable"])
    position = target["position"]

    opened = await seeded_client.post(
        f"/today/{sid}/rows/{position}/adjust?profile=me", data={"expand": "1"}, headers=HX
    )
    stepped = await seeded_client.post(
        f"/today/{sid}/rows/{position}/adjust?profile=me", data={"reps": "1"}, headers=HX
    )

    assert "stepper-panel" in opened.text
    assert f">{target['reps'] + 1}<" in stepped.text


async def test_felt_route_records_the_answer(seeded_client: httpx.AsyncClient) -> None:
    sid = (await _today(seeded_client))["session_id"]
    response = await seeded_client.post(f"/today/{sid}/felt?profile=me", data={"felt": "easy"}, headers=HX)
    assert 'aria-checked="true"' in response.text
    assert (await _today(seeded_client))["felt"] == "easy"


async def test_done_route_asks_once_then_finishes(seeded_client: httpx.AsyncClient) -> None:
    sid = (await _today(seeded_client))["session_id"]

    first = await seeded_client.post(f"/today/{sid}/done?profile=me", data={"confirm": "0"}, headers=HX)
    assert "How did" in first.text

    # HTMX follows a 303 itself and swaps the result in, so the page would never move: the
    # partial asks for a real navigation with HX-Redirect instead.
    second = await seeded_client.post(f"/today/{sid}/done?profile=me", data={"confirm": "1"}, headers=HX)
    assert second.status_code == 204
    assert second.headers["hx-redirect"].startswith(f"/done/{sid}")


async def test_done_route_without_javascript_uses_the_confirm_query(seeded_client: httpx.AsyncClient) -> None:
    sid = (await _today(seeded_client))["session_id"]
    response = await seeded_client.post(f"/today/{sid}/done?profile=me", data={"confirm": "0"})
    assert response.headers["location"] == "/today?profile=me&confirm=1"
    assert "How did" in (await seeded_client.get("/today?profile=me&confirm=1")).text


async def test_done_page_shows_three_lines(seeded_client: httpx.AsyncClient) -> None:
    data = await _today(seeded_client)
    sid = data["session_id"]
    await seeded_client.post(f"/api/sessions/{sid}/rows/1", json={"done": True})
    await seeded_client.post(f"/api/sessions/{sid}/done", json={})

    page = await seeded_client.get(f"/done/{sid}?profile=me")

    assert "minute" in page.text
    assert f"1 of {len(data['rows'])} exercises" in page.text
    assert "Next time" in page.text
    assert "Stored locally." in page.text


async def test_solo_render_detaches_a_together_session(seeded_client: httpx.AsyncClient) -> None:
    """D-075: the tab you are on decides whether one Done finishes one session or two."""
    together = await _today(seeded_client, "together")
    assert together["together_group_id"]

    solo = await _today(seeded_client, "me")
    assert solo["session_id"] == together["session_id"]
    assert solo["together_group_id"] is None

    body = (await seeded_client.post(f"/api/sessions/{solo['session_id']}/done", json={})).json()
    assert "group" not in body["data"]
    partner = (await _today(seeded_client, "son"))["session_id"]
    assert partner == together["sessions"][1]["session_id"]


async def test_done_route_on_a_finished_session_redirects(seeded_client: httpx.AsyncClient) -> None:
    sid = (await _today(seeded_client))["session_id"]
    await seeded_client.post(f"/api/sessions/{sid}/done", json={})
    response = await seeded_client.post(f"/today/{sid}/done?profile=me", data={"confirm": "1"})
    assert response.status_code == 303
    assert response.headers["location"].startswith(f"/done/{sid}")


@pytest.mark.parametrize(
    "path",
    ["/today/nope/rows/1/tick", "/today/nope/rows/1/adjust", "/today/nope/felt", "/today/nope/done"],
)
async def test_write_routes_404_on_an_unknown_session(seeded_client: httpx.AsyncClient, path: str) -> None:
    response = await seeded_client.post(f"{path}?profile=me", data={})
    assert response.status_code == 404
    assert "Traceback" not in response.text


async def test_unknown_today_profile_renders_a_page_not_a_traceback(seeded_client: httpx.AsyncClient) -> None:
    response = await seeded_client.get("/today?profile=cat")
    assert response.status_code == 404
    assert "Traceback" not in response.text
    assert "Not today" in response.text


async def test_done_page_for_an_unknown_session(seeded_client: httpx.AsyncClient) -> None:
    response = await seeded_client.get("/done/nope")
    assert response.status_code == 404


# ------------------------------------------------------------------------------- the PWA


async def test_root_redirects_to_today_once_seeded(seeded_client: httpx.AsyncClient) -> None:
    response = await seeded_client.get("/")
    assert response.status_code == 303
    assert response.headers["location"] == "/today?profile=me"


async def test_root_sends_an_unset_up_install_to_setup(client: httpx.AsyncClient) -> None:
    """PRP-03 replaces D-072's interim page: the front door gates on ``setup_complete`` again.

    Before, an unseeded install got a page naming ``make seed`` because ``/setup`` did not exist
    yet. It does now, and it is the screen that asks for the son's age - the one question the app
    cannot be used correctly without.
    """
    response = await client.get("/")
    assert response.status_code == 303
    assert response.headers["location"] == "/setup"


async def test_service_worker_is_served_from_the_root(seeded_client: httpx.AsyncClient) -> None:
    response = await seeded_client.get("/sw.js")
    assert response.status_code == 200
    assert response.headers["service-worker-allowed"] == "/"
    assert response.headers["content-type"].startswith("application/javascript")
    assert "CACHE_VERSION" in response.text


async def test_manifest_declares_both_icon_sizes(seeded_client: httpx.AsyncClient) -> None:
    body = (await seeded_client.get("/static/manifest.json")).json()
    assert body["name"] == "Cadence"
    assert body["display"] == "standalone"
    assert body["theme_color"]
    sizes = {icon["sizes"] for icon in body["icons"]}
    assert {"192x192", "512x512"} <= sizes
    assert (await seeded_client.get("/manifest.json")).status_code == 200


async def test_htmx_is_vendored_with_a_pinned_version(seeded_client: httpx.AsyncClient) -> None:
    body = (await seeded_client.get("/static/htmx.min.js")).text
    assert body.startswith("/* htmx 2.0.")
    assert "unpkg.com/htmx.org@2.0." in body.split("*/")[0]


async def test_icons_are_real_pngs(seeded_client: httpx.AsyncClient) -> None:
    for name in ("icon-192.png", "icon-512.png"):
        response = await seeded_client.get(f"/static/icons/{name}")
        assert response.status_code == 200
        assert response.content[:8] == b"\x89PNG\r\n\x1a\n"


BODY_IMAGE_PHRASES = ("body fat", "body-fat", "muscle %", "lean mass", "shirtless", "physique", "appearance")

# Standalone words only: "bodyweight squat" is the name of a movement, not a body-image metric,
# and a lookbehind is what keeps the two apart (D-027).
BARE_WORDS = tuple(re.compile(rf"(?<![a-z]){word}\b") for word in ("weight", "fat", "lean", "abs"))


async def test_youth_page_never_shows_body_image_words(seeded_client: httpx.AsyncClient) -> None:
    """D-027: banned phrases over the whole page, bare words over the metric-ish elements only."""
    text = (await seeded_client.get("/today?profile=son")).text.lower()
    for banned in BODY_IMAGE_PHRASES:
        assert banned not in text

    scoped = re.findall(r'class="(?:row-note|row-notice|session-sub|notice)"[^>]*>([^<]*)<', text)
    assert scoped, "no metric-ish elements on the page: the check would pass vacuously"
    for chunk in scoped:
        for pattern in BARE_WORDS:
            assert not pattern.search(chunk), f"{pattern.pattern!r} in {chunk!r}"


async def test_youth_notes_never_use_the_bare_word_weight(seeded_client: httpx.AsyncClient) -> None:
    """The clamp line a loaded youth row always carries (D-063) is the riskiest for D-027."""
    checked = 0
    for _ in range(8):
        data = (await seeded_client.get("/api/today?profile=son")).json()["data"]
        for row in data["rows"]:
            for note in row["notes"]:
                checked += 1
                for pattern in BARE_WORDS:
                    assert not pattern.search(note.lower()), note
        await seeded_client.post(f"/api/sessions/{data['session_id']}/done", json={})
    assert checked, "the son's block carried no notes: the check would pass vacuously"
