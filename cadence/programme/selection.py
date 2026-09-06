"""Which resolved rows a session keeps, and what it drops when it will not fit.

Two separate jobs, both pure over already-resolved rows. Section 6.4's budget decides how many
rows a session of a given length carries; the band caps of section 3.2 then override that count
and the clock alike (P1), because a youth session that does not fit is trimmed, never shipped.
"""

from __future__ import annotations

from cadence.bibliotheque.template import RowRole, WorkoutTemplate
from cadence.programme.context import BuildContext, Candidate
from cadence.programme.prescription import band_minutes
from cadence.programme.schemes import row_budget
from cadence.schema.enums import DayType, ExerciseTag
from cadence.validateur.youth_rules import counts_toward_exercise_cap

MIN_YOUTH_ROWS = 3

ROLE_RANK: dict[RowRole, int] = {"prelude": -1, "main": 0, "assessment": 0, "secondary": 1, "finisher": 2}


def row_count(template: WorkoutTemplate, ctx: BuildContext) -> int:
    """How many training rows this session carries (section 6.4, then section 6.5 for a youth)."""
    total = row_budget(ctx.settings.session_minutes).total
    if ctx.is_youth and ctx.rules is not None:
        return max(MIN_YOUTH_ROWS, min(total - 1, ctx.rules.max_exercises_per_session))
    return total


def budget_pick(candidates: list[Candidate], template: WorkoutTemplate, ctx: BuildContext) -> list[Candidate]:
    """The rows this session carries, truncated to the session-length budget (section 6.4).

    Selection runs over *resolved* candidates, never over the template: a row whose implement is
    missing has already been substituted or has already fallen out, so the budget is spent on rows
    that will actually appear. Choosing indices from the template first and dropping the failures
    afterwards is what left a bodyweight household with three rows instead of five.

    The assessment battery is never truncated: its six rows are the session. Everything else keeps
    one main, fills from secondary in priority order and always ends on a finisher.
    """
    if template.day_type is DayType.ASSESSMENT:
        return candidates

    budget = row_budget(ctx.settings.session_minutes)
    total = row_count(template, ctx)

    mains = [item for item in candidates if item.draft.role == "main"]
    finishers = [item for item in candidates if item.draft.role == "finisher"]
    secondaries = [item for item in candidates if item.draft.role not in ("main", "finisher")]

    want_main = min(budget.main, len(mains), total)
    remaining = total - want_main
    want_finisher = min(budget.finisher, len(finishers), remaining)
    if finishers and want_finisher == 0 and remaining > 0:
        want_finisher = 1
    want_secondary = max(remaining - want_finisher, 0)

    picked = [*mains[:want_main], *secondaries[:want_secondary], *finishers[:want_finisher]]
    if len(picked) < total:
        chosen = {item.index for item in picked}
        picked += [item for item in candidates if item.index not in chosen][: total - len(picked)]

    return picked


def order(picked: list[Candidate]) -> list[Candidate]:
    """Main first, then secondaries in template order, then the finisher (section 6.1)."""
    return sorted(picked, key=lambda item: (ROLE_RANK[item.draft.role], item.index))


def fit_to_band(rows: list[dict], ctx: BuildContext, *, assessment: bool = False) -> list[dict]:
    """Drop the lowest-priority rows until a youth session fits the band's caps (V6 and V9's remedy).

    Two caps, both from principles section 3.2 and both overriding the section 6.4 row count (P1):
    the clock, and the number of exercises. The clock is the one V9 will use, not a second opinion
    (see ``band_minutes``), and the count is V6's own - resolved-exercise `is_prelude`, not the
    row's role, so it agrees with the gate on the three movements D-053 made special.

    Drop order: challenge rows first, since P5 lets one sit above the row count only while it fits
    the caps, then the lowest-priority secondary. The main row, the finisher, the prelude and the
    last remaining `play` row are never the ones dropped.
    """
    if not ctx.is_youth or ctx.rules is None:
        return rows
    while over_band_caps(rows, ctx, assessment=assessment):
        victim = next_to_drop(rows, ctx)
        if victim is None:
            break
        rows = [row for row in rows if row is not victim]
        for position, row in enumerate(rows, start=1):
            row["position"] = position
    return rows


def counted_rows(rows: list[dict], ctx: BuildContext) -> list[dict]:
    """The rows V6 counts: everything whose resolved exercise is not a prelude movement."""
    return [row for row in rows if counts_toward_exercise_cap(ctx.library.exercises.get(row["exercise_id"]))]


def over_band_caps(rows: list[dict], ctx: BuildContext, *, assessment: bool) -> bool:
    rules = ctx.rules
    if rules is None:
        return False
    if band_minutes(rows, ctx, assessment=assessment) > ctx.effective_minutes:
        return True
    return len(counted_rows(rows, ctx)) > rules.max_exercises_per_session


def next_to_drop(rows: list[dict], ctx: BuildContext) -> dict | None:
    body = [row for row in rows if not row["is_prelude"]]
    if len(body) <= MIN_YOUTH_ROWS:
        return None
    challenges = [row for row in body if row.get("is_challenge")]
    if challenges:
        return challenges[-1]
    plays = [row for row in body if row_is_play(row, ctx)]
    droppable = [row for row in body if row["role"] == "secondary" and not (row_is_play(row, ctx) and len(plays) <= 1)]
    return droppable[-1] if droppable else None


def row_is_play(row: dict, ctx: BuildContext) -> bool:
    exercise = ctx.library.exercises.get(row["exercise_id"])
    return exercise is not None and ExerciseTag.PLAY in exercise.tags
