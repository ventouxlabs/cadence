"""``make seed`` - loads ``library/`` into the database.

PRP-01 fills this in. Until then it reports that there is nothing to load and exits 0, so the
Makefile target and CI stay honest rather than green-by-omission.
"""

from __future__ import annotations

import sys
from pathlib import Path

LIBRARY_DIR = Path("library")
CONTENT_DIRS = ("exercises", "workouts", "progressions")


def main() -> int:
    populated = [name for name in CONTENT_DIRS if any((LIBRARY_DIR / name).glob("*.yaml"))]
    if not populated:
        sys.stdout.write("no library yet - PRP-01 adds library/exercises and library/workouts\n")
        return 0
    sys.stdout.write(f"library content found in: {', '.join(populated)}; loader lands in PRP-01\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
