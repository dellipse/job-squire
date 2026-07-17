# Changelog

All notable changes to Job Squire are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows the `VERSION` file at the repo root, displayed in the app
footer as `<VERSION>-<build-sha>`.

## [Unreleased]

### Fixed

- Getting Started → "Resume & documents" step never marked complete for a user who uploaded
  their own resume file: the step's completion check only recognized a `kind="Resume"`
  `CandidateAsset`, which was reserved for the AI-generated draft produced by the resume
  interview. A resume uploaded through the normal document form (correctly filed as
  `kind="Base Resume"`) was invisible to that check, so uploading a resume plus adding an
  online-profile link never satisfied the step.

### Added

- `app/resume_convert.py`: deterministic, non-AI conversion of an uploaded resume document to
  markdown -- docx (via `python-docx`, mapping headings/bold/italic/lists/simple tables), pdf
  (via new dependency `pypdf`, plain text only), and txt/md (passthrough). Uploading a document
  as `kind="Base Resume"` (`app/main.py:settings_asset_upload`) now automatically converts it and
  saves the result as the same `kind="Resume"` markdown draft the resume interview produces
  (`app/onboarding.py:save_resume_draft`), so a plain upload satisfies the Getting Started
  profile step the same way the interview does, with no AI provider required. The converted
  markdown is read back into the "paste your finished resume" box on the Getting Started profile
  step for review/editing. Conversion failures (unsupported file type, corrupted/password-protected
  file, no extractable text) fall back gracefully with a flash message -- the original upload
  always succeeds regardless.

## [0.7.8] - 2026-07-17

### Added

- `job-squire ollama check` / `job-squire ollama setup` (job_squire_cli/ops/ollama_assist.py):
  CLI-side implementation of `docs/PLAN-ollama-assist.md`. `check` detects this *host's* real
  RAM/CPU/GPU (authoritative, unlike an in-container detector, which only ever sees the Docker
  Desktop/Podman VM's allocation) and reports a capability tier with recommended triage/analysis
  models; `setup` installs Ollama via the official channel for the OS if needed, pulls the
  recommended models, bakes the tier's recommended context window into a derived model via a
  generated Modelfile (`ollama create <tag>-ctx<n>` — Ollama's OpenAI-compatible endpoint has no
  per-request way to set context size, confirmed against docs.ollama.com), writes the
  `ai_provider_configs` row for an instance, and runs an end-to-end round-trip test — every step
  reviewable first with `--dry-run`. Model tags (Qwen3.6, Gemma 4, Qwen3, Gemma 3) verified against
  the Ollama library 2026-07-16.
- `AIProviderConfig.num_ctx` (additive migration) and a context-capacity check in
  `call_with_fallback` (app/ai.py): a provider configured with a known context window that's too
  small for a given prompt is now skipped in favor of the next provider in the ranked chain, instead
  of silently sending a request Ollama would truncate without error and returning a plausible-looking
  but degraded answer. If every eligible provider gets skipped this way the resulting error names the
  reason explicitly — meant to surface cleanly through the existing per-batch/per-job error handling
  in unattended worker runs (auto-triage, weekly review, etc.), not just interactive use.
- Settings → AI providers: `triage_model` and `num_ctx` are now editable on the add/edit provider
  forms.
- Prompt chunking for context-constrained providers (app/ai.py): rather than just skipping an
  under-sized provider, tasks that hit `ContextCapacityError` (new — a `RuntimeError` subclass so
  existing callers are unaffected) now shrink the prompt to fit instead of giving up outright. The
  full single-shot prompt is always tried first; chunking is a fallback, never the default, so
  analysis quality is unaffected whenever the configured provider has room. Two strategies, matched
  to the shape of each task: `run_auto_triage`/`run_followup_drafts` batch independent jobs, so a
  capacity failure just means retrying with a smaller batch (`_call_batched_with_capacity_shrink`,
  recursive halving down to one job per call — no reassembly needed, each job is already scored/
  drafted independently). `run_weekly_review`/`run_rejection_analysis` build one aggregate prompt
  over the whole pipeline, so a capacity failure triggers real map-reduce
  (`_run_chunked_or_single` + `_reduce_partial_analyses`): the job list is split into chunks, each
  chunk gets its own analysis pass, then one final call synthesizes the partial results into a
  single coherent review. Whenever the map-reduce path runs, the returned `overall_summary` is
  prefixed with an explicit note that the analysis was chunked due to the model's context window —
  visible in the UI, not just the worker logs — since cross-job pattern detection can be less
  precise across chunk boundaries than a single-pass review.

### Fixed

- `call_with_fallback` (app/ai.py) was reading `AIProviderConfig.model` for every task regardless of
  `is_triage`, so a provider's `triage_model` — settable via `job-squire ollama setup` since it was
  added, but with no Settings form field until this change — was silently ignored in favor of the
  (larger, slower) analysis model on every triage/follow-up call.
- CI's Trivy image-scan gate ("fixable CRITICAL/HIGH CVEs") was failing on curl/libcurl 8.19.0-r0 in
  the LinuxServer Alpine 3.23 base — two HIGH CVEs (CVE-2026-5773, CVE-2026-6276) with no fix
  backported to that branch. Bumped the base image to the Alpine 3.24 line
  (`ghcr.io/linuxserver/baseimage-alpine:3.24-03b33b49-ls6`), which ships curl 8.21.0-r0 and resolves
  both. This also moves the base's Python from 3.12 to 3.14; the full `requirements.txt` lockfile (61
  packages, `cryptography`/`lxml`/`pydantic_core` included) was re-verified to install as binary
  musllinux wheels and import cleanly at runtime under 3.14 before the bump, so the wheel-coverage
  risk retired for 3.12 stays retired. Retires the `.trivyignore` stopgap added while the base was
  still pinned to 3.23.

## [0.7.7] - 2026-07-13

### Fixed

- The AI resume interview (`/getting-started/profile/interview`) could crash
  with a bare 500 and no error message against a slow AI provider. gunicorn's
  worker-timeout watchdog was killing the worker mid-request before the
  provider call's own timeout or fallback logic got a chance to run —
  `requests`' read timeout resets on every socket read rather than covering
  total elapsed time, so a provider trickling a response slowly could run
  past gunicorn's `--timeout` without ever raising a catchable
  `requests.Timeout`. Bumped gunicorn `--timeout` 60→180s as a stopgap;
  moving the interview turn to the same background-thread + poll pattern
  used by every other slow-AI-call route (and possibly a real streaming
  chat session) is tracked as a follow-up in `docs/PLAN-onboarding.md`.
- Getting Started → "Run search now" (and the equivalent button in Settings
  → Search) silently failed every time: the background search thread
  referenced `current_app` after execution had already left the request
  context, raising `RuntimeError: Working outside of application context`
  on every click. Fixed by capturing the real app object before starting
  the thread, matching the pattern already used by every other
  background-thread route in `main.py`.

### Removed

- Dice as a job source (`app/providers.py`). Dice's public RSS feed
  (`dice.com/jobs/rss`) no longer returns RSS — it now serves the same HTML
  search page as a normal browser visit regardless of query params, so the
  adapter's XML parser was silently failing and returning zero results on
  every run with no visible error. Dice's official Jobs API was shut down
  around 2017, and there is currently no free/public replacement, so the
  provider is removed rather than patched. `PROVIDERS`, the `search_dice`
  adapter, RSS-parsing helpers used only by it, and all related UI copy,
  docs, and tests were removed.

### Changed

- Job source ordering (`PROVIDERS` registry, and everywhere it drives
  display order — Settings → Sources, Getting Started → Providers): The
  Muse and Jobicy, the two remaining sources that need no API key, now
  list first.
- New installs now start with The Muse enabled by default, so the first
  automated search isn't empty before any credentials are configured.
  Jobicy is not used for this since it's remote-only and would silently
  skip on-site/hybrid searches. Existing installs are unaffected — this
  only seeds a row when none exists yet for that provider.
- USAJOBS's description no longer references "the Vegas area" — Job Squire
  is a general-audience self-hosted tool, not scoped to one metro.
- UI/validation text that showed a hardcoded example location ("Henderson,
  NV" placeholder on Getting Started, "Boise, ID" in the search-settings
  validation message) now picks a random city from a top-50-US-cities list
  (`app/sample_locations.py`) on each render instead.

## [0.7.6] - 2026-07-12

### Added

- Getting Started → Resume: Phase 2 of the onboarding walkthrough
  (`docs/PLAN-onboarding.md`). Candidates with no resume yet can now build
  one through an interview, in whichever of the three AI modes they're
  already using:
  - **Interactive** — a one-question-at-a-time chat right in the wizard
    (`/getting-started/profile/interview`), driven through the configured
    AI provider chain.
  - **Claude connector** — a new on-demand routine that has Claude
    interview conversationally and save the result directly via a new
    `save_resume_draft` MCP tool.
  - **Any free AI chat** — a self-contained copy-paste prompt; paste the
    finished resume back into the wizard.
  - All three write through one function (`onboarding.save_resume_draft`)
    to a new `CandidateAsset` kind, `"Resume"` (AI-generated), kept
    separate from an uploaded `"Base Resume"` so re-running the interview
    replaces its own draft without touching anything uploaded. Extracted
    background facts are folded into `candidate_profile.md`.
  - Interview content follows current (2026) resume practice: ATS-friendly
    single-column reverse-chronological format, one page under 5 years of
    experience / two pages at 5+, quantified accomplishments, a
    keyword-matched skills section, and no age/marital/health signals.

## [0.7.5] - 2026-07-12

### Fixed

- Settings tabs (`app/static/app.js`) ignored the URL hash entirely and
  always restored whichever tab was last saved in `localStorage` — so a
  link or bookmark to e.g. `#tab-claude` landed on whatever tab you'd
  last had open, not the AI tab the fragment names. The hash now takes
  priority on load, is kept in sync as you switch tabs, and is followed
  on `hashchange` (back/forward).
- Several settings cards, including AI → Providers, referenced theme
  variables (`--surface`, `--surface-alt`, `--border`, `--text-muted`,
  `--danger`, `--success`, etc.) that were never defined in `style.css`,
  so their hardcoded `var(..., #fallback)` colors always won regardless
  of light/dark mode. Defined all of them as aliases onto the existing
  `--panel`/`--line`/`--muted`/`--red`/`--green` tokens for both themes.
- The Muse's API key field showed "(optional)" twice — once baked into
  the field label in `app/providers.py`, once added again generically by
  the template based on `required: False`.
- Getting Started → "Job boards": each keyless-provider checkbox
  auto-submitted its own form independently, so checking several in a
  row could race against each other's page reload and silently drop a
  change. They now save together via one form and an explicit Save
  button (`main.settings_providers_keyless_save`).
- Getting Started → "First search" required a manual page refresh to
  see whether the background search had finished. The page now polls
  itself every 5s while a `SearchRun` is in the `running` state, driven
  by a `data-poll` attribute the server sets from actual run status.
- Settings → Sources → "Pull now" (`settings_provider_pull`) saved new
  jobs but never created a `SearchRun` row, so a pull that found results
  never showed up in Settings → History. It now logs a run the same way
  a scheduled/full search does.
- Fit scores never got refreshed after a candidate profile edit —
  `run_auto_triage()` only scores jobs with no score yet, by design, so
  it silently skips everything already scored. Added a "Rescore all
  Saved jobs" action (Candidate Profile card, `/ai/run/rescore`) that
  clears existing Saved-job scores and re-runs triage against the
  current profile.
- Getting Started → "Resume & documents" marked itself done the moment
  the seeded placeholder `candidate_profile.md` existed on disk, even
  completely unedited — a plain non-empty check doesn't distinguish the
  shipped template from a real profile. It now requires the text to
  actually differ from the bundled template, have most of its bracket
  placeholders replaced, and clear a minimum length.

## [0.7.4] - 2026-07-12

### Added

- **Getting Started walkthrough** (`app/onboarding.py`, `docs/PLAN-onboarding.md`
  Phase 1): fresh installs are no longer dropped on an empty Dashboard. A
  persistent, re-entrant checklist (dashboard card + "Getting started" nav
  entry) walks through setup: who the install is for, an optional second
  account (now creatable in-app instead of env-vars only), AI setup with a
  privacy-first framing (local Ollama → free cloud tiers → Claude connector,
  or an explicit no-AI path), resume/document upload and online-profile links,
  search targets including target salary, job boards with the zero-key trio
  (Dice, The Muse, Jobicy) called out for instant results, and a guided first
  search with next steps. Step completion is derived from real app state so
  the checklist never drifts; every step is skippable and revisitable.
- Search setting **"Include remote jobs"** (`SearchConfig.include_remote`,
  default on): turning it off skips remote-only boards (Jobicy) in search runs.

## [0.7.3] - 2026-07-12

### Fixed

- `job-squire uninstall` tore down every registered instance (and, when the
  venv layout matched, the CLI itself and its PATH entry) with no top-level
  confirmation -- only per-instance data-keep and runtime-removal prompts
  existed, so the destructive part of the command ran unconditionally the
  moment it was invoked. It now asks "Uninstall job-squire?" first, defaulting
  to "no"; `--yes` still bypasses it for scripted use, same as every other
  prompt this command has.
- `job-squire uninstall` could report `Open a new terminal for the PATH
  change to take effect.` even when no PATH line was actually removed --
  that message printed unconditionally whenever the CLI's own venv was
  removed, regardless of whether `strip_path_line` found anything to strip.
  It's now conditioned on an rc file actually having changed, with an
  explicit "no PATH entry was found" message otherwise. Separately,
  `strip_path_line`'s directory match (`bin_dir`, derived from
  `sys.executable`) now falls back to comparing resolved paths, since
  `sys.executable` can come back through a symlink's real target rather
  than the literal path `bootstrap.sh` wrote -- the likely cause of the
  silent no-op in the first place.

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
