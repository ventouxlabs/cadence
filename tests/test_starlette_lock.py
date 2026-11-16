"""Guard: the locked Starlette must include the DoS fixes (>= 0.49.1)."""

import tomllib
from pathlib import Path

MIN_STARLETTE = (0, 49, 1)


def test_locked_starlette_is_patched() -> None:
    lock = tomllib.loads((Path(__file__).parent.parent / "uv.lock").read_text())
    version = next(p["version"] for p in lock["package"] if p["name"] == "starlette")
    assert tuple(int(x) for x in version.split(".")[:3]) >= MIN_STARLETTE
