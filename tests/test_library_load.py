"""PRP-01 acceptance tests 1-8: the seed library loads, links and validates.

The expected id set is transcribed from ``docs/exercise-principles.md`` section 9 and written out
literally here rather than derived from the YAML. That is the point: if the library drifts from
the principles, this test fails, and the bug is in the library.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from cadence.bibliotheque.loader import LibraryError, load_library
from cadence.bibliotheque.template import WorkoutTemplate
from cadence.profils.settings import DEFAULT_SETTINGS
from cadence.profils.tables import Profile
from cadence.programme.materialise import compile_workout
from cadence.schema import AgeBand, Exercise, WorkoutRow, YouthAllow
from cadence.validateur import validate_workout

PRINCIPLES_EXERCISE_IDS: frozenset[str] = frozenset(
    {
        # 9.1 Hinge
        "hip-hinge-bw", "glute-bridge", "bench-hip-thrust", "db-rdl", "single-leg-rdl-db",
        "kb-deadlift", "kb-swing",
        # 9.2 Squat
        "bodyweight-squat", "box-squat-bench", "goblet-squat", "wall-sit", "reverse-lunge",
        "split-squat", "step-up-bench",
        # 9.3 Push
        "knee-push-up", "incline-push-up-bench", "push-up", "db-floor-press", "db-bench-press",
        "db-overhead-press", "pike-push-up", "db-lateral-raise",
        # 9.4 Pull
        "prone-ytw-raise", "bench-supported-db-row", "db-bent-row", "kb-row", "db-rear-delt-raise",
        "inverted-row", "dead-hang", "scap-pull-hang", "db-floor-pullover",
        # 9.5 Carry
        "bear-crawl", "suitcase-carry", "farmer-carry", "overhead-carry",
        # 9.6 Brace and anti-rotation
        "dead-bug", "bird-dog", "plank", "hollow-hold", "side-plank",
        "half-kneeling-db-antirotation-hold",
        # 9.7 Mobility
        "cat-cow", "open-book", "wall-angel", "scap-push-up", "hip-90-90-switch", "couch-stretch",
        # 9.8 Locomotion and play
        "animal-walk-crab", "line-balance-walk", "jumping-jacks", "skipping", "throw-and-catch",
    }
)  # fmt: skip

PRINCIPLES_WORKOUT_IDS: frozenset[str] = frozenset(
    {
        "posture-prelude-v1", "upper-a", "lower-a", "upper-b", "lower-full-b", "son-upper-a",
        "son-lower-a", "son-play-day", "together-full-body", "assessment-day",
        "mobility-carry-day",
    }
)  # fmt: skip

# Section 9 names three; section 10 as written schedules five more nowhere. All eight stay in the
# library as progression targets, section 3.6 fallbacks, section 7.7 gap rows and pool members
# (D-061). The library is never padded to make this set smaller.
NAMED_ORPHANS: frozenset[str] = frozenset({"knee-push-up", "kb-row", "single-leg-rdl-db"})
EXPECTED_ORPHANS: frozenset[str] = NAMED_ORPHANS | {
    "hip-hinge-bw",
    "box-squat-bench",
    "bird-dog",
    "skipping",
    "overhead-carry",
}

YOUTH_BANDS = (AgeBand.U10, AgeBand.AGE_10_13, AgeBand.AGE_14_17)
YOUTH_WORKOUTS = ("son-upper-a", "son-lower-a", "son-play-day", "together-full-body")
ADULT_WORKOUTS = ("upper-a", "lower-a", "lower-full-b", "mobility-carry-day")
BAND_AGE = {AgeBand.U10: 8, AgeBand.AGE_10_13: 11, AgeBand.AGE_14_17: 15}


def _profile(kind: str, band: AgeBand) -> Profile:
    return Profile(
        id="check",
        display_name="check",
        kind=kind,
        age_years=BAND_AGE.get(band),
        has_overhead_anchor=False,
    )


def _validate(library, workout_id: str, kind: str, band: AgeBand, compile_kind: str | None = None):
    """Compile for one profile kind, then judge the result against another.

    The two differ in the negative test: the adult session as the adult actually receives it is
    what must be illegal for a child. Compiling it *for* the child would substitute the illegal
    rows away first, which is the engine working, not the gate.
    """
    compiled_for = _profile(compile_kind or kind, band if (compile_kind or kind) == "youth" else AgeBand.ADULT)
    compiled = compile_workout(library.template(workout_id), 1, compiled_for, DEFAULT_SETTINGS, library)
    return validate_workout(
        compiled,
        profile_kind=kind,
        age_band=band,
        equipment=list(DEFAULT_SETTINGS.equipment),
        exercises=library.exercises,
        youth_rules=library.youth_rules,
    )


def test_every_seed_exercise_validates(library) -> None:
    """1. The library loads with 52 exercises and 11 workouts and zero errors."""
    assert len(library.exercises) == 52
    assert len(library.templates) == 11
    assert all(isinstance(item, Exercise) for item in library.exercises.values())
    assert all(isinstance(item, WorkoutTemplate) for item in library.templates.values())


def test_seed_ids_match_principles(library) -> None:
    """2. The id set equals the literal transcription of section 9, both ways."""
    assert set(library.exercises) == set(PRINCIPLES_EXERCISE_IDS)
    assert set(library.templates) == set(PRINCIPLES_WORKOUT_IDS)


def test_every_link_resolves(library) -> None:
    """3. Every declared link resolves, and the derived transpose is symmetric."""
    for exercise in library.exercises.values():
        for target in (exercise.regression_of, exercise.progression_of):
            assert target is None or target in library.exercises

    edges: set[tuple[str, str]] = set()
    for exercise in library.exercises.values():
        if exercise.regression_of is not None:
            edges.add((exercise.id, exercise.regression_of))
        if exercise.progression_of is not None:
            edges.add((exercise.progression_of, exercise.id))

    for easier, harder in edges:
        assert easier in library.easier_than[harder], f"{easier} should be easier than {harder}"
        assert harder in library.harder_than[easier], f"{harder} should be harder than {easier}"

    # Nothing is invented: every derived edge traces back to a declared one.
    derived = {(low, high) for high, lows in library.easier_than.items() for low in lows}
    assert derived == edges
    assert {(low, high) for low, highs in library.harder_than.items() for high in highs} == edges


def test_three_orphan_exercises_are_expected(library) -> None:
    """4. The orphan set is asserted, not incidental (D-061)."""
    scheduled = {row.exercise for template in library.templates.values() for row in template.all_rows()}
    scheduled |= {
        substitute
        for template in library.templates.values()
        for row in template.all_rows()
        for substitute in (row.u10_sub, *(c.u10_sub for c in (row.adult, row.youth) if c is not None))
        if substitute
    }
    scheduled |= {
        row.anchor_alt.exercise
        for template in library.templates.values()
        for row in template.all_rows()
        if row.anchor_alt
    }
    orphans = set(library.exercises) - scheduled
    assert orphans >= NAMED_ORPHANS, "principles section 9 names these three as deliberately unscheduled"
    assert orphans == set(EXPECTED_ORPHANS)


@pytest.mark.parametrize("workout_id", YOUTH_WORKOUTS)
@pytest.mark.parametrize("band", YOUTH_BANDS)
def test_youth_workouts_pass_every_band(library, workout_id: str, band: AgeBand) -> None:
    """5. Every youth-facing workout is legal at every band it can be served at."""
    result = _validate(library, workout_id, "youth", band)
    assert result.ok, [f"{e.code}: {e.message}" for e in result.errors if e.severity == "error"]


# The codes each adult session must raise at u10, named per workout rather than as a set any one
# of them could satisfy: `mobility-carry-day` fails on the `loaded_carry` tag, `upper-a` on the
# dumbbell load type, and both must fail on both counts they actually break.
U10_REQUIRED_CODES: dict[str, set[str]] = {
    "upper-a": {"youth_load_type_not_allowed", "youth_banned_tag", "youth_load_exceeded", "youth_set_count"},
    "lower-a": {"youth_load_type_not_allowed", "youth_load_exceeded", "youth_set_count"},
    "lower-full-b": {"youth_load_type_not_allowed", "youth_banned_tag", "youth_load_exceeded", "youth_rest_floor"},
    "mobility-carry-day": {"youth_load_type_not_allowed", "youth_banned_tag", "youth_session_length"},
}


@pytest.mark.parametrize("workout_id", ADULT_WORKOUTS)
def test_adult_workouts_fail_youth_validation(library, workout_id: str) -> None:
    """6. Negative: an adult workout is never legal for a nine-year-old, for named reasons."""
    result = _validate(library, workout_id, "youth", AgeBand.U10, compile_kind="adult")
    assert result.ok is False
    codes = {error.code for error in result.errors if error.severity == "error"}
    assert U10_REQUIRED_CODES[workout_id] <= codes, sorted(U10_REQUIRED_CODES[workout_id] - codes)


@pytest.mark.parametrize("workout_id", ADULT_WORKOUTS)
@pytest.mark.parametrize("band", YOUTH_BANDS)
def test_an_adult_session_is_illegal_at_every_youth_band(library, workout_id: str, band: AgeBand) -> None:
    """Not only at u10: the parent's session is never legal for the son at any age he is now."""
    result = _validate(library, workout_id, "youth", band, compile_kind="adult")
    assert result.ok is False, band.value


ALL_TEMPLATES = tuple(sorted(PRINCIPLES_WORKOUT_IDS))


@pytest.mark.parametrize("workout_id", ALL_TEMPLATES)
def test_every_template_validates_for_every_kind_and_band_it_serves(library, workout_id: str) -> None:
    """1 (the "zero errors" half): all eleven templates compiled and judged, not eight.

    Tests 5 and 6 between them reach eight of the eleven. ``posture-prelude-v1``, ``upper-b`` and
    ``assessment-day`` were only ever checked by the loader's own pass, so a regression in
    ``compile_workout`` would not have shown up here.
    """
    template = library.template(workout_id)
    checked = 0
    for kind in template.profile_kinds:
        bands = YOUTH_BANDS if kind == "youth" else (AgeBand.ADULT,)
        for band in bands:
            result = _validate(library, workout_id, kind, band)
            problems = [f"{kind}/{band.value} {e.code}: {e.message}" for e in result.errors if e.severity == "error"]
            assert result.ok, problems
            checked += 1
    assert checked >= 1


@pytest.mark.parametrize("category", ["BARBELL", "UNKNOWN"])
def test_unknown_garmin_category_rejected(library, category: str) -> None:
    """7. Negative: only the confirmed catalog names parse; there is no UNKNOWN member (D-018)."""
    doc = library.exercise("push-up").model_dump(mode="json")
    doc["garmin_category"] = category
    with pytest.raises(ValueError):
        Exercise.model_validate(doc)


def test_template_key_rejected_on_concrete_model(library) -> None:
    """7b. Negative: template keys do not exist on PRP-00's concrete row model."""
    template_row = library.template("upper-a").rows[0]
    raw = template_row.model_dump(mode="json")
    with pytest.raises(ValueError):
        WorkoutRow.model_validate(raw)
    for key in ("role", "per_side", "load_rule", "u10_sub"):
        with pytest.raises(ValueError):
            WorkoutRow.model_validate(
                {
                    "exercise_id": "push-up",
                    "sets": 3,
                    "reps": 8,
                    "load_unit": "bodyweight",
                    "rest_s": 60,
                    key: raw.get(key) or True,
                }
            )


def test_seed_lists_all_three_youth_bands(library) -> None:
    """7c. V14 fails closed on a missing band, so no seed exercise may leave one out."""
    for exercise in library.exercises.values():
        missing = [band.value for band in YOUTH_BANDS if band not in exercise.youth_ok_by_band]
        assert not missing, f"{exercise.id} omits {missing}"


def test_conditional_marks_the_two_kettlebells(library) -> None:
    """7d. `conditional` is exactly the three kettlebell movements, and only at 14-17."""
    conditional = {
        exercise.id
        for exercise in library.exercises.values()
        if YouthAllow.CONDITIONAL in exercise.youth_ok_by_band.values()
    }
    assert conditional == {"kb-deadlift", "kb-swing", "kb-row"}
    for identifier in sorted(conditional):
        allow = library.exercise(identifier).youth_ok_by_band
        assert allow[AgeBand.AGE_14_17] is YouthAllow.CONDITIONAL
        assert allow[AgeBand.U10] is YouthAllow.NO
        assert allow[AgeBand.AGE_10_13] is YouthAllow.NO


def _copy_library(tmp_path: Path) -> Path:
    destination = tmp_path / "library"
    shutil.copytree(Path("library"), destination)
    return destination


def test_missing_link_target_aborts_load(tmp_path: Path) -> None:
    """8. Negative: one dangling link aborts the whole load and seeds nothing."""
    root = _copy_library(tmp_path)
    path = root / "exercises" / "hinge.yaml"
    path.write_text(path.read_text().replace("regression_of: db-rdl", "regression_of: no-such-exercise"))
    with pytest.raises(LibraryError) as caught:
        load_library(root)
    assert any("no-such-exercise" in problem for problem in caught.value.problems)


def test_broken_document_aborts_load(tmp_path: Path) -> None:
    """A schema error in one document is reported with its file and id, and nothing loads."""
    root = _copy_library(tmp_path)
    path = root / "exercises" / "squat.yaml"
    path.write_text(path.read_text().replace("load_type: dumbbell", "load_type: barbell"))
    with pytest.raises(LibraryError) as caught:
        load_library(root)
    assert any("goblet-squat" in problem for problem in caught.value.problems)


def test_missing_library_directory_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(LibraryError):
        load_library(tmp_path / "nowhere")


def test_workout_id_must_match_filename(tmp_path: Path) -> None:
    root = _copy_library(tmp_path)
    path = root / "workouts" / "upper-a.yaml"
    path.write_text(path.read_text().replace("id: upper-a", "id: upper-a-renamed"))
    with pytest.raises(LibraryError) as caught:
        load_library(root)
    assert any("filename stem" in problem for problem in caught.value.problems)


def test_assessment_library_covers_the_six_tests(library) -> None:
    """The four blocks of section 7 are present and complete."""
    assessments = library.assessments
    assert len(assessments.tests) == 6
    assert assessments.youth_prescription(assessments.tests[0].id, AgeBand.U10) is not None
    wall_angel = next(spec for spec in assessments.tests if spec.id.value == "wall_angel_reach")
    assert wall_angel.self_rated is True
    # A self-rated score is never handed to a youth row as a rep count (D-062).
    assert assessments.youth_prescription(wall_angel.id, AgeBand.U10) is None
