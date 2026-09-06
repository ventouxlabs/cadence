"""Reading the 30-day body-composition series out of ``metrics_cache``.

PRP-06 fills the table; this renders whatever is in it. The table may not exist yet on a database
built from an older schema, so the read is guarded and falls through to "No data yet." rather than
500ing the whole page.

Two series, two units, one shared x-axis of dates. VitalForge returns weight in **grams** (contract
section 2.2) and PRP-06 divides by 1000 before caching; a value that is still grams-shaped would
render as "84100 kg", so anything outside a plausible human range is dropped here (D-102).

Nothing in this module is reachable from a youth profile: the route nulls the trend before the
template is built (architecture section 5, PRP-04 risk 5).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session

from cadence.historique.clock import days_ago, parse_date, parse_utc
from cadence.historique.queries import has_table
from cadence.vitalforge.tables import MetricsCache

logger = logging.getLogger(__name__)

DEFAULT_DAYS = 30
MIN_POINTS = 2

WEIGHT_KEY = "weight_kg"
BODY_FAT_KEY = "body_fat_pct"

# Plausible-range guards. A weight still in grams, or a body fat read as a fraction, is dropped
# rather than drawn: a wrong line is worse than a missing one.
WEIGHT_RANGE = (20.0, 300.0)
BODY_FAT_RANGE = (1.0, 70.0)

Point = tuple[date, float]


@dataclass(frozen=True, slots=True)
class TrendSeries:
    """The two series, oldest point first, with how fresh the cache behind them is."""

    weight_kg: tuple[Point, ...]
    body_fat_pct: tuple[Point, ...]
    stale: bool = False
    fetched_at: datetime | None = None

    @property
    def has_weight_line(self) -> bool:
        """Whether the weight series is drawn. The legend key asks this, so it cannot disagree."""
        return len(self.weight_kg) >= MIN_POINTS

    @property
    def has_body_fat_line(self) -> bool:
        return len(self.body_fat_pct) >= MIN_POINTS

    @property
    def renderable(self) -> bool:
        """Whether any series has enough points to draw a line."""
        return self.has_weight_line or self.has_body_fat_line

    @property
    def latest_weight(self) -> float | None:
        return self.weight_kg[-1][1] if self.weight_kg else None

    @property
    def latest_body_fat(self) -> float | None:
        return self.body_fat_pct[-1][1] if self.body_fat_pct else None

    @property
    def weight_label(self) -> str:
        return f"{self.latest_weight:.1f} kg" if self.latest_weight is not None else ""

    @property
    def body_fat_label(self) -> str:
        return f"{self.latest_body_fat:.1f}%" if self.latest_body_fat is not None else ""

    def stale_note(self, now: datetime | None = None) -> str | None:
        """ "Last updated 2 days ago", or nothing at all when the cache is current."""
        if not self.stale:
            return None
        age = self.age_days(now)
        if age is None:
            return "Last updated a while ago"
        if age == 0:
            return "Last updated today"
        return f"Last updated {age} day{'' if age == 1 else 's'} ago"

    def age_days(self, now: datetime | None = None) -> int | None:
        """How old the cached pull is, for the "Last updated N days ago" line."""
        return days_ago(self.fetched_at, now)

    def as_dict(self) -> dict[str, object]:
        return {
            WEIGHT_KEY: [[day.isoformat(), value] for day, value in self.weight_kg],
            BODY_FAT_KEY: [[day.isoformat(), value] for day, value in self.body_fat_pct],
            "stale": self.stale,
        }


def _number(raw: object, bounds: tuple[float, float]) -> float | None:
    """A finite number inside ``bounds``, or ``None``. Booleans are not numbers here."""
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    value = float(raw)
    low, high = bounds
    if value != value or not (low <= value <= high):  # noqa: PLR0124 - NaN is never equal to itself
        return None
    return value


def _series(payload: object, key: str, bounds: tuple[float, float], oldest: date) -> tuple[Point, ...]:
    """One series out of a cached payload: ``[[date, value], ...]``, filtered and sorted.

    Every entry is validated on its own. A payload half-written by an interrupted pull yields the
    points that parsed rather than nothing at all, and never an exception.
    """
    if not isinstance(payload, dict):
        logger.warning("the cached metrics payload is a %s, not an object; no trend", type(payload).__name__)
        return ()
    entries = payload.get(key)
    if entries is None:
        return ()
    if not isinstance(entries, list):
        logger.warning("cached %r is a %s, not a list of points; dropping the series", key, type(entries).__name__)
        return ()
    points: list[Point] = []
    dropped = 0
    for entry in entries:
        if not isinstance(entry, list | tuple) or len(entry) != 2:
            dropped += 1
            continue
        day = parse_date(entry[0])
        value = _number(entry[1], bounds)
        if day is None or value is None:
            # Out of range is the interesting one: it is how a grams-shaped weight shows up
            # (D-102), and silently drawing nothing is exactly what hid it.
            dropped += 1
            continue
        if day < oldest:
            continue
        points.append((day, value))
    if dropped:
        logger.warning("dropped %d unusable point(s) from the cached %r series", dropped, key)
    return tuple(sorted(points, key=lambda point: point[0]))


def body_comp_trend(
    db: Session,
    profile_id: str,
    days: int = DEFAULT_DAYS,
    today: date | None = None,
) -> TrendSeries | None:
    """The last ``days`` of weight and body fat, or ``None`` when there is nothing cached.

    ``None`` covers all three ways this can be empty — no table, no row, unreadable payload — so
    the caller has one branch and the template one condition.
    """
    if not has_table(db, MetricsCache.__tablename__):
        # The ordinary state until PRP-06 ships a migration for an older database. Not a warning.
        logger.debug("no %s table yet; the trend card will say there is no data", MetricsCache.__tablename__)
        return None
    try:
        row = db.get(MetricsCache, profile_id)
    except SQLAlchemyError:  # pragma: no cover - the table exists but the connection is broken
        logger.warning("could not read the metrics cache for %r; showing no trend", profile_id, exc_info=True)
        return None
    if row is None:
        return None
    try:
        payload = json.loads(row.payload_json)
    except (TypeError, ValueError) as exc:
        logger.warning("the cached metrics payload for %r will not parse, so no trend is drawn: %s", profile_id, exc)
        return None
    oldest = (today or datetime.now(UTC).astimezone().date()) - timedelta(days=days)
    return TrendSeries(
        weight_kg=_series(payload, WEIGHT_KEY, WEIGHT_RANGE, oldest),
        body_fat_pct=_series(payload, BODY_FAT_KEY, BODY_FAT_RANGE, oldest),
        stale=bool(row.stale),
        fetched_at=parse_utc(row.fetched_at),
    )
