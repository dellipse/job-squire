#!/usr/bin/env bash
# Job Squire — WAL-safe backup script
#
# Usage:
#   ./scripts/backup.sh [backup-dir]
#
# Env overrides:
#   CONTAINER_NAME   Compose service/container name (default: job-squire)
#   DATA_ENV_DIR     Path to the directory holding data/.env (default:
#                     ./job-squire/data) -- the only thing this script still
#                     reads directly off the host; everything else the app
#                     itself owns lives in a named Docker volume now, not a
#                     host path.
#
# What this does:
#   /data is a named Docker volume, not a host bind mount, so this script
#   cannot read job-squire.db off the host filesystem the way older versions
#   did. Instead it runs `python -m app.backup_cli` INSIDE the running
#   container -- the same WAL-safe snapshot mechanism job-squire-cli's own
#   `backup` command uses (SQLite's Online Backup API via
#   sqlite3.Connection.backup(), the same thing the `sqlite3 .backup` CLI
#   command does), pulling out the database, uploads/, and the other files
#   named in app/backup.py's _SIDE_FILES -- and captures its stdout. The
#   container is never stopped; the Online Backup API is safe to run
#   against a live, concurrently-written database. data/.env is added
#   separately from the host, since it's still a real host file (compose's
#   env_file: has to read it before the container or its volume exist at
#   all).
#
# Podman users: replace `docker compose`/`docker` below with `podman
# compose`/`podman`.

set -euo pipefail

CONTAINER="${CONTAINER_NAME:-job-squire}"
DATA_ENV_DIR="${DATA_ENV_DIR:-./job-squire/data}"
DEST_DIR="${1:-./backups}"

if ! command -v docker >/dev/null 2>&1; then
  echo "docker not found on this host -- this script needs the container running to snapshot its data." >&2
  exit 1
fi

TS="$(date +%Y%m%dT%H%M%S)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

mkdir -p "$DEST_DIR"

echo "Snapshotting live data from the $CONTAINER container (WAL-safe) ..."
if ! docker compose exec -T "$CONTAINER" python3 -m app.backup_cli > "$WORK/data.tgz"; then
  echo "Failed to snapshot data from the $CONTAINER container." >&2
  echo "Is it running and healthy? Check with: docker compose ps" >&2
  exit 1
fi

tar xzf "$WORK/data.tgz" -C "$WORK"
rm -f "$WORK/data.tgz"

if [[ ! -f "$WORK/job-squire.db" ]]; then
  echo "The container reported no database yet -- nothing to back up. (Has Getting Started run?)" >&2
  exit 1
fi

# data/.env is the one thing that's still a plain host file (see this
# script's header comment) -- add it from the host, not the container.
if [[ -f "$DATA_ENV_DIR/.env" ]]; then
  cp -a "$DATA_ENV_DIR/.env" "$WORK/"
else
  echo "Warning: no $DATA_ENV_DIR/.env found -- the archive will be missing SECRET_KEY and won't be" >&2
  echo "restorable as-is. Set DATA_ENV_DIR if data/.env lives somewhere else." >&2
fi

ARCHIVE="$DEST_DIR/job-squire-backup-$TS.tgz"
tar czf "$ARCHIVE" -C "$WORK" .

echo "Wrote $ARCHIVE"
echo "Contains: job-squire.db (integrity-checked snapshot), uploads/, candidate_profile.md,"
echo "profile_prompt.md, oauth_tokens.json, privacy_vault.json, .env"
echo "Restore with: ./scripts/restore.sh $ARCHIVE"
