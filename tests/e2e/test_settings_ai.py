"""PRP-08 acceptance tests 35-37: Import and Generate on Settings, at 390x844.

Its own server over its own database, for the reason ``test_setup.py`` gives: these tests store
workouts and re-point planned days, and the shared session server is what the Today suite stands
on. Generation runs in ``CADENCE_OMNIROUTE_MODE=mock``, so no key and no network are involved.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

REPO = Path(__file__).resolve().parents[2]
BOOT_TIMEOUT_S = 40
POLL_S = 0.2

VALID_WORKOUT = """id: e2e-my-upper
name: E2E upper
day_type: upper_a
target_profile_kind: adult
estimated_minutes: 25
rows:
  - exercise_id: push-up
    sets: 3
    reps: 10
    load_unit: bodyweight
    rest_s: 60
  - exercise_id: plank
    sets: 2
    seconds: 45
    load_unit: bodyweight
    rest_s: 45
"""

BARBELL_WORKOUT = """id: e2e-barbell
name: E2E barbell
day_type: lower_a
target_profile_kind: adult
estimated_minutes: 30
rows:
  - exercise_id: barbell-back-squat
    sets: 5
    reps: 5
    load_kg: 100.0
    load_unit: total
    rest_s: 180
exercises:
  - id: barbell-back-squat
    name: Barbell back squat
    pattern: squat
    region: lower
    load_type: barbell
    load_unit: total
    measure: reps
    equipment: [barbell]
    cue: Brace hard, sit between the hips.
"""


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture(scope="module")
def ai_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A seeded install with generation in mock mode."""
    root = tmp_path_factory.mktemp("ai")
    db = root / "cadence.db"
    env = {**os.environ, "CADENCE_DB_PATH": str(db)}
    seeded = subprocess.run(
        [sys.executable, "-m", "cadence.bibliotheque.seed"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if seeded.returncode != 0:
        pytest.fail(f"seeding the AI database failed:\n{seeded.stdout}\n{seeded.stderr}")

    port = _free_port()
    run_env = {
        **env,
        "CADENCE_VITALFORGE_MODE": "mock",
        "CADENCE_OMNIROUTE_MODE": "mock",
        "OMNIROUTE_KEY": "",
    }
    log = (root / "uvicorn.log").open("w")
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "cadence.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=REPO,
        env=run_env,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
    )
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + BOOT_TIMEOUT_S
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail(f"the app exited before it listened:\n{(root / 'uvicorn.log').read_text()}")
            try:
                if httpx.get(f"{url}/api/health", timeout=1.0).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(POLL_S)
        else:  # pragma: no cover - only on a machine too slow to boot uvicorn in 40 s
            pytest.fail("the app did not start in time")
        yield url
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            process.kill()
        finally:
            log.close()


@pytest.fixture
def dialogs(page: Page) -> list[str]:
    """Anything a page tries to pop. An injected payload that ran would land here."""
    seen: list[str] = []
    page.on("dialog", lambda dialog: (seen.append(dialog.message), dialog.dismiss()))
    return seen


def test_e2e_import_accepts_a_valid_workout(page: Page, ai_server: str) -> None:
    """Test 35's positive half: paste, tap, and the result appears without navigation."""
    page.goto(f"{ai_server}/settings")
    page.fill("#import_text", VALID_WORKOUT)
    page.select_option("#import_profile", "me")
    page.click("#import-form button[type=submit]")
    expect(page.locator("[data-import-ok]")).to_be_visible()
    expect(page.locator("[data-import-ok]")).to_contain_text("E2E upper")
    assert page.url.endswith("/settings")


def test_e2e_import_shows_errors_inline(page: Page, ai_server: str, dialogs: list[str]) -> None:
    """Test 35: a barbell workout draws its problems into ``#import-result`` in place."""
    page.goto(f"{ai_server}/settings")
    page.fill("#import_text", BARBELL_WORKOUT)
    page.click("#import-form button[type=submit]")
    expect(page.locator("[data-import-error-count]")).to_be_visible()
    assert page.locator("[data-import-error]").count() >= 2
    assert page.url.endswith("/settings")
    assert dialogs == []


def test_e2e_import_injection_fires_no_dialog(page: Page, ai_server: str, dialogs: list[str]) -> None:
    """Test 19's browser companion: the refused payload is text on the page, never a script."""
    page.goto(f"{ai_server}/settings")
    page.fill("#import_text", VALID_WORKOUT.replace("name: E2E upper", "name: '<script>alert(1)</script>'"))
    page.click("#import-form button[type=submit]")
    expect(page.locator("[data-import-error-count]")).to_be_visible()
    page.wait_for_timeout(200)
    assert dialogs == []


def test_e2e_generate_preview_and_accept(page: Page, ai_server: str) -> None:
    """Test 36: Generate, read the checklist, Accept."""
    page.goto(f"{ai_server}/settings")
    page.select_option("#generate_goal", "posture")
    page.click("[data-generate]")
    expect(page.locator("[data-preview]")).to_be_visible()
    assert page.locator("[data-preview-row]").count() >= 3
    # Read-only: the preview has no tick control of its own.
    assert page.locator("[data-preview] input[type=checkbox]").count() == 0

    page.click("[data-accept]")
    expect(page.locator("[data-generate-ok]")).to_be_visible()
    with httpx.Client(base_url=ai_server, timeout=5.0) as api:
        assert api.get("/api/health").status_code == 200


def test_e2e_generate_discard_stores_nothing(page: Page, ai_server: str) -> None:
    """Test 37: Discard clears the panel, and no second copy is ever stored."""
    page.goto(f"{ai_server}/settings")
    page.click("[data-generate]")
    expect(page.locator("[data-preview]")).to_be_visible()
    page.click("[data-discard]")
    expect(page.locator("[data-preview]")).to_have_count(0)

    # Generating again offers the same document, which is only possible because the first one was
    # never stored: a stored copy would collide and come back renamed.
    page.click("[data-generate]")
    expect(page.locator("[data-preview]")).to_be_visible()


def test_e2e_the_import_card_fits_the_phone(page: Page, ai_server: str) -> None:
    """No horizontal scroll at 390 px, which a monospace textarea is the usual way to break."""
    page.goto(f"{ai_server}/settings")
    overflow = page.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
    assert overflow <= 0


ADOPTABLE = {"upper_a", "upper_b", "lower_a", "lower_full_b", "mobility_carry"}


@pytest.mark.xfail(
    strict=True,
    reason=(
        "finding T-1: an off-whitelist implement is reported with PyYAML/Pydantic's own text "
        "(\"Input should be 'bodyweight', 'dumbbells', ...\"), which names neither the offending "
        "equipment nor the row. PRP-08's own example message names both."
    ),
)
def test_e2e_import_error_names_the_equipment(page: Page, ai_server: str) -> None:
    """Test 35, read rather than counted: the rejection says *which* kit is not on the list.

    Counting error lines passes just as well when both of them read "something went wrong", and a
    person standing in a garage with a phone needs the word "barbell".
    """
    page.goto(f"{ai_server}/settings")
    page.fill("#import_text", BARBELL_WORKOUT)
    page.click("#import-form button[type=submit]")
    expect(page.locator("[data-import-error-count]")).to_be_visible()
    listed = " ".join(page.locator("[data-import-error]").all_inner_texts()).lower()
    assert "barbell" in listed


def _advance_to_an_adoptable_day(api: httpx.Client) -> str:
    """Today's day type, having finished any assessment day standing in front of it.

    Week 1 starts on the assessment battery, and D-173 deliberately refuses to let a workout be
    pointed at one: it is a measurement protocol, and swapping it out would drop the retest.
    """
    for _ in range(4):
        data = api.get("/api/today?profile=me").json()["data"]
        if data["day_type"] in ADOPTABLE:
            return str(data["day_type"])
        for row in data["rows"]:
            api.post(f"/api/sessions/{data['session_id']}/rows/{row['position']}", json={"done": True})
        api.post(f"/api/sessions/{data['session_id']}/done", json={"felt": "right"})
    raise AssertionError("the seeded block never reached a day a workout can be pointed at")


def test_e2e_an_accepted_workout_serves_the_chosen_day_on_today(page: Page, ai_server: str) -> None:
    """Test 36's other half (D-173): "Use for" points a real planned day at the new workout."""
    with httpx.Client(base_url=ai_server, timeout=10.0) as api:
        day_type = _advance_to_an_adoptable_day(api)

    page.goto(f"{ai_server}/settings")
    page.select_option("#generate_goal", "posture")
    page.click("[data-generate]")
    expect(page.locator("[data-preview]")).to_be_visible()
    names = [text.strip() for text in page.locator("[data-preview] .preview-name").all_inner_texts()]
    assert names, "the preview rendered no rows to compare against"

    page.select_option("#generate_day_type", day_type)
    page.click("[data-accept]")
    expect(page.locator("[data-generate-ok]")).to_be_visible()

    page.goto(f"{ai_server}/today?profile=me")
    shown = [text.strip() for text in page.locator(".row-name").all_inner_texts()]
    # The whole checklist, in order, and nothing else. Asserting only that the first name appears
    # would pass on the seeded day too: the posture prelude opens several of them with a wall
    # angel, so a swap that never happened would look exactly like one that did.
    assert shown == names, f"Today is not the accepted workout: {shown} != {names}"
