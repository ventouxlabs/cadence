"""PRP-01 acceptance tests 14-16: the ``weights_available`` grammar and the load ladder."""

from __future__ import annotations

import pytest

from cadence.profils.settings import DEFAULT_WEIGHTS_AVAILABLE
from cadence.programme.ladder import (
    MAX_RUNGS,
    parse_weights_available,
    round_to_available,
    step_down,
)
from cadence.schema.enums import LoadType

HOUSEHOLD_DB = parse_weights_available(DEFAULT_WEIGHTS_AVAILABLE).ladder(LoadType.DUMBBELL)
HOUSEHOLD_KB = parse_weights_available(DEFAULT_WEIGHTS_AVAILABLE).ladder(LoadType.KETTLEBELL)


@pytest.mark.parametrize(
    ("target", "ladder", "cap", "expected"),
    [
        (20.0, HOUSEHOLD_DB, None, 19.28),
        (1.0, HOUSEHOLD_DB, None, 2.27),
        (20.0, HOUSEHOLD_DB, 5.0, 4.54),
        (20.0, HOUSEHOLD_DB, 1.0, None),
        (20.0, HOUSEHOLD_KB, None, 16.0),
        (24.0, HOUSEHOLD_KB, None, 24.0),
        (15.9, HOUSEHOLD_KB, None, 16.0),
        (20.0, [], None, None),
    ],
)
def test_load_rounding_cases(target: float, ladder: list[float], cap: float | None, expected: float | None) -> None:
    """14. Nearest rung at or below the target and the cap, never above either."""
    assert round_to_available(target, ladder, cap) == expected


def test_rounding_never_exceeds_the_cap() -> None:
    """A cap between two rungs takes the lower one, where round() would take the higher."""
    # 5.67 is nearer to 5.5 than 4.54 is, and 5.67 is over the cap.
    assert round_to_available(20.0, HOUSEHOLD_DB, 5.5) == 4.54


def test_weights_available_parser() -> None:
    """15. The section 8.2 grammar, including the pound conversion and the default steps."""
    parsed = parse_weights_available(DEFAULT_WEIGHTS_AVAILABLE)
    dumbbell = parsed.specs[LoadType.DUMBBELL]
    assert dumbbell.adjustable is True
    assert (dumbbell.min_kg, dumbbell.max_kg, dumbbell.step_kg) == (2.27, 23.81, 1.13)
    assert len(parsed.ladder(LoadType.DUMBBELL)) == 20

    assert set(parsed.ladder(LoadType.KETTLEBELL)) == {16.0, 24.0}
    assert parsed.specs[LoadType.KETTLEBELL].adjustable is False
    assert parsed.bench is True
    assert parsed.warnings == []

    metric = parse_weights_available("DB 2-10 kg")
    assert metric.specs[LoadType.DUMBBELL].step_kg == 1.0
    assert metric.ladder(LoadType.DUMBBELL) == [2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    assert metric.bench is False


def test_ladder_rungs_are_built_in_the_native_unit() -> None:
    """Repeated addition in kilos drifts: the 10 lb rung is 4.54 kg, not 2.27 + 2 x 1.13."""
    rungs = parse_weights_available("DB 5-52.5 lb adj step 2.5").ladder(LoadType.DUMBBELL)
    assert rungs[:4] == [2.27, 3.4, 4.54, 5.67]
    assert rungs[-1] == 23.81


def test_weights_available_garbage() -> None:
    """16. An unreadable token is named in a warning, ignored, and never guessed at."""
    parsed = parse_weights_available("DB banana, KB 16/24 kg")
    assert parsed.ladder(LoadType.DUMBBELL) == []
    assert any("DB banana" in warning for warning in parsed.warnings)
    # The rest of the line still parses.
    assert parsed.ladder(LoadType.KETTLEBELL) == [16.0, 24.0]


@pytest.mark.parametrize(
    "text",
    ["", "   ", "SANDBAG 20 kg", "DB", "DB 10-2 kg", f"DB 1-{MAX_RUNGS + 10} kg step 1"],
)
def test_unparsable_input_never_raises(text: str) -> None:
    parsed = parse_weights_available(text)
    assert parsed.ladder(LoadType.DUMBBELL) == []


def test_duplicate_implement_keeps_the_first() -> None:
    parsed = parse_weights_available("DB 2-10 kg, DB 20-30 kg")
    assert parsed.ladder(LoadType.DUMBBELL)[0] == 2.0
    assert any("twice" in warning for warning in parsed.warnings)


def test_single_discrete_weight() -> None:
    parsed = parse_weights_available("KB 16 kg")
    assert parsed.ladder(LoadType.KETTLEBELL) == [16.0]
    assert parsed.heaviest(LoadType.KETTLEBELL) == 16.0
    assert parsed.heaviest(LoadType.DUMBBELL) is None


def test_step_down_walks_the_ladder() -> None:
    assert step_down(19.28, HOUSEHOLD_DB) == 18.14
    assert step_down(19.28, HOUSEHOLD_DB, rungs=2) == 17.01
    assert step_down(2.27, HOUSEHOLD_DB) == 2.27
    assert step_down(10.0, []) is None
