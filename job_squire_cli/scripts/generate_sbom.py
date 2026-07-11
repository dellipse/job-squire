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
"""Generate a CycloneDX SBOM for job-squire-cli.

Carried over from the old jobsquire-cli project's scripts/generate_sbom.py,
pointed at this package's own pyproject.toml and output path. Scans the
current Python environment (expected to have job-squire-cli installed into
it, [query] extra included so the SBOM covers both command groups) and
writes a CycloneDX 1.6 JSON SBOM to sbom/job-squire-cli.cdx.json. Root
component metadata (name, version) is read directly from pyproject.toml by
cyclonedx-py.

Usage:
    pip install ".[query]"
    pip install cyclonedx-bom
    python scripts/generate_sbom.py
"""
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = REPO_ROOT / "sbom" / "job-squire-cli.cdx.json"


def main() -> None:
    OUTPUT_PATH.parent.mkdir(exist_ok=True)

    subprocess.run(
        [
            "cyclonedx-py",
            "environment",
            "--mc-type",
            "application",
            "--pyproject",
            str(REPO_ROOT / "pyproject.toml"),
            "--sv",
            "1.6",
            "-o",
            str(OUTPUT_PATH),
            sys.executable,
        ],
        check=True,
    )
    print(f"Wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
