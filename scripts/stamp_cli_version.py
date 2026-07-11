#!/usr/bin/env python3
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
"""Stamp job_squire_cli/pyproject.toml's version from the repo's one VERSION
file plus the current commit's short SHA.

See docs/job-squire-cli.md ("Versioning") for the rule this implements:
one source of truth (the root VERSION file), rendered two ways because the
two targets have different syntax constraints --

    Docker image tag (OCI, no '+' allowed):  <VERSION>-<sha>
    job-squire-cli package (PEP 440):        <VERSION>+<sha>

This script produces the second rendering. The first is already computed
inline in .github/workflows/ci.yml (BUILD_VERSION) and is untouched by this
script.

Usage:
    python scripts/stamp_cli_version.py
"""
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT_PATH = REPO_ROOT / "job_squire_cli" / "pyproject.toml"


def main() -> None:
    version = (REPO_ROOT / "VERSION").read_text().strip()
    sha = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout.strip()

    stamped = f"{version}+{sha}"
    text = PYPROJECT_PATH.read_text()
    new_text, count = re.subn(
        r'^version = "[^"]*"$', f'version = "{stamped}"', text, count=1, flags=re.MULTILINE
    )
    if count != 1:
        raise SystemExit(f"Could not find a version = \"...\" line in {PYPROJECT_PATH}")

    PYPROJECT_PATH.write_text(new_text)
    print(f"Stamped {PYPROJECT_PATH} -> {stamped}")


if __name__ == "__main__":
    main()
