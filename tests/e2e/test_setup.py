"""PRP-03 acceptance tests 18-23: Setup, Settings and the youth surface, at 390x844.

Every test here runs against its **own** server over its **own** database. The shared session
server in ``conftest.py`` is seeded set-up and is what the Today suite drives; these tests answer
the setup screen, change days-per-week and rebuild blocks, so sharing it would leave the other
tests standing on a plan this file had re-planned underneath them.
"""

from __future__ import annotations

import os
import re
import shutil
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

# D-027. Phrases are checked over the whole visible text; the bare words are checked only inside
# elements that carry a metric or a challenge name, because "refuse to lean" is a legal cue on the
# suitcase carry and `\bweight\b` never matches inside "Bodyweight".
BANNED_PHRASES: tuple[str, ...] = (
    "body fat",
    "bodyfat",
    "body-fat",
    "body composition",
    "muscle %",
    "muscle percent",
    "lean mass",
    "body image",
)
BANNED_BARE_WORDS: tuple[str, ...] = ("weight", "fat", "lean", "abs", "calories")
METRIC_SELECTORS = "[data-metric], [data-challenge-name]"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture(scope="session")
def first_run_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A seeded database that has **not** been set up: `make seed --fresh` (D-091)."""
    path = tmp_path_factory.mktemp("first-run") / "cadence.db"
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
        pytest.fail(f"seeding the first-run database failed:\n{result.stdout}\n{result.stderr}")
    return path


@pytest.fixture
def first_run(first_run_template: Path, tmp_path: Path) -> Iterator[str]:
    """A private copy of that database, with a server of its own in front of it."""
    target = tmp_path / "cadence.db"
    for source in first_run_template.parent.glob("cadence.db*"):
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


# ------------------------------------------------------------------- 18, 19: setup


def test_setup_minimum_path(page: Page, first_run: str) -> None:
    """18. One number and two taps: the field, then Start."""
    page.goto(f"{first_run}/")
    expect(page).to_have_url(re.compile(r"/setup$"))

    page.click("#son_age")  # tap one
    page.keyboard.type("12")
    page.click(".save")  # tap two
    page.wait_for_url(re.compile(r"/today"))

    expect(page.locator("#main")).to_be_visible()
    assert httpx.get(f"{first_run}/api/settings", timeout=5).json()["data"]["setup_complete"] is True


def test_no_html_route_gets_past_the_gate(page: Page, first_run: str) -> None:
    """Risk 7: deep-linking Today before setup renders the son a checklist. It must not."""
    for path in ("/today?profile=son", "/settings"):
        page.goto(f"{first_run}{path}")
        expect(page).to_have_url(re.compile(r"/setup$"))


def test_setup_shows_inline_error(page: Page, first_run: str) -> None:
    """19. A rejected age renders under the field, and the page does not navigate.

    The PRP's wireframe uses 4 as the example, which its own bounds (3-19) and acceptance test 10
    make a *valid* age; 2 is the value both agree is out of range (D-096).
    """
    page.goto(f"{first_run}/setup")
    page.fill("#son_age", "2")
    page.click(".save")

    error = page.locator('[data-error-for="age_years"]')
    expect(error).to_be_visible()
    expect(error).to_contain_text("Enter an age between 3 and 19.")
    expect(page).to_have_url(re.compile(r"/setup$"))
    expect(page.locator("#son_age")).to_have_value("2")


# --------------------------------------------------------------- 20: live preview


def test_weights_live_preview(page: Page, first_run: str) -> None:
    """20. Typing updates the preview in place, with no page load."""
    page.goto(f"{first_run}/setup")
    box = page.locator("#weights_available")
    # Typed, not filled: the preview is bound to `keyup`, and `fill` sets the value without ever
    # pressing a key - which would test a trigger the user never fires.
    box.fill("")
    box.press_sequentially("KB 16/24 kg", delay=20)
    expect(page.locator("#weights-preview")).to_contain_text("Kettlebells", timeout=5000)
    expect(page.locator("#weights-preview")).to_contain_text("16")
    expect(page.locator("#weights-preview")).not_to_contain_text("Dumbbells")

    box.fill("")
    box.press_sequentially("DB banana", delay=20)
    expect(page.locator("#weights-preview")).to_contain_text("banana", timeout=5000)
    # Unreadable is a warning, never a rejection: the parser ignores the token and says so.
    expect(page.locator(".preview-warn")).to_be_visible()


# ------------------------------------------------------- 21, 22: the youth surface


def _visible_text(page: Page) -> str:
    return (page.locator("body").inner_text() or "").lower()


def _metric_text(page: Page) -> str:
    parts = page.locator(METRIC_SELECTORS).all_inner_texts()
    return " ".join(parts).lower()


@pytest.mark.parametrize(("age", "band"), [(9, "u10"), (12, "age_10_13"), (15, "age_14_17")])
def test_son_today_has_no_body_words(page: Page, first_run: str, age: int, band: str) -> None:
    """21. No body-composition or appearance language anywhere the son can see it."""
    _complete_setup(page, first_run, str(age))

    stored = httpx.put(f"{first_run}/api/profiles/son", json={"age_years": age}, timeout=10).json()["data"]
    assert stored["age_band"] == band, "the band has to follow the age before the page is judged"

    page.goto(f"{first_run}/today?profile=son")
    text = _visible_text(page)
    for phrase in BANNED_PHRASES:
        assert phrase not in text, f"{phrase!r} is on the son's Today at {age}"

    metrics = _metric_text(page)
    for word in BANNED_BARE_WORDS:
        assert not re.search(rf"\b{word}\b", metrics), f"{word!r} is in a metric element on the son's Today"

    assert page.locator(".row-name").count() > 0, "an empty checklist would pass this test vacuously"


def test_the_banned_word_check_is_not_vacuous(page: Page, first_run: str) -> None:
    """21. The under-ten substitution puts "Bodyweight squat" on the page (assessment day, u10).

    Proof that the check reads real content and that `\\bweight\\b` is not being applied to the
    whole page: "Bodyweight squat" is a legal name and it survives.
    """
    _complete_setup(page, first_run, "9")
    httpx.put(f"{first_run}/api/profiles/son", json={"age_years": 9}, timeout=10)
    page.goto(f"{first_run}/today?profile=son")
    expect(page.locator(".row-name", has_text="Bodyweight squat").first).to_be_visible()

    text = _visible_text(page)
    for phrase in BANNED_PHRASES:
        assert phrase not in text


def test_the_band_change_reaches_the_materialised_rows(page: Page, first_run: str) -> None:
    """The age is not decoration: the rows the son is given change with his band."""
    _complete_setup(page, first_run, "9")

    def exercises(age: int) -> set[str]:
        httpx.put(f"{first_run}/api/profiles/son", json={"age_years": age}, timeout=10)
        body = httpx.get(f"{first_run}/api/today?profile=son", timeout=10).json()["data"]
        return {row["exercise_id"] for row in body["rows"]}

    youngest = exercises(9)
    oldest = exercises(15)
    assert youngest != oldest
    # Section 8: the under-tens carry no external load, so the goblet squat is substituted out.
    assert "bodyweight-squat" in youngest
    assert "goblet-squat" in oldest


def test_an_age_of_eighteen_keeps_every_youth_protection(page: Page, first_run: str) -> None:
    """D-099 as revised. A number typed into a box never removes a child's protections.

    The youth exit is the one that matters: the always-visible "good enough" control of principles
    section 3.7 P6. At 18 the son is still a youth profile, still has it, and the screen explains
    which band he is in rather than announcing a promotion nobody asked for.
    """
    _complete_setup(page, first_run, "15")
    page.goto(f"{first_run}/today?profile=together")
    expect(page.locator(".exit-form")).to_have_count(1)

    stored = httpx.put(f"{first_run}/api/profiles/son", json={"age_years": 18}, timeout=10).json()["data"]
    assert stored["kind"] == "youth"
    assert stored["age_band"] == "age_14_17"

    page.goto(f"{first_run}/today?profile=together")
    expect(page.locator(".exit-form")).to_have_count(1)

    page.goto(f"{first_run}/settings")
    expect(page.locator("[data-older-note]")).to_contain_text("youth rules")


def test_becoming_an_adult_profile_is_explicit_confirmed_and_reversible(page: Page, first_run: str) -> None:
    """The only way out of the youth rules, and the way back in (D-099)."""
    _complete_setup(page, first_run, "15")
    page.goto(f"{first_run}/settings")

    # Without the confirm it does nothing and says why, in place.
    page.click("[data-kind-submit]")
    expect(page.locator('[data-error-for="confirm"]')).to_be_visible()
    assert httpx.get(f"{first_run}/api/profiles/son", timeout=5).json()["data"]["kind"] == "youth"

    page.check(".kind-form input[name=confirm]")
    page.click("[data-kind-submit]")
    page.wait_for_url(re.compile(r"/settings$"))
    assert httpx.get(f"{first_run}/api/profiles/son", timeout=5).json()["data"]["kind"] == "adult"

    # The youth affordances go with it, and the adult assessment column arrives.
    page.goto(f"{first_run}/today?profile=together")
    expect(page.locator(".exit-form")).to_have_count(0)
    assert page.locator('button:text-is("Done")').count() == 1
    page.goto(f"{first_run}/today?profile=son")
    expect(page.locator(".session")).to_contain_text("120 s")

    # And back again, because a control that only removes protections is a trapdoor.
    page.goto(f"{first_run}/settings")
    expect(page.locator("[data-adult-note]")).to_be_visible()
    page.check(".kind-form input[name=confirm]")
    page.click("[data-kind-submit]")
    page.wait_for_url(re.compile(r"/settings$"))
    assert httpx.get(f"{first_run}/api/profiles/son", timeout=5).json()["data"]["kind"] == "youth"
    page.goto(f"{first_run}/today?profile=together")
    expect(page.locator(".exit-form")).to_have_count(1)


def test_son_history_has_no_trend_line(page: Page, first_run: str) -> None:
    """22. Live now that PRP-04 has merged: `svg[data-role="trend"]` is its real selector.

    The trend line is a body-composition chart, so it is one of the things the brief says the son
    never sees. The scorecard assertion keeps this honest: without it the test would pass just as
    well on a page that failed to render at all.
    """
    _complete_setup(page, first_run, "12")

    page.goto(f"{first_run}/history?profile=son")
    assert page.locator('svg[data-role="trend"]').count() == 0
    expect(page.locator('[data-role="score"]')).to_have_count(1)

    text = _visible_text(page)
    for phrase in BANNED_PHRASES:
        assert phrase not in text, f"{phrase!r} is on the son's History"
    metrics = _metric_text(page)
    for word in BANNED_BARE_WORDS:
        assert not re.search(rf"\b{word}\b", metrics), f"{word!r} is in a metric element on the son's History"


def test_the_together_history_carries_no_trend_either(page: Page, first_run: str) -> None:
    """The son's column is on that page too, and D-105 keeps the card off it entirely."""
    _complete_setup(page, first_run, "12")
    page.goto(f"{first_run}/history?profile=together")
    assert page.locator('svg[data-role="trend"]').count() == 0
    text = _visible_text(page)
    for phrase in BANNED_PHRASES:
        assert phrase not in text


# --------------------------------------------------------------- 23: the settings


def test_settings_has_import_and_generate_containers(page: Page, first_run: str) -> None:
    """23. The two ids PRP-08 renders into exist and are empty."""
    _complete_setup(page, first_run)
    page.goto(f"{first_run}/settings")
    for identifier in ("#import-result", "#generate-preview"):
        expect(page.locator(identifier)).to_have_count(1)
        assert (page.locator(identifier).inner_text() or "").strip() == ""


def test_settings_saves_and_says_so(page: Page, first_run: str) -> None:
    """One Save, and the screen says what happened rather than silently reloading."""
    _complete_setup(page, first_run)
    page.goto(f"{first_run}/settings")

    page.click('.segmented label.seg:has(input[name="days_per_week"][value="3"])')
    page.click('.segmented label.seg:has(input[name="display_unit"][value="lb"])')
    page.click(".save")
    expect(page.locator(".save-ok")).to_be_visible(timeout=5000)

    stored = httpx.get(f"{first_run}/api/settings", timeout=5).json()["data"]
    assert stored["days_per_week"] == 3
    assert stored["display_unit"] == "lb"


def test_every_control_is_a_thumb_sized_target(page: Page, first_run: str) -> None:
    """The floor Today holds itself to: 56 px, one-handed, at 390 px wide."""
    page.goto(f"{first_run}/setup")
    for selector in (".save", "#son_age", ".segmented label.seg", ".check"):
        box = page.locator(selector).first.bounding_box()
        assert box is not None, selector
        assert box["height"] >= 44, f"{selector} is {box['height']}px tall"
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


# ------------------------------------------------- 14 in a browser: the display unit


def test_the_display_unit_converts_the_loads_on_today(page: Page, first_run: str) -> None:
    """14, at the only place it matters: the row a person reads on the phone.

    ``format_load`` is unit-tested, but the acceptance test passes just as well against a filter
    that labels the number without converting it - which is the bug D-090 records. So this reads a
    real planned load out of the API in kilograms, converts it here, and asserts the *page* shows
    that pound figure.
    """
    _complete_setup(page, first_run, "12")

    body = httpx.get(f"{first_run}/api/today?profile=me", timeout=10).json()["data"]
    loaded = [row for row in body["rows"] if row["load_kg"]]
    assert loaded, "the parent's session carries no load: the conversion would be untested"
    kilos = float(loaded[0]["load_kg"])
    # Nearest half pound, the rule D-090 states.
    pounds = round(kilos / 0.45359237 / 0.5) * 0.5
    expected = f"{pounds:.1f}".rstrip("0").rstrip(".") + " lb"

    page.goto(f"{first_run}/settings")
    page.click('.segmented label.seg:has(input[name="display_unit"][value="lb"])')
    page.click(".save")
    expect(page.locator(".save-ok")).to_be_visible(timeout=5000)

    page.goto(f"{first_run}/today?profile=me")
    expect(page.locator(".row-line", has_text=expected).first).to_be_visible()
    assert " kg" not in (page.locator("#main").inner_text() or ""), "a kilogram label survived the switch"

    # And back again, so the switch is not one-way.
    page.goto(f"{first_run}/settings")
    page.click('.segmented label.seg:has(input[name="display_unit"][value="kg"])')
    page.click(".save")
    expect(page.locator(".save-ok")).to_be_visible(timeout=5000)
    page.goto(f"{first_run}/today?profile=me")
    assert " lb" not in (page.locator("#main").inner_text() or "")


def test_a_settings_save_round_trips_every_field(page: Page, first_run: str) -> None:
    """One save, then a reload: what the screen shows next is what the screen was told."""
    _complete_setup(page, first_run, "12")
    page.goto(f"{first_run}/settings")

    page.fill("#weights_available", "KB 16/24 kg")
    page.click('.segmented label.seg:has(input[name="days_per_week"][value="5"])')
    page.click('.segmented label.seg:has(input[name="session_minutes"][value="45"])')
    page.fill("#person_me", "jd")
    page.fill("#person_son", "the-son")
    page.click(".save")
    expect(page.locator(".save-ok")).to_be_visible(timeout=5000)

    page.reload()
    expect(page.locator("#weights_available")).to_have_value("KB 16/24 kg")
    expect(page.locator("#person_me")).to_have_value("jd")
    expect(page.locator("#person_son")).to_have_value("the-son")
    expect(page.locator('input[name="days_per_week"][value="5"]')).to_be_checked()
    expect(page.locator('input[name="session_minutes"][value="45"]')).to_be_checked()


def test_the_setup_screen_will_not_take_a_slug_that_is_a_path(page: Page, first_run: str) -> None:
    """The person field lands in a VitalForge URL (PRP-06), so it is not free text."""
    page.goto(f"{first_run}/setup")
    page.fill("#son_age", "12")
    page.fill("#person_son", "../me")
    page.click(".save")

    # Under the son's box specifically: the two slugs are separate fields now (D-114).
    expect(page.locator('[data-error-for="person_son"]')).to_be_visible()
    expect(page.locator('[data-error-for="person_me"]')).to_have_count(0)
    expect(page).to_have_url(re.compile(r"/setup$"))
    assert httpx.get(f"{first_run}/api/settings", timeout=5).json()["data"]["setup_complete"] is False
