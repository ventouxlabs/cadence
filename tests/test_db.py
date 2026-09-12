"""The SQLite layer - acceptance tests 7-9."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlmodel import Session, select

from cadence.config import Settings
from cadence.db import EXPECTED_SCHEMA_VERSION, SchemaVersion, get_engine, init_db, update_model


def test_init_db_is_idempotent(settings: Settings) -> None:
    init_db(settings)
    init_db(settings)
    with Session(get_engine(settings)) as session:
        rows = session.exec(select(SchemaVersion)).all()
    assert len(rows) == 1
    assert rows[0].version == EXPECTED_SCHEMA_VERSION


def test_wal_mode_enabled(settings: Settings) -> None:
    init_db(settings)
    with Session(get_engine(settings)) as session:
        mode = session.execute(text("PRAGMA journal_mode")).scalar_one()
    assert str(mode).lower() == "wal"


def test_future_schema_version_raises(settings: Settings) -> None:
    init_db(settings)
    with Session(get_engine(settings)) as session:
        session.execute(text("UPDATE schema_version SET version = 99 WHERE id = 1"))
        session.commit()

    with pytest.raises(RuntimeError) as excinfo:
        init_db(settings)
    message = str(excinfo.value)
    assert "99" in message
    assert str(EXPECTED_SCHEMA_VERSION) in message


def test_update_model_returns_a_copy(settings: Settings) -> None:
    original = SchemaVersion(id=1, version=1, applied_at="2026-09-06T00:00:00+00:00")
    updated = update_model(original, version=2)
    assert original.version == 1
    assert updated.version == 2
    assert updated is not original


def test_update_model_rejects_unknown_field(settings: Settings) -> None:
    original = SchemaVersion(id=1, version=1, applied_at="2026-09-06T00:00:00+00:00")
    with pytest.raises(ValueError, match="no field"):
        update_model(original, nope=1)


# ------------------------------------------------- D-283: columns added after a table shipped

#: The deployed database's `challenge` table, as it stood before `met_on` (D-282 read it off
#: VM-201). `create_all` will not add a column to a table that already exists, so this is what
#: an ORM read has to survive.
_PRE_MET_ON = "ALTER TABLE challenge DROP COLUMN met_on"


def _challenge_columns(settings: Settings) -> list[str]:
    with Session(get_engine(settings)) as session:
        return [str(row[1]) for row in session.execute(text("PRAGMA table_info(challenge)")).all()]


def test_a_column_added_later_reaches_an_existing_database(settings: Settings) -> None:
    """D-283. `create_all` is not a migration, and `Challenge` is ORM-mapped.

    Codex caught this: `has_column` guarded the badge's raw SQL, but `select(Challenge)` emits
    every mapped field, so `active_for` — which `save_battery` calls on every assessment — died
    with `no such column: challenge.met_on` on the deployed database. The guard protected the
    one path that did not need protecting.
    """
    init_db(settings)
    with Session(get_engine(settings)) as session:
        session.execute(text(_PRE_MET_ON))
        session.commit()
    assert "met_on" not in _challenge_columns(settings), "the fixture did not reproduce the old shape"

    init_db(settings)
    assert "met_on" in _challenge_columns(settings), "an existing database never got the column"


def test_the_orm_can_read_challenge_after_the_migration(settings: Settings) -> None:
    """The failure as a user meets it: opening or saving an assessment, not reading a badge."""
    from cadence.bilan import challenges as chal
    from cadence.bilan.tables import ACTIVE, Challenge

    init_db(settings)
    with Session(get_engine(settings)) as session:
        session.add(
            Challenge(
                id="c1",
                profile_id="me",
                name="Dead hang 60 s",
                test_id="dead_hang_s",
                target_value=60.0,
                unit="s",
                baseline_on="2026-03-01",
                due_on="2026-04-01",
                status=ACTIVE,
            )
        )
        session.commit()
        session.execute(text(_PRE_MET_ON))
        session.commit()

    init_db(settings)
    with Session(get_engine(settings)) as session:
        rows = chal.active_for(session, "me")
        assert [row.id for row in rows] == ["c1"], "the row was lost by the migration"
        assert rows[0].met_on is None, "an existing row was given a met-on day it never had"
        assert chal.all_for(session, "me"), "all_for still cannot read the table"


def test_the_migration_keeps_the_data_it_migrates(settings: Settings) -> None:
    """`ADD COLUMN` is additive. A migration that dropped a row would be worse than the bug."""
    from cadence.bilan.tables import ACTIVE, Challenge

    init_db(settings)
    with Session(get_engine(settings)) as session:
        session.add(
            Challenge(
                id="c2",
                profile_id="son",
                name="Plank 45 s",
                test_id="plank_s",
                target_value=45.0,
                unit="s",
                baseline_on="2026-02-01",
                due_on="2026-03-01",
                status=ACTIVE,
                row_json='{"frequency": 2}',
            )
        )
        session.commit()
        session.execute(text(_PRE_MET_ON))
        session.commit()

    init_db(settings)
    with Session(get_engine(settings)) as session:
        row = session.get(Challenge, "c2")
        assert row is not None
        assert (row.profile_id, row.name, row.target_value) == ("son", "Plank 45 s", 45.0)
        assert row.row_json == '{"frequency": 2}', "an unrelated column was rewritten"


def test_the_migration_is_idempotent(settings: Settings) -> None:
    """Every boot runs it. A second pass must not raise `duplicate column name`."""
    init_db(settings)
    init_db(settings)
    init_db(settings)
    assert _challenge_columns(settings).count("met_on") == 1


def test_an_older_database_is_moved_to_the_current_schema_version(settings: Settings) -> None:
    """The version row is bookkeeping, and stale bookkeeping is worse than none.

    `_ensure_schema_version` only ever inserted or refused; an existing row was left at
    whatever it said, so a migrated database would have reported version 1 forever.
    """
    init_db(settings)
    with Session(get_engine(settings)) as session:
        session.execute(text("UPDATE schema_version SET version = 1 WHERE id = 1"))
        session.commit()

    init_db(settings)
    with Session(get_engine(settings)) as session:
        row = session.exec(select(SchemaVersion)).one()
    assert row.version == EXPECTED_SCHEMA_VERSION
