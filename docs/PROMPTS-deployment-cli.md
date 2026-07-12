# Implementation Prompts: The `job-squire` CLI (Deferred CLI Session)

**Companion to:** `docs/PLAN-deployment-modes.md` (Sections 6 and 7, plus the deferred items in Section 8) and `docs/PROMPTS-deployment-modes.md` (the app and image work that must ship first).

**Read this first.** This prompt set builds the `job-squire` deployment CLI: the single front door for bootstrapping, runtime selection, instance creation, lifecycle, backup and restore, and provisioning. It assumes the entire `PROMPTS-deployment-modes.md` set has already landed and been committed, because the CLI generates the env file and `docker-compose.single.yml`, relies on the `DEPLOY_MODE` and `TRUST_PROXY` resolution, catches the startup safety guard's non-zero exit, and manages the `jsq_mcp_` static token. Do not start this set until that one is done.

**A crucial current-state fact.** The existing `jobsquire-cli` project (sibling folder `jobsquire-cli/`) is **not** a deployment tool. It is a thin MCP-client wrapper: a `click` + `rich` CLI whose commands (`health`, `list`, `pipeline`, `contacts`, `job`, `contact`, `followups`) query a running Job Squire over MCP. Today it reaches the MCP server through the Hermes `mcp_client` sidecar at `~/.hermes/`. Its entry point is `jobsquire = jobsquire.cli:main`, it builds with hatchling, and its version is `0.1.0+<sha>` (PEP 440 local version). The Job Squire app repo versions as `0.1.0-<sha>` (hyphen).

**Settled decisions carried into this set (from review on 2026-07-11):**

- **One tool, not two.** The query CLI folds into the same repo and package as the new deployment CLI. There is one installable, one bootstrap, one version. The two surfaces are kept as separate command groups, and the query group's dependencies are optional (an extras group, lazily imported) so a query-only user never pays for the ops surface and the ops commands never require a live MCP endpoint.
- **No dependency on Hermes, and nothing vendored from it.** The query commands must not require the Hermes `mcp_client` at `~/.hermes/` to be present, and must not bundle or copy any Hermes code. Instead they talk to the Job Squire MCP server with a self-contained MCP client built on the standard protocol (reuse the `mcp` library the server already depends on, or a minimal direct Streamable HTTP client). Hermes and any other MCP host remain able to use our MCP server by reading our published MCP documentation; the coupling runs one way, through docs, never through shared code.
- **Canonical command `job-squire`, with `jobsquire` as an alias.** The dashed name is primary (matching the design's prose); the undashed name stays wired as an alias so existing muscle memory keeps working.
- **Unify the two version schemes** into one for the merged package.

**Run these one at a time in Claude Code on a machine with a working container runtime.** Each prompt is self-contained and assumes every earlier prompt in this file has landed and been committed.

**Conventions every prompt must follow (from `CLAUDE.md`):**

- Commit style `TYPE: Short description` (`NEW:`, `FIX:`, `REFACTOR:`, `DOCS:`).
- Secrets are Fernet-encrypted via `app/crypto.py`; the backup archive uses its own passphrase KDF and cipher (Prompt C7). Never write a secret in plaintext to disk or to the registry.
- Deployment values come from env and the registry, never hardcoded.
- The registry is the source of truth for instance metadata. Structural changes made outside the CLI are reconciled, not assumed.
- Keep the two-user-per-instance, single-tenant stance.

**Design guardrails carried from the plan:**

- The operator never needs to run a raw container command, but direct runtime access always remains available (no proprietary wrapper, no hidden control plane).
- Podman is the default runtime on every platform including macOS; OrbStack is a macOS opt-in with its licensing shown at the point of choice; Docker is used when already present or explicitly chosen. Never install a runtime over one that already works.
- Local modes use loopback and print `localhost`/`127.0.0.1` links, never a LAN IP. Network mode always sits behind an external TLS-terminating proxy the app never replaces.
- Tailscale Serve (not Funnel) is the sanctioned private path to a local instance.

**Progress checklist:**

- [x] Prompt C1: fold-in, package structure, command grammar, version unification
- [x] Prompt C2: bootstrap one-liner (install the CLI from GitHub)
- [x] Prompt C3: container runtime detection and install
- [x] Prompt C4: cross-platform instance registry
- [x] Prompt C5: instance lifecycle core (create, start, stop, status, remove)
- [x] Prompt C6: MCP authentication and token-config plumbing
- [ ] Prompt C7: update, rollback, and adopt existing data
- [ ] Prompt C8: passphrase-encrypted backup and restore
- [ ] Prompt C9: reverse-proxy provisioning (SWAG / nginx)
- [ ] Prompt C10: DNS and TLS (DuckDNS auto, Cloudflare semi, others documented)
- [ ] Prompt C11: Tailscale Serve for private remote access
- [ ] Prompt C12: docs supersession, user setup guide rewrite, full verification

---

## Prompt C1 — Fold-in, package structure, command grammar, and version unification

**Depends on:** the full `PROMPTS-deployment-modes.md` set (shipped). Nothing else in this file.

**Reference:** `docs/PLAN-deployment-modes.md` Section 7 ("The CLI is the primary interface", "What stays out of scope here") and the Section 8 open item for the fold-in and version schemes.

**Current state:** two projects. The app repo `job-squire/` versions `0.1.0-<sha>`. The `jobsquire-cli/` project is an MCP-query wrapper (`click` + `rich`, hatchling, entry point `jobsquire = jobsquire.cli:main`, version `0.1.0+<sha>`). The app repo already carries `install.sh`, `update.sh`, and `uninstall.sh` shell scripts whose logic is to be absorbed into CLI subcommands over the following prompts.

**Goal:** establish one CLI package inside the `job-squire` repo that exposes a deployment front door and the existing query commands as two command groups, decoupled from Hermes, with the command name and versioning settled before any behavior is built.

**Do this:**

1. Create one CLI package in the `job-squire` repo (for example a `job_squire_cli` package with its own `pyproject.toml`, matching the repo's layout norms). Move the existing `jobsquire/cli.py` query commands into it, keeping their observable behavior identical.
2. Decouple the query commands from Hermes. Replace the `~/.hermes/mcp_client.py` sidecar dependency with a self-contained MCP client built on the standard protocol: reuse the `mcp` library the server already depends on, or write a minimal direct Streamable HTTP client in the package. Do not require Hermes to be installed and do not vendor or copy any Hermes code. Read the instance's MCP endpoint and token from the CLI's own config (the plumbing built in C6), not from a Hermes token store. Hermes stays able to use our MCP server by reading our published MCP docs; the CLI does not reach into Hermes at all.
3. Make the query group's dependencies optional. Put the query surface behind an extras group (for example `job-squire[query]`) and lazy-import its modules, so a deployment-only user does not pull in the query stack and the ops commands never require a live MCP endpoint.
4. Settle the command grammar and record it in one place:
   - Deployment/lifecycle group (new): `create`, `start`, `stop`, `restart`, `status`, `list`, `update`, `remove`, `configure`, `backup`, `restore`.
   - Query group (existing): `health`, `list` (pipeline stage listing), `pipeline`, `contacts`, `job`, `contact`, `followups`. Resolve the `list` collision (deployment lists instances, query lists jobs) by namespacing the query commands under their own group, so the two `list`s never clash.
5. Wire the console entry points: `job-squire` is the canonical command; `jobsquire` stays wired as an alias to the same entry point so existing usage keeps working.
6. Unify versioning: pick one scheme for the merged package and convert both sources to it, resolving the `0.1.0-<sha>` vs `0.1.0+<sha>` split. Record the rule in `docs/` and update `CLAUDE.md`'s versioning note so the two schemes are no longer described as separate.
7. Set up packaging and CI for the merged package (build, lint, test) alongside the app's existing pipeline. Carry over the SBOM generation the CLI project already had.

**Constraints:** this prompt is structural. Do not add lifecycle behavior yet; later prompts do. The query commands must keep working against a live instance over MCP with no Hermes present and nothing from Hermes bundled.

**Verify before committing:**

- `pip install -e` the merged package with and without the `[query]` extra; the deployment group works without the query dependencies installed, and the console entry point runs as both `job-squire` and `jobsquire`.
- With Hermes absent from the machine (no `~/.hermes/`), the query commands still function against a running instance using the self-contained client (smoke test `health` and one list command).
- Lint and any moved tests pass; the version reports the unified scheme.

**Commit:** `REFACTOR: fold jobsquire-cli into one Hermes-independent CLI package`.

---

## Prompt C2 — Bootstrap one-liner

**Depends on:** C1 (the installable CLI package and its entry point exist).

**Reference:** `docs/PLAN-deployment-modes.md` Section 6 ("The bootstrap one-liner", "Detect first, install only if needed").

**Goal:** a single command that downloads and installs the `job-squire` CLI from the official repository and then launches it, defaulting to the latest release and accepting a pinned version. Nothing else is a separate installer.

**Do this:**

1. Add `bootstrap.sh` (macOS and Linux) and `bootstrap.ps1` (Windows) at the repo root. Each installs the CLI from `github.com/dellipse/job-squire` and then invokes it to drive setup. The one-liner installs the CLI and nothing else.
2. Default to the latest released version. Honor a pin via `JOBSQUIRE_VERSION=<version>` (shell) and `$env:JOBSQUIRE_VERSION="<version>"` (PowerShell).
3. Resolve and verify the version from GitHub releases before installing. Fail clearly if the requested version does not exist. State how integrity is checked (checksum or signature) in a comment.
4. After install, launch the CLI so the user lands in setup, on platforms where that is possible; otherwise print the exact next command.

**Constraints:** the bootstrap is the only script with its own logic; every later step is a CLI subcommand. Do not reintroduce `install.sh`-style branching here.

**Verify before committing:**

- On macOS or Linux, run the one-liner end to end in a scratch environment and confirm the CLI installs and launches, with both the latest default and a pinned version.
- Confirm a bogus `JOBSQUIRE_VERSION` fails with a clear message and installs nothing.

**Commit:** `NEW: bootstrap one-liner to install and launch the job-squire CLI`.

---

## Prompt C3 — Container runtime detection and install

**Depends on:** C1 (CLI package). Bootstrap (C2) hands off to this during setup.

**Reference:** `docs/PLAN-deployment-modes.md` Section 6 (all subsections) and the resolved licensing open item in Section 8.

**Goal:** detect a working runtime and reuse it; only when none is present, install the per-OS default with consent. Record the choice for each instance.

**Do this:**

1. Detection: look for `docker`, `podman`, `orbstack`, and `colima` and confirm one actually runs. If a working runtime is found, use it and install nothing.
2. Per-OS install defaults when none is present, installing only with consent:
   - Linux: Podman rootless (read `/etc/os-release` for the package path); Docker Engine only if already present. Never Docker Desktop on a server.
   - macOS: Podman machine, CLI-automated (script the `podman machine` setup). OrbStack offered as an explicit opt-in with its commercial-license terms shown at that moment.
   - Windows: Podman on WSL2, CLI-automated. Docker Desktop as the graceful fallback. Check for WSL2 first and guide `wsl --install` plus reboot if missing.
3. Licensing awareness: because Podman is the default everywhere, setup never asks about company size and never steers anyone toward a paid product. State OrbStack's and Docker Desktop's thresholds only at their point of choice.
4. Record the selected or detected runtime so later commands drive the right one without re-detecting (this is the `runtime` field written into the registry in C4).
5. Keep manual Docker instructions documented for operators who prefer or require Docker; detect-and-reuse means an existing Docker install is simply used as-is.

**Constraints:** never install a runtime over one that already works. Consent is required before any install.

**Verify before committing:**

- On a machine with an existing runtime, detection reuses it and installs nothing.
- On a clean macOS or Linux environment (or a VM), the default install path completes and produces a working runtime, and the chosen runtime is recorded.
- The Windows path detects a missing WSL2 and guides enabling it (validate the check even if you cannot fully run it).

**Commit:** `NEW: container runtime detection and per-OS install with consent`.

---

## Prompt C4 — Cross-platform instance registry

**Depends on:** C1 (CLI package), C3 (writes the `runtime` field).

**Reference:** `docs/PLAN-deployment-modes.md` Section 4 (all of it) and the registry shape shown there.

**Goal:** a per-user registry the CLI owns, recording non-secret instance metadata, as the source of truth for lifecycle.

**Do this:**

1. Store the registry at the conventional per-user config location per OS: `~/Library/Application Support/job-squire/instances.json` on macOS, `~/.config/job-squire/instances.json` on Linux (honor `XDG_CONFIG_HOME`), `%APPDATA%\job-squire\instances.json` on Windows.
2. Implement read, write, add, update, and remove with the schema from Section 4: `version`, then per instance `name`, `mode`, `runtime`, `data_dir`, `app_port`, `mcp_port`, `cookie_name`, `public_url`, `created`. Never store `SECRET_KEY` or any other secret in the registry.
3. Enforce name rules: sanitize to a safe slug (lowercase, alphanumeric and hyphen), reject a name that collides with an existing instance. The name deterministically drives the data directory, cookie name (`<name>_session`), compose project name (`job-squire-<name>`), and port pair or hostname.
4. Add a divergence check the later `status` command uses: compare the registry against what is actually running and report drift (renamed container, changed ports, deleted volume). Provide a reconcile path.

**Constraints:** the registry holds only non-secret metadata. It is per OS user, so two logins keep separate lists.

**Verify before committing:**

- Unit tests: slug sanitization, collision rejection, round-trip read/write, and that no secret field is ever serialized.
- Create two fake instance records and confirm derived values (cookie name, compose project) are deterministic and unique.

**Commit:** `NEW: cross-platform instance registry`.

---

## Prompt C5 — Instance lifecycle core

**Depends on:** C3 (runtime), C4 (registry), and the app set's `docker-compose.single.yml`, `DEPLOY_MODE`/`TRUST_PROXY` resolution, and startup safety guard.

**Reference:** `docs/PLAN-deployment-modes.md` Section 7 ("Instance lifecycle operations", "Direct runtime access remains available", "Surfacing failures").

**Goal:** the core lifecycle commands, driving whichever runtime the instance was created with, while leaving the containers fully manageable by native tools.

**Do this:**

1. `create`: run setup end to end. Choose mode (`local` or `network`), name the instance (slug + collision check via C4), offer to import basic non-secret settings from an existing instance (search titles, location, radius, schedule hours and timezone, enabled providers by name, SMTP host and port, AI provider selection, interface preferences), with secrets excluded by default and an explicit opt-in to also copy provider keys. Generate a fresh random `SECRET_KEY`. Allocate the next free web and MCP port pair in local mode (record it), or set the hostname in network mode. Write the instance's `data/.env` and its `docker-compose.single.yml` from the app set. Register it in C4. Bring it up on the recorded runtime.
2. `start` / `stop` / `restart`: bring the instance's single container up or down through the recorded runtime, translating to that runtime's compose invocation (`docker compose`, `podman compose`, and their differences stay hidden).
3. `status` / `list`: show each registered instance and its health using the aggregated container healthcheck from the app set, and report any registry-vs-reality divergence from C4.
4. `remove`: tear down the instance, update the registry, and ask whether to keep or delete that instance's data directory so history is never destroyed silently.
5. Direct runtime access: generate the compose and env into a known per-instance location and name containers clearly from the instance name (`job-squire-<name>`), so an operator can run native `docker`/`podman` compose commands directly. Document the division of labor: read-only and operational commands are safe to run directly; structural changes should go through the CLI, and `status` reports drift if they do not.
6. Surfacing failures: when an instance refuses to start because of the app's startup safety guard, catch the non-zero exit and reprint the same reason and fix the app wrote, rather than a generic container error.

**Constraints:** the operator never needs a raw container command, but nothing prevents them from using one. The CLI is primary, not exclusive.

**Verify before committing:**

- Create a local instance end to end; confirm it registers, comes up healthy, and prints only `localhost`/`127.0.0.1` links.
- Create a second local instance and confirm distinct ports and cookie names, with no collision.
- Force an unsafe network config and confirm `create`/`start` surfaces the guard's exact reason and fix on the command line.
- Stop, start, and remove an instance; confirm the keep-or-delete-data prompt on remove.

**Commit:** `NEW: instance lifecycle core (create, start, stop, status, remove)`.

---

## Prompt C6 — MCP authentication and token-config plumbing

**Depends on:** C5 (an instance and its registry entry exist), and the app set's `jsq_mcp_` static-token implementation and in-app token management (Prompt 6 of `PROMPTS-deployment-modes.md`).

**Reference:** `docs/PLAN-deployment-modes.md` Section 5 ("MCP authentication") and Section 7's provisioning touchpoints.

**Why this sits here:** the query command group from C1 needs a real MCP endpoint and token to talk to a running instance. Building that plumbing now, right after instances can be created, means the query surface is wired end to end early instead of waiting until the end of the set. Tailscale (C11) later consumes the same plumbing.

**Goal:** give the CLI first-class management of each instance's MCP authentication, and persist the endpoint and token where the query group reads them, with no Hermes token store involved.

**Do this:**

1. OAuth stays the default MCP flow in every mode. Nothing is generated for OAuth; where a browser flow is available, the query group can use an OAuth access token. Document that OAuth is preferred whenever an instance is reachable beyond the one machine.
2. Local static token: add `configure` support to generate the `jsq_mcp_` static bearer token by calling the app-side generate/rotate/revoke that Prompt 6 built (256-bit, URL-safe base64, Fernet-encrypted at rest, constant-time compared, loopback-only unless explicitly enabled). Support generate, rotate (invalidate the previous value), revoke, and an optional TTL, mirroring the in-app settings controls. Exactly one active token at a time.
3. Token-config plumbing: persist each instance's MCP endpoint (derived from the registry `public_url`/`mcp_port` or the network hostname) and a reference to its token in the CLI's own per-user config location, alongside but separate from the registry (the registry never holds secrets). The query group reads the endpoint and token from here. This is the plumbing C1 stubbed; make it real now.
4. Reachability rule: the CLI only offers and stores the static token for a loopback-reachable instance unless the operator explicitly enables it elsewhere, matching the resolved deployment posture from the app set. Never enable it on a network instance implicitly.

**Constraints:** the static token is local-only by default and off unless enabled. Do not require or read any Hermes token store. Keep OAuth untouched as the default flow.

**Verify before committing:**

- Create a local instance (C5), generate a token with `configure`, and run a query command (for example `health`) end to end with no manually supplied token, confirming the query group reads the endpoint and token from the CLI config.
- Rotate the token and confirm the previous value stops working; revoke it and confirm the query command then fails cleanly.
- Confirm the static token is refused for a network-reachable instance unless explicitly enabled.

**Commit:** `NEW: CLI MCP auth and token-config plumbing`.

---

## Prompt C7 — Update, rollback, and adopt existing data

**Depends on:** C5 (lifecycle core), and the app set's `adopt` helper and additive boot-time migrations.

**Reference:** `docs/PLAN-deployment-modes.md` Section 7 ("Instance lifecycle operations": Update) and Section 8 ("Adopting existing data").

**Goal:** safe version movement and a first-class adopt command that turns an existing data directory into a registered, single-container instance.

**Do this:**

1. `update`: pull the target image version, recreate the container, and rely on the app's additive boot-time migrations to carry the schema forward. Default to latest, accept a pinned version, and support rolling back to a previous version. Shutdown is WAL-safe because s6 forwards `SIGTERM`; confirm the update path never kills the container mid-write.
2. `adopt`: wrap the app set's adopt helper into a CLI command. Given an existing data directory and `.env`, derive the instance name and cookie name from the current `INSTANCE_NAME`, keep the existing `SECRET_KEY` so stored secrets stay decryptable, register the instance in C4, and generate its single-container compose. Existing environment variables continue to be honored; adoption is additive, not a rewrite.
3. After adopt, offer to bring the instance up on the single-container image and verify health, so the operator moves off the legacy three-container topology in one guided step.

**Verify before committing:**

- Update an instance to a pinned version and roll it back; confirm the DB migrates forward and the rollback comes up clean, with no WAL corruption on either transition.
- Adopt a copy of a realistic existing data directory; confirm the instance registers, the derived cookie name matches, a previously stored encrypted secret still decrypts, and the single container is healthy.

**Commit:** `NEW: update, rollback, and adopt-existing-data commands`.

---

## Prompt C8 — Passphrase-encrypted backup and restore

**Depends on:** C4 (registry), C5 (lifecycle), C7 (adopt/restore share the recreate path).

**Reference:** `docs/PLAN-deployment-modes.md` Section 7 ("Backup and restore") and the resolved KDF/cipher open item in Section 8.

**Goal:** one portable, mandatory-encrypted archive per instance, and a faithful restore.

**Do this:**

1. `backup`: produce a single self-contained archive of one instance in the user's home folder, `.tgz` by default with a `.zip` option, named for the instance and a UTC timestamp (for example `job-squire-castelo-20260711T1830Z.tgz`). Include the entire data directory verbatim, including files the app does not manage. Capture is WAL-safe: checkpoint the SQLite write-ahead log first, reusing the existing WAL-safe backup approach. Support an option to back up every registered instance in one run.
2. Manifest: include `backup-manifest.json` with a backup-format version, the timestamp, the instance's full registry entry, the image version, the schema/migration point, the CLI version, and checksums.
3. Encryption is mandatory because the archive necessarily contains the instance's `SECRET_KEY` and OAuth token store (without the `SECRET_KEY`, stored provider and SMTP secrets cannot be recovered). Never write an unencrypted archive to disk. Stretch the user-supplied passphrase with Argon2id and seal the archive with AES-256-GCM, storing a random salt, nonce, and the Argon2id parameters in a small archive header. Both primitives come from the `cryptography` library the app already depends on; add no new dependency. Set restrictive file permissions. State plainly at backup time that a lost passphrase means a lost backup.
4. `restore`: prompt for the passphrase and decrypt, failing clearly on a wrong passphrase; verify checksums and format compatibility; unpack the data directory; restore the env including the `SECRET_KEY`; re-register the instance from the manifest; keep or reallocate ports and hostname as appropriate for the target machine; ensure a runtime is available; and bring the container up on a compatible image, letting additive migrations carry the schema forward if the target is newer. If an instance of the same name exists, prompt to rename or overwrite rather than clobbering silently.

**Constraints:** encryption is not optional. `age` is noted only as a possible portability alternative, not a dependency.

**Verify before committing:**

- Back up an instance, confirm no plaintext archive is ever written, and that the archive header carries salt, nonce, and Argon2id parameters.
- Restore on a fresh machine (or a clean scratch dir): wrong passphrase fails clearly; correct passphrase restores; stored secrets decrypt; the instance comes up healthy; a name collision triggers the rename/overwrite prompt.
- Run the all-instances backup option and confirm one archive per instance.

**Commit:** `NEW: passphrase-encrypted backup and restore (Argon2id + AES-256-GCM)`.

---

## Prompt C9 — Reverse-proxy provisioning

**Depends on:** C5 (network-mode instances exist), and the `examples/nginx/` templates in the app repo.

**Reference:** `docs/PLAN-deployment-modes.md` Section 5 ("Network mode: the proxy is the boundary", "Optional proxy provisioning") and Section 7's provisioning touchpoints.

**Goal:** for network mode, either configure an existing proxy or install and configure SWAG, without ever replacing the app's own TLS stance (the app always speaks plain HTTP to the proxy).

**Do this:**

1. Existing proxy: if the machine already runs SWAG or another nginx-based proxy, generate the Job Squire web and MCP host configurations from `examples/nginx/`, drop them into the proxy's config directory, and reload the proxy. Do not install a second proxy.
2. No proxy: install and run a LinuxServer SWAG container, then generate and install the Job Squire configuration into it. SWAG bundles nginx, certbot, and fail2ban.
3. Keep the boundary intact: the proxy remains a separate, independently maintained component that is not part of the Job Squire image. TLS terminates at the proxy.

**Constraints:** the CLI automates proxy setup as a convenience only. Network mode is still not considered configured without a working proxy in front, which the startup guard already enforces.

**Verify before committing:**

- Against an existing nginx/SWAG proxy in a test environment, generate and load the config and confirm the reload succeeds and routes to the instance.
- With no proxy present, install SWAG, install the config, and confirm the instance is reachable through it over HTTPS once DNS and TLS from C10 are in place.

**Commit:** `NEW: reverse-proxy provisioning for network mode`.

---

## Prompt C10 — DNS and TLS

**Depends on:** C9 (SWAG is present or configured).

**Reference:** `docs/PLAN-deployment-modes.md` Section 5 ("Free and low-cost domain and DNS options") and the resolved auto-configure-versus-document open item in Section 8.

**Goal:** auto-configure the two settled paths and semi-automate the third; document the rest.

**Do this:**

1. DuckDNS (fully automated, the guided network default): collect the subdomain and token, put SWAG into DuckDNS mode, and obtain the Let's Encrypt certificate. Note the DuckDNS tradeoff (main subdomain via HTTP validation or a wildcard via DNS validation, not both at once).
2. Cloudflare DNS-01 (semi-automated): when the operator brings their own domain and API token, write the SWAG Cloudflare configuration and issue the wildcard certificate. The one manual input is the domain and token the operator supplies.
3. Documented only: Cloudflare Tunnel and other SWAG DNS plugins, because they use a different topology or a long tail of provider-specific setup. Provide clear docs, not automation.
4. Be explicit that the CLI cannot conjure a domain and working DNS; TLS still depends on the operator supplying a hostname and a validation path. This applies to network mode only; a local install needs none of it.

**Verify before committing:**

- In a test environment with a real DuckDNS subdomain, run the automated path and confirm a valid certificate is issued and the instance serves HTTPS.
- With a domain and Cloudflare token, run the semi-automated path and confirm the wildcard certificate issues.
- Confirm the documented-only providers are clearly written up and not wired to automation.

**Commit:** `NEW: DNS and TLS provisioning (DuckDNS automated, Cloudflare DNS-01 semi-automated)`.

---

## Prompt C11 — Tailscale Serve for private remote access

**Depends on:** C5 (lifecycle), and C6 (MCP auth and token-config plumbing, whose reachability rules this consumes).

**Reference:** `docs/PLAN-deployment-modes.md` Section 5 ("Reaching a local instance from your own devices (Tailscale)") and Section 7's touchpoints.

**Goal:** a private remote-access path for a local instance, without any public exposure.

**Do this:**

1. Tailscale Serve: for a local instance that wants private remote access, set up Tailscale Serve (never Funnel) in front of the loopback service. Serve terminates real HTTPS with a `device.tailnet.ts.net` certificate and forwards to `127.0.0.1`; the app never leaves loopback. Flip that instance to the network-mode application flags for those sessions (secure cookies on, `TRUST_PROXY` on, `PUBLIC_URL` set to the `ts.net` name), and set `PUBLIC_MCP_URL`/`PUBLIC_MCP_HOST` to the same name. Keep the app bound to loopback so Serve is the only way in. No ports forwarded, nothing published publicly.
2. MCP over the tailnet: because a Tailscale-reachable instance is reachable beyond the one machine, prefer OAuth there over the static token, applying the reachability rule from C6. If the operator still wants the static token on a tailnet-reachable instance, it must be the same explicit opt-in C6 defines, never implicit.

**Constraints:** Serve not Funnel; the app stays on loopback. Do not weaken C6's loopback-only default for the static token.

**Verify before committing:**

- Enable Tailscale Serve for a local instance and reach it from a second tailnet device over HTTPS with a valid certificate, while confirming the app is still bound to loopback and nothing is publicly exposed.
- Confirm a tailnet-reachable instance prefers OAuth and that the static token is offered there only via the explicit C6 opt-in.

**Commit:** `NEW: Tailscale Serve for private remote access`.

---

## Prompt C12 — Documentation supersession, user setup guide rewrite, and full verification

**Depends on:** C1 to C11 (the whole CLI exists).

**Reference:** `docs/PLAN-deployment-modes.md` Section 8 ("Documentation that this supersedes") and the suggested sequencing item 5.

**Goal:** retire the docs this design supersedes, rewrite the user setup guide around the one-line bootstrap and the three modes, and prove the whole system passes.

**Do this:**

1. Replace `deployment.md` with the network-mode and proxy-provisioning material plus the CLI runbook. Replace `multi-instance.md` with the instance model and registry. Absorb `backup-restore.md` into the CLI backup and restore operation (the single encrypted archive). Update `configuration.md` and `architecture.md` for anything the CLI changes. Retire the old `install.sh`/`update.sh`/`uninstall.sh` docs now that their logic lives in CLI subcommands.
2. Rewrite the user setup guide around the one-line bootstrap, the `job-squire` CLI, and the three deployment modes, written for a general audience and a non-technical job seeker, not a named individual.
3. Remove the legacy three-container compose once single-container is proven in practice, per the Section 8 decision, leaving the maintenance-only `job-tracker` repo untouched.

**Verify before committing:**

- Run the full app and CLI test suites; confirm they pass and coverage clears the CI floors.
- Do one clean end-to-end run of the documented setup guide on a fresh environment: bootstrap, create a local instance, open it, run an MCP call with the static token, back it up, restore it, and remove it.
- Do one network-mode dry run through the guide (DuckDNS path) far enough to confirm the instructions match the CLI's real behavior.

**Commit:** `DOCS: supersede legacy deployment docs and rewrite the user setup guide`.

---

## Cross-references back to the app set

These CLI prompts consume work delivered by `PROMPTS-deployment-modes.md`:

- The single-container image and `docker-compose.single.yml` (Prompts 1 and 2) are what `create` generates and `start` drives.
- The aggregated healthcheck (Prompt 2) backs `status`.
- `DEPLOY_MODE` and `TRUST_PROXY` (Prompt 4) are what `create` writes into each instance's env.
- The startup safety guard (Prompt 5) is what C5's "surfacing failures" reprints on the command line.
- The `jsq_mcp_` static token and its in-app management (Prompt 6) are what C6 generates, rotates, and revokes, and what C11 governs over the tailnet.
- The adopt helper (Prompt 7) is what C7's `adopt` command wraps.

If any of those are not yet in place, stop and finish `PROMPTS-deployment-modes.md` first.
