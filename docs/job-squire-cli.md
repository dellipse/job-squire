# The `job-squire` CLI: package, command grammar, and versioning

This is the one place Prompt C1 (`docs/PROMPTS-deployment-cli.md`) asks for
the settled command grammar and version scheme to live. It documents the
outcome of folding the old `jobsquire-cli` project into `job_squire_cli/`
inside this repo.

## Package layout

One installable package, `job_squire_cli/` (distribution name
`job-squire-cli`), with its own `pyproject.toml`:

```
job_squire_cli/
  pyproject.toml
  job_squire_cli/
    cli.py            # top-level click group; wires ops + lazy query group
    ops/commands.py    # deployment/lifecycle click commands (C5-C7); backup/restore stubs remain (C8)
    ops/runtime.py     # container runtime detection and per-OS install (Prompt C3)
    ops/registry.py    # cross-platform instance registry (Prompt C4)
    ops/paths.py       # per-instance directory layout (Prompt C5)
    ops/ports.py       # local-mode port pair allocation (Prompt C5)
    ops/compose.py     # compose/env rendering + runtime-driven compose invocations (C5/C7)
    ops/dotenv.py      # line-preserving .env read/append/set helpers (Prompt C7)
    ops/crypto_mirror.py  # HKDF-SHA256 -> Fernet derivation mirrored from app/crypto.py (C5/C6)
    ops/secrets_copy.py  # Fernet-aware settings import between instances (Prompt C5)
    ops/lifecycle.py   # create/start/stop/restart/status/list/remove/update orchestration (C5/C7)
    ops/mcp_token.py   # jsq_mcp_ static token generate/rotate/revoke (Prompt C6)
    ops/backup.py      # backup/restore orchestration (Prompt C8)
    ops/backup_crypto.py  # Argon2id + AES-256-GCM archive encryption (Prompt C8)
    ops/proxy.py       # reverse-proxy provisioning: detect/install SWAG, nginx confs (Prompt C9)
    ops/dns.py         # DNS/TLS validation for the CLI-installed SWAG: DuckDNS auto, Cloudflare DNS-01 semi-auto (Prompt C10)
    query/
      commands.py      # health, list, pipeline, contacts, job, contact, followups
      mcp_client.py     # self-contained MCP client (Streamable HTTP, no Hermes)
      config.py         # where the query group reads its endpoint/token from
  tests/
```

Two dependency tiers:

- **Core** (`click`): the deployment/lifecycle group. Installing plain
  `job-squire-cli` gets you this and nothing else.
- **`[query]` extra** (`rich`, `mcp`, `anyio`, `httpx`): the query group.
  Its modules are imported lazily (see `cli.py`'s `_LazyGroup`), so a
  deployment-only install never pays the import cost and the deployment
  commands never require a live MCP endpoint.

## Command grammar

Two command groups under one entry point, chosen specifically to resolve
a naming collision: both groups naturally want a command called `list`
(the deployment group lists *instances*; the query group lists *jobs*).
Rather than rename either `list`, the query group is namespaced under its
own subcommand:

| Group | Invocation | Commands |
|---|---|---|
| Deployment/lifecycle | `job-squire <cmd>` (flat, top level) | `create`, `start`, `stop`, `restart`, `status`, `list`, `update`, `remove`, `uninstall`, `configure`, `backup`, `restore`, `proxy` |
| DNS/TLS | `job-squire dns <cmd>` | `duckdns`, `cloudflare` |
| Tailscale | `job-squire tailscale <cmd>` | `enable`, `disable`, `status` |
| Ollama | `job-squire ollama <cmd>` | `check`, `setup` |
| Query | `job-squire query <cmd>` | `health`, `list`, `pipeline`, `contacts`, `job`, `contact`, `followups` |

The deployment group is new in this fold-in; its commands are structural
placeholders as of Prompt C1 (grammar and `--help` text are real, behavior
is not) and land incrementally in Prompts C2-C11 of
`docs/PROMPTS-deployment-cli.md`. `proxy` wasn't part of C1's
original table either -- C1 deliberately deferred `update`'s rollback
design to Prompt C7, the dedicated session for version movement, and
deferred `proxy` to Prompt C9, the dedicated session for reverse-proxy
provisioning (PLAN Section 5). `dns` is namespaced under its own subcommand
for the same reason `query` is (a natural home for two related verbs,
`duckdns` and `cloudflare`, rather than two more flat top-level names) and
was deferred to Prompt C10, the dedicated session for DNS/TLS provisioning.

### Update and rollback (Prompt C7; self-update and `--all` added later)

```
job-squire update                         # self-update the CLI only; no instance touched
job-squire update NAME                    # self-update the CLI, then move NAME to the latest image
job-squire update NAME --version 0.7.0    # move to a pinned tag (or a full image ref)
job-squire update NAME --rollback         # move back to the image running before the last update
job-squire update --all                   # move every registered instance to the latest image
job-squire update --skip-self-update ...  # move instance(s) without updating the CLI first
job-squire update --cli-version 0.6.0     # pin the CLI self-update instead of taking latest
```

**Self-update runs first, unconditionally, unless skipped.** `job-squire
update` always brings the running CLI itself up to date (ops/
self_update.py) before it touches any instance -- resolve the requested
version (default latest) through the GitHub Releases API, pin the tag to
an immutable commit with `git ls-remote` (same integrity mechanism
bootstrap.sh uses), and `pip install --upgrade` a `git+...@<sha>` spec
via the currently-running interpreter's own `pip`, preserving whether
`[query]` was already installed. A failed self-update (offline, GitHub
API hiccup) is a warning, not fatal -- instance update(s) still proceed;
`--skip-self-update` opts out of even trying. Bare `job-squire update`
(no NAME, no `--all`) only does this self-update step.

`--all` updates every registered instance instead of one `NAME` (mutually
exclusive with passing a `NAME`); each instance is moved in registry
order, and the loop stops at the first instance whose update fails,
same as `backup --all`.

The new image is pulled *before* the running container is touched -- a
failed pull changes nothing. Only then is the container stopped
(`compose stop`, a graceful SIGTERM that s6 forwards so the app
checkpoints its SQLite WAL before exiting), the image swapped, and the
container recreated. The image the instance was running is recorded in
its compose-level `.env` (`PREVIOUS_IMAGE`) before the swap, which is what
`--rollback` reads; each rollback swaps current and previous again, so
rolling back twice returns to where you started.

The query group is the old `jobsquire-cli` project's command set, moved
over with its observable behavior unchanged, with two grammar changes
made deliberately as part of settling this fold-in:

- **`overdue` renamed to `followups`.** Reads more clearly next to the
  other nouns (`pipeline`, `contacts`, `job`, `contact`), and `overdue` on
  its own didn't say overdue *what*.
- **`stages` and `top` dropped.** `stages` was a bare alias for `pipeline`
  with no distinct behavior. `top` was a client-side sort/filter over the
  same data `list` already returns (`list Saved` and eyeball the `Fit`
  column, or pipe `--json` output through `jq` for a scripted top-N). Both
  were folded away rather than carried forward as dead weight in the
  settled grammar; if either is missed in practice, it's cheap to add back
  as a real command against the same `list_jobs` tool.

### Entry points

`job-squire` is the canonical command name; `jobsquire` is wired as an
alias to the exact same entry point (`job_squire_cli.cli:main`) so
existing muscle memory and scripts keep working. Both are installed by
`pip install job-squire-cli`.

## Query group configuration

The query group needs an MCP endpoint and (usually) a bearer token for a
running instance. It reads these from, in order:

1. `JOB_SQUIRE_MCP_URL` / `JOB_SQUIRE_MCP_TOKEN` environment variables
   (useful for smoke-testing and CI, and for overriding the file without
   editing it).
2. A JSON file at the per-OS, per-user config directory (the same one the
   instance registry from `PLAN-deployment-modes.md` Section 4 uses):
   `~/.config/job-squire/mcp.json` (Linux, honoring `XDG_CONFIG_HOME`),
   `~/Library/Application Support/job-squire/mcp.json` (macOS),
   `%APPDATA%\job-squire\mcp.json` (Windows), keyed by instance name so one
   machine with several registered instances can hold an endpoint/token
   per instance:

   ```json
   {
     "version": 1,
     "default": "castelo",
     "instances": {
       "castelo": {"endpoint": "http://localhost:9000", "token": "jsq_mcp_..."}
     }
   }
   ```

   `--instance/-i` on `job-squire query` selects which entry to use;
   omitted, it falls back to `default`, or the sole entry if only one is
   configured, or a clear error listing what's configured if the choice is
   ambiguous. The file is written with `0600` permissions since it usually
   holds a plaintext bearer token (this file, unlike the app's own
   `AIConfig.mcp_api_key_enc`, has to hold the plaintext -- it's what gets
   sent as the `Authorization: Bearer ...` header).

**No Hermes involvement, in either direction.** The query group does not
read `~/.hermes/`, does not import or vendor any Hermes code, and does not
require Hermes to be installed. It talks Streamable HTTP directly to
`app/mcp_server.py`'s `/mcp` endpoint using the same `mcp` library the
server itself depends on. Hermes (or any other MCP host) can still use the
Job Squire MCP server by reading its published MCP documentation; that
coupling runs one way, through docs, never through shared code.

## MCP authentication (Prompt C6)

OAuth 2.0/PKCE stays the default, untouched MCP flow in every mode --
`job-squire configure` generates nothing for it. Where a browser flow is
available, `job-squire configure NAME --token <oauth-access-token>
[--endpoint URL]` wires an OAuth access token obtained elsewhere into the
query group's config, without the CLI implementing the OAuth dance itself.
OAuth is preferred whenever an instance is reachable beyond the one
machine (network mode, or a Tailscale-Serve-fronted local instance --
Prompt C11).

The one sanctioned alternative is the local `jsq_mcp_` static bearer
token, matching the settled spec in `PLAN-deployment-modes.md` Section 5:
256 bits of URL-safe base64, Fernet-encrypted at rest, constant-time
compared, loopback-only unless explicitly enabled. `app/mcp_auth.py` and
`app/main.py`'s `settings_mcp_api_key()` route implement the app side
(Prompt 6 of `PROMPTS-deployment-modes.md`), reachable only from an
authenticated, CSRF-protected browser session -- there is no Flask CLI
command or admin API route to call into instead. So `job_squire_cli/ops/
mcp_token.py` writes the same `AIConfig` columns directly with the stdlib
`sqlite3` module, mirroring the app's token shape and its HKDF-SHA256 ->
Fernet derivation (`ops/crypto_mirror.py`, shared with `ops/
secrets_copy.py`) rather than importing the app package -- exactly the
precedent `ops/secrets_copy.py` already established for the app's other
Fernet-encrypted columns. A write lands on the very next MCP request with
no restart needed, since `app/mcp_server.py` re-fetches `AIConfig` fresh
on every call.

```
job-squire configure NAME --mcp-token generate [--ttl-hours N] [--allow-network]
job-squire configure NAME --mcp-token rotate     # replaces the active token
job-squire configure NAME --mcp-token revoke     # clears it everywhere
job-squire configure NAME --show                 # print current MCP auth state; the default with no flags
```

`generate` and `rotate` both mint a fresh token (the app only ever keeps
one active, so overwriting *is* rotation -- there's no separate rotate
code path on either side); `generate` refuses to clobber an existing
active token, `rotate` requires one to already exist, and `revoke` clears
the app-side columns and the query group's stored copy together. Every
successful generate/rotate also records the derived MCP endpoint (local
mode: `http://localhost:<mcp_port>` from the registry; network mode: the
registry has no `public_mcp_host` field, so it falls back to the same
`mcp-<hostname>` convention `create --mcp-hostname` defaults to, correctable
with `--endpoint`) and the plaintext token into `mcp.json`, and makes the
instance the query group's default if none is set yet (`--set-default`/
`--no-set-default` to control this explicitly).

**Reachability rule.** The static token is refused for a network-mode
instance unless `--allow-network` is passed explicitly (`--allow-network`
alone, without `--mcp-token`, just toggles the opt-in without minting a
token) -- it is never enabled implicitly, matching
`app/mcp_auth.py`'s `is_static_token_allowed()` and the resolved
`DEPLOY_MODE` posture from the app set (the registry's `Instance.mode` is
that same value by construction, since `create` writes
`DEPLOY_MODE=mode` verbatim into the instance's `data/.env`).

## Container runtime detection and install

Prompt C3 adds `job_squire_cli/ops/runtime.py`, which `create` (Prompt C5)
calls before it can bring an instance up. It follows
`docs/PLAN-deployment-modes.md` Section 6 exactly:

1. **Detect first.** `detect_working_runtime()` looks for `docker`,
   `podman`, `orbstack` (checked via its `orbctl`/`orb` binary), and
   `colima`, in that order, and confirms whichever is found on `PATH`
   actually runs (`docker info` / `podman info` / `orbctl status` /
   `colima status`). If one works, it is used and nothing is installed.
2. **Install only with consent, and only the per-OS default.** When
   nothing works, `ensure_runtime()` builds an `InstallPlan` for the
   current platform and only runs it after the caller's `confirm`
   callback returns true:
   - **Linux** (`linux_install_plan`): Podman rootless, with the package
     manager chosen by reading `/etc/os-release` (`dnf` on
     Fedora/RHEL-likes, `apt-get` on Debian/Ubuntu-likes, `pacman` on
     Arch). Docker is never auto-installed here — only used if detection
     already found it. An unrecognized distribution raises
     `RuntimeSelectionError` pointing at the manual install docs rather
     than guessing a package manager.
   - **macOS** (`macos_install_plan`): Podman machine, scripted end to
     end (`brew install podman`, `podman machine init`, `podman machine
     start`). OrbStack is only built when the caller explicitly passes
     `prefer_orbstack=True`, and its commercial-use threshold
     (`ORBSTACK_LICENSE_NOTICE`) is printed at exactly that point, never
     before.
   - **Windows** (`windows_install_plan`): Podman on WSL2, scripted the
     same way. `check_wsl2()` is a shared prerequisite check for both
     Podman and Docker Desktop (both run their Linux containers inside
     WSL2) — a missing `wsl` binary or an unhealthy `wsl --status` raises
     `RuntimeSelectionError` with `wsl --install` plus a reboot as the
     guidance, before any install is attempted. Docker Desktop is only
     built when the caller passes `prefer_docker_desktop=True`, with its
     own threshold (`DOCKER_DESKTOP_LICENSE_NOTICE`) shown at that point.
3. **Recording the choice.** `record_runtime_choice()` /
   `load_runtime_choice()` persist `{"runtime", "source", "recorded_at"}`
   to `runtime.json` in the same per-user config directory `mcp.json`
   lives in (see above) — never a secret, just which runtime was detected
   or installed and when. This is an interim, machine-wide cache; Prompt
   C4 formalizes the same information into the `runtime` field of each
   instance's registry entry, one per instance rather than one per
   machine.

Every subprocess call and `PATH` lookup in this module is injected
(`run`/`which` parameters), so `tests/test_runtime.py` exercises every
branch — detect-and-reuse, each OS's install plan, the WSL2 guard, consent
gating, and the recording round-trip — without ever touching a real
container runtime or a real `PATH`.

## Instance lifecycle core (Prompt C5)

`create`, `start`, `stop`, `restart`, `status`, `list`, and `remove` are
real as of Prompt C5, wired to `ops/lifecycle.py` (`configure` followed in
Prompt C6, wired to `ops/mcp_token.py` and `query/config.py` -- see "MCP
authentication" below); `update`, `backup`, and `restore` remain
structural stubs until C7-C8. Every real
command follows the same shape as `ops/runtime.py`: `ops/commands.py` is a
thin click adapter (prompting, printing, mapping exceptions to a clean
`exit(1)`), and `ops/lifecycle.py` takes no click objects at all -- every
function accepts its subprocess `run`, `PATH` `which`, `confirm`, and
`sleep` callables as parameters, so it's directly unit-testable
(`tests/test_lifecycle.py`) against a fully injected fake runtime, never a
real `docker`/`podman`.

**Per-instance layout** (`ops/paths.py`). Each instance is one
self-contained directory, `~/job-squire/<name>/` by default
(`JOB_SQUIRE_HOME` overrides the root):

```
<name>/
  docker-compose.yml   # generated; image pinned, no build: block
  .env                        # compose-level vars (PUID/PGID, host ports)
  data/
    .env                      # container env (SECRET_KEY, DEPLOY_MODE, ...)
```

`job-squire.db` and `uploads/` are *not* under this directory -- they live
in the instance's own named Docker volume (`<container_name>-data`,
declared in `docker-compose.yml`, addressed by `job-squire backup`/
`restore` through the container itself, never by walking a host path). This
directory is deliberately the whole thing an operator needs for "direct
runtime access remains available" (PLAN Section 7): `cd` into it and run
`docker compose ...` or `podman compose ...` directly. It's also the exact
directory registered as the instance's `data_dir` -- `SECRET_KEY` is what a
backup archive needs from here, alongside the database pulled out of the
volume separately.

**Compose/env rendering** (`ops/compose.py`). The generated
`docker-compose.yml` is *not* a copy of the repo's own file of the
same name -- that one has a `build:` block for local development from a
checkout, which a CLI-created instance (no checkout involved) must not
have. `render_compose_yaml`/`render_compose_env`/`render_data_env` are
hand-rolled f-string templates (no new YAML/dotenv dependency) covering
the fixed, small shape both files need. `runtime_binary`/`compose_binary`
translate the registry's `runtime` field (`docker`, `podman`, `orbstack`,
`colima`) into the right CLI: OrbStack and Colima both provide the
`docker` binary (PLAN Section 6), so only Podman gets its own branch.
`inspect_state`/`container_logs`/`extract_fatal_lines` are what `status`
and the startup-guard surfacing below read back.

**Port allocation** (`ops/ports.py`). `allocate_port_pair` checks both the
registry (no two instances get the same recorded port) and a real
loopback socket bind (nothing squats on a port the registry doesn't know
about), starting from `8080`/`9000`.

**Session cookie collision, closed.** `render_data_env` sets
`SESSION_COOKIE_NAME` explicitly to the registry's derived
`<slug>_session` rather than relying on the app's own `INSTANCE_NAME`
derivation (`app/__init__.py`): the app's derivation turns *both* hyphens
and spaces into underscores, while the registry's slug allows hyphens, so
for any instance name containing a hyphen the two derivations would
otherwise silently disagree.

**Leftover-volume check.** `/data` is a named Docker volume, not a host
bind mount (`ops/compose.py`'s `render_compose_yaml`), and `remove`/
`uninstall` only delete it when the operator chose to delete the instance's
data (see "Remove never destroys data silently" below) -- so a same-named
instance created, removed with its data *kept*, and created again would
otherwise silently reattach to the old volume: the fresh `ADMIN_PASSWORD`
`create` writes into the new `data/.env` is never actually applied, because
`app/__init__.py`'s `_seed_users` only seeds a user that doesn't already
exist, and the old database's admin row (with its own, different password)
is what answers login. `create_instance` checks for a volume matching
`compose.data_volume_key(container_name)` (via `docker/podman volume ls
--filter name=...`) before writing anything to disk; if one exists, it asks
(the same injected `confirm` callable everything else here uses -- `--yes`
answers it the same way it answers the runtime-install prompt) whether to
remove it and continue, or raises `LeftoverVolumeError` if declined. Each
generated compose file also pins its volume to an explicit `name:` (rather
than letting Compose default-prefix it with the project name) specifically
so this substring check, and the literal volume Docker/Podman materializes,
always agree -- without it, since `container_name` is used both as the
compose project (`-p`) and as the volume key's own prefix, the actual
volume would come out doubled (e.g. `job-squire-testdb_job-squire-testdb-
data` instead of `job-squire-testdb-data`).

**Importing settings from an existing instance** (`ops/secrets_copy.py`,
`create --import-from`). PLAN Section 4's import prompt splits across the
same two config layers the app itself uses: schedule hours/timezone are
`data/.env` variables, read as plain text before the new instance's first
boot; everything else (search targets, enabled providers, SMTP host/port,
AI provider selection, interface preferences) lives in the database, read
and written directly with the stdlib `sqlite3` module against a
hand-maintained column allowlist (this package does not depend on
Flask/SQLAlchemy/the app package at all). Secrets are excluded by default;
`--copy-keys` decrypts each secret column with the *source* instance's
`SECRET_KEY` and re-encrypts it with the destination's, using an
HKDF-SHA256 -> Fernet derivation mirrored byte-for-byte from
`app/crypto.py` (verified in `tests/test_secrets_copy.py` by loading the
real `app/crypto.py` file directly, bypassing `app/__init__.py`'s
Flask-only imports). The database copy runs *after* the new instance's
first boot (so the app's own schema creation/seeding has already run),
bracketed by a compose `stop`/`start` so it never races the app's own
writes to the same SQLite file.

**Surfacing the startup guard** (PLAN Section 7). When `app/deploy.py`'s
startup safety guard refuses to boot (an unsafe `DEPLOY_MODE`/`PUBLIC_URL`/
`TRUST_PROXY` combination), s6 brings the whole container down and it
writes `FATAL: ...` lines to stderr before exiting. `create`/`start`/
`restart` wait for the container to report healthy or exited
(`lifecycle.wait_for_state`), and on an exited container, pull its logs
and re-raise `StartupGuardFailure` with the exact `FATAL:` lines intact --
the click layer reprints them verbatim instead of a generic "container
exited" message. A collision or a validation error is checked *before* any
of this (runtime detection, port allocation, or writing to disk), so a
bad `create` invocation never has side effects to clean up.

**Remove never destroys data silently.** `remove_instance` always asks
(via an injected `confirm_delete` callable) before deleting an instance's
data -- both its named Docker volume (the database and uploads) and its
host data directory (`SECRET_KEY`); if nothing asks (no `confirm_delete`,
no explicit `keep_data`), the default is to keep the data -- PLAN Section
4's rule that removing an instance must never silently destroy someone's
job-search history. The volume cleanup itself is two-layered: `compose
down` gets `-v` whenever data is being deleted (removes the volume the
compose file itself declares), and a direct `volume ls`/`rm` sweep runs
afterward regardless -- catching a volume `down -v` didn't reach (e.g. the
instance's `root` directory, and so its compose file, was already gone) and
reporting back exactly which volume name(s) actually disappeared
(`RemoveResult.volumes_removed`, printed by both `remove` and `uninstall`).

**`compose down` never removes the image it was running -- `remove`
leaves it alone by default; `uninstall` removes it by default.**
`docker/podman compose down` only tears down the container and network;
the pulled image is left on disk exactly as before. `job-squire remove
NAME --remove-image` opts into cleaning that up too: after the container
is down, `_image_still_in_use` checks every *other* currently-registered
instance's own compose file for the same image ref before calling `rmi`,
so a shared `ghcr.io/dellipse/job-squire:latest` tag (the default every
`create`-made instance gets unless `--image` overrides it) is never
pulled out from under a sibling instance still running it -- it's only
removed once the last instance referencing it is gone. A failed `rmi`
(e.g. something outside job-squire's own registry is still using the
image) is reported, not raised, so it never blocks the rest of the
removal. `uninstall` uses the exact same machinery but flips the
operator-facing default to "remove" -- see below.

## Uninstalling (`ops/uninstall.py`)

```
job-squire uninstall                          # prompts: uninstall? then keep the image(s)? (default: remove)
job-squire uninstall --keep-data              # force-keep every instance's data (volume + data directory)
job-squire uninstall --delete-data            # force-delete every instance's data (volume + data directory)
job-squire uninstall --remove-runtime         # also remove the container runtime, if job-squire installed it
job-squire uninstall --remove-image           # skip the keep-image prompt; remove (this is the default anyway)
job-squire uninstall --keep-image             # skip the keep-image prompt; leave every image in place
job-squire uninstall --yes                    # no prompts: keeps data, removes images, leaves the runtime alone
```

Not part of the original C1-C12 set (`docs/PROMPTS-deployment-cli.md`) --
added afterward, since getting job-squire *off* a machine cleanly matters
as much as getting it on. `bootstrap.sh`/`bootstrap.ps1` already put the
CLI on `PATH` idempotently (a marker-commented line appended to
`~/.zshrc`/`~/.bashrc`/`~/.profile` on macOS/Linux, the `HKCU\Environment`
`Path` value on Windows, each guarded so re-running the bootstrap never
duplicates the entry) -- `uninstall` is what reverses that, plus the two
other things a full setup can leave behind:

1. **Every registered instance**, via the exact same `remove_instance`
   (one call per instance) `remove` itself uses -- the same keep-or-
   delete-data prompt and the same safe keep-by-default fallback, so
   uninstalling everything is never more destructive to *data* than
   removing one instance at a time would be. Image cleanup is the one
   place `uninstall` is *more* aggressive than `remove` by default,
   deliberately: an uninstall is normally a full teardown, so leaving
   multi-hundred-MB images behind would surprise more people than it'd
   protect. Without `--remove-image`/`--keep-image` on the command line,
   `job-squire uninstall` asks "Keep the container image(s) instead of
   removing them?" (default No -- Enter removes them); `--yes` skips
   straight to that same default without asking. Either way the resolved
   choice is forwarded to each instance's own `remove_instance` call
   unchanged; instances are torn down in registry order, so a tag shared
   across several instances is kept until the last one referencing it is
   reached.
2. **The container runtime** (Podman, OrbStack, or Docker Desktop) --
   opt-in only, via `--remove-runtime`, and even then only if
   `ops/runtime.py`'s `runtime.json` recorded `source: "installed"` (Prompt
   C3). A runtime `ensure_runtime` found already working (`source:
   "detected"`) is never touched, mirroring "never install over one that
   already works" in reverse: never uninstall one job-squire didn't put
   there. `runtime_uninstall_plan` is the literal reverse of each per-OS
   `*_install_plan` in `ops/runtime.py` (`brew uninstall`, `dnf/apt-get/
   pacman remove`, `winget uninstall`, stopping and removing a Podman
   machine before uninstalling the package).
3. **The CLI's own venv and `PATH` entry.** No install manifest is
   written or needed: the venv location is read from `sys.prefix` of the
   *running* interpreter, gated by `looks_like_bootstrap_venv` (the
   directory must be named `cli`, its parent `job-squire` or
   `.job-squire`, with a real `pyvenv.cfg` inside) so this only ever
   proposes deleting a directory that actually matches what
   `bootstrap.sh`/`bootstrap.ps1` create -- never a system Python or a
   developer's `pip install -e` checkout. When it doesn't match,
   `uninstall` leaves the CLI's files alone and prints `pip uninstall
   job-squire-cli` instead of guessing.

The CLI's own config directory (the instance registry, `mcp.json`,
`runtime.json`) is always cleared as part of `uninstall` -- it's metadata
about a CLI that is, by that point, either fully removed or about to be
removed by hand, never "data" in the job-search sense (that's each
instance's own named Docker volume plus its host `data_dir`, together
governed by `--keep-data`/`--delete-data` above).

## Reverse-proxy provisioning (Prompt C9)

```
job-squire proxy NAME                       # detect an existing proxy, or offer to install SWAG
job-squire proxy NAME --container swag2     # use a specific proxy container instead of auto-detecting one
job-squire proxy NAME --config-dir /path    # a bare (non-containerized) nginx install's config directory
job-squire proxy NAME --no-install          # fail instead of installing SWAG if none is detected
job-squire proxy NAME --yes                 # don't prompt before installing SWAG
```

Only applies to a `network`-mode instance (PLAN Section 5: local modes use
loopback only and need no proxy). `ops/proxy.py` covers the two cases from
"Optional proxy provisioning":

- **An existing proxy.** `detect_existing_proxy` looks for a running
  container that looks like SWAG (name/image containing `swag`) or bare
  nginx, and reads its `/config` (or `/etc/nginx/conf.d`) bind mount via
  `docker/podman inspect` to find the host directory to drop confs into.
  No second proxy is ever installed.
- **No proxy.** `install_swag` writes a small standalone compose file at
  `~/job-squire/_proxy/` (sibling to, but not one of, the per-instance
  directories in `ops/paths.py` -- it's never registered as an instance)
  and brings up a LinuxServer SWAG container. DNS/certificate validation
  (DuckDNS, Cloudflare DNS-01, ...) is Prompt C10's job, not this one --
  `--url`/`--validation` here are just SWAG's own required env vars,
  passed through as-is or left as placeholders C10 fills in later.

**The nginx conf templates are hand-rolled in `ops/proxy.py`, not read from
`examples/nginx/` at runtime**, for the same reason `ops/compose.py`
doesn't read the repo's own `docker-compose.yml`: this package is
`pip install`-able with no repo checkout on disk, and `pyproject.toml`
only ships the `job_squire_cli` package itself. `_WEB_CONF_TEMPLATE`/
`_MCP_CONF_TEMPLATE` mirror `examples/nginx/job-squire.subdomain.conf` and
`mcp-squire.subdomain.conf` by hand, adapted for the single-container
image: both examples originally named two different upstream containers
from the old three-container topology (`job-squire` on 8000,
`job-squire-mcp` on 9000); here both point at the *same* container (this
CLI only ever creates one), just on two different ports, and the
generated filenames are namespaced per instance
(`job-squire-<name>.subdomain.conf`, `mcp-job-squire-<name>.subdomain.conf`)
so more than one CLI-managed instance can share a proxy.

**Two upstream forms**, chosen by whether the proxy is itself a container:

- **Containerized** (SWAG or any proxy container): the instance's
  container joins the proxy's Docker network (`resolve_shared_network`
  reuses the proxy's existing custom network if it has one, otherwise
  creates `--network`'s value and attaches both sides to it), and the conf
  resolves the instance by container name over Docker's embedded DNS
  (`resolver 127.0.0.11`). The
  instance's `docker-compose.yml` is rewritten in place with the
  new `networks:` block (`compose.write_compose_files`'s `proxy_network`
  parameter) and the container is recreated to pick it up -- additive to
  the existing host-port publish, not a replacement for it, so direct
  host-port access for troubleshooting still works.
- **Bare nginx on the host** (`--config-dir` with no matching container):
  no Docker network exists to join, so the conf proxies straight to the
  instance's published host ports (`proxy_pass http://127.0.0.1:<port>;`),
  matching the fallback the example conf's own comments already document.

Either way, the proxy stays a separate, independently maintained component
-- nothing here is baked into the Job Squire image, and TLS still
terminates at the proxy. Network mode is still not considered configured
without a working proxy in front, which the app's own startup guard
(PLAN Section 3) already enforces regardless of whether `job-squire proxy`
was ever run.

## DNS and TLS provisioning (Prompt C10)

```
job-squire dns duckdns NAME --subdomain castelo --token <duckdns-token>              # wildcard, DNS-01 (default)
job-squire dns duckdns NAME --subdomain castelo --token <duckdns-token> --main-only   # main subdomain, HTTP-01
job-squire dns cloudflare NAME --domain example.com --token <cloudflare-api-token>    # wildcard, DNS-01
```

`NAME` is a registered network-mode instance -- used only to reuse its
recorded `runtime` (the same way `job-squire proxy NAME` does), since SWAG
is shared across every instance on that proxy and neither command touches
anything instance-specific otherwise. Both commands prompt for `--token`
(hidden input) if it's omitted rather than accepting it only on the
command line, where it would land in shell history.

`ops/dns.py` implements the two auto-configured paths from PLAN Section 5
("Free and low-cost domain and DNS options for personal use") plus the
resolved auto-configure-versus-document open item in Section 8:

- **DuckDNS (fully automated).** `configure_duckdns` rewrites the
  CLI-installed SWAG's compose file with the operator's subdomain and
  account token and recreates the container. DuckDNS's own tradeoff
  carries straight through unchanged: `--wildcard` (the default) sets
  `VALIDATION=duckdns`, SWAG's native DNS-01 mode that drives DuckDNS's
  TXT-record API directly, no inbound port needed, covering
  `*.<subdomain>.duckdns.org`; `--main-only` sets `VALIDATION=http`
  instead, ordinary HTTP-01 for just `<subdomain>.duckdns.org`, which
  needs port 80 reachable from the internet. SWAG cannot do both from one
  config at once, which is why this is a flag rather than automatic.
- **Cloudflare DNS-01 (semi-automated).** `configure_cloudflare` takes the
  domain and API token the operator already owns and brings with them --
  the one manual input, since the CLI cannot conjure either -- writes
  certbot's Cloudflare DNS-01 plugin credentials to
  `config/dns-conf/cloudflare.ini` (`dns_cloudflare_api_token = ...`,
  permissioned `0600`), and rewrites the compose file with
  `VALIDATION=dns`, `DNSPLUGIN=cloudflare`, `SUBDOMAINS=wildcard`. A
  Cloudflare API token should be scoped to `Zone:DNS:Edit` for the target
  domain, not the legacy account-wide global key.
- **Documented only.** Cloudflare Tunnel and the long tail of other SWAG
  DNS plugins (Route53, Google Domains, Porkbun, DNSimple, and the rest
  SWAG's own `dns-conf/` directory ships templates for) are not wired to
  any command here. Tunnel uses a fundamentally different topology --
  TLS terminates at the tunnel provider, not at a locally facing proxy --
  and the other plugins are an open-ended, provider-specific credentials
  problem this module does not try to enumerate. An operator who wants one
  of these configures it directly against the CLI-installed SWAG's
  `config/` directory (`swag_root()/config`, see `ops/proxy.py`) using
  SWAG's own documentation for that plugin, then restarts the SWAG
  container by hand; nothing about a manually configured SWAG conflicts
  with what `job-squire proxy` already installed. See PLAN Section 5's own
  prose on Tailscale Funnel and Cloudflare Tunnel for the tradeoffs.

Neither automated path will touch a proxy `job-squire proxy` didn't
install itself. If that instance's `proxy` run detected and reused an
existing third-party SWAG or bare nginx instead of installing a fresh one
(`ops/proxy.py`'s `detect_existing_proxy`), `dns duckdns`/`dns cloudflare`
refuse to run (`_managed_swag_target` raises `DnsError`) rather than
guessing at how to reach into a proxy the operator already set up and
already has its own DNS/TLS story for.

**Waiting for the certificate.** After rewriting and recreating SWAG, both
commands poll `docker/podman logs` on the SWAG container for certbot's own
success or failure wording (`_await_certificate`, attempt-counted against
`--timeout`/a fixed 10-second poll interval, not wall-clock-timed, so it's
deterministic under test). A failure marker (a bad token, a DNS record
that doesn't resolve) raises immediately with the recent log tail attached
rather than waiting out the full timeout pointlessly; hitting the timeout
with no marker either way is reported as "not yet issued," not an error,
since DNS propagation and Let's Encrypt's own rate limits can both add
delay a wrong-credentials failure wouldn't. `--no-wait` skips polling
entirely and just applies the configuration.

**Never conjures a domain or working DNS.** Both commands assume the
operator already holds a registered DuckDNS subdomain (free, from
duckdns.org) or a Cloudflare-managed domain and API token before running
either one -- exactly the PLAN Section 5/7 boundary restated: "there is one
thing the CLI cannot conjure: a domain and working DNS." This is a
network-mode-only concern; a local install uses loopback and needs none of
it.

## Tailscale Serve for private remote access (Prompt C11)

```
job-squire tailscale enable NAME                                  # Serve on 443 (web) / 8443 (MCP), the defaults
job-squire tailscale enable NAME --web-port 8443 --mcp-port 10000  # a second Tailscale-enabled instance on one machine
job-squire tailscale disable NAME                                 # back to loopback-only
job-squire tailscale status NAME
```

Only applies to a `local`-mode instance -- this is a private remote-access
path for a local install, not a substitute for network mode's own reverse
proxy (`ops/proxy.py`/`ops/dns.py`), and `ops/tailscale.py` refuses a
`network`-mode instance outright.

**Serve, never Funnel.** `enable` only ever calls `tailscale serve --bg
--https=<port> http://127.0.0.1:<port>`, forwarding to the instance's
existing loopback host port (the same one `create` already published --
nothing about the compose file changes, unlike `job-squire proxy`'s
network-mode provisioning, since Serve runs as a host-level daemon and
reaches the instance the same way any other host process would). Funnel is
public exposure and this module never invokes it. Serve only issues a
valid certificate on three ports -- `443`, `8443`, `10000` -- so an
operator running Tailscale for a second instance on the same machine picks
a different pair from that same set of three with `--web-port`/`--mcp-port`.

**Local mode stays local mode.** Per PLAN Section 5, this is "local mode
with a private Serve front door rather than a separate mode." `enable`
never touches `Instance.mode` or `DEPLOY_MODE` -- both stay `local`. What
it does flip, in the instance's `data/.env`, are the individual overrides
`app/deploy.py`'s `DEPLOY_MODE` resolution already supports independently
of the mode string: `TRUST_PROXY=true`, `SESSION_COOKIE_SECURE=true`, and
`PUBLIC_URL`/`PUBLIC_MCP_URL`/`PUBLIC_MCP_HOST` set to this device's
`<device>.<tailnet>.ts.net` name (from `tailscale status --json`'s
`Self.DNSName`), then recreates the container so the new env takes effect.
`disable` reverts all five to exactly what `create` itself would have
written for a local instance (loopback URLs, both flags off) and recreates
the container again. The registry's `public_url` is updated to match on
both sides, so `job-squire status`/`list` show the real reachable address
while Serve is on.

**A known, expected app-side warning.** `app/deploy.py`'s startup guard
treats `DEPLOY_MODE=local` combined with a non-loopback `PUBLIC_URL` as a
*warning*, not fatal -- the container keeps running. A Tailscale-enabled
instance is exactly that combination by design, so `enable` prints this
expectation up front rather than leaving the operator to discover a
surprise banner and wonder if something broke.

**Where the on/off state lives.** Not the registry -- `Instance` (Prompt
C4) is a fixed, non-secret schema, and this is a toggle on an existing
field's *meaning*, not new instance identity. Instead a small
`tailscale.json` manifest sits beside `docker-compose.yml` in the
instance's own directory (`ops/tailscale.py`'s `read_state`/`is_tailnet_
reachable`), the same per-instance-directory precedent `ops/mcp_token.py`
already established for state with no natural home in the registry.

**MCP over the tailnet, and the reachability rule.** Because `Instance.mode`
stays `"local"` for a Serve-fronted instance, Prompt C6's `is_static_token_
allowed()` check (keyed on `mode`) can't see the tailnet reachability on
its own. `job-squire configure`'s static-token gate additionally consults
`ops/tailscale.py`'s state manifest: a tailnet-reachable instance is
refused the static token exactly like a `network`-mode one, unless the
operator passes the *same* `--allow-network` opt-in C6 defines -- never a
separate flag, never implicit. OAuth remains preferred there, same as any
instance reachable beyond the one machine.

## Local AI capability detection and guided Ollama install (docs/PLAN-ollama-assist.md)

```
job-squire ollama check                          # host-only: RAM/CPU/GPU, tier verdict, recommended models
job-squire ollama check NAME                      # also writes NAME's data/host_capabilities.json
job-squire ollama setup NAME                      # full chain: install, pull, configure, test
job-squire ollama setup NAME --dry-run            # print every step, change nothing
job-squire ollama setup NAME --triage-model qwen3:8b --analysis-model gemma4:12b
job-squire ollama setup NAME --num-ctx 16384       # override the tier's recommended context window
job-squire ollama setup NAME --skip-pull           # already pulled the base models
job-squire ollama setup NAME --skip-derive         # write the base tags as-is (Ollama's 2048-token default applies)
job-squire ollama setup NAME --skip-test           # skip the round-trip generation check
job-squire ollama setup NAME --rank 2              # provider chain position (default: append, or keep existing)
job-squire ollama setup NAME --base-url http://192.168.1.50:11434  # only needed if Ollama is on another machine
job-squire ollama setup NAME --skip-enable-features  # don't flip on Settings' "Automatic Features" toggle
job-squire ollama setup NAME --yes                 # don't ask before installing Ollama
```

**`--base-url` defaults to `http://host.docker.internal:11434`, not `localhost`.** Ollama is expected to
be a native install on the same host as NAME's container (the common case), and plain `localhost` inside
that container always means the container itself, never the host -- so it could never have been a correct
default. `host.docker.internal` resolves out of the box on Docker Desktop/OrbStack (macOS, Windows); on
Linux, `ops/compose.py`'s generated compose file (and the repo's own `docker-compose.yml`) declare
`extra_hosts: ["host.docker.internal:host-gateway"]` unconditionally so the same name resolves there too
(Docker Engine 20.10+). Only pass `--base-url` explicitly when Ollama runs on a different machine.

**`setup` also enables Settings' "Automatic Features" toggle (`ai_config.api_enabled`) by default.**
Writing the provider row alone doesn't make auto-triage/follow-up drafts/weekly review start running --
those are independently gated on that toggle in `app/worker.py`. Pass `--skip-enable-features` to configure
Ollama for manual/MCP-only use without flipping it.

**Why `check` runs on the host.** A containerized in-app detector sees the
Docker Desktop/Podman machine VM's RAM allocation, not the real machine's --
and never sees an Apple Silicon GPU at all. `job-squire ollama check` runs
directly on the host (as every other command in this file already does),
so it's the authoritative source the plan's "Container Blindness" section
calls for. Passing `NAME` additionally writes the result into that
instance's `data/host_capabilities.json` -- `data/`, not the instance root,
though note `data/` is host-only now (only `data/.env` is still bind-mounted
into the container; the rest of `/data` is a named Docker volume, see
`docs/PLAN-deployment-modes.md`), so this file is CLI/host tooling reading
CLI/host tooling's own output, not something the running app can see. The
web app's own onboarding integration this was meant to feed was never
actually built.

**Tier -> model mapping is data, not doctrine.** `ops/ollama_assist.py`'s
`TIER_TABLE` maps five tiers (not-reasonable / entry / capable / strong /
workstation) to a triage model and an analysis model each, verified
against https://ollama.com/library on 2026-07-16. It will age -- re-check
before trusting it far into the future. Below the entry tier, `check`
explains *why* Ollama isn't recommended rather than hiding it, and points
at a free cloud provider instead.

**`setup`'s chain, each step skippable or dry-run-able:** detect -> install
(official channel only -- Homebrew formula on macOS, the official install
script on Linux, winget on Windows; skipped if Ollama already works) ->
start/verify the service -> `ollama pull` the two recommended (or
overridden) base tags -> derive a context-sized model from each
(`ollama create <tag>-ctx<n>` from a generated Modelfile -- see below) ->
write the `ai_provider_configs` row directly via `sqlite3` (this package
never depends on Flask/SQLAlchemy/the app package, same as
`ops/secrets_copy.py`) -> a direct round-trip generation request against
Ollama's own API to confirm it actually answers.

**Why there's a "derive" step.** Ollama's OpenAI-compatible endpoint (what
app/ai.py calls) has no per-request way to set context size -- confirmed
against https://docs.ollama.com/api/openai-compatibility, which prescribes
exactly one method: a Modelfile's `PARAMETER num_ctx`, applied when the
model is created, then referenced by its derived name. `setup` does this
automatically (e.g. `qwen3:8b` -> `qwen3:8b-ctx16384`) and writes the
*derived* name into the provider row, not the base tag. `--skip-derive`
opts out (the base tag is used as-is, at Ollama's 2048-token default).

**The `num_ctx` gap is closed app-side too (2026-07-16).**
`AIProviderConfig.num_ctx` (app/models.py, additive migration in
app/__init__.py) records what a provider's model was actually built with.
`call_with_fallback` (app/ai.py) estimates whether a prompt will fit before
calling a provider with `num_ctx` set, skipping to the next provider in the
chain instead of risking a silently truncated response -- the same
mechanism an unmet `use_for_triage`/`use_for_analysis` flag already uses.
If every eligible provider gets skipped this way, the error says so
explicitly. `triage_model` and `num_ctx` are also now editable directly in
Settings → AI providers (previously `triage_model` had no form field at
all, and was actually being ignored by every triage call regardless --
both fixed in the same change).

**Not the app's own "Test Ollama" button.** Step 6 above talks to Ollama's
API directly rather than through `app/ai.py`'s provider adapter, which
runs inside the container this host-side CLI doesn't import. It's a
useful pre-flight check, not a substitute for the in-app check the plan's
web-onboarding/Settings work will eventually add.

## Versioning

The old split was `0.1.0-<sha>` for the app versus `0.1.0+<sha>` for
`jobsquire-cli`. That wasn't two competing conventions so much as two
targets with different syntax rules colliding on the same idea:

- An **OCI image tag** (the Docker image this repo publishes) cannot
  contain a `+` — the tag character set is `[a-zA-Z0-9_.-]` — so a hyphen
  is the only option there.
- A **Python package version** should be PEP 440-valid so `pip` and
  friends parse it correctly, and PEP 440's local-version-identifier
  syntax is specifically `<public-version>+<local-label>` — a `+` is the
  correct separator there, not a stylistic choice.

The fix is one source of truth, rendered two ways for two targets that
each require a different separator, rather than two independent schemes:

- **Single source of truth:** the root `VERSION` file (currently `0.5.0`).
- **Docker image tag** (`.github/workflows/ci.yml`, `BUILD_VERSION`):
  `<VERSION>-<short-sha>`, e.g. `0.5.0-162722a`. Unchanged by this prompt.
- **`job-squire-cli` package version:** `<VERSION>+<short-sha>`, e.g.
  `0.5.0+162722a`, PEP 440-valid. Produced by
  `scripts/stamp_cli_version.py` (repo root), which rewrites
  `job_squire_cli/pyproject.toml`'s `version` field from the same
  `VERSION` file plus `git rev-parse --short HEAD`. `.github/workflows/
  release.yml` runs this and commits the result automatically whenever
  `VERSION` changes, retargeting that release's tag at the resulting
  commit -- so every tagged release (and everything `bootstrap.sh`
  installs from one) always carries a correctly stamped version. The
  committed value in `pyproject.toml` between releases is a `+dev`
  placeholder, not a real one; don't hand-run the script expecting that
  placeholder to matter outside of local/manual builds.

Both numbers always agree on the base (`0.5.0` in the example above) and
differ only in the separator their target format requires.
