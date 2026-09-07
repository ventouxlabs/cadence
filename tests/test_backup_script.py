"""Acceptance tests 12-16: ``scripts/backup.sh``.

The point of every one of these is that a backup which loses data, or a failure which eats the
history, is invisible until a restore - which is the worst moment to find out.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "backup.sh"

pytestmark = pytest.mark.skipif(shutil.which("sqlite3") is None, reason="sqlite3 CLI not installed")


def _run(
    db: Path, *, keep: str | None = None, path: str | None = None, backup_dir: str | None = None
) -> subprocess.CompletedProcess[str]:
    env = {"PATH": path or "/usr/bin:/bin:/usr/local/bin", "CADENCE_DB_PATH": str(db)}
    if keep is not None:
        env["CADENCE_BACKUP_KEEP"] = keep
    if backup_dir is not None:
        env["CADENCE_BACKUP_DIR"] = backup_dir
    return subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, check=False, cwd=str(REPO))


def _backups(db: Path) -> list[Path]:
    return sorted((db.parent / "backups").glob("cadence-*.db"))


def test_backup_creates_timestamped_copy(tmp_path: Path) -> None:
    """12. The test that proves ``cp`` would have been wrong.

    The connection is deliberately held open across the subprocess call. In WAL mode the row
    lives in ``cadence.db-wal`` and not in the main file until the writer checkpoints, so a
    ``cp`` of the ``.db`` here would produce a backup with no row in it.
    """
    db = tmp_path / "cadence.db"
    connection = sqlite3.connect(db)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE session (id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO session VALUES ('only-in-the-wal')")
        connection.commit()
        assert (tmp_path / "cadence.db-wal").exists(), "the fixture is not exercising WAL"

        result = _run(db)
        assert result.returncode == 0, result.stderr

        copies = _backups(db)
        assert len(copies) == 1
        assert copies[0].name.startswith("cadence-")
        # cadence-YYYYmmdd-HHMM.db
        assert len(copies[0].stem) == len("cadence-20260907-0317")

        with sqlite3.connect(copies[0]) as snapshot:
            rows = snapshot.execute("SELECT id FROM session").fetchall()
        assert rows == [("only-in-the-wal",)], "the backup lost a committed row still in the WAL"
    finally:
        connection.close()


def test_backup_passes_integrity_check(tmp_path: Path) -> None:
    """13."""
    db = tmp_path / "cadence.db"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE t (id INTEGER)")
    assert _run(db).returncode == 0

    with sqlite3.connect(_backups(db)[0]) as snapshot:
        assert snapshot.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_backup_keeps_only_30(tmp_path: Path) -> None:
    """14. Thirty-five snapshots in, exactly the thirty newest survive."""
    db = tmp_path / "cadence.db"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE t (id INTEGER)")
    out_dir = tmp_path / "backups"
    out_dir.mkdir()
    for minute in range(35):
        stale = out_dir / f"cadence-20260101-{minute:04d}.db"
        stale.write_text("")
        # Oldest first by mtime, so "newest 30" and "highest numbered 30" are the same set.
        import os

        os.utime(stale, (1_700_000_000 + minute, 1_700_000_000 + minute))

    assert _run(db).returncode == 0

    survivors = sorted(path.name for path in out_dir.glob("cadence-*.db"))
    assert len(survivors) == 30
    assert "cadence-20260101-0000.db" not in survivors, "the oldest snapshot was kept"
    assert "cadence-20260101-0034.db" in survivors, "a recent snapshot was pruned"


def test_backup_prune_does_not_run_if_backup_failed(tmp_path: Path) -> None:
    """15. A failed run must never touch the history it could not add to."""
    db = tmp_path / "cadence.db"
    out_dir = tmp_path / "backups"
    out_dir.mkdir()
    existing = [out_dir / f"cadence-20260101-{minute:04d}.db" for minute in range(35)]
    for path in existing:
        path.write_text("keep me")

    result = _run(db)  # the database does not exist

    assert result.returncode != 0
    assert all(path.exists() for path in existing), "a failed backup pruned the good ones"


def test_backup_handles_empty_backup_dir(tmp_path: Path) -> None:
    """16. The first-ever run has nothing to prune and must not fail on it."""
    db = tmp_path / "cadence.db"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE t (id INTEGER)")
    assert not (tmp_path / "backups").exists()

    result = _run(db)

    assert result.returncode == 0, result.stderr
    assert len(_backups(db)) == 1


def test_backup_rejects_a_nonsense_keep(tmp_path: Path) -> None:
    """Not in the PRP's list. ``KEEP=0`` would make ``tail -n +1`` delete every snapshot,
    including the one just written, which is a silent way to have no backups at all."""
    db = tmp_path / "cadence.db"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE t (id INTEGER)")
    assert _run(db, keep="0").returncode != 0
    assert _run(db, keep="thirty").returncode != 0


def test_backup_works_without_the_sqlite3_cli(tmp_path: Path) -> None:
    """Not in the PRP's list, and the reason the script has two engines at all.

    The image ships no sqlite3 binary, so ``docker compose exec cadence ./scripts/backup.sh`` -
    the way the cron line actually runs - would be impossible with the CLI alone. This hides
    the CLI from PATH and asserts the Python engine produces the same verified snapshot.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("bash", "date", "ls", "tail", "xargs", "rm", "grep", "mkdir", "dirname", "chmod", "python3"):
        found = shutil.which(tool)
        if found:
            (bin_dir / tool).symlink_to(found)

    db = tmp_path / "cadence.db"
    connection = sqlite3.connect(db)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE session (id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO session VALUES ('only-in-the-wal')")
        connection.commit()

        result = _run(db, path=str(bin_dir))
        assert result.returncode == 0, result.stderr
        assert "via python" in result.stdout, result.stdout
    finally:
        connection.close()

    with sqlite3.connect(_backups(db)[0]) as snapshot:
        assert snapshot.execute("SELECT id FROM session").fetchall() == [("only-in-the-wal",)]


# --------------------------------------------------------------------- the restore round trip
#
# `docs/deploy.md` §6 is the only place the restore procedure exists - there is no restore.sh -
# so the runbook itself is the artefact under test. These lift the shell out of the document
# and run it, because a restore that is only ever read is a restore nobody has run.

RESTORE_DOC = (REPO / "docs" / "deploy.md").read_text()


def _documented_restore_commands() -> list[str]:
    """The `mv` and `cp` lines from the runbook's Restore block, verbatim.

    Restated commands would pass forever after someone edited the runbook, which is the drift
    this is here to catch. The `docker compose` and `sudo chown` lines are dropped: neither is
    available in a test, and neither is what can silently lose data.
    """
    block = RESTORE_DOC[RESTORE_DOC.index("### Restore") :]
    block = block[block.index("```bash") + len("```bash") : block.index("```", block.index("```bash") + 7)]
    return [line.strip() for line in block.splitlines() if line.strip().startswith(("mv ", "cp ", "mkdir "))]


def _seed(db: Path, exercises: int, workouts: int) -> None:
    """A database shaped like Cadence's, left with its newest rows still in the WAL."""
    connection = sqlite3.connect(db)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE IF NOT EXISTS exercise (id INTEGER PRIMARY KEY)")
    connection.execute("CREATE TABLE IF NOT EXISTS workout (id INTEGER PRIMARY KEY)")
    connection.executemany("INSERT INTO exercise VALUES (?)", [(n,) for n in range(exercises)])
    connection.executemany("INSERT INTO workout VALUES (?)", [(n,) for n in range(workouts)])
    connection.commit()
    return connection


def _counts(db: Path) -> tuple[int, int]:
    with sqlite3.connect(db) as connection:
        return (
            connection.execute("SELECT count(*) FROM exercise").fetchone()[0],
            connection.execute("SELECT count(*) FROM workout").fetchone()[0],
        )


def test_documented_restore_recovers_the_snapshot_counts(tmp_path: Path) -> None:
    """The runbook's own commands, executed: a snapshot restores to the counts it captured.

    The manual checklist in `docs/deploy.md` §12 has JD tick that a backup file exists. A file
    existing is not a restore, and the first time anyone finds out is an outage.
    """
    root = tmp_path / "cadence"
    (root / "data").mkdir(parents=True)
    db = root / "data" / "cadence.db"

    connection = _seed(db, exercises=42, workouts=7)
    try:
        assert (root / "data" / "cadence.db-wal").exists(), "the fixture is not exercising WAL"
        assert _run(db).returncode == 0
        snapshot = _backups(db)[0]

        # The live database moves on, then goes bad. Only the snapshot has the 42/7 shape.
        connection.execute("INSERT INTO exercise VALUES (999)")
        connection.commit()
    finally:
        connection.close()

    commands = _documented_restore_commands()
    assert commands, "no mv/cp lines found in the runbook's Restore block"
    script = "\n".join(commands).replace("data/backups/cadence-20260907-0317.db", f"data/backups/{snapshot.name}")
    result = subprocess.run(["bash", "-c", script], cwd=str(root), capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr

    assert _counts(db) == (42, 7), "the restored database does not match the snapshot"


def test_documented_restore_leaves_no_stale_wal_beside_the_restored_db(tmp_path: Path) -> None:
    """PRP-09 risk 5, the step everyone forgets.

    SQLite replays a `-wal` it finds next to a database on the next open. A restore that copies
    the snapshot over `cadence.db` while the old `cadence.db-wal` is still there silently undoes
    itself - and it undoes itself *later*, on some subsequent open, not while anyone is watching.

    `test_deploy_doc_documents_restore_removes_wal` asserts the words `-wal` and `-shm` appear.
    This asserts the commands carrying them actually clear the files.
    """
    root = tmp_path / "cadence"
    (root / "data").mkdir(parents=True)
    db = root / "data" / "cadence.db"

    connection = _seed(db, exercises=3, workouts=1)
    try:
        assert _run(db).returncode == 0
        snapshot = _backups(db)[0]
    finally:
        connection.close()

    # A WAL and its index, left behind by an unclean shutdown - the shape a restore meets.
    (root / "data" / "cadence.db-wal").write_bytes(b"\x00" * 32)
    (root / "data" / "cadence.db-shm").write_bytes(b"\x00" * 32)

    script = "\n".join(_documented_restore_commands()).replace(
        "data/backups/cadence-20260907-0317.db", f"data/backups/{snapshot.name}"
    )
    subprocess.run(["bash", "-c", script], cwd=str(root), capture_output=True, text=True, check=False)

    assert not (root / "data" / "cadence.db-wal").exists(), "a stale WAL survived the documented restore"
    assert not (root / "data" / "cadence.db-shm").exists(), "a stale -shm survived the documented restore"
    assert _counts(db) == (3, 1)


def test_backup_refuses_a_path_that_would_escape_the_backup_command(tmp_path: Path) -> None:
    """A quote in the path redirects the snapshot out of the directory that protects it.

    ``.backup '$OUT'`` is split into words like a shell line, so ``a' 'b`` ends the argument
    early and ``.backup`` reads the remainder as its optional ``?DB?`` — the snapshot lands at
    ``b``, outside OUT_DIR and outside its ``umask 077``/``chmod 700``, while the integrity check
    and the prune still inspect the path the script meant (D-209).
    """
    db = tmp_path / "cadence.db"
    sqlite3.connect(db).close()
    escaped = tmp_path / "elsewhere.db"

    result = _run(db, backup_dir=f"{tmp_path}/out' '{escaped}")
    assert result.returncode == 2, result.stdout + result.stderr
    assert "refusing path" in result.stderr, result.stderr
    assert not escaped.exists(), "the snapshot escaped OUT_DIR"


def test_backup_refuses_a_path_that_reads_as_an_option(tmp_path: Path) -> None:
    """A leading dash is an option to every tool this script hands a path to."""
    db = tmp_path / "cadence.db"
    sqlite3.connect(db).close()

    result = _run(db, backup_dir="-rf")
    assert result.returncode == 2, result.stdout + result.stderr
    assert "refusing path" in result.stderr, result.stderr


def test_backup_still_accepts_an_ordinary_path(tmp_path: Path) -> None:
    """The guard is only worth having if it leaves the real paths alone."""
    db = tmp_path / "cadence.db"
    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE session (id TEXT PRIMARY KEY)")
    connection.commit()
    connection.close()

    result = _run(db, backup_dir=str(tmp_path / "backups-2"))
    assert result.returncode == 0, result.stdout + result.stderr
