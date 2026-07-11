#!/bin/sh
# Job Squire — bootstrap (macOS, Linux)
#
# The one command that lands the `job-squire` CLI and hands off to it. This
# script installs the CLI and nothing else — every step after this is a
# `job-squire` subcommand (see docs/PLAN-deployment-modes.md Section 6).
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/dellipse/job-squire/main/bootstrap.sh | sh
#
# Pin a specific version instead of the latest release:
#   JOBSQUIRE_VERSION=0.6.0 curl -fsSL https://raw.githubusercontent.com/dellipse/job-squire/main/bootstrap.sh | sh
#
# This script is piped to `sh`, not `bash` — it must run correctly under
# dash/ash/POSIX sh, not just bash. Avoid bashisms (`[[`, arrays, `set -o
# pipefail`) in any change here.
#
# Integrity: the requested version is resolved through the GitHub Releases
# API (never a bare branch), then that release's tag is resolved to an
# immutable commit SHA with `git ls-remote` before anything is installed.
# The CLI is installed with `pip install ... @ git+https://...@<sha>`, so
# pip/git fetch exactly that commit — git's object store is content-addressed
# (every tree/blob/commit is verified against its own hash as part of the
# clone), and the fetch itself runs over HTTPS/TLS. No separate checksum or
# signature file is published for the CLI today (unlike the Docker image,
# which is cosign-signed in .github/workflows/ci.yml) — pinning to the
# resolved commit SHA rather than the mutable tag name is the integrity
# mechanism here: what gets installed cannot silently change after the
# version-resolution step above, even if the tag is later moved.

set -eu

REPO="dellipse/job-squire"
GIT_URL="https://github.com/${REPO}.git"
API="https://api.github.com/repos/${REPO}"
INSTALL_DIR="${JOBSQUIRE_INSTALL_DIR:-$HOME/.job-squire}"
VENV_DIR="$INSTALL_DIR/cli"
BIN_DIR="$VENV_DIR/bin"

if [ -t 1 ]; then
  RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; BOLD='\033[1m'; RESET='\033[0m'
else
  RED=''; GREEN=''; YELLOW=''; BLUE=''; BOLD=''; RESET=''
fi
info() { printf "%b\n" "${BLUE}->${RESET} $*"; }
ok()   { printf "%b\n" "${GREEN}OK${RESET} $*"; }
warn() { printf "%b\n" "${YELLOW}!${RESET} $*"; }
die()  { printf "%b\n" "${RED}x${RESET} $*" >&2; exit 1; }

# ── Prerequisites ─────────────────────────────────────────────────────────
# Deliberately not auto-installed: the bootstrap installs the CLI and
# nothing else (see the file header). Runtime (Podman/Docker) install-with-
# consent is the CLI's own job (Prompt C3), not this script's.
require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "'$1' is required but wasn't found on PATH. Install it and re-run this command."
}
require_cmd curl
require_cmd git
require_cmd python3
require_cmd mktemp
require_cmd awk
require_cmd grep
require_cmd sed

PY_OK=$(python3 -c 'import sys; print(1 if sys.version_info >= (3, 11) else 0)')
if [ "$PY_OK" != "1" ]; then
  die "Python 3.11+ is required (found: $(python3 --version 2>&1)). Install a newer Python 3 and re-run this command."
fi

# ── GitHub API helper ────────────────────────────────────────────────────
# Sets API_STATUS (HTTP status code) and API_BODY_FILE (path to the response
# body) as globals. Not run inside a command substitution, so the globals it
# sets are visible to the caller.
api_get() {
  API_BODY_FILE=$(mktemp)
  API_STATUS=$(curl -sS --max-time 10 \
    -H "Accept: application/vnd.github+json" \
    -H "User-Agent: job-squire-bootstrap" \
    ${GITHUB_TOKEN:+-H "Authorization: Bearer ${GITHUB_TOKEN}"} \
    -o "$API_BODY_FILE" -w '%{http_code}' "$1" 2>/dev/null || echo "000")
}

json_field() {
  # json_field <file> <field> -- prints a top-level string field, or "".
  python3 -c "
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
print(d.get(sys.argv[2], '') if isinstance(d, dict) else '')
" "$1" "$2"
}

first_release_tag_and_prerelease() {
  # first_release_tag_and_prerelease <file> -- for a /releases list response,
  # prints "<tag_name>\n<1-or-0>" for the most recently published release,
  # or two empty lines if the list is empty/unparseable.
  python3 -c "
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    d = []
if d:
    print(d[0].get('tag_name', ''))
    print('1' if d[0].get('prerelease') else '0')
else:
    print('')
    print('0')
" "$1"
}

# ── Resolve the requested version to a release tag ──────────────────────
if [ -n "${JOBSQUIRE_VERSION:-}" ]; then
  ver=${JOBSQUIRE_VERSION#v}
  tag="v${ver}"
  info "Looking up release ${tag} ..."
  api_get "$API/releases/tags/$tag"
  if [ "$API_STATUS" = "200" ]; then
    : # found
  elif [ "$API_STATUS" = "404" ]; then
    rm -f "$API_BODY_FILE"
    die "JOBSQUIRE_VERSION=$JOBSQUIRE_VERSION does not match a published release (looked for tag '$tag'). See https://github.com/${REPO}/releases for available versions. Nothing was installed."
  else
    rm -f "$API_BODY_FILE"
    die "Could not reach the GitHub releases API to verify JOBSQUIRE_VERSION=$JOBSQUIRE_VERSION (HTTP $API_STATUS). Check your network connection and try again. Nothing was installed."
  fi
  rm -f "$API_BODY_FILE"
else
  info "Looking up the latest job-squire release ..."
  api_get "$API/releases/latest"
  if [ "$API_STATUS" = "200" ]; then
    tag=$(json_field "$API_BODY_FILE" tag_name)
    rm -f "$API_BODY_FILE"
  elif [ "$API_STATUS" = "404" ]; then
    # /releases/latest only ever returns a non-prerelease, non-draft release,
    # and can 404 even when releases exist (e.g. everything so far is a
    # pre-release, as during this project's early phase). Fall back to the
    # most recently published release of any kind rather than leaving the
    # default path with nothing to install.
    rm -f "$API_BODY_FILE"
    api_get "$API/releases"
    [ "$API_STATUS" = "200" ] || { rm -f "$API_BODY_FILE"; die "Could not reach the GitHub releases API (HTTP $API_STATUS). Check your network connection and try again. Nothing was installed."; }
    release_info=$(first_release_tag_and_prerelease "$API_BODY_FILE")
    rm -f "$API_BODY_FILE"
    tag=$(printf '%s\n' "$release_info" | sed -n '1p')
    is_pre=$(printf '%s\n' "$release_info" | sed -n '2p')
    [ -n "$tag" ] || die "No releases have been published yet at https://github.com/${REPO}/releases. Nothing to install — try again later, or pin a version with JOBSQUIRE_VERSION once one exists."
    [ "$is_pre" = "1" ] && warn "Latest published release (${tag}) is a pre-release; installing it since no stable release exists yet."
  else
    rm -f "$API_BODY_FILE"
    die "Could not reach the GitHub releases API (HTTP $API_STATUS). Check your network connection and try again. Nothing was installed."
  fi
  [ -n "$tag" ] || die "GitHub returned a release with no tag_name — this shouldn't happen. See https://github.com/${REPO}/releases."
fi

ok "Target version: ${tag}"

# ── Pin the tag to an immutable commit before installing anything ───────
info "Resolving ${tag} to a commit ..."
sha=$(git ls-remote "$GIT_URL" "refs/tags/${tag}" "refs/tags/${tag}^{}" 2>/dev/null | awk '{print $1}' | tail -n1)
[ -n "$sha" ] || die "Could not resolve tag '${tag}' to a commit via 'git ls-remote'. Nothing was installed."
ok "Pinned to commit ${sha}"

# ── Install into an isolated environment ─────────────────────────────────
# A dedicated venv rather than a bare `pip install --user` sidesteps PEP 668
# "externally managed environment" failures on distros that lock down the
# system Python, and keeps the CLI's dependencies from ever colliding with
# anything else on the machine. Safe to re-run: reuses the venv if present.
if [ ! -x "$BIN_DIR/python" ]; then
  info "Creating an isolated environment at ${VENV_DIR} ..."
  python3 -m venv "$VENV_DIR"
fi
"$BIN_DIR/pip" install --quiet --upgrade pip
info "Installing job-squire-cli (${tag}) ..."
"$BIN_DIR/pip" install --quiet --upgrade \
  "job-squire-cli[query] @ git+${GIT_URL}@${sha}#subdirectory=job_squire_cli"
ok "Installed to ${BIN_DIR}"

# ── Put job-squire on PATH for future shells ─────────────────────────────
path_line="export PATH=\"${BIN_DIR}:\$PATH\"  # added by job-squire bootstrap"
add_path_line() {
  rcfile="$1"
  [ -f "$rcfile" ] || return 0
  grep -qF "$BIN_DIR" "$rcfile" 2>/dev/null && return 0
  printf '\n%s\n' "$path_line" >> "$rcfile"
  ok "Added ${BIN_DIR} to PATH in ${rcfile}"
}
rcfile_for_shell=""
case "$(basename "${SHELL:-}")" in
  zsh)  rcfile_for_shell="$HOME/.zshrc" ;;
  bash) rcfile_for_shell="$HOME/.bashrc" ;;
esac
[ -n "$rcfile_for_shell" ] && add_path_line "$rcfile_for_shell"
add_path_line "$HOME/.profile"
PATH="$BIN_DIR:$PATH"
export PATH

# ── Launch ─────────────────────────────────────────────────────────────
# Stdin here is the curl pipe, not the terminal, even in an interactive
# session — so an exec'd interactive command must be reconnected to
# /dev/tty explicitly, or it would read EOF immediately.
if [ -t 1 ] && [ -r /dev/tty ]; then
  info "Launching job-squire ..."
  exec "$BIN_DIR/job-squire" create < /dev/tty
else
  ok "job-squire installed at ${BIN_DIR}/job-squire"
  echo
  echo "Open a new terminal (so PATH picks up job-squire), then run:"
  echo "    job-squire create"
fi
