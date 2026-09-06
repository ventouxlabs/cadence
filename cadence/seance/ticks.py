"""Ticking a row and adjusting what was actually done.

Both operations are the offline queue's replay targets, so both are idempotent and both are
ordered by the *client's* timestamp: a queued tick that predates the stored ``done_at`` is dropped
rather than resurrecting a state the user has since undone.

Adjust never touches ``done``, and never writes free text: reps step by one and load steps one rung
on the household ladder, clamped to the band's effective cap (principles section 3.3).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
from typing import Any, Literal

from sqlmodel import Session

from cadence.profils.settings import ProgramSettings
from cadence.programme.bands import effective_cap
from cadence.programme.ladder import parse_weights_available
from cadence.schema.enums import LoadType, LoadUnit
from cadence.seance.catalog import library_bundle, require_band_rules
from cadence.seance.tables import SessionRecord, SessionRowRecord
from cadence.seance.today import RowView, SessionView

# Zero is a real answer: "I attempted this and completed none of it". Clamping it up to one
# rewrote an honest number, and the row can still be ticked either way (D-082).
MIN_REPS = 0
MAX_REPS = 200
LOAD_EPSILON = 1e-9

Field = Literal["reps", "load"]


class TickError(ValueError):
    """A row operation the caller may not perform: a distance row, or an unknown field."""


@dataclass(frozen=True, slots=True)
class TickResult:
    """The row after the operation, and anything the user needs told about it."""

    row: RowView
    changed: bool
    notice: str | None = None


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def is_stale(row: SessionRowRecord, ts: str | None) -> bool:
    """Whether this operation is older than the row's last recorded tick.

    Last-write-wins on the client clock, which is what stops a queue drained after a reconnect
    from replaying a tick the user has since untapped. It can only order operations against a
    *stored* ``done_at``: an untick clears that column, so a stale tick arriving after an untick
    is accepted. Closing that needs a column architecture section 3 does not have (D-073).
    """
    stamp = _parse_ts(ts)
    stored = _parse_ts(row.done_at)
    return stamp is not None and stored is not None and stamp < stored


def _stage(db: Session, view: SessionView, record: SessionRowRecord) -> RowView:
    """Queue the write. Committing is the *public* function's job, once, at the end.

    ``apply_patch`` writes up to three columns, and committing per column made a single replayed
    operation three transactions: a crash between them left a row half-applied, and SQLite paid
    three fsyncs for one tap.
    """
    db.add(record)
    spec = next(item.spec for item in view.rows if item.position == record.position)
    return RowView(spec=spec, record=record)


def _start_session(db: Session, record: SessionRecord, when: str) -> None:
    """``started_at`` moves on the first tick and never again (D-024)."""
    if record.started_at is None:
        record.started_at = when
        db.add(record)


def set_done(db: Session, view: SessionView, position: int, done: bool | None, ts: str | None = None) -> TickResult:
    """Tick or untick one row. Replaying the same body is a no-op returning the current row."""
    return _committed(db, _stage_done(db, view, position, done, ts))


def _stage_done(db: Session, view: SessionView, position: int, done: bool | None, ts: str | None) -> TickResult:
    """The tick itself, uncommitted, so ``apply_patch`` can fold it into one transaction."""
    row = view.row(position)
    if row is None:
        raise TickError(f"row {position} is not on this session")
    record = row.record
    if done is None or is_stale(record, ts):
        return TickResult(row=row, changed=False)
    if bool(record.done) == bool(done):
        return TickResult(row=row, changed=False)

    when = ts or now_iso()
    record.done = bool(done)
    record.done_at = when if done else None
    if done:
        _start_session(db, view.record, when)
    return TickResult(row=_stage(db, view, record), changed=True)


def _committed(db: Session, result: TickResult) -> TickResult:
    """Flush one operation's writes. The single commit point for every public entry here."""
    if result.changed:
        db.commit()
    return result


def toggle(db: Session, view: SessionView, position: int, ts: str | None = None) -> TickResult:
    """The HTMX tick: whatever the row is now, make it the other thing."""
    row = view.row(position)
    if row is None:
        raise TickError(f"row {position} is not on this session")
    return set_done(db, view, position, not row.record.done, ts)


# ------------------------------------------------------------------------------------------- adjust


def _ladder(settings: ProgramSettings, load_type_value: str | None) -> list[float]:
    try:
        load_type = LoadType(load_type_value) if load_type_value else None
    except ValueError:
        return []
    if load_type is None:
        return []
    return parse_weights_available(settings.weights_available).ladder(load_type)


def load_cap(view: SessionView, spec: dict[str, Any]) -> float | None:
    """The heaviest load this row may carry, or ``None`` when the band does not cap it.

    The band table is the authority and the cap stored in ``rows_json`` is a second bound, so a
    plan built before an age was recorded cannot outlive the rule that made it. ``None`` from
    either side means "not capped here", never "capped at nothing".

    Raises ``BandRulesUnavailable`` for a youth profile whose rules will not load: a limit that
    cannot be computed is not a limit of ``None``.
    """
    stored = spec.get("progression", {}).get("cap_load_kg") if isinstance(spec.get("progression"), dict) else None
    live: float | None = None
    # Raises for a youth profile whose band table did not load, rather than treating the missing
    # rules as "no cap here" and falling back to whatever the row happened to carry.
    if require_band_rules(view.profile, view.rules) is not None:
        try:
            unit = LoadUnit(spec.get("load_unit") or LoadUnit.BODYWEIGHT.value)
        except ValueError:
            unit = LoadUnit.BODYWEIGHT
        live = effective_cap(view.rules, unit, view.profile.bodyweight_kg)
    bounds = [value for value in (stored, live) if isinstance(value, int | float)]
    return min(float(value) for value in bounds) if bounds else None


def _step_load(current: float | None, delta: int, ladder: list[float], cap: float | None) -> float | None:
    """The next rung up or down from ``current``, or ``None`` when the cap blocks the step."""
    if not ladder:
        return None
    rungs = sorted(ladder)
    if current is None:
        current = rungs[0]
    if delta > 0:
        above = [rung for rung in rungs if rung > current + LOAD_EPSILON]
        if not above:
            return None
        target = above[0]
        if cap is not None and target > cap + LOAD_EPSILON:
            return None
        return target
    below = [rung for rung in rungs if rung < current - LOAD_EPSILON]
    return below[-1] if below else None


def _cap_notice(row: RowView) -> str:
    """The row's own note about the cap in force, or a plain line saying the step is blocked.

    Never the word "weight": a youth profile must never see body-image language (D-027), and this
    line renders on the son's screen.
    """
    notes = [str(note) for note in row.spec.get("notes") or []]
    return notes[0] if notes else "That is as much load as this exercise goes to."


def adjust(
    db: Session,
    view: SessionView,
    position: int,
    field: Field,
    delta: int,
    settings: ProgramSettings,
    ts: str | None = None,
) -> TickResult:
    """Step reps or load by one, writing the ``*_done`` columns only."""
    row = view.row(position)
    if row is None:
        raise TickError(f"row {position} is not on this session")
    if not row.adjustable:
        raise TickError("a distance row is ticked, not adjusted; the planned distance is what counts")
    if field not in ("reps", "load"):
        raise TickError(f"cannot adjust {field!r}")
    if delta == 0:
        return TickResult(row=row, changed=False)

    record = row.record
    if field == "reps":
        return _committed(db, _adjust_reps(db, view, row, record, delta))
    return _committed(db, _adjust_load(db, view, row, record, delta, settings))


def _adjust_reps(db: Session, view: SessionView, row: RowView, record: SessionRowRecord, delta: int) -> TickResult:
    current = row.reps
    if current is None:
        return TickResult(row=row, changed=False, notice="This row is not counted in reps.")
    target = max(MIN_REPS, min(MAX_REPS, current + (1 if delta > 0 else -1)))
    if target == current:
        return TickResult(row=row, changed=False)
    record.reps_done = target
    return TickResult(row=_stage(db, view, record), changed=True)


def _adjust_load(
    db: Session,
    view: SessionView,
    row: RowView,
    record: SessionRowRecord,
    delta: int,
    settings: ProgramSettings,
) -> TickResult:
    ladder = _ladder(settings, _load_type_of(row))
    if not ladder:
        return TickResult(row=row, changed=False, notice="No adjustable load is set up for this exercise.")
    cap = load_cap(view, row.spec)
    target = _step_load(row.load_kg, delta, ladder, cap)
    if target is None:
        # A blocked step shows the row's notes line instead of moving (PRP-02 adjust wireframe).
        return TickResult(row=row, changed=False, notice=_cap_notice(row) if delta > 0 else None)
    record.load_done_kg = target
    return TickResult(row=_stage(db, view, record), changed=True)


def _load_type_of(row: RowView) -> str | None:
    """Which implement's ladder this row steps on, read off the library exercise.

    It cannot be inferred from ``load_unit``: the goblet squat is ``per_implement`` and held in
    two hands, but the implement is a dumbbell, and walking the kettlebell ladder for it would
    offer a 16 kg jump where the rack steps by a pound.
    """
    bundle = library_bundle()
    if bundle is None:
        return None
    exercise = bundle.exercises.get(str(row.spec.get("exercise_id")))
    return exercise.load_type.value if exercise is not None else None


def apply_patch(
    db: Session,
    view: SessionView,
    position: int,
    patch: dict[str, Any],
    settings: ProgramSettings,
) -> TickResult:
    """The JSON API's write: only the fields present in the body are written.

    The offline replay target. ``reps_done`` and ``load_done_kg`` arrive as absolute values here
    rather than as steps, because the queue records what the phone already showed; both are still
    bounded by the same clamp the stepper uses.

    The whole patch is ordered by ``ts``, not just its ``done`` field. A queue drained after a
    reconnect replays oldest-first, so an adjust recorded before the row was ticked arrives after
    a newer one, and writing its reps unconditionally rolled the row back to a number the user had
    already changed. One timestamp, one decision, every field.
    """
    row = view.row(position)
    if row is None:
        raise TickError(f"row {position} is not on this session")
    ts = patch.get("ts")
    result = TickResult(row=row, changed=False)
    if is_stale(row.record, ts):
        return result

    if "reps_done" in patch and patch["reps_done"] is not None:
        if not row.adjustable:
            raise TickError("a distance row is ticked, not adjusted; the planned distance is what counts")
        value = max(MIN_REPS, min(MAX_REPS, int(patch["reps_done"])))
        row.record.reps_done = value
        result = TickResult(row=_stage(db, view, row.record), changed=True)

    if "load_done_kg" in patch and patch["load_done_kg"] is not None:
        result = _write_load(db, view, row, float(patch["load_done_kg"]), result)

    if "done" in patch and patch["done"] is not None:
        done_result = _stage_done(db, view, position, bool(patch["done"]), ts)
        result = TickResult(
            row=done_result.row,
            changed=result.changed or done_result.changed,
            notice=result.notice,
        )
    return _committed(db, result)


def _write_load(db: Session, view: SessionView, row: RowView, value: float, previous: TickResult) -> TickResult:
    """Write an absolute load from the JSON API, refusing what the stepper cannot reach.

    Three refusals, because this endpoint is the offline replay target and nothing downstream
    re-checks it. A distance row has no load column at all. A row the plan gave **no** load is a
    bodyweight movement, and its unit is one the band table has no cap for (D-037a's
    ``youth_load_uncappable``) - so an unguarded write would put any number a client sent on a
    child's session and, from PRP-06, into Garmin. And a load must be a real, non-negative number.
    """
    if not row.adjustable:
        raise TickError("a distance row is ticked, not adjusted; the planned distance is what counts")
    if row.spec.get("load_kg") is None:
        raise TickError("this exercise is done at bodyweight; there is no load to record")
    if not isfinite(value) or value < 0:
        raise TickError("a load has to be a positive number of kilograms")

    cap = load_cap(view, row.spec)
    over_cap = cap is not None and value > cap + LOAD_EPSILON
    row.record.load_done_kg = min(value, cap) if cap is not None else value
    return TickResult(
        row=_stage(db, view, row.record),
        changed=True,
        notice=_cap_notice(row) if over_cap else previous.notice,
    )
