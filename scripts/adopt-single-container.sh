#!/usr/bin/env bash
# Job Squire — adopt an existing three-container install onto the
# single-container image (docker-compose.single.yml)
#
# Usage:
#   ./scripts/adopt-single-container.sh [install-dir]
#
#   install-dir defaults to the current directory. It must contain
#   data/.env (the existing install's config) and docker-compose.yml (the
#   three-container topology being migrated away from).
#
# What this does — additive only, never rewrites or re-encrypts anything:
#   1. Confirms data/.env and docker-compose.single.yml exist.
#   2. Backs up data/.env before touching it.
#   3. Appends TRUST_PROXY=1 if not already set, replicating the
#      pre-single-container app's *unconditional* ProxyFix (it trusted
#      X-Forwarded-* headers no matter what — DEPLOY_MODE didn't exist yet).
#   4. Appends SESSION_COOKIE_SECURE=true if not already set, replicating
#      the old code's implicit default (most real installs already set
#      this explicitly via install.sh, so this rarely fires).
#   5. Prints the derived instance/cookie name and the exact next commands
#      to stop the old stack, start the new one, and verify.
#
# What this deliberately does NOT do:
#   - Touch SECRET_KEY, or any other existing line in data/.env.
#   - Set DEPLOY_MODE. A guessed DEPLOY_MODE=network could refuse to boot
#     (the Prompt 5 startup guard is fatal if PUBLIC_URL isn't https) --
#     far worse for a migration tool than an occasional warning banner.
#     Left unset, DEPLOY_MODE defaults to "local", which never exits
#     non-zero. See the printed guidance below for when to set it yourself.
#   - Stop or start any containers. See docs/adopt-single-container.md for
#     the full runbook this script is one step of.
#
# Podman users: this script only touches files, not the runtime, so it
# works unmodified under Podman too.

set -euo pipefail

INSTALL_DIR="${1:-.}"
DATA_DIR="$INSTALL_DIR/data"
ENV_FILE="$DATA_DIR/.env"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "No existing install found: $ENV_FILE does not exist." >&2
  echo "Usage: ./scripts/adopt-single-container.sh [install-dir]" >&2
  exit 1
fi

if [[ ! -f "$INSTALL_DIR/docker-compose.single.yml" ]]; then
  echo "docker-compose.single.yml not found in $INSTALL_DIR." >&2
  echo "Update your checkout (git pull) so it includes the single-container compose file, then re-run." >&2
  exit 1
fi

if ! grep -q '^SECRET_KEY=' "$ENV_FILE"; then
  echo "No SECRET_KEY found in $ENV_FILE -- this doesn't look like an existing Job Squire install." >&2
  exit 1
fi

INSTANCE_NAME=$(grep -m1 '^INSTANCE_NAME=' "$ENV_FILE" | cut -d= -f2- || true)
INSTANCE_NAME="${INSTANCE_NAME:-job-squire}"

# Mirror app/__init__.py's SESSION_COOKIE_NAME derivation, for display only --
# the running app computes this itself at boot; this is just a preview.
COOKIE_NAME=$(echo "$INSTANCE_NAME" | tr '[:upper:]' '[:lower:]' | tr ' -' '__')
COOKIE_NAME="${COOKIE_NAME:-jt}_session"

PUBLIC_URL=$(grep -m1 '^PUBLIC_URL=' "$ENV_FILE" | cut -d= -f2- || true)

echo "Adopting install at: $INSTALL_DIR"
echo "  Instance name:  $INSTANCE_NAME"
echo "  Cookie name:    $COOKIE_NAME (unchanged -- derived the same way as before)"
echo "  Data directory: $DATA_DIR (unchanged)"
echo

if command -v docker &>/dev/null && docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$INSTANCE_NAME"; then
  echo "NOTE: a container named '$INSTANCE_NAME' looks like it's still running." >&2
  echo "Stop the three-container stack first (see docs/adopt-single-container.md)" >&2
  echo "so nothing writes to the database while you switch images." >&2
  echo >&2
fi

BACKUP="$ENV_FILE.bak.$(date +%Y%m%dT%H%M%S)"
cp "$ENV_FILE" "$BACKUP"
echo "Backed up current config to: $BACKUP"

APPENDED=()

if ! grep -q '^TRUST_PROXY=' "$ENV_FILE"; then
  {
    echo ""
    echo "# Added by adopt-single-container.sh: the pre-single-container app"
    echo "# always trusted the reverse proxy's X-Forwarded-* headers"
    echo "# unconditionally. TRUST_PROXY=1 replicates that exactly, regardless"
    echo "# of DEPLOY_MODE. If this instance has never actually sat behind a"
    echo "# reverse proxy, you can safely change this to 0."
    echo "TRUST_PROXY=1"
  } >> "$ENV_FILE"
  APPENDED+=("TRUST_PROXY=1")
fi

if ! grep -q '^SESSION_COOKIE_SECURE=' "$ENV_FILE"; then
  {
    echo ""
    echo "# Added by adopt-single-container.sh: the pre-single-container app"
    echo "# defaulted SESSION_COOKIE_SECURE to true when unset. This line"
    echo "# preserves that."
    echo "SESSION_COOKIE_SECURE=true"
  } >> "$ENV_FILE"
  APPENDED+=("SESSION_COOKIE_SECURE=true")
fi

if [[ ${#APPENDED[@]} -eq 0 ]]; then
  echo "No changes needed -- $ENV_FILE already sets TRUST_PROXY and SESSION_COOKIE_SECURE explicitly."
else
  echo "Appended to $ENV_FILE: ${APPENDED[*]}"
fi

echo
if [[ "$PUBLIC_URL" == https://* ]]; then
  echo "PUBLIC_URL is already an https:// URL. Once the single-container instance"
  echo "is up and healthy, you can add DEPLOY_MODE=network to $ENV_FILE for full"
  echo "parity with a fresh network-mode install (this also turns on the startup"
  echo "safety guard's network-mode checks)."
else
  echo "PUBLIC_URL is not an https:// URL (or is unset). DEPLOY_MODE is left unset"
  echo "(defaults to 'local'), so the app boots either way. If this instance"
  echo "genuinely sits behind a TLS-terminating reverse proxy, set PUBLIC_URL to"
  echo "its https:// address and add DEPLOY_MODE=network once you've confirmed it."
fi

echo
echo "Next steps (see docs/adopt-single-container.md for the full runbook):"
echo "  1. Stop the old stack:"
echo "       cd $INSTALL_DIR && docker compose --env-file data/.env -f docker-compose.yml down"
echo "  2. Start the single-container stack:"
echo "       cd $INSTALL_DIR && docker compose --env-file data/.env -f docker-compose.single.yml up -d"
echo "  3. Verify:"
echo "       docker compose --env-file data/.env -f docker-compose.single.yml ps"
echo "       curl -f http://localhost:\${APP_HOST_PORT:-8080}/health"
echo "       Log in and confirm stored provider/SMTP keys still work."
