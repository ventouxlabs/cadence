"""The ``weights_available`` grammar in anger, and what section 8.4/8.5 does with the result.

``tests/test_ladder.py`` covers the two functions in isolation. This file drives the same rules
through ``materialise_rows``, which is where a rounding mistake actually reaches a person: a load
that rounds up past a youth cap, or a bell the household does not own rendering as a number.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from cadence.profils.settings import DEFAULT_SETTINGS
from cadence.profils.tables import Profile
from cadence.programme.ladder import parse_weights_available, round_to_available
from cadence.programme.materialise import materialise_rows
from cadence.schema.enums import AgeBand, LoadType

BAND_AGE = {AgeBand.U10: 8, AgeBand.AGE_10_13: 11, AgeBand.AGE_14_17: 15}
YOUTH_RULES_YAML = Path(__file__).resolve().parents[1] / "library" / "youth_rules.yaml"


def _youth(band: AgeBand, **fields) -> Profile:
    return Profile(id="son", display_name="Son", kind="youth", age_years=BAND_AGE[band], **fields)


def _body(rows: list[dict]) -> list[dict]:
    return [row for row in rows if row["role"] != "prelude"]


def _loads(library, workout_id: str, profile: Profile, weights: str) -> dict[str, float | None]:
    settings = DEFAULT_SETTINGS.with_changes(weights_available=weights)
    rows = _body(materialise_rows(library.template(workout_id), 1, profile, settings, library))
    return {row["exercise_id"]: row["load_kg"] for row in rows}


# ------------------------------------------------------------------------------ (e) the grammar


@pytest.mark.parametrize(
    ("text", "dumbbells", "kettlebells", "bench", "warned"),
    [
        # The default step fires when `step` is omitted: 2.5 lb in pounds, 1.0 kg in kilos.
        ("DB 5-52.5 lb adj", 20, [], False, False),
        ("KB 16/24 kg", 0, [16.0, 24.0], False, False),
        ("DB 2.5-25 kg", 23, [], False, False),
        # Case is not part of the grammar.
        ("kb 12 kg", 0, [12.0], False, False),
        ("Db 5-52.5 LB Adj, kB 16/24 KG, bench adjustable", 20, [16.0, 24.0], True, False),
        # Spaces around the separators and between tokens are noise.
        ("  DB   2 - 10   kg  ,   KB  16 / 24  kg  ", 9, [16.0, 24.0], False, False),
        ("", 0, [], False, False),
        ("      ", 0, [], False, False),
        # Never guess: an unreadable token is dropped with a warning naming it.
        ("DB banana", 0, [], False, True),
        ("DB -5 kg", 0, [], False, True),
        ("DB -10--5 kg", 0, [], False, True),
        ("SANDBAG 20 kg, KB 16 kg", 0, [16.0], False, True),
        ("DB 5-52.5 lb adj step 0", 0, [], False, True),
        # A range has two ends. Three numbers, or a discrete set spliced into a range, is a typo.
        ("DB 1-2-3 kg", 0, [], False, True),
        ("DB 5/10-20 kg", 0, [], False, True),
        # A zero-kilo bell is not a weight; section 8.3 gives bodyweight an empty ladder.
        ("KB 0 kg", 0, [], False, True),
        ("DB 0-0 kg", 0, [], False, True),
    ],
)
def test_weights_available_grammar_table(
    text: str, dumbbells: int, kettlebells: list[float], bench: bool, warned: bool
) -> None:
    """(e) The section 8.2 grammar, its defaults, its noise tolerance and its refusals."""
    parsed = parse_weights_available(text)
    assert len(parsed.ladder(LoadType.DUMBBELL)) == dumbbells, parsed.warnings
    assert parsed.ladder(LoadType.KETTLEBELL) == kettlebells, parsed.warnings
    assert parsed.bench is bench
    assert bool(parsed.warnings) is warned, parsed.warnings
    assert all(rung > 0 for rung in parsed.ladder(LoadType.DUMBBELL))


def test_the_default_step_matches_an_explicit_one() -> None:
    """`DB 5-52.5 lb adj` and `... step 2.5` must be the same rack, not two different ones."""
    implicit = parse_weights_available("DB 5-52.5 lb adj").ladder(LoadType.DUMBBELL)
    explicit = parse_weights_available("DB 5-52.5 lb adj step 2.5").ladder(LoadType.DUMBBELL)
    assert implicit == explicit
    assert implicit[0] == 2.27 and implicit[-1] == 23.81


def test_a_thousand_kilo_dumbbell_is_refused_with_a_warning() -> None:
    """The grammar has a sanity ceiling: nothing on the whitelist weighs more than MAX_RUNG_KG.

    Reported by the tester as unbounded and fixed in D-068. An over-heavy token is refused the way
    any unreadable token is - ignored, named in a warning, never guessed at (section 8.2) - so the
    implement falls back to bodyweight rather than prescribing a 1000 kg bench press.
    """
    parsed = parse_weights_available("DB 1000 kg")
    assert parsed.ladder(LoadType.DUMBBELL) == []
    assert any("1000" in warning and "200" in warning for warning in parsed.warnings)

    # The ceiling is a ceiling, not a rejection of heavy kit: 200 kg itself is still legal.
    assert parse_weights_available("DB 200 kg").ladder(LoadType.DUMBBELL) == [200.0]
    # And a range that runs past it is refused whole, rather than silently truncated.
    over = parse_weights_available("DB 100-300 kg step 50")
    assert over.ladder(LoadType.DUMBBELL) == []
    assert over.warnings


def test_a_huge_ladder_is_refused_rather_than_built() -> None:
    parsed = parse_weights_available("DB 1-100000 kg step 0.01")
    assert parsed.ladder(LoadType.DUMBBELL) == []
    assert parsed.warnings


# ---------------------------------------------------------------- (f) rounding through the engine


def test_a_ladder_row_takes_the_nearest_lower_rung(library, adult_profile: Profile) -> None:
    """(f) Section 8.4 end to end: every resolved load is a rung the household actually owns."""
    household = parse_weights_available(DEFAULT_SETTINGS.weights_available)
    rungs = set(household.ladder(LoadType.DUMBBELL)) | set(household.ladder(LoadType.KETTLEBELL))
    for workout_id in ("upper-a", "lower-a", "upper-b", "lower-full-b", "mobility-carry-day"):
        rows = _body(materialise_rows(library.template(workout_id), 1, adult_profile, DEFAULT_SETTINGS, library))
        for row in rows:
            if row["load_kg"] is not None:
                assert row["load_kg"] in rungs, f"{workout_id} {row['exercise_id']} {row['load_kg']}"


def test_a_metric_rack_rounds_down_not_to_nearest(library, adult_profile: Profile) -> None:
    """A 2 kg-step rack and a target between rungs: the answer is the rung below, always."""
    loads = _loads(library, "upper-a", adult_profile, "DB 2-20 kg step 2")
    assert loads["db-bench-press"] == 10.0  # 0.55 x 20 = 11.0, and 12.0 would be rounding up
    assert loads["db-overhead-press"] == 6.0  # 0.6 x 10 = 6.0 exactly


def test_no_dumbbells_demotes_the_row_to_its_bodyweight_regression(library, adult_profile: Profile) -> None:
    """(f) Section 8.5: nothing legal on the ladder, so the row takes ``regression_of``."""
    loads = _loads(library, "lower-a", adult_profile, "BENCH adjustable")
    assert "bodyweight-squat" in loads and "goblet-squat" not in loads
    assert loads["bodyweight-squat"] is None

    rows = _body(
        materialise_rows(
            library.template("lower-a"),
            1,
            adult_profile,
            DEFAULT_SETTINGS.with_changes(weights_available="BENCH adjustable"),
            library,
        )
    )
    squat = next(row for row in rows if row["exercise_id"] == "bodyweight-squat")
    assert any("no legal load" in note for note in squat["notes"])


def test_a_zero_kilo_rack_is_treated_as_no_rack_at_all(library, adult_profile: Profile) -> None:
    """A ladder of 0.0 used to render `goblet-squat` at 0 kg instead of demoting the row."""
    loads = _loads(library, "lower-a", adult_profile, "DB 0 kg, KB 0 kg")
    assert "goblet-squat" not in loads
    assert loads["bodyweight-squat"] is None
    assert all(value is None or value > 0 for value in loads.values())


@pytest.mark.parametrize("band", list(BAND_AGE))
@pytest.mark.parametrize("bodyweight", [None, 25.0, 40.0, 45.7, 60.0])
def test_the_youth_cap_only_ever_clamps_down(library, band: AgeBand, bodyweight: float | None) -> None:
    """(f) A youth load is never above the band cap for its own unit, and never above the adult's.

    Both caps of section 3.3 apply and the lower wins, so adding a bodyweight can only tighten the
    number - it must never raise one.
    """
    caps = yaml.safe_load(YOUTH_RULES_YAML.read_text(encoding="utf-8"))[band.value]
    profile = _youth(band, bodyweight_kg=bodyweight)
    unknown = _youth(band)

    for workout_id in ("together-full-body", "son-upper-a", "assessment-day"):
        loads = _loads(library, workout_id, profile, DEFAULT_SETTINGS.weights_available)
        rows = _body(materialise_rows(library.template(workout_id), 1, profile, DEFAULT_SETTINGS, library))
        for row in rows:
            if row["load_kg"] is None:
                continue
            key = "max_load_kg_per_hand" if row["load_unit"] == "per_hand" else "max_load_kg_per_implement"
            assert row["load_kg"] <= caps[key], f"{workout_id} {row['exercise_id']} {row['load_unit']}"
            pct_key = "max_load_pct_bw_per_hand" if row["load_unit"] == "per_hand" else "max_load_pct_bw_per_implement"
            if bodyweight is not None and caps[pct_key]:
                assert row["load_kg"] <= caps[pct_key] * bodyweight, f"{workout_id} {row['exercise_id']}"

        without = _loads(library, workout_id, unknown, DEFAULT_SETTINGS.weights_available)
        for exercise_id, value in loads.items():
            other = without.get(exercise_id)
            if value is not None and other is not None:
                assert value <= other, f"a known bodyweight raised {exercise_id} from {other} to {value}"


def test_a_clamped_youth_row_says_why_it_is_light(library) -> None:
    """D-063: a loaded youth row always carries the note, because "why so light" is the question."""
    son = _youth(AgeBand.AGE_10_13, bodyweight_kg=40.0)
    rows = _body(materialise_rows(library.template("together-full-body"), 1, son, DEFAULT_SETTINGS, library))
    for row in rows:
        if row["load_kg"] is not None:
            assert any("at this age" in note for note in row["notes"]), row["exercise_id"]
            assert row["progression"]["allow_load_progression"] is False
            assert row["progression"]["cap_load_kg"] is not None


def test_rounding_never_selects_a_rung_above_the_cap() -> None:
    """(f) The unit test for the direction that matters: a cap between two rungs takes the lower."""
    ladder = [2.0, 4.0, 6.0, 8.0, 10.0]
    assert round_to_available(9.0, ladder, 5.0) == 4.0
    assert round_to_available(9.0, ladder, 5.9) == 4.0
    assert round_to_available(9.0, ladder, 6.0) == 6.0
    assert round_to_available(1.0, ladder, 8.0) == 2.0  # below the lightest rung, but it fits
    assert round_to_available(9.0, ladder, 1.0) is None  # nothing on the ladder is legal
    assert round_to_available(9.0, ladder, 0.0) is None  # a u10 cap of zero admits no rung
