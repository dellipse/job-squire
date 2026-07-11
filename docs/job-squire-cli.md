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
    ops/commands.py    # deployment/lifecycle command stubs (real behavior: C2-C11)
    ops/runtime.py     # container runtime detection and per-OS install (Prompt C3)
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
