"""Pulling VitalForge's numbers into ``metrics_cache``, and the one-line readiness nudge.

Nothing on the request path for ``GET /today`` calls anything here that touches the network: the
periodic task refreshes the cache and every screen reads it (``docs/architecture.md`` section 5).

Two unit traps, both from contract section 2.2, both handled by one helper. ``metrics/weight`` and
``metrics/muscle_mass`` read ``weight_history``, whose columns are **grams**; ``body_fat`` is
already a percentage. Everyone remembers the weight division and forgets the muscle one, and
34 500 kg of muscle renders without an exception (PRP-06 risk 1).

The youth profile never sees body composition, so ``refresh_metrics`` never asks for it: one call,
to ``readiness``, and the cached payload has no body-comp keys **at all** - absent, not null, so a
template that renders whatever the dict holds cannot leak them (architecture section 5).
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlmodel import Session, select

from cadence.profils.tables import Profile
from cadence.vitalforge.client import VitalForgeClient
from cadence.vitalforge.tables import MetricsCache

logger = logging.getLogger(__name__)

STALE_AFTER = timedelta(hours=6)

# VitalForge metric name -> (the key Cadence stores it under, whether it arrives in grams).
ADULT_METRICS: dict[str, tuple[str, bool]] = {
    "weight": ("weight_kg", True),
    "body_fat": ("body_fat", False),
    "muscle_mass": ("muscle_mass_kg", True),
    "resting_hr": ("resting_hr", False),
    "sleep_score": ("sleep_score", False),
    "body_battery": ("body_battery", False),
}

# The two series PRP-04's trend line reads, under the key names D-101 fixed.
SERIES_KEYS: dict[str, str] = {"weight": "weight_kg", "body_fat": "body_fat_pct"}

GRAMS_PER_KG = 1000.0
SERIES_DAYS = 30

NUDGE_PUSH = "Good day to push"
NUDGE_STEADY = "Steady day"
NUDGE_EASY = "Easy day — hold loads"
NUDGE_NONE = "Readiness not available"

PUSH_AT = 70
STEADY_AT = 50


def _grams_to_kg(value: float) -> float:
    """The one division. Both ``weight`` and ``muscle_mass`` come through here (PRP-06 risk 1)."""
    return round(value / GRAMS_PER_KG, 2)


def nudge_for(score: float | None) -> str:
    """The one line Today shows. ``None`` is the son's permanent answer, and is not a failure."""
    if score is None:
        return NUDGE_NONE
    if score >= PUSH_AT:
        return NUDGE_PUSH
    if score >= STEADY_AT:
        return NUDGE_STEADY
    return NUDGE_EASY


@dataclass(frozen=True, slots=True)
class Readiness:
    """``{score, status}`` only: ``components`` is VitalForge's business, not Cadence's."""

    score: float | None = None
    status: str | None = None

    @property
    def nudge(self) -> str:
        return nudge_for(self.score)

    def as_dict(self) -> dict[str, Any]:
        return {"score": self.score, "status": self.status}


@dataclass(frozen=True, slots=True)
class ProfileMetrics:
    """One profile's cached numbers. Immutable; a refresh builds a new one."""

    profile_id: str
    fetched_at: datetime
    stale: bool = False
    # Only the metrics that actually came back. A youth profile's is always empty.
    latest: dict[str, float] = field(default_factory=dict)
    # ``{"weight_kg": [[iso_date, kg], ...], "body_fat_pct": [...]}`` - D-101's shape exactly.
    series: dict[str, list[list[Any]]] = field(default_factory=dict)
    readiness: Readiness = field(default_factory=Readiness)

    def as_payload(self) -> dict[str, Any]:
        """What goes in ``metrics_cache.payload_json``.

        The two series stay at the top level under D-101's names, because PRP-04's trend reader
        already parses them there. The scalars live under ``latest`` so that ``weight_kg`` never
        means two different shapes in one document (D-120).
        """
        return {**self.series, "latest": dict(self.latest), "readiness": self.readiness.as_dict()}

    def as_data(self) -> dict[str, Any]:
        """The ``GET /api/metrics`` body (PRP-06 section 4.1). Absent keys stay absent."""
        return {
            "profile": self.profile_id,
            "fetched_at": self.fetched_at.isoformat(),
            "stale": self.stale,
            **{key: value for key, value in self.latest.items()},
            "readiness": self.readiness.as_dict(),
            "nudge": self.readiness.nudge,
        }


def _point_value(entry: object) -> float | None:
    if not isinstance(entry, dict):
        return None
    raw = entry.get("value")
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    return float(raw)


class Malformed(Exception):
    """A 200 whose body is not the shape ``docs/vitalforge-contract.md`` section 2.2 describes."""


def _points(body: object) -> list[tuple[str, float]]:
    """``[(date, value), ...]`` from a ``metrics/{name}`` body, oldest first, bad rows dropped.

    Raises :class:`Malformed` when the envelope itself is wrong, which is **not** the same as a
    series with no rows in it. A proxy returning an HTML error page with status 200, or a
    rewritten API, otherwise looked exactly like "this person has no weight readings": the
    refresh counted as a success and replaced a good cache with nothing, marked fresh (D-139).
    """
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        raise Malformed(f"expected an object with a 'data' list, got {type(body).__name__}")
    points: list[tuple[str, float]] = []
    for entry in body["data"]:
        value = _point_value(entry)
        day = entry.get("date") if isinstance(entry, dict) else None
        if value is not None and isinstance(day, str) and day:
            points.append((day, value))
    return sorted(points, key=lambda point: point[0])


async def _gather(client: VitalForgeClient, slug: str, names: tuple[str, ...]) -> dict[str, Any]:
    """Every metric plus readiness, in parallel, keeping whatever came back.

    ``return_exceptions=True`` is the point: one metric 500ing must not blank the other five, and
    a partial answer is still a better nudge than no answer (PRP-06 test 12).
    """
    tasks = [client.get_metric(slug, name, days=SERIES_DAYS) for name in names]
    tasks.append(client.get_readiness(slug))
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return dict(zip((*names, "readiness"), results, strict=True))


def _readiness_from(result: Any) -> tuple[Readiness, bool]:
    """``(readiness, ok)``. A failure is "not available", never a score of zero (risk 13).

    ``{"score": null, "status": "insufficient_data"}`` is a **valid** answer and the son's
    permanent one; a body that is not an object at all, or that carries neither key, is not.
    """
    if isinstance(result, BaseException) or not isinstance(result, dict):
        return Readiness(), False
    if "score" not in result and "status" not in result:
        return Readiness(), False
    raw = result.get("score")
    score = None if isinstance(raw, bool) or not isinstance(raw, int | float) else float(raw)
    status = result.get("status")
    return Readiness(score=score, status=status if isinstance(status, str) else None), True


async def refresh_metrics(db: Session, profile: Profile, client: VitalForgeClient) -> ProfileMetrics:
    """Pull, convert, cache, return. **Never raises**: a failed pull keeps the last good payload.

    A youth profile asks for readiness and nothing else, so there is no body composition to leak
    even if a template later renders the dict blind.
    """
    now = datetime.now(UTC)
    previous = read_cached(db, profile, now=now)
    names = () if profile.kind == "youth" else tuple(ADULT_METRICS)
    slug = profile.vitalforge_person
    if not client.can_reach(slug):
        # An unconfigured install is the shipped state, not a fault: one line per profile per
        # pass, at INFO, instead of seven WARNINGs about requests nobody made. The cache keeps
        # whatever it had and says it is stale, which is exactly true.
        logger.info(
            "VitalForge is not configured for %r (token or person slug missing); metrics not refreshed",
            profile.id,
        )
        return _store(db, _stale_copy(previous, profile, now))
    try:
        answers = await _gather(client, slug, names)
    except Exception as exc:  # noqa: BLE001 - a blank slug or a missing token is not a crash
        logger.warning("VitalForge metrics for %r were not refreshed: %s", profile.id, exc)
        return _store(db, _stale_copy(previous, profile, now))

    readiness, readiness_ok = _readiness_from(answers.get("readiness"))
    latest: dict[str, float] = {}
    series: dict[str, list[list[Any]]] = {}
    usable = 1 if readiness_ok else 0
    for name in names:
        result = answers.get(name)
        if isinstance(result, BaseException):
            logger.warning("VitalForge metric %r for %r failed: %s", name, profile.id, result)
            continue
        try:
            points = _points(result)
        except Malformed as exc:
            logger.warning("VitalForge answered %r for %r with an unusable body: %s", name, profile.id, exc)
            continue
        usable += 1
        _absorb(name, points, latest, series)

    if not usable:
        # Nothing came back that this code could read: every request failed, or every one of them
        # answered with something that is not the contract's shape. Either way the previous
        # numbers are better than none, and ``stale`` says why the screen is not moving. The old
        # rule counted a malformed 200 as a success and overwrote a good cache with emptiness.
        logger.warning("no usable VitalForge reads for %r; serving the cached payload", profile.id)
        return _store(db, _stale_copy(previous, profile, now))
    return _store(db, ProfileMetrics(profile.id, now, False, latest, series, readiness))


def _absorb(
    name: str, points: list[tuple[str, float]], latest: dict[str, float], series: dict[str, list[list[Any]]]
) -> None:
    """One metric's points into the scalar and, for the two trend metrics, the series."""
    if not points:
        return
    key, in_grams = ADULT_METRICS[name]
    convert = _grams_to_kg if in_grams else (lambda value: value)
    latest[key] = convert(points[-1][1])
    series_key = SERIES_KEYS.get(name)
    if series_key is not None:
        series[series_key] = [[day, convert(value)] for day, value in points]


def _stale_copy(previous: ProfileMetrics | None, profile: Profile, now: datetime) -> ProfileMetrics:
    """The last good payload, flagged stale. An empty one when there has never been a good one."""
    if previous is None:
        return ProfileMetrics(profile.id, now, True)
    return ProfileMetrics(profile.id, previous.fetched_at, True, previous.latest, previous.series, previous.readiness)


def _store(db: Session, metrics: ProfileMetrics) -> ProfileMetrics:
    """Replace this profile's row wholesale. A failed write is logged, never raised."""
    row = MetricsCache(
        profile_id=metrics.profile_id,
        fetched_at=metrics.fetched_at.isoformat(),
        payload_json=json.dumps(metrics.as_payload()),
        stale=metrics.stale,
    )
    try:
        db.merge(row)
        db.commit()
    except Exception:  # noqa: BLE001 - the cache is a convenience; a broken write is not fatal
        db.rollback()
        logger.warning("could not write the metrics cache for %r", metrics.profile_id, exc_info=True)
    return metrics


def _parse(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def read_cached(db: Session, profile: Profile, now: datetime | None = None) -> ProfileMetrics | None:
    """This profile's cached metrics, or ``None`` when nothing has ever been pulled.

    Staleness is re-evaluated here, not only at write time: a cache that ages between refreshes
    has to report honestly, and six hours pass without anyone writing a row.
    """
    row = db.get(MetricsCache, profile.id)
    if row is None:
        return None
    try:
        payload = json.loads(row.payload_json)
    except (TypeError, ValueError):
        logger.warning("the cached metrics payload for %r will not parse", profile.id)
        return None
    if not isinstance(payload, dict):
        return None
    fetched = _parse(row.fetched_at) or datetime.now(UTC)
    aged = (now or datetime.now(UTC)) - fetched > STALE_AFTER
    return ProfileMetrics(
        profile_id=profile.id,
        fetched_at=fetched,
        stale=bool(row.stale) or aged,
        latest=_latest_of(payload, profile),
        series=_series_of(payload, profile),
        readiness=_cached_readiness(payload),
    )


def _latest_of(payload: dict[str, Any], profile: Profile) -> dict[str, float]:
    """The scalars, and nothing at all for a youth profile whatever the payload holds."""
    if profile.kind == "youth":
        return {}
    raw = payload.get("latest")
    if not isinstance(raw, dict):
        return {}
    return {key: value for key, value in raw.items() if isinstance(value, int | float) and not isinstance(value, bool)}


def _series_of(payload: dict[str, Any], profile: Profile) -> dict[str, list[list[Any]]]:
    if profile.kind == "youth":
        return {}
    return {key: payload[key] for key in SERIES_KEYS.values() if isinstance(payload.get(key), list)}


def _cached_readiness(payload: dict[str, Any]) -> Readiness:
    raw = payload.get("readiness")
    if not isinstance(raw, dict):
        return Readiness()
    score = raw.get("score")
    status = raw.get("status")
    return Readiness(
        score=None if isinstance(score, bool) or not isinstance(score, int | float) else float(score),
        status=status if isinstance(status, str) else None,
    )


async def refresh_all(db: Session, client: VitalForgeClient) -> dict[str, ProfileMetrics]:
    """Refresh every profile's cache. One profile failing never stops the next."""
    results: dict[str, ProfileMetrics] = {}
    for profile in db.exec(select(Profile)).all():
        try:
            results[profile.id] = await refresh_metrics(db, profile, client)
        except Exception:  # noqa: BLE001 - the periodic task must survive every iteration
            logger.warning("refreshing VitalForge metrics for %r raised", profile.id, exc_info=True)
    return results
