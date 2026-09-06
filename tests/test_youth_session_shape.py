"""Section 6.5's row count for the son, and the two documented reasons it moves.

The son's session is one row shorter than the parent's, floored at three and capped by his band:
``max(3, min(parent_row_count - 1, max_exercises_per_session))``. Two rules bend that number and
both are written down - D-059 adds a row back when truncation would leave no ``play`` row, and
D-065 takes rows away when the band's clock cannot fit them. Nothing else may move it.

``tests/test_program_matrix.py`` asserts the *cap*; this file asserts the *formula*, which is a
different claim: a session of three rows satisfies the cap and still breaks section 6.5.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from cadence.profils.settings import DEFAULT_SETTINGS
from cadence.profils.tables import Profile
from cadence.programme.materialise import materialise_rows
from cadence.schema.enums import AgeBand, ExerciseTag

BAND_AGE = {AgeBand.U10: 8, AgeBand.AGE_10_13: 11, AgeBand.AGE_14_17: 15}
RAW_RULES: dict = yaml.safe_load(
    (Path(__file__).resolve().parents[1] / "library" / "youth_rules.yaml").read_text(encoding="utf-8")
)

# Section 6.4's parent budget, excluding the prelude.
PARENT_ROWS = {15: 3, 30: 5, 45: 7}
MIN_YOUTH_ROWS = 3

# Templates where D-059 can never fire, so the formula holds exactly: every row of `son-play-day`
# carries `play`, and `together-full-body` is a `both` workout whose ids section 10.9 fixes across
# the two columns, which puts it outside D-059's scope.
NO_PLAY_INSERTION = ("son-play-day", "together-full-body")
YOUTH_ONLY = ("son-upper-a", "son-lower-a", "son-play-day")


def _youth(band: AgeBand) -> Profile:
    return Profile(id="son", display_name="Son", kind="youth", age_years=BAND_AGE[band])


def _body(rows: list[dict]) -> list[dict]:
    return [row for row in rows if row["role"] != "prelude"]


def _expected(band: AgeBand, minutes: int, declared_rows: int) -> int:
    cap = RAW_RULES[band.value]["max_exercises_per_session"]
    return max(MIN_YOUTH_ROWS, min(PARENT_ROWS[minutes] - 1, cap, declared_rows))


@pytest.mark.parametrize("band", list(BAND_AGE))
@pytest.mark.parametrize("minutes", sorted(PARENT_ROWS))
@pytest.mark.parametrize("workout_id", NO_PLAY_INSERTION)
def test_the_son_gets_one_row_fewer_than_his_parent(library, band: AgeBand, minutes: int, workout_id: str) -> None:
    """Section 6.5's formula, on the templates where nothing else can move the count."""
    template = library.template(workout_id)
    rows = _body(
        materialise_rows(template, 1, _youth(band), DEFAULT_SETTINGS.with_changes(session_minutes=minutes), library)
    )
    assert len(rows) == _expected(band, minutes, len(template.rows)), f"{workout_id} {band.value} {minutes}min"


@pytest.mark.parametrize("band", list(BAND_AGE))
@pytest.mark.parametrize("minutes", sorted(PARENT_ROWS))
@pytest.mark.parametrize("workout_id", YOUTH_ONLY)
def test_the_count_moves_only_by_the_two_documented_rules(
    library, band: AgeBand, minutes: int, workout_id: str
) -> None:
    """Never below three, never above the formula plus D-059's single re-inserted play row."""
    template = library.template(workout_id)
    cap = RAW_RULES[band.value]["max_exercises_per_session"]
    baseline = _expected(band, minutes, len(template.rows))
    rows = _body(
        materialise_rows(template, 1, _youth(band), DEFAULT_SETTINGS.with_changes(session_minutes=minutes), library)
    )

    assert MIN_YOUTH_ROWS <= len(rows) <= min(baseline + 1, cap), f"{workout_id} {band.value} {minutes}min"


@pytest.mark.parametrize("band", list(BAND_AGE))
def test_a_truncated_youth_session_buys_its_play_row_back(library, band: AgeBand) -> None:
    """D-059: at 15 minutes ``son-upper-a`` truncates past ``bear-crawl``, and it comes back.

    The formula gives three rows and the session runs four, because section 6.5 requires a fun row
    and the three that survive priority order are not it. The extra row is that play row and
    nothing else, and it never takes the session past the band's cap.
    """
    settings = DEFAULT_SETTINGS.with_changes(session_minutes=15)
    rows = _body(materialise_rows(library.template("son-upper-a"), 1, _youth(band), settings, library))
    plays = [row for row in rows if ExerciseTag.PLAY in library.exercise(row["exercise_id"]).tags]

    assert len(rows) == _expected(band, 15, 5) + 1 == 4
    assert len(rows) <= RAW_RULES[band.value]["max_exercises_per_session"]
    assert [row["exercise_id"] for row in plays] == ["bear-crawl"]


def test_the_clock_takes_a_row_back_off_at_u10(library) -> None:
    """D-065: the formula asks for five rows of ``son-lower-a`` at 45 min and u10 affords four.

    Section 3.2's twenty minutes beats section 6.4's row count (P1), so the session is short by
    design. The other two bands, with 25 and 35 minutes, keep all five.
    """
    settings = DEFAULT_SETTINGS.with_changes(session_minutes=45)
    counts = {
        band: len(_body(materialise_rows(library.template("son-lower-a"), 1, _youth(band), settings, library)))
        for band in BAND_AGE
    }
    assert counts[AgeBand.U10] == 4 < _expected(AgeBand.U10, 45, 5)
    assert counts[AgeBand.AGE_10_13] == counts[AgeBand.AGE_14_17] == 5


@pytest.mark.parametrize("band", list(BAND_AGE))
@pytest.mark.parametrize("minutes", sorted(PARENT_ROWS))
def test_the_son_is_never_more_than_one_row_ahead_of_his_parent(library, band: AgeBand, minutes: int) -> None:
    """Section 6.5 reads "one row fewer", and D-059's play row eats that margin.

    Where truncation would leave the son no fun row, D-059 puts one back, so his session lands
    level with his parent's at 30 minutes and one *above* it at 15, where section 6.5's floor of
    three is already binding for both. The band's minute cap still governs, so this buys no
    dangerous volume; it is pinned here rather than left for a reader of section 6.5 to trip over.
    """
    settings = DEFAULT_SETTINGS.with_changes(session_minutes=minutes)
    adult = Profile(id="me", display_name="Me", kind="adult")
    parent = _body(materialise_rows(library.template("lower-a"), 1, adult, settings, library))
    assert len(parent) == PARENT_ROWS[minutes]

    for workout_id in YOUTH_ONLY:
        child = _body(materialise_rows(library.template(workout_id), 1, _youth(band), settings, library))
        where = f"{workout_id} {band.value} {minutes}min"
        assert len(child) <= len(parent) + 1, where
        assert len(child) <= RAW_RULES[band.value]["max_exercises_per_session"], where
        if minutes == 45:
            assert len(child) < len(parent), where


@pytest.mark.parametrize("band", list(BAND_AGE))
def test_at_fifteen_minutes_the_son_does_one_row_more_than_his_parent(library, band: AgeBand) -> None:
    """The exact case the rule above bounds: three rows for the parent, four for the son."""
    settings = DEFAULT_SETTINGS.with_changes(session_minutes=15)
    adult = Profile(id="me", display_name="Me", kind="adult")
    parent = _body(materialise_rows(library.template("lower-a"), 1, adult, settings, library))
    child = _body(materialise_rows(library.template("son-upper-a"), 1, _youth(band), settings, library))
    assert (len(parent), len(child)) == (3, 4)
