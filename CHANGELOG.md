# Changelog

All notable changes to Job Squire are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows the `VERSION` file at the repo root, displayed in the app
footer as `<VERSION>-<build-sha>`.

## [0.5.0] - 2026-07-05

Initial public release: a self-hosted, two-user job-search assistant with
automated job discovery, full application tracking, and three independent AI
integration paths (manual, direct API, MCP connector).

Confirmed working on Ubuntu Server 24.04 LTS with Docker. macOS, Windows, and
Podman are documented in `docs/install/` but not yet verified end-to-end —
see the GitHub release notes.

### Added

- Automated job search across eight providers (Adzuna, Jooble, USAJOBS, The
  Muse, ZipRecruiter, Google Jobs via SerpApi, Dice, Jobicy) with dedup and
  per-provider cooldowns.
- Full application tracking funnel (`Saved` through `Hired`, plus terminal
  states), interview debriefs, recruiter/contact log, file attachments, and
  CSV export.
- AI integration: manual copy/paste export, direct multi-provider API calls
  (ranked chain with fallback), and a remote MCP server exposing 23 tools for
  live read/write by Claude Pro, Hermes Agent, or OpenClaw.
- Automatic Features: auto-triage after each search, daily follow-up drafts,
  weekly strategy review, ATS keyword gap analysis, rejection pattern alerts.
- `ai_fit_score` exposed in the `get_job` MCP tool.
- Semantic version (`VERSION` file) tracked alongside the build SHA.
- CycloneDX SBOM generation, committed to `sbom/` on every build.
- pytest suite covering migrations, crypto round-trips, auth/rate-limiting,
  MCP OAuth (PKCE, redirect_uri validation, token TTL, static key), and
  provider adapters (84 tests).
- Consolidated CI/CD pipeline (`.github/workflows/ci.yml`): ruff lint, tests
  under coverage floors, `pip-audit`, Docker build, Trivy scan (gates on
  fixable CRITICAL/HIGH before push), keyless cosign signing via GitHub OIDC,
  and SBOM attestation. Added CodeQL (Python SAST) and Dependabot (pip,
  GitHub Actions, Docker).
- `SECURITY.md` with a private vulnerability disclosure process and
  instructions for verifying a published image's signature and provenance.
- `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, and this changelog.
- Docker `HEALTHCHECK` in the Dockerfile (baseline for the web role), plus
  `healthcheck:` blocks for `job-squire-worker` and `job-squire-mcp` in both
  compose files. The worker has no HTTP endpoint, so it's checked via a new
  heartbeat file (`DATA_DIR/.worker_heartbeat`, touched every
  `HEARTBEAT_INTERVAL_MINUTES`, default 5) that's independent of the search
  schedule — a stale heartbeat means the process died or wedged, not that
  search is merely idle. The same signal is now surfaced in-app (Dashboard
  banner + Settings → History tab), not just `docker ps`.
- `docs/backup-restore.md`, `scripts/backup.sh`, and `scripts/restore.sh`: a
  WAL-safe hot-backup procedure (SQLite's Online Backup API via stdlib
  `sqlite3`, with an integrity check before the archive is written) plus a
  tested restore procedure and post-restore verification checklist. Replaces
  the untested "just tar the data folder" note in `.env.example`/
  `deployment.md`, which could produce an inconsistent snapshot under WAL mode.
- Self-service password change at `/account` (linked from the username in the
  header) for both the admin and user accounts. Requires the current password;
  rate-limited like login. Previously the only way to rotate a password was
  editing env vars and restarting with `RESET_UIDS_AND_PWDS_ON_START`.
- One-click backup download: Settings → Backup builds the same WAL-safe
  archive `scripts/backup.sh` produces (DB snapshot + `uploads/` +
  `candidate_profile.md` + `oauth_tokens.json`, optionally `.env`) and streams
  it straight from the browser — no shell/`docker exec` access needed just to
  grab a backup. Restore is still a CLI step (`scripts/restore.sh`): a safe
  restore has to stop all three containers before the data directory is
  replaced, which this app has no way to do to itself.
- International location support: Search Settings has a new Country field
  (ISO 3166-1 alpha-2, default `US`). Outside the US, the "City, ST" location
  format is no longer required — any non-empty location works. Adzuna and
  Google Jobs now use the configured country instead of a hardcoded `us`;
  Adzuna requests are skipped with a clear message if the configured country
  isn't one it supports (AT, AU, BR, CA, DE, FR, GB, IN, IT, MX, NL, PL, RU,
  SG, US, ZA). USAJOBS remains US-federal-only regardless of this setting.
  Non-US operators should also set `SCHEDULE_TZ` explicitly — `timezones.py`'s
  location-based lookup only covers US states and otherwise falls back to UTC.

### Fixed

- Settings routes (SMTP credentials, provider keys, Anthropic key, MCP key,
  candidate profile) are now restricted to the admin account; previously only
  job deletion enforced `admin_required`.
- OAuth access-token store (`oauth_tokens.json`) is now encrypted at rest
  instead of plaintext.
- 17 known CVEs closed by bumping pinned dependencies to their minimum fixed
  versions: Flask 3.0.3 to 3.1.3, Werkzeug 3.0.3 to 3.1.6, requests 2.32.3 to
  2.33.0, cryptography 42.0.8 to 48.0.1, Markdown 3.7 to 3.8.1, mcp 1.12.4 to
  1.23.0.
- `notify.py` module-level `html` import was shadowed by a same-named local
  variable in `build_digest()` and `build_error_report()`, raising
  `UnboundLocalError` on every `html.escape()` call in those functions. This
  silently broke search-digest and error-report emails whenever SMTP
  notifications were enabled.
- Code-scanning upload (Trivy SARIF, CodeQL) no longer fails the whole CI
  pipeline now that the repository is public and code scanning is live.
- Docs undercounted the MCP server at 22 tools (16 core + 6 routine-support)
  and never listed `update_job_notes` anywhere — corrected to 23 tools (17
  core + 6 routine-support) in `README.md`, `docs/README.md`,
  `docs/mcp-connector.md`, `docs/mcp-setup-guide.md`, `docs/API-Reference.md`,
  and `docs/code-reference.md`.

### Changed

- Removed unused imports, unused variables, and one ambiguous name flagged by
  the new lint gate.
