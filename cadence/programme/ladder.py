"""The ``weights_available`` grammar and the load ladder (principles section 8).

Parse failure on a token is never a guess: the token is ignored, a warning naming it is surfaced
in Settings, and the affected implement falls back to an empty ladder - which means bodyweight
only, because every load rule that cannot find a rung drops to its bodyweight regression (8.5).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from cadence.schema.enums import LoadType

LB_TO_KG = 0.45359237
KG_DP = 2

# Section 8.2 defaults: an adjustable range in pounds steps by 2.5 lb, in kilos by 1.0 kg.
DEFAULT_STEP_LB = 2.5
DEFAULT_STEP_KG = 1.0

# A ladder longer than this is a typo, not a dumbbell rack.
MAX_RUNGS = 500

# And a rung heavier than this is a typo too. The whitelist is dumbbells, kettlebells and a bench
# (section 8.1); nothing a household picks up in one hand weighs more. Without a ceiling
# "DB 1000 kg" prescribes a 1000 kg bench press, which the row schema then rejects as an error
# about a number rather than about the setting the person actually typed.
MAX_RUNG_KG = 200.0

_IMPLEMENTS: dict[str, LoadType] = {"DB": LoadType.DUMBBELL, "KB": LoadType.KETTLEBELL}

_TOKEN = re.compile(
    r"""^(?P<impl>[A-Za-z]+)\s+
        (?P<spec>\d+(?:\.\d+)?(?:\s*[-/]\s*\d+(?:\.\d+)?)*)\s*
        (?P<unit>kg|lb|lbs)?\s*
        (?P<adj>adj|adjustable)?\s*
        (?:step\s*(?P<step>\d+(?:\.\d+)?))?\s*$""",
    re.IGNORECASE | re.VERBOSE,
)
_BENCH = re.compile(r"^bench\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ImplementSpec:
    """What one implement token resolved to, in kilograms."""

    load_type: LoadType
    adjustable: bool
    min_kg: float
    max_kg: float
    step_kg: float | None
    rungs: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class WeightsAvailable:
    """The parsed ``weights_available`` setting."""

    ladders: dict[LoadType, list[float]] = field(default_factory=dict)
    specs: dict[LoadType, ImplementSpec] = field(default_factory=dict)
    bench: bool = False
    warnings: list[str] = field(default_factory=list)

    def ladder(self, load_type: LoadType) -> list[float]:
        """The rungs for this implement; empty means bodyweight only."""
        return list(self.ladders.get(load_type, ()))

    def heaviest(self, load_type: LoadType) -> float | None:
        rungs = self.ladders.get(load_type)
        return rungs[-1] if rungs else None


def _to_kg(value: float, unit: str | None) -> float:
    factor = LB_TO_KG if unit and unit.lower().startswith("lb") else 1.0
    return round(value * factor, KG_DP)


def _range_rungs(low: float, high: float, step: float, unit: str | None) -> tuple[float, ...] | None:
    """Rungs for an adjustable range, built in the native unit then converted.

    Accumulating by repeated addition drifts, and converting the endpoints before stepping puts
    the rungs on the wrong numbers: the household's 10 lb rung is 4.54 kg, not 2.27 + 2 x 1.13.
    So the index is an integer and each rung is converted on its own.
    """
    if step <= 0 or high < low:
        return None
    count = int(round((high - low) / step)) + 1
    if count > MAX_RUNGS:
        return None
    rungs = [_to_kg(low + index * step, unit) for index in range(count)]
    return _clean(rungs)


def _clean(rungs: list[float]) -> tuple[float, ...] | None:
    """Sorted, de-duplicated, positive rungs - or ``None`` if nothing is left.

    A zero-kilo rung is not a weight: section 8.3 gives bodyweight an *empty* ladder, and letting
    0.0 through renders a dumbbell row at "0 kg" instead of demoting it to its bodyweight
    regression (section 8.5), because the load resolves to a number rather than to nothing.
    """
    positive = tuple(sorted({rung for rung in rungs if rung > 0}))
    return positive or None


def _within_ceiling(rungs: tuple[float, ...]) -> bool:
    """Whether every rung is a weight a person could actually pick up (``MAX_RUNG_KG``)."""
    return rungs[-1] <= MAX_RUNG_KG


def _parse_token(token: str) -> tuple[ImplementSpec | None, bool, str | None]:
    """One token to (spec, bench_seen, warning)."""
    if _BENCH.match(token):
        return None, True, None
    match = _TOKEN.match(token)
    if match is None:
        return None, False, f"could not read {token!r}; ignoring it"
    impl = match.group("impl").upper()
    load_type = _IMPLEMENTS.get(impl)
    if load_type is None:
        return None, False, f"unknown implement in {token!r}; ignoring it"

    spec = match.group("spec")
    unit = match.group("unit")
    numbers = [float(part) for part in re.split(r"[-/]", spec.replace(" ", ""))]
    is_range = "-" in spec

    if is_range:
        if len(numbers) != 2:
            return None, False, f"could not read the range in {token!r}; ignoring it"
        raw_step = match.group("step")
        in_pounds = bool(unit) and unit.lower().startswith("lb")
        step = float(raw_step) if raw_step else (DEFAULT_STEP_LB if in_pounds else DEFAULT_STEP_KG)
        rungs = _range_rungs(numbers[0], numbers[1], step, unit)
        if rungs is None:
            return None, False, f"could not build a ladder from {token!r}; ignoring it"
        if not _within_ceiling(rungs):
            return None, False, f"{token!r} goes above {MAX_RUNG_KG:g} kg, which is not a dumbbell; ignoring it"
        return (
            ImplementSpec(
                load_type=load_type,
                adjustable=True,
                min_kg=rungs[0],
                max_kg=rungs[-1],
                step_kg=_to_kg(step, unit),
                rungs=rungs,
            ),
            False,
            None,
        )

    rungs = _clean([_to_kg(number, unit) for number in numbers])
    if rungs is None:
        return None, False, f"no usable weights found in {token!r}; ignoring it"
    if not _within_ceiling(rungs):
        return None, False, f"{token!r} goes above {MAX_RUNG_KG:g} kg, which is not a dumbbell; ignoring it"
    return (
        ImplementSpec(
            load_type=load_type,
            adjustable=False,
            min_kg=rungs[0],
            max_kg=rungs[-1],
            step_kg=None,
            rungs=rungs,
        ),
        False,
        None,
    )


def parse_weights_available(text: str) -> WeightsAvailable:
    """Parse the free-text setting into per-implement ladders (principles section 8.2)."""
    ladders: dict[LoadType, list[float]] = {}
    specs: dict[LoadType, ImplementSpec] = {}
    warnings: list[str] = []
    bench = False

    for raw in (text or "").split(","):
        token = raw.strip()
        if not token:
            continue
        spec, bench_seen, warning = _parse_token(token)
        bench = bench or bench_seen
        if warning is not None:
            warnings.append(warning)
            continue
        if spec is None:
            continue
        if spec.load_type in specs:
            warnings.append(
                f"{spec.load_type.value} is described twice; keeping the first entry and ignoring {token!r}"
            )
            continue
        specs[spec.load_type] = spec
        ladders[spec.load_type] = list(spec.rungs)

    return WeightsAvailable(ladders=ladders, specs=specs, bench=bench, warnings=warnings)


def round_to_available(target_kg: float, ladder: list[float], cap_kg: float | None) -> float | None:
    """Principles section 8.4, exactly: nearest rung at or below the target and the cap.

    Never ``round()``. Rounding to nearest can land above a youth cap, which is the one direction
    this function must never go.
    """
    if not ladder:
        return None
    rungs = sorted(ladder)
    limit = target_kg if cap_kg is None else min(target_kg, cap_kg)
    candidates = [rung for rung in rungs if rung <= limit + 1e-9]
    if candidates:
        return max(candidates)
    lightest = rungs[0]
    if cap_kg is None or lightest <= cap_kg + 1e-9:
        return lightest
    return None


def step_down(load_kg: float, ladder: list[float], rungs: int = 1) -> float | None:
    """The rung ``rungs`` places below ``load_kg``, or the lightest rung. Used by section 8.5."""
    if not ladder:
        return None
    ordered = sorted(ladder)
    below = [rung for rung in ordered if rung < load_kg - 1e-9]
    if not below:
        return ordered[0]
    return below[max(len(below) - rungs, 0)]
