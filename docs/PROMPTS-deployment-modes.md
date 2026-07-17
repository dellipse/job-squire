# Implementation Prompts: Deployment Modes and Single-Container Design

**Companion to:** `docs/PLAN-deployment-modes.md` (the design of record, 2026-07-11).
**How to use:** run these prompts one at a time in Claude Code on a machine with a working container runtime (Podman, Docker, OrbStack, or Colima). Each prompt is self-contained and assumes every earlier prompt has already landed and been committed. Do not skip ahead; later prompts depend on files and behavior introduced earlier.

**Scope of this file:** the parts of the design that live in the `job-squire` repo and can be built and tested now. That is the single-container image (Section 2), the configuration model and startup guard (Section 3), the app-side MCP static token (Section 5), and the adopt-existing-data path (Section 8). The full `job-squire` CLI (Sections 6 and 7) and the provisioning automation (SWAG, DuckDNS, Tailscale) are deferred to the dedicated CLI session and are **not** covered here.

**Conventions every prompt must follow (from `CLAUDE.md`):**

- Commit style `TYPE: Short description` (`NEW:`, `FIX:`, `REFACTOR:`, `DOCS:`).
- Schema changes are additive `ALTER TABLE` statements in `_run_migrations()` in `app/__init__.py`. No Flask-Migrate.
- All stored secrets are Fernet-encrypted via `app/crypto.py`. Never plaintext.
- No inline JavaScript; the CSP blocks it. All JS lives in `app/static/app.js`.
- Deployment-specific values come from `os.environ.get()`, never hardcoded.
- Keep the two-user-per-instance, single-tenant stance. Do not add multi-tenancy.

**Progress checklist:**

- [x] Prompt 1: single-container image on LinuxServer Alpine + s6
- [x] Prompt 2: aggregated healthcheck + single-container compose (legacy kept)
- [x] Prompt 3: multi-architecture CI build
- [x] Prompt 4: `DEPLOY_MODE` preset and granular flags
- [x] Prompt 5: startup safety guard with three surfacing channels
- [x] Prompt 6: app-side MCP static token hardening
- [x] Prompt 7: adopt-existing-data path and backward-compat regression
- [x] Prompt 8: docs update and full-suite verification

---

## Prompt 1 — Single-container image on LinuxServer Alpine + s6-overlay

**Depends on:** nothing. This is the first change.

**Reference:** `docs/PLAN-deployment-modes.md` Section 2, and the migration notes in Section 8.

**Current state to be aware of:** `Dockerfile` uses `python:3.14-slim`, runs one process (gunicorn on `wsgi:app`) as a non-root user created with `groupadd`/`useradd` (glibc tools), and has a single web `/health` HEALTHCHECK. The scheduler and MCP server run today as two additional compose services (`app.worker` and `app.mcp_server`) off the same image.

**Goal:** rebuild the image so one container runs all three processes under s6-overlay as PID 1, on the LinuxServer Alpine base. Application logic does not change.

**Do this:**

1. Rewrite `Dockerfile` to build `FROM ghcr.io/linuxserver/baseimage-alpine:<pinned-dated-tag>` on the Alpine 3.23 line. Pin an exact dated tag, not a floating one, because these images have no `latest`. Install Python with `apk add --no-cache python3 py3-pip` and confirm the base ships Python 3.12. Install `requirements.txt` against that interpreter. Keep `DATA_DIR=/data`, `BUILD_VERSION` build arg, and the `VOLUME`/`EXPOSE` intent.
2. Set `ENV LSIO_FIRST_PARTY=false` so LinuxServer init does not overwrite our banner.
3. Add the branding file at `/etc/s6-overlay/s6-rc.d/init-adduser/branding` using the ASCII art in Section 2 of the design, verbatim.
4. Move from the `useradd`/`USER` model to the LinuxServer `PUID`/`PGID`/`UMASK` convention. The app process runs as the non-root user, and `/data` stays owned by that user across updates. Preserve the existing `PUID`/`PGID` build-arg defaults of 1000.
5. Define three s6 longrun services under `/etc/s6-overlay/s6-rc.d/`:
   - `web`: `gunicorn --bind 0.0.0.0:8000 --workers 2 --timeout 60 --access-logfile - --error-logfile - wsgi:app`. This service owns first-boot DB init, migrations, and seeding (it already does via `create_app`).
   - `worker`: `python -m app.worker`. Exactly one process so each scheduled slot fires once. Depends on `web`.
   - `mcp`: `python -m app.mcp_server`. Depends on `web`.
   Express the dependencies so `worker` and `mcp` start after `web`, matching today's compose `depends_on: service_healthy`.
6. Pin `pydantic` and `pydantic-core` to exact versions in `requirements.txt` so the musl wheel resolution stays deterministic.
7. Leave the existing three-container `docker-compose.yml` in place for now. Prompt 2 introduces the single-container compose and keeps the legacy one as a fallback.

**Constraints:** do not change any Python in `app/`. Signals must reach each service so SQLite WAL shuts down cleanly; that is the whole reason for s6 rather than a shell script that backgrounds three processes.

**Verify before committing:**

- `docker build` (or `podman build`) succeeds on your host architecture with no source builds during pip install (confirm every wheel is a binary `musllinux` wheel; fail the prompt if `cryptography`, `lxml`, `pydantic-core`, `greenlet`, or `sqlalchemy` compiles from source).
- Run the image with a throwaway `/data` bind mount and a test `SECRET_KEY` and `ADMIN_PASSWORD`. Confirm all three processes are up under s6 (`ps` inside the container), the branding banner prints, `GET http://localhost:8000/health` returns 200, and `GET http://localhost:9000/health` returns 200.
- `docker stop` the container and confirm from the logs that each service received `SIGTERM` and shut down cleanly (no WAL corruption warnings on the next start).

**Commit:** `NEW: single-container image on LinuxServer Alpine + s6-overlay`.

---

## Prompt 2 — Aggregated healthcheck and single-container compose

**Depends on:** Prompt 1 (the s6 image exists and boots all three services).

**Reference:** `docs/PLAN-deployment-modes.md` Section 2 ("Health and observability") and Section 8.

**Current state to be aware of:** healthchecks live per-service in `docker-compose.yml`: a web `/health` check, a worker `.worker_heartbeat` file check, and an MCP `/health` check on `MCP_PORT`. The image-level HEALTHCHECK in `Dockerfile` only probes web `/health`.

**Goal:** one container-level healthcheck that passes only when all three internal probes pass, and a single-container compose file that supersedes the three-service one without deleting it yet.

**Do this:**

1. Replace the image-level HEALTHCHECK in `Dockerfile` with an aggregated check that passes only if all three are healthy: web `/health` on 8000, MCP `/health` on `MCP_PORT` (default 9000), and the worker liveness check, which stays the existing `.worker_heartbeat` freshness test (same threshold logic used in today's compose worker healthcheck). Keep it pure Python so no extra packages are needed.
2. Add a new `docker-compose.yml` that runs the one image as a single service with the aggregated healthcheck, the `data/.env` env_file, the `${DATA_HOST_DIR}:/data` bind mount, and the `PUID`/`PGID`/`UMASK` passthrough. Publish `8000` and `MCP_PORT` on the host per the current port variables (`APP_HOST_PORT`, `MCP_HOST_PORT`), bound to loopback exactly as the current compose does.
3. Keep the existing three-container `docker-compose.yml` unchanged as the documented fallback for anyone who wants component isolation during migration. Add a short comment at the top of each compose file pointing to the other.

**Verify before committing:**

- Bring the stack up with `docker compose -f docker-compose.yml up -d`. Confirm the container reports `healthy` only after all three probes pass, and that killing the worker process inside the container (to stale the heartbeat) flips the container to `unhealthy`.
- Confirm the legacy `docker-compose.yml` still stands up the three-container topology unchanged.

**Commit:** `NEW: aggregated container healthcheck and single-container compose`.

---

## Prompt 3 — Multi-architecture image build in CI

**Depends on:** Prompt 1 (the Alpine/s6 Dockerfile is what gets built).

**Reference:** `docs/PLAN-deployment-modes.md` Section 2 ("Multi-architecture build") and Section 8.

**Current state to be aware of:** `.github/workflows/ci.yml` already uses `docker/setup-buildx-action@v4` and `docker/build-push-action@v7` but builds a single architecture to GHCR. There are two build-push steps (a PR build and a main publish), both passing `BUILD_VERSION`.

**Goal:** build and publish a multi-arch image for `linux/amd64` and `linux/arm64` so Intel and ARM hosts pull the right variant automatically.

**Do this:**

1. Add `docker/setup-qemu-action` before buildx so the runner can emulate the non-native architecture.
2. Add `platforms: linux/amd64,linux/arm64` to the publish `build-push-action` step. Keep `BUILD_VERSION` and the existing tags.
3. For the PR/non-push build step, decide and document whether it builds both platforms (slower, catches arch issues early) or stays single-arch for speed. Recommend both platforms with `push: false`. State the choice in a comment.
4. Confirm the SBOM and image-signing/scan steps still operate on the published multi-arch image (the manifest list), not just one platform. Adjust if a step assumed a single-platform digest.

**Verify before committing:**

- Locally, `docker buildx build --platform linux/amd64,linux/arm64 .` completes for both platforms with no source builds on either (the musl wheels must exist for both arches).
- Confirm the CI job builds green. After a publish, `docker buildx imagetools inspect ghcr.io/dellipse/job-squire:latest` should show a manifest list with both `amd64` and `arm64` entries.

**Commit:** `NEW: multi-architecture image build for amd64 and arm64`.

---

## Prompt 4 — `DEPLOY_MODE` preset and granular flags

**Depends on:** Prompts 1 to 3 give a buildable single-container image; this prompt is pure Python and can be tested with `python wsgi.py` even without rebuilding the image.

**Reference:** `docs/PLAN-deployment-modes.md` Section 3 (all of it) and the granular-flags table.

**Current state to be aware of:** `app/__init__.py::create_app` sets `SESSION_COOKIE_SECURE` from `_bool_env("SESSION_COOKIE_SECURE", True)` and **always** applies `ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)` unconditionally. The cookie name is already derived from `INSTANCE_NAME`. There is no `DEPLOY_MODE` and no `TRUST_PROXY`.

**Goal:** introduce a `DEPLOY_MODE` preset that expands into granular flags, where the running code reads only the granular flags and never branches on the mode string. This must not repeat the legacy `AIConfig.mode` mistake.

**Do this:**

1. Add a small preset table in `app/__init__.py` (or a dedicated `app/deploy.py` helper imported by the factory) mapping `local` and `network` to defaults for at least `trust_proxy` and `secure_cookie`. Default `DEPLOY_MODE` to `local`.
2. Resolve each granular flag with the precedence from the design: an explicitly set environment variable wins; if unset, the preset default for the current mode fills it; the mode string is consulted only to pick the preset table.
   - `TRUST_PROXY` (new): local default `0`, network default `1`.
   - `SESSION_COOKIE_SECURE` (existing): local default `false`, network default `true`. Preserve today's behavior for anyone who sets it explicitly.
3. Make `ProxyFix` conditional: apply it only when the resolved `trust_proxy` is true. On loopback with `trust_proxy` off, `ProxyFix` must not be applied, so forwarded headers cannot be spoofed.
4. Keep the cookie-name derivation from `INSTANCE_NAME` exactly as it is.
5. Update `examples/.env.example` to document `DEPLOY_MODE` and `TRUST_PROXY`, including the local vs network defaults and the rule that they exist mostly so the eventual CLI sets them for the user.

**Constraints:** the two genuinely new variables are `DEPLOY_MODE` and `TRUST_PROXY`. Everything else already exists; give it mode-aware defaults rather than adding new knobs. Existing installs with neither variable set must keep their current effective behavior (see the precedence: an existing `.env` that sets `SESSION_COOKIE_SECURE=true` still gets secure cookies).

**Verify before committing:**

- Add unit tests that assert the precedence table: explicit env overrides win; unset flags take the mode preset; `local` yields `trust_proxy=0`/`secure_cookie=false` and `network` yields `trust_proxy=1`/`secure_cookie=true`.
- Add a test that `ProxyFix` wraps `wsgi_app` only when `trust_proxy` is true.
- Run the existing suite; nothing should regress.

**Commit:** `NEW: DEPLOY_MODE preset with granular trust_proxy and secure_cookie flags`.

---

## Prompt 5 — Startup safety guard with three surfacing channels

**Depends on:** Prompt 4 (the granular flags and `DEPLOY_MODE` resolution exist).

**Reference:** `docs/PLAN-deployment-modes.md` Section 3 ("Startup safety guard") and the severity table.

**Current state to be aware of:** the app already has an in-app banner mechanism used for worker-status and staleness warnings. Reuse it; do not build a new banner system.

**Goal:** validate the effective configuration at startup and turn the two dangerous misconfigurations into loud, early, actionable signals, each message naming the offending variable, its current value, why it is unsafe, and the exact fix.

**Do this:**

1. In the app factory, after resolving the granular flags, run a validation pass with two severities:
   - **Fatal (clearly unsafe):** `DEPLOY_MODE=network` but `PUBLIC_URL` is not HTTPS, or `TRUST_PROXY` is not set. The app refuses to start and exits non-zero. Write the reason and fix to the log **and** print them plainly to stderr so the console shows them.
   - **Warning (risky but runnable):** `DEPLOY_MODE=local` but the app is bound to a non-loopback interface with no proxy in front (a plain-HTTP instance exposed to the network). The app starts, writes the message to the log, echoes it to the console at startup, **and** raises a persistent in-app banner that clears itself once the condition is resolved.
2. Route the in-app banner through the existing banner mechanism so this is an extension, not a new system.
3. Ensure the fatal path's non-zero exit and stderr message are shaped so the future `job-squire` CLI can catch the exit and reprint the same reason and fix. Keep the message text in one place so all channels emit identical wording.

**Constraints:** every unsafe condition names the variable, its value, the reason, and the fix. No unsafe condition is ever left to the log alone.

**Verify before committing:**

- Unit tests for each combination: network without HTTPS `PUBLIC_URL` exits non-zero with the right message on stderr; network without `TRUST_PROXY` exits non-zero; local bound to a non-loopback interface starts but registers the warning banner; a correct local and a correct network config both start clean.
- Confirm the banner appears in the running UI for the warning case and clears when the config is fixed.

**Commit:** `NEW: startup safety guard for unsafe deploy configurations`.

---

## Prompt 6 — App-side MCP static token hardening

**Depends on:** Prompts 4 and 5 (mode and guard exist; the token's loopback-only rule leans on the resolved deployment posture).

**Reference:** `docs/PLAN-deployment-modes.md` Section 5 ("MCP authentication") and the resolved token spec in Section 8.

**Current state to be aware of:** `app/mcp_server.py` already supports a static bearer token via a `MCP_API_KEY` env var alongside OAuth 2.0/PKCE, and `ai_config` already has an `mcp_api_key_enc` column (added in `_run_migrations()`). The server currently binds `0.0.0.0`. This prompt formalizes the token into the settled spec and enforces the loopback rule; it does not touch OAuth, which stays the default everywhere.

**Goal:** make the local static MCP token match the settled spec and be safe by construction.

**Do this:**

1. Token shape: 256 bits of cryptographically random data, encoded URL-safe base64, prefixed `jsq_mcp_` so it is recognizable in logs and by secret scanners.
2. Storage: Fernet-encrypted at rest in `ai_config.mcp_api_key_enc` via `app/crypto.py`, like every other secret. Never plaintext, never in the registry.
3. Comparison: constant-time compare on every request (`hmac.compare_digest` or equivalent).
4. Scope: the full MCP tool set for the instance's single user, no per-tool subdivision.
5. Reachability rule: the server accepts the static token only on a loopback bind. On any network-reachable instance it is rejected unless the operator has explicitly enabled it there. Tie "network-reachable" to the resolved deployment posture from Prompt 4, not to a raw guess.
6. Lifecycle: exactly one active token at a time. Rotation regenerates and immediately invalidates the previous value. Record creation and last-used timestamps (additive migration if new columns are needed). No forced expiry by default, but support an optional TTL.
7. In-app management: add settings-page controls to generate, view once, rotate, revoke, and optionally set a TTL for the token. Follow the no-inline-JS rule; put any script in `app/static/app.js`. Keep the existing `MCP_API_KEY` env path working for backward compatibility, or migrate it into the DB-stored token with a clear precedence, and document which wins.

**Constraints:** this is local-only by default and off unless enabled. Do not enable it on a network instance implicitly. Keep OAuth as the untouched default flow.

**Verify before committing:**

- Unit tests: token generated with the `jsq_mcp_` prefix and 256-bit entropy; stored value round-trips through Fernet; constant-time compare accepts the right token and rejects a wrong one; rotation invalidates the old token; a network-reachable bind rejects the static token unless explicitly enabled; last-used timestamp updates on a successful call.
- Manual check: enable the token on a loopback instance, call an MCP tool with it, confirm success and that the timestamp advances; rotate and confirm the old token now fails.

**Commit:** `NEW: harden local MCP static token (jsq_mcp_ prefix, Fernet, loopback-only, rotation)`.

---

## Prompt 7 — Adopt-existing-data path and backward-compat regression

**Depends on:** Prompts 1 to 6 (the new image, config model, guard, and token all exist).

**Reference:** `docs/PLAN-deployment-modes.md` Section 8 ("Adopting existing data").

**Current state to be aware of:** existing installs already have a `/data` directory, an `.env` with a `SECRET_KEY` and `INSTANCE_NAME`, and the three-container topology. The full CLI adopt command is deferred, but the app must adopt cleanly and an operator needs a documented path onto the single-container image without losing data or re-encrypting secrets.

**Goal:** guarantee an existing install moves onto the single-container image with its data and secrets intact, and provide a scriptable adopt helper plus a regression test that proves backward compatibility.

**Do this:**

1. Add an adopt helper under `scripts/` (shell is fine, matching the existing `install.sh`/`update.sh` style) that, given an existing data directory and `.env`, derives the instance name and cookie name from the current `INSTANCE_NAME`, keeps the existing `SECRET_KEY` so stored secrets stay decryptable, and generates a `docker-compose.yml`-based configuration pointed at that data directory. It must be additive: existing environment variables continue to be honored, nothing is rewritten or re-encrypted.
2. Confirm the config model from Prompt 4 preserves current behavior for an existing `.env` that has neither `DEPLOY_MODE` nor `TRUST_PROXY`: it should resolve to the same effective flags the install runs with today (secure cookies as currently set, proxy trust matching today's always-on `ProxyFix` only if the operator was actually behind a proxy). Note any behavior change explicitly and default toward preserving the current install's semantics.
3. Document the adopt steps in `docs/` (a short runbook, superseded later by the CLI): stop the three-container stack, run the adopt helper, start the single-container stack, verify.

**Verify before committing:**

- Regression test: boot the app against a data directory seeded to look like an existing install (populated DB, encrypted provider secret, `INSTANCE_NAME` set, no `DEPLOY_MODE`). Assert it starts, the cookie name matches the derived name, and a previously stored encrypted secret still decrypts with the retained `SECRET_KEY`.
- Manual check: run the adopt helper against a copy of a real data directory and confirm the single container comes up healthy and the login and stored secrets work.

**Commit:** `NEW: adopt-existing-data helper and single-container migration path`.

---

## Prompt 8 — Documentation update and full-suite verification

**Depends on:** Prompts 1 to 7 (everything above has landed).

**Reference:** `docs/PLAN-deployment-modes.md` Section 8 ("Documentation that this supersedes"). Full doc supersession is a later increment; this prompt does the minimum needed to keep shipped behavior documented and runs the whole suite as the closing gate.

**Goal:** make sure the two new env vars and the single-container topology are documented where a user would look, and prove the whole system still passes.

**Do this:**

1. Update `docs/configuration.md` for `DEPLOY_MODE`, `TRUST_PROXY`, and the mode-aware defaults for `SESSION_COOKIE_SECURE` and the cookie name.
2. Update `docs/architecture.md` for the single-container s6 topology (one container, three s6 services, aggregated healthcheck), and note the legacy three-container compose remains as a fallback during migration.
3. Confirm `examples/.env.example` reflects the final variable set from Prompts 4 and 6.
4. Leave the deeper supersession (`deployment.md`, `multi-instance.md`, `backup-restore.md`, and the rewritten user setup guide) to the later increment; add a one-line note in each pointing to `PLAN-deployment-modes.md` so no reader is misled in the interim.

**Verify before committing:**

- Run the full test suite (`pytest`) and confirm it passes, including the new tests from Prompts 4 to 7. Confirm coverage still clears the CI floors.
- Run `ruff` (the repo's linter) clean.
- Build the single-container image once more and boot it end to end (login, a search-config save, an MCP call with the static token, a clean `docker stop`) as a final smoke test.

**Commit:** `DOCS: document DEPLOY_MODE, TRUST_PROXY, and single-container topology`.

---

## What is deliberately not here

These belong to the dedicated `job-squire` CLI session and the later increments, per the design:

- The `job-squire` CLI itself: bootstrap one-liner, runtime detect/install, the cross-platform instance registry, and the lifecycle commands (create, start, stop, update, remove, configure, status).
- Backup and restore as a single passphrase-encrypted archive (Argon2id + AES-256-GCM).
- Provisioning: SWAG install and configuration, DuckDNS as the guided network default, Cloudflare DNS-01 semi-automation, and Tailscale Serve for private local remote access.
- Folding `jobsquire-cli` into this monorepo and unifying the two version schemes.
- Full documentation supersession and the rewritten user setup guide.
