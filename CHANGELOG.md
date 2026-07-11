# Changelog

All notable changes to Job Squire are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows the `VERSION` file at the repo root, displayed in the app
footer as `<VERSION>-<build-sha>`.

## [Unreleased]

## [0.6.0] - 2026-07-11

First increment of the single-container / `DEPLOY_MODE` / `job-squire` CLI
deployment overhaul described in `docs/PLAN-deployment-modes.md`. This is a
pre-release: the CLI's deployment/lifecycle commands (`create`, `start`,
`update`, `backup`, ...) are structural placeholders that print "not
implemented yet" — the command grammar is real and discoverable via
`--help`, but real behavior lands incrementally in the prompts tracked in
`docs/PROMPTS-deployment-cli.md`. The three-container Docker Compose install
documented in `docs/install/` is unaffected and remains the supported path
until that CLI is complete.

### Added

- macOS install: OrbStack is now a supported container runtime alongside Podman
  and Colima. `install.sh` offers it as a third option (installs via
  `brew install --cask orbstack`, launches the app, and waits for the Docker
  engine), `uninstall.sh` tears it down, and it is documented in
  `docs/install/macos.md` and `docs/install/docker-vs-podman.md`.
- Single-container image: the web, worker, and MCP processes now also run as
  three s6-overlay longrun services inside one container on the LinuxServer
  Alpine base, with startup ordering, `SIGTERM` forwarding for WAL-safe
  shutdown, an aggregated healthcheck across all three services, and a new
  `docker-compose.single.yml`. The existing three-container compose is
  unchanged and stays supported during the migration.
- Multi-architecture image build (`linux/amd64` and `linux/arm64`) via
  `docker buildx`, with QEMU set up in CI.
- `DEPLOY_MODE` (`local` or `network`, default `local`): a preset that fills
  in granular, independently-overridable defaults — `TRUST_PROXY` (new) and
  `SESSION_COOKIE_SECURE` (existing) — rather than being read directly by
  application code. See `docs/PLAN-deployment-modes.md` Section 3.
- Startup safety guard: the app validates its effective deploy configuration
  at boot and refuses to start (or shows a persistent in-app banner, for
  risky-but-runnable cases) on unsafe combinations such as network mode
  without HTTPS/`TRUST_PROXY`, naming the offending variable and the fix in
  the log, the console, and — once the CLI lifecycle lands — on the command
  line.
- Local MCP static token hardened: `jsq_mcp_`-prefixed, 256-bit, Fernet-
  encrypted at rest, constant-time compared, loopback-only by default, with
  rotation (invalidating the previous value) and revocation.
- `adopt` helper and single-container migration path for turning an existing
  three-container data directory into a single-container instance without
  losing the `SECRET_KEY` or requiring a rewrite of existing env vars.
- `job-squire` CLI (`job_squire_cli/`, distribution name `job-squire-cli`):
  the old `jobsquire-cli` MCP query wrapper (`health`, `list`, `pipeline`,
  `contacts`, `job`, `contact`, `followups` — `overdue` renamed to
  `followups`, `stages`/`top` dropped) folds into this repo as one
  installable, decoupled from the Hermes `~/.hermes/` sidecar in favor of a
  self-contained MCP client. It gains a new deployment/lifecycle command
  group (`create`, `start`, `stop`, `restart`, `status`, `list`, `update`,
  `remove`, `configure`, `backup`, `restore`) namespaced apart from the
  query group's own `list` via `job-squire query list`. `job-squire` is the
  canonical entry point; `jobsquire` remains an alias. See
  `docs/job-squire-cli.md`.
- `bootstrap.sh` (macOS/Linux) and `bootstrap.ps1` (Windows): the one-line
  install for the CLI (`curl -fsSL .../bootstrap.sh | sh`, or
  `irm .../bootstrap.ps1 | iex`). Resolves the latest GitHub release by
  default or a pin via `JOBSQUIRE_VERSION`, pins the resolved tag to an
  immutable commit before installing, installs into an isolated per-user
  environment, and hands off to `job-squire create`.

### Changed

- Versioning: the app's `<VERSION>-<sha>` (OCI image tag) and the CLI's
  `<VERSION>+<sha>` (PEP 440 local version) are now explicitly documented as
  one `VERSION` file rendered two ways for two targets with different
  syntax rules, not two independent schemes — see `docs/job-squire-cli.md`
  ("Versioning") and the root `CLAUDE.md`.

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
