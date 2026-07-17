#!/usr/bin/env bash
# Job Squire — restore from a backup created by scripts/backup.sh
#
# Usage:
#   ./scripts/restore.sh path/to/job-squire-backup-*.tgz
#
# Env overrides:
#   CONTAINER_NAME   Compose service/container name (default: job-squire)
#   DATA_ENV_DIR     Path to the directory holding data/.env (default:
#                     ./job-squire/data) -- see backup.sh's header comment
#                     for why this is the one thing still a host path.
#
# What this does:
#   /data is a named Docker volume, not a host bind mount, so a restore
#   can't just move a host directory aside and untar into it the way older
#   versions did. Instead: tear the container and its volume down entirely
#   (`docker compose down -v`, never deletes the *archive* you're restoring
#   from, just the live container+volume), extract the backup, recreate the
#   container without starting it (`docker compose create`) so a fresh empty
#   volume exists, `docker cp` the restored data straight into it, then
#   start. The image's own init-data-dir service re-owns everything under
#   /data to the app's account on every boot, so there's no manual chown
#   step needed here (unlike the old bind-mount version of this script).
#
# IMPORTANT — SECRET_KEY: the archive includes the data/.env that was active
# at backup time, so by default this restores that SECRET_KEY too, which
# keeps every encrypted secret (provider keys, SMTP password, Anthropic key,
# OAuth tokens) readable. If you are restoring onto a host that already has
# its own data/.env (e.g. moving to new hardware) and want to KEEP that key
# instead, answer "n" when prompted and merge the two .env files by hand
# afterward — see docs/backup-restore.md and docs/deployment.md's "Rotating
# SECRET_KEY" section for what re-entering secrets involves if the keys end
# up mismatched.
#
# Podman users: replace `docker compose`/`docker` below with `podman
# compose`/`podman`.

set -euo pipefail

BACKUP_FILE="${1:?Usage: ./scripts/restore.sh path/to/job-squire-backup-*.tgz}"
CONTAINER="${CONTAINER_NAME:-job-squire}"
DATA_ENV_DIR="${DATA_ENV_DIR:-./job-squire/data}"

if [[ ! -f "$BACKUP_FILE" ]]; then
  echo "Backup file not found: $BACKUP_FILE" >&2
  exit 1
fi

echo "This will:"
echo "  1. Stop and remove the $CONTAINER container and its data volume"
echo "  2. Extract $BACKUP_FILE and copy it into a freshly created volume"
echo "  3. Restore data/.env at $DATA_ENV_DIR (the previous one is kept, not deleted)"
echo "  4. Start the container"
echo
read -r -p "Continue? [y/N] " ans
if [[ "$ans" != "y" && "$ans" != "Y" ]]; then
  echo "Aborted."
  exit 1
fi

echo "Tearing down the current container and its data volume (if any)..."
docker compose down -v

TS="$(date +%Y%m%dT%H%M%S)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
tar xzf "$BACKUP_FILE" -C "$WORK"

if [[ ! -f "$WORK/job-squire.db" ]]; then
  echo "The archive has no job-squire.db -- doesn't look like a job-squire backup." >&2
  exit 1
fi

# data/.env travels inside the archive but is restored to the host
# separately, not copied into the volume -- see this script's header.
if [[ -f "$WORK/.env" ]]; then
  mkdir -p "$DATA_ENV_DIR"
  if [[ -f "$DATA_ENV_DIR/.env" ]]; then
    SIDECAR="$DATA_ENV_DIR/.env.pre-restore-$TS"
    read -r -p "$DATA_ENV_DIR/.env already exists. Overwrite with the archive's SECRET_KEY? [y/N] " env_ans
    if [[ "$env_ans" == "y" || "$env_ans" == "Y" ]]; then
      mv "$DATA_ENV_DIR/.env" "$SIDECAR"
      echo "Previous data/.env moved to $SIDECAR"
      mv "$WORK/.env" "$DATA_ENV_DIR/.env"
    else
      echo "Keeping the existing data/.env -- if secrets don't decrypt after this restore, see"
      echo "docs/deployment.md's 'Rotating SECRET_KEY' section."
    fi
  else
    mv "$WORK/.env" "$DATA_ENV_DIR/.env"
  fi
  chmod 600 "$DATA_ENV_DIR/.env" 2>/dev/null || true
  rm -f "$WORK/.env"
fi

echo "Creating the container so its data volume exists (not starting it yet)..."
docker compose create

echo "Copying restored data into the volume..."
docker cp "$WORK/." "$CONTAINER:/data"

echo "Starting the container..."
docker compose start "$CONTAINER"

echo
echo "Waiting for the container's aggregated healthcheck..."
sleep 10
docker compose ps "$CONTAINER"

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
