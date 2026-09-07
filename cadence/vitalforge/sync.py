"""The write-back queue: one ``sync_job`` per finished session, retried on a bounded backoff.

Bounded is the whole point (D-016, contract section 5.9). VitalForge has no retry worker, and
``garminconnect`` raises on a 429 with no backoff of its own, so a tight client loop can get the
shared Garmin credential IP-blocked - which breaks JD's weight logging too, because it is the same
module-level client. Hence: exponential 1 min -> 2 h, eight attempts, and **never** an automatic
retry of a 409 or a 422, both of which need a human to change something.

``next_attempt_at IS NULL`` means terminal. It is the only flag the drainer reads, so a status and
a schedule can never disagree about whether a job is finished with.

**The eight-attempt cap counts session-attributable failures only** (D-137): a 5xx, a timeout, a
transport error. A 404 (VitalForge has no ``/api/activity`` yet), a 401 (the token is wrong) and a
missing token or person slug are *configuration* problems that no number of attempts changes and
that JD fixing ``.env`` should heal, so they retry on the slow two-hour schedule forever without
spending the budget. The budget exists to stop Cadence hammering a shared Garmin credential over
one session; a two-hourly poll does not do that.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import and_, or_, update
from sqlmodel import Session, select

from cadence.profils.tables import Profile
from cadence.seance.tables import SessionRecord
from cadence.vitalforge.client import VitalForgeClient
from cadence.vitalforge.errors import VitalForgeNotConfigured, VitalForgeUnavailable, sanitise
from cadence.vitalforge.outcomes import apply_result
from cadence.vitalforge.schedule import (
    BAD_TOKEN_ERROR,
    DRAIN_LIMIT,
    FORCE_COOLDOWN,
    JUST_TRIED,
    MAX_ATTEMPTS,
    NO_ENDPOINT_ERROR,
    NO_TOKEN_WARNING,
    NOT_RETRYABLE,
    REFUSALS,
    REJECTED,
    THROTTLED,
    UNPROCESSABLE,
    at,
    delay_minutes,
    now,
    parse_at,
    retry_at,
    slow_retry_at,
)
from cadence.vitalforge.tables import CANCELLED, FAILED, PENDING, SENT, SKIPPED, SyncJob

logger = logging.getLogger(__name__)

__all__ = [
    "BAD_TOKEN_ERROR",
    "FORCE_COOLDOWN",
    "MAX_ATTEMPTS",
    "NOT_RETRYABLE",
    "NO_ENDPOINT_ERROR",
    "NO_TOKEN_WARNING",
    "JUST_TRIED",
    "DrainReport",
    "ForceRefused",
    "SyncJob",
    "attempt",
    "build_job",
    "cancel",
    "withdraw_retry",
    "delay_minutes",
    "drain",
    "due_jobs",
    "enqueue",
    "job_for",
    "open_jobs",
]


@dataclass(frozen=True, slots=True)
class DrainReport:
    """What one pass over the queue did."""

    drained: int = 0
    sent: int = 0
    failed: int = 0
    skipped: int = 0

    def plus(self, job: SyncJob) -> DrainReport:
        return DrainReport(
            drained=self.drained + 1,
            sent=self.sent + (job.status == SENT),
            failed=self.failed + (job.status == FAILED),
            skipped=self.skipped + (job.status == SKIPPED),
        )

    def as_dict(self) -> dict[str, int]:
        return {"drained": self.drained, "sent": self.sent, "failed": self.failed, "skipped": self.skipped}


def job_for(db: Session, session_id: str) -> SyncJob | None:
    """This session's job, or nothing. One row per session, guaranteed by the unique index."""
    return db.exec(select(SyncJob).where(SyncJob.session_id == session_id)).first()


def enqueue(db: Session, session_id: str, payload: dict[str, Any]) -> SyncJob:
    """Get-or-create on ``session_id``.

    Not an insert: ``POST /api/sessions/{id}/done`` is idempotent and is what the offline queue
    replays into, so a blind insert files the same session twice the first time a phone comes back
    online. An existing job is returned untouched - its stored payload is what was actually sent.
    """
    job = build_job(db, session_id, payload)
    db.commit()
    db.refresh(job)
    return job


def build_job(db: Session, session_id: str, payload: dict[str, Any]) -> SyncJob:
    """Get-or-create **without committing**, so the caller can choose the transaction.

    ``seance.done.finish`` uses this to write the job in the same transaction that finalises the
    session. A job created after that commit means a crash in between leaves a finished session
    with nothing queued and no way to notice: the session looks done, VitalForge never hears
    about it, and no screen says otherwise. One transaction makes "finished" and "queued" a
    single fact.

    The lookup flushes any job added earlier in the same uncommitted transaction, so a Together
    Done still gets exactly one job per session.
    """
    existing = job_for(db, session_id)
    if existing is not None:
        return existing
    job = SyncJob(
        id=str(uuid.uuid4()),
        session_id=session_id,
        status=PENDING,
        attempts=0,
        next_attempt_at=at(now()),
        payload_json=json.dumps(payload),
        target_slug=profile_slug(db, session_id),
    )
    db.add(job)
    return job


SYNC_JOB_FIELDS: tuple[str, ...] = (
    "id",
    "session_id",
    "target",
    "status",
    "attempts",
    "next_attempt_at",
    "last_error",
    "payload_json",
    "remote_ref",
    "target_slug",
    "last_attempt_at",
    "last_status",
)


def _save(db: Session, job: SyncJob, **fields: Any) -> SyncJob:
    """Write a new version of the job. Never mutates the loaded row in place.

    A **fresh** ``SyncJob`` rather than ``update_model``: ``model_copy`` shallow-copies the
    instance, so the copy shares the original's SQLAlchemy instance state and the row that
    actually gets flushed is the unchanged one. Building a new object and merging it is the
    version of "return a new object" that survives the ORM.
    """
    unknown = set(fields) - set(SYNC_JOB_FIELDS)
    if unknown:
        raise ValueError(f"SyncJob has no field(s): {', '.join(sorted(unknown))}")
    values = {name: getattr(job, name) for name in SYNC_JOB_FIELDS}
    merged = db.merge(SyncJob(**{**values, **fields}))
    db.commit()
    db.refresh(merged)
    return merged


def profile_slug(db: Session, session_id: str) -> str:
    """The VitalForge person slug this session's profile currently carries (D-017)."""
    record = db.get(SessionRecord, session_id)
    profile = db.get(Profile, record.profile_id) if record is not None else None
    return (profile.vitalforge_person if profile is not None else "") or ""


def _target_slug(db: Session, job: SyncJob) -> str:
    """Where this job is addressed, preferring what it was addressed to when it was queued.

    A retry must not re-resolve the slug. A session that timed out, was edited in Settings to a
    different person, and then retried would be filed under **someone else's** VitalForge person
    — a real body of work attributed to the wrong human, and VitalForge's idempotency key is
    per-person, so nothing downstream would flag it.

    The one exception is a job queued before anybody was configured. Its frozen slug is blank,
    which addresses nothing, so it resolves live and is persisted the first time an answer
    exists — which is what lets the backlog from a fresh install heal (D-138).
    """
    if job.target_slug:
        return job.target_slug
    resolved = profile_slug(db, job.session_id)
    if resolved:
        logger.info("session %s had no VitalForge person when it was queued; using %r", job.session_id, resolved)
    return resolved


def _skip(db: Session, job: SyncJob, message: str) -> SyncJob:
    """Store locally and come back to it. Logged once per job, not once per drain.

    **Not terminal.** A blank token and a blank person slug are the shipped ``.env.example``
    state, so the very first Done on a new install lands here — and a job the drainer never looks
    at again would mean that session, and every session before JD filled in ``.env``, silently
    never reaching VitalForge however correctly it was configured afterwards. The slow two-hour
    schedule costs nothing when there is no token (the attempt returns without opening a socket)
    and picks the backlog up by itself once there is one.
    """
    if job.status != SKIPPED:
        logger.warning(message)
    return _save(db, job, status=SKIPPED, next_attempt_at=slow_retry_at(), last_error=message)


def _claim(db: Session, job: SyncJob) -> SyncJob | None:
    """Take the attempt **before** making it, or return ``None`` because someone else has it.

    Without this the periodic pass and a person tapping Retry can both read ``attempts = 3``,
    both POST, and both write ``attempts = 4`` — so ``MAX_ATTEMPTS`` bounds nothing and two
    requests race to file the same session. VitalForge dedupes on ``session_id``, so the damage
    is bounded there, but the budget this whole module exists to enforce is not.

    One UPDATE, guarded on the attempts count the caller read, is the lease: SQLite serialises
    it, exactly one writer sees ``rowcount == 1``, and the loser leaves the job alone. The next
    schedule is written now as well, so a process that dies mid-POST leaves a job that comes back
    on the ordinary backoff rather than one that looks due forever.
    """
    attempts = job.attempts + 1
    started = at(now())
    result = db.execute(
        update(SyncJob)
        .where(SyncJob.id == job.id, SyncJob.attempts == job.attempts)
        .values(attempts=attempts, next_attempt_at=retry_at(attempts), last_attempt_at=started)
    )
    db.commit()
    if result.rowcount != 1:
        logger.info("session %s is already being attempted elsewhere; not attempting it again", job.session_id)
        return None
    claimed = db.get(SyncJob, job.id)
    if claimed is not None:
        db.refresh(claimed)
    return claimed


async def attempt(db: Session, job: SyncJob, client: VitalForgeClient) -> SyncJob:
    """One POST, five seconds, and the new job. Never raises.

    The attempt is claimed before the request goes out and the outcome is written after, so two
    drainers cannot both spend the same one.
    """
    if not client.configured:
        return _skip(db, job, NO_TOKEN_WARNING)
    try:
        payload = json.loads(job.payload_json)
    except (TypeError, ValueError):
        return _save(db, job, status=FAILED, next_attempt_at=None, last_error="the stored payload will not parse")

    before = job.attempts
    claimed = _claim(db, job)
    if claimed is None:
        return job_for(db, job.session_id) or job

    slug = _target_slug(db, claimed)
    if slug and not claimed.target_slug:
        claimed = _save(db, claimed, target_slug=slug)
    try:
        result = await client.post_activity(slug, payload)
    except VitalForgeNotConfigured as exc:
        # A blank person slug, found only once the request was already claimed. Hand the attempt
        # back: nothing was sent, and a configuration gap must not spend the budget (D-137).
        return _skip(db, _save(db, claimed, attempts=before), str(exc))
    except VitalForgeUnavailable as exc:
        return _save(db, claimed, status=FAILED, next_attempt_at=retry_at(claimed.attempts), last_error=str(exc))
    except Exception as exc:  # noqa: BLE001 - a broken sync must never take the caller with it
        logger.warning("the VitalForge write-back for %s failed unexpectedly", job.session_id, exc_info=True)
        return _save(db, claimed, status=FAILED, next_attempt_at=retry_at(claimed.attempts), last_error=sanitise(exc))
    return apply_result(db, _save, claimed, result, before)


class ForceRefused(Exception):
    """A manual retry this queue will not make.

    Carries both the sentence to show the person and a ``code``, because the plain-form Retry
    answers with a redirect and only the code can safely travel in a URL.
    """

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(REFUSALS[code])


def _refuse_force(job: SyncJob | None, now: datetime) -> None:
    """Why a Retry button may do nothing, in the order a person would ask.

    Two refusals, both about not making things worse. A 422 means VitalForge rejected the payload
    itself, so the same bytes will be rejected again however many times they are sent — offering
    to try is a lie. And a retry within a minute of the last one is the tight loop D-016 forbids:
    a held button, or an impatient double tap, is how a shared Garmin credential gets IP-blocked
    and JD's weight logging stops working (contract section 5.9).
    """
    if job is None:
        return
    if job.last_status == UNPROCESSABLE:
        raise ForceRefused(REJECTED)
    last = parse_at(job.last_attempt_at)
    if last is not None and now - last < FORCE_COOLDOWN:
        raise ForceRefused(THROTTLED)


def due_jobs(db: Session, limit: int = DRAIN_LIMIT, force_session_id: str | None = None) -> list[SyncJob]:
    """The jobs a drain would attempt, oldest schedule first.

    An explicit ``force_session_id`` ignores the backoff and a spent attempt budget: the Retry
    button on the Done screen exists precisely for a job the automatic schedule has given up on.
    It does **not** ignore :func:`_refuse_force`, which raises ``ForceRefused`` rather than
    quietly returning nothing, so the caller can say which of the two reasons it was.

    A ``sent`` job with no schedule is simply done: VitalForge has the session and, if it was
    going to Garmin, Garmin has it too. Nothing to ask for, and no refusal to explain.
    """
    if force_session_id is not None:
        job = job_for(db, force_session_id)
        if job is None or (job.status == SENT and job.next_attempt_at is None):
            return []
        _refuse_force(job, now())
        return [job]
    statement = (
        select(SyncJob)
        # ``skipped`` is in the set on purpose: it is not terminal, it is "no token yet".
        # ``sent`` is too, because a session VitalForge stored but Garmin has not confirmed
        # keeps a schedule and the contract says a re-POST is how that push is re-attempted.
        .where(SyncJob.status.in_((PENDING, FAILED, SKIPPED, SENT)))  # type: ignore[attr-defined]
        .where(SyncJob.next_attempt_at.is_not(None))  # type: ignore[union-attr]
        .where(SyncJob.next_attempt_at <= at(now()))  # type: ignore[operator]
        .order_by(SyncJob.next_attempt_at)  # type: ignore[arg-type]
        .limit(limit)
    )
    return list(db.exec(statement).all())


async def drain(
    db: Session,
    client: VitalForgeClient,
    *,
    limit: int = DRAIN_LIMIT,
    force_session_id: str | None = None,
) -> DrainReport:
    """Attempt every due job. One failing job never stops the others (PRP-06 risk 9)."""
    report = DrainReport()
    for job in due_jobs(db, limit, force_session_id):
        try:
            report = report.plus(await attempt(db, job, client))
        except Exception:  # noqa: BLE001 - keep draining; the next job is unrelated to this one
            logger.warning("draining the VitalForge job for %s raised", job.session_id, exc_info=True)
            report = DrainReport(report.drained + 1, report.sent, report.failed + 1, report.skipped)
    return report


def cancel(db: Session, job: SyncJob, reason: str) -> SyncJob:
    """Stop a job for good because a person changed their mind, not because it failed.

    Terminal and distinct from ``failed`` on purpose: nothing went wrong, and the History badge
    should not say it did. The stored payload is left exactly as it was sent-or-would-have-been,
    because it is the record of what the app intended at the time; rewriting it to flip a flag
    would make the queue lie about its own history.
    """
    logger.info("cancelling the VitalForge write-back for %s: %s", job.session_id, reason)
    return _save(db, job, status=CANCELLED, next_attempt_at=None, last_error=reason)


def withdraw_retry(db: Session, job: SyncJob, reason: str) -> SyncJob:
    """Call off a job's remaining attempts without pretending it never happened.

    For a job VitalForge has already **stored**: ``cancel`` would be a lie, because the session
    is filed and the History badge should keep saying so. What is being withdrawn is the
    outstanding re-POST — the one that would ask VitalForge to try the Garmin push again — so the
    schedule goes and the status stays.
    """
    logger.info("withdrawing the remaining VitalForge attempts for %s: %s", job.session_id, reason)
    return _save(db, job, next_attempt_at=None, last_error=reason)


def open_jobs(db: Session) -> list[SyncJob]:
    """Every job that would still make a request. Used when a setting invalidates a queue.

    Four live states, not three. ``sent`` **with a schedule** is the one that is easy to miss and
    was: VitalForge has the session but Garmin has not confirmed the activity, so the drain will
    re-POST the frozen body to ask again (contract section 4.5). A caller withdrawing a queue has
    to see that job, or the request it is trying to stop goes out two hours later anyway.
    """
    statement = select(SyncJob).where(
        or_(
            SyncJob.status.in_((PENDING, FAILED, SKIPPED)),  # type: ignore[attr-defined]
            and_(SyncJob.status == SENT, SyncJob.next_attempt_at.is_not(None)),  # type: ignore[union-attr]
        )
    )
    return list(db.exec(statement).all())
