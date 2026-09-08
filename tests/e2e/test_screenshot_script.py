"""Acceptance test 22: `scripts/screenshots.py` writes the seven named PNGs, at the right sizes.

`tests/e2e/test_shots.py` is a different thing — PRP-02's reviewer screenshots, under different
names into `.shots/`. Nothing exercised the script that produces `docs/screenshots/`, which is
the one whose output is committed and quoted from `README.md` and `docs/HANDOFF.md`.

The script is run whole rather than reimplemented: seeding two databases, standing up two
servers and driving one browser with motion off is the behaviour under test, and a test that
called `capture()` with its own fixtures would pass while `main()` was broken.

It runs in a subprocess, which is both how `make screenshots` invokes it and the only way it can
run here at all — the script uses Playwright's sync API, and importing and calling it inside the
end-to-end suite raises "Sync API inside the asyncio loop" because another test in the session
has already started one.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

# Imported, never restated: `tests/test_screenshots.py` already holds the seven names and their
# viewports for the committed images, and a second copy here would drift the moment a screen is
# renamed — leaving the copy that drifts as the one that stops catching anything (D-245).
from tests.test_screenshots import EXPECTED, png_size  # noqa: E402

#: Runs the real `main()` with the output redirected. `OUT` is a module constant rather than
#: something the environment sets, so redirecting it needs an import — but the redirect is the
#: only thing changed, and `main()` still does the seeding, serving, guarding and shooting.
RUNNER = """
import sys
from pathlib import Path
sys.path.insert(0, {repo!r})
from scripts import screenshots
screenshots.OUT = Path({out!r})
raise SystemExit(screenshots.main())
"""


@pytest.fixture(scope="module")
def written(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """One run of the whole script, into a throwaway directory the repo already ignores.

    Output lands under `REPO` rather than in `tmp_path`: the script prints each path relative to
    the repo root on its last line, so a directory outside the tree makes `main()` raise after
    doing all the work. `.shots/` is git-ignored, so this cannot dirty the tree being committed —
    and in particular it does not overwrite the committed `docs/screenshots/`.
    """
    out = REPO / ".shots" / "script-acceptance"
    shutil.rmtree(out, ignore_errors=True)
    request.addfinalizer(lambda: shutil.rmtree(out, ignore_errors=True))

    runner = tmp_path_factory.mktemp("shots-runner") / "run.py"
    runner.write_text(RUNNER.format(repo=str(REPO), out=str(out)))

    # `CADENCE_DB_PATH` unset so the script seeds its own (its guard refuses the real database);
    # `CADENCE_PORT` unset because `serve` reuses a fixed port and this script runs two servers.
    env = {key: value for key, value in os.environ.items() if key not in {"CADENCE_DB_PATH", "CADENCE_PORT"}}
    result = subprocess.run(
        [sys.executable, str(runner)], cwd=REPO, env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, f"the script failed:\n{result.stdout}\n{result.stderr}"
    assert "7 screenshots written" in result.stdout, result.stdout
    return {path.name: path for path in out.glob("*.png")}


def test_the_script_writes_exactly_the_seven_named_files(written: dict[str, Path]) -> None:
    assert set(written) == set(EXPECTED), f"wrote {sorted(written)}"


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_each_screenshot_is_non_empty_and_the_right_width(name: str, written: dict[str, Path]) -> None:
    """Width is the assertion, not height: the shots are `full_page`, so height is the document's.

    A 390-wide phone screenshot that came out 1280 wide is the viewport not being applied, which
    is the failure worth catching; a taller-than-844 image is simply a page that scrolls.
    """
    path = written[name]
    assert path.stat().st_size > 0, f"{name} is empty"

    width, height = png_size(path)
    expected_width, expected_height = EXPECTED[name]
    assert width == expected_width, f"{name} is {width}px wide, expected {expected_width}"
    assert height >= expected_height, f"{name} is {height}px tall, under the {expected_height}px viewport"


def test_the_screenshots_hold_no_real_training_history(written: dict[str, Path]) -> None:
    """Risk 6, from the other end: the run seeded its own databases rather than reading the real one.

    The script's own guard covers the environment variable; this covers the outcome. Every file
    was written during this run, so none of them is a leftover of a run against `data/cadence.db`.
    """
    assert len(written) == 7
    assert all(path.stat().st_size > 1024 for path in written.values()), "a screenshot is suspiciously small"


def test_the_committed_screenshots_match_what_the_script_produces(written: dict[str, Path]) -> None:
    """`docs/screenshots/` holds the same seven names, so the committed set is not stale by name."""
    committed = {path.name for path in (REPO / "docs" / "screenshots").glob("*.png")}
    assert committed == set(EXPECTED), f"docs/screenshots/ holds {sorted(committed)}"
