# Deployment Runbook

Job Squire is deployed and operated through the `job-squire` CLI. One bootstrap command lands
the CLI; every step after that — creating an instance, starting or stopping it, updating it,
putting a reverse proxy and TLS in front of it, backing it up — is a `job-squire` subcommand. See
[`Setup-Guide.md`](Setup-Guide.md) for the guided, narrative walkthrough aimed at a first-time,
non-technical operator. This doc is the operator's reference runbook: every lifecycle command,
what it does, and the network-mode/proxy mechanics behind it. The full design rationale lives in
[`PLAN-deployment-modes.md`](PLAN-deployment-modes.md); the exact command grammar and internals are
in [`job-squire-cli.md`](job-squire-cli.md).

---

## The three deployment modes

Every instance is one of three modes. The mode is a convenience preset the CLI sets when it
creates an instance's env file — the running app never branches on the mode string itself, only
on the granular flags (`TRUST_PROXY`, `SESSION_COOKIE_SECURE`, ...) the mode fills in.

| Mode | Who it's for | Exposure | Proxy / TLS |
|---|---|---|---|
| Local, single | One person, one machine | `http://localhost:PORT`, loopback only | None — loopback is a secure context in every modern browser |
| Local, multi-instance | Two-plus people on one machine, kept fully separate | `http://localhost:PORT` per instance, distinct ports | None |
| Network | A server, small lab, or classroom | A hostname per instance | Mandatory external reverse proxy terminating TLS |

Local instances need nothing beyond `job-squire create`. Network instances need a reverse proxy
and a certificate in front of them — the CLI can provision both, covered below.

---

## Installing the CLI

```bash
# macOS, Linux
curl -fsSL https://raw.githubusercontent.com/dellipse/job-squire/main/bootstrap.sh | sh

# Windows (PowerShell)
irm https://raw.githubusercontent.com/dellipse/job-squire/main/bootstrap.ps1 | iex
```

Pin a version instead of the latest release with `JOBSQUIRE_VERSION=<version>` (shell) or
`$env:JOBSQUIRE_VERSION="<version>"` (PowerShell) before the command. The bootstrap installs the
CLI and nothing else; it resolves the requested version against GitHub Releases, pins it to an
immutable commit SHA, and installs `job-squire` (alias: `jobsquire`) into an isolated virtualenv
under `~/.job-squire/`. There is no separate `install.sh` anymore — see
["What replaced `install.sh`/`update.sh`/`uninstall.sh`"](#what-replaced-installshupdateshuninstallsh)
below.

The CLI detects an existing container runtime (Docker, Podman, OrbStack, Colima) and reuses it. If
none is found, it proposes installing Podman — the default on every platform, including macOS,
since it's free for commercial use with no threshold — and only installs with your consent.

---

## Instance lifecycle

```bash
job-squire create                 # interactive: mode, name, ports/hostname, secrets, first boot
job-squire start NAME
job-squire stop NAME
job-squire restart NAME
job-squire status NAME            # health + registry-vs-reality drift
job-squire list                   # every registered instance
job-squire remove NAME            # always asks before deleting the data directory
```

`create` walks through choosing a mode, naming the instance (a slug, unique on this machine),
generating a fresh `SECRET_KEY`, allocating a local port pair or collecting a network hostname, and
bringing the instance up. If other instances already exist, it offers to import their non-secret
settings (search targets, schedule, enabled providers, SMTP host/port, AI provider selection) —
secrets are excluded unless you pass `--copy-keys` explicitly.

Every instance lives in its own directory (`~/job-squire/<name>/` by default), containing a
generated `docker-compose.yml`, a compose-level `.env` (host ports, `PUID`/`PGID`), and
`data/.env` plus the SQLite database under `data/`. Nothing about this is proprietary: `cd` into
the directory and run `docker compose ...` (or `podman compose ...`) directly any time. Read-only
and operational commands (`logs`, `ps`, `stop`, `restart`, `exec`) are always safe to run this way.
Structural changes (renaming a container, changing published ports) should go through the CLI, and
`job-squire status` reports drift if the registry and reality disagree.

**If an instance won't start because of an unsafe configuration** — for example a network-mode
instance with `PUBLIC_URL` not set to `https://` — the app's own startup safety guard refuses to
boot and writes a `FATAL:` reason and fix to its log. `create`/`start`/`restart` catch that and
reprint the exact same reason and fix on the command line, rather than a generic "container
exited."

### Updating and rolling back

```bash
job-squire update NAME                    # move to the latest published image
job-squire update NAME --version 0.7.0    # move to a pinned tag
job-squire update NAME --rollback         # move back to the image running before the last update
```

The new image is pulled before the running container is touched, so a failed pull changes nothing.
Only then is the container stopped with a graceful `SIGTERM` (which s6-overlay forwards so the app
checkpoints its SQLite WAL before exiting), the image swapped, and the container recreated. Each
rollback swaps current and previous again, so rolling back twice returns to where you started.

---

## Network mode: the reverse proxy

Network mode always sits behind an external TLS-terminating reverse proxy; the app itself never
terminates TLS. The CLI can provision one for you.

```bash
job-squire proxy NAME                       # detect an existing proxy, or offer to install SWAG
job-squire proxy NAME --no-install          # fail instead of installing SWAG if none is detected
job-squire proxy NAME --yes                 # don't prompt before installing SWAG
```

If the machine already runs a SWAG or nginx-based proxy, `job-squire proxy` generates the Job
Squire web and MCP host configurations, drops them into the proxy's config directory, joins the
instance to the proxy's Docker network, and reloads it — no second proxy is ever installed. If
nothing is running, it installs and brings up a LinuxServer SWAG container (bundling nginx, certbot,
and fail2ban) and configures it the same way. Either way the proxy stays a separate, independently
maintained component — nothing here is baked into the Job Squire image.

### DNS and TLS

A hostname and a certificate are the one thing the CLI cannot conjure — you need a domain (or a
free DuckDNS subdomain) and working DNS before running either of these:

```bash
job-squire dns duckdns NAME --subdomain castelo --token <duckdns-token>              # wildcard, DNS-01
job-squire dns duckdns NAME --subdomain castelo --token <duckdns-token> --main-only  # main subdomain, HTTP-01
job-squire dns cloudflare NAME --domain example.com --token <cloudflare-api-token>   # wildcard, DNS-01
```

**DuckDNS** (free, fully automated) is the recommended zero-cost default — sign up for a
`yourname.duckdns.org` subdomain at duckdns.org, then run the command above with your subdomain and
account token. The default is the wildcard/DNS-01 path (no inbound port needed); `--main-only` uses
ordinary HTTP-01 for just the one subdomain if you'd rather not use DNS validation. SWAG can't do
both from one config at once, hence the flag.

**Cloudflare DNS-01** (semi-automated) is for anyone who already owns a domain on Cloudflare: bring
your domain and an API token scoped to `Zone:DNS:Edit`, and the command writes the certbot plugin
config and issues a wildcard certificate.

**Everything else** — Cloudflare Tunnel, Route53, and the rest of the providers SWAG's own
`dns-conf/` directory supports — is documented, not automated, because each is either a different
topology (TLS terminating at a tunnel provider instead of your own proxy) or a long tail of
provider-specific credentials. Configure one directly against the CLI-installed SWAG's `config/`
directory using SWAG's own docs for that plugin; nothing about a manually configured SWAG conflicts
with what `job-squire proxy` already installed.

Both `dns` commands only apply to a proxy `job-squire proxy` installed itself — they refuse to
touch a third-party SWAG or bare nginx they didn't provision.

### Reaching a local instance remotely without going to network mode

If you just want to check your pipeline from your phone without exposing anything publicly,
Tailscale Serve is the sanctioned path — it keeps the instance local and unexposed while giving it
a real HTTPS front door on your own private tailnet:

```bash
job-squire tailscale enable NAME     # Serve on 443 (web) / 8443 (MCP)
job-squire tailscale status NAME
job-squire tailscale disable NAME    # back to loopback-only
```

This is Serve, never Funnel — the app stays bound to loopback and nothing is published to the
public internet. See [`PLAN-deployment-modes.md`](PLAN-deployment-modes.md) Section 5 for why this
is safe and what it changes under the hood (briefly: it's local mode with a private front door, not
a separate mode).

---

## MCP authentication

OAuth 2.0/PKCE is the default in every mode and needs no CLI setup — Claude's connector flow
handles it. For headless clients that can't complete a browser redirect, a local static token is
available on local instances only:

```bash
job-squire configure NAME --mcp-token generate [--ttl-hours N] [--allow-network]
job-squire configure NAME --mcp-token rotate
job-squire configure NAME --mcp-token revoke
job-squire configure NAME --show
```

The token is refused for a network-reachable instance unless `--allow-network` is passed
explicitly — it's never enabled implicitly. See [`job-squire-cli.md`](job-squire-cli.md#mcp-authentication-prompt-c6)
for the full mechanics.

---

## Backup and restore

```bash
job-squire backup NAME              # writes an encrypted archive to your home folder
job-squire backup --all             # one archive per registered instance
job-squire restore /path/to/archive.tgz
```

See [`backup-restore.md`](backup-restore.md) for the full runbook — the archive is always
passphrase-encrypted, and losing the passphrase means losing the backup.

---

## Running multiple instances

Every instance the CLI creates is already isolated by design — its own data directory, its own
`SECRET_KEY`, its own session cookie name, its own ports (local mode) or hostname (network mode).
See [`multi-instance.md`](multi-instance.md) for the instance model and the registry that makes
this work.

---

## What replaced `install.sh`/`update.sh`/`uninstall.sh`

The repository's old standalone shell scripts are retired. Their logic now lives in CLI
subcommands:

| Old script | Replaced by |
|---|---|
| `install.sh` | `bootstrap.sh` (or `bootstrap.ps1`) + `job-squire create` |
| `update.sh` | `job-squire update` |
| `uninstall.sh` | `job-squire remove` |

The legacy per-platform manual guides that walked through those scripts
(`docs/install/linux.md`, `macos.md`, `windows.md`, `docker-vs-podman.md`) are retired for the same
reason — everything they covered (runtime install, `docker compose` invocation, reverse-proxy
wiring) is now a CLI concern documented above and in [`Setup-Guide.md`](Setup-Guide.md).

## Resetting a password

Set the new value in the instance's `data/.env` (`ADMIN_PASSWORD`/`USER_PASSWORD`), add
`RESET_UIDS_AND_PWDS_ON_START=true`, `job-squire restart NAME`, confirm login, then remove the line
and restart again.

## Rotating `SECRET_KEY` (and re-entering secrets)

`SECRET_KEY` signs session cookies **and** derives the Fernet key that encrypts every stored
secret (provider API keys, the Anthropic key, the SMTP password, and the OAuth token store).
Rotating it does not corrupt the app, but everything encrypted with the old key becomes
undecryptable, so those secrets must be re-entered afterward. Rotate if the key may have been
exposed (committed to git, printed in logs, shared).

There is no re-encrypt-in-place migration by design — the app never holds two keys at once. If you
only need to cut off access, revoke MCP tokens (`job-squire configure NAME --mcp-token revoke`) and
reset passwords instead of rotating.

1. **Revoke live MCP tokens first**, in-app (Settings → Connections → "Revoke all tokens") or via
   `job-squire configure NAME --mcp-token revoke`.
2. **Generate a new key:** `python3 -c "import secrets; print(secrets.token_hex(32))"`.
3. **Set it** in the instance's `data/.env` as `SECRET_KEY`, then `job-squire restart NAME`.
4. **Sign back in** — every session cookie signed with the old key is now invalid.
5. **Re-enter every stored secret** on the Settings page. The UI shows a "could not decrypt"
   warning for anything it can no longer read.
6. **Re-authorize the Claude MCP connector** — old OAuth tokens can no longer be decrypted.

## Wiping data (dev / re-init)

```bash
job-squire stop NAME
rm -rf ~/job-squire/NAME/data/{job-squire.db,job-squire.db-*,uploads,.init.lock,provider_cooldowns.json}
job-squire start NAME
```

Leaving `candidate_profile.md` in place keeps the master profile; delete it too for a truly clean
slate (it's re-seeded from the bundled copy on next boot).
