"""What one answer from ``POST /api/activity`` means for the job that sent it.

Separated from ``sync.py`` so the outcome table - PRP-06 section 5.4, the single most reviewed
paragraph of this PRP - can be read on its own. Nothing here performs a request or decides when
to try again; it maps a response to the next state and hands the write back to its caller.

``save`` is passed in rather than imported to keep the dependency pointing one way: ``sync`` owns
the row and how it is written, this module owns what to write.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from cadence.vitalforge.client import ActivityResult
from cadence.vitalforge.schedule import (
    BAD_TOKEN_ERROR,
    CONFLICT,
    GARMIN_RETRYABLE,
    GARMIN_UNKNOWN,
    MAX_ATTEMPTS,
    NO_ENDPOINT_ERROR,
    NOT_FOUND,
    UNAUTHORISED,
    UNPROCESSABLE,
    retry_at,
    slow_retry_at,
)
from cadence.vitalforge.tables import FAILED, SENT, SyncJob

logger = logging.getLogger(__name__)

# ``sync._save``: (db, job, **fields) -> SyncJob.
Save = Callable[..., SyncJob]


def _stored(db: Any, save: Save, job: SyncJob, result: ActivityResult, before: int) -> SyncJob:
    """VitalForge has the session. What is left to decide is the Garmin half."""
    garmin = result.garmin_status
    if garmin == GARMIN_UNKNOWN:
        # Stored, but VitalForge cannot say whether Garmin has it (D-049). Terminal: a re-POST
        # reconciles rather than duplicates, and only a human should ask for that.
        logger.warning(
            "VitalForge stored session %s but could not confirm the Garmin push (%s)",
            job.session_id,
            result.error or "no detail given",
        )
    elif garmin in GARMIN_RETRYABLE:
        # The session is safe; the activity is not filed yet. Contract section 4.5: a re-POST of
        # the same session_id re-attempts the push while the stored status is pending or failed,
        # and that client-driven retry is the only retry mechanism VitalForge has. Slow, because
        # nothing here is urgent and the credential is shared. Bounded by the same eight attempts
        # as everything else, so a push Garmin will never accept stops asking.
        schedule = None if job.attempts >= MAX_ATTEMPTS else slow_retry_at()
        return save(
            db,
            job,
            status=SENT,
            next_attempt_at=schedule,
            remote_ref=result.remote_id,
            last_error=result.error,
            last_status=result.status_code,
        )
    # synced, or skipped because this session was never meant for Garmin (D-042 makes that
    # permanent). Either way there is nothing left to ask for.
    return save(
        db,
        job,
        status=SENT,
        next_attempt_at=None,
        remote_ref=result.remote_id,
        last_error=result.error,
        last_status=result.status_code,
    )


def apply_result(db: Any, save: Save, job: SyncJob, result: ActivityResult, before: int) -> SyncJob:
    """Turn one HTTP answer into the job's next state (PRP-06 section 5.4's outcome table).

    ``job`` already carries the incremented ``attempts`` written by :func:`_claim`; ``before`` is
    what it was, for the outcomes that must not spend one.
    """
    if result.stored:
        return _stored(db, save, job, result, before)

    status = result.status_code
    if status in (CONFLICT, UNPROCESSABLE):
        # Terminal. A 409 needs a settings change and a 422 needs a code change; retrying either
        # only hammers the shared Garmin credential (contract section 5.9).
        return save(db, job, status=FAILED, next_attempt_at=None, last_error=result.error, last_status=status)
    if status == NOT_FOUND:
        # The expected state until PRP-05 is deployed. Not a bug, and not a burnt attempt (D-137).
        return save(
            db,
            job,
            status=FAILED,
            attempts=before,
            next_attempt_at=slow_retry_at(),
            last_error=NO_ENDPOINT_ERROR,
            last_status=status,
        )
    if status in UNAUTHORISED:
        return save(
            db,
            job,
            status=FAILED,
            attempts=before,
            next_attempt_at=slow_retry_at(),
            last_error=BAD_TOKEN_ERROR,
            last_status=status,
        )
    return save(
        db,
        job,
        status=FAILED,
        next_attempt_at=retry_at(job.attempts),
        last_error=result.error or f"VitalForge answered {status}",
        last_status=status,
    )
