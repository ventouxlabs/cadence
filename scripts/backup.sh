#!/usr/bin/env bash
# Snapshot the SQLite database, verify the snapshot, then prune to the newest N.
#
# WAL means `cp` is not a backup: PRP-00 runs the database in WAL mode, so the .db file alone
# can be missing the newest committed transactions. `sqlite3 .backup` takes a consistent
# snapshot of a live database instead, with the app still running.
#
# Runs on the host against ./data/cadence.db (needs `sqlite3` on the VM), or inside the
# container, where CADENCE_DB_PATH already points at /app/data/cadence.db. Cron line is in
# docs/deploy.md.
#
#   CADENCE_DB_PATH      database to back up   (default: data/cadence.db)
#   CADENCE_BACKUP_DIR   where snapshots go    (default: <db dir>/backups)
#   CADENCE_BACKUP_KEEP  how many to keep      (default: 30)
#
# It uses the `sqlite3` CLI when there is one and Python's sqlite3 module otherwise. Both call
# the same SQLite Online Backup API; the fallback exists because the image ships no sqlite3
# binary, so `docker compose exec cadence ./scripts/backup.sh` would otherwise be impossible.

set -euo pipefail

DB="${CADENCE_DB_PATH:-data/cadence.db}"
OUT_DIR="${CADENCE_BACKUP_DIR:-$(dirname "$DB")/backups}"
KEEP="${CADENCE_BACKUP_KEEP:-30}"

# Both paths are operator-set environment, and the output one does not stay an argv: the sqlite3
# engine runs `.backup '$OUT'`, and a dot-command is split into words the way a shell would.
# A single quote in the path therefore ends the argument early, and `.backup` re-reads what
# follows as its optional ?DB? - so `a' 'b` writes the snapshot to `b`, outside OUT_DIR and
# outside the umask/chmod that protect it, while the integrity check and the prune still look at
# the path the script meant. Verified directly; it is argument splitting, not SQL injection - a
# `;` in a dot-command is not a statement separator. A leading `-` is the other shape, read as an
# option rather than a path (D-209).
for candidate in "$DB" "$OUT_DIR"; do
  if [[ "$candidate" == -* ]]; then
    echo "backup: refusing path '$candidate': a leading '-' is read as an option, not a path" >&2
    exit 2
  fi
  if [[ "$candidate" == *"'"* ]]; then
    echo "backup: refusing path '$candidate': a single quote breaks out of sqlite3's .backup argument" >&2
    exit 2
  fi
done

if [[ ! -f "$DB" ]]; then
  echo "backup: no database at '$DB'" >&2
  exit 1
fi
if ! [[ "$KEEP" =~ ^[0-9]+$ ]] || [[ "$KEEP" -lt 1 ]]; then
  echo "backup: CADENCE_BACKUP_KEEP must be a positive integer, got '$KEEP'" >&2
  exit 2
fi
PYTHON="$(command -v python3 || command -v python || true)"
if command -v sqlite3 >/dev/null 2>&1; then
  ENGINE="sqlite3"
elif [[ -n "$PYTHON" ]]; then
  ENGINE="python"
else
  echo "backup: neither the sqlite3 CLI nor python is available (apt install sqlite3)" >&2
  exit 3
fi

snapshot() {
  if [[ "$ENGINE" == "sqlite3" ]]; then
    sqlite3 "$1" ".backup '$2'"
  else
    "$PYTHON" -c 'import sqlite3,sys
source = sqlite3.connect(sys.argv[1])
target = sqlite3.connect(sys.argv[2])
try:
    source.backup(target)
finally:
    target.close()
    source.close()' "$1" "$2"
  fi
}

integrity() {
  if [[ "$ENGINE" == "sqlite3" ]]; then
    sqlite3 "$1" "PRAGMA integrity_check;"
  else
    "$PYTHON" -c 'import sqlite3,sys
db = sqlite3.connect(sys.argv[1])
try:
    print(db.execute("PRAGMA integrity_check").fetchone()[0])
finally:
    db.close()' "$1"
  fi
}

# Before the directory is made and before anything is written into it. A snapshot is the whole
# database - a child's training history, bodyweights and body-fat readings - and VM-201 is a
# shared host. The default umask leaves it 0644 in a 0755 directory, readable by every account
# on the box, while the .env beside it is 600. Set here rather than around `snapshot` alone so
# the directory itself is 0700 too (D-201).
umask 077

mkdir -p "$OUT_DIR" 2>/dev/null || true
# Refuse *before* doing any work if the snapshot cannot land, and say how to run it properly.
# On VM-201 `data/` belongs to the container's uid 10001 (D-162), so running this from the host
# used to reach the `chmod` below, die under `set -e`, and print a permissions warning - leaving
# an operator who had just "taken a backup" with no backup and a rollback point that does not
# exist. A backup tool must never fail in a way that reads like a warning (D-277).
if [[ ! -d "$OUT_DIR" ]] || [[ ! -w "$OUT_DIR" ]]; then
  echo "backup: cannot write to '$OUT_DIR' - no snapshot was taken." >&2
  echo "backup: on the VM, data/ is owned by the container's user, so run it in the container:" >&2
  echo "backup:   docker compose exec -T cadence ./scripts/backup.sh" >&2
  exit 4
fi
# Explicit, because the umask only governs directories *this* script creates and on VM-201 it
# does not create this one: scripts/deploy.sh runs `mkdir -p data/backups` on every deploy, with
# the deploying user's umask, which leaves it 0755. Without this line the snapshots would be
# 0600 inside a directory anyone could list. Not fatal on its own - the files are what matter -
# but the point is that the directory's mode must not depend on who got there first.
#
# Only when we own it: a directory we can write but not chmod still takes a 0600 snapshot, and
# losing the backup over the mode of its parent would be the same mistake in the other
# direction. Warn, keep the backup.
if [[ -O "$OUT_DIR" ]]; then
  chmod 700 "$OUT_DIR"
elif [[ "$(stat -c %a "$OUT_DIR" 2>/dev/null || echo 700)" != "700" ]]; then
  echo "backup: '$OUT_DIR' is not mine to chmod; snapshots are still 0600 but the directory is listable" >&2
fi

OUT="$OUT_DIR/cadence-$(date +%Y%m%d-%H%M).db"

# `set -e` plus this ordering is what protects the history: if the snapshot fails, the script
# exits here and the prune below never runs. A failed backup that also pruned would eat one
# good snapshot every night until there were none.
snapshot "$DB" "$OUT"

# On the copy, not the original: a snapshot that cannot be opened is worse than no snapshot,
# and this catches it while the source is still there to try again from.
if ! integrity "$OUT" | grep -qx "ok"; then
  echo "backup: integrity check failed on '$OUT'; keeping it for inspection" >&2
  exit 4
fi

# Newest first, drop everything past KEEP. `xargs -r` makes the first-ever run, with nothing to
# prune, a no-op rather than an error.
ls -1t "$OUT_DIR"/cadence-*.db 2>/dev/null | tail -n "+$((KEEP + 1))" | xargs -r rm --

echo "backup: $OUT (via $ENGINE)"
