"""Timestamps in, local dates out.

Everything is stored in UTC and formatted at the edge (PRP-04 implementation notes). The ISO week
that ``done_this_week`` counts against is the *local* one, because a session finished at 22:00 on
a Sunday belongs to the week the user just lived, not to the UTC day it happened to land in.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

# ``%-d`` is glibc's "no leading zero"; the app targets Linux (D-004's containers) and Playwright
# runs there too, so no fallback is carried.
DAY_FORMAT = "%a %-d %b"


def parse_utc(raw: str | None) -> datetime | None:
    """An ISO timestamp as an aware datetime, or ``None`` when it will not parse.

    A naive value is read as UTC: that is what every writer in this app stores, and guessing local
    would silently move a session across a week boundary.
    """
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def local_date(raw: str | None) -> date | None:
    """The local calendar date a stored timestamp falls on."""
    parsed = parse_utc(raw)
    return parsed.astimezone().date() if parsed is not None else None


def parse_date(raw: object) -> date | None:
    """A ``YYYY-MM-DD`` string from a cached payload, or ``None``. Never raises."""
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    if not isinstance(raw, str):
        return None
    try:
        return date.fromisoformat(raw.strip()[:10])
    except ValueError:
        return None


def week_start(day: date) -> date:
    """The Monday of ``day``'s ISO week."""
    return day - timedelta(days=day.weekday())


def same_iso_week(left: date, right: date) -> bool:
    """Whether two dates share an ISO year and week number."""
    return left.isocalendar()[:2] == right.isocalendar()[:2]


def format_day(day: date | None) -> str:
    """``Fri 5 Sep``. An unparseable timestamp renders as a dash rather than as a crash."""
    return day.strftime(DAY_FORMAT) if day is not None else "—"


def days_ago(stamp: datetime | None, now: datetime | None = None) -> int | None:
    """Whole days between ``stamp`` and now, floored at zero. ``None`` in, ``None`` out."""
    if stamp is None:
        return None
    reference = now or datetime.now(UTC)
    return max(0, (reference - stamp).days)
