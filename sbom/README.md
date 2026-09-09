# SBOM

This directory is a build-time scratch space, not a place to look for the
SBOM. `scripts/generate_sbom.py` writes `job-squire.cdx.json` here during
CI (git-ignored, not committed), and the `build-and-publish` job in
`.github/workflows/ci.yml` immediately signs and attaches it to the
published image as a [cosign](https://docs.sigstore.dev/cosign/) attestation
— that signed copy, tied to the exact digest that shipped, is the real one.

To pull it:

```sh
cosign download attestation ghcr.io/dellipse/job-squire:latest \
  --predicate-type https://cyclonedx.org/bom | jq -r '.payload | @base64d | fromjson | .predicate'
```

or verify it against the maintainer's identity first with
`cosign verify-attestation`.

A version of this file used to be committed to the repo on every publish
for convenience. Dropped 2026-09-08: it carried no integrity guarantee (a
plain JSON file, not tied to any specific image build) and had gone stale
for weeks whenever an earlier CI step failed — see
`job-squire-audit-2026-09-08.md`'s DEP-01 finding — and once branch
protection started requiring status checks on every push to `main`, the
`[skip ci]` bot commit that updated it could never actually produce them,
so it could no longer land at all.
