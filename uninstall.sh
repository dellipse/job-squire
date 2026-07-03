#!/usr/bin/env bash
# Job Squire — uninstall script
#
# Undoes what install.sh did. Each step shows you what it will do and asks
# before taking any action. Nothing is deleted without your confirmation.
#
# Usage:
#   bash uninstall.sh
#
# The script looks for an install state file in:
#   1. ./.install.state  (if you run this from the Job Squire directory)
#   2. $HOME/.jobsquire-install.state  (written by install.sh during install)
#
# If neither is found, the script will ask where Job Squire is installed.

# Note: not using set -e — we want to continue even if individual steps fail.
set -uo pipefail

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
err()     { echo -e "${RED}✗${RESET} $*" >&2; }
section() { echo -e "\n${BOLD}── $* ──────────────────────────────────────────────────${RESET}"; }

read_tty() { read "$@" </dev/tty; }

ask() {
    local __var="$1" __msg="$2" __default="${3:-}"
    local __prompt
    if [[ -n "$__default" ]]; then
        __prompt="${BOLD}${__msg}${RESET} [${__default}]: "
    else
        __prompt="${BOLD}${__msg}${RESET}: "
    fi
    read_tty -r -p "$(echo -e "$__prompt")" __reply || true
    printf -v "$__var" '%s' "${__reply:-$__default}"
}

confirm() {
    local __msg="$1" __default="${2:-y}" __hint __reply
    [[ "$__default" == "y" ]] && __hint="[Y/n]" || __hint="[y/N]"
    read_tty -r -p "$(echo -e "${BOLD}${__msg}${RESET} ${__hint} ")" __reply || true
    __reply="${__reply:-$__default}"
    [[ "$__reply" =~ ^[Yy] ]]
}

# ── OS detection ──────────────────────────────────────────────────────────────
OS_FAMILY="unknown"
if [[ "$(uname -s)" == "Darwin" ]]; then
    OS_FAMILY="macos"
elif [[ -f /etc/os-release ]]; then
    _id=$(grep -m1 "^ID=" /etc/os-release | cut -d= -f2- | tr -d '"' | tr '[:upper:]' '[:lower:]')
    _id_like=$(grep -m1 "^ID_LIKE=" /etc/os-release 2>/dev/null | cut -d= -f2- | tr -d '"' | tr '[:upper:]' '[:lower:]' || echo "")
    case "${_id:-} ${_id_like:-}" in
        *fedora*|*rhel*|*centos*|*rocky*|*alma*) OS_FAMILY="rhel" ;;
        *debian*|*ubuntu*|*mint*)                OS_FAMILY="debian" ;;
        *arch*|*manjaro*|*endeavour*)            OS_FAMILY="arch" ;;
    esac
fi

# ── Banner ────────────────────────────────────────────────────────────────────
echo
echo -e "${BOLD}  Job Squire — uninstall${RESET}"
echo    "  https://github.com/dellipse/job-squire"
echo
echo "This script will ask before removing anything."
echo "Answer 'n' to any step you want to skip."
echo

# ── Load install state ────────────────────────────────────────────────────────
INSTALL_DIR=""
RUNTIME=""
COMPOSE_CMD=""
INSTALLED_RUNTIME=false
ADDED_TO_DOCKER_GROUP=false
INSTALLED_COLIMA=false
PODMAN_MACHINE_INIT=false
ENABLED_PODMAN_SOCKET=false
ENABLED_LINGERING=false
CREATED_INSTALL_DIR=false
INSTALL_DATE=""
COMPOSE_FILE="docker-compose.yml"
NETWORK_MODE="standalone"
SWAG_NETWORK=""

STATE_FILE=""
if [[ -f "./.install.state" ]]; then
    STATE_FILE="./.install.state"
elif [[ -f "$HOME/.jobsquire-install.state" ]]; then
    STATE_FILE="$HOME/.jobsquire-install.state"
fi

if [[ -n "$STATE_FILE" ]]; then
    ok "Found install state: $STATE_FILE"
    # shellcheck source=/dev/null
    source "$STATE_FILE" 2>/dev/null || true
    [[ -n "$INSTALL_DATE" ]] && info "Installed: $INSTALL_DATE"
else
    warn "No install state file found."
    warn "(Searched: ./.install.state and $HOME/.jobsquire-install.state)"
    echo
    ask INSTALL_DIR "Where is Job Squire installed?" "$HOME/jobsquire"
    if [[ -f "$INSTALL_DIR/.install.state" ]]; then
        # shellcheck source=/dev/null
        source "$INSTALL_DIR/.install.state" 2>/dev/null || true
        ok "Found state file at $INSTALL_DIR/.install.state"
    fi
fi

if [[ -z "$INSTALL_DIR" ]]; then
    ask INSTALL_DIR "Where is Job Squire installed?" "$HOME/jobsquire"
fi

# Fill in runtime details if missing from state.
if [[ -z "$RUNTIME" ]]; then
    command -v docker &>/dev/null && RUNTIME=docker || true
    command -v podman &>/dev/null && RUNTIME=podman || true
fi

if [[ -z "$COMPOSE_CMD" ]]; then
    if [[ "$RUNTIME" == "docker" ]]; then
        COMPOSE_CMD="docker compose"
    elif [[ "$RUNTIME" == "podman" ]]; then
        podman compose version &>/dev/null 2>&1 && COMPOSE_CMD="podman compose" || COMPOSE_CMD="podman-compose"
    fi
fi

echo
echo "Install directory:  ${INSTALL_DIR:-unknown}"
echo "Runtime:            ${RUNTIME:-unknown}"
echo
echo "Changes recorded by install.sh:"
[[ "$INSTALLED_RUNTIME"     == "true" ]] && echo "  - Installed $RUNTIME" || true
[[ "$INSTALLED_COLIMA"      == "true" ]] && echo "  - Installed Colima (macOS Docker runtime)" || true
[[ "$PODMAN_MACHINE_INIT"   == "true" ]] && echo "  - Initialized podman machine (macOS)" || true
[[ "$ADDED_TO_DOCKER_GROUP" == "true" ]] && echo "  - Added $USER to docker group" || true
[[ "$ENABLED_PODMAN_SOCKET" == "true" ]] && echo "  - Enabled podman.socket" || true
[[ "$ENABLED_LINGERING"     == "true" ]] && echo "  - Enabled loginctl linger" || true
[[ "$CREATED_INSTALL_DIR"   == "true" ]] && echo "  - Created $INSTALL_DIR" || true

# ── Step 1: Stop containers ───────────────────────────────────────────────────
section "Step 1: Stop containers"

ENV_FILE="$INSTALL_DIR/data/.env"

_compose_file_path="$INSTALL_DIR/$COMPOSE_FILE"
# Fall back to the base file if the SWAG variant isn't present (e.g. older install).
[[ ! -f "$_compose_file_path" ]] && _compose_file_path="$INSTALL_DIR/docker-compose.yml"

if [[ -f "$_compose_file_path" && -f "$ENV_FILE" && -n "$COMPOSE_CMD" ]]; then
    if confirm "Stop and remove Job Squire containers?"; then
        (
            cd "$INSTALL_DIR"
            $COMPOSE_CMD -f "$_compose_file_path" --env-file "$ENV_FILE" down --remove-orphans 2>/dev/null \
                && ok "Containers stopped and removed." \
                || warn "Could not stop containers cleanly. They may already be stopped."
        )
    else
        info "Skipping — containers left running."
    fi
else
    [[ ! -f "$_compose_file_path" ]] \
        && warn "No compose file found at $INSTALL_DIR — skipping container stop." || true
    [[ ! -f "$ENV_FILE" ]] \
        && warn "No .env file found — skipping container stop." || true
fi

# ── Step 2: Data directory ────────────────────────────────────────────────────
section "Step 2: Data directory"

DATA_DIR="$INSTALL_DIR/data"

if [[ -d "$DATA_DIR" ]]; then
    echo -e "${RED}${BOLD}WARNING:${RESET} $DATA_DIR contains your job applications, resume,"
    echo "candidate profile, interview notes, and all configuration."
    echo "This data cannot be recovered once deleted."
    echo
    if confirm "Delete the data directory? ($DATA_DIR)" "n"; then
        rm -rf "$DATA_DIR" \
            && ok "Data directory deleted." \
            || err "Could not delete $DATA_DIR — check permissions and try: rm -rf $DATA_DIR"
    else
        info "Data directory kept at $DATA_DIR"
        info "Delete it manually later with: rm -rf $DATA_DIR"
    fi
else
    info "Data directory not found at $DATA_DIR — nothing to remove."
fi

# ── Step 3: Install directory ─────────────────────────────────────────────────
section "Step 3: Install directory"

if [[ -d "$INSTALL_DIR" ]]; then
    if [[ "$CREATED_INSTALL_DIR" == "true" ]]; then
        echo "The directory $INSTALL_DIR was created by install.sh."
        if confirm "Remove the install directory?"; then
            rm -rf "$INSTALL_DIR" \
                && ok "Install directory removed." \
                || err "Could not remove $INSTALL_DIR — check permissions."
        else
            info "Install directory kept at $INSTALL_DIR"
        fi
    else
        info "$INSTALL_DIR was not created by install.sh (e.g., a cloned repo)."
        info "Leaving it in place. Remove manually if you no longer need it."
    fi
else
    info "Install directory not found — nothing to remove."
fi

# ── Step 4: macOS runtime cleanup ────────────────────────────────────────────
if [[ "$OS_FAMILY" == "macos" ]]; then

    if [[ "$PODMAN_MACHINE_INIT" == "true" ]]; then
        section "Step 4a: Podman machine (macOS)"
        echo "install.sh initialized a Podman machine to run containers."
        if confirm "Stop and remove the Podman machine?"; then
            podman machine stop 2>/dev/null \
                && ok "Podman machine stopped." \
                || warn "Machine may already be stopped."
            podman machine rm --force 2>/dev/null \
                && ok "Podman machine removed." \
                || warn "Could not remove podman machine — run: podman machine rm --force"
        else
            info "Podman machine left in place."
        fi
    fi

    if [[ "$INSTALLED_COLIMA" == "true" ]]; then
        section "Step 4b: Colima (macOS Docker runtime)"
        echo "install.sh installed and started Colima as the Docker runtime."
        if confirm "Stop and delete the Colima instance?"; then
            colima stop 2>/dev/null \
                && ok "Colima stopped." \
                || warn "Colima may already be stopped."
            colima delete --force 2>/dev/null \
                && ok "Colima instance deleted." \
                || warn "Could not delete Colima instance — run: colima delete --force"
        else
            info "Colima left in place."
        fi
    fi

fi

# ── Step 5: Linux Podman cleanup ─────────────────────────────────────────────
if [[ "$OS_FAMILY" != "macos" && "$RUNTIME" == "podman" \
        && ("$ENABLED_PODMAN_SOCKET" == "true" || "$ENABLED_LINGERING" == "true") ]]; then
    section "Step 5: Podman service cleanup"

    if [[ "$ENABLED_PODMAN_SOCKET" == "true" ]]; then
        if confirm "Disable the podman.socket user service?"; then
            systemctl --user disable --now podman.socket 2>/dev/null \
                && ok "podman.socket disabled." \
                || warn "Could not disable podman.socket — run: systemctl --user disable --now podman.socket"
        fi
    fi

    if [[ "$ENABLED_LINGERING" == "true" ]]; then
        if confirm "Disable loginctl lingering for $USER?"; then
            sudo loginctl disable-linger "$USER" 2>/dev/null \
                && ok "Lingering disabled." \
                || warn "Could not disable lingering — run: sudo loginctl disable-linger $USER"
        fi
    fi
fi

# ── Step 6: Docker group membership ──────────────────────────────────────────
if [[ "$ADDED_TO_DOCKER_GROUP" == "true" ]]; then
    section "Step 6: Docker group membership"
    echo "install.sh added $USER to the 'docker' group."
    if confirm "Remove $USER from the 'docker' group?"; then
        sudo gpasswd -d "$USER" docker 2>/dev/null \
            && ok "$USER removed from docker group. Takes effect on next login." \
            || warn "Could not remove from docker group — run: sudo gpasswd -d $USER docker"
    else
        info "$USER remains in the docker group."
    fi
fi

# ── Step 7: Remove runtime ────────────────────────────────────────────────────
if [[ "$INSTALLED_RUNTIME" == "true" && -n "$RUNTIME" ]]; then
    section "Step 7: Remove $RUNTIME"

    echo -e "${YELLOW}Warning:${RESET} $RUNTIME was installed by install.sh."
    echo "Removing it may affect other containers or images on this system."
    echo

    # Count non-Job-Squire containers/images as a safety check.
    _other_containers=0
    _other_images=0
    if command -v "$RUNTIME" &>/dev/null; then
        _other_containers=$($RUNTIME ps -a --format '{{.Names}}' 2>/dev/null \
            | grep -v -E "job-squire" | wc -l || echo "0")
        _other_images=$($RUNTIME images --format '{{.Repository}}' 2>/dev/null \
            | grep -v "dellipse/job-squire" | grep -v "^<none>$" | wc -l || echo "0")
    fi

    if (( _other_containers > 0 || _other_images > 0 )); then
        warn "Found ${_other_containers} other container(s) and ${_other_images} other image(s) not from Job Squire."
        warn "Removing $RUNTIME will also remove these."
        echo
    fi

    if confirm "Remove $RUNTIME from this system?" "n"; then
        _uninstall_ok=false

        case "$OS_FAMILY" in
            macos)
                if [[ "$RUNTIME" == "podman" ]]; then
                    brew uninstall podman 2>/dev/null && _uninstall_ok=true || true
                elif [[ "$INSTALLED_COLIMA" == "true" ]]; then
                    brew uninstall colima docker docker-compose 2>/dev/null && _uninstall_ok=true || true
                fi
                ;;
            rhel)
                if [[ "$RUNTIME" == "docker" ]]; then
                    sudo dnf remove -y docker-ce docker-ce-cli containerd.io \
                        docker-compose-plugin 2>/dev/null && _uninstall_ok=true || true
                    sudo rm -f /etc/yum.repos.d/docker-ce.repo 2>/dev/null || true
                else
                    sudo dnf remove -y podman podman-compose 2>/dev/null && _uninstall_ok=true || true
                fi
                ;;
            debian)
                if [[ "$RUNTIME" == "docker" ]]; then
                    sudo apt-get purge -y docker-ce docker-ce-cli containerd.io \
                        docker-compose-plugin 2>/dev/null && _uninstall_ok=true || true
                    sudo rm -f /etc/apt/sources.list.d/docker.list \
                               /etc/apt/keyrings/docker.gpg 2>/dev/null || true
                else
                    sudo apt-get purge -y podman podman-compose 2>/dev/null && _uninstall_ok=true || true
                fi
                ;;
            arch)
                sudo pacman -Rns --noconfirm "$RUNTIME" 2>/dev/null && _uninstall_ok=true || true
                ;;
            *)
                warn "Cannot determine package manager for removing $RUNTIME."
                ;;
        esac

        if $_uninstall_ok; then
            ok "$RUNTIME removed."
        else
            warn "Could not remove $RUNTIME automatically. Remove it manually using your package manager."
        fi
    else
        info "$RUNTIME left installed."
    fi
fi

# ── Clean up state files ──────────────────────────────────────────────────────
rm -f "$HOME/.jobsquire-install.state" 2>/dev/null || true

# ── Done ──────────────────────────────────────────────────────────────────────
echo
echo -e "${GREEN}${BOLD}Uninstall complete.${RESET}"
echo
