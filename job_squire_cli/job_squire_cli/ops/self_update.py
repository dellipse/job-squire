# Copyright (C) 2026 D. Brandmeyer
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""Self-update: bring the running `job-squire` CLI itself up to date.

`job-squire update` moving a *deployed instance* forward (ops/lifecycle.py's
`update_instance`) is a different operation from moving the CLI binary the
operator is typing commands into forward -- but an operator who hasn't
updated the CLI in a while is also the operator most likely to be missing
a lifecycle fix that a newer CLI needs in order to update an instance
correctly. So `job-squire update` does this first, unconditionally (unless
skipped), before touching any instance.

This deliberately mirrors bootstrap.sh's own install logic instead of
inventing a second mechanism: resolve the requested version (default
latest) to a release tag through the GitHub Releases API, pin that tag to
an immutable commit with `git ls-remote` (integrity: what gets installed
cannot change under us after this step, even if the tag is later moved),
and `pip install --upgrade` a `git+https://...@<sha>` spec at that exact
commit. Using `sys.executable -m pip` rather than guessing an install
path means this works whether the CLI lives in bootstrap.sh's own venv,
a hand-rolled venv, or (its own `-m pip`) inside a pipx-managed one --
whatever interpreter is currently running this code is the one that gets
upgraded.
"""
from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

Runner = Callable[..., "subprocess.CompletedProcess[str]"]
# (status_code, body) -- kept this narrow (rather than passing a full
# urllib response object around) so tests can fake a GitHub API call with
# a plain function instead of standing up an HTTP server or mocking
# urllib internals.
HttpGet = Callable[[str], "tuple[int, bytes]"]

REPO = "dellipse/job-squire"
GIT_URL = f"https://github.com/{REPO}.git"
API_BASE = f"https://api.github.com/repos/{REPO}"
PACKAGE_NAME = "job-squire-cli"
DIST_NAME = "job_squire_cli"


class SelfUpdateError(RuntimeError):
    """Raised for any failure resolving or installing a new CLI version.

    Deliberately a plain RuntimeError, not `ops.lifecycle.LifecycleError`
    -- self-update failing is not an instance-lifecycle failure, and
    `ops/commands.py` treats the two differently (a failed self-update is
    a warning that instance updates still proceed past; a failed instance
    update is fatal for that instance).
    """


@dataclass(frozen=True)
class SelfUpdateResult:
    updated: bool
    previous_version: str
    new_version: str
    tag: str


def _default_http_get(url: str) -> tuple[int, bytes]:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "job-squire-cli"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 -- fixed https host
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except urllib.error.URLError as exc:
        raise SelfUpdateError(
            f"Could not reach the GitHub releases API ({exc.reason}). Check your network "
            f"connection, or pass --skip-self-update to update instances without it."
        ) from exc


def _resolve_tag(version: str | None, *, http_get: HttpGet) -> str:
    """A pinned `version` resolves to `releases/tags/v<version>`; omitted,
    to whatever `releases/latest` returns -- same fallback bootstrap.sh
    uses if that 404s (e.g. every release so far is a pre-release)."""
    if version:
        tag = version if version.startswith("v") else f"v{version}"
        status, body = http_get(f"{API_BASE}/releases/tags/{tag}")
        if status == 404:
            raise SelfUpdateError(
                f"No published release matches version {version!r} (looked for tag {tag!r}). "
                f"See https://github.com/{REPO}/releases for available versions."
            )
        if status != 200:
            raise SelfUpdateError(f"GitHub releases API returned HTTP {status} for tag {tag!r}.")
        return json.loads(body).get("tag_name") or tag

    status, body = http_get(f"{API_BASE}/releases/latest")
    if status == 200:
        data = json.loads(body)
        tag = data.get("tag_name")
        if tag:
            return tag
    elif status != 404:
        raise SelfUpdateError(f"GitHub releases API returned HTTP {status} for the latest release.")

    # /releases/latest 404s if every release published so far is a
    # pre-release (or none exist) -- fall back to the newest release of
    # any kind, same as bootstrap.sh.
    status, body = http_get(f"{API_BASE}/releases")
    if status != 200:
        raise SelfUpdateError(f"GitHub releases API returned HTTP {status} listing releases.")
    releases = json.loads(body)
    if not releases:
        raise SelfUpdateError(f"No releases have been published yet at https://github.com/{REPO}/releases.")
    tag = releases[0].get("tag_name")
    if not tag:
        raise SelfUpdateError("GitHub returned a release with no tag_name -- this shouldn't happen.")
    return tag


def _resolve_commit(tag: str, *, run: Runner) -> str:
    try:
        result = run(
            ["git", "ls-remote", GIT_URL, f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SelfUpdateError(f"Could not run 'git ls-remote' to resolve tag {tag!r}: {exc}") from exc
    lines = [line for line in (result.stdout or "").splitlines() if line.strip()]
    if not lines:
        raise SelfUpdateError(f"Could not resolve tag {tag!r} to a commit via 'git ls-remote'.")
    # Same as bootstrap.sh: take the last matching ref (the ^{} annotated
    # tag entry, if git returned both), which is the commit the tag itself
    # points at rather than the separate tag object's own sha.
    return lines[-1].split()[0]


def _current_short_sha() -> str | None:
    """The commit suffix off the currently installed `<VERSION>+<sha>`
    (PEP 440 local version, `scripts/stamp_cli_version.py`), or None for a
    dev/unknown install (`0.0.0+unknown`, an editable checkout, etc.) --
    those always go through a real upgrade rather than being compared."""
    try:
        version = importlib.metadata.version(PACKAGE_NAME)
    except importlib.metadata.PackageNotFoundError:
        return None
    match = re.search(r"\+([0-9a-f]{4,40})$", version)
    return match.group(1) if match else None


def _installed_with_query_extra() -> bool:
    """Whether to carry the `[query]` extra forward on upgrade -- self-
    update should preserve the shape of the existing install (ops-only vs
    ops+query), never silently grow it."""
    return importlib.util.find_spec("rich") is not None and importlib.util.find_spec("mcp") is not None


def self_update(
    version: str | None = None, *, http_get: HttpGet = _default_http_get, run: Runner = subprocess.run,
) -> SelfUpdateResult:
    """Move the running CLI itself to `version` (default: latest published
    release) before any instance is touched. Safe to call repeatedly --
    if the resolved commit matches what's already installed, no `pip
    install` is run at all."""
    try:
        previous_version = importlib.metadata.version(PACKAGE_NAME)
    except importlib.metadata.PackageNotFoundError:
        previous_version = "0.0.0+unknown"

    tag = _resolve_tag(version, http_get=http_get)
    sha = _resolve_commit(tag, run=run)

    current_sha = _current_short_sha()
    if current_sha and sha.startswith(current_sha):
        return SelfUpdateResult(updated=False, previous_version=previous_version, new_version=previous_version, tag=tag)

    extra = "[query]" if _installed_with_query_extra() else ""
    spec = f"{PACKAGE_NAME}{extra} @ git+{GIT_URL}@{sha}#subdirectory=job_squire_cli"
    result = run(
        [sys.executable, "-m", "pip", "install", "--quiet", "--upgrade", spec],
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0:
        raise SelfUpdateError(
            f"Failed to update job-squire to {tag}: {(result.stderr or result.stdout).strip()}"
        )

    try:
        new_version = importlib.metadata.version(PACKAGE_NAME)
    except importlib.metadata.PackageNotFoundError:
        new_version = previous_version

    return SelfUpdateResult(updated=True, previous_version=previous_version, new_version=new_version, tag=tag)
