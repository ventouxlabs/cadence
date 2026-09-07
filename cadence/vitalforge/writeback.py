"""What Done does: create the job, try once, and get out of the way.

The rule this module exists to keep is that **the write-back is bounded and cannot fail Done**.
One inline attempt per finished session with the client's five-second timeout, and failure is an
ordinary outcome that still renders the summary; everything after that belongs to the periodic
drain (PRP-06 risk 10).

Bounded is not instant. A Together Done finalises two sessions and attempts them one after the
other, so the worst case a person waits is 2 x 5 s, not 5 s (D-134). The Done button says
"Saving…" and disables itself for exactly that reason: ten seconds of a button that looks
untapped is how a session gets finished twice.

Done runs in a synchronous request handler, and the client is async, so the attempt is bridged
with :func:`run_blocking`. FastAPI runs a ``def`` route in a worker thread with no event loop of
its own, which is exactly the situation ``asyncio.run`` is for.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Coroutine, Sequence
from dataclasses import dataclass
from typing import Any

from sqlmodel import Session, select

from cadence.config import Settings, get_settings
from cadence.db import ExerciseRecord
from cadence.seance.catalog import load_settings
from cadence.seance.tables import SessionRecord
from cadence.seance.today import SessionView, view_for_session
from cadence.vitalforge.client import VitalForgeClient
from cadence.vitalforge.payload import CREDENTIAL_PERSON, payload_for_view
from cadence.vitalforge.schedule import GARMIN_WITHDRAWN
from cadence.vitalforge.sync import attempt, build_job, cancel, enqueue, job_for, open_jobs, withdraw_retry
from cadence.vitalforge.tables import CANCELLED, FAILED, PENDING, SENT, SKIPPED, SyncJob

logger = logging.getLogger(__name__)

# The five states of PRP-06 section 4.3. Deliberately different wording from PRP-02's offline
# banner ("Saved on this phone — will sync"), which is about the browser's own outbox: both lines
# can be on screen at once and they mean different things.
LINE_SENT = "synced ✓"
LINE_PENDING = "will sync"
LINE_RETRYING = "sync failed (retrying)"
LINE_TERMINAL = "sync failed"
LINE_SKIPPED = "stored locally — VitalForge not configured"
LINE_GARMIN_PENDING = "stored in VitalForge, Garmin pending"
LINE_GARMIN_WITHDRAWN = "stored in VitalForge — Garmin push switched off"
LINE_CANCELLED = "not sent — pushing to Garmin was switched off"

# Stored on the cancelled job so History and the Done screen say the same thing.
CANCELLED_REASON = "pushing the son's sessions to Garmin was switched off before this one was sent"
LINE_NONE = "Stored locally."

# The badge ``cadence/historique/`` already uses for a session with no job at all.
LOCAL = "local"


@dataclass(frozen=True, slots=True)
class SyncLine:
    """One line of static HTML, and whether it comes with a Retry button.

    ``notice`` is what the queue said when it declined to do what the button asked. It belongs on
    the line rather than in a flash message because it is about this session and nothing else,
    and because a button that silently does nothing is worse than one that says why.
    """

    text: str
    retry: bool = False
    status: str = LOCAL
    notice: str | None = None

    def with_notice(self, notice: str | None) -> SyncLine:
        return SyncLine(self.text, self.retry, self.status, notice)


def run_blocking(coro: Coroutine[Any, Any, Any]) -> Any | None:
    """Run one coroutine from a synchronous request handler.

    Returns ``None`` rather than raising if there *is* a running loop in this thread: that would
    mean an async caller reached here, and the right answer is to leave the job pending for the
    drain rather than to break Done over a wiring mistake.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    coro.close()
    logger.warning("the inline VitalForge attempt was skipped: this caller should await it instead")
    return None


def _catalog(db: Session, view: SessionView) -> dict[str, Any]:
    """The exercise docs behind this session's rows, for the optional Garmin sub-category."""
    ids = sorted({row.record.exercise_id for row in view.rows})
    if not ids:
        return {}
    try:
        rows = db.exec(select(ExerciseRecord).where(ExerciseRecord.id.in_(ids))).all()  # type: ignore[attr-defined]
        return {row.id: json.loads(row.doc_json) for row in rows}
    except Exception:  # noqa: BLE001 - the row spec already carries name and category
        logger.warning("could not read the exercise catalog for the activity payload", exc_info=True)
        return {}


def sync_session(db: Session, record: SessionRecord, *, config: Settings | None = None) -> SyncJob | None:
    """Queue one finished session and attempt it once. Never raises, never blocks the redirect."""
    active = config or get_settings()
    existing = job_for(db, record.id)
    if existing is not None and existing.status == SENT and existing.next_attempt_at is None:
        return existing
    if existing is None:
        payload = _payload_for(db, record, active)
        if payload is None:
            return None
        existing = enqueue(db, record.id, payload)
    return attempt_now(db, existing, active)


def queue_session(db: Session, record: SessionRecord, *, config: Settings | None = None) -> SyncJob | None:
    """Write the job **without committing**, for a caller inside its own transaction.

    ``seance.done.finish`` calls this before its commit, so a finished session and its queued
    write-back land together or not at all. The POST is a separate step: it happens after the
    commit, because a five-second request has no business inside a database transaction.
    """
    active = config or get_settings()
    payload = _payload_for(db, record, active)
    if payload is None:
        return None
    return build_job(db, record.id, payload)


def _payload_for(db: Session, record: SessionRecord, config: Settings) -> dict[str, Any] | None:
    """The ``ActivityIn`` body for this session, or ``None`` when there is nothing to send."""
    view = view_for_session(db, record)
    if view is None:
        logger.warning("session %s has no profile or plan; nothing to send to VitalForge", record.id)
        return None
    try:
        payload = payload_for_view(view, load_settings(db), _catalog(db, view))
    except Exception:  # noqa: BLE001 - a session that cannot be described is still a session
        logger.warning("could not build the VitalForge payload for %s", record.id, exc_info=True)
        return None
    if not payload["exercises"]:
        logger.info("session %s finished with no completed rows; not sending it to VitalForge", record.id)
        return None
    return payload


def attempt_now(db: Session, job: SyncJob | None, config: Settings | None = None) -> SyncJob | None:
    """One inline attempt from a synchronous caller. Never raises, never blocks the redirect."""
    if job is None:
        return None
    client = VitalForgeClient(config or get_settings())
    return run_blocking(attempt(db, job, client)) or job


def sync_finished(
    db: Session, records: Sequence[SessionRecord], *, config: Settings | None = None
) -> dict[str, SyncJob]:
    """Every session one Done finalised. Together mode files two, each with its own push rule."""
    jobs: dict[str, SyncJob] = {}
    for record in records:
        try:
            job = sync_session(db, record, config=config)
        except Exception:  # noqa: BLE001 - Done must render whatever the integration did
            logger.warning("the VitalForge write-back for %s failed", record.id, exc_info=True)
            continue
        if job is not None:
            jobs[record.id] = job
    return jobs


def line_for(job: SyncJob | None) -> SyncLine:
    """The Done screen's sync line (PRP-06 section 4.3). ``next_attempt_at`` decides, not status."""
    if job is None:
        return SyncLine(LINE_NONE)
    if job.status == SENT:
        # A schedule on a sent job means VitalForge has the session but Garmin has not confirmed
        # the activity. Saying "synced" there would claim something nobody has verified.
        if job.next_attempt_at is not None:
            return SyncLine(LINE_GARMIN_PENDING, status=SENT)
        if job.last_error == GARMIN_WITHDRAWN:
            # Stored, and the Garmin half called off by the household. "synced ✓" would say the
            # activity is on Garmin, which is the one thing that was deliberately prevented.
            return SyncLine(LINE_GARMIN_WITHDRAWN, status=SENT)
        return SyncLine(LINE_SENT, status=SENT)
    if job.status == CANCELLED:
        # Nothing went wrong. Somebody decided, and the line says so rather than reading as a
        # failure the person is expected to do something about.
        return SyncLine(LINE_CANCELLED, status=CANCELLED)
    if job.status == SKIPPED:
        # Retryable on purpose: this is the shipped ``.env.example`` state, and the person who
        # has just filled the token in should not have to do another workout to use it.
        return SyncLine(LINE_SKIPPED, retry=True, status=SKIPPED)
    if job.status == PENDING or (job.status == FAILED and job.attempts == 0):
        # A 404 or a 401 leaves ``attempts`` untouched: nothing about the session is wrong, so
        # the honest line is still "will sync".
        return SyncLine(LINE_PENDING, status=PENDING)
    if job.next_attempt_at is None:
        return SyncLine(LINE_TERMINAL, retry=True, status=FAILED)
    return SyncLine(LINE_RETRYING, status=FAILED)


def line_for_session(db: Session, session_id: str) -> SyncLine:
    return line_for(job_for(db, session_id))


def sync_status(db: Session, session_id: str) -> str:
    """This session's ``sync_job`` status, or ``local`` when nothing has queued it."""
    job = job_for(db, session_id)
    return job.status if job is not None else LOCAL


def backfill(db: Session, *, config: Settings | None = None, limit: int = 20) -> list[SyncJob]:
    """Queue any finished session that has no ``sync_job``. Belt and braces for D-138.

    ``finish`` writes the job in the same transaction as the finalisation, so this should never
    find anything. It runs anyway, once per periodic pass, because "should never" is how a
    session goes missing quietly: a database restored from a backup taken mid-write, a row
    finalised by a future code path that forgets, a job deleted by hand. Finding nothing is the
    expected result and costs one indexed query.
    """
    statement = (
        select(SessionRecord)
        .where(SessionRecord.finished_at.is_not(None))  # type: ignore[union-attr]
        .where(SessionRecord.id.not_in(select(SyncJob.session_id)))  # type: ignore[attr-defined]
        .order_by(SessionRecord.finished_at)  # type: ignore[arg-type]
        .limit(limit)
    )
    queued: list[SyncJob] = []
    for record in db.exec(statement).all():
        try:
            job = queue_session(db, record, config=config)
        except Exception:  # noqa: BLE001 - one unqueueable session must not stop the rest
            logger.warning("could not queue the orphaned session %s", record.id, exc_info=True)
            continue
        if job is not None:
            logger.warning("session %s was finished with no write-back queued; queuing it now", record.id)
            queued.append(job)
    if queued:
        db.commit()
    return queued


def cancel_youth_pushes(db: Session) -> list[SyncJob]:
    """Cancel every queued job that would still push a youth session to Garmin.

    Called when ``push_son_to_garmin`` is switched **off**. The flag is decided when the payload
    is built and frozen in the stored body, so a job queued while the setting was on keeps
    ``push_to_garmin: true`` however long it sits in the queue: a session that timed out on
    Monday would file itself under the parent's Garmin account on Wednesday, after the household
    had explicitly said no. VitalForge cannot help — it does what the body asks.

    The bodies are not rewritten. A body that no longer matches what the user wants is not a body
    to fix, it is a request to withdraw, and ``cancelled`` says that without pretending the
    session failed.

    Two kinds of withdrawal, because there are two kinds of live job (D-142). One VitalForge has
    never seen is **cancelled**: nothing exists anywhere and nothing will. One VitalForge has
    already stored but whose Garmin push it has not confirmed is still ``sent`` and still
    scheduled, because the contract's re-POST is how that push gets retried — for that one only
    the *schedule* is withdrawn, since the session really is filed and the badge should say so.
    Miss the second and the drain re-POSTs the frozen body two hours later, and the server files
    the son's session under the parent's Garmin after the household has said no.
    """
    withdrawn: list[SyncJob] = []
    for job in open_jobs(db):
        try:
            body = json.loads(job.payload_json)
        except (TypeError, ValueError):
            continue
        if not body.get("push_to_garmin") or body.get("garmin_target") != CREDENTIAL_PERSON:
            continue
        if job.status == SENT:
            withdrawn.append(withdraw_retry(db, job, GARMIN_WITHDRAWN))
        else:
            withdrawn.append(cancel(db, job, CANCELLED_REASON))
    return withdrawn
