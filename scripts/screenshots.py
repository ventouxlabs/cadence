#!/usr/bin/env -S uv run python
"""The seven screenshots in `docs/screenshots/`, driven against a throwaway seeded database.

Never against `data/cadence.db`. The images go into git and a household's real database holds a
child's training history, so this refuses to run when `CADENCE_DB_PATH` points at the default
path (PRP-10 risk 6). It builds its own database with the same seeder `make seed` uses, starts
its own server on its own port, and deletes neither — the temp directory goes when the process
does.

Deterministic on purpose (risk 7): reduced motion is forced, so the tick animation is not
mid-flight when the shutter opens; the metrics cache is written by hand with fixed values, so the
trend line is the same three points every run; and the ticks are placed through the API rather
than by clicking, so no screenshot depends on a race between HTMX and the camera.

    make screenshots
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from playwright.sync_api import sync_playwright

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "docs" / "screenshots"
DEFAULT_DB = REPO / "data" / "cadence.db"

PHONE = {"width": 390, "height": 844}
TABLET = {"width": 1024, "height": 768}
DESKTOP = {"width": 1280, "height": 800}

TICKED_ROWS = 2
FIXED_WEIGHTS = ((28, 85.2), (14, 84.6), (0, 84.1))
FIXED_BODY_FAT = ((28, 19.1), (14, 18.6), (0, 18.2))

sys.path.insert(0, str(REPO))
from tests.e2e.conftest import serve  # noqa: E402 - the path has to be set first


class ScreenshotRefused(RuntimeError):
    """The script will not run against the database it was pointed at."""


def guard_database() -> None:
    """Refuse the real database, however it was named.

    ``CADENCE_DB_PATH`` is the variable the app actually reads (`cadence/config.py`); an unset one
    means the default, which is the same file. Resolved before comparing, so `./data/../data/…`
    is caught too.
    """
    configured = os.environ.get("CADENCE_DB_PATH")
    if configured is None:
        return
    if Path(configured).expanduser().resolve() == DEFAULT_DB.resolve():
        raise ScreenshotRefused(
            f"CADENCE_DB_PATH points at {DEFAULT_DB}, which holds real training history. "
            "Unset it, or point it somewhere disposable; this script seeds its own database."
        )


def seed(path: Path, *, fresh: bool = False) -> Path:
    """A database seeded exactly the way ``make seed`` seeds the real one.

    Its own function rather than the end-to-end suite's, which reports failure through
    ``pytest.fail`` and has no way to ask for ``--fresh`` — the state ``/setup`` renders in.
    """
    command = [sys.executable, "-m", "cadence.bibliotheque.seed"] + (["--fresh"] if fresh else [])
    result = subprocess.run(
        command,
        cwd=REPO,
        env={**os.environ, "CADENCE_DB_PATH": str(path)},
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ScreenshotRefused(f"seeding {path.name} failed:\n{result.stdout}\n{result.stderr}")
    return path


def _fixed_metrics(db_path: Path) -> None:
    """Three weight and three body-fat points, so the trend card is identical every run."""
    now = datetime.now(UTC)
    today = now.astimezone().date()
    payload = {
        "weight_kg": [[(today - timedelta(days=ago)).isoformat(), value] for ago, value in FIXED_WEIGHTS],
        "body_fat_pct": [[(today - timedelta(days=ago)).isoformat(), value] for ago, value in FIXED_BODY_FAT],
    }
    db = sqlite3.connect(db_path, timeout=10)
    try:
        db.execute(
            "INSERT OR REPLACE INTO metrics_cache (profile_id, fetched_at, payload_json, stale) VALUES ('me', ?, ?, 0)",
            (now.isoformat(), json.dumps(payload)),
        )
        db.commit()
    finally:
        db.close()


def _finish_one_session(base_url: str) -> None:
    """One real Done through the app's own routes, so History has a card to show."""
    with httpx.Client(base_url=base_url, timeout=10.0) as api:
        data = api.get("/api/today?profile=me").json()["data"]
        session_id = data["session_id"]
        for row in data["rows"]:
            api.post(f"/api/sessions/{session_id}/rows/{row['position']}", json={"done": True})
        api.post(f"/api/sessions/{session_id}/done", json={"felt": "right"})


def _tick(base_url: str, profile: str, count: int) -> None:
    """Leave ``count`` rows ticked on the open checklist, through the API rather than the mouse."""
    with httpx.Client(base_url=base_url, timeout=10.0) as api:
        data = api.get(f"/api/today?profile={profile}").json()["data"]
        for row in data["rows"][:count]:
            api.post(f"/api/sessions/{data['session_id']}/rows/{row['position']}", json={"done": True})


def _shoot(context, url: str, viewport: dict[str, int], name: str) -> Path:
    page = context.new_page()
    page.set_viewport_size(viewport)
    page.goto(url, wait_until="networkidle")
    target = OUT / name
    # Viewport, not full page (D-250). `.footer` is `position: fixed` (PRP-02), and a full-page
    # capture composites it over whatever content happens to sit at its viewport offset — in the
    # first cut it covered a prelude row halfway down the son's checklist. A viewport shot is
    # also the more honest picture: it is exactly what the phone shows, bar included.
    page.screenshot(path=str(target), full_page=False)
    page.close()
    if not target.exists() or target.stat().st_size == 0:
        raise ScreenshotRefused(f"{name} was not written")
    return target


def capture(seeded_url: str, fresh_url: str) -> list[Path]:
    """Every screen, in one browser, with motion off."""
    OUT.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    with sync_playwright() as play:
        browser = play.chromium.launch()
        # `reduced_motion="reduce"` is what makes the tick animation a no-op for the camera.
        context = browser.new_context(reduced_motion="reduce")
        try:
            shots = (
                (seeded_url, "/today?profile=me", PHONE, "today-mobile.png"),
                (seeded_url, "/today?profile=me", DESKTOP, "today-desktop.png"),
                (seeded_url, "/today?profile=son", PHONE, "son-today-mobile.png"),
                (seeded_url, "/today?profile=together", PHONE, "together-mobile.png"),
                (seeded_url, "/today?profile=together", TABLET, "together-tablet.png"),
                (seeded_url, "/history?profile=me", PHONE, "history-mobile.png"),
                (fresh_url, "/setup", PHONE, "setup-mobile.png"),
            )
            for base, path, viewport, name in shots:
                written.append(_shoot(context, base + path, viewport, name))
        finally:
            context.close()
            browser.close()
    return written


def main() -> int:
    guard_database()
    with TemporaryDirectory(prefix="cadence-shots-") as work:
        root = Path(work)
        seeded = seed(root / "seeded.db")
        # `--fresh` leaves setup incomplete, which is the only state `/setup` renders in (D-091).
        fresh = seed(root / "fresh.db", fresh=True)
        with serve(seeded, "shots-seeded.log", CADENCE_VITALFORGE_MODE="mock") as seeded_url:
            _finish_one_session(seeded_url)
            _fixed_metrics(seeded)
            _tick(seeded_url, "me", TICKED_ROWS)
            with serve(fresh, "shots-fresh.log", CADENCE_VITALFORGE_MODE="mock") as fresh_url:
                written = capture(seeded_url, fresh_url)
    for path in written:
        print(f"  {path.relative_to(REPO)}  {path.stat().st_size} B")
    print(f"{len(written)} screenshots written to {OUT.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
