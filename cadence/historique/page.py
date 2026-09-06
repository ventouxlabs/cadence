"""Assembling one History screen, for both the HTML router and the JSON API.

The **youth guard lives here**, not in a template: the column is built with ``trend=None`` for any
``profile.kind == "youth"`` before the template is reached, so a template bug cannot leak a body
metric onto the son's screen (PRP-04 risk 5). The serialiser drops the ``trend`` key entirely for
the same profiles — absent, never null.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlmodel import Session

from cadence.historique.queries import DEFAULT_LIMIT, SessionSummary, clamp_limit, recent_sessions
from cadence.historique.scorecard import Scorecard, weekly_scorecard
from cadence.historique.sparkline import sparkline
from cadence.historique.trend import TrendSeries, body_comp_trend
from cadence.profils.tables import PROFILE_ME, PROFILE_SON, Profile
from cadence.seance.today import TOGETHER, TodayError, normalise_profile_key

YOUTH = "youth"


@dataclass(frozen=True, slots=True)
class HistoryColumn:
    """One profile's history: the scorecard, the sessions, and the parent-only trend."""

    profile: Profile
    scorecard: Scorecard
    sessions: tuple[SessionSummary, ...]
    trend: TrendSeries | None
    svg: str | None

    @property
    def is_youth(self) -> bool:
        return self.profile.kind == YOUTH

    def as_dict(self) -> dict[str, Any]:
        """The scorecard payload. ``trend`` is absent for a youth profile, not null."""
        payload = self.scorecard.as_dict()
        if not self.is_youth:
            payload["trend"] = self.trend.as_dict() if self.trend is not None else None
        return payload


@dataclass(frozen=True, slots=True)
class HistoryView:
    """What ``GET /history`` renders: one column, or two on the Together tab."""

    profile_key: str
    columns: tuple[HistoryColumn, ...]
    missing: tuple[str, ...] = ()

    @property
    def together(self) -> bool:
        return self.profile_key == TOGETHER

    @property
    def primary(self) -> HistoryColumn:
        return self.columns[0]


def build_column(
    db: Session,
    profile: Profile,
    limit: int = DEFAULT_LIMIT,
    today: date | None = None,
    *,
    with_trend: bool = True,
) -> HistoryColumn:
    """One profile's history. A youth profile never gets as far as reading ``metrics_cache``.

    ``with_trend=False`` is the Together tab (D-105): the son is standing at the same phone, and
    architecture section 5 says he never sees a body metric, not that he never sees his own.
    """
    youth = profile.kind == YOUTH
    trend = body_comp_trend(db, profile.id, today=today) if with_trend and not youth else None
    return HistoryColumn(
        profile=profile,
        scorecard=weekly_scorecard(db, profile.id, today),
        sessions=tuple(recent_sessions(db, profile.id, limit)),
        trend=trend,
        svg=sparkline(trend),
    )


def _profile_ids(profile_key: str) -> tuple[str, ...]:
    return (PROFILE_ME, PROFILE_SON) if profile_key == TOGETHER else (profile_key,)


def resolve_profiles(db: Session, raw_profile: str | None) -> tuple[str, tuple[Profile, ...]]:
    """The key and the profile rows behind ``?profile=``.

    Raises ``TodayError`` for a key that is not ``me``, ``son`` or ``together``, and for one whose
    profile row has not been seeded: an empty history is a page, a missing profile is a 404.
    """
    profile_key = normalise_profile_key(raw_profile)
    found = tuple(
        profile
        for profile in (db.get(Profile, profile_id) for profile_id in _profile_ids(profile_key))
        if profile is not None
    )
    if not found:
        raise TodayError(f"unknown profile {raw_profile!r}")
    return profile_key, found


def build_page(
    db: Session,
    raw_profile: str | None,
    limit: int = DEFAULT_LIMIT,
    today: date | None = None,
) -> HistoryView:
    """History for one profile, or both of them side by side.

    Raises ``TodayError`` for a profile key that is not ``me``, ``son`` or ``together``, and for a
    key whose profile row has not been seeded — an empty history is a page, a missing profile is a
    404.
    """
    size = clamp_limit(limit)
    profile_key, profiles = resolve_profiles(db, raw_profile)
    found = {profile.id for profile in profiles}
    missing = tuple(name for name in _profile_ids(profile_key) if name not in found)
    solo = profile_key != TOGETHER
    columns = tuple(build_column(db, profile, size, today, with_trend=solo) for profile in profiles)
    return HistoryView(profile_key=profile_key, columns=columns, missing=missing)
