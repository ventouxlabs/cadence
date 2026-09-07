"""SQLite engine, session factory and schema versioning.

Backup note: WAL leaves ``cadence.db-wal`` and ``cadence.db-shm`` beside the database. Copying
only the ``.db`` file yields a torn backup; PRP-09 uses ``sqlite3 .backup`` instead of ``cp``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, TypeVar

from fastapi import Depends
from sqlalchemy import Engine, event
from sqlmodel import Field, Session, SQLModel, create_engine, select

from cadence.config import Settings, get_settings

# Imported for their side effect: a table class has to be imported before
# ``SQLModel.metadata.create_all`` can see it. PRP-01 added four, PRP-02 two more, PRP-04 one, PRP-06 one.
from cadence.profils.tables import Profile as Profile  # noqa: F401
from cadence.profils.tables import Setting as Setting  # noqa: F401
from cadence.programme.tables import PlannedSession as PlannedSession  # noqa: F401
from cadence.programme.tables import Program as Program  # noqa: F401
from cadence.seance.tables import SessionRecord as SessionRecord  # noqa: F401
from cadence.seance.tables import SessionRowRecord as SessionRowRecord  # noqa: F401
from cadence.vitalforge.tables import MetricsCache as MetricsCache  # noqa: F401
from cadence.vitalforge.tables import SyncJob as SyncJob  # noqa: F401

EXPECTED_SCHEMA_VERSION = 1

ModelT = TypeVar("ModelT", bound=SQLModel)


class SchemaVersion(SQLModel, table=True):
    __tablename__ = "schema_version"

    id: int = Field(default=1, primary_key=True)
    version: int
    applied_at: str


class ExerciseRecord(SQLModel, table=True):
    __tablename__ = "exercise"

    id: str = Field(primary_key=True)
    doc_json: str
    source: str
    created_at: str


class WorkoutRecord(SQLModel, table=True):
    __tablename__ = "workout"

    id: str = Field(primary_key=True)
    doc_json: str
    source: str
    target_profile_kind: str
    created_at: str


@event.listens_for(Engine, "connect")
def _set_sqlite_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
    """Applied on every new connection, not once at startup - pooled connections are recycled."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()


_engines: dict[str, Engine] = {}


def get_engine(settings: Settings | None = None) -> Engine:
    """One engine per database path, reused across calls."""
    resolved = (settings or get_settings()).db_path
    key = str(Path(resolved).resolve())
    engine = _engines.get(key)
    if engine is None:
        engine = create_engine(f"sqlite:///{key}", connect_args={"check_same_thread": False})
        _engines[key] = engine
    return engine


def init_db(settings: Settings | None = None) -> Engine:
    """Create the database file, the tables, and the schema-version row. Idempotent."""
    active = settings or get_settings()
    Path(active.db_path).resolve().parent.mkdir(parents=True, exist_ok=True)
    engine = get_engine(active)
    SQLModel.metadata.create_all(engine)
    _ensure_schema_version(engine)
    return engine


def _ensure_schema_version(engine: Engine) -> None:
    """Insert the version row if absent; refuse to run against a newer database."""
    with Session(engine) as session:
        row = session.exec(select(SchemaVersion).where(SchemaVersion.id == 1)).first()
        if row is None:
            session.add(
                SchemaVersion(
                    id=1,
                    version=EXPECTED_SCHEMA_VERSION,
                    applied_at=datetime.now(UTC).isoformat(),
                )
            )
            session.commit()
            return
        if row.version > EXPECTED_SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema version {row.version} is newer than this build expects "
                f"({EXPECTED_SCHEMA_VERSION}); refusing to start rather than corrupt it"
            )


def get_session(settings: Annotated[Settings, Depends(get_settings)]) -> Iterator[Session]:
    """FastAPI dependency yielding a database session."""
    with Session(get_engine(settings)) as session:
        yield session


def update_model(obj: ModelT, **fields: Any) -> ModelT:
    """Return a copy of ``obj`` with ``fields`` replaced.

    The only sanctioned way to change a loaded row: services build new objects rather than
    mutating attributes in place (``docs/architecture.md`` section 3).
    """
    unknown = set(fields) - set(type(obj).model_fields)
    if unknown:
        raise ValueError(f"{type(obj).__name__} has no field(s): {', '.join(sorted(unknown))}")
    return obj.model_copy(update=fields)


def reset_engines() -> None:
    """Dispose every cached engine. Test-support only."""
    for engine in _engines.values():
        engine.dispose()
    _engines.clear()
