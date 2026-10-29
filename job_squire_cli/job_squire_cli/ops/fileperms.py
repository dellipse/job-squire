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
"""Restrict a secrets-bearing file to its owner, on every host OS.

The CLI writes several files that hold credentials: the instance data
`.env` (SECRET_KEY / ADMIN_PASSWORD), backup archives, the SWAG compose
file (DuckDNS token) and certbot's `cloudflare.ini` (API token). On
macOS/Linux `chmod 0600` is enough. On Windows `os.chmod` only toggles the
read-only attribute, so before this module those files silently kept
whatever ACL their folder granted -- readable by every local user if the
data root sat outside the user profile (e.g. `C:\\job-squire`).

On Windows this replaces the file's ACL via the built-in `icacls`:
inheritance off, then full control for the current user's SID and for
SYSTEM only. SYSTEM stays because Windows services (and anything running
as SYSTEM) can take ownership regardless; removing it gains nothing and
can break tooling. The current user is identified by SID from
`whoami /user` rather than by name, so domain/local name ambiguity can't
grant the wrong account.

Best-effort on every platform, like the bare `chmod` calls it replaces:
some filesystems (FAT/exFAT, certain bind mounts) can't hold permissions,
and failing a backup or `create` over that would be worse than the file
staying at the folder's default access.
"""

from __future__ import annotations

import csv
import io
import os
import platform
import subprocess
from pathlib import Path
from typing import Callable

Runner = Callable[..., "subprocess.CompletedProcess[str]"]

SYSTEM_SID = "S-1-5-18"


def current_user_sid(*, run: Runner = subprocess.run) -> str | None:
    """The current Windows user's SID, or None if `whoami` can't say."""
    try:
        result = run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    for row in csv.reader(io.StringIO(result.stdout or "")):
        if len(row) >= 2 and row[-1].strip().startswith("S-1-"):
            return row[-1].strip()
    return None


def restrict_to_owner(path: Path, *, system: str | None = None, run: Runner = subprocess.run) -> bool:
    """Make `path` readable/writable by its owner only. Returns True if the
    restriction was applied, False if this platform/filesystem refused
    (the file is left as it was; callers treat that as best-effort)."""
    system = system or platform.system()
    try:
        os.chmod(path, 0o600)
    except OSError:
        if system != "Windows":
            return False
    if system != "Windows":
        return True

    sid = current_user_sid(run=run)
    if not sid:
        return False
    argv = ["icacls", str(path), "/inheritance:r",
            "/grant:r", f"*{sid}:F", "/grant:r", f"*{SYSTEM_SID}:F"]
    try:
        result = run(argv, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0
