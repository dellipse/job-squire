#!/usr/bin/env python3
"""Generate a CycloneDX SBOM for job-squire.

Scans the current Python environment (expected to have requirements.txt
already installed into it) and writes a CycloneDX 1.6 JSON SBOM to
sbom/job-squire.cdx.json. job-squire has no pyproject.toml, so the root
component metadata is filled in manually after generation.

Usage:
    pip install -r requirements.txt
    pip install cyclonedx-bom
    python scripts/generate_sbom.py
"""
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = REPO_ROOT / "sbom" / "job-squire.cdx.json"


def main() -> None:
    OUTPUT_PATH.parent.mkdir(exist_ok=True)
    version = (REPO_ROOT / "VERSION").read_text().strip()

    subprocess.run(
        [
            "cyclonedx-py",
            "environment",
            "--mc-type",
            "application",
            "--sv",
            "1.6",
            "-o",
            str(OUTPUT_PATH),
            sys.executable,
        ],
        check=True,
    )

    data = json.loads(OUTPUT_PATH.read_text())

    # Strip the two fields cyclonedx regenerates on every run: a random
    # serialNumber UUID and a wall-clock metadata.timestamp. Left in, they make
    # the SBOM differ on every build even when no dependency changed, so the
    # "Commit SBOM if changed" step commits and pushes to main every single
    # build -- which is what keeps racing release.yml's CLI-stamp push. Dropping
    # them makes the SBOM a pure function of the installed dependencies + the
    # app version, so an unchanged dependency set produces a byte-identical file
    # and no commit at all. (Build time/provenance is still recorded out-of-band
    # by the cosign SBOM attestation on the published image.)
    data.pop("serialNumber", None)
    data.get("metadata", {}).pop("timestamp", None)

    data["metadata"]["component"] = {
        "bom-ref": "root-component",
        "type": "application",
        "name": "job-squire",
        "version": version,
        "description": (
            "Self-hosted, two-user job-search companion: automates job "
            "discovery, tracks applications through a hiring funnel, and "
            "integrates with Claude as an AI coach via MCP."
        ),
        "licenses": [{"license": {"id": "AGPL-3.0-only"}}],
        "externalReferences": [
            {"type": "vcs", "url": "https://github.com/dellipse/job-squire"}
        ],
    }
    OUTPUT_PATH.write_text(json.dumps(data, indent=2) + "\n")
    print(f"Wrote {OUTPUT_PATH} ({len(data.get('components', []))} components)")


if __name__ == "__main__":
    main()
