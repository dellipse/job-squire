#!/usr/bin/env bash
# Job Squire — interactive install script
#
# Usage (download-only — no git clone required):
#   curl -fsSL https://raw.githubusercontent.com/dellipse/job-squire/main/install.sh -o install.sh
#   bash install.sh
#
# Usage (from a cloned repo):
#   bash install.sh
#
# Note: Download the script before running rather than piping curl directly to
# bash — the script needs an interactive terminal for password prompts.
#
# Private registry auth:
#   If ghcr.io requires authentication, save a GitHub PAT with read:packages
#   scope to data/.ghcr_token (chmod 600) before running. The script will use
#   it automatically. If no token file is present, an unauthenticated pull is
#   attempted.
#
# Supports:
#   Linux  — Docker or Podman; auto-installs via dnf/apt/pacman if neither is present
#   macOS  — Podman, Colima (Docker CE), or OrbStack; installed via Homebrew (no Desktop apps needed)
#
# To undo everything this script does, run: bash uninstall.sh

set -euo pipefail

# ── Colors / output helpers ──────────────────────────────────────────────────
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

# Redirect reads through /dev/tty so they work even if stdin was redirected.
read_tty() { read "$@" </dev/tty; }

ask() {
    # ask VAR "Prompt text" [default]
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

ask_secret() {
    # ask_secret VAR "Prompt text"
    local __var="$1" __msg="$2"
    read_tty -r -s -p "$(echo -e "${BOLD}${__msg}${RESET}: ")" __reply || true
    echo
    printf -v "$__var" '%s' "$__reply"
}

confirm() {
    # confirm "Prompt" [y|n default=y]  → 0 if yes, 1 if no
    local __msg="$1" __default="${2:-y}" __hint __reply
    [[ "$__default" == "y" ]] && __hint="[Y/n]" || __hint="[y/N]"
    read_tty -r -p "$(echo -e "${BOLD}${__msg}${RESET} ${__hint} ")" __reply || true
    __reply="${__reply:-$__default}"
    [[ "$__reply" =~ ^[Yy] ]]
}

# ── State tracking ────────────────────────────────────────────────────────────
# These variables record every change this script makes so that uninstall.sh
# can reverse them. The state file is written to $HOME after each major step.
INSTALL_DATE=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
RUNTIME=""
COMPOSE_CMD=""
INSTALL_DIR=""
INSTALLED_RUNTIME=false
ADDED_TO_DOCKER_GROUP=false
INSTALLED_COLIMA=false         # macOS only: installed Colima for Docker
INSTALLED_ORBSTACK=false       # macOS only: installed OrbStack for Docker
PODMAN_MACHINE_INIT=false      # macOS only: ran podman machine init/start
ENABLED_PODMAN_SOCKET=false    # Linux only
ENABLED_LINGERING=false        # Linux only
CREATED_INSTALL_DIR=false
VERSION_CHECK=true             # whether update.sh checks for new releases
COMPOSE_FILE="docker-compose.yml"  # which compose file is in use
NETWORK_MODE="standalone"          # standalone | swag
SWAG_NETWORK=""                    # external network name (swag mode only)
CLONED_INSTALL=false               # true when running from inside a cloned repo

write_state() {
    local state_home="$HOME/.jobsquire-install.state"
    cat > "$state_home" << EOF
# Job Squire install state — written by install.sh, read by uninstall.sh and update.sh
# Do not edit manually.
INSTALL_DATE=${INSTALL_DATE}
INSTALL_DIR=${INSTALL_DIR:-}
RUNTIME=${RUNTIME:-}
COMPOSE_CMD=${COMPOSE_CMD:-}
INSTALLED_RUNTIME=${INSTALLED_RUNTIME}
ADDED_TO_DOCKER_GROUP=${ADDED_TO_DOCKER_GROUP}
INSTALLED_COLIMA=${INSTALLED_COLIMA}
INSTALLED_ORBSTACK=${INSTALLED_ORBSTACK}
PODMAN_MACHINE_INIT=${PODMAN_MACHINE_INIT}
ENABLED_PODMAN_SOCKET=${ENABLED_PODMAN_SOCKET}
ENABLED_LINGERING=${ENABLED_LINGERING}
CREATED_INSTALL_DIR=${CREATED_INSTALL_DIR}
VERSION_CHECK=${VERSION_CHECK}
COMPOSE_FILE=${COMPOSE_FILE:-docker-compose.yml}
NETWORK_MODE=${NETWORK_MODE:-standalone}
SWAG_NETWORK=${SWAG_NETWORK:-}
CLONED_INSTALL=${CLONED_INSTALL}
EOF
    # Mirror into the install directory once it exists.
    if [[ -n "${INSTALL_DIR:-}" && -d "${INSTALL_DIR}" ]]; then
        cp "$state_home" "${INSTALL_DIR}/.install.state"
    fi
}

# ── Port availability helpers ─────────────────────────────────────────────────

port_in_use() {
    local port=$1
    if command -v ss &>/dev/null; then
        ss -tln 2>/dev/null | grep -q ":${port} " \
            || ss -tln 2>/dev/null | grep -q ":${port}$"
    elif command -v netstat &>/dev/null; then
        netstat -an 2>/dev/null | grep -E "(LISTEN|LISTENING)" \
            | grep -qE "[.:]${port}[[:space:]]"
    elif command -v python3 &>/dev/null; then
        ! python3 -c "
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.bind(('', $port))
    s.close()
except OSError:
    raise SystemExit(1)
" 2>/dev/null
    else
        return 1  # can't determine — assume free
    fi
}

find_free_port() {
    local port=$1
    local tries=0
    while port_in_use "$port"; do
        port=$((port + 10))
        tries=$((tries + 1))
        (( tries >= 20 )) && { warn "Could not find a free port after 20 tries; using ${port}."; break; }
    done
    echo "$port"
}

# ── OS detection ──────────────────────────────────────────────────────────────
OS_FAMILY="unknown"   # rhel | debian | arch | macos | unknown
OS_ID="unknown"       # fedora, ubuntu, debian, arch, macos, etc.

detect_os_family() {
    if [[ "$(uname -s)" == "Darwin" ]]; then
        OS_FAMILY="macos"; OS_ID="macos"; return
    fi
    [[ -f /etc/os-release ]] || return
    local _id _id_like
    _id=$(grep -m1 "^ID=" /etc/os-release | cut -d= -f2- | tr -d '"' | tr '[:upper:]' '[:lower:]')
    _id_like=$(grep -m1 "^ID_LIKE=" /etc/os-release 2>/dev/null | cut -d= -f2- | tr -d '"' | tr '[:upper:]' '[:lower:]' || echo "")
    OS_ID="${_id:-unknown}"
    local _combo="${_id:-} ${_id_like:-}"
    case "$_combo" in
        *fedora*|*rhel*|*centos*|*rocky*|*alma*|*oracle*) OS_FAMILY="rhel" ;;
        *debian*|*ubuntu*|*mint*)                          OS_FAMILY="debian" ;;
        *arch*|*manjaro*|*endeavour*)                      OS_FAMILY="arch" ;;
    esac
}

# ── Runtime install functions ─────────────────────────────────────────────────
# Each function prints the exact commands before asking permission.
# On success it sets INSTALLED_RUNTIME=true and calls write_state.

_check_and_install_podman_compose() {
    # Tries to install compose support when podman is present but compose is not.
    if podman compose version &>/dev/null 2>&1; then
        ok "Podman compose built-in support confirmed."
        return 0
    fi
    echo
    info "This Podman version does not have built-in compose support."
    info "Installing podman-compose..."
    case "$OS_FAMILY" in
        rhel)
            echo "  Will run: sudo dnf install -y podman-compose"
            if confirm "Install podman-compose?"; then
                sudo dnf install -y podman-compose 2>/dev/null || {
                    warn "Not found in dnf repos — trying pip3..."
                    pip3 install --user podman-compose \
                        || die "Could not install podman-compose. Run: pip3 install podman-compose"
                }
            else
                die "A compose tool is required. Install manually: sudo dnf install podman-compose"
            fi
            ;;
        debian)
            echo "  Will run: sudo apt-get install -y podman-compose"
            if confirm "Install podman-compose?"; then
                sudo apt-get install -y podman-compose 2>/dev/null || {
                    warn "Not found in apt repos — trying pip3..."
                    pip3 install --user podman-compose \
                        || die "Could not install podman-compose. Run: pip3 install podman-compose"
                }
            else
                die "A compose tool is required. Install manually: sudo apt install podman-compose"
            fi
            ;;
        arch)
            echo "  Will run: sudo pacman -S --noconfirm python-podman-compose"
            if confirm "Install python-podman-compose?"; then
                sudo pacman -S --noconfirm python-podman-compose 2>/dev/null \
                    || pip3 install --user podman-compose \
                    || die "Could not install podman-compose."
            else
                die "A compose tool is required."
            fi
            ;;
        macos)
            echo "  Will run: brew install podman-compose"
            if confirm "Install podman-compose?"; then
                brew install podman-compose \
                    || pip3 install --user podman-compose \
                    || die "Could not install podman-compose."
            else
                die "A compose tool is required."
            fi
            ;;
        *)
            die "No compose tool found. Install manually: pip3 install podman-compose"
            ;;
    esac
    ok "podman-compose installed."
}

# ── Linux install functions ───────────────────────────────────────────────────

install_podman_rhel() {
    echo
    echo "Podman is the native container runtime on Fedora/RHEL systems."
    echo "It is maintained by Red Hat and available from your system's package manager."
    echo
    echo "The following commands will run:"
    echo
    echo "  sudo dnf install -y podman"
    echo
    warn "These commands require sudo (administrator) access."
    echo
    confirm "Proceed with installing Podman?" \
        || die "Cancelled. Install Podman manually and re-run this script."
    echo
    info "Running: sudo dnf install -y podman"
    sudo dnf install -y podman \
        || die "podman install failed. See: https://podman.io/docs/installation"
    ok "Podman installed."
    INSTALLED_RUNTIME=true
    write_state
}

install_docker_rhel() {
    local _docker_repo_os="centos"
    [[ "$OS_ID" == "fedora" ]] && _docker_repo_os="fedora"
    local _repo_url="https://download.docker.com/linux/${_docker_repo_os}/docker-ce.repo"
    echo
    echo "Docker will be installed from Docker's official package repository."
    echo
    echo "The following commands will run:"
    echo
    echo "  sudo dnf install -y dnf-plugins-core"
    echo "  sudo dnf config-manager --add-repo \\"
    echo "      ${_repo_url}"
    echo "  sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin"
    echo "  sudo systemctl enable --now docker"
    echo "  sudo usermod -aG docker $USER"
    echo
    warn "These commands require sudo (administrator) access."
    echo
    confirm "Proceed with installing Docker?" \
        || die "Cancelled. Install Docker manually and re-run this script."
    echo
    info "Installing dnf-plugins-core..."
    sudo dnf install -y dnf-plugins-core
    info "Adding Docker's package repository..."
    sudo dnf config-manager --add-repo "$_repo_url"
    info "Installing Docker CE (this may take a minute)..."
    sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin \
        || die "Docker install failed. See: https://docs.docker.com/engine/install/fedora/"
    info "Enabling Docker service..."
    sudo systemctl enable --now docker
    if ! groups "$USER" | grep -q '\bdocker\b'; then
        info "Adding $USER to the 'docker' group..."
        sudo usermod -aG docker "$USER"
        ADDED_TO_DOCKER_GROUP=true
        echo
        warn "Added $USER to the 'docker' group."
        warn "This takes full effect on your next login."
        warn "For this install session the script will use 'sudo docker compose'."
        echo
    fi
    ok "Docker installed."
    INSTALLED_RUNTIME=true
    write_state
}

install_podman_debian() {
    echo
    if [[ "$OS_ID" == "ubuntu" ]]; then
        local _ver
        _ver=$(grep -m1 "^VERSION_ID=" /etc/os-release 2>/dev/null \
            | cut -d= -f2 | tr -d '"' | cut -d. -f1 || echo "99")
        if (( _ver < 22 )); then
            warn "Ubuntu ${_ver} ships Podman v3.x in its default apt repos."
            warn "Podman v4+ is recommended for built-in compose support."
            warn "See https://podman.io/docs/installation#ubuntu for updated packages."
            warn "Alternatively, Docker is typically the simpler choice on Ubuntu ${_ver}."
            echo
            confirm "Continue with the apt version of Podman anyway?" "n" \
                || die "Cancelled."
        fi
    fi
    echo "Podman will be installed from your system's apt repositories."
    echo
    echo "The following commands will run:"
    echo
    echo "  sudo apt-get update"
    echo "  sudo apt-get install -y podman"
    echo
    warn "These commands require sudo (administrator) access."
    echo
    confirm "Proceed with installing Podman?" \
        || die "Cancelled. Install Podman manually and re-run this script."
    echo
    info "Running: sudo apt-get update"
    sudo apt-get update -q || die "apt-get update failed."
    info "Running: sudo apt-get install -y podman"
    sudo apt-get install -y podman \
        || die "podman install failed. See: https://podman.io/docs/installation"
    ok "Podman installed."
    INSTALLED_RUNTIME=true
    write_state
}

install_docker_debian() {
    echo
    echo "Docker will be installed using Docker's official install script."
    echo
    echo "What the script does:"
    echo "  - Adds Docker's GPG key and apt repository to your system"
    echo "  - Installs docker-ce, docker-ce-cli, containerd.io, and docker-compose-plugin"
    echo "  - Enables and starts the Docker service"
    echo
    echo "After installing, this script will add $USER to the 'docker' group"
    echo "so Docker commands work without sudo."
    echo
    echo "Script source: https://get.docker.com"
    echo "Review it first: curl -fsSL https://get.docker.com | less"
    echo
    warn "Installation requires sudo (administrator) access."
    echo
    confirm "Proceed with installing Docker?" \
        || die "Cancelled. Install Docker manually and re-run this script."
    echo
    info "Downloading Docker install script from https://get.docker.com ..."
    curl -fsSL https://get.docker.com -o /tmp/install-docker.sh \
        || die "Download failed. Check your network connection and try again."
    info "Running Docker install script (this may take a minute)..."
    sudo sh /tmp/install-docker.sh \
        || die "Docker installation failed. See: https://docs.docker.com/engine/install/ubuntu/"
    rm -f /tmp/install-docker.sh
    ok "Docker installed."
    if ! groups "$USER" | grep -q '\bdocker\b'; then
        info "Adding $USER to the 'docker' group..."
        sudo usermod -aG docker "$USER"
        ADDED_TO_DOCKER_GROUP=true
        echo
        warn "Added $USER to the 'docker' group."
        warn "This takes full effect on your next login."
        warn "For this install session the script will use 'sudo docker compose'."
        echo
    fi
    INSTALLED_RUNTIME=true
    write_state
}

install_podman_arch() {
    echo
    echo "Podman will be installed from the Arch Linux package repositories."
    echo
    echo "The following commands will run:"
    echo
    echo "  sudo pacman -S --noconfirm podman"
    echo
    warn "These commands require sudo (administrator) access."
    echo
    confirm "Proceed with installing Podman?" \
        || die "Cancelled."
    echo
    info "Running: sudo pacman -S --noconfirm podman"
    sudo pacman -S --noconfirm podman \
        || die "pacman install podman failed."
    ok "Podman installed."
    INSTALLED_RUNTIME=true
    write_state
}

install_docker_arch() {
    echo
    echo "Docker will be installed from the Arch Linux package repositories."
    echo
    echo "The following commands will run:"
    echo
    echo "  sudo pacman -S --noconfirm docker"
    echo "  sudo systemctl enable --now docker"
    echo "  sudo usermod -aG docker $USER"
    echo
    warn "These commands require sudo (administrator) access."
    echo
    confirm "Proceed with installing Docker?" \
        || die "Cancelled."
    echo
    info "Running: sudo pacman -S --noconfirm docker"
    sudo pacman -S --noconfirm docker \
        || die "pacman install docker failed."
    info "Enabling Docker service..."
    sudo systemctl enable --now docker
    if ! groups "$USER" | grep -q '\bdocker\b'; then
        info "Adding $USER to the 'docker' group..."
        sudo usermod -aG docker "$USER"
        ADDED_TO_DOCKER_GROUP=true
        echo
        warn "Added $USER to the 'docker' group. Takes effect on next login."
        echo
    fi
    ok "Docker installed."
    INSTALLED_RUNTIME=true
    write_state
}

# ── macOS install functions ───────────────────────────────────────────────────

install_podman_macos() {
    echo
    echo "Podman will be installed via Homebrew."
    echo
    echo "On macOS, Podman requires a lightweight Linux VM to host containers."
    echo "The 'podman machine' command manages that VM — this script will"
    echo "initialize and start it for you."
    echo
    echo "The following commands will run:"
    echo
    echo "  brew install podman"
    echo "  podman machine init"
    echo "  podman machine start"
    echo
    confirm "Proceed?" \
        || die "Cancelled."
    echo
    info "Running: brew install podman"
    brew install podman \
        || die "brew install podman failed."
    info "Running: podman machine init"
    podman machine init \
        || die "podman machine init failed."
    info "Running: podman machine start"
    podman machine start \
        || die "podman machine start failed."
    ok "Podman installed and machine started."
    INSTALLED_RUNTIME=true
    PODMAN_MACHINE_INIT=true
    write_state
}

install_docker_macos_colima() {
    echo
    echo "Docker will be installed via Colima — a lightweight macOS container runtime"
    echo "that uses a small Linux VM. No Docker Desktop or root daemon required."
    echo
    echo "The following commands will run:"
    echo
    echo "  brew install colima docker docker-compose"
    echo "  colima start"
    echo
    confirm "Proceed?" \
        || die "Cancelled."
    echo
    info "Running: brew install colima docker docker-compose"
    brew install colima docker docker-compose \
        || die "brew install failed."
    info "Running: colima start (starts the container runtime VM)..."
    colima start \
        || die "colima start failed. Try running 'colima start' manually after this script finishes."
    ok "Colima started. Docker is available."
    INSTALLED_RUNTIME=true
    INSTALLED_COLIMA=true
    write_state
}

install_docker_macos_orbstack() {
    echo
    echo "Docker will be provided by OrbStack — a fast, lightweight Docker Desktop"
    echo "alternative built for macOS. It is a drop-in replacement: the standard"
    echo "'docker' and 'docker compose' commands work unchanged. Requires macOS 14+."
    echo
    echo "The following commands will run:"
    echo
    echo "  brew install --cask orbstack"
    echo "  open -a OrbStack        # launches OrbStack so it installs the docker CLI"
    echo
    echo "OrbStack is free for personal use; commercial use in larger organizations"
    echo "requires a paid license."
    echo
    confirm "Proceed?" \
        || die "Cancelled."
    echo
    info "Running: brew install --cask orbstack"
    brew install --cask orbstack \
        || die "brew install --cask orbstack failed."
    info "Launching OrbStack to set up the docker CLI and start the engine..."
    open -a OrbStack \
        || die "Could not launch OrbStack. Open it once from Applications, then re-run this script."

    # Wait for the Docker socket to come up (OrbStack starts in a few seconds).
    info "Waiting for the Docker engine to become ready..."
    local _tries=0
    until docker info &>/dev/null; do
        _tries=$((_tries + 1))
        if [[ $_tries -ge 60 ]]; then
            die "Docker engine did not become ready. Open OrbStack from Applications, wait for it to finish starting, then re-run this script."
        fi
        sleep 2
    done
    ok "OrbStack started. Docker is available."
    INSTALLED_RUNTIME=true
    INSTALLED_ORBSTACK=true
    write_state
}

# ── Banner ────────────────────────────────────────────────────────────────────
echo
echo -e "${BOLD}  Job Squire — self-hosted AI job-hunt Job Squire${RESET}"
echo    "  https://github.com/dellipse/job-squire"
echo

detect_os_family

# ── Step 1: Container runtime ─────────────────────────────────────────────────
section "Step 1: Container runtime"

have_docker=false; have_podman=false
command -v docker &>/dev/null && have_docker=true
command -v podman &>/dev/null && have_podman=true

if $have_docker && $have_podman; then
    echo "Both Docker and Podman are installed."
    echo "  1) Docker  — widely used; requires a daemon; most popular on Debian/Ubuntu"
    echo "  2) Podman  — rootless by default; better security; native on Fedora/RHEL"
    echo
    echo "  Not sure? See docs/install/docker-vs-podman.md for guidance."
    echo
    ask rt_choice "Which runtime?" "1"
    [[ "$rt_choice" == "2" ]] && RUNTIME=podman || RUNTIME=docker

elif $have_docker; then
    ok "Docker found."
    RUNTIME=docker

elif $have_podman; then
    ok "Podman found."
    RUNTIME=podman

else
    # ── Neither runtime is installed ─────────────────────────────────────────
    echo "Neither Docker nor Podman was found on this system."
    echo

    case "$OS_FAMILY" in

        macos)
            echo "Detected: macOS"
            echo
            if command -v brew &>/dev/null; then
                ok "Homebrew detected."
                echo
                echo "  1) Podman  (recommended) — rootless, daemonless, CLI-only"
                echo "     Installs via brew; uses a small Linux VM (podman machine)"
                echo
                echo "  2) Docker via Colima — lightweight CLI alternative to Docker Desktop"
                echo "     Installs via brew; uses a small Linux VM (Colima)"
                echo
                echo "  3) Docker via OrbStack — fastest, lightest Docker Desktop alternative"
                echo "     Installs via brew cask; native app, best fit on Apple Silicon (macOS 14+)"
                echo
                echo "  All three are fully supported. No Desktop application required for 1 or 2."
                echo "  Not sure? See docs/install/docker-vs-podman.md"
                echo
                ask rt_choice "Which would you like to install?" "1"
                echo
                if [[ "$rt_choice" == "2" ]]; then
                    install_docker_macos_colima
                    RUNTIME=docker
                elif [[ "$rt_choice" == "3" ]]; then
                    install_docker_macos_orbstack
                    RUNTIME=docker
                else
                    install_podman_macos
                    RUNTIME=podman
                fi
            else
                warn "Homebrew is not installed."
                echo
                echo "Homebrew is the recommended way to install container runtimes on macOS."
                echo
                echo "Install Homebrew first, then re-run this script:"
                echo
                echo "  /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
                echo
                echo "Or install a runtime manually without Homebrew:"
                echo "  Podman:    https://podman.io/docs/installation#macos"
                echo "             podman machine init && podman machine start"
                echo "  Colima:    https://github.com/abiosoft/colima"
                echo "  OrbStack:  https://orbstack.dev/download  (launch it once after installing)"
                echo
                exit 1
            fi
            ;;

        rhel)
            echo "Detected: Fedora / RHEL / Rocky / AlmaLinux"
            echo
            echo "Podman is the recommended runtime for this distribution."
            echo "It is native to Fedora/RHEL, daemonless, and runs containers rootless by default."
            echo
            echo "  1) Podman  (recommended) — rootless, daemonless, native to this distro"
            echo "  2) Docker  — requires adding Docker's external repository"
            echo
            ask rt_choice "Which would you like to install?" "1"
            echo
            if [[ "$rt_choice" == "2" ]]; then
                install_docker_rhel
                RUNTIME=docker
            else
                install_podman_rhel
                RUNTIME=podman
            fi
            ;;

        debian)
            echo "Detected: Debian / Ubuntu"
            echo
            echo "Docker is the recommended runtime for Debian/Ubuntu systems."
            echo "Docker's official install script handles the full setup automatically."
            echo
            echo "  1) Docker  (recommended) — official install script, easiest setup"
            echo "  2) Podman  — rootless and daemonless; more complex on Ubuntu < 22"
            echo
            ask rt_choice "Which would you like to install?" "1"
            echo
            if [[ "$rt_choice" == "2" ]]; then
                install_podman_debian
                RUNTIME=podman
            else
                install_docker_debian
                RUNTIME=docker
            fi
            ;;

        arch)
            echo "Detected: Arch Linux"
            echo
            echo "  1) Podman  (recommended) — rootless, daemonless, in official repos"
            echo "  2) Docker  — available in official repos"
            echo
            ask rt_choice "Which would you like to install?" "1"
            echo
            if [[ "$rt_choice" == "2" ]]; then
                install_docker_arch
                RUNTIME=docker
            else
                install_podman_arch
                RUNTIME=podman
            fi
            ;;

        *)
            echo "Could not determine your distribution's package manager."
            echo
            echo "Install one of the following manually, then re-run this script:"
            echo
            echo "  Podman: https://podman.io/docs/installation"
            echo "  Docker: https://docs.docker.com/engine/install/"
            echo
            exit 1
            ;;
    esac
fi

info "Using runtime: $RUNTIME"

# ── Step 2: Compose command ───────────────────────────────────────────────────
section "Step 2: Compose command"

if [[ "$RUNTIME" == "docker" ]]; then
    if docker compose version &>/dev/null 2>&1; then
        COMPOSE_CMD="docker compose"
        ok "docker compose (v2 plugin) — OK"
    elif command -v docker-compose &>/dev/null; then
        COMPOSE_CMD="docker-compose"
        warn "Found docker-compose v1, which is deprecated. Consider upgrading to the v2 plugin."
        warn "See: https://docs.docker.com/compose/migrate/"
    else
        die "Docker is installed but the Compose plugin is missing.
  Install it: https://docs.docker.com/compose/install/linux/"
    fi

    # If we just added the user to the docker group, the group membership does
    # not apply in this login session — prefix compose commands with sudo.
    if [[ "$ADDED_TO_DOCKER_GROUP" == "true" ]]; then
        COMPOSE_CMD="sudo $COMPOSE_CMD"
        info "Using 'sudo docker compose' for this install session."
        info "After your next login, 'docker compose' will work without sudo."
    fi

else
    # Podman — prefer built-in compose (v4+) over the separate package.
    if podman compose version &>/dev/null 2>&1; then
        COMPOSE_CMD="podman compose"
        ok "podman compose (built-in, Podman v4+) — OK"
    elif command -v podman-compose &>/dev/null; then
        COMPOSE_CMD="podman-compose"
        ok "podman-compose (separate package) — OK"
    else
        _check_and_install_podman_compose
        if podman compose version &>/dev/null 2>&1; then
            COMPOSE_CMD="podman compose"
        else
            COMPOSE_CMD="podman-compose"
        fi
    fi
fi

info "Compose command: $COMPOSE_CMD"
write_state

# ── Step 3: Install location ──────────────────────────────────────────────────
section "Step 3: Install location"

GITHUB_RAW="https://raw.githubusercontent.com/dellipse/job-squire/main"

if [[ -f "$(pwd)/docker-compose.yml" ]]; then
    # Running from inside a cloned repo — use everything that's already here.
    INSTALL_DIR="$(pwd)"
    CLONED_INSTALL=true
    ok "Running from cloned repo: $INSTALL_DIR"
else
    # Download-only install — fetch compose files, no git clone needed.
    ask INSTALL_DIR "Where should Job Squire be installed?" "$HOME/jobsquire"
    INSTALL_DIR="${INSTALL_DIR%/}"
    if [[ ! -d "$INSTALL_DIR" ]]; then
        mkdir -p "$INSTALL_DIR"
        CREATED_INSTALL_DIR=true
    fi

    info "Downloading docker-compose.yml..."
    curl -fsSL "${GITHUB_RAW}/docker-compose.yml" -o "$INSTALL_DIR/docker-compose.yml" \
        || die "Download failed. Check your internet connection and try again."
    ok "docker-compose.yml saved to $INSTALL_DIR/"
fi

DATA_DIR="$INSTALL_DIR/data"
ENV_FILE="$DATA_DIR/.env"

write_state

# ── Step 4: Configuration ─────────────────────────────────────────────────────
section "Step 4: Configuration"

SKIP_ENV=false
INSTANCE_NAME="job-squire"
APP_HOST_PORT="8080"
MCP_HOST_PORT="9000"
DEPLOY_TYPE="local"
PUBLIC_URL=""
PUBLIC_MCP_URL=""
PUBLIC_MCP_HOST=""
SESSION_COOKIE_SECURE="false"

if [[ -f "$ENV_FILE" ]]; then
    warn "An existing configuration was found at $ENV_FILE"
    if ! confirm "Replace it with a fresh configuration?"; then
        info "Keeping existing configuration."
        SKIP_ENV=true
        # Load all relevant vars from the existing config so Step 6 and the Done section work.
        if grep -q '^SWAG_NETWORK=' "$ENV_FILE" 2>/dev/null; then
            NETWORK_MODE="swag"
            COMPOSE_FILE="docker-compose.swag.yml"
            SWAG_NETWORK=$(grep -m1 '^SWAG_NETWORK=' "$ENV_FILE" | cut -d= -f2-)
        fi
        INSTANCE_NAME=$(grep -m1 '^INSTANCE_NAME=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- || echo "job-squire")
        APP_HOST_PORT=$(grep -m1 '^APP_HOST_PORT=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- || echo "8080")
        MCP_HOST_PORT=$(grep -m1 '^MCP_HOST_PORT=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- || echo "9000")
        PUBLIC_URL=$(grep -m1 '^PUBLIC_URL=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- || echo "")
        PUBLIC_MCP_URL=$(grep -m1 '^PUBLIC_MCP_URL=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- || echo "")
        PUBLIC_MCP_HOST=$(grep -m1 '^PUBLIC_MCP_HOST=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- || echo "")
        SESSION_COOKIE_SECURE=$(grep -m1 '^SESSION_COOKIE_SECURE=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- || echo "false")
        [[ -n "$PUBLIC_URL" ]] && DEPLOY_TYPE="production"
    fi
fi

if ! $SKIP_ENV; then
    mkdir -p "$DATA_DIR"
    chmod 750 "$DATA_DIR"

    # Secret key
    if command -v python3 &>/dev/null; then
        SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    elif command -v openssl &>/dev/null; then
        SECRET_KEY=$(openssl rand -hex 32)
    else
        die "python3 or openssl is required to generate a secure key. Install either and re-run."
    fi
    ok "Secret key generated."

    # Admin password
    echo
    info "The 'admin' account manages settings and data. Set a strong password."
    ADMIN_PASSWORD=""
    while true; do
        ask_secret ADMIN_PASSWORD "Admin password"
        [[ -z "$ADMIN_PASSWORD" ]] && { warn "Password cannot be empty."; continue; }
        ask_secret ADMIN_PASSWORD2 "Confirm admin password"
        [[ "$ADMIN_PASSWORD" == "$ADMIN_PASSWORD2" ]] && break
        warn "Passwords do not match — try again."
    done

    # User (job-seeker) password
    echo
    info "The 'user' account is for the job seeker. If this is for yourself, you"
    info "can skip it and use the admin account for everything."
    USER_PASSWORD=""
    if confirm "Set a separate password for the 'user' (job seeker) account?"; then
        while true; do
            ask_secret USER_PASSWORD "Job seeker password"
            [[ -z "$USER_PASSWORD" ]] && { warn "Password cannot be empty."; continue; }
            ask_secret USER_PASSWORD2 "Confirm job seeker password"
            [[ "$USER_PASSWORD" == "$USER_PASSWORD2" ]] && break
            warn "Passwords do not match — try again."
        done
    fi

    # Instance name (container naming — matters when running multiple instances)
    echo
    info "Instance name is used for container names (e.g. 'job-squire', 'alice', 'caleb')."
    info "Use the default unless you are running more than one Job Squire on this machine."
    ask INSTANCE_NAME "Instance name" "job-squire"
    INSTANCE_NAME="${INSTANCE_NAME// /-}"   # spaces → hyphens

    # Network mode
    echo
    echo "Container networking:"
    echo "  1) Standalone  — bind host ports (default 8080 / 9000); works anywhere"
    echo "  2) SWAG        — join an existing Docker network; SWAG handles TLS and routing"
    echo
    echo "  Choose SWAG if linuxserver/swag (or another nginx proxy) is already"
    echo "  running in Docker and you want Job Squire on the same network."
    echo
    ask net_choice "Network mode" "1"
    echo

    if [[ "$net_choice" == "2" ]]; then
        NETWORK_MODE="swag"
        COMPOSE_FILE="docker-compose.swag.yml"
        ask SWAG_NETWORK "Docker network name" "swag"
        info "Tip: run 'docker network ls' to confirm the network name."
        info "SWAG proxy should point to: ${INSTANCE_NAME}:8000 (app) and ${INSTANCE_NAME}-mcp:9000 (MCP)"

        # In a download-only install, fetch the SWAG compose file as well.
        if [[ "$CLONED_INSTALL" == "false" && ! -f "$INSTALL_DIR/docker-compose.swag.yml" ]]; then
            info "Downloading docker-compose.swag.yml..."
            curl -fsSL "${GITHUB_RAW}/docker-compose.swag.yml" \
                -o "$INSTALL_DIR/docker-compose.swag.yml" \
                || die "Download failed. Check your internet connection and try again."
            ok "docker-compose.swag.yml saved to $INSTALL_DIR/"
        fi
    else
        NETWORK_MODE="standalone"
        COMPOSE_FILE="docker-compose.yml"

        # Scan for available ports; increment by 10 until a free one is found.
        info "Scanning host ports..."
        DEFAULT_APP_PORT=$(find_free_port 8080)
        DEFAULT_MCP_PORT=$(find_free_port 9000)
        [[ "$DEFAULT_APP_PORT" != "8080" ]] \
            && warn "Port 8080 is in use — suggesting ${DEFAULT_APP_PORT} for the web app."
        [[ "$DEFAULT_MCP_PORT" != "9000" ]] \
            && warn "Port 9000 is in use — suggesting ${DEFAULT_MCP_PORT} for the MCP server."

        ask APP_HOST_PORT "Web app host port" "$DEFAULT_APP_PORT"
        ask MCP_HOST_PORT "MCP server host port" "$DEFAULT_MCP_PORT"
    fi

    # Deployment type
    echo
    echo "How will you access Job Squire?"
    if [[ "$NETWORK_MODE" == "swag" ]]; then
        echo "  1) Production  — https://yourdomain.com via SWAG (recommended)"
        echo "  2) Local / dev — no TLS; SESSION_COOKIE_SECURE stays false"
    else
        echo "  1) Local only  — http://localhost:${APP_HOST_PORT}, no domain or TLS needed"
        echo "  2) Production  — https://yourdomain.com, requires a domain and reverse proxy"
    fi
    echo
    ask deploy_choice "Deployment type" "1"

    PUBLIC_URL=""
    PUBLIC_MCP_URL=""
    PUBLIC_MCP_HOST=""
    SESSION_COOKIE_SECURE="false"

    # In SWAG mode option 1 = production; in standalone mode option 2 = production.
    _prod_choice=2
    [[ "$NETWORK_MODE" == "swag" ]] && _prod_choice=1

    if [[ "$deploy_choice" == "$_prod_choice" ]]; then
        DEPLOY_TYPE="production"
        SESSION_COOKIE_SECURE="true"
        echo
        ask DOMAIN "Your domain (e.g. example.com)"
        DOMAIN="${DOMAIN// /}"
        ask APP_SUB "App subdomain" "Job Squire"
        ask MCP_SUB "MCP subdomain" "mcp-squire"
        PUBLIC_URL="https://${APP_SUB}.${DOMAIN}"
        PUBLIC_MCP_URL="https://${MCP_SUB}.${DOMAIN}"
        PUBLIC_MCP_HOST="${MCP_SUB}.${DOMAIN}"
        info "App URL:  $PUBLIC_URL"
        info "MCP URL:  $PUBLIC_MCP_URL"
    fi

    PUID=$(id -u)
    PGID=$(id -g)

    {
        echo "# Job Squire configuration"
        echo "# Generated by install.sh on $(date -u '+%Y-%m-%d %H:%M UTC')"
        echo "# Keep this file private — it contains your secret key and passwords."
        echo
        echo "SECRET_KEY=${SECRET_KEY}"
        echo "ADMIN_PASSWORD=${ADMIN_PASSWORD}"
        [[ -n "$USER_PASSWORD" ]] && echo "USER_PASSWORD=${USER_PASSWORD}"
        echo
        echo "INSTANCE_NAME=${INSTANCE_NAME}"
        if [[ "$NETWORK_MODE" == "standalone" ]]; then
            echo "APP_HOST_PORT=${APP_HOST_PORT}"
            echo "MCP_HOST_PORT=${MCP_HOST_PORT}"
        else
            echo "SWAG_NETWORK=${SWAG_NETWORK}"
        fi
        echo
        echo "SESSION_COOKIE_SECURE=${SESSION_COOKIE_SECURE}"
        [[ -n "$PUBLIC_URL" ]]      && echo "PUBLIC_URL=${PUBLIC_URL}"
        [[ -n "$PUBLIC_MCP_URL" ]]  && echo "PUBLIC_MCP_URL=${PUBLIC_MCP_URL}"
        [[ -n "$PUBLIC_MCP_HOST" ]] && echo "PUBLIC_MCP_HOST=${PUBLIC_MCP_HOST}"
        echo
        echo "DATA_HOST_DIR=${DATA_DIR}"
        echo "PUID=${PUID}"
        echo "PGID=${PGID}"
    } > "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    ok "Configuration written to $ENV_FILE"

    # ── nginx proxy confs (SWAG + production only) ────────────────────────────
    if [[ "$NETWORK_MODE" == "swag" && "$DEPLOY_TYPE" == "production" ]]; then
        echo
        info "Generating SWAG nginx proxy confs..."
        _nginx_dir="$INSTALL_DIR/data/nginx"
        _app_conf="${APP_SUB}.subdomain.conf"
        _mcp_conf="${MCP_SUB}.subdomain.conf"
        mkdir -p "$_nginx_dir"

        cat > "$_nginx_dir/${_app_conf}" << NGINXEOF
## Version $(date -u '+%Y-%m-%d')
## Job Squire — ${INSTANCE_NAME} (web app)
## Generated by install.sh. Copy to <swag-config>/nginx/proxy-confs/
## Then reload nginx: docker exec <swag-container> nginx -s reload

server {
    listen 443 ssl;
    listen [::]:443 ssl;

    server_name ${APP_SUB}.*;

    include /config/nginx/ssl.conf;

    client_max_body_size 12m;

    location / {
        include /config/nginx/proxy.conf;
        resolver 127.0.0.11 valid=30s;
        set \$upstream_app ${INSTANCE_NAME};
        set \$upstream_port 8000;
        set \$upstream_proto http;
        proxy_pass \$upstream_proto://\$upstream_app:\$upstream_port;
    }
}
NGINXEOF

        cat > "$_nginx_dir/${_mcp_conf}" << NGINXEOF
## Version $(date -u '+%Y-%m-%d')
## Job Squire — ${INSTANCE_NAME}-mcp (MCP server)
## Generated by install.sh. Copy to <swag-config>/nginx/proxy-confs/
## Then reload nginx: docker exec <swag-container> nginx -s reload
## Add as a Claude custom connector: ${PUBLIC_MCP_URL}

server {
    listen 443 ssl;
    listen [::]:443 ssl;

    server_name ${MCP_SUB}.*;

    include /config/nginx/ssl.conf;

    http2 off;

    client_max_body_size 12m;

    location / {
        include /config/nginx/proxy.conf;
        resolver 127.0.0.11 valid=30s;
        set \$upstream_app ${INSTANCE_NAME}-mcp;
        set \$upstream_port 9000;
        set \$upstream_proto http;
        proxy_pass \$upstream_proto://\$upstream_app:\$upstream_port;
    }
}
NGINXEOF

        ok "Proxy confs written to ${_nginx_dir}/"
        ok "  App:  ${_app_conf}"
        ok "  MCP:  ${_mcp_conf}"
        echo
        info "Copy these to your SWAG proxy-confs directory to activate them."
        info "Leave blank to copy manually after install."
        ask SWAG_CONFS_DIR "SWAG proxy-confs path" ""
        if [[ -n "$SWAG_CONFS_DIR" ]]; then
            if [[ -d "$SWAG_CONFS_DIR" ]]; then
                cp "$_nginx_dir/${_app_conf}" "$SWAG_CONFS_DIR/${_app_conf}"
                cp "$_nginx_dir/${_mcp_conf}" "$SWAG_CONFS_DIR/${_mcp_conf}"
                ok "Copied to $SWAG_CONFS_DIR/"
                ask SWAG_CONTAINER "SWAG container name" "swag"
                if docker exec "$SWAG_CONTAINER" nginx -s reload 2>/dev/null; then
                    ok "nginx reloaded in $SWAG_CONTAINER."
                else
                    warn "Could not reload nginx automatically."
                    warn "Run: docker exec ${SWAG_CONTAINER} nginx -s reload"
                fi
            else
                warn "$SWAG_CONFS_DIR not found — skipping copy."
                info "Copy manually when ready:"
                info "  cp ${_nginx_dir}/${_app_conf} <swag-confs-dir>/"
                info "  cp ${_nginx_dir}/${_mcp_conf} <swag-confs-dir>/"
            fi
        else
            info "Copy manually when ready:"
            info "  cp ${_nginx_dir}/${_app_conf} <swag-confs-dir>/"
            info "  cp ${_nginx_dir}/${_mcp_conf} <swag-confs-dir>/"
        fi
    fi
fi

# ── Step 5: Podman-specific setup (Linux only) ────────────────────────────────
if [[ "$RUNTIME" == "podman" && "$OS_FAMILY" != "macos" ]]; then
    section "Step 5: Podman setup"

    if command -v systemctl &>/dev/null; then
        if systemctl --user is-active podman.socket &>/dev/null 2>&1; then
            ok "Podman user socket is already active."
        else
            info "Enabling podman.socket for your user..."
            info "(Required for containers to restart automatically after reboot.)"
            if systemctl --user enable --now podman.socket 2>/dev/null; then
                ok "podman.socket enabled."
                ENABLED_PODMAN_SOCKET=true
            else
                warn "Could not enable podman.socket automatically."
                warn "Run this manually if containers don't restart after reboot:"
                warn "  systemctl --user enable --now podman.socket"
            fi
        fi

        if loginctl show-user "$USER" 2>/dev/null | grep -q "Linger=yes"; then
            ok "Lingering is already enabled for $USER."
        else
            info "Enabling lingering for $USER..."
            info "(Keeps containers running after you log out.)"
            if sudo loginctl enable-linger "$USER" 2>/dev/null; then
                ok "Lingering enabled."
                ENABLED_LINGERING=true
            else
                warn "Could not enable lingering (requires sudo)."
                warn "Run manually to keep containers alive after logout:"
                warn "  sudo loginctl enable-linger $USER"
            fi
        fi
    else
        warn "systemd not detected — skipping socket and linger setup."
        warn "On non-systemd systems, containers may not restart automatically after reboot."
    fi

    write_state
fi

# ── Step 6: Pull image and start ──────────────────────────────────────────────
section "Step 6: Start Job Squire"

cd "$INSTALL_DIR"

# Authenticate with ghcr.io if a token file is present.
# Save a GitHub PAT with read:packages scope to data/.ghcr_token (chmod 600)
# to enable pulls from a private registry package.
GHCR_USER="dellipse"
GHCR_TOKEN_FILE="${DATA_DIR}/.ghcr_token"

_LOGIN_CMD="$RUNTIME"
[[ "$ADDED_TO_DOCKER_GROUP" == "true" ]] && _LOGIN_CMD="sudo $RUNTIME"

if [[ -f "$GHCR_TOKEN_FILE" ]]; then
    info "Authenticating with ghcr.io (token from $GHCR_TOKEN_FILE)..."
    cat "$GHCR_TOKEN_FILE" | $_LOGIN_CMD login ghcr.io -u "$GHCR_USER" --password-stdin \
        || warn "ghcr.io login failed — attempting unauthenticated pull."
else
    info "No token file found at $GHCR_TOKEN_FILE — attempting unauthenticated pull."
    info "(If the pull fails, save a GitHub PAT with read:packages scope to that path and re-run.)"
fi

info "Pulling Job Squire image from ghcr.io/${GHCR_USER}/job-squire..."
$COMPOSE_CMD -f "$INSTALL_DIR/$COMPOSE_FILE" --env-file "$ENV_FILE" \
    pull job-squire job-squire-worker job-squire-mcp

info "Starting containers..."
$COMPOSE_CMD -f "$INSTALL_DIR/$COMPOSE_FILE" --env-file "$ENV_FILE" \
    up -d --no-deps --force-recreate job-squire job-squire-worker job-squire-mcp

sleep 4

info "Container status:"
$COMPOSE_CMD -f "$INSTALL_DIR/$COMPOSE_FILE" --env-file "$ENV_FILE" ps

# ── Write final state ─────────────────────────────────────────────────────────
write_state

# ── Done ──────────────────────────────────────────────────────────────────────
echo
echo -e "${GREEN}${BOLD}Job Squire is running.${RESET}"
echo

_inst="${INSTANCE_NAME:-job-squire}"

if [[ "$NETWORK_MODE" == "swag" ]]; then
    echo "  Containers joined network: ${SWAG_NETWORK:-swag}"
    echo
    echo -e "${YELLOW}SWAG proxy targets:${RESET}"
    echo "    App:  http://${_inst}:8000"
    echo "    MCP:  http://${_inst}-mcp:9000"
    echo
    if [[ "$DEPLOY_TYPE" == "production" ]]; then
        echo
        echo -e "${YELLOW}nginx proxy confs generated:${RESET}"
        echo "    $INSTALL_DIR/data/nginx/"
        echo "    (Copy to your SWAG proxy-confs directory and reload nginx)"
        echo
        echo -e "  App URL:  ${BOLD}${PUBLIC_URL}${RESET}"
        echo -e "  MCP URL:  ${BOLD}${PUBLIC_MCP_URL}${RESET}"
    else
        echo
        echo "  Add nginx proxy confs pointing to these container names."
        echo "  Sample configs: https://github.com/dellipse/job-squire/tree/main/examples/nginx/"
    fi
elif [[ "$DEPLOY_TYPE" == "production" ]]; then
    echo -e "  App:  ${BOLD}${PUBLIC_URL}${RESET}  (once your reverse proxy is configured)"
    echo -e "  MCP:  ${BOLD}${PUBLIC_MCP_URL}${RESET}"
    echo
    echo -e "${YELLOW}Reverse proxy:${RESET}"
    echo "  Point your proxy (nginx, Caddy, SWAG, Traefik) at:"
    echo "    App container:  http://127.0.0.1:${APP_HOST_PORT:-8080}"
    echo "    MCP container:  http://127.0.0.1:${MCP_HOST_PORT:-9000}"
    echo "  Setup guide: https://github.com/dellipse/job-squire/blob/main/docs/Setup-Guide.md"
else
    echo -e "  App:  ${BOLD}http://localhost:${APP_HOST_PORT:-8080}${RESET}"
fi

echo
echo "  Login:    admin  /  (the password you set)"
echo

if [[ "$ADDED_TO_DOCKER_GROUP" == "true" ]]; then
    echo -e "${YELLOW}Action required:${RESET} Log out and back in so Docker works without sudo."
    echo
fi

# Display non-sudo version of compose command for the help text.
_display_compose="${COMPOSE_CMD#sudo }"
_f_flag="-f ${COMPOSE_FILE} "
[[ "$COMPOSE_FILE" == "docker-compose.yml" ]] && _f_flag=""

echo "Useful commands (run from $INSTALL_DIR):"
echo "  Logs:     ${_display_compose} ${_f_flag}--env-file data/.env logs -f"
echo "  Stop:     ${_display_compose} ${_f_flag}--env-file data/.env down"
if [[ "$CLONED_INSTALL" == "true" ]]; then
    echo "  Update:   git pull && bash install.sh"
else
    echo "  Update:   bash update.sh"
fi
echo "  Uninstall: bash uninstall.sh"
echo
echo "Next: open Settings and configure your search targets, job sources, and AI provider."
echo "Docs: https://github.com/dellipse/job-squire/blob/main/docs/"
echo
