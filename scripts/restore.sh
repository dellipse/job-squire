#!/usr/bin/env bash
# Job Squire — restore from a backup created by scripts/backup.sh
#
# Usage:
#   ./scripts/restore.sh path/to/job-squire-backup-*.tgz
#
# Env overrides (same names as data/.env):
#   DATA_HOST_DIR   Path to the data directory (default: ./job-squire/data)
#
# What this does:
#   Stops the Job Squire container, moves the current data directory
#   aside (never deletes it — see the .pre-restore-<timestamp> path printed
#   at the end), extracts the backup archive in its place, then restarts.
#   Stopping the container for the few seconds this takes is the simplest
#   way to guarantee nothing writes to the data directory mid-restore.
#
# IMPORTANT — SECRET_KEY: the archive includes the .env that was active at
# backup time, so by default this restores that SECRET_KEY too, which keeps
# every encrypted secret (provider keys, SMTP password, Anthropic key, OAuth
# tokens) readable. If you are restoring onto a host that already has its own
# data/.env (e.g. moving to new hardware) and want to KEEP that key instead,
# answer "n" when prompted and merge the two .env files by hand afterward —
# see docs/backup-restore.md and docs/deployment.md's "Rotating SECRET_KEY"
# section for what re-entering secrets involves if the keys end up mismatched.
#
# Podman users: replace `docker compose` below with `podman compose` (or
# `podman-compose`, depending on how you installed it).

set -euo pipefail

BACKUP_FILE="${1:?Usage: ./scripts/restore.sh path/to/job-squire-backup-*.tgz}"
DATA_DIR="${DATA_HOST_DIR:-./job-squire/data}"

if [[ ! -f "$BACKUP_FILE" ]]; then
  echo "Backup file not found: $BACKUP_FILE" >&2
  exit 1
fi

echo "This will:"
echo "  1. Stop the job-squire container"
echo "  2. Move $DATA_DIR aside (kept, not deleted)"
echo "  3. Extract $BACKUP_FILE into $DATA_DIR"
echo "  4. Restart the container"
echo
read -r -p "Continue? [y/N] " ans
if [[ "$ans" != "y" && "$ans" != "Y" ]]; then
  echo "Aborted."
  exit 1
fi

echo "Stopping the container..."
docker compose stop job-squire

TS="$(date +%Y%m%dT%H%M%S)"
if [[ -d "$DATA_DIR" ]]; then
  SIDECAR="${DATA_DIR%/}.pre-restore-$TS"
  mv "$DATA_DIR" "$SIDECAR"
  echo "Previous data moved to $SIDECAR"
fi

mkdir -p "$DATA_DIR"
tar xzf "$BACKUP_FILE" -C "$DATA_DIR"

# The container runs as PUID:PGID (see data/.env); make sure the restored
# files are owned correctly, or the app can't write to them on next boot.
if [[ -f "$DATA_DIR/.env" ]]; then
  PUID_VAL="$(grep -E '^PUID=' "$DATA_DIR/.env" | cut -d= -f2 || true)"
  PGID_VAL="$(grep -E '^PGID=' "$DATA_DIR/.env" | cut -d= -f2 || true)"
fi
PUID_VAL="${PUID_VAL:-1000}"
PGID_VAL="${PGID_VAL:-1000}"
if command -v sudo >/dev/null 2>&1; then
  sudo chown -R "${PUID_VAL}:${PGID_VAL}" "$DATA_DIR" || \
    echo "Warning: could not chown $DATA_DIR to ${PUID_VAL}:${PGID_VAL} — do this manually if the app fails to write." >&2
fi

echo "Restored into $DATA_DIR."
echo "Starting the container..."
docker compose up -d job-squire

echo
echo "Waiting for the container's aggregated healthcheck..."
sleep 10
docker compose ps job-squire

cat <<'EOF'

Verify the restore:
  - `docker compose ps` above shows the container as "healthy" (the
    aggregated healthcheck covers web, worker, and mcp together -- it may
    take up to a minute for the worker's first heartbeat to land).
  - curl -is http://localhost:8080/health           -> {"ok": true}
  - Log in and confirm your jobs/pipeline are present.
  - Settings > History tab shows the SearchRun history you expect (a gap
    here just means no scheduled run fell in that window, not a bad restore).
  - If secrets show a "could not decrypt" warning, the restored .env's
    SECRET_KEY doesn't match what encrypted them — see docs/deployment.md's
    "Rotating SECRET_KEY" section for the re-entry steps.
EOF
