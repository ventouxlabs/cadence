"""The JSON API's half of solo mode (D-260): a profile-addressed read of a hidden profile is a 404.

An API is not a screen. ``cadence.web.solo`` sends a browser asking for a hidden profile to the
parent's tab, because a person who taps a stale bookmark wants a screen and not an error; a client
that asks for ``?profile=son`` wants *the son*, and answering with somebody else's numbers under a
200 would be a lie a caller has no way to detect. So the API says the thing is not there (D-265).

**Reads only, and addressed by profile only.** The offline queue replays into
``POST /api/sessions/{id}/rows/{position}`` and ``POST /api/sessions/{id}/done`` (``app.js``), both
addressed by session id, and it treats any 4xx as a permanent refusal that drops the operation
rather than retrying it. A queue holding a tick the son made on Sunday must still land on Monday
after the toggle was turned off on Sunday night, so nothing on that path is gated here (D-264).
"""

from __future__ import annotations

from fastapi.responses import JSONResponse
from sqlmodel import Session

from cadence.api.envelope import err
from cadence.profils.visibility import is_hidden
from cadence.seance.catalog import load_settings

NOT_FOUND = 404


def refuse_hidden(db: Session, raw_profile: str | None, message: str | None = None) -> JSONResponse | None:
    """The 404 for a profile solo mode hides, or ``None`` when the read may go ahead.

    ``message`` must be the sentence **this endpoint** already gives an absent profile, and each
    caller passes its own because the four of them do not agree: ``/api/today`` says "unknown
    profile", ``/api/metrics`` says "no profile", ``/api/profiles`` says "there is no profile".
    A single shared sentence here would have made hidden and absent distinguishable on three of
    the four endpoints - the exact side channel this function exists to close (D-273). The
    default is the wording the two ``TodayError`` routes use.
    """
    if not is_hidden(load_settings(db), raw_profile):
        return None
    return JSONResponse(status_code=NOT_FOUND, content=err(message or f"unknown profile {raw_profile!r}"))


__all__ = ["refuse_hidden"]
