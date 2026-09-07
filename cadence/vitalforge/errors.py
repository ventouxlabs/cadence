"""What this package raises instead of letting an ``httpx`` exception escape.

An ``httpx`` error carries the failing request in its ``repr``, and that request carries the
``Authorization`` header. One ``logger.exception`` anywhere upstream would put the VitalForge
token in the log file. Every call is wrapped here, and every message that leaves this module has
been through :func:`sanitise` (PRP-06 section 5.1, risk 2).

The header is not the only way a token travels. VitalForge's own error text can name the value
it rejected, so ``sanitise`` redacts the configured token by value and anything else shaped like
one, not merely the string after the word "Bearer" (D-139).
"""

from __future__ import annotations

import re

from pydantic import SecretStr

# 500 is the PRP's cap: enough of a stack trace or an HTML error page to recognise it, short
# enough that ``sync_job.last_error`` stays a column a human can read.
MAX_EXCERPT = 500

# Any bearer token, whatever surrounds it. Applied to bodies as well as messages: a VitalForge
# 500 page could echo the request headers back.
_BEARER = re.compile(r"[Bb]earer\s+\S+")
REDACTED = "Bearer [redacted]"
SECRET = "[redacted]"

# The shape of a secret when nobody labelled it as one. ``secrets.token_urlsafe(32)``, which is
# how VitalForge mints its tokens (contract section 1.3), is 43 characters of exactly this
# alphabet. Catching it by shape is what covers a message like ``invalid token: <the token>``,
# where the word "Bearer" never appears and the value is the whole point.
_LONG_RUN = re.compile(r"[A-Za-z0-9_\-]{24,}")

# ...except a UUID, which is 36 characters of the same alphabet and is never a secret here. It is
# a Cadence ``session_id``, and redacting it out of ``last_error`` would take away the one thing
# that tells an operator which session the message is about.
_UUID = re.compile(r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")

# Below this length a "secret" is more likely to be an ordinary word, and redacting every short
# run would make errors unreadable without making them safer.
MIN_SECRET_LEN = 8

_SECRETS: list[SecretStr] = []


def register_secret(secret: SecretStr | None) -> None:
    """Teach :func:`sanitise` a value it must never let through.

    The client registers the VitalForge token at construction. Values are held as ``SecretStr``
    and unwrapped only inside ``sanitise``, so the registry itself cannot leak them into a log
    line through its own ``repr``.
    """
    if secret is None or len(secret.get_secret_value()) < MIN_SECRET_LEN:
        return
    if all(existing.get_secret_value() != secret.get_secret_value() for existing in _SECRETS):
        _SECRETS.append(secret)


def _hide_long_runs(text: str) -> str:
    """Redact anything shaped like a token, leaving UUIDs alone."""
    return _LONG_RUN.sub(lambda match: match.group(0) if _UUID.match(match.group(0)) else SECRET, text)


def sanitise(text: object) -> str:
    """A short, token-free version of ``text``, safe to log or store.

    Three passes, narrowest first. The **exact** registered secrets, because a token shorter than
    the generic rule's threshold would otherwise slip through; then the ``Bearer <value>`` form,
    which is how a header echoed back looks; then anything token-shaped, which is what catches a
    VitalForge message that names the value without labelling it — ``invalid token: abc…`` was
    reaching exceptions, logs and ``sync_job.last_error`` untouched.
    """
    raw = text if isinstance(text, str) else str(text)
    for secret in _SECRETS:
        raw = raw.replace(secret.get_secret_value(), SECRET)
    cleaned = _BEARER.sub(REDACTED, raw)
    return _hide_long_runs(cleaned)[:MAX_EXCERPT]


class VitalForgeError(Exception):
    """Base class. Every message is sanitised on the way in."""

    def __init__(self, message: object = "") -> None:
        super().__init__(sanitise(message))


class VitalForgeNotConfigured(VitalForgeError):
    """No token, or no person slug for this profile. Nothing was sent."""


class VitalForgeUnavailable(VitalForgeError):
    """Timeout, connection refused, DNS failure - the request never got an answer."""


class VitalForgeHTTPError(VitalForgeError):
    """A 4xx or 5xx. ``status_code`` decides the retry; ``body_excerpt`` explains it."""

    def __init__(self, status_code: int, body: object = "") -> None:
        self.status_code = status_code
        self.body_excerpt = sanitise(body)
        super().__init__(f"VitalForge answered {status_code}: {self.body_excerpt}")
