"""The whole build matrix: days/week x session length x both profiles, band caps enforced.

Acceptance tests 9-13 sample this space at ``days_per_week=4``. This file walks all of it, and it
judges the result against ``library/youth_rules.yaml`` read straight off disk rather than through
``validate_workout`` - so the validator is not both the thing producing the answer and the thing
grading it. The band table is the law (principles section 3.2); a row that breaks it is a bug
whatever the validator says.
"""

from __future__ import annotations

import json
import math
import random
from datetime import date
from pathlib import Path

import pytest
import yaml
from sqlmodel import Session, select

from cadence.bibliotheque.seed import seed
from cadence.db import init_db
from cadence.profils.settings import DEFAULT_SETTINGS
from cadence.profils.tables import Profile
from cadence.programme.builder import build_program
from cadence.programme.materialise import materialise_rows
from cadence.programme.tables import PlannedSession
from cadence.schema.enums import AgeBand
from cadence.validateur import validate_workout

START = date(2026, 9, 7)
DAYS_PER_WEEK = (2, 3, 4, 5, 6)
SESSION_MINUTES = (15, 30, 45)
BLOCK_WEEKS = 4
BAND_AGE = {AgeBand.U10: 8, AgeBand.AGE_10_13: 11, AgeBand.AGE_14_17: 15}

YOUTH_RULES_YAML = Path(__file__).resolve().parents[1] / "library" / "youth_rules.yaml"
RAW_RULES: dict = yaml.safe_load(YOUTH_RULES_YAML.read_text(encoding="utf-8"))

# Section 6.4's row-time model, restated here so the assertion does not call the code it checks.
SECONDS_PER_REP = 3.0
SECONDS_PER_METRE = 1.2

_A, _LA, _UB, _LFB, _MC, _ASSESS = (
    "upper_a",
    "lower_a",
    "upper_b",
    "lower_full_b",
    "mobility_carry",
    "assessment",
)

# Section 6.3, written out. `assessment` replaces the first slot of week 1; the displaced day type
# is skipped for the block and the rotation index still advances. The three-day week is the one
# that walks the rotation continuously, so a week never repeats the week before it.
WEEK_PATTERNS: dict[int, tuple[tuple[str, ...], ...]] = {
    2: ((_A, _LFB),) * BLOCK_WEEKS,
    3: ((_A, _LA, _UB), (_LFB, _A, _LA), (_UB, _LFB, _A), (_LA, _UB, _LFB)),
    4: ((_A, _LA, _UB, _LFB),) * BLOCK_WEEKS,
    5: ((_A, _LA, _UB, _LFB, _MC),) * BLOCK_WEEKS,
    6: ((_A, _LA, _UB, _LFB, _MC, _A),) * BLOCK_WEEKS,
}
# Six days for a youth profile: the sixth slot is a second play day, not a repeat of upper A.
YOUTH_WEEK_SIX = ((_A, _LA, _UB, _LFB, _MC, _MC),) * BLOCK_WEEKS

# D-058, written out rather than imported: which seed template serves each day type.
ADULT_TEMPLATE_FOR = {
    _A: "upper-a",
    _LA: "lower-a",
    _UB: "upper-b",
    _LFB: "lower-full-b",
    _MC: "mobility-carry-day",
    _ASSESS: "assessment-day",
}
YOUTH_TEMPLATE_FOR = {
    _A: "son-upper-a",
    _LA: "son-lower-a",
    _UB: "son-upper-a",
    _LFB: "together-full-body",
    _MC: "son-play-day",
    _ASSESS: "assessment-day",
}


def _profile(kind: str, band: AgeBand | None = None, **fields) -> Profile:
    age = BAND_AGE.get(band) if band is not None else None
    return Profile(id=kind, display_name=kind.title(), kind=kind, age_years=age, **fields)


def _expected_day_types(days: int, *, is_youth: bool) -> list[str]:
    patterns = YOUTH_WEEK_SIX if (days == 6 and is_youth) else WEEK_PATTERNS[days]
    flat: list[str] = []
    for week_index, week in enumerate(patterns):
        for slot, day_type in enumerate(week):
            flat.append(_ASSESS if (week_index == 0 and slot == 0) else day_type)
    return flat


def _body(rows: list[dict]) -> list[dict]:
    return [row for row in rows if row["role"] != "prelude"]


def _minutes(rows: list[dict]) -> int:
    """Section 6.4's estimate, recomputed here. ``per_side`` doubles the row's time."""
    total = 0.0
    for row in rows:
        if row["reps"] is not None:
            work = row["reps"] * SECONDS_PER_REP
        elif row["seconds"] is not None:
            work = float(row["seconds"])
        elif row["meters"] is not None:
            work = row["meters"] * SECONDS_PER_METRE
        else:
            work = 0.0
        seconds = row["sets"] * (work + row["rest_s"])
        total += seconds * 2 if row["per_side"] else seconds
    return math.ceil(total / 60)


# -------------------------------------------------------------------------------- the block's shape


@pytest.mark.parametrize("days", DAYS_PER_WEEK)
@pytest.mark.parametrize("kind", ["adult", "youth"])
def test_block_has_four_weeks_of_the_declared_day_types(library, days: int, kind: str) -> None:
    """(d) Session count and day-type sequence for every days/week, both profiles."""
    settings = DEFAULT_SETTINGS.with_changes(days_per_week=days)
    plan = build_program(_profile(kind), settings, library, START)
    is_youth = kind == "youth"

    assert plan.session_count == BLOCK_WEEKS * days
    assert [session.day_type for session in plan.sessions] == _expected_day_types(days, is_youth=is_youth)

    table = YOUTH_TEMPLATE_FOR if is_youth else ADULT_TEMPLATE_FOR
    assert [s.workout_id for s in plan.sessions] == [table[s.day_type] for s in plan.sessions]
    assert [s.week for s in plan.sessions] == [week for week in range(1, 5) for _ in range(days)]
    assert len({session.id for session in plan.sessions}) == plan.session_count


@pytest.mark.parametrize("days", DAYS_PER_WEEK)
@pytest.mark.parametrize("minutes", SESSION_MINUTES)
@pytest.mark.parametrize("kind", ["adult", "youth"])
def test_every_session_opens_on_the_prelude_and_leaves_the_challenge_slot_empty(
    library, days: int, minutes: int, kind: str
) -> None:
    """(d) The prelude is first in every session (D-051) and PRP-07's slot is untouched here."""
    settings = DEFAULT_SETTINGS.with_changes(days_per_week=days, session_minutes=minutes)
    for planned in build_program(_profile(kind), settings, library, START).sessions:
        rows = json.loads(planned.rows_json)
        assert rows[0]["role"] == "prelude" and rows[0]["exercise_id"] == "cat-cow", planned.id
        assert rows[0]["is_prelude"] is True
        prelude_length = len([row for row in rows if row["role"] == "prelude"])
        assert [row["role"] for row in rows[:prelude_length]] == ["prelude"] * prelude_length
        assert not any(row["is_challenge"] for row in rows), planned.id
        assert [row["position"] for row in rows] == list(range(1, len(rows) + 1))


DELOADING_TEMPLATES = {
    "adult": ("upper-a", "lower-a", "upper-b", "lower-full-b", "mobility-carry-day", "together-full-body"),
    "youth": ("son-upper-a", "son-lower-a", "son-play-day", "together-full-body"),
}


@pytest.mark.parametrize("kind", ["adult", "youth"])
@pytest.mark.parametrize("minutes", SESSION_MINUTES)
def test_week_four_deloads_every_day_type(library, kind: str, minutes: int) -> None:
    """(d) Section 5.5 across every template a block can schedule, not just ``upper-a``.

    ``assessment-day`` is excluded on purpose: section 10.10 runs no autoregulation and prescribes
    one set per measured row, so the block week never touches it.
    """
    settings = DEFAULT_SETTINGS.with_changes(session_minutes=minutes)
    profile = _profile(kind, AgeBand.AGE_10_13 if kind == "youth" else None)
    checked = 0

    for workout_id in DELOADING_TEMPLATES[kind]:
        template = library.template(workout_id)

        # Rows are matched by exercise, not by index: a youth week-3 session carries heavier
        # numbers, so D-065's clock trim can drop a row there that weeks 1 and 4 both keep.
        def rows_for(week: int, template=template) -> dict[str, dict]:
            built = _body(materialise_rows(template, week, profile, settings, library))
            return {row["exercise_id"]: row for row in built}

        weeks = {week: rows_for(week) for week in (1, 3, BLOCK_WEEKS)}
        shared = sorted(set(weeks[1]) & set(weeks[3]) & set(weeks[BLOCK_WEEKS]))
        assert shared, workout_id
        for exercise_id in shared:
            week1, week3, week4 = (weeks[week][exercise_id] for week in (1, 3, BLOCK_WEEKS))
            where = f"{kind} {workout_id} {exercise_id}"
            assert week4["sets"] == max(2, math.floor(week3["sets"] * 0.60)), where
            assert week4["load_kg"] == week3["load_kg"], where
            assert week4["reps"] == week1["reps"], where
            assert week4["seconds"] == week1["seconds"], where
            assert week4["meters"] == week1["meters"], where
            checked += 1
    assert checked > 0, "the deload assertion never ran"


def test_a_youth_week_three_may_carry_fewer_rows_than_week_one(library) -> None:
    """The clock cap binds per week, so the row set is not constant across a youth block.

    Week 3 adds a set to the main row and two steps to every secondary (section 6.2), which pushes
    ``son-lower-a`` past the band's minutes and costs it a row (D-065). Pinned here because it is
    surprising, legal, and the reason the deload test above matches rows by exercise id.
    """
    settings = DEFAULT_SETTINGS.with_changes(session_minutes=30)
    profile = _profile("youth", AgeBand.AGE_10_13)
    counts = {
        week: len(_body(materialise_rows(library.template("son-lower-a"), week, profile, settings, library)))
        for week in (1, 2, 3, 4)
    }
    assert counts == {1: 5, 2: 5, 3: 4, 4: 5}


# ------------------------------------------------------------------- the band table, read off disk


@pytest.mark.parametrize("band", list(BAND_AGE))
@pytest.mark.parametrize("days", DAYS_PER_WEEK)
@pytest.mark.parametrize("minutes", SESSION_MINUTES)
def test_youth_rows_obey_the_shipped_band_table(library, band: AgeBand, days: int, minutes: int) -> None:
    """(d)/(h) Every materialised youth row against ``library/youth_rules.yaml``.

    Row cap, minute cap, set cap and both load caps. The exercise-count assertion deliberately
    counts *every* training row, including the three low-load movements D-053 exempts from the
    validator's own count - so the seed has to fit the band on its own terms.
    """
    caps = RAW_RULES[band.value]
    settings = DEFAULT_SETTINGS.with_changes(days_per_week=days, session_minutes=minutes)
    plan = build_program(_profile("youth", band), settings, library, START)

    for planned in plan.sessions:
        rows = json.loads(planned.rows_json)
        where = f"{planned.id} {planned.workout_id} band={band.value} m={minutes}"
        assert len(_body(rows)) <= caps["max_exercises_per_session"], where
        assert _minutes(rows) <= caps["max_session_minutes"], where
        for row in rows:
            assert row["sets"] <= caps["max_sets_per_exercise"], f"{where} {row['exercise_id']}"
            if row["load_kg"] is None:
                continue
            key = "max_load_kg_per_hand" if row["load_unit"] == "per_hand" else "max_load_kg_per_implement"
            assert row["load_kg"] <= caps[key], f"{where} {row['exercise_id']} {row['load_unit']}"


@pytest.mark.parametrize("days", DAYS_PER_WEEK)
@pytest.mark.parametrize("minutes", SESSION_MINUTES)
def test_u10_never_renders_an_external_load(library, days: int, minutes: int) -> None:
    """(f)/(h) At u10 both load caps are zero, so the answer is a bodyweight row, not a 0 kg one.

    ``is None`` and not ``<= 0``: a dumbbell row rendered at zero kilos is the wrong movement
    wearing the right name, and it would slip past a numeric cap check.
    """
    settings = DEFAULT_SETTINGS.with_changes(days_per_week=days, session_minutes=minutes)
    for planned in build_program(_profile("youth", AgeBand.U10), settings, library, START).sessions:
        for row in json.loads(planned.rows_json):
            assert row["load_kg"] is None, f"{planned.id} {row['exercise_id']}"
            assert row["load_unit"] in {"bodyweight", "per_hand", "per_implement"}


def test_a_dropped_youth_row_is_never_the_main_finisher_or_last_play_row(library) -> None:
    """(d) D-065 drops the lowest-priority rows to fit the clock, and names what it never drops."""
    settings = DEFAULT_SETTINGS.with_changes(session_minutes=45)
    for band in BAND_AGE:
        for workout_id in ("son-upper-a", "son-lower-a", "son-play-day"):
            rows = _body(materialise_rows(library.template(workout_id), 3, _profile("youth", band), settings, library))
            roles = [row["role"] for row in rows]
            assert "main" in roles, (band, workout_id)
            assert roles[-1] == "finisher", (band, workout_id)
            assert len(rows) >= 3, (band, workout_id)
            plays = [row for row in rows if "play" in {t.value for t in library.exercise(row["exercise_id"]).tags}]
            assert plays, f"section 6.5 needs a fun row: {band} {workout_id}"


def test_the_prelude_exemption_is_not_load_bearing(library) -> None:
    """D-053 lets three bodyweight movements skip the exercise count. Measure what it buys.

    ``son-lower-a`` uses ``glute-bridge`` and ``dead-bug`` as training rows and ``assessment-day``
    uses ``wall-angel``, so the validator's count runs up to two rows short of the truth. The seed
    still fits the u10 cap of five counting every row, which is what the test above asserts; this
    one pins the size of the gap so a future seed cannot quietly grow into it.
    """
    settings = DEFAULT_SETTINGS.with_changes(session_minutes=30)
    profile = _profile("youth", AgeBand.U10)
    gaps = {}
    for workout_id in ("son-lower-a", "assessment-day"):
        rows = _body(materialise_rows(library.template(workout_id), 1, profile, settings, library))
        exempt = [row for row in rows if library.exercise(row["exercise_id"]).is_prelude]
        gaps[workout_id] = (len(rows), len(exempt))
    assert gaps == {"son-lower-a": (4, 2), "assessment-day": (5, 1)}
    assert all(total <= RAW_RULES["u10"]["max_exercises_per_session"] for total, _ in gaps.values())


# ----------------------------------------------------------------------------- the seeded database


def test_the_seeded_son_is_u10_in_every_stored_row(settings) -> None:
    """(h) ``son`` ships with no age, so section 3.8's strictest band governs the whole block."""
    engine = init_db(settings)
    caps = RAW_RULES["u10"]
    with Session(engine) as session:
        seed(session, Path("library"))
        son = session.get(Profile, "son")
        me = session.get(Profile, "me")
        assert (son.kind, son.age_years, son.age_band) == ("youth", None, "u10")
        assert (me.kind, me.age_band) == ("adult", "adult")

        stored = session.exec(select(PlannedSession).where(PlannedSession.profile_id == "son")).all()
        assert len(stored) == 16
        for planned in stored:
            rows = json.loads(planned.rows_json)
            assert len(_body(rows)) <= caps["max_exercises_per_session"], planned.id
            assert _minutes(rows) <= caps["max_session_minutes"], planned.id
            for row in rows:
                assert row["load_kg"] is None, f"{planned.id} {row['exercise_id']}"
                assert row["sets"] <= caps["max_sets_per_exercise"]
                assert row["rpe_target"] is None or row["rpe_target"] <= caps["rpe_cap"]


# --------------------------------------------------------------------------- the property-style sweep

_WEIGHTS = (
    "DB 5-52.5 lb adj step 2.5, KB 16/24 kg, BENCH adjustable",
    "DB 2-10 kg, KB 16 kg",
    "BENCH adjustable",
    "KB 16/24 kg",
    "DB banana, KB 16/24 kg",
    "",
)
_EQUIPMENT = (
    ("bodyweight", "dumbbells", "kettlebells", "bench"),
    ("bodyweight", "dumbbells"),
    ("bodyweight",),
    ("bodyweight", "bench"),
)


def test_two_hundred_random_settings_never_produce_an_illegal_youth_row(library) -> None:
    """(i) A fixed seed, so a failure reproduces exactly rather than once in a hundred runs."""
    from cadence.programme.materialise import compile_workout

    rng = random.Random(20260907)
    templates = ("son-upper-a", "son-lower-a", "son-play-day", "together-full-body", "assessment-day")
    failures: list[str] = []

    for _ in range(200):
        band = rng.choice(list(BAND_AGE))
        bodyweight = rng.choice([None, 22.0, 30.0, 40.0, 45.7, 52.0, 68.0])
        profile = _profile(
            "youth",
            band,
            bodyweight_kg=bodyweight,
            has_overhead_anchor=rng.choice([True, False]),
        )
        program_settings = DEFAULT_SETTINGS.with_changes(
            days_per_week=rng.choice(DAYS_PER_WEEK),
            session_minutes=rng.choice(SESSION_MINUTES),
            weights_available=rng.choice(_WEIGHTS),
            equipment=rng.choice(_EQUIPMENT),
        )
        template_id = rng.choice(templates)
        week = rng.randint(1, BLOCK_WEEKS)
        compiled = compile_workout(library.template(template_id), week, profile, program_settings, library)
        result = validate_workout(
            compiled,
            profile_kind="youth",
            age_band=band,
            equipment=list(program_settings.equipment),
            bodyweight_kg=bodyweight,
            has_overhead_anchor=profile.has_overhead_anchor,
            exercises=library.exercises,
            youth_rules=library.youth_rules,
        )
        if not result.ok:
            failures += [
                f"{template_id} w{week} band={band.value} bw={bodyweight} "
                f"equip={program_settings.equipment} weights={program_settings.weights_available!r} "
                f"{error.code}: {error.message}"
                for error in result.errors
                if error.severity == "error"
            ]

    assert failures == [], failures[:5]
