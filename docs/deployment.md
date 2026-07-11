# Deployment Runbook

> **Heads up:** this doc predates the single-container image, `DEPLOY_MODE`, and the eventual
> `job-squire` CLI. It's still accurate for the three-container topology described here, but see
> [`PLAN-deployment-modes.md`](PLAN-deployment-modes.md) for what's landed and what's still
> in flight, and [`adopt-single-container.md`](adopt-single-container.md) if you want to move an
> existing install onto the single-container image now rather than waiting for the full rewrite.

Target: a Linux host running Docker + LinuxServer **SWAG** reverse proxy. Commands assume you run
as a sudo-capable non-root user. The examples below use `yourdomain.com` with a wildcard cert;
substitute your own domain throughout.

## Host layout

| Path | What |
|---|---|
| `job-squire/` | App source, `examples/`, and `docker-compose.yml` (the 3 Job Squire services) |
| `./job-squire/data/` | Persistent data (`job-squire.db`, `uploads/`, `candidate_profile.md`, `oauth_tokens.json`) + `data/.env`. Bind-mounted into each container at `/data`. |
| `job-squire/examples/.env.example` | Template for `data/.env` |
| `job-squire/examples/nginx/` | Sample nginx/SWAG proxy-conf files — copy to your proxy's conf directory |

Persistent data is a **host bind mount** (`DATA_HOST_DIR`, defaulting to
`./job-squire/data`), not a Docker named volume — so a backup is just a copy of
that folder.

## First-time deploy

1. **Source in place:** clone or copy the repo to your host so `job-squire/` is on disk
   (e.g. `git clone https://github.com/dellipse/job-squire.git` or `scp -r`).

2. **Secrets:** copy `examples/.env.example` to `data/.env`, then fill in the required values:
   ```
   mkdir -p ./job-squire/data
   cp job-squire/examples/.env.example ./job-squire/data/.env
   # Edit data/.env — at minimum set SECRET_KEY and ADMIN_PASSWORD.
   # USER_PASSWORD is optional; omit it to run with a single admin account.
   # Generate SECRET_KEY with:
   python3 -c "import secrets; print('SECRET_KEY=' + secrets.token_hex(32))"
   sudo tee ./job-squire/data/.env >/dev/null <<'EOF'
   SECRET_KEY=...
   ADMIN_PASSWORD=...           # no '$' (or escape as '$$')
   # USER_PASSWORD=...          # optional — omit to run with admin account only
   SESSION_COOKIE_SECURE=true
   PUBLIC_URL=https://squire.yourdomain.com
   PUBLIC_MCP_URL=https://mcp-squire.yourdomain.com
   SCHEDULE_TZ=                 # blank = derive from the in-app search location
   SCHEDULE_WEEKDAY_HOURS=8,13,17
   SCHEDULE_WEEKEND_HOURS=9
   INGEST_API_KEY=...
   EOF
   sudo chmod 660 ./job-squire/data/.env
   ```

3. **SWAG proxy-confs:**
   ```
   sudo cp job-squire/examples/nginx/job-squire.subdomain.conf \
           /containers/docker/swag/config/nginx/proxy-confs/
   sudo cp job-squire/examples/nginx/mcp-squire.subdomain.conf \
           /containers/docker/swag/config/nginx/proxy-confs/
   ```

4. **Pull + start** all Job Squire services (SWAG should already be running):
   ```
   cd job-squire
   sudo docker compose pull
   sudo docker compose up -d job-squire job-squire-worker job-squire-mcp
   ```

5. **Reload SWAG** and verify:
   ```
   sudo docker exec swag nginx -t && sudo docker exec swag nginx -s reload
   sudo docker logs job-squire            # gunicorn up, accounts seeded, no traceback
   sudo docker logs job-squire-worker     # "scheduler up ..."
   sudo docker logs job-squire-mcp        # uvicorn on :9000
   sudo docker exec swag ping -c1 job-squire
   curl -is https://squire.yourdomain.com/ | head -1
   curl -is https://mcp-squire.yourdomain.com/health   # {"ok": true}
   ```

6. **In-app setup:** sign in, open **Settings**, enter your search titles + location (Search tab),
   add provider API keys (Sources tab) + SMTP (Email tab), set the candidate profile and documents
   (Candidate Profile tab), pick the AI mode (AI tab), and (for MCP) add the **base** MCP URL as
   a custom connector in Claude (OAuth sign-in — no token to generate).

## Updating to a new version

Pull the latest image from ghcr.io and restart Job Squire services **without touching SWAG**:

```
cd job-squire
sudo docker compose pull
sudo docker compose up -d --no-deps --force-recreate job-squire job-squire-worker job-squire-mcp
# then confirm the new version shows in the page footer
```

- **Static assets (CSS/JS) changed?** Also hard-refresh the browser (Ctrl/Cmd+Shift+R); the old
  `app.js`/page can be cached.
- **`requirements.txt` changed?** The `--build` is required.
- **A new DB table was added?** No action — `create_all()` creates it on next boot. A new
  **column** on an existing table needs a manual migration (or wipe the volume if no data).
- **Verify the running image has your code:** `sudo docker exec job-squire grep -c <marker>
  /app/app/...` is a quick way to confirm a build actually picked up a change.

## Resetting a password

Set the new values in `data/.env`, add `RESET_UIDS_AND_PWDS_ON_START=true`, `up -d` the web service,
confirm login, then remove the line and `up -d` again.

## Rotating SECRET_KEY (and re-entering secrets)

`SECRET_KEY` does two jobs: it signs session cookies **and** derives the Fernet
key that encrypts every stored secret (provider API keys, the Anthropic key, the
SMTP password, and the on-disk OAuth token store). Rotating it does not corrupt
the app, but it makes everything encrypted with the old key undecryptable, so
those secrets must be re-entered afterward. Rotate if the key may have been
exposed (committed to git, printed in logs, or shared).

There is no re-encrypt-in-place migration by design: the app never holds two
keys at once. If you only need to cut off access, revoke MCP tokens and reset
passwords (above) instead of rotating.

Steps:

1. **Revoke live MCP tokens first.** Settings, Connections, "Revoke all tokens".
   This ensures no old bearer token survives the rotation.
2. **Generate a new key:** `python -c "import secrets; print(secrets.token_hex(32))"`
3. **Set it** in `data/.env` as `SECRET_KEY`, then `up -d` all three services
   (`job-squire`, `job-squire-worker`, `job-squire-mcp`).
4. **Sign back in.** Every session cookie signed with the old key is now invalid,
   so all users are logged out.
5. **Re-enter every stored secret** on the Settings page: provider API keys, the
   Anthropic API key, and the SMTP password. The UI shows a "could not decrypt"
   warning for any secret it can no longer read.
6. **Re-authorize the Claude MCP connector.** Old OAuth tokens can no longer be
   decrypted (and were revoked in step 1), so clients must re-run authorization.

If you skip the re-entry step, providers surface a decryption warning, SMTP
sending fails until the password is re-saved, and the MCP connector rejects the
old tokens. Nothing is silently wrong beyond the secrets themselves.

## Wiping data (dev / re-init)

Removes all jobs, settings, uploads. Needed if you change `PUID/PGID` or want a clean slate.
Stop Job Squire services (not `down`, which would stop SWAG), then clear the bind-mounted data
folder and bring them back up:

```
cd job-squire
sudo docker compose rm -sf job-squire job-squire-worker job-squire-mcp
sudo rm -rf ./job-squire/data/{job-squire.db,job-squire.db-*,uploads,.init.lock,provider_cooldowns.json}
sudo docker compose up -d job-squire job-squire-worker job-squire-mcp
```

> Leaving `candidate_profile.md` in place keeps the master profile; delete it too for a truly
> clean slate (it is re-seeded from the bundled copy on next boot).

## Backups

Everything is in the host data folder (`DATA_HOST_DIR`, default
`./job-squire/data`): `job-squire.db`, `uploads/`, `candidate_profile.md`,
`oauth_tokens.json`, and `.env`.

The database runs in WAL mode, so a plain `tar`/`cp` of the folder while the
app is live is not guaranteed to be point-in-time consistent (recent commits
can be sitting in `job-squire.db-wal` rather than `job-squire.db`). Use one of:

```
./scripts/backup.sh              # hot backup, no downtime (recommended)
```

or stop the services first for a cold backup:

```
sudo docker compose stop job-squire job-squire-worker job-squire-mcp
sudo tar czf job-squire-backup-$(date +%F).tgz -C ./job-squire/data .
sudo docker compose up -d job-squire job-squire-worker job-squire-mcp
```

See **[`backup-restore.md`](backup-restore.md)** for the full runbook: why WAL mode matters, the
restore procedure (`scripts/restore.sh`), a post-restore verification checklist, and how to
schedule backups via cron.

## Running multiple instances

To run more than one independent deployment on the same host — for example, one instance per job seeker — see **[`docs/multi-instance.md`](multi-instance.md)**. In brief:

- Each instance needs its own `data/` directory, its own `SECRET_KEY`, its own public URLs, and different host port values for `APP_HOST_PORT` and `MCP_HOST_PORT`.
- Run each instance with a unique project name: `docker compose -p <name> --env-file <data-dir>/.env up -d`.
- Each instance gets its own SWAG proxy conf and its own MCP connector entry in Claude.

## Compose / networking notes

- The Job Squire `docker-compose.yml` defines the three services. By default it publishes the web
  app on `127.0.0.1:8080` and the MCP server on `127.0.0.1:9000` (host-port mode — Option A). To
  use SWAG or another nginx container on a shared Docker network, switch to Option B: comment out
  the `ports` blocks and uncomment the `networks` blocks in `docker-compose.yml`, then create the
  network once with `sudo docker network create <network-name>`.
- The MCP subdomain conf uses `http2 off` and **no** `proxy_*` overrides (SWAG's bundled
  `proxy.conf` already sets `proxy_http_version`, the Connection header, buffering, and timeouts —
  redeclaring them makes nginx reject the file).
