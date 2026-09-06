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
