"""``make seed`` as a command: one line out, non-zero and loud on a bad library.

PRP-00's version of this file asserted that the stub exited 0 with "no library yet". PRP-01
replaces the stub, and the contract inverts: a missing or broken library is now a failure, because
a seed step that cannot seed must not report success.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from sqlmodel import Session, select

from cadence.bibliotheque import seed as seed_module
from cadence.bibliotheque.loader import LibraryError
from cadence.db import ExerciseRecord, WorkoutRecord, init_db
from cadence.profils.tables import Profile, Setting
from cadence.programme.tables import PlannedSession, Program


@pytest.fixture
def seeded_env(settings, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(seed_module, "init_db", lambda: init_db(settings))
    return settings


def test_seed_main_reports_one_line(seeded_env, capsys) -> None:
    assert seed_module.main([]) == 0
    out = capsys.readouterr().out
    assert out.count("\n") == 1
    assert "52 exercises" in out
    assert "11 workouts" in out
    assert "2 profiles (me, son)" in out
    assert "planned sessions: me 16, son 16" in out


def test_running_make_seed_twice_changes_nothing(seeded_env, settings, capsys) -> None:
    """(g) ``make seed`` twice, through the command, not through one long-lived Session.

    Two calls to ``seed()`` inside one open Session share an identity map, so ``session.get``
    answers from memory and a duplicate insert would not necessarily show. ``main`` opens its own
    Session per run, which is what the Makefile target actually does, and the census below is
    taken from a third, fresh one.
    """
    assert seed_module.main([]) == 0
    assert seed_module.main([]) == 0
    capsys.readouterr()

    engine = init_db(settings)
    with Session(engine) as fresh:
        planned = fresh.exec(select(PlannedSession)).all()
        assert len(planned) == 32
        assert len({row.id for row in planned}) == 32
        assert len(fresh.exec(select(ExerciseRecord)).all()) == 52
        assert len(fresh.exec(select(WorkoutRecord)).all()) == 11
        assert len(fresh.exec(select(Profile)).all()) == 2
        assert len(fresh.exec(select(Program)).all()) == 2
        assert len(fresh.exec(select(Setting)).all()) == 8


def test_seed_main_accepts_reset(seeded_env, capsys) -> None:
    assert seed_module.main([]) == 0
    capsys.readouterr()
    assert seed_module.main(["--reset"]) == 0
    assert "planned sessions: me 16, son 16" in capsys.readouterr().out


def test_seed_main_fails_on_a_missing_library(seeded_env, monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setattr(seed_module, "LIBRARY_DIR", tmp_path / "nowhere")
    assert seed_module.main([]) == 1
    assert "library could not be loaded" in capsys.readouterr().err


def test_seed_main_fails_on_an_off_schema_document(seeded_env, monkeypatch, tmp_path: Path, capsys) -> None:
    root = tmp_path / "library"
    (root / "exercises").mkdir(parents=True)
    (root / "exercises" / "push-up.yaml").write_text("id: push-up\n")
    monkeypatch.setattr(seed_module, "LIBRARY_DIR", root)
    assert seed_module.main([]) == 1
    assert "kind must be 'exercises'" in capsys.readouterr().err


def test_seed_writes_nothing_when_the_library_is_broken(settings, tmp_path: Path) -> None:
    """The load is all-or-nothing: a broken library never leaves half a plan behind."""
    engine = init_db(settings)
    with Session(engine) as session:
        with pytest.raises(LibraryError):
            seed_module.seed(session, tmp_path / "nowhere")
        assert session.exec(select(PlannedSession)).all() == []


def test_a_dangling_link_seeds_nothing(settings, tmp_path: Path) -> None:
    """8 (second half): the load aborts *and* the database is untouched, not merely the load."""
    root = tmp_path / "library"
    shutil.copytree(Path("library"), root)
    path = root / "exercises" / "hinge.yaml"
    path.write_text(path.read_text().replace("regression_of: db-rdl", "regression_of: no-such-exercise"))

    engine = init_db(settings)
    with Session(engine) as session:
        with pytest.raises(LibraryError):
            seed_module.seed(session, root)
        assert session.exec(select(ExerciseRecord)).all() == []
        assert session.exec(select(PlannedSession)).all() == []
        assert session.exec(select(Profile)).all() == []


def test_seeding_twice_changes_no_row_count_and_duplicates_nothing(settings) -> None:
    """(g) Idempotence measured on every table seed writes to, not only on planned sessions."""
    engine = init_db(settings)
    with Session(engine) as session:

        def census() -> dict[str, list[str]]:
            return {
                "exercises": sorted(row.id for row in session.exec(select(ExerciseRecord)).all()),
                "workouts": sorted(row.id for row in session.exec(select(WorkoutRecord)).all()),
                "profiles": sorted(row.id for row in session.exec(select(Profile)).all()),
                "programs": sorted(row.id for row in session.exec(select(Program)).all()),
                "sessions": sorted(row.id for row in session.exec(select(PlannedSession)).all()),
                "settings": sorted(row.key for row in session.exec(select(Setting)).all()),
            }

        seed_module.seed(session, Path("library"))
        first = census()
        rows_before = {row.id: row.rows_json for row in session.exec(select(PlannedSession)).all()}

        seed_module.seed(session, Path("library"))
        second = census()

        assert first == second
        for table, ids in second.items():
            assert len(ids) == len(set(ids)), f"{table} has duplicate ids"
        after = {row.id: row.rows_json for row in session.exec(select(PlannedSession)).all()}
        assert after == rows_before, "a re-seed rewrote a planned session's rows"


def test_a_done_planned_session_survives_a_re_seed(settings) -> None:
    """(g) The son finishing Monday must not have Monday rebuilt underneath him.

    ``seed`` refreshes a planned session only while its status is still ``planned``; a session the
    household has already worked through keeps the rows it was actually done against.
    """
    engine = init_db(settings)
    with Session(engine) as session:
        seed_module.seed(session, Path("library"))
        done = session.exec(select(PlannedSession).where(PlannedSession.profile_id == "son")).all()[0]
        done.status = "done"
        done.rows_json = '[{"position": 1, "exercise_id": "bear-crawl", "note": "as performed"}]'
        session.commit()
        identifier = done.id

        seed_module.seed(session, Path("library"))
        kept = session.get(PlannedSession, identifier)
        assert kept is not None
        assert kept.status == "done"
        assert kept.rows_json == '[{"position": 1, "exercise_id": "bear-crawl", "note": "as performed"}]'
        assert len(session.exec(select(PlannedSession)).all()) == 32
