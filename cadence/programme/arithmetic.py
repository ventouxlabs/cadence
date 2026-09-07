"""``apply_outcome`` - principles section 5.5's arithmetic, and section 5.6's youth order.

Pure. Every function here takes a materialised row dict and returns a **new** one; nothing is
mutated in place, so a caller can compare the old and the new row to see what changed and a
failed validation can simply drop the result.

Order matters twice over. Post-bump the load is re-rounded to the ladder and *then* clamped to the
band cap (section 8.4, then section 3.3): rounding after clamping can push the number back over
the cap, which is risk 3. And a youth bump walks section 5.6's four steps in order, load last and
only when the first three are exhausted, so load never rises by any path (risk 2).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from typing import Any

from cadence.programme.decision import Outcome
from cadence.programme.ladder import round_to_available
from cadence.schema.exercise import Exercise
from cadence.schema.youth import YouthRuleSet

# Section 5.5: seconds cannot regress below twice their own step.
SECONDS_FLOOR_MULTIPLIER = 2
DELOAD_RPE_CAP = 6
MIN_DELOAD_SETS = 2
MIN_SETS = 1

#: Resolves an exercise id to a movement this profile may legally be given, or ``None``.
Resolver = Callable[[str], Exercise | None]


def _replace(row: dict[str, Any], **changes: Any) -> dict[str, Any]:
    """A new row with these keys changed. The one way this module writes."""
    return {**row, **changes}


def _progression(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("progression")
    return dict(value) if isinstance(value, dict) else {}


def _with_progression(row: dict[str, Any], **changes: Any) -> dict[str, Any]:
    return _replace(row, progression={**_progression(row), **changes})


def _cap(row: dict[str, Any]) -> float | None:
    value = _progression(row).get("cap_load_kg")
    return float(value) if isinstance(value, int | float) else None


def _rung(row: dict[str, Any], target: float, ladder: Sequence[float]) -> float | None:
    """Round down to the ladder, then clamp to the band cap. Never the other way round."""
    return round_to_available(float(target), list(ladder), _cap(row))


def _rep_ceiling(row: dict[str, Any], rules: YouthRuleSet | None) -> int | None:
    """The highest rep this row may be prescribed at: the progression's, then the band's."""
    progression = _progression(row)
    ceilings = [value for value in (progression.get("rep_max"),) if isinstance(value, int)]
    if rules is not None:
        loaded = bool(row.get("load_kg"))
        band_ceiling = rules.rep_max_loaded if loaded and rules.rep_max_loaded else rules.rep_max_bodyweight
        ceilings.append(int(band_ceiling))
    return min(ceilings) if ceilings else None


def _rep_floor(row: dict[str, Any]) -> int:
    value = _progression(row).get("rep_min")
    return int(value) if isinstance(value, int) and value >= 1 else 1


def _load_step(row: dict[str, Any]) -> float | None:
    """The kilograms one bump adds: the absolute step, or the percentage one (section 5.1)."""
    progression = _progression(row)
    step = progression.get("load_step_kg")
    if isinstance(step, int | float) and step > 0:
        return float(step)
    pct = progression.get("load_step_pct")
    load = row.get("load_kg")
    if isinstance(pct, int | float) and pct > 0 and isinstance(load, int | float):
        return float(load) * float(pct)
    return None


def _swap(row: dict[str, Any], exercise: Exercise | None, *, reps: int | None) -> dict[str, Any] | None:
    """Move this row onto a linked movement, keeping its position and role.

    The load does not travel: a different movement has a different ladder and a different cap, and
    carrying the old number across is how a bodyweight regression arrives holding a dumbbell.
    """
    if exercise is None or exercise.id == row.get("exercise_id"):
        return None
    swapped = _replace(
        row,
        exercise_id=exercise.id,
        name=exercise.name,
        cue=exercise.cue[:120],
        measure=exercise.measure.value,
        load_unit=exercise.load_unit.value,
        garmin_category=exercise.garmin_category.value if exercise.garmin_category else None,
        load_kg=None,
    )
    if reps is not None and swapped.get("reps") is not None:
        swapped = _replace(swapped, reps=reps)
    return swapped


def _linked(row: dict[str, Any], key: str, resolve: Resolver | None) -> Exercise | None:
    """The ``progression_of`` / ``regression_of`` neighbour of this row's movement."""
    if resolve is None:
        return None
    origin = resolve(str(row.get("exercise_id") or ""))
    if origin is None:
        return None
    linked_id = getattr(origin, key, None)
    return resolve(str(linked_id)) if linked_id else None


# ------------------------------------------------------------------------------------------- bump


def _bump_time(row: dict[str, Any]) -> dict[str, Any] | None:
    """Section 5.5: a held or walked row gains one step of time or distance."""
    progression = _progression(row)
    seconds, time_step = row.get("seconds"), progression.get("time_step_s")
    if seconds is not None and isinstance(time_step, int) and time_step > 0:
        return _replace(row, seconds=int(seconds) + time_step)
    meters, distance_step = row.get("meters"), progression.get("distance_step_m")
    if meters is not None and isinstance(distance_step, int) and distance_step > 0:
        return _replace(row, meters=round(float(meters) + distance_step, 1))
    return None


def _bump_load(row: dict[str, Any], ladder: Sequence[float], *, reset_reps: bool) -> dict[str, Any] | None:
    """One rung up, re-rounded then clamped. ``None`` when the ladder has nothing higher.

    Section 8.5: a target above the heaviest legal rung does not silently stay put wearing a
    ``double_progression`` label. The row becomes a rep progression, so the next bump adds reps
    rather than asking again for a weight the household does not own.
    """
    load, step = row.get("load_kg"), _load_step(row)
    if not isinstance(load, int | float) or step is None:
        return None
    landed = _rung(row, float(load) + step, ladder)
    if landed is None or landed <= float(load) + 1e-9:
        return None
    if reset_reps and row.get("reps") is not None:
        return _replace(row, load_kg=landed, reps=_rep_floor(row))
    return _replace(row, load_kg=landed)


def _exhausted_loaded_row(row: dict[str, Any]) -> dict[str, Any]:
    """A loaded row with no heavier rung left becomes a rep progression (section 8.5)."""
    kind = "time_progression" if row.get("seconds") is not None or row.get("meters") is not None else "rep_progression"
    return _with_progression(row, type=kind)


def _bump_adult(
    row: dict[str, Any], ladder: Sequence[float], rules: YouthRuleSet | None, resolve: Resolver | None
) -> dict[str, Any]:
    kind = str(_progression(row).get("type") or "rep_progression")
    reps, ceiling = row.get("reps"), _rep_ceiling(row, rules)

    if kind == "linear_load":
        return _bump_load(row, ladder, reset_reps=False) or _exhausted_loaded_row(row)
    if kind == "time_progression":
        return _bump_time(row) or row
    if kind == "double_progression" and reps is not None:
        if ceiling is None or int(reps) < ceiling:
            return _replace(row, reps=int(reps) + 1)
        return _bump_load(row, ladder, reset_reps=True) or _exhausted_loaded_row(row)
    if reps is not None:
        if ceiling is None or int(reps) < ceiling:
            return _replace(row, reps=int(reps) + 1)
        # rep_progression at its ceiling: take the harder movement at its rep floor.
        return _swap(row, _linked(row, "progression_of", resolve), reps=_rep_floor(row)) or row
    return _bump_time(row) or row


def _bump_youth(
    row: dict[str, Any], ladder: Sequence[float], rules: YouthRuleSet | None, resolve: Resolver | None
) -> dict[str, Any]:
    """Section 5.6's four steps, in order, first applicable wins. Load is step four."""
    reps, ceiling = row.get("reps"), _rep_ceiling(row, rules)
    if reps is not None and (ceiling is None or int(reps) < ceiling):
        return _replace(row, reps=int(reps) + 1)
    stepped = _bump_time(row)
    if stepped is not None:
        return stepped
    swapped = _swap(row, _linked(row, "progression_of", resolve), reps=_rep_floor(row))
    if swapped is not None:
        return swapped
    return _bump_load(row, ladder, reset_reps=False) or row


# ---------------------------------------------------------------------------------------- regress


def _regress(row: dict[str, Any], ladder: Sequence[float], resolve: Resolver | None) -> dict[str, Any]:
    """Load down a step, else reps or seconds down, else the easier movement (section 5.5)."""
    load, step = row.get("load_kg"), _load_step(row)
    if isinstance(load, int | float) and step is not None and ladder:
        floor = min(ladder)
        target = float(load) - step
        if target >= floor - 1e-9:
            landed = _rung(row, target, ladder)
            if landed is not None and landed < float(load) - 1e-9:
                return _replace(row, load_kg=landed)

    progression = _progression(row)
    reps, rep_floor = row.get("reps"), _rep_floor(row)
    if reps is not None and int(reps) > rep_floor:
        shed = int(progression.get("regress_reps") or 2)
        return _replace(row, reps=max(rep_floor, int(reps) - shed))

    seconds, time_step = row.get("seconds"), progression.get("time_step_s")
    if seconds is not None and isinstance(time_step, int) and time_step > 0:
        floor_s = time_step * SECONDS_FLOOR_MULTIPLIER
        if int(seconds) - time_step >= floor_s:
            return _replace(row, seconds=int(seconds) - time_step)

    return _swap(row, _linked(row, "regression_of", resolve), reps=rep_floor) or row


# ----------------------------------------------------------------------------------------- deload


def _rpe_only(row: dict[str, Any]) -> dict[str, Any]:
    """The part of a deload the week scheme has not already applied.

    ``materialise_rows(template, 4, ...)`` builds a week-4 session at section 6.2's deload shape
    already - two sets, the week-1 reps, the week-1 carry distance - so running section 5.5's
    arithmetic over those rows again scales what is already scaled. Sets and reps survive it
    (``max(2, floor(2 x 0.6)) == 2``, and ``rep_min`` *is* the week-1 value), but distance
    compounds: a 30 m carry became 18 m. Only the RPE cap is genuinely left to apply (D-218).
    """
    rpe = row.get("rpe_target")
    return _replace(row, rpe_target=min(int(rpe), DELOAD_RPE_CAP)) if rpe is not None else dict(row)


def _deload(row: dict[str, Any]) -> dict[str, Any]:
    """Week 4 (section 5.5): fewer sets, reps back to the floor, the same weight, RPE 6."""
    progression = _progression(row)
    pct = float(progression.get("deload_pct") or 0.60)
    sets = int(row.get("sets") or 1)
    changes: dict[str, Any] = {"sets": max(MIN_DELOAD_SETS, math.floor(sets * pct)) if sets > 1 else MIN_SETS}
    if row.get("reps") is not None:
        changes["reps"] = _rep_floor(row)
    if row.get("meters") is not None:
        changes["meters"] = round(float(row["meters"]) * pct, 1)
    rpe = row.get("rpe_target")
    if rpe is not None:
        changes["rpe_target"] = min(int(rpe), DELOAD_RPE_CAP)
    return _replace(row, **changes)


# -------------------------------------------------------------------------------------- the API


def apply_outcome(
    row: dict[str, Any],
    outcome: Outcome,
    rules: YouthRuleSet | None = None,
    ladder: Sequence[float] = (),
    *,
    resolve: Resolver | None = None,
    earned_bump: bool = False,
    pre_scheduled: bool = False,
) -> dict[str, Any]:
    """One row, one outcome, a new row. Never mutates its argument.

    ``rules`` is the band's rule set, or ``None`` for an adult. ``ladder`` is the rungs available
    for this row's implement. ``resolve`` turns an exercise id into a movement this profile may
    legally be given; without it the two swap steps of sections 5.5 and 5.6 simply do not fire and
    the row holds, which is the safe direction.

    ``allow_load_progression`` chooses the bump order (section 5.6). It is false for every youth
    band, set by ``progression_state`` at build time, so the youth path is reached by the row's own
    data rather than by a caller remembering to say so.

    ``pre_scheduled`` says the row was already built at the week it is being judged for, which is
    always true of a stored ``planned_session`` row and never true of one a test hands over. It
    only changes ``deload``: see ``_rpe_only`` (D-218).
    """
    if row.get("is_prelude"):
        # Section 2: the prelude is fixed. It is not progressed and not autoregulated.
        return dict(row)

    progression = _progression(row)
    loaded_ok = progression.get("allow_load_progression", True) is not False

    if outcome == "deload":
        moved = _rpe_only(row) if pre_scheduled else _deload(row)
        return _finish(moved, "deload", pending_bump=earned_bump or bool(row.get("pending_bump")))
    if outcome == "bump":
        moved = _bump_adult(row, ladder, rules, resolve) if loaded_ok else _bump_youth(row, ladder, rules, resolve)
        return _finish(moved, "bump", pending_bump=False)
    if outcome == "regress":
        return _finish(_regress(row, ladder, resolve), "regress", pending_bump=False)
    return _finish(dict(row), "hold", pending_bump=bool(row.get("pending_bump")))


def _finish(row: dict[str, Any], outcome: Outcome, *, pending_bump: bool) -> dict[str, Any]:
    return _replace(row, last_outcome=outcome, pending_bump=pending_bump)


def consume_pending_bump(
    row: dict[str, Any],
    rules: YouthRuleSet | None = None,
    ladder: Sequence[float] = (),
    *,
    resolve: Resolver | None = None,
) -> dict[str, Any]:
    """Apply a bump week 4 suppressed, and clear the flag (section 5.4).

    Called for week 1 of the next block only. A flag that is not set returns the row untouched, so
    this is safe to run over every row of a week-1 session.
    """
    if not row.get("pending_bump"):
        return dict(row)
    bumped = apply_outcome(row, "bump", rules, ladder, resolve=resolve)
    return _replace(bumped, pending_bump=False)


__all__ = ["Resolver", "apply_outcome", "consume_pending_bump"]
