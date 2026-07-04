#!/usr/bin/env bash
# Job Squire — WAL-safe backup script
#
# Usage:
#   ./scripts/backup.sh [backup-dir]
#
# Env overrides (same names as data/.env):
#   DATA_HOST_DIR   Path to the data directory (default: ./job-squire/data)
#
# What this does:
#   The SQLite DB runs in WAL mode, so `cp`/`tar` of job-squire.db alone can
#   miss committed data still sitting in job-squire.db-wal, and a plain copy
#   of all three files (.db/.db-wal/.db-shm) while the app is live is not
#   guaranteed atomic. This script instead uses Python's stdlib
#   sqlite3.Connection.backup(), which drives SQLite's own Online Backup API
#   -- the same mechanism the `sqlite3 .backup` CLI command uses -- to produce
#   a single consistent snapshot safely, even with the app running and
#   writing concurrently. No downtime required.
#
# Run this from the host (needs python3 with the stdlib sqlite3 module,
# present on effectively every Python install -- nothing extra to pip
# install). If your host has no python3, run the equivalent command inside
# the running container instead:
#   docker compose exec job-squire python3 -c "..."
# (see docs/backup-restore.md for the full inline command).
#
# Podman users: this script only shells out to `tar`/`python3`, not docker
# itself, so it works unmodified under Podman too.

set -euo pipefail

DATA_DIR="${DATA_HOST_DIR:-./job-squire/data}"
DEST_DIR="${1:-./backups}"

if [[ ! -d "$DATA_DIR" ]]; then
  echo "Data directory not found: $DATA_DIR" >&2
  echo "Set DATA_HOST_DIR or pass the right path, e.g.: DATA_HOST_DIR=/path/to/data ./scripts/backup.sh" >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 not found on this host. Run the backup from inside the container instead:" >&2
  echo "  docker compose exec job-squire python3 -c \"import sqlite3; s=sqlite3.connect('/data/job-squire.db'); d=sqlite3.connect('/data/job-squire-backup.db');" \
       "\nwith d: s.backup(d)\"" >&2
  exit 1
fi

TS="$(date +%Y%m%dT%H%M%S)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

mkdir -p "$DEST_DIR"

DB_PATH="$DATA_DIR/job-squire.db"
if [[ ! -f "$DB_PATH" ]]; then
  echo "No job-squire.db found at $DB_PATH — nothing to back up yet." >&2
  exit 1
fi

echo "Snapshotting $DB_PATH (WAL-safe, live) ..."
python3 - "$DB_PATH" "$WORK/job-squire.db" <<'PY'
import sqlite3
import sys

src_path, dst_path = sys.argv[1], sys.argv[2]
src = sqlite3.connect(src_path)
dst = sqlite3.connect(dst_path)
try:
    with dst:
        src.backup(dst)
finally:
    src.close()
    dst.close()
PY

# Sanity check: the snapshot must open cleanly and pass an integrity check
# before we call the backup good.
python3 - "$WORK/job-squire.db" <<'PY'
import sqlite3
import sys

conn = sqlite3.connect(sys.argv[1])
result = conn.execute("PRAGMA integrity_check").fetchone()[0]
conn.close()
if result != "ok":
    print(f"Integrity check failed on the snapshot: {result}", file=sys.stderr)
    sys.exit(1)
PY

# Bring along everything else needed for a full restore.
cp -a "$DATA_DIR/uploads" "$WORK/" 2>/dev/null || true
cp -a "$DATA_DIR/candidate_profile.md" "$WORK/" 2>/dev/null || true
cp -a "$DATA_DIR/oauth_tokens.json" "$WORK/" 2>/dev/null || true
cp -a "$DATA_DIR/.env" "$WORK/" 2>/dev/null || true

ARCHIVE="$DEST_DIR/job-squire-backup-$TS.tgz"
tar czf "$ARCHIVE" -C "$WORK" .

echo "Wrote $ARCHIVE"
echo "Contains: job-squire.db (integrity-checked snapshot), uploads/, candidate_profile.md, oauth_tokens.json, .env"
echo "Restore with: ./scripts/restore.sh $ARCHIVE"
