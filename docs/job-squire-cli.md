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
    ops/commands.py    # deployment/lifecycle click commands (C5); C6-C8 stubs remain
    ops/runtime.py     # container runtime detection and per-OS install (Prompt C3)
    ops/registry.py    # cross-platform instance registry (Prompt C4)
    ops/paths.py       # per-instance directory layout (Prompt C5)
    ops/ports.py       # local-mode port pair allocation (Prompt C5)
    ops/compose.py     # compose/env rendering + runtime-driven compose invocations (C5)
    ops/secrets_copy.py  # Fernet-aware settings import between instances (Prompt C5)
    ops/lifecycle.py   # create/start/stop/restart/status/list/remove orchestration (C5)
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
| Deployment/lifecycle | `job-squire <cmd>` (flat, top level) | `create`, `start`, `stop`, `restart`, `status`, `list`, `update`, `remove`, `configure`, `backup`, `restore` |
| Query | `job-squire query <cmd>` | `health`, `list`, `pipeline`, `contacts`, `job`, `contact`, `followups` |

The deployment group is new in this fold-in; its commands are structural
placeholders as of Prompt C1 (grammar and `--help` text are real, behavior
is not) and land incrementally in Prompts C2-C11 of
`docs/PROMPTS-deployment-cli.md`.

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

1. `JOB_SQUIRE_MCP_URL` / `JOB_SQUIRE_MCP_TOKEN` environment variables.
2. A JSON file at the per-OS, per-user config directory (the same one the
   instance registry from `PLAN-deployment-modes.md` Section 4 uses):
   `~/.config/job-squire/mcp.json` (Linux, honoring `XDG_CONFIG_HOME`),
   `~/Library/Application Support/job-squire/mcp.json` (macOS),
   `%APPDATA%\job-squire\mcp.json` (Windows). Shape:
   `{"endpoint": "http://localhost:9000", "token": "jsq_mcp_..."}`.

Prompt C6 replaces the second source with real `job-squire configure`
plumbing (generate/rotate/revoke, multi-instance support, the loopback-
reachability rule); the location and shape above are expected to grow to
match what C6 builds, not to be replaced by something unrelated.

**No Hermes involvement, in either direction.** The query group does not
read `~/.hermes/`, does not import or vendor any Hermes code, and does not
require Hermes to be installed. It talks Streamable HTTP directly to
`app/mcp_server.py`'s `/mcp` endpoint using the same `mcp` library the
server itself depends on. Hermes (or any other MCP host) can still use the
Job Squire MCP server by reading its published MCP documentation; that
coupling runs one way, through docs, never through shared code.

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
real as of Prompt C5, wired to `ops/lifecycle.py`; `update`, `configure`,
`backup`, and `restore` remain structural stubs until C6-C8. Every real
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
  docker-compose.single.yml   # generated; image pinned, no build: block
  .env                        # compose-level vars (PUID/PGID, host ports)
  data/
    .env                      # container env (SECRET_KEY, DEPLOY_MODE, ...)
    job-squire.db             # created by the app on first boot
    uploads/
```

This is deliberately the whole thing an operator needs for "direct runtime
access remains available" (PLAN Section 7): `cd` into it and run
`docker compose ...` or `podman compose ...` directly. It's also the exact
directory registered as the instance's `data_dir`, since a future backup
archive (Prompt C8) needs to capture `SECRET_KEY` along with the database.

**Compose/env rendering** (`ops/compose.py`). The generated
`docker-compose.single.yml` is *not* a copy of the repo's own file of the
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
data directory; if nothing asks (no `confirm_delete`, no explicit
`keep_data`), the default is to keep the data -- PLAN Section 4's rule
that removing an instance must never silently destroy someone's
job-search history.

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
  `VERSION` file plus `git rev-parse --short HEAD`. Run it before building
  or publishing the CLI package; the committed value in `pyproject.toml`
  between stamps is a `+dev` placeholder, not a real release version.

Both numbers always agree on the base (`0.5.0` in the example above) and
differ only in the separator their target format requires.
