#!/usr/bin/env bash
# Job Squire — update script
#
# Pulls the latest image, restarts containers, and optionally checks whether
# a new release is available on GitHub.
#
# Usage:
#   bash update.sh                     # check version, pull, restart
#   bash update.sh --check-only        # check for updates without pulling
#   bash update.sh --no-check          # pull and restart, skip version check
#   bash update.sh --enable-version-check   # turn on version checks (persists)
#   bash update.sh --disable-version-check  # turn off version checks (persists)
#
# Version checks are enabled by default. To disable permanently, run:
#   bash update.sh --disable-version-check
#
# The state file at $HOME/.jobsquire-install.state (or ./.install.state) stores
# the VERSION_CHECK preference along with install details.

set -euo pipefail

# ── Colors / output helpers ───────────────────────────────────────────────────
if [[ -t 1 ]]; then
  RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
  BLUE='\033[0;34m'; BOLD='\033[1m'; RESET='\033[0m'
else
  RED=''; GREEN=''; YELLOW=''; BLUE=''; BOLD=''; RESET=''
fi

info()    { echo -e "${BLUE}→${RESET} $*"; }
ok()      { echo -e "${GREEN}✓${RESET} $*"; }
warn()    { echo -e "${YELLOW}!${RESET} $*"; }
die()     { echo -e "${RED}✗${RESET} $*" >&2; exit 1; }
section() { echo -e "\n${BOLD}── $* ──────────────────────────────────────────────────${RESET}"; }

# ── Parse flags ───────────────────────────────────────────────────────────────
CHECK_ONLY=false
NO_CHECK=false
MODIFY_CHECK=""

for _arg in "${@:-}"; do
    case "$_arg" in
        --check-only)              CHECK_ONLY=true ;;
        --no-check)                NO_CHECK=true ;;
        --enable-version-check)    MODIFY_CHECK="true" ;;
        --disable-version-check)   MODIFY_CHECK="false" ;;
        --help|-h)
            sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "Unknown flag: $_arg"
            echo "Usage: bash update.sh [--check-only | --no-check | --enable-version-check | --disable-version-check]"
            exit 1
            ;;
    esac
done

# ── Load install state ────────────────────────────────────────────────────────
INSTALL_DIR=""
RUNTIME=""
COMPOSE_CMD=""
VERSION_CHECK="true"

_load_state() {
    local _file="$1"
    [[ -f "$_file" ]] || return 1
    # shellcheck source=/dev/null
    source "$_file" 2>/dev/null || return 1
    return 0
}

if ! _load_state "./.install.state" && ! _load_state "$HOME/.jobsquire-install.state"; then
    # State file not found — try to infer from the current directory.
    if [[ -f "./docker-compose.yml" ]]; then
        INSTALL_DIR="$(pwd)"
    else
        die "No install state found. Run this script from your Job Squire directory, or run install.sh first."
    fi
fi

[[ -z "$INSTALL_DIR" ]] && INSTALL_DIR="$(pwd)"
ENV_FILE="$INSTALL_DIR/data/.env"
STATE_FILE_HOME="$HOME/.jobsquire-install.state"
STATE_FILE_LOCAL="$INSTALL_DIR/.install.state"

# Detect runtime if not in state.
if [[ -z "$RUNTIME" ]]; then
    command -v docker &>/dev/null && RUNTIME=docker || true
    command -v podman &>/dev/null && RUNTIME=podman || true
    [[ -z "$RUNTIME" ]] && die "No container runtime found. Is Docker or Podman installed?"
fi

# Detect compose command if not in state.
if [[ -z "$COMPOSE_CMD" ]]; then
    if [[ "$RUNTIME" == "docker" ]]; then
        docker compose version &>/dev/null 2>&1 \
            && COMPOSE_CMD="docker compose" \
            || COMPOSE_CMD="docker-compose"
    else
        podman compose version &>/dev/null 2>&1 \
            && COMPOSE_CMD="podman compose" \
            || COMPOSE_CMD="podman-compose"
    fi
fi

[[ -f "$ENV_FILE" ]] || die "No .env file found at $ENV_FILE. Is Job Squire installed?"
[[ -f "$INSTALL_DIR/docker-compose.yml" ]] \
    || die "No docker-compose.yml found at $INSTALL_DIR."

# ── Persist VERSION_CHECK toggle if requested ─────────────────────────────────
if [[ -n "$MODIFY_CHECK" ]]; then
    VERSION_CHECK="$MODIFY_CHECK"

    _update_state_key() {
        local _file="$1" _key="$2" _val="$3"
        [[ -f "$_file" ]] || return 0
        if grep -q "^${_key}=" "$_file"; then
            # Portable sed in-place: write to tmp then move.
            local _tmp
            _tmp=$(mktemp)
            sed "s|^${_key}=.*|${_key}=${_val}|" "$_file" > "$_tmp" && mv "$_tmp" "$_file"
        else
            echo "${_key}=${_val}" >> "$_file"
        fi
    }

    _update_state_key "$STATE_FILE_HOME"  "VERSION_CHECK" "$VERSION_CHECK"
    _update_state_key "$STATE_FILE_LOCAL" "VERSION_CHECK" "$VERSION_CHECK"

    if [[ "$MODIFY_CHECK" == "true" ]]; then
        ok "Version checks enabled. Run 'bash update.sh --check-only' to test."
    else
        ok "Version checks disabled. Run 'bash update.sh --enable-version-check' to re-enable."
    fi
    exit 0
fi

# ── Banner ────────────────────────────────────────────────────────────────────
echo
echo -e "${BOLD}  Job Squire — update${RESET}"
echo

# ── Version check ─────────────────────────────────────────────────────────────
# Compares the locally installed image version against the latest GitHub release.
# Skipped when VERSION_CHECK=false or --no-check is passed.

_parse_json_field() {
    # Portable JSON field extraction without requiring jq.
    local _json="$1" _field="$2"
    if command -v python3 &>/dev/null; then
        echo "$_json" | python3 -c \
            "import sys,json; print(json.load(sys.stdin).get('${_field}',''))" 2>/dev/null || echo ""
    elif command -v jq &>/dev/null; then
        echo "$_json" | jq -r ".${_field} // empty" 2>/dev/null || echo ""
    else
        # Fallback: grep for the field value.
        echo "$_json" | grep -o "\"${_field}\":\"[^\"]*\"" | cut -d'"' -f4 || echo ""
    fi
}

check_for_updates() {
    local _latest_tag _current_version _api_response

    # Hit the GitHub releases API.
    _api_response=$(curl -fsSL --max-time 8 \
        "https://api.github.com/repos/dellipse/job-squire/releases/latest" 2>/dev/null || echo "")

    if [[ -z "$_api_response" ]]; then
        warn "Could not reach GitHub to check for updates (network unavailable or rate-limited)."
        return 0
    fi

    _latest_tag=$(_parse_json_field "$_api_response" "tag_name")

    if [[ -z "$_latest_tag" ]]; then
        warn "No releases found on GitHub yet."
        return 0
    fi

    # Try to read the version label from the locally pulled image.
    _current_version=$($RUNTIME inspect \
        "ghcr.io/dellipse/job-squire:latest" \
        --format '{{index .Config.Labels "org.opencontainers.image.version"}}' 2>/dev/null || echo "")

    # Strip leading 'v' for comparison.
    local _latest_clean="${_latest_tag#v}"
    local _current_clean="${_current_version#v}"

    if [[ -z "$_current_version" ]]; then
        info "Latest release on GitHub: ${_latest_tag}"
        info "(Could not determine the currently installed version — run 'bash update.sh' to pull latest.)"
    elif [[ "$_current_clean" == "$_latest_clean" ]]; then
        ok "Up to date: ${_latest_tag}"
    else
        echo
        echo -e "  ${YELLOW}${BOLD}New version available: ${_latest_tag}${RESET}"
        echo    "  Currently installed:   v${_current_clean}"
        echo    "  Run 'bash update.sh' to update."
        echo
    fi
}

if [[ "${VERSION_CHECK:-true}" == "true" && "$NO_CHECK" != "true" ]]; then
    section "Version check"
    check_for_updates
fi

if $CHECK_ONLY; then
    echo
    info "Check complete. Pass no flags to pull and restart."
    echo
    exit 0
fi

# ── Pull latest image ─────────────────────────────────────────────────────────
section "Pull latest image"

cd "$INSTALL_DIR"
info "Pulling ghcr.io/dellipse/job-squire:latest ..."
$COMPOSE_CMD --env-file "$ENV_FILE" pull

# ── Restart containers ────────────────────────────────────────────────────────
section "Restart containers"

info "Restarting job-squire, job-squire-worker, job-squire-mcp ..."
$COMPOSE_CMD --env-file "$ENV_FILE" up -d \
    --no-deps --force-recreate \
    job-squire job-squire-worker job-squire-mcp

sleep 3

info "Container status:"
$COMPOSE_CMD --env-file "$ENV_FILE" ps

# ── Done ──────────────────────────────────────────────────────────────────────
echo
ok "Update complete."
echo
echo "Hard-refresh your browser (Ctrl+Shift+R / Cmd+Shift+R) to clear cached CSS/JS."
echo
echo "To check for a new version without updating: bash update.sh --check-only"
echo "To disable automatic version checks:         bash update.sh --disable-version-check"
echo
