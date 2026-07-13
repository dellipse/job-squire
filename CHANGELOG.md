# Changelog

All notable changes to Job Squire are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows the `VERSION` file at the repo root, displayed in the app
footer as `<VERSION>-<build-sha>`.

## [0.7.2] - 2026-07-12

### Fixed

- `job-squire uninstall` aborted its entire run if any registered instance's
  data root had already been deleted outside the CLI (a removed scratch/verify
  directory, or a prior uninstall that died partway through) -- `compose_down`
  ran with `cwd` set to that missing directory, and `subprocess.Popen` raises
  `FileNotFoundError` before it can exec the runtime binary. `remove_instance`
  now skips the compose teardown when the root is already gone and proceeds to
  clear the registry entry, so one missing instance no longer blocks removal
  of the rest.

## [0.7.1] - 2026-07-12

### Fixed

- `bootstrap.sh` silently failed to put `job-squire`/`jobsquire` on `PATH`
  whenever the target rc file (`~/.zshrc`, `~/.bashrc`, or the `.profile`
  fallback) didn't already exist — which is the default state on a fresh
  macOS account, since macOS doesn't create `~/.zshrc` for you. `add_path_line`
  now creates the file (`>>` does this on its own) instead of skipping it.

### Added

- **AI privacy redaction** (`app/privacy.py`, `docs/PLAN-ai-privacy.md`): personal
  identifiers (names, emails, phones, addresses, SSNs, LinkedIn URLs, clearance /
  work-authorization statements) are replaced with deterministic
  `{{PII:KIND_digest}}` placeholders before anything is sent to an AI provider,
  and swapped back in the results. Sensitive personal information that should not
  reach employers at all (health details, age signals, marital status) is stripped
  outbound and reported as coaching flags. Applies to all three AI paths: the API
  provider chain (`call_with_fallback`), manual-mode export/import, and every MCP
  tool. On by default; Settings → AI → Privacy adds a strict mode (also
  pseudonymize employer names/locations) and a local-provider toggle — local
  providers such as Ollama skip redaction by default since data never leaves the
  machine. Placeholder mappings are stored Fernet-encrypted in
  `DATA_DIR/privacy_vault.json`.
- `job-squire uninstall` — removes every registered instance, optionally the
  container runtime job-squire itself installed (`--remove-runtime`, never a
  runtime it only found already working), and the CLI's own venv and `PATH`
  entry. `--keep-data`/`--delete-data` and `--yes` mirror `remove`'s existing,
  safe-by-default flags. See `docs/job-squire-cli.md` ("Uninstalling").

## [0.7.0] - 2026-07-12

Prompt C12 (`docs/PROMPTS-deployment-cli.md`), the last in the `job-squire` CLI's
deployment/lifecycle build-out: documentation supersession and the rewritten
user setup guide, now that Prompts C1-C11 have landed the whole CLI
(create/start/stop/restart/status/list/remove/update/adopt/configure/backup/
restore/proxy/dns/tailscale).

**This is a pre-release.** The CLI's deployment/lifecycle command set is now
feature-complete, but hasn't yet had the broad real-world mileage across all
three deployment modes and both container runtimes that would justify
dropping the pre-release label.

### Changed

- `docs/deployment.md`, `docs/multi-instance.md`, and `docs/backup-restore.md`
  rewritten around the `job-squire` CLI as the primary interface — instance
  lifecycle, network-mode reverse-proxy/DNS/TLS provisioning, the instance
  registry, and the passphrase-encrypted backup archive — replacing the
  three-container/manual-script runbooks they previously described.
- `docs/Setup-Guide.md` rewritten for a first-time, non-technical operator
  around the one-line bootstrap, `job-squire create`, and the three
  deployment modes, rather than a manual `install.sh`/`docker compose` walkthrough.
- `docs/configuration.md` and `docs/architecture.md` updated for the CLI's
  per-instance directory layout and the now-permanent single-container
  topology.
- `README.md` and `docs/README.md` updated to lead with the CLI bootstrap
  instead of `install.sh` and the three-container compose.

### Fixed

- `job-squire query`'s group-level options (`--json`, `--instance`/`-i`) were
  silently unusable through the real `job-squire` entry point: `_LazyGroup`
  (`job_squire_cli/cli.py`) only overrode `list_commands`/`get_command`, so
  `job-squire query --instance NAME health` failed with "No such option
  '--instance'" and `job-squire query --help` omitted every group-level
  option, even though the same options worked fine in tests that invoked the
  real `query` group directly (never through the lazy wrapper). Found during
  this prompt's own end-to-end MCP verification. Fixed by having
  `_LazyGroup.get_params()` load and delegate to the real group's params.
- `job-squire proxy`'s fresh-SWAG-install path could never actually finish:
  a blank `--url` (the documented default, since DNS/TLS is deliberately a
  separate `job-squire dns` step) left SWAG's own `init-require-url` service
  waiting forever (`sleep infinity`), so nginx's real config was never
  generated from its `.sample` templates and every reload failed with
  `nginx: [emerg] open() ".../proxy.conf" failed`. Found during this
  prompt's own end-to-end network-mode dry run. Fixed by defaulting the SWAG
  `URL` env var to the instance's own hostname (still correct once DNS/TLS
  is configured for real) and by waiting for SWAG's init to actually
  populate its config before the first reload, rather than assuming the
  container being "up" means its entrypoint has finished.

### Removed

- The legacy three-container `docker-compose.yml` and `docker-compose.swag.yml`,
  now that the single-container image (`docker-compose.single.yml`) is
  proven in practice (`docs/PLAN-deployment-modes.md` Section 8). Existing
  three-container installs move onto the single-container image with
  `job-squire adopt` or `scripts/adopt-single-container.sh`, both unaffected
  by this removal.
- `install.sh`, `update.sh`, `uninstall.sh`, and the `docs/install/` platform
  guides that walked through them — superseded by `bootstrap.sh`/`bootstrap.ps1`
  and the `job-squire create`/`update`/`remove` subcommands.

### Security

- `app/main.py`: the `/ai/task/<run_id>/poll` and `/ai/task/<run_id>/status`
  routes took `run_id` straight from the URL and used it to build a
  filesystem path (`os.path.exists`/`open`/`os.unlink`) with no validation,
  letting a logged-in user read or delete arbitrary files via path
  traversal. Fixed by validating `run_id` against its actual `uuid4().hex`
  shape and sanitizing the resulting filename with
  `werkzeug.utils.secure_filename()` (CodeQL `py/path-injection`, alerts
  #5/#6/#7).
- `job_squire_cli/ops/compose.py`: `data/.env` (holding `SECRET_KEY` and
  `ADMIN_PASSWORD`) was written with the default umask and only chmod'd to
  `0600` afterward, leaving a brief window where it could be world-readable.
  Now written with `0600` permissions from the moment of creation (CodeQL
  `py/clear-text-storage-sensitive-data`, alert #178).
- `Dockerfile`: pip inside the shipped image's `/opt/venv` is now upgraded
  right after venv creation, closing five known pip CVEs bundled in the base
  image's pip 25.0.1 (path traversal / arbitrary file overwrite via
  malicious wheel installs: CVE-2026-8643, CVE-2026-6357, CVE-2026-3219,
  CVE-2025-8869, CVE-2026-1703).
- Five additional CodeQL findings reviewed and dismissed as false
  positives rather than left open indefinitely: an open redirect in
  `app/auth.py` already guarded by `_is_safe_next()` since the repo's first
  commit, and four test-file-only assertions CodeQL misread as hardcoded
  secrets or unsanitized URLs.

## [0.6.1] - 2026-07-11

Continues the `job-squire` CLI's deployment/lifecycle build-out from 0.6.0
(`docs/PROMPTS-deployment-cli.md`, Prompts C3-C7). The command grammar has
been real and discoverable via `--help` since 0.6.0; this release is where
most of it stops being a stub. Still a pre-release: `backup`/`restore` are
the only commands left unimplemented (Prompt C8). The three-container
Docker Compose install documented in `docs/install/` is unaffected.

### Added

- `job_squire_cli/ops/runtime.py`: container runtime detection and per-OS
  install with consent (Prompt C3). Detects a working `docker`, `podman`,
  `orbstack`, or `colima` and reuses it, installing nothing. When none is
  present, proposes the per-OS default only with explicit consent: Podman
  rootless on Linux (package manager chosen from `/etc/os-release`), Podman
  machine on macOS (OrbStack as an opt-in fallback with its commercial-use
  threshold shown at that point), and Podman on WSL2 on Windows (Docker
  Desktop as the fallback, gated on a WSL2 check that guides
  `wsl --install` plus a reboot when missing). See `docs/job-squire-cli.md`
  ("Container runtime detection and install").
- `job_squire_cli/ops/registry.py`: the cross-platform instance registry
  (Prompt C4) — a per-user `instances.json` at the platform's config
  directory (`~/Library/Application Support/job-squire` on macOS,
  `~/.config/job-squire` on Linux, `%APPDATA%\job-squire` on Windows),
  holding only non-secret metadata (name, mode, runtime, data directory,
  ports, cookie name, public URL, created date). Instance names are
  sanitized to a safe slug with collision detection, and `status` can
  report drift between the registry and what a runtime inspect actually
  observes (a renamed/missing container, a deleted data directory).
- Real `create`, `start`, `stop`, `restart`, `status`, `list`, and `remove`
  commands (Prompt C5), replacing their 0.6.0 stubs. `create` writes a
  self-contained per-instance directory (compose file, compose-level
  `.env`, `data/.env`), allocates a free local-mode port pair, generates a
  fresh `SECRET_KEY`, and brings the instance up on its recorded runtime,
  reprinting the app's own startup-guard `FATAL` reason and fix verbatim
  instead of a generic container error. `--import-from` copies non-secret
  settings from another registered instance, with `--copy-keys` as an
  explicit opt-in that decrypts with the source `SECRET_KEY` and
  re-encrypts with the destination's. `remove` always asks before deleting
  an instance's data directory and defaults to keeping it.
- `job-squire configure` (Prompt C6): manages the local `jsq_mcp_` static
  MCP bearer token end to end (generate/rotate/revoke, optional TTL,
  loopback-only unless explicitly opted in on a network-reachable
  instance), and wires in an OAuth access token obtained elsewhere as the
  alternative. Persists each instance's MCP endpoint and token in the
  CLI's own per-user `mcp.json` (keyed by instance name, selected with
  `job-squire query --instance/-i`) rather than any Hermes token store.
- `job-squire update` (Prompt C7): moves an instance to a new image
  version (`--version`, default `latest`) or rolls back to the image it
  was running before its last update (`--rollback`). The new image is
  pulled before anything about the running instance changes; only once
  that succeeds is the container stopped (`compose stop`, a graceful
  `SIGTERM` that s6 forwards so the app checkpoints its SQLite WAL first),
  the image swapped, and the container recreated. The previous image is
  recorded so a rollback can undo it, and each rollback swaps current and
  previous again.
- `job-squire adopt` (Prompt C7): turns an existing three-container
  install's data directory into a registered, single-container instance
  in place, wrapping `scripts/adopt-single-container.sh`'s logic as a
  first-class command. Derives the instance name and cookie name from the
  install's own `INSTANCE_NAME`, keeps its existing `SECRET_KEY` so stored
  secrets stay decryptable, and only ever appends two behavior-parity
  lines to `data/.env` (`TRUST_PROXY=1`, `SESSION_COOKIE_SECURE=true`, each
  only if not already set) after backing it up — never rewriting or
  re-encrypting anything already there. `--up` (or the interactive prompt)
  then offers to bring the instance up on the single-container image and
  verify health, refusing if the old three-container stack still looks
  like it's running.

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
