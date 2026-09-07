"""PRP-07 acceptance tests 17-28: assessments, gaps and challenges as domain objects.

The HTTP half lives in ``tests/test_assessments_api.py``. Everything here drives ``cadence.bilan``
directly, because the rules under test - the section 7.5 ranking and its tie-break, the section 7.4
youth vocabulary, and the section 7.7 row insertion - are decisions the routes only relay.

Several of these are written so they can *fail*: the tie-break is fed in the wrong order, the
youth-name check asserts the phrase is really there before it asserts what is not, and the
"unavailable" test proves a recorded zero would have been a gap. A negative test that passes
because nothing was computed at all is worse than no test.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import date, timedelta
from typing import Any

import pytest
from sqlmodel import Session as DbSession
from sqlmodel import select

from cadence.bilan import assessments as bilan
from cadence.bilan import gaps as gap_rules
from cadence.bilan.gaps import BODY_COMP, Gap
from cadence.bilan.service import save_battery
from cadence.bilan.tables import UNIT_UNAVAILABLE, Assessment
from cadence.profils.settings import DEFAULT_SETTINGS
from cadence.profils.tables import PROFILE_ME, PROFILE_SON, Profile
from cadence.programme.tables import PLANNED, PlannedSession
from cadence.schema.enums import AgeBand
from cadence.vitalforge.tables import MetricsCache

BASELINE = date(2026, 9, 6)
YOUTH_BANDS: tuple[AgeBand, ...] = (AgeBand.U10, AgeBand.AGE_10_13, AgeBand.AGE_14_17)

#: Six adult readings that are all at or above their section 7.3 ``ok`` tier, so a test that wants
#: exactly one gap can name exactly one number and leave the rest alone.
ADULT_PASS: dict[str, float] = {
    "push_up_max": 20.0,
    "dead_hang_s": 45.0,
    "plank_s": 90.0,
    "wall_angel_reach": 3.0,
    "goblet_squat_quality": 4.0,
    "farmer_carry_s": 45.0,
}
#: The same for a ``u10`` son against his section 7.4 fun targets.
YOUTH_PASS: dict[str, float] = {
    "push_up_max": 8.0,
    "dead_hang_s": 20.0,
    "plank_s": 30.0,
    "wall_angel_reach": 2.0,
    "goblet_squat_quality": 3.0,
    "farmer_carry_s": 20.0,
}


def _rows(pairs: Iterable[tuple[str, float | None]], profile_id: str = PROFILE_ME) -> list[Assessment]:
    """One battery as unsaved rows, in the order given - which is what the tie-break is fed."""
    return [
        Assessment(
            id=f"{profile_id}-{test_id}",
            profile_id=profile_id,
            test_id=test_id,
            value=value,
            unit=UNIT_UNAVAILABLE if value is None else "reps",
            recorded_on=BASELINE.isoformat(),
        )
        for test_id, value in pairs
    ]


def _detect(
    db: DbSession,
    rows: list[Assessment],
    library: Any,
    *,
    is_youth: bool = False,
    band: AgeBand = AgeBand.ADULT,
    profile_id: str = PROFILE_ME,
    today: date = BASELINE,
    limit: int = 3,
) -> list[Gap]:
    return gap_rules.detect(
        db, profile_id, rows, library.assessments, band, is_youth=is_youth, today=today, limit=limit
    )


def _battery(
    library: Any, values: Mapping[str, float], *, is_youth: bool, unavailable: Iterable[str] = ()
) -> list[Any]:
    skip = set(unavailable)
    items: list[dict[str, Any]] = [
        {"test_id": test_id, "value": value} for test_id, value in values.items() if test_id not in skip
    ]
    items += [{"test_id": test_id, "unavailable": True} for test_id in unavailable]
    return [bilan.parse_result(library.assessments, item, is_youth=is_youth) for item in items]


def _save(
    db: DbSession,
    profile: Profile,
    library: Any,
    values: Mapping[str, float],
    *,
    unavailable: Iterable[str] = (),
    on: date = BASELINE,
    today: date | None = None,
    settings: Any = DEFAULT_SETTINGS,
) -> Any:
    results = _battery(library, values, is_youth=profile.kind == "youth", unavailable=unavailable)
    return save_battery(db, profile, results, library, settings, on, today or on)


def _planned(db: DbSession, profile_id: str) -> list[PlannedSession]:
    statement = (
        select(PlannedSession)
        .where(PlannedSession.profile_id == profile_id, PlannedSession.status == PLANNED)
        .order_by(PlannedSession.week, PlannedSession.day_index)  # type: ignore[arg-type]
    )
    return list(db.exec(statement).all())


def _trend(db: DbSession, profile_id: str, points: int) -> None:
    """A cached body-composition series: fat rising, muscle falling, both well off the flat band."""
    days = [BASELINE - timedelta(days=2 * (points - 1 - index)) for index in range(points)]
    payload = {
        "body_fat_pct": [[day.isoformat(), 20.0 + 0.2 * index] for index, day in enumerate(days)],
        "muscle_pct": [[day.isoformat(), 40.0 - 0.2 * index] for index, day in enumerate(days)],
    }
    db.merge(MetricsCache(profile_id=profile_id, fetched_at=BASELINE.isoformat(), payload_json=json.dumps(payload)))
    db.commit()


# ------------------------------------------------------------------ 17-18: the section 7.5 ranking


def test_gap_ranking(db_session: DbSession, library: Any) -> None:
    """17. Four sub-``ok`` results rank by severity, and only the worst three come back."""
    rows = _rows(
        [
            ("farmer_carry_s", 24.0),  # (30-24)/30 = 0.2, the one that is dropped
            ("push_up_max", 3.0),  # (15-3)/15 = 0.8
            ("plank_s", 30.0),  # (60-30)/60 = 0.5
            ("goblet_squat_quality", 1.0),  # (3-1)/3 = 0.667
            ("wall_angel_reach", 2.0),  # exactly ok, never a gap
            ("dead_hang_s", 45.0),
        ]
    )
    found = _detect(db_session, rows, library)

    assert [gap.test_id for gap in found] == ["push_up_max", "goblet_squat_quality", "plank_s"]
    assert [gap.rank for gap in found] == [1, 2, 3]
    assert found[0].severity == pytest.approx(0.8)
    assert "farmer_carry_s" not in {gap.test_id for gap in found}, "the fourth gap must be dropped, not kept"


def test_gap_tie_break(db_session: DbSession, library: Any) -> None:
    """18. Equal severities resolve in the section 7.5.3 order, not in the order they arrived."""
    # Both fall exactly half short. `push_up_max` is fed first on purpose: a stable sort with no
    # tie-break would leave it first, so this test can actually fail.
    rows = _rows([("push_up_max", 7.5), ("dead_hang_s", 15.0)])
    found = _detect(db_session, rows, library)

    assert found[0].severity == pytest.approx(found[1].severity)
    assert [gap.test_id for gap in found] == ["dead_hang_s", "push_up_max"]


# ------------------------------------------------- 19-21: what is never a gap, and the body one


def test_unavailable_is_never_a_gap(seeded: Any, db_session: DbSession, library: Any) -> None:
    """19. **Negative.** A dead hang nobody could run is stored, ranked out, and un-challenged."""
    me = db_session.get(Profile, PROFILE_ME)
    assert me is not None
    state = _save(db_session, me, library, {**ADULT_PASS, "plank_s": 30.0}, unavailable=["dead_hang_s"])

    stored = {row.test_id: row for row in state.latest}
    assert stored["dead_hang_s"].value is None, "an unrunnable test is stored NULL, never zero (D-019)"
    assert stored["dead_hang_s"].unit == UNIT_UNAVAILABLE
    assert [gap.test_id for gap in state.gaps] == ["plank_s"], "the battery must produce some gap, or this is vacuous"
    assert "dead_hang_s" not in {item.test_id for item in state.challenges}

    # The contrast: the same test recorded as a real zero *is* the worst gap there is.
    zeroed = _detect(db_session, _rows([("dead_hang_s", 0.0), ("plank_s", 30.0)]), library)
    assert zeroed[0].test_id == "dead_hang_s"


def test_body_comp_gap_never_for_youth(db_session: DbSession, library: Any) -> None:
    """20. **Negative.** Section 3.5 bans the goal type, so the trend is not even consulted."""
    _trend(db_session, PROFILE_SON, 11)
    _trend(db_session, PROFILE_ME, 11)
    rows = _rows([("plank_s", 10.0)], profile_id=PROFILE_SON)

    son = _detect(db_session, rows, library, is_youth=True, band=AgeBand.U10, profile_id=PROFILE_SON)
    assert BODY_COMP not in {gap.test_id for gap in son}

    # The same series under an adult profile does produce the gap, so the youth guard is what
    # suppressed it rather than a series the detector could not read.
    adult = _detect(db_session, _rows([("plank_s", 10.0)]), library)
    assert BODY_COMP in {gap.test_id for gap in adult}


def test_body_comp_gap_needs_ten_points(db_session: DbSession, library: Any) -> None:
    """21. Nine cached points are noise; eleven with the right slopes append a gap, ranked last."""
    rows = _rows([("push_up_max", 3.0), ("plank_s", 30.0)])

    _trend(db_session, PROFILE_ME, 9)
    assert BODY_COMP not in {gap.test_id for gap in _detect(db_session, rows, library)}

    _trend(db_session, PROFILE_ME, 11)
    found = _detect(db_session, rows, library)
    assert [gap.test_id for gap in found] == ["push_up_max", "plank_s", BODY_COMP]
    assert found[-1].rank == 3
