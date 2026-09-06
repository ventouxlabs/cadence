"""Build-time substitution: anchors, youth bands, missing implements.

Principles section 3.3 and D-019 are explicit that the program engine *clamps and substitutes*
where the validator *rejects*. A row that cannot be served as written becomes a legal row, or it
is dropped - it never reaches the screen illegal, and it never blocks the session from rendering.
"""

from __future__ import annotations

from collections.abc import Container
from dataclasses import dataclass

from cadence.bibliotheque.bundle import LibraryBundle
from cadence.bibliotheque.template import ColumnSpec, ProfileKind
from cadence.schema.enums import AgeBand, Equipment, ExerciseTag, LoadType, LoadUnit, Measure
from cadence.schema.exercise import Exercise
from cadence.schema.youth import YouthRuleSet
from cadence.validateur.youth_rules import usable_bodyweight, youth_allows

# Principles section 1.7. Neither substitution counts as a regression for progression purposes.
ANCHOR_SUBSTITUTES: dict[str, str] = {
    "dead-hang": "db-floor-pullover",
    "scap-pull-hang": "db-floor-pullover",
    "inverted-row": "bench-supported-db-row",
}

# Section 3.6: the 16 kg bell two-handed, and only above this bodyweight.
KETTLEBELL_MIN_BODYWEIGHT_KG = 45.7
KETTLEBELL_YOUTH_KG = 16.0
KETTLEBELL_FALLBACK = "kb-deadlift"
KETTLEBELL_LAST_RESORT = "hip-hinge-bw"

# Used when a substitute is measured differently from the row it replaces.
DEFAULT_BY_MEASURE: dict[Measure, float] = {
    Measure.REPS: 10,
    Measure.SECONDS: 30,
    Measure.METERS: 20,
    Measure.STEPS: 20,
}


@dataclass(frozen=True, slots=True)
class BandContext:
    """Everything a substitution decision needs, and nothing it can write to."""

    kind: ProfileKind
    band: AgeBand
    rules: YouthRuleSet | None
    equipment: frozenset[Equipment]
    has_overhead_anchor: bool
    bodyweight_kg: float | None
    library: LibraryBundle

    @property
    def is_youth(self) -> bool:
        return self.kind == "youth"


def has_equipment(exercise: Exercise, ctx: BandContext) -> bool:
    return all(item in ctx.equipment for item in exercise.equipment)


def needs_missing_anchor(exercise: Exercise, ctx: BandContext) -> bool:
    return ExerciseTag.REQUIRES_ANCHOR in exercise.tags and not ctx.has_overhead_anchor


def youth_legal(exercise: Exercise, ctx: BandContext) -> bool:
    """Whether a youth profile at this band may be offered this movement at all.

    The section 9 allowlist, the band's load types and the band's banned tags together - the same
    three the validator checks, so the engine never offers a row V1, V8 or V14 would reject.
    """
    if ctx.rules is None:
        return True
    if not youth_allows(exercise, ctx.band, bodyweight_kg=ctx.bodyweight_kg, profile_kind="youth"):
        return False
    if exercise.load_type not in ctx.rules.allowed_load_types:
        return False
    return not any(tag in ctx.rules.banned_tags for tag in exercise.tags)


def is_offerable(exercise: Exercise, ctx: BandContext) -> bool:
    """Whether this movement can appear in this profile's session as it stands."""
    if not has_equipment(exercise, ctx) or needs_missing_anchor(exercise, ctx):
        return False
    return youth_legal(exercise, ctx) if ctx.is_youth else True


def kettlebell_swap(exercise: Exercise, ctx: BandContext) -> str | None:
    """Principles section 3.6's kettlebell chain, as an exercise id or ``None`` to keep it.

    The gate is bodyweight, not age alone: a weighed teenager over 45.7 kg keeps the 16 kg bell,
    a lighter one drops to ``kb-deadlift``, and an unweighed one gets the bodyweight hinge -
    section 3.6 says an unknown bodyweight is the substitution case, not the light-bell case.
    """
    if not ctx.is_youth or exercise.load_type is not LoadType.KETTLEBELL:
        return None
    # Section 3.6 is about the bell held in two hands. A one-handed bell has no legal youth
    # weight at any band, so it takes the ordinary easier-movement chain instead.
    if exercise.load_unit is not LoadUnit.PER_IMPLEMENT:
        return None
    weight = usable_bodyweight(ctx.bodyweight_kg)
    if weight is None:
        return KETTLEBELL_LAST_RESORT
    if ctx.band is AgeBand.AGE_14_17 and weight >= KETTLEBELL_MIN_BODYWEIGHT_KG:
        return None
    # A weighed youth who is under the gate gets the deadlift, per section 3.6. Whether the 16 kg
    # bell then fits the band cap is the load stage's decision, and section 8.5 demotes the row to
    # a bodyweight movement when it does not.
    return KETTLEBELL_FALLBACK if exercise.id != KETTLEBELL_FALLBACK else None


def _candidates(exercise: Exercise, column: ColumnSpec, ctx: BandContext) -> list[str]:
    """Substitutes to try, in a fixed order: section 1.7, the row's own fallback, u10, 3.6, easier.

    ``fallback_exercise`` comes before the easier-movement chain because it is the author's own
    answer to "the implement is missing" (section 8.5) - `lower-full-b` names `db-rdl` for the
    kettlebell deadlift, and a household with no bells should get that rather than whatever the
    progression graph happens to reach.
    """
    ordered: list[str] = []
    if needs_missing_anchor(exercise, ctx) and exercise.id in ANCHOR_SUBSTITUTES:
        ordered.append(ANCHOR_SUBSTITUTES[exercise.id])
    if column.load_rule.fallback_exercise:
        ordered.append(column.load_rule.fallback_exercise)
    if column.u10_sub:
        ordered.append(column.u10_sub)
    swap = kettlebell_swap(exercise, ctx)
    if swap is not None:
        ordered.append(swap)
    ordered.extend(ctx.library.easier_than.get(exercise.id, ()))
    seen: set[str] = set()
    return [item for item in ordered if item in ctx.library.exercises and not (item in seen or seen.add(item))]


def _acceptable(candidate: Exercise, ctx: BandContext, taken: Container[str]) -> bool:
    """Legal for this profile, and not already prescribed elsewhere in the same session."""
    if candidate.id in taken:
        return False
    return is_offerable(candidate, ctx) and kettlebell_swap(candidate, ctx) in (None, candidate.id)


def resolve_exercise(
    exercise: Exercise,
    column: ColumnSpec,
    ctx: BandContext,
    taken: Container[str] = (),
) -> tuple[Exercise | None, list[str]]:
    """The movement this row will actually prescribe, plus any user-facing notes.

    Returns ``(None, notes)`` when nothing legal is reachable, which drops the row. Walks the
    substitution graph breadth-first with a visited set, so a cycle in the library links cannot
    hang the build.

    ``taken`` is the set of exercise ids already prescribed in this session. Without it two rows
    whose implements are both missing collapse onto the same substitute - a bodyweight household
    got `upper-a` with `prone-ytw-raise` twice - which reads as a bug to the person doing it and
    wastes one of five slots.
    """
    notes: list[str] = []
    original = exercise
    # Sections 10.6 and 10.9 name the u10 substitute unconditionally, not only when the movement
    # it replaces is illegal: at that band the row simply is the substitute.
    if ctx.band is AgeBand.U10 and column.u10_sub and column.u10_sub in ctx.library.exercises:
        exercise = ctx.library.exercises[column.u10_sub]
        if exercise.id != original.id:
            notes.append(_swap_note(original, exercise, ctx))
    if _acceptable(exercise, ctx, taken):
        return exercise, notes
    queue = [(exercise, column)]
    visited = {original.id, exercise.id}
    while queue:
        current, spec = queue.pop(0)
        for candidate_id in _candidates(current, spec, ctx):
            if candidate_id in visited:
                continue
            visited.add(candidate_id)
            candidate = ctx.library.exercises[candidate_id]
            if _acceptable(candidate, ctx, taken):
                notes.append(_swap_note(exercise, candidate, ctx))
                return candidate, notes
            queue.append((candidate, spec))
    return None, notes


def bodyweight_alternative(exercise: Exercise, ctx: BandContext, taken: Container[str]) -> Exercise | None:
    """A movement that needs no weight, for a row whose implement has no legal rung (section 8.5).

    Walks the easier-than graph breadth-first rather than one hop: the chain out of a loaded
    movement can pass through another loaded one before it reaches the floor.

    Section 9's links do not always get there at all - the ``push_h`` graph is two disconnected
    components, so ``db-bench-press`` reaches ``db-floor-press`` and stops, with ``push-up`` in the
    other half - so the pattern is the second resort, exactly as it is for a main row that runs out
    of substitutes. Without it a household whose ``weights_available`` is blank or unparsable lost
    the row, and with it the main row, and the build refused.
    """
    queue = [exercise.id]
    visited = {exercise.id}
    while queue:
        for candidate_id in ctx.library.easier_than.get(queue.pop(0), ()):
            if candidate_id in visited:
                continue
            visited.add(candidate_id)
            candidate = ctx.library.exercises[candidate_id]
            if candidate.load_unit is LoadUnit.BODYWEIGHT and _acceptable(candidate, ctx, taken):
                return candidate
            queue.append(candidate_id)
    return pattern_pool(exercise, ctx, taken, bodyweight_only=True)


def pattern_pool(
    exercise: Exercise, ctx: BandContext, taken: Container[str], *, bodyweight_only: bool = False
) -> Exercise | None:
    """The best remaining movement in the same pattern, when the substitution chain runs dry.

    Section 6.1 describes a day type by its pattern, not by one exercise id, so a main row whose
    whole chain is unavailable is filled from the pattern rather than dropped - a bodyweight-only
    household needs `upper-a` to open on a push, and `db-bench-press` reaches only `db-floor-press`
    before the graph ends.

    Ordered by how many easier variants a movement has, which puts the hardest legal option first,
    then by id so the choice never depends on dictionary order. Prelude movements and the measured
    assessment tests are excluded: promoting `glute-bridge` to a main lift would both mis-describe
    the session and slip past the youth exercise-count cap, which ignores prelude rows (D-053).
    """
    same_region = [
        candidate
        for candidate in ctx.library.exercises.values()
        if candidate.pattern is exercise.pattern
        and candidate.region is exercise.region
        and not candidate.is_prelude
        and ExerciseTag.ASSESSMENT_ONLY not in candidate.tags
        and (not bodyweight_only or candidate.load_unit is LoadUnit.BODYWEIGHT)
        and _acceptable(candidate, ctx, taken)
    ]
    if not same_region:
        return None
    return sorted(same_region, key=lambda item: (-len(ctx.library.easier_than.get(item.id, ())), item.id))[0]


def _swap_note(original: Exercise, chosen: Exercise, ctx: BandContext) -> str:
    if needs_missing_anchor(original, ctx):
        return f"No overhead bar, so {chosen.name.lower()} replaces {original.name.lower()}."
    if ctx.is_youth:
        return f"{chosen.name} replaces {original.name.lower()} at this age."
    return f"{chosen.name} replaces {original.name.lower()} with the kit you have."


def retarget(column: ColumnSpec, original: Exercise, chosen: Exercise) -> ColumnSpec:
    """Move the prescription onto the substitute's own measure.

    A swap can cross measures - ``dead-hang`` is timed, ``db-floor-pullover`` is counted - and a
    row prescribed in the wrong measure fails the validator with an error that reads like a typo.
    The number does not survive the crossing; the substitute's own default does.
    """
    if original.measure is chosen.measure:
        return column
    blank: dict[str, float | None] = {"reps": None, "seconds": None, "meters": None, "steps": None}
    value = DEFAULT_BY_MEASURE[chosen.measure]
    blank[chosen.measure.value] = int(value) if chosen.measure is not Measure.METERS else float(value)
    return column.model_copy(update=blank)
