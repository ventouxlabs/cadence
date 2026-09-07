"""When the write-back queue tries again, and the vocabulary for saying why.

Pure time arithmetic and the constants that shape it, kept apart from ``sync.py`` so the retry
policy can be read — and argued with — without reading the code that performs the requests.

The policy in one paragraph (D-016, contract section 5.9). VitalForge has no retry worker and
``garminconnect`` raises on a 429 with no backoff of its own, so a tight client loop can get the
shared Garmin credential IP-blocked and break JD's weight logging, which has nothing to do with
Cadence. Hence a bounded exponential backoff, a hard cap on attempts, and a much slower schedule
for the failures no number of attempts can fix.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

# Session-attributable failures only: a 5xx, a timeout, a transport error (D-137).
MAX_ATTEMPTS = 8
BACKOFF_CAP_MIN = 120
DRAIN_LIMIT = 20

# 404 (no /api/activity yet), 401 (bad token), no token, no person slug, and a Garmin push
# VitalForge has not confirmed. None of these is the session's fault and none is fixed by trying
# harder, so they poll gently and forever rather than spending the budget.
SLOW_RETRY = timedelta(hours=2)

# A person tapping Retry beats the backoff, but not by more than once a minute: holding the
# button is exactly the tight loop D-016 exists to prevent.
FORCE_COOLDOWN = timedelta(seconds=60)

NO_TOKEN_WARNING = "VitalForge token not configured — session stored locally only"
NO_ENDPOINT_ERROR = "VitalForge has no /api/activity yet — deploy the cadence/activity-endpoint branch"
BAD_TOKEN_ERROR = "VitalForge rejected the token"
JUST_TRIED = "that session was just tried — give it a minute"
NOT_RETRYABLE = "that session cannot be retried: VitalForge rejected this session"

# Codes, because the plain-form Retry answers with a redirect and the reason has to survive it.
# A code and a lookup, never the sentence itself in the query string: whatever comes back off a
# URL is rendered on the page, and a whitelist is the difference between a message and an
# injection point.
THROTTLED = "throttled"
REJECTED = "rejected"
REFUSALS: dict[str, str] = {THROTTLED: JUST_TRIED, REJECTED: NOT_RETRYABLE}

# Written on a job VitalForge already holds when the household withdraws its Garmin push.
# The session stays stored and stays ``sent``; only the outstanding push is called off.
GARMIN_WITHDRAWN = "the Garmin push was withdrawn: pushing the son's sessions was switched off"

NOT_FOUND = 404
CONFLICT = 409
UNPROCESSABLE = 422
UNAUTHORISED = (401, 403)

# VitalForge could not tell whether its Garmin push landed (D-049). Never auto-retried.
GARMIN_UNKNOWN = "unknown"
# Stored, not filed. Contract section 4.5: a re-POST re-attempts the push while the stored status
# is one of these, and only while it is.
GARMIN_RETRYABLE = ("pending", "failed")


def now() -> datetime:
    return datetime.now(UTC)


def at(moment: datetime) -> str:
    return moment.isoformat()


def parse_at(raw: str | None) -> datetime | None:
    """A stored timestamp as an aware datetime, or nothing at all."""
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def delay_minutes(attempts: int) -> int:
    """1, 2, 4, 8, 16, 32, 64, 120 - the D-016 schedule, capped at two hours."""
    return int(min(2 ** (max(1, attempts) - 1), BACKOFF_CAP_MIN))


def retry_at(attempts: int) -> str | None:
    """When to try again after a session-attributable failure, or ``None`` once spent."""
    if attempts >= MAX_ATTEMPTS:
        return None
    return at(now() + timedelta(minutes=delay_minutes(attempts)))


def slow_retry_at() -> str:
    """Two hours from now: the schedule for everything a person has to fix (D-137)."""
    return at(now() + SLOW_RETRY)
