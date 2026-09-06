"""Compiling a ``WorkoutTemplate`` into concrete rows for one profile and one block week.

``materialise_rows`` returns the exact ``planned_session.rows_json`` shape; ``compile_workout``
wraps the same rows in a PRP-00 ``Workout`` so the validator can judge them. Both are pure: no
clock, no randomness, no database.

Substitution, load resolution and the row shape live here. Which rows survive the section 6.4
budget and the band caps lives in ``cadence.programme.selection``; the week scheme and the band
clamps live in ``cadence.programme.prescription``.
"""

from __future__ import annotations

from typing import Any

from cadence.bibliotheque.bundle import LibraryBundle
from cadence.bibliotheque.template import ColumnSpec, LoadRule, TemplateRow, WorkoutTemplate
from cadence.profils.settings import ProgramSettings
from cadence.profils.tables import Profile
from cadence.programme import substitution as sub
from cadence.programme.bands import effective_cap
from cadence.programme.context import MEASURE_FIELD, BuildContext, Candidate, Draft, make_context
from cadence.programme.errors import ProgramBuildError
from cadence.programme.ladder import round_to_available
from cadence.programme.prescription import (
    assessment_value,
    estimated_minutes,
    prescribe,
    progression_state,
)
from cadence.programme.selection import budget_pick, fit_to_band, order
from cadence.schema.enums import DayType, ExerciseTag, LoadType, LoadUnit, Measure
from cadence.schema.exercise import Exercise
from cadence.schema.workout import Workout

# Used when a ladder row names no target at all.
DEFAULT_LADDER_FRACTION = 0.5


# --------------------------------------------------------------------------------------- selection


def _select(template: WorkoutTemplate, ctx: BuildContext) -> list[Candidate]:
    """Resolve every row this profile could be given, then keep the ones that fit.

    Three steps in order: substitute and price the candidates, spend section 6.4's budget on the
    survivors, and guarantee the son a fun row. Selection never sees the template's unresolved
    rows, which is what kept a household missing an implement from getting a short session.
    """
    candidates = _resolve_candidates(template, ctx)
    picked = budget_pick(candidates, template, ctx)
    return order(_ensure_play_row(picked, candidates, template, ctx))


def _ensure_play_row(
    picked: list[Candidate], candidates: list[Candidate], template: WorkoutTemplate, ctx: BuildContext
) -> list[Candidate]:
    """Section 6.5: a youth session carries at least one ``play`` row.

    Scoped to youth templates. On a ``both`` workout section 10.9 fixes the exercise ids across
    the two columns, and adding a row for one of them would break that. The guarantee may take the
    session one row over the section 6.4 count, never over ``max_exercises_per_session`` (P5).
    """
    if not ctx.is_youth or ctx.rules is None or template.target_profile_kind != "youth":
        return picked
    if any(_is_play(item.draft.exercise) for item in picked):
        return picked
    if len(picked) >= ctx.rules.max_exercises_per_session:
        return picked
    chosen = {item.index for item in picked}
    extra = next((item for item in candidates if _is_play(item.draft.exercise) and item.index not in chosen), None)
    if extra is not None:
        return [*picked, extra]
    taken = {item.draft.exercise.id for item in picked}
    pool = [item for item in ctx.library.play_pool() if sub.is_offerable(item, ctx.band) and item.id not in taken]
    if not pool:
        return picked
    synthetic = _draft_row(_play_row(pool[0]), ctx, taken)
    if synthetic is None:
        return picked
    return [*picked, Candidate(index=len(candidates), draft=synthetic, load_kg=None, cap_kg=None)]


def _is_play(exercise: Exercise) -> bool:
    return ExerciseTag.PLAY in exercise.tags


def _play_row(exercise: Exercise) -> TemplateRow:
    """A minimal fun row drawn from the section 6.5 pool when the template offers none."""
    field = MEASURE_FIELD[exercise.measure]
    value: float = 10.0 if exercise.measure is Measure.METERS else 10
    doc = {"exercise": exercise.id, "role": "secondary", "sets": 2, "rest_s": 45, field: value}
    return TemplateRow.model_validate(doc)


# ------------------------------------------------------------------------------------------ drafts


def _draft_row(row: TemplateRow, ctx: BuildContext, taken: set[str]) -> Draft | None:
    """Resolve one template row to the movement it will actually prescribe.

    ``taken`` carries the ids already spoken for in this session, so two rows cannot collapse onto
    the same substitute. It covers the training block only: the prelude is a fixed warm-up flow
    that section 2 runs whole, and section 10.11 deliberately lists `open-book` in the extras of a
    workout whose prelude already contains it.
    """
    column = row.column(ctx.band.kind)
    origin = ctx.library.exercises.get(row.exercise)
    if origin is None:
        return None
    if row.anchor_alt is not None and ctx.band.has_overhead_anchor:
        alt = ctx.library.exercises.get(row.anchor_alt.exercise)
        if alt is not None:
            column = _apply_anchor_alt(column, row.anchor_alt)
            column = sub.retarget(column, origin, alt)
            origin = alt
    if row.role == "assessment" and sub.needs_missing_anchor(origin, ctx.band):
        # Section 1.7: without an anchor the dead hang is recorded `unavailable`, not zero and
        # not a gap. A substitute would be a different test, so the row goes rather than changes.
        return None
    chosen, notes = sub.resolve_exercise(origin, column, ctx.band, taken)
    if chosen is None and row.role == "main":
        # Section 6.1 names a day type by its pattern, not by one id. A compound row whose whole
        # chain is unavailable is refilled from the pattern rather than leaving a session with no
        # main lift at all.
        chosen = sub.pattern_pool(origin, ctx.band, taken)
        if chosen is not None:
            notes.append(f"{chosen.name} takes the main lift: {origin.name.lower()} needs kit you do not have.")
    if chosen is None:
        return None
    return Draft(
        role=row.role,
        origin_id=row.exercise,
        exercise=chosen,
        column=sub.retarget(column, origin, chosen),
        notes=tuple(notes),
        assessment_id=row.assessment_id,
    )


def _resolve_candidates(template: WorkoutTemplate, ctx: BuildContext) -> list[Candidate]:
    """Every training row this profile could be given, substituted and priced, in template order.

    Loads are resolved here rather than after selection so a row's weight does not change with the
    session length: ``relative_to`` reads the press load whether or not the press made the cut.
    """
    resolved: dict[str, float] = {}
    taken: set[str] = set()
    # A *substitute* must not land on a prelude movement - a session that warms up with the glute
    # bridge and then trains it reads as a bug, and the row would not count toward the youth
    # exercise cap either (D-053). A row that names one outright still gets it: section 10.11 puts
    # `open-book` in the extras of a workout whose prelude already contains it, deliberately.
    prelude_ids = {item.draft.exercise.id for item in _prelude(template, ctx)}
    candidates: list[Candidate] = []
    for index, row in enumerate(template.all_rows()):
        draft = _draft_row(row, ctx, taken | (prelude_ids - {row.exercise}))
        if draft is None:
            continue
        load_kg, cap_kg = _resolve_load(draft, ctx, resolved)
        if load_kg is None and _needs_a_load(draft):
            draft = _demote_to_bodyweight(draft, ctx, taken)
            if draft is None:
                continue
            load_kg, cap_kg = _resolve_load(draft, ctx, resolved)
        if load_kg is not None:
            resolved[draft.origin_id] = load_kg
        taken.add(draft.exercise.id)
        candidates.append(Candidate(index=index, draft=draft, load_kg=load_kg, cap_kg=cap_kg))
    return candidates


def _require_main(template: WorkoutTemplate, picked: list[Candidate], ctx: BuildContext) -> None:
    """A day type that declares a compound row must ship with one.

    Only a template that names a ``main`` row is held to this: section 6.1 gives `mobility_carry`
    and `assessment` no compound at all, and neither is missing anything by not having one.
    """
    if not any(row.role == "main" for row in template.all_rows()):
        return
    if any(item.draft.role == "main" for item in picked):
        return
    raise ProgramBuildError(f"{template.id} has no legal main row for profile {ctx.profile.id!r}", [_household(ctx)])


def _household(ctx: BuildContext) -> str:
    """The facts that decided a build, for an error a person can act on."""
    return (
        f"equipment {sorted(item.value for item in ctx.settings.equipment)}, "
        f"weights {ctx.settings.weights_available!r}, band {ctx.band.band.value}, "
        f"anchor {ctx.band.has_overhead_anchor}"
    )


def _require_rows(template: WorkoutTemplate, rows: list[dict], ctx: BuildContext) -> None:
    """A session with no rows at all is a configuration error, not a workout.

    Reached when the equipment list excludes ``bodyweight``, which drops the prelude and every
    bodyweight substitute with it. PRP-03's Settings screen is where that becomes unselectable;
    until then it fails here by name rather than as a schema error about an empty tuple.
    """
    if rows:
        return
    raise ProgramBuildError(
        f"{template.id} resolved to no rows at all for profile {ctx.profile.id!r}", [_household(ctx)]
    )


def _apply_anchor_alt(column: ColumnSpec, alt: Any) -> ColumnSpec:
    update = {
        key: getattr(alt, key)
        for key in ("sets", "reps", "seconds", "meters", "steps", "rest_s", "load_rule")
        if getattr(alt, key) is not None
    }
    if any(key in update for key in ("reps", "seconds", "meters", "steps")):
        for key in ("reps", "seconds", "meters", "steps"):
            update.setdefault(key, None)
    return column.model_copy(update=update)


# -------------------------------------------------------------------------------------------- load


def _target_kg(rule: LoadRule, ctx: BuildContext, load_type: LoadType, resolved: dict[str, float]) -> float | None:
    heaviest = ctx.weights.heaviest(load_type)
    if rule.mode == "fixed_kg":
        return rule.kg
    if rule.relative_to is not None:
        base = resolved.get(rule.relative_to)
        if base is not None:
            return base * (rule.pct or 1.0)
    if rule.kg is not None:
        return min(rule.kg, heaviest) if (rule.prefer == "heaviest_rung" and heaviest is not None) else rule.kg
    if heaviest is None:
        return None
    if rule.pct_of_heaviest is not None:
        return heaviest * rule.pct_of_heaviest
    if rule.prefer == "heaviest_rung":
        return heaviest
    return heaviest * DEFAULT_LADDER_FRACTION


def _resolve_load(draft: Draft, ctx: BuildContext, resolved: dict[str, float]) -> tuple[float | None, float | None]:
    """This row's load in kg and the youth cap in force, or ``(None, cap)`` for a bodyweight row."""
    rule = draft.column.load_rule
    exercise = draft.exercise
    cap = effective_cap(ctx.rules, exercise.load_unit, ctx.band.bodyweight_kg) if ctx.rules is not None else None
    if rule.mode == "bodyweight" or exercise.load_unit is LoadUnit.BODYWEIGHT:
        return None, cap
    ladder = ctx.weights.ladder(exercise.load_type)
    target = _target_kg(rule, ctx, exercise.load_type, resolved)
    if target is None:
        return None, cap
    if rule.max_kg is not None:
        target = min(target, rule.max_kg)
    return round_to_available(target, ladder, cap), cap


def _row_dict(
    position: int,
    draft: Draft,
    week: int,
    load_kg: float | None,
    cap_kg: float | None,
    ctx: BuildContext,
) -> dict[str, Any]:
    prescribed = assessment_value(draft, prescribe(draft, week, load_kg, cap_kg, ctx), ctx)
    week_one = assessment_value(draft, prescribe(draft, 1, load_kg, cap_kg, ctx), ctx)
    exercise = draft.exercise
    cue = draft.column.cue_override or exercise.cue
    if draft.column.per_side:
        cue = f"{cue} Each side."
    return {
        "position": position,
        "exercise_id": exercise.id,
        "role": draft.role,
        "name": exercise.name,
        "cue": cue[:120],
        "sets": prescribed.sets,
        "reps": prescribed.reps,
        "seconds": prescribed.seconds,
        "meters": prescribed.meters,
        "steps": prescribed.steps,
        "per_side": draft.column.per_side,
        "rest_s": prescribed.rest_s,
        "rpe_target": prescribed.rpe_target,
        "amrap": prescribed.amrap,
        "load_kg": load_kg,
        "load_unit": exercise.load_unit.value,
        "measure": exercise.measure.value,
        "garmin_category": exercise.garmin_category.value if exercise.garmin_category else None,
        "is_prelude": draft.role == "prelude",
        "is_challenge": False,
        "assessment_id": draft.assessment_id.value if draft.assessment_id else None,
        "notes": [*draft.notes, *prescribed.notes],
        "progression": progression_state(draft, week_one, cap_kg, ctx),
    }


# ----------------------------------------------------------------------------------------- the API


def materialise_rows(
    template: WorkoutTemplate,
    week: int,
    profile: Profile,
    settings: ProgramSettings,
    library: LibraryBundle,
    prev_rows: list[dict] | None = None,
    challenge_rows: list[dict] | None = None,
) -> list[dict]:
    """Compile one template into ``planned_session.rows_json`` for one profile and week.

    ``prev_rows`` and ``challenge_rows`` are PRP-07's seams: this PRP carries the challenge rows
    through unchanged and does not read the previous session at all.
    """
    ctx = make_context(profile, settings, library)
    selected = _select(template, ctx)
    _require_main(template, selected, ctx)

    rows = [
        _row_dict(position, item.draft, week, item.load_kg, item.cap_kg, ctx)
        for position, item in enumerate(_prelude(template, ctx) + selected, start=1)
    ]
    # Challenge rows join before the trim, not after it: appending past the band caps is exactly
    # what P5 bounds, and a row added after the clock check is a row nothing ever checked.
    for extra in challenge_rows or []:
        rows.append({**extra, "position": len(rows) + 1, "is_challenge": True})
    rows = fit_to_band(rows, ctx, assessment=template.day_type is DayType.ASSESSMENT)
    _require_rows(template, rows, ctx)
    return rows


def _prelude(template: WorkoutTemplate, ctx: BuildContext) -> list[Candidate]:
    """The posture prelude, prepended to every session (section 2, D-051)."""
    if template.prelude is None:
        return []
    prelude = ctx.library.templates.get(template.prelude)
    if prelude is None:
        return []
    candidates: list[Candidate] = []
    for index, row in enumerate(prelude.rows):
        if ctx.is_youth and row.youth_skip:
            continue
        draft = _draft_row(row, ctx, set())
        if draft is not None:
            candidates.append(Candidate(index=index, draft=draft, load_kg=None, cap_kg=None))
    return candidates


def _needs_a_load(draft: Draft) -> bool:
    """Whether this row is meaningless without a weight - a kettlebell with no bell."""
    return draft.column.load_rule.mode != "bodyweight" and draft.exercise.load_unit is not LoadUnit.BODYWEIGHT


def _demote_to_bodyweight(draft: Draft, ctx: BuildContext, taken: set[str]) -> Draft | None:
    """Section 8.5: with no legal rung, take a bodyweight movement, or drop the row.

    Reached when the ladder has nothing at or under the cap - an implement the household does not
    own, a youth cap below the lightest bell, or a ``weights_available`` line that did not parse
    and so left the implement with an empty ladder (section 8.2: ignore the token, warn, fall back
    to bodyweight, never guess). Rendering the movement with no weight would be a different
    exercise wearing its name.
    """
    candidate = sub.bodyweight_alternative(draft.exercise, ctx.band, taken)
    if candidate is None:
        return None
    return Draft(
        role=draft.role,
        origin_id=draft.origin_id,
        exercise=candidate,
        column=sub.retarget(draft.column, draft.exercise, candidate),
        notes=(*draft.notes, f"{candidate.name} replaces {draft.exercise.name.lower()}: no legal weight."),
        assessment_id=draft.assessment_id,
    )


def compile_workout(
    template: WorkoutTemplate,
    week: int,
    profile: Profile,
    settings: ProgramSettings,
    library: LibraryBundle,
) -> Workout:
    """The same rows as a PRP-00 ``Workout``, ready for ``validate_workout``."""
    rows = materialise_rows(template, week, profile, settings, library)
    ctx = make_context(profile, settings, library)
    return workout_from_rows(template, rows, is_youth=ctx.is_youth)


def workout_from_rows(template: WorkoutTemplate, rows: list[dict], *, is_youth: bool) -> Workout:
    """Wrap already-materialised rows as a ``Workout``, without building them again.

    ``build_program`` validates the rows it is about to store, and recompiling to do that would
    both double the work and give the two paths a chance to differ.
    """
    # Section 2: the prelude runs its youth variant for any youth band. A template that names its
    # own variant keeps it - son-play-day is "play", and saying "youth" would lose that.
    variant = template.variant or ("youth" if is_youth else None)
    return Workout.model_validate(
        {
            "id": template.id,
            "name": template.name,
            "day_type": template.day_type.value,
            "target_profile_kind": template.target_profile_kind,
            "variant": variant,
            "estimated_minutes": estimated_minutes(rows),
            "rows": [
                {
                    "exercise_id": row["exercise_id"],
                    "sets": row["sets"],
                    "reps": row["reps"],
                    "seconds": row["seconds"],
                    "meters": row["meters"],
                    "steps": row["steps"],
                    "load_kg": row["load_kg"],
                    "load_unit": row["load_unit"],
                    "rest_s": row["rest_s"],
                    "rpe_target": row["rpe_target"],
                    "amrap": row["amrap"],
                    "is_prelude": row["is_prelude"],
                    "is_challenge": row["is_challenge"],
                    "cue_override": None,
                    "progression_id": None,
                }
                for row in rows
            ],
        }
    )
