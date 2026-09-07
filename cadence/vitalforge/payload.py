"""Building the exact ``ActivityIn`` body VitalForge accepts.

The request model on the far side is ``extra="forbid"`` (contract section 4.3), so a key this
module invents is a hard 422 that no amount of retrying fixes. The field set here is PRP-06
section 4.4's table, which is PRP-05 section 4.1's table, reproduced rather than referenced
precisely so the two implementations cannot drift apart quietly.

Three rules that look like details and are not:

* **Omit, never send ``null``.** ``garmin_category: null`` and an absent key mean the same thing
  to Pydantic but not to a reader, and ``"UNKNOWN"`` is not a Garmin category at all (D-018).
* **Timed work is ``reps: 1`` plus ``seconds``.** ``reps`` is required ``ge=1``, and a plank has
  no rep count (D-022).
* **The son's push is the setting, full stop** (D-021). Reading it as ``profile.push_to_garmin
  and setting`` would let a seeded ``False`` pin him off while the settings screen says on.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from cadence.profils.settings import ProgramSettings
from cadence.profils.tables import Profile
from cadence.programme import next_time_note
from cadence.programme.tables import PlannedSession
from cadence.schema.labels import day_label
from cadence.seance.tables import SessionRecord
from cadence.seance.today import RowView, SessionView

logger = logging.getLogger(__name__)

SOURCE = "cadence"
CREDENTIAL_PERSON = "credential_person"
YOUTH = "youth"

# Every bound is the contract's. Cadence clamps rather than sends an out-of-range value: a 422 is
# terminal, and losing a whole session because one rest timer read 4000 seconds is a bad trade.
MAX_EXERCISES = 50
MAX_LABEL = 60
MAX_NOTES = 1000
SETS_RANGE = (1, 100)
REPS_RANGE = (1, 100)
SECONDS_RANGE = (1, 3600)
REST_RANGE = (0, 3600)
WEIGHT_RANGE = (0.0, 500.0)
MIN_DURATION_MIN = 1
MAX_DURATION_MIN = 600

FELT_LABELS: dict[str, str] = {"easy": "easy", "right": "right", "hard": "hard"}


def _clamp(value: float, bounds: tuple[float, float]) -> float:
    low, high = bounds
    return max(low, min(high, value))


def _int_or_none(value: object) -> int | None:
    """A real integer, or nothing. ``bool`` is not a number here: VitalForge 422s on it."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value)


def _float_or_none(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _parse(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _at_most_now(moment: datetime | None, now: datetime) -> datetime | None:
    """``moment``, never later than ``now``.

    The timestamps on a session come from the **phone**: ``POST /api/sessions/{id}/rows/{n}``
    takes a client ``ts`` so the offline queue can replay a tick with the time it actually
    happened. A device whose clock runs fast therefore hands Cadence a future instant, and
    VitalForge rejects a ``start`` more than 60 s ahead of its own clock with a **422** — which
    ``sync.py`` treats as terminal, because a 422 normally means the payload is wrong and
    retrying cannot fix it. It would never be fixed here either: the stored session keeps the
    same bad timestamp forever, so that session would never reach Garmin.

    Clamping loses up to the clock skew on one field. Not clamping loses the session.
    """
    if moment is None:
        return None
    return min(moment.astimezone(UTC), now)


def start_iso(record: SessionRecord, now: datetime | None = None) -> str:
    """``started_at`` as UTC with an explicit ``+00:00``, never in the future.

    Cadence always sends UTC. The local wall clock Garmin wants is computed server-side from
    VitalForge's own ``TZ``; adjusting the offset on this side would misfile the activity twice.
    A session with no first tick falls back to when it finished, then to now, because ``start`` is
    required and a missing one is a 422.
    """
    moment = now or datetime.now(UTC)
    started = _at_most_now(_parse(record.started_at), moment) or _at_most_now(_parse(record.finished_at), moment)
    return (started or moment).astimezone(UTC).isoformat()


def duration_min(record: SessionRecord, now: datetime | None = None) -> int:
    """At least one minute. A twenty-second smoke session would otherwise 422 on zero.

    Both ends are clamped to now for the same reason ``start`` is, and in the same direction, so
    a fast phone clock cannot turn a real session into a negative or a 600-minute one.
    """
    moment = now or datetime.now(UTC)
    started = _at_most_now(_parse(record.started_at), moment)
    finished = _at_most_now(_parse(record.finished_at), moment)
    if started is not None and finished is not None:
        minutes = round((finished - started).total_seconds() / 60)
    else:
        minutes = int(record.duration_min or 0)
    return int(_clamp(max(MIN_DURATION_MIN, minutes), (MIN_DURATION_MIN, MAX_DURATION_MIN)))


def session_label(planned: PlannedSession | None) -> str | None:
    """The day's screen name - "Lower A" - so Garmin's activity title matches what Today said."""
    if planned is None:
        return None
    return day_label(planned.day_type)[:MAX_LABEL] or None


def notes_for(record: SessionRecord) -> str | None:
    """How it felt, and what the progression engine says about next time."""
    parts: list[str] = []
    felt = FELT_LABELS.get(record.felt or "")
    if felt:
        parts.append(f"felt: {felt}")
    note = next_time_note(record)
    if note:
        parts.append(note)
    joined = " · ".join(parts)
    return joined[:MAX_NOTES] or None


def push_to_garmin(profile: Profile, settings: ProgramSettings) -> bool:
    """D-021: the son's answer is the household setting, never an AND with his profile flag."""
    if profile.kind == YOUTH:
        return bool(settings.push_son_to_garmin)
    return bool(profile.push_to_garmin)


def _doc_value(exercises: Mapping[str, Any] | None, exercise_id: str, key: str) -> Any:
    """One field off an exercise doc, whether the catalog holds dicts or ``Exercise`` models."""
    doc = (exercises or {}).get(exercise_id)
    if doc is None:
        return None
    value = doc.get(key) if isinstance(doc, Mapping) else getattr(doc, key, None)
    return getattr(value, "value", value)


def _is_done(row: RowView) -> bool:
    """A row counts when it was ticked, or when some sets were logged against it."""
    return bool(row.record.done) or bool(_int_or_none(row.record.sets_done) or 0)


def _entry(row: RowView, exercises: Mapping[str, Any] | None) -> dict[str, Any]:
    """One ``exercises[]`` element. Optional keys are omitted, never sent as ``null``."""
    record = row.record
    spec = row.spec
    # ``sets_done or sets_planned``, the same shape as ``reps`` (PRP-06 section 5.3). The row
    # spec is the last resort rather than the second, so that what the *session* recorded always
    # wins over what the plan said - they agree today, and a divergence should follow the row.
    sets = _int_or_none(record.sets_done) or _int_or_none(record.sets_planned) or _int_or_none(spec.get("sets")) or 1
    reps = _int_or_none(record.reps_done) or _int_or_none(record.reps_planned) or _int_or_none(spec.get("reps")) or 1
    entry: dict[str, Any] = {
        "name": str(spec.get("name") or record.exercise_id)[:100],
        "sets": int(_clamp(sets, SETS_RANGE)),
        "reps": int(_clamp(reps, REPS_RANGE)),
    }
    category = _doc_value(exercises, record.exercise_id, "garmin_category") or spec.get("garmin_category")
    if category:
        entry["garmin_category"] = str(category)
        sub = _doc_value(exercises, record.exercise_id, "garmin_exercise") or spec.get("garmin_exercise")
        if sub:
            entry["garmin_exercise"] = str(sub)[:100]
    seconds = (
        _int_or_none(record.seconds_done) or _int_or_none(record.seconds_planned) or _int_or_none(spec.get("seconds"))
    )
    if seconds:
        entry["seconds"] = int(_clamp(seconds, SECONDS_RANGE))
    load = _float_or_none(record.load_done_kg)
    if load is None:
        load = _float_or_none(record.load_planned_kg)
    if load is not None:
        entry["weight_kg"] = round(_clamp(load, WEIGHT_RANGE), 2)
    rest = _int_or_none(spec.get("rest_s"))
    if rest is not None:
        entry["rest_s"] = int(_clamp(rest, REST_RANGE))
    return entry


def build_exercises(rows: Iterable[RowView], exercises: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """The done rows, in position order, capped at the contract's fifty.

    Truncating loses the tail of a very long session; a 422 would lose the whole thing.
    """
    done = [row for row in sorted(rows, key=lambda item: item.position) if _is_done(row)]
    if len(done) > MAX_EXERCISES:
        logger.warning(
            "session has %d completed rows; sending the first %d, which is all VitalForge accepts",
            len(done),
            MAX_EXERCISES,
        )
        done = done[:MAX_EXERCISES]
    return [_entry(row, exercises) for row in done]


def build_activity_payload(
    session: SessionRecord,
    rows: Iterable[RowView],
    profile: Profile,
    settings: ProgramSettings,
    exercises: Mapping[str, Any] | None = None,
    *,
    planned: PlannedSession | None = None,
) -> dict[str, Any]:
    """The complete ``ActivityIn`` body for one finished session.

    ``exercises`` is empty when the caller has no reason to look a doc up: the materialised row
    already carries the name and the Garmin category, and the catalog only adds the optional
    sub-category.
    """
    push = push_to_garmin(profile, settings)
    # One clock reading for both fields, so a slow build cannot clamp them to different "now"s.
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "session_id": session.id,
        "start": start_iso(session, now),
        "duration_min": duration_min(session, now),
        "exercises": build_exercises(rows, exercises),
        "source": SOURCE,
        "push_to_garmin": push,
    }
    label = session_label(planned)
    if label:
        payload["session_label"] = label
    notes = notes_for(session)
    if notes:
        payload["notes"] = notes
    if push and profile.kind == YOUTH:
        # D-015. Cadence never asks who owns the Garmin credential - the youth profile is the one
        # that is never the owner, and that is the whole condition.
        payload["garmin_target"] = CREDENTIAL_PERSON
    return payload


def payload_for_view(
    view: SessionView, settings: ProgramSettings, exercises: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """The same payload from what the routes already hold."""
    return build_activity_payload(view.record, view.rows, view.profile, settings, exercises, planned=view.planned)
