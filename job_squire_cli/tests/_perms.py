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
"""Shared assertion that a secrets-bearing file is owner-only, per host OS.

POSIX: mode bits are exactly 0600. Windows: `os.chmod` can't express that,
so job_squire_cli.ops.fileperms replaces the file's ACL instead; check the
real ACL via `icacls` -- no inherited entries, and none of the broad local
groups that an inherited folder ACL would have granted.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

_BROAD_PRINCIPALS = ("Everyone", "BUILTIN\\Users", "Authenticated Users")


def assert_owner_only(path: Path) -> None:
    if os.name == "nt":
        out = subprocess.run(["icacls", str(path)], capture_output=True, text=True, check=True).stdout
        assert "(I)" not in out, f"inherited ACL entries remain:\n{out}"
        for principal in _BROAD_PRINCIPALS:
            assert principal not in out, f"{principal} still has access:\n{out}"
    else:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
