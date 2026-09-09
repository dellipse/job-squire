# SBOM

This directory is a build-time scratch space, not a place to look for the
SBOM. `scripts/generate_sbom.py` writes `job-squire-cli.cdx.json` here during
CI (git-ignored, not committed), and the `sbom` job in
`.github/workflows/cli.yml` uploads it as a GitHub Actions build artifact
(`job-squire-cli-sbom`, retained 90 days) on the run that produced it.

To get it, open the latest successful `main`-branch run of the **CLI**
workflow (Actions tab → CLI → filter to `main`) and download the
`job-squire-cli-sbom` artifact.

`job_squire_cli` is a pip-distributed package rather than a container image,
so there's no published OCI digest for `cosign attest` to sign the way
`sbom/README.md` describes for the main app image -- a build artifact
attached to the run is the closest equivalent: the CI-produced file itself
is canonical, and it's never copied back into the repo.

A version of this file used to be committed to the repo on every publish
for convenience. Dropped 2026-09: `job_squire_cli/sbom/job-squire-cli.cdx.json`
was never actually tracked in git, so the job's `git diff --quiet` check
against it always exited 0 and it silently never committed anything -- the
same shape of bug `sbom/README.md` describes for the main app image's SBOM
(fixed there in PR #34) -- see `job-squire-audit-2026-09-08.md`'s DEAD-08/
DEP-01 findings.
