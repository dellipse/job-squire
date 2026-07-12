# Running Multiple Instances

Job Squire is a single-tenant, two-account application: one running instance always serves exactly
one admin and one job seeker. Running it for more than one person — say, two unrelated job
seekers sharing a machine, or a small classroom — means running the program more than once, as
fully separate **instances**. This is not a special mode; it's what the `job-squire` CLI's
instance model is built around from the start. Even a single install on a single machine is
created as a named instance, which is exactly what lets a second one be added later with no
surprises.

Each instance is fully isolated: its own database, uploads, `SECRET_KEY` and therefore its own
encryption of every stored secret, session cookie name, and (in local mode) port pair or (in
network mode) hostname. Instances share only the container image.

---

## The instance name is the primary key

Setup assigns each instance a unique name — a slug, lowercase, alphanumeric and hyphens — and that
name deterministically drives almost everything else about it:

| What the name drives | Example for `castelo` | Why it must be unique |
|---|---|---|
| Data directory | `~/job-squire/castelo/` | Separate SQLite database and uploads — the actual data isolation |
| `SESSION_COOKIE_NAME` | `castelo_session` | Browsers scope cookies by hostname, not port, so two instances on `localhost` would otherwise clobber each other's sessions |
| Compose project / container name | `job-squire-castelo` | Namespaces containers so instances don't collide |
| Port pair (local mode) | `8080` / `9000` | Distinct web and MCP host ports, allocated automatically |
| Hostname (network mode) | `castelo.example.com` | The reverse proxy routes each instance by its own hostname |

The one thing the name does *not* drive is `SECRET_KEY`, which is generated randomly at creation
precisely because the instance name isn't secret. There is no shared-secret path between
instances — if two people want to share a provider API key across their instances, it's entered in
each one separately.

---

## The registry

The CLI keeps a per-user registry of every instance it knows about, at the conventional per-OS
config location:

| OS | Registry location |
|---|---|
| macOS | `~/Library/Application Support/job-squire/instances.json` |
| Linux | `~/.config/job-squire/instances.json` (honors `XDG_CONFIG_HOME`) |
| Windows | `%APPDATA%\job-squire\instances.json` |

It holds only non-secret metadata — name, mode, runtime, data directory, ports or hostname, cookie
name, public URL, creation date — never a key or password. This is the source of truth
`job-squire status`/`list` read from, and what `job-squire create` checks to reject a name
collision and allocate the next free port pair. The registry is per operating-system user, so two
different OS logins on the same machine keep entirely separate instance lists.

---

## Creating a second instance

```bash
job-squire create
```

is the whole command — it's interactive. When other instances already exist, it detects them via
the registry and offers to import their non-secret settings (search titles, location, radius,
schedule hours and timezone, enabled job-board providers, SMTP host and port, AI provider
selection, interface preferences) so the second person doesn't start from a blank slate. Secrets
are excluded by default — no API keys, no SMTP password, no `SECRET_KEY` — and copied only with an
explicit `--copy-keys` opt-in, which decrypts each secret with the source instance's key and
re-encrypts it with the destination's.

In local mode, `create` allocates the next free web/MCP port pair automatically (checking both the
registry and a real socket bind, so nothing collides with something the registry doesn't know
about) and prints only `localhost`/`127.0.0.1` links — never a LAN IP, which wouldn't get the
browser's secure-context treatment. In network mode, it collects a hostname instead, and a
reverse proxy routes each instance's hostname to it (see
[`deployment.md`](deployment.md#network-mode-the-reverse-proxy)).

Each instance is a self-contained directory:

```
~/job-squire/castelo/
  docker-compose.single.yml   # generated; image pinned
  .env                        # compose-level vars (PUID/PGID, host ports)
  data/
    .env                      # container env: SECRET_KEY, DEPLOY_MODE, SESSION_COOKIE_NAME, ...
    job-squire.db
    uploads/
```

Because it's the whole thing an operator needs, direct runtime access always remains available —
`cd ~/job-squire/castelo && docker compose ...` (or `podman compose ...`) works exactly as if the
CLI weren't involved, using whichever runtime the registry recorded for that instance.

---

## Lifecycle for more than one instance

Every lifecycle command takes the instance name, so managing several is just repeating the command:

```bash
job-squire list                        # every registered instance and its health
job-squire status castelo
job-squire start bob
job-squire update castelo --version 0.7.0
job-squire backup --all                # one encrypted archive per registered instance
job-squire remove bob                  # always asks before deleting bob's data directory
```

`job-squire status`/`list` also report drift if an instance's registry entry and what's actually
running have diverged (a renamed container, a changed port, a deleted volume) — the CLI treats the
registry as the source of truth and can reconcile it.

---

## MCP connector per instance

Each instance's MCP server is a separate OAuth authorization server with its own token store — you
connect each one independently in Claude, using that instance's own connector URL. For a local
instance's headless/non-browser clients, `job-squire configure <name> --mcp-token generate` mints
a token scoped to that one instance; see
[`deployment.md`](deployment.md#mcp-authentication) for the command reference.

---

## Backups

`job-squire backup --all` produces one encrypted archive per registered instance in a single run.
See [`backup-restore.md`](backup-restore.md) for the full format and the restore procedure —
restoring re-registers the instance from the archive's manifest and prompts to rename or overwrite
if a name collision would otherwise occur.
