# Adopting an Existing Install onto the Single-Container Image

Short runbook for moving an existing three-container install (`job-squire` +
`job-squire-worker` + `job-squire-mcp`, each in its own container) onto the
single-container image (`docker-compose.single.yml`), with its data and
secrets intact. See `docs/PLAN-deployment-modes.md` Section 2 for why the
single-container image exists and Section 8 for the full migration design.

**The `job-squire` CLI's own `adopt` command now wraps this exact logic**
(`job-squire adopt /path/to/existing/install`, see `docs/job-squire-cli.md`)
— prefer it if you have the CLI installed; it does everything below plus
registering the instance and offering to bring it up, in one guided step.
This doc and `scripts/adopt-single-container.sh` remain as the manual,
scriptable path for anyone not using the CLI, and as a reference for
exactly what the CLI command does under the hood.

---

## Before you start

- Back up first. `./scripts/backup.sh` (see `docs/backup-restore.md`) or a
  cold copy of your data directory. The adopt helper only edits `.env`
  (with its own timestamped backup — see below), but back up the database
  too before touching anything in production.
- Update your checkout: `git pull` (or re-download the release) so
  `docker-compose.single.yml` and `Dockerfile` are the current, s6-based
  versions. The adopt helper checks for `docker-compose.single.yml` and
  refuses to run without it.
- This assumes your existing install already has `data/.env` with a
  `SECRET_KEY`, matching the three-container `docker-compose.yml` layout
  `install.sh` produces. If your layout differs, adjust the paths below.

## The four steps

### 1. Stop the three-container stack

```
docker compose --env-file data/.env -f docker-compose.yml down
```

Nothing should be writing to the database while you switch images.

### 2. Run the adopt helper

```
./scripts/adopt-single-container.sh
```

(Pass a path if you're not running it from the install directory:
`./scripts/adopt-single-container.sh /path/to/install`.)

What it does — see the script's own header comment for the full detail:

- Confirms `data/.env` and `docker-compose.single.yml` both exist.
- Backs up `data/.env` to `data/.env.bak.<timestamp>` before touching it.
- Appends `TRUST_PROXY=1` if not already set. **This is the one setting
  that matters for behavioral parity**: the pre-single-container app
  always trusted the reverse proxy's `X-Forwarded-*` headers
  unconditionally (there was no `DEPLOY_MODE`/`TRUST_PROXY` to turn it
  off). `TRUST_PROXY=1` replicates that exactly, regardless of what
  `DEPLOY_MODE` ends up being. If you're certain this instance has never
  actually sat behind a reverse proxy, you can safely change this to `0`
  afterward for a real security improvement — but the default is chosen to
  never silently change your instance's behavior.
- Appends `SESSION_COOKIE_SECURE=true` if not already set, matching the
  old code's implicit default. In practice this rarely fires:
  `install.sh` has always written this line explicitly for a production
  install, so it's usually already there and untouched.
- Prints the derived instance/cookie name (unchanged — same `INSTANCE_NAME`,
  same derivation) and the exact next commands.

It never touches `SECRET_KEY` or any other existing line in `data/.env`,
and never re-encrypts anything. It's additive only.

**On `DEPLOY_MODE`:** the helper deliberately does *not* set it. Setting
`DEPLOY_MODE=network` turns on the startup safety guard's fatal check that
`PUBLIC_URL` must be `https://` (see `docs/PLAN-deployment-modes.md`
Section 3) — a wrong guess here would refuse to boot, which is a far worse
failure mode for a migration tool than a warning banner. Left unset,
`DEPLOY_MODE` resolves to `local`, which never exits non-zero. The helper
tells you which case you're in:

- If `PUBLIC_URL` is already `https://...`: once you've confirmed the
  instance is up and healthy, add `DEPLOY_MODE=network` to `data/.env`
  yourself for full parity with a fresh network-mode install (and the
  guard's protection against future `PUBLIC_URL`/`TRUST_PROXY` mistakes).
- If `PUBLIC_URL` isn't `https://` (or is unset): the instance still boots
  fine. If it genuinely sits behind a TLS-terminating reverse proxy, set
  `PUBLIC_URL` to that `https://` address first, then add
  `DEPLOY_MODE=network`.

Until you add `DEPLOY_MODE=network`, expect a persistent "Deployment
configuration warning" banner in the app if `PUBLIC_URL` is set to a
non-loopback address — that's the Prompt 5 startup guard correctly
noticing the mode/URL mismatch. It's informational, not blocking.

### 3. Start the single-container stack

```
docker compose --env-file data/.env -f docker-compose.single.yml up -d
```

This builds (or pulls, once published) the single image and starts one
container running web, worker, and mcp together under s6-overlay, bound to
the same `DATA_HOST_DIR` your three-container stack was already using.

### 4. Verify

```
docker compose --env-file data/.env -f docker-compose.single.yml ps
curl -f http://localhost:${APP_HOST_PORT:-8080}/health
curl -f http://localhost:${MCP_HOST_PORT:-9000}/health
```

Both should return `200`. Then in the browser:

- Log in with your existing admin/user credentials — unchanged, since
  `SECRET_KEY` and the database moved over untouched.
- Check Settings → AI Analysis and Settings → SMTP: previously-saved
  provider keys, the SMTP password, and the Anthropic key should all still
  be there (they decrypt with the same retained `SECRET_KEY`).
- If you use the MCP connector, confirm a tool call still works.

If everything checks out, the old `docker-compose.yml`-based containers and
images can be removed (`docker compose -f docker-compose.yml down --rmi
local`, or leave them as a fallback — they still work unmodified against
the same data directory, since only `data/.env` gained two lines).

## Rolling back

Nothing was destroyed. To go back to the three-container topology: stop the
single container and run
`docker compose --env-file data/.env -f docker-compose.yml up -d`.
The two lines the adopt helper appended are harmless to leave in place —
`TRUST_PROXY`/`DEPLOY_MODE` are read by the same application code
(`app/__init__.py`) regardless of which compose file is driving it, since
both point at the same image. `TRUST_PROXY=1` there gives the legacy
three-container stack the same explicit proxy trust it always had, rather
than silently falling back to the new `local`-mode default of off.
