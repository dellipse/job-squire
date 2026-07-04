# Security Policy

Job Squire is a self-hosted, two-user job-search assistant. Because operators run
it themselves and it stores real credentials (job-board API keys, an Anthropic
API key, SMTP passwords, and live MCP access tokens), we take security reports
seriously and want them handled privately.

## Supported versions

Job Squire is at an early `0.x` release. Security fixes are applied to the latest
`main` and the most recent published image only. There is no long-term support
branch yet.

| Version | Supported |
|---|---|
| latest `main` / newest image | yes |
| older tagged builds | no (upgrade to the latest) |

## Reporting a vulnerability

Please **do not** open a public GitHub issue for a suspected vulnerability, and
do not disclose it publicly until it has been fixed.

Report privately through GitHub's private vulnerability reporting:

1. Go to the repository's **Security** tab: <https://github.com/dellipse/job-squire/security>
2. Click **Report a vulnerability**.
3. Fill in the form. Only the maintainers can see it.

Please include, as best you can:

- what the issue is and the impact you think it has,
- the version or image tag (the app footer shows the running version),
- steps to reproduce, and a proof of concept if you have one,
- any suggested fix or mitigation.

> Maintainer note: private vulnerability reporting must be turned on for the
> "Report a vulnerability" button to appear. Enable it under
> **Settings, Security, Private vulnerability reporting**.

## What to expect

This is a small, volunteer-maintained project, so please allow for reasonable
response times rather than same-day turnaround.

- Acknowledgement of your report within about 7 days.
- An initial assessment (confirmed, need more info, or not a vulnerability)
  within about 14 days.
- For confirmed issues, a fix or documented mitigation as soon as practical,
  coordinated with you on timing before any public disclosure.
- Credit in the release notes or the published advisory if you would like it.

## Verifying a published image

Every image pushed to `ghcr.io/dellipse/job-squire` from `main` is:

- built and scanned for known CVEs (Trivy, fails the build on fixable
  CRITICAL/HIGH findings) before it is pushed,
- signed keylessly with [cosign](https://github.com/sigstore/cosign) using
  GitHub's OIDC identity for this repository's CI workflow,
- published with SLSA build provenance and an SBOM attestation.

To verify a pulled image was built by this repository's CI and not tampered
with in transit or on the registry:

```bash
cosign verify \
  --certificate-identity-regexp "^https://github.com/dellipse/job-squire/" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  ghcr.io/dellipse/job-squire:latest
```

To inspect the build provenance attestation:

```bash
cosign verify-attestation \
  --type slsaprovenance \
  --certificate-identity-regexp "^https://github.com/dellipse/job-squire/" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  ghcr.io/dellipse/job-squire:latest
```

A versioned CycloneDX SBOM is also committed to [`sbom/`](sbom/) on every
build.

## Security model and scope

Understanding the intended model helps separate real vulnerabilities from
expected behavior.

- **Two trusted users.** The app is designed for two accounts (an `admin`
  operator and an optional `user`). It is **not** hardened for untrusted
  multi-tenant use. Both accounts are assumed to be trusted humans.
- **Run it behind TLS.** Job Squire must sit behind a reverse proxy that
  terminates TLS (SWAG / nginx). Do not expose the raw HTTP ports to a network
  you do not control.
- **Privilege boundary.** Settings, stored secrets, provider and SMTP
  configuration, AI/MCP configuration, and the candidate profile are restricted
  to the `admin` account. The `user` account cannot read or change stored
  secrets.
- **Secrets at rest.** All stored secrets (provider keys, the Anthropic key,
  SMTP password) are encrypted in the database with a Fernet key derived from
  `SECRET_KEY`. The on-disk OAuth access-token store is likewise encrypted at
  rest and written `0600`.
- **`SECRET_KEY` is critical.** It both signs sessions and derives the secret
  encryption key. Keep it out of version control and logs. If it is ever
  exposed, rotate it and re-enter secrets, see
  [`docs/deployment.md`](docs/deployment.md#rotating-secret_key-and-re-entering-secrets).
- **Host access is game over.** Anyone who can read the data directory or run as
  the app's user has, by design, access to the app's data. Restrict filesystem
  permissions on `DATA_DIR` accordingly.

### Usually out of scope

- Issues that require an already-authenticated, trusted account acting against
  itself (the trust model above).
- Missing hardening that only matters for untrusted multi-tenant hosting.
- Reports against a deployment that exposes HTTP directly, without the required
  TLS reverse proxy.
- Vulnerabilities in third-party job boards or in Anthropic's API rather than in
  Job Squire itself.

Thank you for helping keep Job Squire and the people who self-host it safe.
