"""PRP-07 acceptance tests 33-36: the check-in screen and Today's card, at 390x844.

Every test here runs against its **own** server over its **own** database, for the reason
``test_setup.py`` states in its own docstring: saving a battery re-derives this profile's
challenges and rewrites the ``rows_json`` of every planned session a challenge row lands in, so
sharing the session-scoped server would leave the Today and History suites standing on a plan
this file had re-planned underneath them.

VitalForge is mocked, the same way the shared fixture mocks it.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from cadence.bilan.assessments import RETEST_DAYS

REPO = Path(__file__).resolve().parents[2]
BOOT_TIMEOUT_S = 40
POLL_S = 0.2
#: The floor every control on this screen holds itself to, one-handed at 390 px.
MIN_TAP_PX = 56

#: Principles section 7.1. None of these may appear on the son's form (section 3.5, D-027).
ADULT_NAMES: tuple[str, ...] = (
    "Push-up max",
    "Dead hang",
    "Plank",
    "Wall angel reach",
    "Goblet squat quality",
    "Farmer carry",
)
#: The two self-rated tests of section 7.2, with the number of steps their rubric has.
SELF_RATED: tuple[tuple[str, int], ...] = (("wall_angel_reach", 4), ("goblet_squat_quality", 5))

#: A battery at or above every adult ``ok`` tier, so it produces no gap and no challenge - and
#: therefore rewrites no planned session. Test 35 is about the card, not about the weaving.
CLEAN_BATTERY: list[dict[str, object]] = [
    {"test_id": "push_up_max", "value": 30},
    {"test_id": "dead_hang_s", "unavailable": True},
    {"test_id": "plank_s", "value": 120},
    {"test_id": "wall_angel_reach", "value": 2},
    {"test_id": "goblet_squat_quality", "value": 3},
    {"test_id": "farmer_carry_s", "value": 60},
]


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture(scope="module")
def assess_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A seeded database that has not been set up, so the gate is exercised on the way in."""
    path = tmp_path_factory.mktemp("assess") / "cadence.db"
    env = {**os.environ, "CADENCE_DB_PATH": str(path), "CADENCE_SEED_SETUP_COMPLETE": "0"}
    result = subprocess.run(
        [sys.executable, "-m", "cadence.bibliotheque.seed", "--fresh"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"seeding the check-in database failed:\n{result.stdout}\n{result.stderr}")
    return path


@pytest.fixture
def assess_app(assess_template: Path, tmp_path: Path) -> Iterator[str]:
    """A private copy of that database, with a server of its own in front of it."""
    target = tmp_path / "cadence.db"
    for source in assess_template.parent.glob("cadence.db*"):
        shutil.copy(source, tmp_path / source.name)

    port = _free_port()
    env = {**os.environ, "CADENCE_DB_PATH": str(target), "CADENCE_VITALFORGE_MODE": "mock"}
    log = (tmp_path / "uvicorn.log").open("w")
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "cadence.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=REPO,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
    )
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + BOOT_TIMEOUT_S
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail(f"the app exited before it listened:\n{(tmp_path / 'uvicorn.log').read_text()}")
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


def _complete_setup(page: Page, base: str, age: str = "12") -> None:
    page.goto(f"{base}/setup")
    page.fill("#son_age", age)
    page.click(".save")
    page.wait_for_url(re.compile(r"/today"))


def _test_names(page: Page) -> list[str]:
    """The six display names, and nothing else on the page.

    Read off ``data-test-name`` rather than the whole document on purpose: the son's phrasing for
    the press-up is "Press up like a plank on the move", so a page-wide search for "Plank" would
    fail on entirely correct output.
    """
    return [text.strip() for text in page.locator("[data-test-name]").all_inner_texts()]


# ------------------------------------------------------ 33: the form renders six tests


def test_assess_form_renders_six_tests(page: Page, assess_app: str) -> None:
    """33. Six blocks, and a segmented control on each of the two self-rated tests."""
    _complete_setup(page, assess_app)
    page.goto(f"{assess_app}/assess?profile=me")

    expect(page.locator("[data-test-block]")).to_have_count(6)
    for test_id, steps in SELF_RATED:
        block = page.locator(f'[data-test-block="{test_id}"]')
        expect(block.locator(".segmented")).to_have_count(1)
        expect(block.locator(".seg")).to_have_count(steps)
        # Radios, so the control still works with JavaScript off.
        expect(block.locator('.seg input[type="radio"]')).to_have_count(steps)
        assert block.locator("[data-stepper]").count() == 0
        box = block.locator(".seg").first.bounding_box()
        assert box is not None and box["height"] >= MIN_TAP_PX, box
        # The rubric of section 7.2, under the control the wireframe puts it under.
        expect(block.locator(".rubric li")).to_have_count(steps)

    # The four measured tests are steppers, and the dead hang is one of them only when it can be
    # run - see test 34. Every other measured block carries one.
    assert page.locator("[data-stepper]").count() >= 3
    for selector in (".astep", ".avalue", ".asave"):
        box = page.locator(selector).first.bounding_box()
        assert box is not None and box["height"] >= MIN_TAP_PX, f"{selector} is {box}"

    save = page.locator(".asave").bounding_box()
    form = page.locator("#assess-form").bounding_box()
    assert save is not None and form is not None
    assert save["width"] >= form["width"] - 1, "the primary button does not span the form"
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

    # Bad input never costs the user the page: the rejected form comes back with what was typed,
    # and it comes back at all, which needs the `#assess-form` opt-in in `app.js` (HTMX drops a
    # 4xx body by default, so without it Save would appear to do nothing).
    page.fill('[data-test-block="plank_s"] .avalue', "95")
    page.fill('[data-test-block="farmer_carry_s"] .avalue', "")
    page.click(".asave")
    expect(page.locator("[data-form-error]")).to_be_visible(timeout=10000)
    expect(page.locator('[data-test-block="plank_s"] .avalue')).to_have_value("95")


# ------------------------------------------------- 34: no anchor, no dead-hang stepper


def test_dead_hang_unavailable_without_anchor(page: Page, assess_app: str) -> None:
    """34. D-019: a test that cannot be run offers the unavailable control and no number."""
    _complete_setup(page, assess_app)
    stored = httpx.get(f"{assess_app}/api/profiles/me", timeout=5).json()["data"]
    assert stored["has_overhead_anchor"] is False, "the block under test needs the anchor switched off"

    page.goto(f"{assess_app}/assess?profile=me")
    block = page.locator('[data-test-block="dead_hang_s"]')
    expect(block).to_be_visible()
    expect(block.locator('[data-unavailable] input[type="checkbox"]')).to_be_checked()
    assert block.locator("[data-stepper]").count() == 0, "an unrunnable test must not offer a number"
    assert block.locator(".avalue").count() == 0

    # And it saves as unavailable rather than as a zero, which would rank as the worst gap there is.
    page.click(".asave")
    expect(page.locator('[data-role="assess-result"]')).to_be_visible(timeout=10000)
    latest = httpx.get(f"{assess_app}/api/assessments?profile=me", timeout=10).json()["data"]["latest"]
    hang = next(row for row in latest if row["test_id"] == "dead_hang_s")
    assert hang["value"] is None and hang["unit"] == "unavailable", hang


# --------------------------------------------- 35: the card on Today, until a baseline


def test_assessment_card_on_today(page: Page, assess_app: str) -> None:
    """35. The card is there before the baseline and gone once one is recorded."""
    _complete_setup(page, assess_app)

    page.goto(f"{assess_app}/today?profile=me")
    card = page.locator('[data-role="assessment-card"]')
    expect(card).to_have_count(1)
    expect(card.locator('a[href="/assess?profile=me"]')).to_be_visible()
    expect(card.locator('form[action="/assess/skip?profile=me"]')).to_have_count(1)

    body = {"profile": "me", "results": CLEAN_BATTERY}
    saved = httpx.post(f"{assess_app}/api/assessments", json=body, timeout=15)
    assert saved.status_code == 200, saved.text
    assert saved.json()["data"]["challenges"] == [], "this battery is at tier, so it earns no challenge"

    page.goto(f"{assess_app}/today?profile=me")
    expect(page.locator('[data-role="assessment-card"]')).to_have_count(0)
    # Not a blank page passing vacuously: the checklist is still there underneath.
    assert page.locator(".row-name").count() > 0


def _queued_assessment_days(db_path: Path, profile_id: str = "me") -> int:
    connection = sqlite3.connect(db_path)
    try:
        return connection.execute(
            "SELECT count(*) FROM planned_session"
            " WHERE profile_id = ? AND status = 'planned' AND day_type = 'assessment'",
            (profile_id,),
        ).fetchone()[0]
    finally:
        connection.close()


def test_the_retest_card_returns_four_weeks_after_a_baseline(page: Page, assess_app: str, tmp_path: Path) -> None:
    """D-226. The four-weekly retest reminder, which nothing used to bring back.

    Section 7 asks for a retest every four weeks. ``card_due`` shows the card only while an
    assessment day sits at the head of the queue - deliberately, so the reminder never renders
    over a training session - and once the seeded block's own assessment day had been consumed,
    nothing ever queued another. The reminder arrived once, at first run, and never again.

    Backdating the baseline by 28 days is the same thing as waiting 28 days, and does not need the
    clock moved underneath a server that is already running.
    """
    _complete_setup(page, assess_app)
    stale = (date.today() - timedelta(days=RETEST_DAYS)).isoformat()
    saved = httpx.post(
        f"{assess_app}/api/assessments",
        json={"profile": "me", "recorded_on": stale, "results": CLEAN_BATTERY},
        timeout=15,
    )
    assert saved.status_code == 200, saved.text
    assert _queued_assessment_days(tmp_path / "cadence.db") == 0, "the baseline should consume the queued day"

    page.goto(f"{assess_app}/today?profile=me")
    expect(page.locator('[data-role="assessment-card"]')).to_have_count(1)
    assert _queued_assessment_days(tmp_path / "cadence.db") == 1
    # The card and the checklist under it have to be the same day (D-218b).
    expect(page.locator('[data-role="assessment-card"] a[href="/assess?profile=me"]')).to_be_visible()

    # A second render must not queue a second retest: the row's id is derived from the due date.
    page.goto(f"{assess_app}/today?profile=me")
    expect(page.locator('[data-role="assessment-card"]')).to_have_count(1)
    assert _queued_assessment_days(tmp_path / "cadence.db") == 1, "a second render queued a second retest"


# --------------------------------------------------- 36: the son's form speaks section 7.4


def test_youth_assess_uses_play_phrasing(page: Page, assess_app: str) -> None:
    """36. The play strings of section 7.4, verbatim, and not one adult test name."""
    _complete_setup(page, assess_app, "12")
    page.goto(f"{assess_app}/assess?profile=son")

    names = _test_names(page)
    assert len(names) == 6, names
    assert "Hang like a monkey" in names
    for adult in ADULT_NAMES:
        assert adult not in names, f"{adult!r} is an adult test name and it is on the son's form"

    # Section 7.4's cap is the protocol, so the screen says where the count stops.
    expect(page.locator('[data-test-block="push_up_max"] [data-cap]')).to_contain_text("Stops at 20")
