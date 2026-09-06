"""The seed library against ``docs/exercise-principles.md``, field by field.

PRP-01's first devil's-advocate risk is that an implementer "fixes" a name, a rep count or a
category to make a test pass. The defence is that the principles document is the source: section
9's tables are *parsed here* and compared cell by cell, and section 10's prescriptions are
transcribed literally into this file. A mismatch is a bug in the library, never in the document.

Three prescriptions cannot reach the validator as section 10 writes them, and D-062 adjusts the
YAML instead of loosening the gate. Each carve-out is named below, so it is visible as a decision
rather than absent as an oversight.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from cadence.schema.enums import AgeBand

PRINCIPLES = Path(__file__).resolve().parents[1] / "docs" / "exercise-principles.md"

_BACKTICKS = re.compile(r"`([^`]+)`")
_DASHES = frozenset({"—", "–", "-", ""})
# Section 9's youth columns. `Y*` is section 3.6's conditional, never a truthy yes.
_ALLOW = {"Y": "yes", "N": "no", "Y*": "conditional"}
# D-018: `garminconnect` 0.3.11 has no UNKNOWN member, so section 9's UNKNOWN is a null column.
_UNKNOWN = "UNKNOWN"

SECTION_9_COLUMNS = 14


def _strip(cell: str) -> str:
    return cell.replace("`", "").strip()


def _table_rows(first_heading: str, stop_heading: str) -> list[list[str]]:
    """Every data row of every pipe table between two headings, cells stripped."""
    lines = PRINCIPLES.read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith(first_heading))
    stop = next(index for index, line in enumerate(lines) if line.startswith(stop_heading))
    rows: list[list[str]] = []
    for line in lines[start:stop]:
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        is_header = cells[0] == "id"
        is_rule = all(set(cell) <= {"-", ":"} and cell for cell in cells)
        if not (is_header or is_rule):
            rows.append(cells)
    return rows


def _links(cell: str) -> tuple[str | None, str | None]:
    """``regr_of: X`` / ``prog_of: X`` to ``(regression_of, progression_of)``."""
    if cell in _DASHES:
        return None, None
    regression = progression = None
    for part in cell.split(","):
        part = part.strip()
        if part.startswith("regr_of:"):
            regression = _strip(part.split(":", 1)[1])
        elif part.startswith("prog_of:"):
            progression = _strip(part.split(":", 1)[1])
        else:  # pragma: no cover - a new link kind in the document, not a code path
            raise AssertionError(f"unreadable link cell {cell!r}")
    return regression, progression


def _tags(cell: str) -> tuple[str, ...]:
    return () if cell.strip() in _DASHES else tuple(sorted(_BACKTICKS.findall(cell)))


SECTION_9_ROWS: list[list[str]] = _table_rows("### 9.1", "## 10.")
SECTION_9_IDS: list[str] = [_strip(row[0]) for row in SECTION_9_ROWS]


def test_the_section_9_parser_read_the_whole_table() -> None:
    """If the parse silently returns nothing, every row test below passes vacuously."""
    assert len(SECTION_9_ROWS) == 52
    assert all(len(row) == SECTION_9_COLUMNS for row in SECTION_9_ROWS)
    assert len(set(SECTION_9_IDS)) == 52


@pytest.mark.parametrize("row", SECTION_9_ROWS, ids=SECTION_9_IDS)
def test_section_9_row_is_transcribed_verbatim(library, row: list[str]) -> None:
    """Every cell of section 9 against the loaded exercise: 52 rows x 11 fields.

    Acceptance test 2 pins the id set. This pins everything the ids carry - the pattern, the load
    unit, the three youth columns and the Garmin category the PRP names, plus the region, load
    type, measure, name, cue, tags and links that would drift the same way.
    """
    identifier, name, pattern, region, load_type, measure, unit, u10, mid, teen, tags, links, cue, garmin = row
    identifier = _strip(identifier)
    exercise = library.exercises.get(identifier)
    assert exercise is not None, f"{identifier} is in principles section 9 and not in the library"

    assert exercise.name == name.strip()
    assert exercise.pattern.value == pattern
    assert exercise.region.value == region
    assert exercise.load_type.value == load_type
    assert exercise.measure.value == measure
    assert exercise.load_unit.value == unit
    assert exercise.cue == cue.strip()
    assert tuple(sorted(tag.value for tag in exercise.tags)) == _tags(tags)
    assert (exercise.regression_of, exercise.progression_of) == _links(links)

    expected_category = None if garmin.strip() == _UNKNOWN else garmin.strip()
    actual_category = exercise.garmin_category.value if exercise.garmin_category else None
    assert actual_category == expected_category

    expected_youth = {"u10": _ALLOW[u10], "age_10_13": _ALLOW[mid], "age_14_17": _ALLOW[teen]}
    actual_youth = {band.value: allow.value for band, allow in exercise.youth_ok_by_band.items()}
    assert actual_youth == expected_youth


def test_the_library_invents_nothing_section_9_does_not_list(library) -> None:
    """The other direction: no extra exercise slipped in to make a workout resolve."""
    assert sorted(library.exercises) == sorted(SECTION_9_IDS)


def test_every_garmin_category_comes_from_the_closed_list(library) -> None:
    """Section 9's closed list, transcribed. `Never invent a category name.`"""
    closed = {
        "PUSH_UP", "SQUAT", "DEADLIFT", "ROW", "PLANK", "CARRY", "LUNGE", "HIP_RAISE", "CORE",
        "SHOULDER_PRESS", "BENCH_PRESS", "PULL_UP", "FLYE", "LATERAL_RAISE", "HYPEREXTENSION",
        "TOTAL_BODY", "WARM_UP", "CARDIO",
    }  # fmt: skip
    used = {ex.garmin_category.value for ex in library.exercises.values() if ex.garmin_category}
    assert used <= closed, sorted(used - closed)


# ------------------------------------------------------------------- section 10, transcribed by hand
#
# (exercise, role, sets, measure, value, per_side, rest_s). Section 10's tables are shaped
# differently per workout, so they are written out rather than parsed. Values are block week 1.

ADULT_TEMPLATES: dict[str, tuple[tuple, ...]] = {
    # 10.1 / section 2 - the prelude block, fixed order, 0 s between movements.
    "posture-prelude-v1": (
        ("cat-cow", "prelude", 1, "reps", 8, False, 0),
        ("open-book", "prelude", 1, "reps", 6, True, 0),
        ("wall-angel", "prelude", 1, "reps", 8, False, 0),
        ("glute-bridge", "prelude", 1, "reps", 12, False, 0),
        ("dead-bug", "prelude", 1, "reps", 8, True, 0),
    ),
    # 10.2
    "upper-a": (
        ("db-bench-press", "main", 3, "reps", 8, False, 90),
        ("db-bent-row", "secondary", 3, "reps", 8, False, 90),
        ("db-overhead-press", "secondary", 3, "reps", 10, False, 60),
        ("prone-ytw-raise", "secondary", 2, "reps", 10, False, 45),
        ("plank", "finisher", 3, "seconds", 40, False, 45),
    ),
    # 10.3
    "lower-a": (
        ("goblet-squat", "main", 3, "reps", 8, False, 90),
        ("db-rdl", "secondary", 3, "reps", 8, False, 90),
        ("reverse-lunge", "secondary", 3, "reps", 10, True, 60),
        ("bench-hip-thrust", "secondary", 3, "reps", 12, False, 60),
        ("side-plank", "finisher", 2, "seconds", 30, True, 45),
    ),
    # 10.4
    "upper-b": (
        ("push-up", "main", 3, "reps", 12, False, 90),
        ("bench-supported-db-row", "secondary", 3, "reps", 10, False, 90),
        ("db-floor-press", "secondary", 3, "reps", 10, False, 60),
        ("db-floor-pullover", "secondary", 2, "reps", 12, False, 60),
        ("hollow-hold", "finisher", 3, "seconds", 30, False, 45),
    ),
    # 10.5
    "lower-full-b": (
        ("kb-deadlift", "main", 3, "reps", 10, False, 90),
        ("split-squat", "secondary", 3, "reps", 8, True, 90),
        ("step-up-bench", "secondary", 3, "reps", 10, True, 60),
        ("farmer-carry", "secondary", 3, "meters", 30.0, False, 120),
        ("half-kneeling-db-antirotation-hold", "finisher", 2, "seconds", 20, True, 45),
    ),
    # 10.9, adult column
    "together-full-body": (
        ("goblet-squat", "main", 3, "reps", 10, False, 90),
        ("push-up", "secondary", 3, "reps", 12, False, 60),
        ("db-bent-row", "secondary", 3, "reps", 10, False, 60),
        ("farmer-carry", "secondary", 3, "meters", 30.0, False, 90),
        ("plank", "finisher", 3, "seconds", 45, False, 45),
    ),
    # 10.11 - the 15-minute option, three rows only.
    "mobility-carry-day": (
        ("hip-90-90-switch", "secondary", 2, "reps", 8, True, 30),
        ("couch-stretch", "secondary", 2, "seconds", 45, True, 30),
        ("farmer-carry", "finisher", 3, "meters", 40.0, False, 120),
    ),
}

YOUTH_TEMPLATES: dict[str, tuple[tuple, ...]] = {
    # 10.6, specified at age_10_13; the u10 substitutes are asserted separately below.
    "son-upper-a": (
        ("incline-push-up-bench", "main", 2, "reps", 10, False, 60),
        ("bench-supported-db-row", "secondary", 2, "reps", 10, False, 60),
        ("pike-push-up", "secondary", 2, "reps", 8, False, 60),
        ("bear-crawl", "secondary", 2, "meters", 10.0, False, 60),
        ("plank", "finisher", 2, "seconds", 20, False, 60),
    ),
    # 10.7
    "son-lower-a": (
        ("bodyweight-squat", "main", 2, "reps", 12, False, 60),
        ("glute-bridge", "secondary", 2, "reps", 12, False, 60),
        ("reverse-lunge", "secondary", 2, "reps", 8, True, 60),
        ("line-balance-walk", "secondary", 2, "meters", 10.0, False, 60),
        ("dead-bug", "finisher", 2, "reps", 8, True, 60),
    ),
    # 10.8 - every row a play row, every row bodyweight.
    "son-play-day": (
        ("animal-walk-crab", "main", 2, "meters", 10.0, False, 45),
        ("jumping-jacks", "secondary", 2, "reps", 20, False, 45),
        ("throw-and-catch", "secondary", 2, "reps", 20, False, 45),
        ("line-balance-walk", "secondary", 2, "meters", 10.0, False, 45),
        ("bear-crawl", "finisher", 2, "meters", 10.0, False, 45),
    ),
    # 10.9, son column
    "together-full-body": (
        ("goblet-squat", "main", 2, "reps", 10, False, 60),
        ("push-up", "secondary", 2, "reps", 8, False, 60),
        ("db-bent-row", "secondary", 2, "reps", 10, False, 60),
        ("farmer-carry", "secondary", 2, "meters", 15.0, False, 60),
        ("plank", "finisher", 2, "seconds", 20, False, 60),
    ),
}

# 10 names an `extras` pool per workout; 10.4's prose adds `inverted-row` as an extras option.
SECTION_10_EXTRAS: dict[str, tuple[str, ...]] = {
    "upper-a": ("db-lateral-raise", "db-rear-delt-raise"),
    "lower-a": ("wall-sit", "hip-90-90-switch"),
    "upper-b": ("db-rear-delt-raise", "scap-push-up", "inverted-row"),
    "lower-full-b": ("kb-swing", "suitcase-carry"),
    "mobility-carry-day": ("suitcase-carry", "bear-crawl", "open-book", "scap-push-up"),
}

# 10.6 and 10.9: the automatic substitute when the band is u10.
SECTION_10_U10_SUBS: dict[str, dict[str, str]] = {
    "son-upper-a": {"bench-supported-db-row": "prone-ytw-raise", "pike-push-up": "scap-push-up"},
    "together-full-body": {
        "goblet-squat": "bodyweight-squat",
        "push-up": "incline-push-up-bench",
        "db-bent-row": "prone-ytw-raise",
        "farmer-carry": "bear-crawl",
    },
}


def _prescription(row, kind: str) -> tuple:
    column = row.column(kind)
    for measure in ("reps", "seconds", "meters", "steps"):
        value = getattr(column, measure)
        if value is not None:
            break
    return (row.exercise, row.role, column.sets, measure, value, column.per_side, column.rest_s)


@pytest.mark.parametrize("workout_id", sorted(ADULT_TEMPLATES))
def test_section_10_adult_rows_match_in_order(library, workout_id: str) -> None:
    """(b) Row order, exercise ids and the whole week-1 prescription, adult column."""
    rows = tuple(_prescription(row, "adult") for row in library.template(workout_id).rows)
    assert rows == ADULT_TEMPLATES[workout_id]


@pytest.mark.parametrize("workout_id", sorted(YOUTH_TEMPLATES))
def test_section_10_youth_rows_match_in_order(library, workout_id: str) -> None:
    """(b) The same for the son's column, including section 10.9's per-profile split."""
    rows = tuple(_prescription(row, "youth") for row in library.template(workout_id).rows)
    assert rows == YOUTH_TEMPLATES[workout_id]


@pytest.mark.parametrize("workout_id", sorted(SECTION_10_EXTRAS))
def test_section_10_extras_pools_match(library, workout_id: str) -> None:
    extras = tuple(row.exercise for row in library.template(workout_id).extras)
    assert extras == SECTION_10_EXTRAS[workout_id]


@pytest.mark.parametrize("workout_id", sorted(SECTION_10_U10_SUBS))
def test_section_10_u10_substitutes_are_declared(library, workout_id: str) -> None:
    declared = {
        row.exercise: row.column("youth").u10_sub
        for row in library.template(workout_id).rows
        if row.column("youth").u10_sub is not None
    }
    assert declared == SECTION_10_U10_SUBS[workout_id]


def test_assessment_day_row_order_matches_section_10_10(library) -> None:
    """The order is fixed so the grip tests do not contaminate each other."""
    rows = library.template("assessment-day").rows
    assert [row.exercise for row in rows] == [
        "wall-angel",
        "goblet-squat",
        "push-up",
        "dead-hang",
        "plank",
        "farmer-carry",
    ]
    assert [row.assessment_id.value for row in rows] == [
        "wall_angel_reach",
        "goblet_squat_quality",
        "push_up_max",
        "dead_hang_s",
        "plank_s",
        "farmer_carry_s",
    ]
    assert all(row.column("adult").sets == 1 for row in rows), "every measured row is a single set"


def test_d062_carve_outs_are_present_and_are_the_only_ones(library) -> None:
    """The three places section 10 cannot reach the validator as written (D-062).

    Written as assertions so a silent revert shows up here rather than as a validator failure
    three modules away.
    """
    rows = {row.exercise: row for row in library.template("assessment-day").rows}

    # (a) `farmer_carry_s` is timed in section 7.1 but `farmer-carry` measures metres, and a row
    # must be prescribed in its exercise's own measure.
    assert library.exercise("farmer-carry").measure.value == "meters"
    assert rows["farmer-carry"].column("adult").meters is not None
    assert rows["farmer-carry"].column("adult").seconds is None

    # (b) section 7.1 prescribes 5 goblet-squat reps, below the youth loaded-rep floor of 8.
    floor = library.youth_rules[AgeBand.AGE_10_13].rep_min_loaded
    assert rows["goblet-squat"].column("adult").reps == 5 < floor
    assert rows["goblet-squat"].column("youth").reps == floor == 8

    # (c) the youth push-up row takes section 7.4's fun target, never section 7.4's cap column of
    # 20, which u10's bodyweight rep ceiling of 15 would reject.
    assert rows["push-up"].column("adult").reps == 20
    assert rows["push-up"].column("youth").reps <= 15


def test_no_seed_exercise_carries_max_effort_or_assessment_only(library) -> None:
    """D-054: section 9's tags column carries neither, and tagging them would ban the movement
    for every youth band in the workouts section 10 prescribes it in."""
    banned_here = {"max_effort", "assessment_only", "one_rm", "to_failure"}
    for exercise in library.exercises.values():
        assert not ({tag.value for tag in exercise.tags} & banned_here), exercise.id


def test_prelude_flag_is_exactly_the_five_section_2_movements(library) -> None:
    """D-053 puts `is_prelude` on the exercise, so the set it covers must be the section 2 five."""
    flagged = {exercise.id for exercise in library.exercises.values() if exercise.is_prelude}
    assert flagged == {"cat-cow", "open-book", "wall-angel", "glute-bridge", "dead-bug"}
