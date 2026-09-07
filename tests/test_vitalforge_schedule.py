"""The retry policy and the outcome table, unit-tested without a database or a socket.

``schedule.py`` is arithmetic and ``outcomes.py`` is a decision table; both were reachable only
through ``attempt`` until now, which meant every boundary in them cost a fixture, a mocked
response and a commit. Tested directly, the rules are readable as rules.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from cadence.vitalforge.client import ActivityResult
from cadence.vitalforge.outcomes import apply_result
from cadence.vitalforge.schedule import (
    BACKOFF_CAP_MIN,
    MAX_ATTEMPTS,
    SLOW_RETRY,
    at,
    delay_minutes,
    now,
    parse_at,
    retry_at,
    slow_retry_at,
)
from cadence.vitalforge.tables import FAILED, SENT, SyncJob


class _Recorder:
    """Stands in for ``sync._save``: records the fields, returns a job carrying them."""

    def __init__(self) -> None:
        self.fields: dict = {}

    def __call__(self, _db, job: SyncJob, **fields) -> SyncJob:
        self.fields = fields
        merged = {name: getattr(job, name) for name in ("id", "session_id", "status", "attempts")}
        return SyncJob(**{**merged, **fields})


def _job(attempts: int = 1) -> SyncJob:
    return SyncJob(id="j", session_id="s", status="pending", attempts=attempts)


def _result(status: int, **fields) -> ActivityResult:
    return replace(ActivityResult(status_code=status), **fields)


# ------------------------------------------------------------------------------ schedule


@pytest.mark.parametrize(
    ("attempts", "minutes"), [(1, 1), (2, 2), (3, 4), (4, 8), (5, 16), (6, 32), (7, 64), (8, 120), (99, 120)]
)
def test_the_backoff_doubles_and_then_stops_doubling(attempts: int, minutes: int) -> None:
    assert delay_minutes(attempts) == minutes


def test_the_backoff_never_returns_zero() -> None:
    """A zero delay is a tight loop. ``attempts=0`` should not be reachable, and is bounded anyway."""
    assert delay_minutes(0) == 1
    assert delay_minutes(-5) == 1
    assert delay_minutes(1000) == BACKOFF_CAP_MIN


def test_retry_at_stops_at_the_cap() -> None:
    assert retry_at(MAX_ATTEMPTS) is None
    assert retry_at(MAX_ATTEMPTS + 1) is None
    assert retry_at(MAX_ATTEMPTS - 1) is not None


def test_retry_at_is_in_the_future_and_parses_back() -> None:
    scheduled = parse_at(retry_at(1))

    assert scheduled is not None and scheduled.tzinfo is not None
    assert timedelta(0) < scheduled - now() <= timedelta(minutes=1)


def test_the_slow_schedule_is_two_hours() -> None:
    due = parse_at(slow_retry_at()) - now()

    assert timedelta(hours=1, minutes=55) < due <= SLOW_RETRY


def test_parse_at_reads_a_naive_timestamp_as_utc() -> None:
    """Nothing in this app writes one, but a hand-edited row must not crash the drainer."""
    assert parse_at("2026-09-06T08:00:00") == datetime(2026, 9, 6, 8, tzinfo=UTC)
    assert parse_at("not a time") is None
    assert parse_at(None) is None
    assert parse_at("") is None


def test_at_round_trips() -> None:
    moment = now()
    assert parse_at(at(moment)) == moment


# ------------------------------------------------------------------------------ outcomes


def test_a_202_is_sent_and_finished() -> None:
    save = _Recorder()

    job = apply_result(None, save, _job(1), _result(202, garmin_status="synced", remote_id="7"), 0)

    assert job.status == SENT
    assert save.fields["next_attempt_at"] is None
    assert save.fields["remote_ref"] == "7"


def test_a_200_dedup_is_also_finished() -> None:
    save = _Recorder()

    apply_result(None, save, _job(1), _result(200, deduplicated=True, garmin_status="synced"), 0)

    assert save.fields["status"] == SENT and save.fields["next_attempt_at"] is None


@pytest.mark.parametrize("garmin", ["pending", "failed"])
def test_a_stored_session_garmin_has_not_filed_comes_back(garmin: str) -> None:
    """Contract section 4.5: the re-POST is the only retry VitalForge has."""
    save = _Recorder()

    apply_result(None, save, _job(1), _result(202, garmin_status=garmin), 0)

    assert save.fields["status"] == SENT
    assert save.fields["next_attempt_at"] is not None


def test_a_stored_session_stops_asking_once_the_budget_is_spent() -> None:
    save = _Recorder()

    apply_result(None, save, _job(MAX_ATTEMPTS), _result(202, garmin_status="failed"), MAX_ATTEMPTS - 1)

    assert save.fields["next_attempt_at"] is None


@pytest.mark.parametrize("garmin", ["synced", "skipped", "unknown"])
def test_the_terminal_garmin_statuses_are_terminal(garmin: str) -> None:
    """``skipped`` is D-042 and ``unknown`` is D-049; both mean a re-POST is the wrong move."""
    save = _Recorder()

    apply_result(None, save, _job(1), _result(202, garmin_status=garmin), 0)

    assert save.fields["status"] == SENT and save.fields["next_attempt_at"] is None


@pytest.mark.parametrize("status", [409, 422])
def test_a_refusal_is_terminal_and_spends_the_attempt(status: int) -> None:
    save = _Recorder()

    apply_result(None, save, _job(3), _result(status, error="no"), 2)

    assert save.fields["status"] == FAILED
    assert save.fields["next_attempt_at"] is None
    assert save.fields["last_status"] == status
    assert "attempts" not in save.fields, "the claim already spent it"


@pytest.mark.parametrize("status", [404, 401, 403])
def test_a_configuration_problem_hands_the_attempt_back(status: int) -> None:
    """D-137: the cap counts session-attributable failures, and none of these is one."""
    save = _Recorder()

    apply_result(None, save, _job(4), _result(status), 3)

    assert save.fields["attempts"] == 3
    assert save.fields["next_attempt_at"] is not None
    assert parse_at(save.fields["next_attempt_at"]) - now() > timedelta(hours=1)


def test_the_404_message_names_the_branch_to_deploy() -> None:
    save = _Recorder()

    apply_result(None, save, _job(1), _result(404), 0)

    assert save.fields["last_error"] == (
        "VitalForge has no /api/activity yet — deploy the cadence/activity-endpoint branch"
    )


def test_a_5xx_is_retried_on_the_ordinary_backoff() -> None:
    save = _Recorder()

    apply_result(None, save, _job(2), _result(503, error="upstream down"), 1)

    assert save.fields["status"] == FAILED
    due = parse_at(save.fields["next_attempt_at"]) - now()
    assert timedelta(minutes=1) < due <= timedelta(minutes=2)


def test_a_5xx_on_the_last_attempt_is_terminal() -> None:
    save = _Recorder()

    apply_result(None, save, _job(MAX_ATTEMPTS), _result(500), MAX_ATTEMPTS - 1)

    assert save.fields["next_attempt_at"] is None


def test_an_unexplained_5xx_still_says_something() -> None:
    save = _Recorder()

    apply_result(None, save, _job(1), _result(502), 0)

    assert "502" in save.fields["last_error"]
