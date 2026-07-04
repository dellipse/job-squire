# Backup and Restore

All persistent state lives in one host directory (`DATA_HOST_DIR`, default `./job-squire/data`),
bind-mounted into all three containers at `/data`:

| File / dir | What |
|---|---|
| `job-squire.db` | SQLite database — jobs, contacts, settings, everything except uploaded files |
| `job-squire.db-wal`, `job-squire.db-shm` | SQLite WAL-mode journal files (present while the app is running) |
| `uploads/` | Uploaded resumes, cover letters, and other attachments |
| `candidate_profile.md` | The master candidate profile used by AI routines |
| `oauth_tokens.json` | Encrypted MCP OAuth access tokens |
| `.env` | Secrets and config — `SECRET_KEY`, passwords, provider credentials are all Fernet-encrypted *using* `SECRET_KEY`, so this file and the DB must travel together |
| `.init.lock`, `provider_cooldowns.json`, `.worker_heartbeat` | Transient/operational — safe to skip in a backup, regenerated automatically |

## Why not just `cp` or `tar` the data folder

The database runs in **WAL (Write-Ahead Log) mode**. While the app is live, recently committed
rows can sit in `job-squire.db-wal` rather than in `job-squire.db` itself, and a plain file copy of
the three files while a writer is active is not guaranteed to be point-in-time consistent — you can
end up with a `.db` file paired with a `.db-wal` from a different moment. Two ways to avoid that:

1. **Hot backup (no downtime)** — use SQLite's own Online Backup API, which is safe under
   concurrent writers by design. This is what `scripts/backup.sh` does.
2. **Cold backup (simplest, brief downtime)** — stop all three containers first, *then* copy the
   folder. No WAL subtlety applies because nothing is writing.

Either is fine. Hot backup is what you want for a scheduled/unattended job (e.g. cron); cold backup
is fine for a one-off before an upgrade.

## Option 0 — In-app download (easiest, no host/shell access needed)

Settings → Backup → **Download backup**, signed in as the admin account. Builds the exact same
archive as `scripts/backup.sh` (Online Backup API snapshot + integrity check + `uploads/` +
`candidate_profile.md` + `oauth_tokens.json`, with an option to include or leave out `.env`) and
streams it straight to your browser — nothing to install, no SSH or `docker exec` needed. Restore
still requires the CLI (see [Restoring](#restoring) below): a safe restore has to stop all three
containers before the data directory is replaced, and the web app has no way to do that to itself.

## Option 1 — Hot backup (recommended, no downtime)

```
./scripts/backup.sh                    # writes to ./backups/job-squire-backup-<timestamp>.tgz
./scripts/backup.sh /path/to/backups   # or a custom destination
```

What it does, in order:

1. Runs `sqlite3.Connection.backup()` (Python stdlib, driving SQLite's Backup API) against the
   live `job-squire.db`, producing a single consistent snapshot file. This folds in any pending WAL
   frames automatically — you do **not** need to copy `.db-wal`/`.db-shm` separately.
2. Runs `PRAGMA integrity_check` against that snapshot and aborts if it doesn't come back `ok`.
3. Copies `uploads/`, `candidate_profile.md`, `oauth_tokens.json`, and `.env` alongside it.
4. Tars the result into a timestamped `.tgz` in the destination directory.

Needs `python3` on the host (stdlib only — nothing to `pip install`). If the host has no Python,
run the backup from inside the running container instead:

```
docker compose exec job-squire python3 -c "
import sqlite3
s = sqlite3.connect('/data/job-squire.db')
d = sqlite3.connect('/data/job-squire-backup.db')
with d:
    s.backup(d)
"
docker cp job-squire:/data/job-squire-backup.db ./job-squire-backup-$(date +%F).db
docker exec job-squire rm /data/job-squire-backup.db
```

Then separately copy `uploads/`, `candidate_profile.md`, `oauth_tokens.json`, and `.env` from the
host data directory.

### Scheduling it

Add a cron entry on the host, e.g. nightly at 2 AM, keeping the last 14 backups:

```
0 2 * * * cd /path/to/job-squire && ./scripts/backup.sh /path/to/backups >> /var/log/job-squire-backup.log 2>&1 && \
  find /path/to/backups -name 'job-squire-backup-*.tgz' -mtime +14 -delete
```

## Option 2 — Cold backup (simplest, brief downtime)

```
cd job-squire
sudo docker compose stop job-squire job-squire-worker job-squire-mcp
sudo tar czf job-squire-backup-$(date +%F).tgz -C ./job-squire/data .
sudo docker compose up -d job-squire job-squire-worker job-squire-mcp
```

Downtime is however long the tar takes (typically a few seconds unless `uploads/` is large).

## Restoring

```
./scripts/restore.sh /path/to/job-squire-backup-<timestamp>.tgz
```

What it does:

1. Prompts for confirmation, then stops all three services.
2. Moves the *current* data directory aside to `<data-dir>.pre-restore-<timestamp>` — it is never
   deleted, so a bad restore is itself recoverable.
3. Extracts the archive into a fresh data directory.
4. Fixes ownership to match `PUID`/`PGID` from the restored `.env` (defaults to 1000:1000 if unset)
   — the container runs as that host user/group and can't write to files owned by anyone else.
5. Restarts the three services and waits for the web app's healthcheck.

**SECRET_KEY note:** the archive includes the `.env` that was active when the backup was taken, so
by default the restore brings that `SECRET_KEY` back too — which is what you want, since every
encrypted secret (provider API keys, SMTP password, Anthropic key, OAuth tokens) was encrypted
with it. If you're restoring onto a host that already has its own `data/.env` and want to *keep*
that key instead, decline the prompt in the archive-vs-current `.env` step and merge them by hand;
otherwise expect a "could not decrypt" warning on the Settings page and follow the re-entry steps
in [`deployment.md`](deployment.md#rotating-secret_key-and-re-entering-secrets).

Manual restore, if you'd rather not use the script:

```
cd job-squire
sudo docker compose stop job-squire job-squire-worker job-squire-mcp
sudo mv ./job-squire/data ./job-squire/data.bak-$(date +%F)
sudo mkdir -p ./job-squire/data
sudo tar xzf job-squire-backup-<timestamp>.tgz -C ./job-squire/data
sudo chown -R <PUID>:<PGID> ./job-squire/data
sudo docker compose up -d job-squire job-squire-worker job-squire-mcp
```

## Verifying a restore (tested checklist)

Run through this after every restore — restoring is only as good as confirming it worked:

1. `docker compose ps` — all three services show `healthy` (the worker may take up to a minute for
   its first heartbeat; see [Operations — worker healthchecks](#operations-worker-healthchecks) below).
2. `curl -is http://localhost:8080/health` (or your public URL) returns `{"ok": true}`.
3. Log in with your normal credentials and confirm the job pipeline, contacts, and settings match
   what you expect from the backup's point in time.
4. Settings → History tab shows the `SearchRun` history you expect. A gap just means no scheduled
   run fell in that window — not a bad restore.
5. Settings → Sources/AI/Email tabs show provider keys and the SMTP password as already set (no
   "could not decrypt" warning). If you do see one, `SECRET_KEY` doesn't match what encrypted them
   — see the note above.
6. If MCP is in use, reconnect the Claude connector once (existing OAuth tokens are restored, but
   verifying end-to-end catches anything `oauth_tokens.json` alone can't).

This exercise (backup → restore into a scratch directory → confirm the checklist above) is worth
running once after first deploying, so the procedure is proven before you ever need it for real.

## Operations: worker healthchecks

The three containers each have a Docker `healthcheck:` (see `docker-compose.yml`):

- `job-squire` (web) and `job-squire-mcp` check their respective `/health` HTTP endpoints.
- `job-squire-worker` has no HTTP endpoint (it's a background APScheduler loop), so it instead
  touches `DATA_DIR/.worker_heartbeat` every `HEARTBEAT_INTERVAL_MINUTES` (default 5) and the
  healthcheck fails if that file goes stale (>15 minutes old). This is independent of whether
  automated search is enabled — a stopped heartbeat means the *process* died or wedged, not that
  search is merely idle between scheduled runs.

`docker compose ps` shows the health status directly. The same signal is also surfaced in the app
itself (Dashboard banner + Settings → History tab), so you don't have to be at a terminal to notice
a dead worker.
