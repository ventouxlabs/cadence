"""The committed screenshots, and the script that produces them.

Acceptance item 22 in prose, without a browser. `make screenshots` and CI both regenerate these,
but regeneration alone cannot notice that a screen was renamed or dropped — and a byte-for-byte
`git diff` gate cannot work either, because PNG rasterisation differs between machines (font
hinting, GPU, Chromium build), so it would fail permanently on the first CI run and be deleted
within the week (D-251). What *is* stable across machines is which files exist and how big the
viewport was, and that is what this asserts.

Dimensions come from the PNG header rather than from Pillow: `IHDR` is the first chunk of every
PNG and carries width and height as big-endian uint32 at a fixed offset, so this needs no
dependency the project does not already have.

The second half covers `scripts/screenshots.py` itself — specifically PRP-10 risk 6, the script
pointed at `data/cadence.db`, which holds a child's real training history and would photograph it
into a directory that is committed. Actually *driving* the script needs a browser and lives in
`tests/e2e/test_screenshot_script.py`, which imports the file list below rather than restating it.
"""

from __future__ import annotations

import re
import struct
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SHOTS = REPO / "docs" / "screenshots"
sys.path.insert(0, str(REPO))

from scripts.screenshots import (  # noqa: E402 - the path has to be set first
    DEFAULT_DB,
    OUT,
    PHONE,
    ScreenshotRefused,
    guard_database,
)

DB_VAR = "CADENCE_DB_PATH"

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
#: Every screen PRP-10 ships, at the viewport it was taken in.
EXPECTED: dict[str, tuple[int, int]] = {
    "today-mobile.png": (390, 844),
    "today-desktop.png": (1280, 800),
    "son-today-mobile.png": (390, 844),
    "together-mobile.png": (390, 844),
    "together-tablet.png": (1024, 768),
    "history-mobile.png": (390, 844),
    "setup-mobile.png": (390, 844),
}


def png_size(path: Path) -> tuple[int, int]:
    """``(width, height)`` from the IHDR chunk, or an assertion failure if it is not a PNG."""
    header = path.read_bytes()[:24]
    assert header[:8] == PNG_MAGIC, f"{path.name} is not a PNG"
    width, height = struct.unpack(">II", header[16:24])
    return int(width), int(height)


def test_the_seven_screenshots_are_committed() -> None:
    """No more and no fewer: a renamed screen must not leave its old picture behind."""
    assert SHOTS.is_dir(), "docs/screenshots/ is missing"
    found = {path.name for path in SHOTS.glob("*.png")}
    assert found == set(EXPECTED), f"missing {set(EXPECTED) - found}, unexpected {found - set(EXPECTED)}"


@pytest.mark.parametrize(("name", "size"), sorted(EXPECTED.items()))
def test_each_screenshot_has_its_viewport_dimensions(name: str, size: tuple[int, int]) -> None:
    path = SHOTS / name
    assert path.stat().st_size > 0, f"{name} is empty"
    assert png_size(path) == size, f"{name} is {png_size(path)}, expected {size}"


#: A markdown image embed, `![alt](path)`. Only these are checked: a path mentioned in prose or
#: inside backticks is documentation about the directory, not a link that can break.
_EMBED = re.compile(r"!\[[^\]]*\]\(([^)\s]+)")


def test_the_docs_that_embed_a_screenshot_point_at_one_that_exists() -> None:
    """README and HANDOFF both embed an image; a moved file would render as a broken box."""
    embedded = 0
    for doc in (REPO / "README.md", REPO / "docs" / "HANDOFF.md"):
        for target in _EMBED.findall(doc.read_text()):
            if "screenshots/" not in target:
                continue
            name = target.rsplit("/", 1)[-1]
            assert name in EXPECTED, f"{doc.name} embeds {name!r}, which is not a shipped screenshot"
            embedded += 1
    assert embedded, "neither doc embeds a screenshot: this check would pass vacuously"


# ------------------------------------------------------- the script that writes them (risk 6)


def test_the_guard_refuses_the_real_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DB_VAR, str(DEFAULT_DB))
    with pytest.raises(ScreenshotRefused, match="real training history"):
        guard_database()


def test_the_guard_refuses_a_roundabout_spelling(monkeypatch: pytest.MonkeyPatch) -> None:
    """D-240: the path is resolved before comparing, so `data/../data/cadence.db` is the same file."""
    sneaky = REPO / "data" / ".." / "data" / "cadence.db"
    assert str(sneaky) != str(DEFAULT_DB), "the two spellings are literally equal, so this proves nothing"
    monkeypatch.setenv(DB_VAR, str(sneaky))
    with pytest.raises(ScreenshotRefused):
        guard_database()


def test_the_guard_refuses_a_relative_spelling(monkeypatch: pytest.MonkeyPatch) -> None:
    """`CADENCE_DB_PATH=data/cadence.db`, typed from the repo root, is the real database too."""
    monkeypatch.chdir(REPO)
    monkeypatch.setenv(DB_VAR, "data/cadence.db")
    with pytest.raises(ScreenshotRefused):
        guard_database()


def test_the_guard_allows_a_disposable_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(DB_VAR, str(tmp_path / "throwaway.db"))
    guard_database()


def test_the_guard_allows_an_unset_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset means the script seeds its own, which is the normal path `make screenshots` takes."""
    monkeypatch.delenv(DB_VAR, raising=False)
    guard_database()


def test_the_script_writes_where_the_committed_images_live() -> None:
    """If `OUT` drifts, `make screenshots` silently stops refreshing what the docs embed."""
    assert OUT == SHOTS


def test_the_phone_shots_are_the_prp_viewport() -> None:
    """The script's own viewport constant, against the size `EXPECTED` says the files came out at."""
    assert (PHONE["width"], PHONE["height"]) == EXPECTED["today-mobile.png"]
