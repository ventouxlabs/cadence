"""PRP-06 acceptance tests 20-30: the ``ActivityIn`` body.

The model on the far side is ``extra="forbid"``, so every one of these guards a 422 that only
shows up once a real session has already been done and cannot be re-done.
"""

from __future__ import annotations

import logging

import pytest

from cadence.profils.settings import DEFAULT_SETTINGS
from cadence.profils.tables import Profile
from cadence.programme.tables import PlannedSession
from cadence.seance.tables import SessionRecord, SessionRowRecord
from cadence.seance.today import RowView
from cadence.vitalforge.payload import MAX_EXERCISES, build_activity_payload

# PRP-06 section 4.4, which is PRP-05 section 4.1. Any key outside these is a hard 422.
ACTIVITY_KEYS = {
    "session_id",
    "session_label",
    "start",
    "duration_min",
    "exercises",
    "notes",
    "source",
    "push_to_garmin",
    "garmin_target",
}
EXERCISE_KEYS = {"name", "garmin_category", "garmin_exercise", "sets", "reps", "seconds", "weight_kg", "rest_s"}

START = "2026-09-06T08:00:00+00:00"
FINISH = "2026-09-06T08:42:00+00:00"


def _session(**fields) -> SessionRecord:
    base = {
        "id": "session-1",
        "profile_id": "me",
        "planned_session_id": "planned-1",
        "started_at": START,
        "finished_at": FINISH,
        "duration_min": 42,
        "felt": "right",
    }
    return SessionRecord(**{**base, **fields})


def _planned(day_type: str = "lower_a") -> PlannedSession:
    return PlannedSession(
        id="planned-1",
        program_id="p",
        profile_id="me",
        week=1,
        day_index=1,
        day_type=day_type,
        workout_id="lower-a",
        rows_json="[]",
    )


def _row(position: int, spec: dict, **record_fields) -> RowView:
    base = {
        "id": f"r{position}",
        "session_id": "session-1",
        "position": position,
        "exercise_id": spec.get("exercise_id", "goblet-squat"),
        "sets_planned": spec.get("sets"),
        "reps_planned": spec.get("reps"),
        "seconds_planned": spec.get("seconds"),
        "load_planned_kg": spec.get("load_kg"),
        "done": True,
    }
    return RowView(spec={"position": position, **spec}, record=SessionRowRecord(**{**base, **record_fields}))


SQUAT = {
    "exercise_id": "goblet-squat",
    "name": "Goblet Squat",
    "garmin_category": "SQUAT",
    "sets": 3,
    "reps": 10,
    "load_kg": 20.0,
    "rest_s": 90,
}
PLANK = {"exercise_id": "plank", "name": "Plank", "garmin_category": "PLANK", "sets": 3, "seconds": 45, "rest_s": 60}


def _build(rows, profile=None, settings=None, planned=None, session=None, exercises=None) -> dict:
    return build_activity_payload(
        session or _session(),
        rows,
        profile or Profile(id="me", display_name="Me", kind="adult", push_to_garmin=True),
        settings or DEFAULT_SETTINGS,
        exercises,
        planned=planned or _planned(),
    )


def test_payload_matches_contract_field_set() -> None:
    """Test 20. Guards ``extra="forbid"`` drift between PRP-05 and PRP-06."""
    payload = _build([_row(1, SQUAT), _row(2, PLANK)])

    assert set(payload) <= ACTIVITY_KEYS
    assert {"session_id", "start", "duration_min", "exercises"} <= set(payload)
    for entry in payload["exercises"]:
        assert set(entry) <= EXERCISE_KEYS
        assert {"name", "sets", "reps"} <= set(entry)


def test_start_has_utc_offset() -> None:
    """Test 21. A naive datetime is a 422: VitalForge cannot guess whose clock it was."""
    from datetime import datetime

    payload = _build([_row(1, SQUAT)])

    assert payload["start"].endswith("+00:00")
    assert datetime.fromisoformat(payload["start"]).tzinfo is not None


def test_start_is_converted_not_relabelled() -> None:
    """An offset session keeps its instant. Adjusting it here would misfile the activity twice."""
    payload = _build([_row(1, SQUAT)], session=_session(started_at="2026-09-06T10:00:00+02:00"))

    assert payload["start"] == "2026-09-06T08:00:00+00:00"


def test_duration_min_never_zero() -> None:
    """Test 22. A twenty-second smoke session would otherwise 422 on ``ge=1``."""
    payload = _build(
        [_row(1, SQUAT)],
        session=_session(
            started_at="2026-09-06T08:00:00+00:00", finished_at="2026-09-06T08:00:20+00:00", duration_min=0
        ),
    )

    assert payload["duration_min"] == 1


def test_only_done_rows_included() -> None:
    """Test 23. Ticked, or some sets logged. An untouched row is not a thing that happened."""
    rows = [
        _row(1, SQUAT),
        _row(2, PLANK, done=False, sets_done=2),
        _row(3, {**SQUAT, "exercise_id": "row", "name": "Row"}, done=False),
    ]

    payload = _build(rows)

    assert [entry["name"] for entry in payload["exercises"]] == ["Goblet Squat", "Plank"]


def test_load_done_beats_planned() -> None:
    """Test 24. "Adjust" is the whole point: what was lifted, not what was prescribed."""
    payload = _build([_row(1, SQUAT, load_done_kg=22.5)])

    assert payload["exercises"][0]["weight_kg"] == 22.5


def test_weight_key_omitted_when_no_load() -> None:
    """Test 25. Omit, never ``null``: a bodyweight row has no weight, it does not weigh nothing."""
    payload = _build([_row(1, {"name": "Push-up", "sets": 3, "reps": 12})])

    assert "weight_kg" not in payload["exercises"][0]


def test_garmin_category_key_omitted_when_none() -> None:
    """Test 26. D-018: ``UNKNOWN`` is not a Garmin category, and emitting one earns a 400."""
    payload = _build([_row(1, {"name": "Bear Crawl", "sets": 2, "reps": 10, "garmin_category": None})])

    assert "garmin_category" not in payload["exercises"][0]
    assert "UNKNOWN" not in str(payload)


def test_time_row_sends_reps_one_and_seconds() -> None:
    """Test 27. ``reps`` is required ``ge=1`` and a plank has no rep count (D-022)."""
    entry = _build([_row(1, PLANK)])["exercises"][0]

    assert entry["reps"] == 1
    assert entry["seconds"] == 45
    assert entry["sets"] == 3


@pytest.mark.parametrize(
    ("kind", "push_son", "profile_flag", "expected_push", "expects_target"),
    [
        ("youth", True, False, True, True),
        ("youth", False, True, False, False),
        ("adult", True, True, True, False),
        ("adult", True, False, False, False),
    ],
)
def test_garmin_target_present_only_when_pushing_son(
    kind: str, push_son: bool, profile_flag: bool, expected_push: bool, expects_target: bool
) -> None:
    """Test 28. D-021 and D-015 together.

    The son's answer is the household setting, **not** an AND with his profile flag: reading it as
    a conjunction would let a seeded ``False`` pin him off while the settings screen says on. And
    ``garmin_target`` fills the parent's Garmin with the kid's sessions if it is ever a default.
    """
    profile = Profile(id=kind, display_name="X", kind=kind, push_to_garmin=profile_flag)
    settings = DEFAULT_SETTINGS.with_changes(push_son_to_garmin=push_son)

    payload = _build([_row(1, SQUAT)], profile=profile, settings=settings)

    assert payload["push_to_garmin"] is expected_push
    assert ("garmin_target" in payload) is expects_target


def test_notes_combines_felt_and_next_time() -> None:
    """Test 29. Both halves, joined, and never longer than the contract's thousand."""
    payload = _build([_row(1, SQUAT)])

    assert payload["notes"].startswith("felt: right · ")
    assert "Next time" in payload["notes"]


def test_notes_truncated_to_1000() -> None:
    """Test 29, second half."""
    session = _session(notes="x" * 5000)
    payload = _build([_row(1, SQUAT)], session=session)

    assert len(payload["notes"]) <= 1000


def test_more_than_50_rows_truncated_not_rejected(caplog: pytest.LogCaptureFixture) -> None:
    """Test 30. ``max_length=50``: a 422 would lose the whole session rather than its tail."""
    caplog.set_level(logging.WARNING)
    rows = [_row(index, {**SQUAT, "exercise_id": f"e{index}", "name": f"Move {index}"}) for index in range(1, 61)]

    payload = _build(rows)

    assert len(payload["exercises"]) == MAX_EXERCISES == 50
    assert "60 completed rows" in caplog.text


def test_session_label_is_the_day_the_user_saw() -> None:
    """ "Lower A" on Today, "Lower A" in the Garmin activity title."""
    payload = _build([_row(1, SQUAT)], planned=_planned("lower_a"))

    assert payload["session_label"] == "Lower A"
    assert len(payload["session_label"]) <= 60


def test_the_catalog_supplies_the_optional_sub_category() -> None:
    """``garmin_exercise`` is not on the materialised row; the exercise doc carries it."""
    catalog = {"goblet-squat": {"garmin_category": "SQUAT", "garmin_exercise": "GOBLET_SQUAT"}}

    entry = _build([_row(1, SQUAT)], exercises=catalog)["exercises"][0]

    assert entry["garmin_exercise"] == "GOBLET_SQUAT"


def test_out_of_range_numbers_are_clamped_not_sent() -> None:
    """Clamping loses precision; a 422 loses the session. Every bound is the contract's."""
    entry = _build([_row(1, {**SQUAT, "rest_s": 9999}, sets_done=300, reps_done=400, load_done_kg=900.0)])["exercises"][
        0
    ]

    assert entry["sets"] == 100
    assert entry["reps"] == 100
    assert entry["weight_kg"] == 500.0
    assert entry["rest_s"] == 3600


def test_no_boolean_ever_reaches_a_numeric_field() -> None:
    """VitalForge rejects ``true`` for a number, and Python's ``bool`` is an ``int``."""
    payload = _build([_row(1, {**SQUAT, "sets": True, "reps": True})])

    for value in payload["exercises"][0].values():
        assert not isinstance(value, bool)


def test_a_future_phone_clock_never_reaches_the_wire() -> None:
    """High 1. ``start`` comes from the phone, and VitalForge 422s anything > 60 s ahead.

    ``POST /api/sessions/{id}/rows/{n}`` takes a client ``ts`` so an offline tick replays with the
    time it happened, so a device whose clock runs fast hands Cadence a future instant. A 422 is
    terminal in ``sync.py``, and the stored timestamp never changes, so that session would never
    reach Garmin however many times the queue came back to it.
    """
    from datetime import UTC, datetime, timedelta

    ahead = datetime.now(UTC) + timedelta(minutes=5)
    session = _session(started_at=ahead.isoformat(), finished_at=(ahead + timedelta(minutes=40)).isoformat())

    payload = _build([_row(1, SQUAT)], session=session)

    start = datetime.fromisoformat(payload["start"])
    assert start <= datetime.now(UTC), "a future start is a permanent 422"
    assert payload["duration_min"] >= 1


def test_a_future_finish_cannot_inflate_the_duration() -> None:
    """Both ends are clamped, in the same direction, off one clock reading."""
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    session = _session(
        started_at=(now - timedelta(minutes=30)).isoformat(),
        finished_at=(now + timedelta(hours=20)).isoformat(),
    )

    payload = _build([_row(1, SQUAT)], session=session)

    assert 29 <= payload["duration_min"] <= 31
