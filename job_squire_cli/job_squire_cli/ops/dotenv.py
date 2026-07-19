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
"""Minimal, line-preserving `KEY=value` env-file read/append/set helpers,
used by ops/compose.py (compose-level bookkeeping like
`update`'s `PREVIOUS_IMAGE`), ops/tailscale.py, and ops/backup.py's restore.

`set_line`/`append_if_absent` never reorder or rewrite a file's other
lines -- each is a targeted single-line change, which is what "additive,
never assumed" (CLAUDE.md's migration convention) requires of anything
that touches an *existing* install's `data/.env`.
"""
from __future__ import annotations

from pathlib import Path


def parse(path: Path) -> dict[str, str]:
    """Every `KEY=value` line in `path` as a dict. Missing file -> `{}`."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


def get(path: Path, key: str, default: str | None = None) -> str | None:
    return parse(path).get(key, default)


def set_line(path: Path, key: str, value: str) -> None:
    """Set `KEY=value`, replacing an existing line for that key in place,
    or appending one if absent. Every other line is preserved verbatim."""
    lines = path.read_text().splitlines() if path.exists() else []
    prefix = f"{key}="
    for i, line in enumerate(lines):
        if line.strip().startswith(prefix):
            lines[i] = f"{key}={value}"
            path.write_text("\n".join(lines) + "\n")
            return
    lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n")


def append_if_absent(path: Path, key: str, value: str, *, comment: str | None = None) -> bool:
    """Append `KEY=value` (with an optional preceding comment block) only
    if `key` isn't already set anywhere in the file. Returns whether it
    appended anything, so a caller can report exactly what changed."""
    if key in parse(path):
        return False
    text = path.read_text() if path.exists() else ""
    block = (f"{comment}\n" if comment else "") + f"{key}={value}\n"
    separator = "" if (not text or text.endswith("\n")) else "\n"
    with path.open("a") as f:
        f.write(f"{separator}\n{block}" if text else block)
    return True
