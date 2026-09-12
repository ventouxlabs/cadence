"""Every column this build declares has to be reachable on the database that is deployed.

D-283 shipped a column onto an ORM-mapped table and nothing noticed until `/codex review` read
the diff. The reason nothing noticed is structural, and it is worth stating plainly: **every
other test in this suite builds its database with `create_all`**, so the schema under test is
always this build's schema. A test written that way cannot observe the only thing that matters
here, which is what happens when this build opens a database it did not create.

`tests/fixtures/deployed_schema.json` is the missing half — the real schema of the real
database, read off VM-201 (`/app/data/cadence.db`) on 2026-09-12. It is data, not a model dump,
and it is deliberately not regenerated from `SQLModel.metadata`: a baseline generated from the
same source it is checked against would agree with itself forever and catch nothing.

The invariant: a column in the models but not in that baseline is a column the deployed database
lacks, so it must be in `cadence.db._ADDED_COLUMNS` or the first query naming it raises
`no such column` (D-283). New *tables* need no entry — `create_all` makes those.

Refresh the fixture only after the migration that adds the column has actually run against
VM-201, and say so in the commit. Refreshing it to silence this test is how the check dies.
"""

from __future__ import annotations

import json
from pathlib import Path

from sqlmodel import SQLModel

import cadence.db  # noqa: F401  - imported for the side effect of registering every table
from cadence.db import _ADDED_COLUMNS

FIXTURE = Path(__file__).parent / "fixtures" / "deployed_schema.json"
DEPLOYED: dict[str, list[str]] = json.loads(FIXTURE.read_text())


def _declared() -> dict[str, set[str]]:
    return {table.name: {column.name for column in table.columns} for table in SQLModel.metadata.sorted_tables}


def _migrated() -> dict[str, set[str]]:
    """What `_add_missing_columns` will add, as ``{table: {column}}``."""
    out: dict[str, set[str]] = {}
    for table, column, _ddl in _ADDED_COLUMNS:
        out.setdefault(table, set()).add(column)
    return out


def test_every_new_column_on_an_existing_table_has_a_migration() -> None:
    """The D-283 invariant, checkable without a network or a production database.

    Fails the moment somebody adds a field to a table the deployed database already has and
    does not add the matching `_ADDED_COLUMNS` entry — which is exactly the mistake that
    shipped, and exactly the one CI could not see.
    """
    migrated = _migrated()
    unmigrated: list[str] = []
    for table, columns in sorted(_declared().items()):
        if table not in DEPLOYED:
            continue  # A table the deployed database lacks entirely; `create_all` makes it.
        added = columns - set(DEPLOYED[table]) - migrated.get(table, set())
        unmigrated += [f"{table}.{column}" for column in sorted(added)]

    assert not unmigrated, (
        "these columns are declared on a table the deployed database already has, and no "
        f"migration adds them: {', '.join(unmigrated)}. Every read of that table will raise "
        "`no such column` against the live database (D-283). Add each to `_ADDED_COLUMNS` in "
        "cadence/db.py, or drop the column."
    )


def test_the_met_on_migration_is_the_one_this_check_was_built_for() -> None:
    """Anchors the fixture to the bug, so a refresh that erases the case is visible.

    If `met_on` ever appears in the fixture, the migration has run on VM-201 and the entry in
    `_ADDED_COLUMNS` becomes a no-op that still has to stay, for any database older still.
    """
    assert "challenge" in DEPLOYED, "the fixture lost the table the bug was in"
    if "met_on" in DEPLOYED["challenge"]:  # pragma: no cover - true only after a refresh
        return
    assert ("challenge", "met_on", "VARCHAR") in _ADDED_COLUMNS, (
        "the deployed database has no challenge.met_on and nothing migrates it - this is D-283 reopening"
    )


def test_no_migration_names_a_column_the_models_do_not_declare() -> None:
    """The other direction: a stale entry adds a column nothing reads, forever, on every boot."""
    declared = _declared()
    stale = [f"{table}.{column}" for table, column, _ddl in _ADDED_COLUMNS if column not in declared.get(table, set())]
    assert not stale, f"_ADDED_COLUMNS migrates columns no model declares: {', '.join(stale)}"


def test_no_migration_names_a_table_that_does_not_exist() -> None:
    """A typo in a table name is silent at runtime — `_add_missing_columns` logs and moves on."""
    declared = _declared()
    unknown = sorted({table for table, _c, _d in _ADDED_COLUMNS if table not in declared})
    assert not unknown, f"_ADDED_COLUMNS names tables this build does not declare: {unknown}"


def test_the_fixture_is_the_deployed_schema_and_not_a_model_dump() -> None:
    """Guards the fixture's whole reason to exist.

    A baseline regenerated from `SQLModel.metadata` agrees with the models by construction and
    the drift check becomes decorative. The live database differing by at least the one known
    migration is the cheap proof it still comes from somewhere else.
    """
    declared = _declared()
    differences = [
        table for table, columns in DEPLOYED.items() if table in declared and set(columns) != declared[table]
    ]
    assert differences or not _ADDED_COLUMNS, (
        "the fixture matches the models exactly while migrations are still pending — it looks "
        "regenerated from SQLModel.metadata rather than read off the deployed database"
    )
