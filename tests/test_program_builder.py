"""PRP-01 acceptance tests 9-13 and 17-23: the program engine and ``make seed``."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from sqlmodel import Session, select

from cadence.bibliotheque.seed import seed
from cadence.db import ExerciseRecord, WorkoutRecord, init_db
from cadence.profils.settings import DEFAULT_SETTINGS, SETTING_KEYS
from cadence.profils.tables import Profile, Setting
from cadence.programme.bands import age_band, band_for_age, effective_cap, week_of_block
from cadence.programme.builder import build_program
from cadence.programme.errors import ProgramBuildError
from cadence.programme.ladder import parse_weights_available
from cadence.programme.materialise import compile_workout, materialise_rows, workout_from_rows
from cadence.programme.schemes import week_day_types, workout_id_for
from cadence.programme.substitution import BandContext, resolve_exercise
from cadence.programme.tables import PlannedSession, Program
from cadence.schema.enums import AgeBand, DayType, Equipment, LoadType, LoadUnit
from cadence.validateur import validate_workout
from cadence.validateur.youth_rules import effective_band

START = date(2026, 9, 7)
ANCHOR_ONLY = {"dead-hang", "inverted-row", "scap-pull-hang"}


def body_rows(rows: list[dict]) -> list[dict]:
    return [row for row in rows if row["role"] != "prelude"]


def _youth(band: AgeBand, **fields) -> Profile:
    ages = {AgeBand.U10: 8, AgeBand.AGE_10_13: 11, AgeBand.AGE_14_17: 15}
    return Profile(id="son", display_name="Son", kind="youth", age_years=ages[band], **fields)


# ------------------------------------------------------------------------------- bands and weeks


@pytest.mark.parametrize(
    ("age", "expected"),
    [(None, AgeBand.U10), (5, AgeBand.U10), (9, AgeBand.U10), (10, AgeBand.AGE_10_13),
     (13, AgeBand.AGE_10_13), (14, AgeBand.AGE_14_17), (17, AgeBand.AGE_14_17), (18, AgeBand.ADULT)],
)  # fmt: skip
def test_band_for_age(age: int | None, expected: AgeBand) -> None:
    assert band_for_age(age) is expected


def test_age_band_holds_a_youth_profile_inside_the_youth_rules(adult_profile: Profile) -> None:
    """The parent is always adult; a youth profile never ages out of the youth rules by arithmetic.

    Two different unknowns, two different answers (D-099). No recorded age is `u10`: nothing is
    known, so nothing is assumed. An age past the section 3.1 table is `age_14_17`: it is an
    answer the table does not extend to, and reading a correct entry of 40 as "under ten" is not
    caution, it is a wrong number. Neither leaves the youth rules, which is the property that
    matters and is what this test is named for.
    """
    assert age_band(adult_profile) is AgeBand.ADULT
    assert age_band(Profile(id="son", display_name="Son", kind="youth")) is AgeBand.U10
    assert age_band(Profile(id="son", display_name="Son", kind="youth", age_years=40)) is AgeBand.AGE_14_17

    # And the validator does not disagree: it coerces only a band of `adult`, which a youth
    # profile is now never given, so the engine cannot offer a row the gate would reject (D-066).
    for years in (None, 8, 12, 17, 18, 40):
        band = age_band(Profile(id="son", display_name="Son", kind="youth", age_years=years))
        assert effective_band("youth", band) == (band, False)


def test_effective_cap_takes_the_lower_of_the_two(library) -> None:
    rules = library.youth_rules[AgeBand.AGE_14_17]
    assert effective_cap(rules, LoadUnit.PER_HAND, None) == 12.0
    assert effective_cap(rules, LoadUnit.PER_HAND, 40.0) == 10.0
    assert effective_cap(rules, LoadUnit.PER_IMPLEMENT, 40.0) == 14.0
    assert effective_cap(rules, LoadUnit.BODYWEIGHT, 40.0) is None
    assert effective_cap(library.youth_rules[AgeBand.ADULT], LoadUnit.PER_HAND, 80.0) is None


@pytest.mark.parametrize(
    ("completed", "days", "expected"),
    [(0, 4, 1), (3, 4, 1), (4, 4, 2), (11, 4, 3), (12, 4, 4), (40, 4, 4), (0, 2, 1), (2, 2, 2)],
)
def test_week_of_block(completed: int, days: int, expected: int) -> None:
    assert week_of_block(completed, days) == expected


def test_week_of_block_rejects_a_zero_week() -> None:
    with pytest.raises(ValueError):
        week_of_block(4, 0)


# ---------------------------------------------------------------------------------- the block plan


def test_program_4day_30min_has_16_sessions(library, adult_profile: Profile) -> None:
    """9. Sixteen sessions, every one opening on the prelude, the first of them the assessment."""
    plan = build_program(adult_profile, DEFAULT_SETTINGS, library, START)
    assert plan.session_count == 16
    assert plan.sessions[0].day_type == DayType.ASSESSMENT.value
    assert sum(1 for s in plan.sessions if s.day_type == DayType.ASSESSMENT.value) == 1
    for planned in plan.sessions:
        rows = json.loads(planned.rows_json)
        assert rows[0]["role"] == "prelude"
        assert rows[0]["exercise_id"] == "cat-cow"


def test_prelude_is_prepended_to_assessment_day_too(library, adult_profile: Profile) -> None:
    """D-051: section 2 is categorical, section 10.10's list is the measured block only."""
    plan = build_program(adult_profile, DEFAULT_SETTINGS, library, START)
    rows = json.loads(plan.sessions[0].rows_json)
    assert [row["exercise_id"] for row in rows if row["role"] == "prelude"] == [
        "cat-cow",
        "open-book",
        "wall-angel",
        "glute-bridge",
        "dead-bug",
    ]


def test_youth_prelude_drops_dead_bug(library, youth_profile: Profile) -> None:
    rows = materialise_rows(library.template("son-lower-a"), 1, youth_profile, DEFAULT_SETTINGS, library)
    prelude = [row for row in rows if row["role"] == "prelude"]
    assert [row["exercise_id"] for row in prelude] == ["cat-cow", "open-book", "wall-angel", "glute-bridge"]
    assert prelude[1]["reps"] == 4


@pytest.mark.parametrize(("minutes", "expected"), [(15, 3), (30, 5), (45, 7)])
def test_30min_session_has_five_rows_excluding_prelude(
    library, adult_profile: Profile, minutes: int, expected: int
) -> None:
    """10. The section 6.4 row budget, and a session that always ends on a finisher."""
    settings = DEFAULT_SETTINGS.with_changes(session_minutes=minutes)
    for workout_id in ("upper-a", "lower-a", "upper-b", "lower-full-b", "mobility-carry-day"):
        rows = body_rows(materialise_rows(library.template(workout_id), 1, adult_profile, settings, library))
        assert len(rows) == expected, workout_id
        assert rows[-1]["role"] == "finisher", workout_id


def test_days_per_week_mapping(library, adult_profile: Profile, youth_profile: Profile) -> None:
    """11. Section 6.3, including the three-day rotation continuing across weeks."""
    assert week_day_types(1, 2, is_youth=False) == (DayType.UPPER_A, DayType.LOWER_FULL_B)
    assert week_day_types(1, 3, is_youth=False) == (DayType.UPPER_A, DayType.LOWER_A, DayType.UPPER_B)
    assert week_day_types(2, 3, is_youth=False) == (DayType.LOWER_FULL_B, DayType.UPPER_A, DayType.LOWER_A)
    assert week_day_types(1, 4, is_youth=False)[-1] is DayType.LOWER_FULL_B
    assert week_day_types(1, 5, is_youth=False)[-1] is DayType.MOBILITY_CARRY
    assert week_day_types(1, 6, is_youth=False)[-1] is DayType.UPPER_A
    assert week_day_types(1, 6, is_youth=True)[-2:] == (DayType.MOBILITY_CARRY, DayType.MOBILITY_CARRY)

    assert workout_id_for(DayType.MOBILITY_CARRY, "adult") == "mobility-carry-day"
    assert workout_id_for(DayType.MOBILITY_CARRY, "youth") == "son-play-day"

    five = DEFAULT_SETTINGS.with_changes(days_per_week=5)
    adult_plan = build_program(adult_profile, five, library, START)
    youth_plan = build_program(youth_profile, five, library, START)
    assert adult_plan.sessions[4].workout_id == "mobility-carry-day"
    assert youth_plan.sessions[4].workout_id == "son-play-day"

    six = DEFAULT_SETTINGS.with_changes(days_per_week=6)
    youth_six = build_program(youth_profile, six, library, START)
    play_days = [s for s in youth_six.sessions[:6] if s.workout_id == "son-play-day"]
    assert len(play_days) == 2
    assert build_program(adult_profile, six, library, START).sessions[5].workout_id == "upper-a"


def test_two_day_week_gives_eight_sessions(library, adult_profile: Profile) -> None:
    settings = DEFAULT_SETTINGS.with_changes(days_per_week=2)
    assert build_program(adult_profile, settings, library, START).session_count == 8


# ------------------------------------------------------------------------------------ week schemes


def test_week_schemes(library, adult_profile: Profile) -> None:
    """13. Main rows run 3x8 / 3x10 / 4x8, and the template's own week 1 wins (P7)."""
    per_week = {
        week: body_rows(materialise_rows(library.template("upper-a"), week, adult_profile, DEFAULT_SETTINGS, library))
        for week in (1, 2, 3, 4)
    }
    mains = [per_week[week][0] for week in (1, 2, 3)]
    assert [row["reps"] for row in mains] == [8, 10, 8]
    assert [row["sets"] for row in mains] == [3, 3, 4]

    planks = [per_week[week][-1] for week in (1, 2, 3)]
    assert [row["exercise_id"] for row in planks] == ["plank"] * 3
    # Section 10.2 prescribes 40 s in week 1, not section 6.2's canonical 30 s.
    assert [row["seconds"] for row in planks] == [40, 50, 60]


def test_deload_week_reduces_sets(library, adult_profile: Profile) -> None:
    """12. Week 4 is two sets at the week-1 value, with the load unchanged (section 5.5)."""
    week3 = body_rows(materialise_rows(library.template("upper-a"), 3, adult_profile, DEFAULT_SETTINGS, library))
    week4 = body_rows(materialise_rows(library.template("upper-a"), 4, adult_profile, DEFAULT_SETTINGS, library))
    week1 = body_rows(materialise_rows(library.template("upper-a"), 1, adult_profile, DEFAULT_SETTINGS, library))
    assert len(week3) == len(week4)
    for third, fourth, first in zip(week3, week4, week1, strict=True):
        assert fourth["sets"] == max(2, int(third["sets"] * 0.60))
        assert fourth["load_kg"] == third["load_kg"]
        if fourth["reps"] is not None:
            assert fourth["reps"] == fourth["progression"]["rep_min"] == first["reps"]
        if fourth["seconds"] is not None:
            assert fourth["seconds"] == first["seconds"]


# ------------------------------------------------------------------------ youth clamping and swaps


def test_youth_load_clamped_not_rejected(library) -> None:
    """17. A load over the band cap shrinks and the row survives, with a note saying so."""
    son = _youth(AgeBand.AGE_10_13, bodyweight_kg=40.0)
    rows = body_rows(materialise_rows(library.template("together-full-body"), 1, son, DEFAULT_SETTINGS, library))
    goblet = next(row for row in rows if row["exercise_id"] == "goblet-squat")
    assert goblet["load_kg"] is not None
    assert goblet["load_kg"] <= 8.0
    assert goblet["notes"], "a clamped youth row must say why it is light"
    assert goblet["progression"]["cap_load_kg"] == 8.0
    assert goblet["progression"]["allow_load_progression"] is False


def test_u10_substitution(library, youth_profile: Profile) -> None:
    """18. At u10 the named substitutes take rows 2 and 3 of `son-upper-a`."""
    rows = body_rows(materialise_rows(library.template("son-upper-a"), 1, youth_profile, DEFAULT_SETTINGS, library))
    assert rows[1]["exercise_id"] == "prone-ytw-raise"
    assert rows[2]["exercise_id"] == "scap-push-up"
    assert all(row["load_kg"] is None for row in rows), "u10 carries no external load"
    assert any("play" in library.exercise(row["exercise_id"]).tags for row in rows) or any(
        row["exercise_id"] == "bear-crawl" for row in rows
    )


def test_youth_session_keeps_a_play_row(library, youth_profile: Profile) -> None:
    """Section 6.5: every son session carries at least one fun row (D-059)."""
    for workout_id in ("son-upper-a", "son-lower-a", "son-play-day"):
        rows = body_rows(materialise_rows(library.template(workout_id), 1, youth_profile, DEFAULT_SETTINGS, library))
        tags = {tag for row in rows for tag in library.exercise(row["exercise_id"]).tags}
        assert any(tag.value == "play" for tag in tags), workout_id


def test_kettlebell_conditional(library) -> None:
    """19. Section 3.6's chain: the bell, the lighter lift, or the bodyweight hinge."""
    kb_swing = library.exercise("kb-swing")
    column = library.template("lower-full-b").extras[0].column("youth")

    def resolve(bodyweight: float | None) -> str | None:
        ctx = BandContext(
            kind="youth",
            band=AgeBand.AGE_14_17,
            rules=library.youth_rules[AgeBand.AGE_14_17],
            equipment=frozenset(Equipment),
            has_overhead_anchor=False,
            bodyweight_kg=bodyweight,
            library=library,
        )
        chosen, _ = resolve_exercise(kb_swing, column, ctx)
        return chosen.id if chosen else None

    assert resolve(40.0) == "kb-deadlift"
    assert resolve(50.0) == "kb-swing"
    assert resolve(None) == "hip-hinge-bw"


def test_no_anchor_filters_pull_v(library, adult_profile: Profile) -> None:
    """20. Without an overhead bar nothing that needs one is ever scheduled (section 1.7)."""
    plan = build_program(adult_profile, DEFAULT_SETTINGS, library, START)
    scheduled = {row["exercise_id"] for planned in plan.sessions for row in json.loads(planned.rows_json)}
    assert scheduled & ANCHOR_ONLY == set()

    rows = body_rows(materialise_rows(library.template("upper-b"), 1, adult_profile, DEFAULT_SETTINGS, library))
    assert rows[3]["exercise_id"] == "db-floor-pullover"

    with_bar = Profile(id="me", display_name="Me", kind="adult", has_overhead_anchor=True)
    upgraded = body_rows(materialise_rows(library.template("upper-b"), 1, with_bar, DEFAULT_SETTINGS, library))
    assert (upgraded[3]["exercise_id"], upgraded[3]["sets"], upgraded[3]["reps"]) == ("scap-pull-hang", 3, 8)


def test_dead_hang_is_dropped_from_the_battery_without_an_anchor(library, adult_profile: Profile) -> None:
    """Section 1.7: the hang is recorded `unavailable`, never substituted by another test."""
    rows = body_rows(materialise_rows(library.template("assessment-day"), 1, adult_profile, DEFAULT_SETTINGS, library))
    assert "dead-hang" not in {row["exercise_id"] for row in rows}
    assert len(rows) == 5

    with_bar = Profile(id="me", display_name="Me", kind="adult", has_overhead_anchor=True)
    full = body_rows(materialise_rows(library.template("assessment-day"), 1, with_bar, DEFAULT_SETTINGS, library))
    assert [row["exercise_id"] for row in full] == [
        "wall-angel",
        "goblet-squat",
        "push-up",
        "dead-hang",
        "plank",
        "farmer-carry",
    ]


def test_youth_assessment_takes_the_band_target(library) -> None:
    """Section 7.4's fun target, not the cap column, which u10's rep ceiling would reject (D-062)."""
    targets = {}
    for band in (AgeBand.U10, AgeBand.AGE_10_13, AgeBand.AGE_14_17):
        rows = body_rows(
            materialise_rows(library.template("assessment-day"), 1, _youth(band), DEFAULT_SETTINGS, library)
        )
        push_up = next(row for row in rows if row["exercise_id"] == "push-up")
        targets[band] = push_up["reps"]
    assert targets == {AgeBand.U10: 8, AgeBand.AGE_10_13: 12, AgeBand.AGE_14_17: 20}


def test_youth_session_never_exceeds_the_band_clock(library) -> None:
    """Section 3.2's minute cap wins over the section 6.4 row count (P1)."""
    from cadence.programme.materialise import estimated_minutes, make_context

    settings = DEFAULT_SETTINGS.with_changes(session_minutes=45)
    for band in (AgeBand.U10, AgeBand.AGE_10_13, AgeBand.AGE_14_17):
        profile = _youth(band)
        ctx = make_context(profile, settings, library)
        for workout_id in ("son-upper-a", "son-lower-a", "son-play-day"):
            rows = materialise_rows(library.template(workout_id), 1, profile, settings, library)
            assert estimated_minutes(rows) <= ctx.effective_minutes, (band, workout_id)


def test_build_program_is_deterministic(library, adult_profile: Profile, youth_profile: Profile) -> None:
    """23. Two calls with equal inputs produce byte-identical rows."""
    for profile in (adult_profile, youth_profile):
        first = build_program(profile, DEFAULT_SETTINGS, library, START)
        second = build_program(profile, DEFAULT_SETTINGS, library, START)
        assert [s.rows_json for s in first.sessions] == [s.rows_json for s in second.sessions]
        assert [s.id for s in first.sessions] == [s.id for s in second.sessions]


def test_build_program_saves_nothing(library, adult_profile: Profile, settings) -> None:
    """9 (risk): the builder is pure, so PRP-03 can splice a rebuild into an existing plan."""
    engine = init_db(settings)
    with Session(engine) as session:
        build_program(adult_profile, DEFAULT_SETTINGS, library, START)
        assert session.exec(select(Program)).all() == []
        assert session.exec(select(PlannedSession)).all() == []


# -------------------------------------------------------------------------------------- make seed


def test_seed_creates_two_profiles_and_two_programs(settings) -> None:
    """22. The two demo profiles, one active block each."""
    engine = init_db(settings)
    with Session(engine) as session:
        report = seed(session, Path("library"))
        assert report.exercises == 52
        assert report.workouts == 11
        assert report.profiles == ("me", "son")
        assert report.sessions == {"me": 16, "son": 16}

        profiles = {row.id: row for row in session.exec(select(Profile)).all()}
        assert profiles["me"].kind == "adult"
        assert profiles["me"].push_to_garmin is True
        assert profiles["son"].kind == "youth"
        assert profiles["son"].age_years is None
        assert profiles["son"].age_band == AgeBand.U10.value
        assert profiles["son"].push_to_garmin is False

        assert len(session.exec(select(Program)).all()) == 2
        assert len(session.exec(select(PlannedSession)).all()) == 32
        # Every key this build knows, not a number to re-edit each time a PRP adds one
        # (PRP-03 added `display_unit`, D-090).
        assert len(session.exec(select(Setting)).all()) == len(SETTING_KEYS)
        assert "seeded 52 exercises" in report.summary()


def test_seed_is_idempotent(settings) -> None:
    """21. Running seed twice leaves identical counts and no duplicate ids."""
    engine = init_db(settings)
    with Session(engine) as session:
        seed(session, Path("library"))
        first = [row.id for row in session.exec(select(PlannedSession)).all()]
        seed(session, Path("library"))
        second = [row.id for row in session.exec(select(PlannedSession)).all()]
        assert sorted(first) == sorted(second)
        assert len(set(second)) == len(second) == 32
        assert len(session.exec(select(ExerciseRecord)).all()) == 52
        assert len(session.exec(select(WorkoutRecord)).all()) == 11


def test_seed_leaves_imported_rows_alone(settings) -> None:
    """An imported workout survives a re-seed: seed only ever touches `source = seed` rows."""
    engine = init_db(settings)
    with Session(engine) as session:
        seed(session, Path("library"))
        session.add(
            WorkoutRecord(
                id="pasted-upper",
                doc_json="{}",
                source="import",
                target_profile_kind="adult",
                created_at="2026-09-07T00:00:00+00:00",
            )
        )
        session.commit()
        seed(session, Path("library"))
        imported = session.get(WorkoutRecord, "pasted-upper")
        assert imported is not None
        assert imported.doc_json == "{}"


def test_seed_reset_clears_seed_rows_first(settings) -> None:
    engine = init_db(settings)
    with Session(engine) as session:
        seed(session, Path("library"))
        report = seed(session, Path("library"), reset=True)
        assert report.sessions == {"me": 16, "son": 16}
        assert len(session.exec(select(ExerciseRecord)).all()) == 52


# ------------------------------------------------------------------------------ load rule resolution


def test_relative_load_rules_follow_the_row_they_name(library, adult_profile: Profile) -> None:
    """Section 10.2: the row matches the press load, the overhead press takes ~60 % of it."""
    rows = body_rows(materialise_rows(library.template("upper-a"), 1, adult_profile, DEFAULT_SETTINGS, library))
    loads = {row["exercise_id"]: row["load_kg"] for row in rows}
    assert loads["db-bent-row"] == loads["db-bench-press"]
    assert loads["db-overhead-press"] < loads["db-bench-press"]
    assert loads["db-overhead-press"] == 6.8
    assert loads["prone-ytw-raise"] is None


def test_absolute_load_rules_round_down_to_what_the_household_owns(library, adult_profile: Profile) -> None:
    """Section 10.5: the 24 kg bell exists, so it is used; 16 kg dumbbells do not, so 15.88 is."""
    rows = body_rows(materialise_rows(library.template("lower-full-b"), 1, adult_profile, DEFAULT_SETTINGS, library))
    loads = {row["exercise_id"]: row["load_kg"] for row in rows}
    assert loads["kb-deadlift"] == 24.0
    assert loads["farmer-carry"] == 15.88


def test_a_row_demotes_when_no_rung_is_legal(library) -> None:
    """Section 8.5: with no dumbbells the goblet squat becomes the bodyweight squat, not a blank."""
    settings = DEFAULT_SETTINGS.with_changes(weights_available="BENCH adjustable")
    profile = Profile(id="me", display_name="Me", kind="adult")
    rows = body_rows(materialise_rows(library.template("lower-a"), 1, profile, settings, library))
    assert rows[0]["exercise_id"] == "bodyweight-squat"
    assert rows[0]["load_kg"] is None
    assert any("no legal load" in note for note in rows[0]["notes"])


def test_rows_json_carries_the_documented_shape(library, adult_profile: Profile) -> None:
    """The keys PRP-01 specifies for ``planned_session.rows_json``, plus the D-057 progression."""
    rows = materialise_rows(library.template("upper-a"), 1, adult_profile, DEFAULT_SETTINGS, library)
    expected = {
        "position", "exercise_id", "role", "name", "cue", "sets", "reps", "seconds", "meters",
        "steps", "per_side", "rest_s", "load_kg", "load_unit", "measure", "garmin_category",
        "is_challenge", "notes", "progression", "rpe_target", "amrap", "is_prelude",
        "assessment_id",
    }  # fmt: skip
    assert set(rows[0]) == expected
    assert [row["position"] for row in rows] == list(range(1, len(rows) + 1))
    assert rows[0]["notes"] == []
    assert rows[0]["garmin_category"] == "WARM_UP"


def test_challenge_rows_are_appended_and_flagged(library, adult_profile: Profile) -> None:
    """The PRP-07 seam: this PRP carries a challenge row through and marks it."""
    base = materialise_rows(library.template("upper-a"), 1, adult_profile, DEFAULT_SETTINGS, library)
    with_challenge = materialise_rows(
        library.template("upper-a"),
        1,
        adult_profile,
        DEFAULT_SETTINGS,
        library,
        challenge_rows=[{"exercise_id": "push-up", "role": "secondary", "sets": 3, "reps": 10}],
    )
    assert len(with_challenge) == len(base) + 1
    assert with_challenge[-1]["is_challenge"] is True
    assert with_challenge[-1]["position"] == len(with_challenge)


# ------------------------------------------------------------------- every session of every block


def _validate_plan(library, profile: Profile, settings) -> list[str]:
    """Validate every session the builder actually produces, at the week it produces it for."""
    band = age_band(profile)
    kind = "youth" if profile.kind == "youth" else "adult"
    failures: list[str] = []
    for planned in build_program(profile, settings, library, START).sessions:
        compiled = compile_workout(library.template(planned.workout_id), planned.week, profile, settings, library)
        result = validate_workout(
            compiled,
            profile_kind=kind,
            age_band=band,
            equipment=list(settings.equipment),
            bodyweight_kg=profile.bodyweight_kg,
            has_overhead_anchor=profile.has_overhead_anchor,
            exercises=library.exercises,
            youth_rules=library.youth_rules,
        )
        if not result.ok:
            failures += [
                f"{planned.id} ({planned.workout_id} w{planned.week}) {e.code}: {e.message}"
                for e in result.errors
                if e.severity == "error"
            ]
    return failures


@pytest.mark.parametrize("band", [AgeBand.U10, AgeBand.AGE_10_13, AgeBand.AGE_14_17])
@pytest.mark.parametrize("minutes", [15, 30, 45])
def test_every_planned_youth_session_validates(library, band: AgeBand, minutes: int) -> None:
    """Weeks 2-4 are where the numbers peak, and 12 of the son's 16 sessions live there.

    The week scheme adds a set to main rows in week 3 and two steps to every secondary, so a
    session legal in week 1 is not automatically legal in week 3. This validates what the builder
    actually stores, not a sample of it.
    """
    settings = DEFAULT_SETTINGS.with_changes(session_minutes=minutes)
    assert _validate_plan(library, _youth(band), settings) == []


@pytest.mark.parametrize("minutes", [15, 30, 45])
def test_every_planned_adult_session_validates(library, adult_profile: Profile, minutes: int) -> None:
    settings = DEFAULT_SETTINGS.with_changes(session_minutes=minutes)
    assert _validate_plan(library, adult_profile, settings) == []


@pytest.mark.parametrize("band", [AgeBand.U10, AgeBand.AGE_10_13, AgeBand.AGE_14_17])
def test_youth_with_an_overhead_anchor_still_validates(library, band: AgeBand) -> None:
    """The bar puts `dead-hang` back in the battery, and the u10 exercise cap is exactly five.

    The loader only ever checks the shipped default of no anchor, so this is the configuration a
    household changes in Settings and nothing else covers.
    """
    profile = _youth(band, has_overhead_anchor=True)
    assert _validate_plan(library, profile, DEFAULT_SETTINGS) == []
    rows = body_rows(materialise_rows(library.template("assessment-day"), 1, profile, DEFAULT_SETTINGS, library))
    assert "dead-hang" in {row["exercise_id"] for row in rows}


def test_youth_with_a_recorded_bodyweight_still_validates(library) -> None:
    """A known bodyweight turns the percentage caps on, which only ever tightens them."""
    for band, weight in ((AgeBand.AGE_10_13, 32.0), (AgeBand.AGE_14_17, 50.0)):
        assert _validate_plan(library, _youth(band, bodyweight_kg=weight), DEFAULT_SETTINGS) == []


# ------------------------------------------------------------------------------ rebuild and history


def test_reseeding_after_a_settings_change_keeps_completed_sessions(settings) -> None:
    """A finished session is history, not a slot the next plan may reclaim.

    Changing days-per-week reshapes the queue ahead of the user; it must not erase what they have
    already done behind them.
    """
    engine = init_db(settings)
    with Session(engine) as session:
        seed(session, Path("library"))
        done = session.exec(
            select(PlannedSession).where(PlannedSession.profile_id == "me", PlannedSession.week == 1)
        ).all()[-1]
        done_id, done_rows = done.id, done.rows_json
        done.status = "done"
        skipped = session.exec(
            select(PlannedSession).where(PlannedSession.profile_id == "me", PlannedSession.week == 4)
        ).all()[-1]
        skipped_id = skipped.id
        skipped.status = "skipped"

        three_days = json.dumps(3)
        session.get(Setting, "days_per_week").value_json = three_days
        session.commit()

        seed(session, Path("library"))

        survivor = session.get(PlannedSession, done_id)
        assert survivor is not None, "a completed session was deleted by the rebuild"
        assert survivor.status == "done"
        assert survivor.rows_json == done_rows, "a completed session was rewritten by the rebuild"
        assert session.get(PlannedSession, skipped_id) is not None

        planned = session.exec(
            select(PlannedSession).where(PlannedSession.profile_id == "me", PlannedSession.status == "planned")
        ).all()
        # Twelve slots in a four-week block at three days a week, two of them already spent on the
        # done and the skipped session, so ten are left to do. `make seed` and the Settings screen
        # share one rebuild now (D-098d), and this is that rule: a finished session occupies its
        # place in the block rather than having a fresh one appended past the end of it.
        # Twelve slots in a four-week block at three days a week. The done session spent one of
        # them; the skipped one did not, because a day that did not happen must not shorten the
        # plan (D-111). So eleven are left to do.
        assert len(planned) == 11, "the new three-day block replaces only the sessions still pending"


def test_reseeding_drops_planned_sessions_the_new_block_does_not_need(settings) -> None:
    engine = init_db(settings)
    with Session(engine) as session:
        seed(session, Path("library"))
        assert len(session.exec(select(PlannedSession)).all()) == 32
        session.get(Setting, "days_per_week").value_json = json.dumps(2)
        session.commit()
        seed(session, Path("library"))
        # Two profiles at two days a week: eight sessions each, none of them left over.
        assert len(session.exec(select(PlannedSession)).all()) == 16


# ------------------------------------------------------------- degraded households (review 2, 3, 6)


def _household(equipment: list[str], weights: str):
    return DEFAULT_SETTINGS.with_changes(equipment=equipment, weights_available=weights)


BODYWEIGHT_AND_BENCH = (["bodyweight", "bench"], "BENCH adjustable")
DUMBBELLS_AND_BENCH = (["bodyweight", "dumbbells", "bench"], "DB 5-52.5 lb adj step 2.5, BENCH adjustable")
COMPOUND_DAYS = ("upper-a", "lower-a", "upper-b", "lower-full-b")


@pytest.mark.parametrize("household", [BODYWEIGHT_AND_BENCH, DUMBBELLS_AND_BENCH])
@pytest.mark.parametrize("workout_id", COMPOUND_DAYS)
def test_a_compound_day_always_ships_with_a_main(library, adult_profile: Profile, household, workout_id: str) -> None:
    """Selection runs over resolved rows, so a missing implement cannot leave a day with no main."""
    rows = body_rows(materialise_rows(library.template(workout_id), 1, adult_profile, _household(*household), library))
    assert rows, workout_id
    assert rows[0]["role"] == "main", (workout_id, [row["exercise_id"] for row in rows])


@pytest.mark.parametrize("household", [BODYWEIGHT_AND_BENCH, DUMBBELLS_AND_BENCH])
@pytest.mark.parametrize("workout_id", [*COMPOUND_DAYS, "mobility-carry-day"])
def test_no_exercise_is_prescribed_twice(library, adult_profile: Profile, household, workout_id: str) -> None:
    """Two rows whose implements are both missing must not collapse onto the same substitute."""
    rows = body_rows(materialise_rows(library.template(workout_id), 1, adult_profile, _household(*household), library))
    ids = [row["exercise_id"] for row in rows]
    assert len(ids) == len(set(ids)), (workout_id, ids)


def test_bodyweight_household_gets_a_push_for_its_main(library, adult_profile: Profile) -> None:
    """`db-bench-press` reaches only `db-floor-press` before the graph ends, so the pattern fills it."""
    rows = body_rows(
        materialise_rows(library.template("upper-a"), 1, adult_profile, _household(*BODYWEIGHT_AND_BENCH), library)
    )
    main = rows[0]
    assert main["exercise_id"] == "push-up"
    assert library.exercise(main["exercise_id"]).pattern is library.exercise("db-bench-press").pattern
    assert any("main lift" in note for note in main["notes"])


def test_no_kettlebells_falls_back_to_the_declared_exercise(library, adult_profile: Profile) -> None:
    """`lower-full-b` names `db-rdl` as the fallback for its kettlebell deadlift (review 3)."""
    rows = body_rows(
        materialise_rows(library.template("lower-full-b"), 1, adult_profile, _household(*DUMBBELLS_AND_BENCH), library)
    )
    assert rows[0]["exercise_id"] == "db-rdl"
    assert rows[0]["role"] == "main"
    assert rows[0]["load_kg"] is not None


def test_a_substitute_never_lands_on_a_prelude_movement(library, adult_profile: Profile) -> None:
    """Warming up with a movement and then training it reads as a bug, and dodges the V6 count."""
    for workout_id in [*COMPOUND_DAYS, "mobility-carry-day"]:
        rows = materialise_rows(
            library.template(workout_id), 1, adult_profile, _household(*BODYWEIGHT_AND_BENCH), library
        )
        prelude = {row["exercise_id"] for row in rows if row["role"] == "prelude"}
        for row in body_rows(rows):
            declared = {r.exercise for r in library.template(workout_id).all_rows()}
            assert row["exercise_id"] not in prelude or row["exercise_id"] in declared, (workout_id, row["exercise_id"])


def test_a_day_with_no_legal_main_refuses_to_build(library, adult_profile: Profile) -> None:
    """Nothing left to substitute to is the one case the engine refuses rather than papers over."""
    kettlebells_only = _household(["kettlebells"], "KB 16/24 kg")
    with pytest.raises(ProgramBuildError) as caught:
        materialise_rows(library.template("upper-a"), 1, adult_profile, kettlebells_only, library)
    assert "upper-a" in str(caught.value)
    assert "no legal main row" in str(caught.value)


def test_a_day_type_with_no_compound_is_not_held_to_one(library, adult_profile: Profile) -> None:
    """Section 6.1 gives mobility_carry no main, so its absence is not a failure."""
    rows = body_rows(
        materialise_rows(library.template("mobility-carry-day"), 1, adult_profile, DEFAULT_SETTINGS, library)
    )
    assert not any(row["role"] == "main" for row in rows)
    assert rows[-1]["role"] == "finisher"


# ---------------------------------------------------------------- band caps and challenge rows (5, 7)


CHALLENGE_ROW = {
    "exercise_id": "jumping-jacks", "role": "secondary", "name": "Jumping jacks", "cue": "Land soft.",
    "sets": 2, "reps": 12, "seconds": None, "meters": None, "steps": None, "per_side": False,
    "rest_s": 45, "rpe_target": None, "amrap": False, "load_kg": None, "load_unit": "bodyweight",
    "measure": "reps", "garmin_category": "CARDIO", "is_prelude": False, "is_challenge": False,
    "assessment_id": None, "notes": [], "progression": {},
}  # fmt: skip


def test_a_challenge_row_is_dropped_rather_than_breaking_a_band_cap(library) -> None:
    """P5: the challenge row may sit one over the row count, never over max_exercises_per_session."""
    at_the_cap = _youth(AgeBand.U10)
    rows = materialise_rows(
        library.template("son-upper-a"), 1, at_the_cap, DEFAULT_SETTINGS, library, challenge_rows=[dict(CHALLENGE_ROW)]
    )
    assert not any(row["is_challenge"] for row in rows), "u10 already has five counted rows"

    with_room = _youth(AgeBand.AGE_10_13)
    kept = materialise_rows(
        library.template("son-upper-a"), 1, with_room, DEFAULT_SETTINGS, library, challenge_rows=[dict(CHALLENGE_ROW)]
    )
    assert sum(1 for row in kept if row["is_challenge"]) == 1
    assert body_rows(kept)[-1]["is_challenge"] is True


@pytest.mark.parametrize("band", [AgeBand.U10, AgeBand.AGE_10_13, AgeBand.AGE_14_17])
def test_a_session_with_a_challenge_row_still_validates(library, band: AgeBand) -> None:
    """The row joins before the caps are checked, so nothing reaches the gate unmeasured."""
    profile = _youth(band)
    for workout_id in ("son-upper-a", "son-lower-a", "son-play-day"):
        rows = materialise_rows(
            library.template(workout_id), 1, profile, DEFAULT_SETTINGS, library, challenge_rows=[dict(CHALLENGE_ROW)]
        )
        result = validate_workout(
            workout_from_rows(library.template(workout_id), rows, is_youth=True),
            profile_kind="youth",
            age_band=band,
            equipment=list(DEFAULT_SETTINGS.equipment),
            exercises=library.exercises,
            youth_rules=library.youth_rules,
        )
        assert result.ok, [e.message for e in result.errors if e.severity == "error"]


def test_the_trim_uses_the_clock_the_validator_will_use(library) -> None:
    """Two clocks that disagree is how a session is trimmed to fit one and rejected by the other."""
    from cadence.programme.materialise import make_context
    from cadence.programme.prescription import band_minutes, estimated_minutes, validator_minutes

    profile = _youth(AgeBand.U10)
    ctx = make_context(profile, DEFAULT_SETTINGS, library)
    rows = materialise_rows(library.template("son-lower-a"), 1, profile, DEFAULT_SETTINGS, library)
    assert band_minutes(rows, ctx, assessment=False) == max(estimated_minutes(rows), validator_minutes(rows, ctx))
    assert band_minutes(rows, ctx, assessment=False) <= ctx.effective_minutes
    # Assessment day is judged on the derived figure alone, exactly as V9 judges it.
    battery = materialise_rows(library.template("assessment-day"), 1, profile, DEFAULT_SETTINGS, library)
    assert band_minutes(battery, ctx, assessment=True) == validator_minutes(battery, ctx)


# ------------------------------------------------------- unparsable weights demote (review 8)


UNUSABLE_WEIGHTS = ["", "DB banana", "DB 0 kg", "DB 1000 kg"]


@pytest.mark.parametrize("weights", UNUSABLE_WEIGHTS)
@pytest.mark.parametrize("workout_id", COMPOUND_DAYS)
def test_unusable_weights_demote_every_loaded_row(
    library, adult_profile: Profile, weights: str, workout_id: str
) -> None:
    """Section 8.2: ignore the token, warn, fall back to bodyweight for that implement, never guess.

    The household still owns dumbbells, so the equipment check passes and the rows only fail at the
    ladder. Every one of them must land on a bodyweight movement rather than being dropped - which
    took the main row with it, and the build refused.
    """
    settings = DEFAULT_SETTINGS.with_changes(weights_available=weights)
    rows = body_rows(materialise_rows(library.template(workout_id), 1, adult_profile, settings, library))
    assert rows[0]["role"] == "main", workout_id
    for row in rows:
        assert row["load_kg"] is None, (workout_id, row["exercise_id"])
        assert library.exercise(row["exercise_id"]).load_unit is LoadUnit.BODYWEIGHT, row["exercise_id"]


@pytest.mark.parametrize("weights", UNUSABLE_WEIGHTS)
def test_unusable_weights_still_build_a_whole_block(
    library, adult_profile: Profile, youth_profile: Profile, weights: str
) -> None:
    settings = DEFAULT_SETTINGS.with_changes(weights_available=weights)
    for profile in (adult_profile, youth_profile):
        assert build_program(profile, settings, library, START).session_count == 16


@pytest.mark.parametrize("weights", ["DB banana", "DB 0 kg", "DB 1000 kg"])
def test_an_unreadable_weights_token_is_named_in_a_warning(weights: str) -> None:
    parsed = parse_weights_available(weights)
    assert parsed.ladder(LoadType.DUMBBELL) == []
    assert any(weights.split()[1] in warning for warning in parsed.warnings), parsed.warnings


def test_the_demotion_crosses_a_loaded_link_to_reach_the_floor(library, adult_profile: Profile) -> None:
    """`db-bench-press` reaches only `db-floor-press`, still a dumbbell, so the pattern finishes it.

    Section 9's push_h links are two disconnected components, so walking the easier-than graph
    alone - however deeply - never arrives at a bodyweight push.
    """
    assert library.easier_than["db-bench-press"] == ("db-floor-press",)
    assert library.easier_than["db-floor-press"] == ()
    settings = DEFAULT_SETTINGS.with_changes(weights_available="")
    main = body_rows(materialise_rows(library.template("upper-a"), 1, adult_profile, settings, library))[0]
    assert main["exercise_id"] == "push-up"
    assert any("no legal load" in note or "main lift" in note for note in main["notes"])


def test_a_session_with_no_rows_at_all_refuses_by_name(library, adult_profile: Profile) -> None:
    """Unticking bodyweight drops the prelude and every substitute; say so, do not raise on a tuple."""
    settings = DEFAULT_SETTINGS.with_changes(equipment=["dumbbells"], weights_available="")
    # mobility_carry declares no main (section 6.1), so it reaches the empty-session guard rather
    # than the missing-main one, which every other day type hits first.
    with pytest.raises(ProgramBuildError) as caught:
        materialise_rows(library.template("mobility-carry-day"), 1, adult_profile, settings, library)
    assert "no rows at all" in str(caught.value)
    assert "equipment ['dumbbells']" in str(caught.value)

    with pytest.raises(ProgramBuildError) as missing_main:
        materialise_rows(library.template("upper-a"), 1, adult_profile, settings, library)
    assert "no legal main row" in str(missing_main.value)


def test_reseeding_keeps_the_date_the_block_began(settings) -> None:
    """Re-seeding is not starting over, so start_date is the one field a rebuild leaves alone."""
    engine = init_db(settings)
    with Session(engine) as session:
        seed(session, Path("library"))
        program = session.get(Program, "me-block-1")
        program.start_date = "2026-01-15"
        session.commit()

        seed(session, Path("library"))
        assert session.get(Program, "me-block-1").start_date == "2026-01-15"
