# Backup and Restore

Backup and restore are `job-squire` CLI operations. Each backup is a single, portable,
**passphrase-encrypted** archive per instance — there is no unencrypted archive format, because
the archive necessarily contains the instance's `SECRET_KEY` (without it, every stored provider
key, the SMTP password, and the Anthropic key can't be decrypted on restore).

## Creating a backup

```bash
job-squire backup NAME                         # prompts for a passphrase, writes to your home folder
job-squire backup NAME --dest /path/to/backups # a custom destination
job-squire backup NAME --format zip            # .zip instead of the default .tgz
job-squire backup --all                        # one archive per registered instance
```

The archive is named for the instance and a UTC timestamp, e.g.
`job-squire-castelo-20260711T1830Z.tgz`. You'll be prompted twice for a passphrase (to catch
typos) unless you pass `--passphrase` — prefer the prompt over the flag so the passphrase never
lands in shell history.

**Write the passphrase down somewhere safe.** There is no recovery path: a lost passphrase means a
lost backup. The CLI states this plainly every time you run `backup`.

### What's inside

- The entire data directory verbatim — the SQLite database, `uploads/`, `candidate_profile.md`,
  the OAuth token store, and anything else sitting in that folder, so nothing you didn't expect to
  lose a backup of is left out.
- The database is captured through a WAL-safe snapshot (SQLite's own Online Backup API, the same
  mechanism the app's own in-app backup uses), so a live instance can be backed up with no
  downtime and no risk of pairing a `.db` file with a `.db-wal` from a different moment.
- `backup-manifest.json`: a backup-format version, the timestamp, the instance's full registry
  entry (name, mode, runtime, ports or hostname, cookie name, public URL), the image version the
  instance was running, a fingerprint of the database schema, the CLI version, and a SHA-256
  checksum for every file in the archive.

### How it's encrypted

The passphrase is stretched with **Argon2id** (the current OWASP-recommended passphrase KDF), and
the archive is sealed with **AES-256-GCM** for authenticated encryption — a corrupted or tampered
archive fails loudly on restore instead of silently decrypting to garbage. A random salt and nonce,
plus the Argon2id parameters, are stored in a small archive header. Both primitives come from the
`cryptography` library the app already depends on; nothing new was added for this, and no external
tool (like `age`) is required.

## Restoring

```bash
job-squire restore /path/to/job-squire-castelo-20260711T1830Z.tgz
job-squire restore /path/to/archive.tgz --rename-to castelo-2
job-squire restore /path/to/archive.tgz --overwrite
job-squire restore /path/to/archive.tgz --no-up
job-squire restore /path/to/archive.tgz --image ghcr.io/dellipse/job-squire:0.6.0
```

What happens, in order:

1. Prompts for the passphrase (unless `--passphrase` is given) and decrypts. A wrong passphrase or
   a corrupted archive fails clearly rather than producing garbage data.
2. Verifies every file's checksum against the manifest, and the backup-format version, before
   writing anything to disk.
3. Unpacks the data directory and restores `data/.env`, including `SECRET_KEY` — this is what lets
   every previously stored secret decrypt correctly on the restored instance.
4. Re-registers the instance from the manifest's registry entry, reallocating ports or hostname as
   appropriate if the target machine differs from where the backup was taken.
5. If an instance of the same name is already registered, prompts to `--rename-to` a different
   name or `--overwrite` the existing one, rather than clobbering it silently.
6. Ensures a container runtime is available (prompting for consent to install one unless `--yes`
   was implied elsewhere), then brings the instance up — on the image recorded in the manifest by
   default, or the one passed via `--image` — unless `--no-up` was given. The app's own additive
   boot-time migrations carry the schema forward if the target image is newer than the one the
   backup was taken on.

## Verifying a restore (worth doing once, before you need it for real)

Run through this after every restore that matters:

1. `job-squire status NAME` — the instance reports healthy.
2. Log in with your normal credentials and confirm the job pipeline, contacts, and settings match
   what you expect from the backup's point in time.
3. Settings → History tab shows the `SearchRun` history you expect.
4. Settings → Sources/AI/Email tabs show provider keys and the SMTP password as already set (no
   "could not decrypt" warning — if you see one, something about `SECRET_KEY` didn't restore
   correctly).
5. If MCP is in use, reconnect the Claude connector once to confirm the OAuth token store
   round-tripped.

Doing one backup → restore into a scratch instance → confirm the checklist above is worth running
right after your first real deploy, so the procedure is proven before you ever need it under
pressure.

## Other ways to grab a copy of your data

`job-squire backup`/`restore` is the recommended path — it's the only one that's a complete,
portable, encrypted single file. Two lighter-weight options still exist underneath it, for quick
ad-hoc use without the CLI:

- **Settings → Backup → Download backup**, in the app itself: streams the same kind of WAL-safe
  snapshot (database + `uploads/` + `candidate_profile.md` + the OAuth token store, optionally
  `.env`) straight to your browser, unencrypted. No shell or CLI access needed to grab a copy.
- **`scripts/backup.sh`/`scripts/restore.sh`**, if you have a checkout on the host: the same
  WAL-safe hot-backup mechanism, callable directly. Restoring this way still has to stop the
  instance itself first, since the app can't safely replace its own data directory out from under
  itself.

Neither of these encrypts the archive or bundles the registry/version manifest the CLI's format
does, so prefer `job-squire backup` for anything you intend to keep or move to another machine.
