# Copyright (C) 2026 D. Brandmeyer
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""Per-instance compose/env file generation and the runtime-driven compose
invocations that drive them (Prompt C5).

Two things are deliberately hand-rolled as f-string templates rather than
built with a YAML/dotenv library: neither `job-squire-cli`'s core
dependency set (just `click`, see pyproject.toml) nor the app's own
`docker-compose.yml` need a YAML library, and adding one only for
this would be new dependency weight for a fixed, small, well-tested shape.

The generated compose file is deliberately NOT the repo's own
docker-compose.yml copied verbatim: that file has a `build:` block
(for local development from a checkout) which a CLI-created instance must
not have -- an operator using the CLI never clones the app repo, so there
is nothing for `build:` to point at, and CLI-created instances always run
the published `ghcr.io/dellipse/job-squire` image. The two files describe
the same runtime shape (PLAN Section 2's single s6-supervised container,
one named-volume-backed /data plus a single bind-mounted data/.env, one
aggregated healthcheck) and are kept in sync by hand; drift between them
is a one-file diff to check.

PLAN Section 7's "direct runtime access remains available" is why every
generated instance is a complete, ordinary compose project in its own
directory (see ops/paths.py): `cd` into it and run `docker compose ...` or
`podman compose ...` directly, with no CLI-specific machinery required to
read the files it left behind.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import dotenv, paths

DEFAULT_IMAGE = "ghcr.io/dellipse/job-squire:latest"

Runner = Callable[..., "subprocess.CompletedProcess[str]"]

# Runtimes that speak the `docker` CLI directly (PLAN Section 6: "on
# OrbStack and Colima the docker CLI is provided, so Docker commands work
# there as well"), versus podman's own CLI/compose plugin.
_DOCKER_LIKE = {"docker", "orbstack", "colima"}
_PODMAN_LIKE = {"podman"}


class ComposeError(RuntimeError):
    """Raised for an unrecognized runtime or a failed compose invocation."""


# ── Runtime -> CLI translation ──────────────────────────────────────────


def runtime_binary(runtime: str) -> str:
    """The single-binary CLI (for `inspect`/`logs`) this runtime provides."""
    if runtime in _DOCKER_LIKE:
        return "docker"
    if runtime in _PODMAN_LIKE:
        return "podman"
    raise ComposeError(f"Unknown runtime {runtime!r} -- expected one of docker, podman, orbstack, colima.")


def compose_binary(runtime: str) -> tuple[str, ...]:
    """The compose sub-invocation for this runtime.

    `docker compose` / `podman compose` (the plugin form) rather than the
    legacy standalone `docker-compose`/`podman-compose` scripts -- both
    runtimes bundle the plugin form today, and this is what keeps "docker
    compose, podman compose, and their differences stay hidden" (PLAN
    Section 7) to exactly this one function.
    """
    return (runtime_binary(runtime), "compose")


def data_volume_key(container_name: str) -> str:
    """The literal volume key this CLI's own generated compose file declares
    for `container_name` (`render_compose_yaml`'s `volumes:` block) -- the
    single source of truth so `ops/lifecycle.py`'s leftover-volume checks
    (before `create`) and cleanup (in `remove_instance`) always agree with
    what `create` actually wrote, rather than each re-deriving the `-data`
    suffix convention independently and risking drift between them."""
    return f"{container_name}-data"


# ── Rendering ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class InstanceEnv:
    """Everything `render_data_env` needs, gathered in one place so
    `lifecycle.create_instance` has one call to make instead of scattering
    env-var construction across two modules."""

    secret_key: str
    admin_username: str
    admin_password: str
    admin_name: str = "Admin"
    user_username: str = "user"
    user_name: str = "User"
    user_password: str = ""
    instance_name: str = ""
    cookie_name: str = ""
    deploy_mode: str = "local"
    public_url: str = ""
    public_mcp_url: str = ""
    public_mcp_host: str = ""
    mcp_port: int = 9000
    trust_proxy: bool | None = None  # None = let DEPLOY_MODE's preset decide
    session_cookie_secure: bool | None = None  # same
    # Extra raw KEY=VALUE lines appended verbatim, e.g. the schedule
    # variables (SCHEDULE_TZ, SCHEDULE_WEEKDAY_HOURS, ...) that `create
    # --import-from` copies from another instance's data/.env
    # (ops/secrets_copy.py's read_schedule_env). Not modeled as their own
    # InstanceEnv fields because they have no CLI-side meaning beyond
    # passthrough -- the app is the only thing that interprets them.
    extra: dict[str, str] = field(default_factory=dict)


def render_compose_yaml(
    *, container_name: str, image: str, loopback_only: bool, proxy_network: str | None = None,
) -> str:
    """The instance's docker-compose.yml.

    `loopback_only` mirrors PLAN Section 3's "Host publish interface" row:
    local mode always binds 127.0.0.1 only; network mode binds 0.0.0.0
    behind the operator's own firewall/proxy, per that same table's
    documented alternative.

    `proxy_network` is Prompt C9's shared proxy network: when a network-mode
    instance has been provisioned behind SWAG or another nginx proxy
    (ops/proxy.py), the container also joins this external Docker network
    so the proxy can resolve it by container name (`set $upstream_app
    <container_name>;` in the generated nginx conf) rather than guessing at
    a host IP from inside its own network namespace. This is *in addition
    to* the host-port publish above, not a replacement for it -- direct
    host-port access still works for troubleshooting, and nothing about
    local mode changes (`proxy_network` is only ever passed for a
    network-mode instance).
    """
    bind_host = "127.0.0.1" if loopback_only else "0.0.0.0"
    networks_service_block = f"""\
    networks:
      - {proxy_network}
""" if proxy_network else ""
    networks_top_block = f"""
networks:
  {proxy_network}:
    external: true
""" if proxy_network else ""
    return f"""\
# Generated by job-squire create -- do not hand-edit port/image here without
# also updating the instance registry (job-squire status will report drift).
# See docs/PLAN-deployment-modes.md Section 2 for what this container runs.
services:
  job-squire:
    image: {image}
    container_name: {container_name}
    restart: unless-stopped
    environment:
      PUID: "${{PUID:-1000}}"
      PGID: "${{PGID:-1000}}"
      UMASK: "${{UMASK:-022}}"
    env_file:
      - data/.env
    volumes:
      # Named volume, not a host bind mount -- see docs/PLAN-deployment-modes.md
      # Section 2 and app/db_utils.py's module docstring for why (WAL-mode
      # SQLite over a bind mount bridged through OrbStack/Docker Desktop's
      # VM filesystem layer intermittently throws "disk I/O error" under
      # concurrent access; a named volume is native daemon-managed storage,
      # not bridged through the host filesystem). `job-squire backup`/
      # `restore` (ops/backup.py) read and write this volume's contents
      # through the container itself (docker/podman exec + cp), never by
      # walking a host path directly.
      - {data_volume_key(container_name)}:/data
      # data/.env is the one thing that stays a plain host file: `env_file:`
      # above is read by `docker compose` itself at "up" time, before the
      # named volume even exists, so it can never point inside one.
      # Layering this single-file bind mount on top of the named volume's
      # mount point is safe -- a lone small text file is never touched
      # concurrently by SQLite the way the database itself was.
      - ./data/.env:/data/.env:ro
    healthcheck:
      test: ["CMD", "/etc/s6-overlay/scripts/healthcheck"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 45s
    ports:
      - "{bind_host}:${{APP_HOST_PORT:-8080}}:8000"
      - "{bind_host}:${{MCP_HOST_PORT:-9000}}:${{MCP_PORT:-9000}}"
    # Lets the container reach a service running natively on this same host
    # (e.g. Ollama) via the literal name "host.docker.internal" -- Docker
    # Desktop/OrbStack already provide that DNS entry for free on macOS/
    # Windows, but plain Docker Engine on Linux needs it spelled out via the
    # special "host-gateway" target (supported on Engine 20.10+). Harmless
    # to declare on every platform: on Desktop/OrbStack this just adds a
    # second /etc/hosts line pointing at the same address they already
    # resolve. See ops/ollama_assist.py's OLLAMA_CONTAINER_HOST, which is
    # what this instance's Ollama provider base_url defaults to.
    extra_hosts:
      - "host.docker.internal:host-gateway"
{networks_service_block}{networks_top_block}
volumes:
  # An explicit `name:` here is what keeps the volume Docker/Podman actually
  # create as exactly `data_volume_key(container_name)` -- without it,
  # Compose's default naming prefixes the volume key with the project name
  # (`-p {container_name}` above), and since `container_name` is *also* the
  # volume key's own prefix, the result would be doubled up
  # (`{container_name}_{container_name}-data` instead of the
  # `{container_name}-data` every other part of this CLI, including the
  # leftover-volume check in ops/lifecycle.py's `create_instance`, expects).
  {data_volume_key(container_name)}:
    name: {data_volume_key(container_name)}
"""


def render_compose_env(
    *, puid: int = 1000, pgid: int = 1000, umask: str = "022",
    app_port: int | None, mcp_port: int | None,
) -> str:
    """Compose-level `.env` (variable substitution for the compose file
    itself, read by `docker compose`/`podman compose` -- NOT forwarded into
    the container; that's `data/.env`, rendered by `render_data_env`).

    No DATA_HOST_DIR here anymore: the app's persistent data lives in a
    named volume (render_compose_yaml), which compose addresses by name,
    not by a configurable host path. The only host path the compose file
    still references (`data/.env`) is a fixed relative path, not a variable,
    since it never needs to move independently of the instance directory
    itself.
    """
    lines = [
        "# Generated by job-squire -- compose-level variables only.",
        f"PUID={puid}",
        f"PGID={pgid}",
        f"UMASK={umask}",
    ]
    if app_port is not None:
        lines.append(f"APP_HOST_PORT={app_port}")
    if mcp_port is not None:
        lines.append(f"MCP_HOST_PORT={mcp_port}")
    return "\n".join(lines) + "\n"


def _bool_env(value: bool) -> str:
    return "true" if value else "false"


def render_data_env(env: InstanceEnv) -> str:
    """The container's `data/.env` -- everything from examples/.env.example
    that a CLI-created instance needs, with SESSION_COOKIE_NAME set
    *explicitly* to the registry's derived cookie name rather than left to
    the app's own INSTANCE_NAME-based derivation: the app derivation
    lowercases and turns BOTH hyphens and spaces into underscores
    (app/__init__.py), while the registry's slug allows hyphens, so for any
    instance name containing a hyphen the two derivations would disagree.
    Setting SESSION_COOKIE_NAME here keeps them identical by construction.
    """
    lines = [
        "# Generated by job-squire create. SECRET_KEY is unique to this",
        "# instance -- never copy it to another instance's data/.env.",
        f"SECRET_KEY={env.secret_key}",
        "",
        f"ADMIN_USERNAME={env.admin_username}",
        f"ADMIN_NAME={env.admin_name}",
        f"ADMIN_PASSWORD={env.admin_password}",
        "",
        f"USER_USERNAME={env.user_username}",
        f"USER_NAME={env.user_name}",
        f"USER_PASSWORD={env.user_password}",
        "",
        f"INSTANCE_NAME={env.instance_name}",
        f"SESSION_COOKIE_NAME={env.cookie_name}",
        "",
        f"DEPLOY_MODE={env.deploy_mode}",
    ]
    if env.trust_proxy is not None:
        lines.append(f"TRUST_PROXY={_bool_env(env.trust_proxy)}")
    if env.session_cookie_secure is not None:
        lines.append(f"SESSION_COOKIE_SECURE={_bool_env(env.session_cookie_secure)}")
    if env.public_url:
        lines.append(f"PUBLIC_URL={env.public_url}")
    if env.public_mcp_url:
        lines.append(f"PUBLIC_MCP_URL={env.public_mcp_url}")
    if env.public_mcp_host:
        lines.append(f"PUBLIC_MCP_HOST={env.public_mcp_host}")
    lines.append(f"MCP_PORT={env.mcp_port}")
    if env.extra:
        lines.append("")
        lines.append("# Imported from another instance (job-squire create --import-from).")
        lines.extend(f"{k}={v}" for k, v in env.extra.items())
    return "\n".join(lines) + "\n"


def _write_secret_file(path: Path, content: str) -> None:
    """Write `content` to `path` with 0600 permissions in place from creation,
    rather than write-then-chmod, so the file holding SECRET_KEY/
    ADMIN_PASSWORD is never briefly readable at the umask-default mode
    between the write and a follow-up chmod call. If `path` already exists
    (e.g. a re-run of `create`), O_CREAT's mode argument is ignored
    by POSIX for pre-existing files, so we chmod unconditionally afterward
    too -- belt and suspenders, not a substitute for the O_CREAT mode on the
    common (fresh file) path.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        f = os.fdopen(fd, "w")
    except BaseException:
        os.close(fd)
        raise
    with f:
        f.write(content)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def write_compose_files(
    root: Path, *, container_name: str, image: str, loopback_only: bool,
    app_port: int | None, mcp_port: int | None, proxy_network: str | None = None,
    puid: int = 1000, pgid: int = 1000, umask: str = "022",
) -> None:
    """Write docker-compose.yml and the compose-level .env under
    `root`. Never touches `data/.env` -- split out from
    `write_instance_files` so a rewrite of just these two files (see
    `proxy_network` below) never risks touching the container-level secrets
    in `data/.env`.

    `proxy_network` (Prompt C9) is a surgical, idempotent rewrite: calling
    this again on an already-created instance to attach it to a reverse
    proxy's shared network only changes the `networks:` block, leaving
    image/ports/healthcheck exactly as `create` wrote them.
    """
    root.mkdir(parents=True, exist_ok=True)
    paths.compose_path(root).write_text(
        render_compose_yaml(
            container_name=container_name, image=image, loopback_only=loopback_only,
            proxy_network=proxy_network,
        )
    )
    paths.compose_env_path(root).write_text(
        render_compose_env(puid=puid, pgid=pgid, umask=umask, app_port=app_port, mcp_port=mcp_port)
    )


def write_instance_files(root: Path, *, container_name: str, image: str, loopback_only: bool,
                          app_port: int | None, mcp_port: int | None, env: InstanceEnv) -> None:
    """Write docker-compose.yml, .env, and data/.env under `root`, creating
    `root/data` (which now holds only `.env` -- the app's actual data lives
    in a named Docker volume, not this directory). Does not touch the
    registry or start anything -- see ops/lifecycle.py for orchestration.
    """
    data = paths.data_dir(root)
    data.mkdir(parents=True, exist_ok=True)
    write_compose_files(
        root, container_name=container_name, image=image, loopback_only=loopback_only,
        app_port=app_port, mcp_port=mcp_port,
    )
    data_env = paths.data_env_path(root)
    _write_secret_file(data_env, render_data_env(env))  # holds SECRET_KEY and ADMIN_PASSWORD


# ── Driving the runtime ──────────────────────────────────────────────────


def _compose_argv(runtime: str, root: Path, project: str) -> list[str]:
    return [
        *compose_binary(runtime),
        "--project-directory", str(root),
        "-f", str(paths.compose_path(root)),
        "--env-file", str(paths.compose_env_path(root)),
        "-p", project,
    ]


def _run_compose(runtime: str, root: Path, project: str, args: list[str], *,
                  run: Runner, timeout: float = 120.0) -> "subprocess.CompletedProcess[str]":
    argv = _compose_argv(runtime, root, project) + args
    try:
        return run(argv, cwd=str(root), capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ComposeError(f"Failed to run {' '.join(argv)}: {exc}") from exc


def compose_up(runtime: str, root: Path, project: str, *, run: Runner = subprocess.run,
                timeout: float = 180.0, extra_args: list[str] | None = None) -> "subprocess.CompletedProcess[str]":
    return _run_compose(runtime, root, project, ["up", "-d", *(extra_args or [])], run=run, timeout=timeout)


def compose_create(runtime: str, root: Path, project: str, *, run: Runner = subprocess.run,
                    timeout: float = 180.0) -> "subprocess.CompletedProcess[str]":
    """`compose create`: materializes the container (and any named volumes
    its service declares) without starting anything. `restore_instance`
    (ops/backup.py) uses this so the container's /data volume exists as a
    target for a `docker cp` before the app's own processes ever touch
    it -- copying into a *running* container risks the app writing its own
    fresh, empty database in the same instant the restore is trying to
    place the real one there."""
    return _run_compose(runtime, root, project, ["create"], run=run, timeout=timeout)


def compose_stop(runtime: str, root: Path, project: str, *, run: Runner = subprocess.run,
                  timeout: float = 60.0) -> "subprocess.CompletedProcess[str]":
    return _run_compose(runtime, root, project, ["stop"], run=run, timeout=timeout)


def compose_start(runtime: str, root: Path, project: str, *, run: Runner = subprocess.run,
                   timeout: float = 120.0) -> "subprocess.CompletedProcess[str]":
    return _run_compose(runtime, root, project, ["start"], run=run, timeout=timeout)


def compose_restart(runtime: str, root: Path, project: str, *, run: Runner = subprocess.run,
                     timeout: float = 120.0) -> "subprocess.CompletedProcess[str]":
    return _run_compose(runtime, root, project, ["restart"], run=run, timeout=timeout)


def compose_down(runtime: str, root: Path, project: str, *, run: Runner = subprocess.run,
                  timeout: float = 60.0, remove_volumes: bool = False) -> "subprocess.CompletedProcess[str]":
    """`compose down`, optionally with `-v` to also remove the named
    volume(s) this project's compose file declares. `/data` is a named
    Docker volume, not a host bind mount (render_compose_yaml) -- plain
    `compose down` never touches it, so `remove_volumes=True` is what
    `ops/lifecycle.py`'s `remove_instance` passes exactly when the operator
    chose to delete the instance's data; left False (the default), every
    other caller (`update`/`rollback`'s stop-then-recreate, `restore`'s
    teardown-before-replace) keeps behaving exactly as before this flag was
    added."""
    args = ["down", "-v"] if remove_volumes else ["down"]
    return _run_compose(runtime, root, project, args, run=run, timeout=timeout)


def list_matching_volumes(runtime: str, name_substring: str, *, run: Runner = subprocess.run,
                           timeout: float = 15.0) -> list[str]:
    """Every volume whose name contains `name_substring` (Docker/Podman's
    own `volume ls --filter name=` substring match -- both CLIs support this
    filter form, so no project-prefix-naming assumption is needed to find
    them). Used by `ops/lifecycle.py`'s `create_instance` to spot a leftover
    volume from a same-named instance that was removed with its data kept,
    before that new instance silently reattaches to the old database, and
    by `remove_instance` as a belt-and-suspenders sweep after `compose down
    -v` to confirm (or catch what a compose file already gone from disk
    couldn't reach). Returns an empty list on any runtime error rather than
    raising -- an unreadable `volume ls` should never itself block create/
    remove; the caller just proceeds as if nothing leftover was found."""
    argv = [runtime_binary(runtime), "volume", "ls", "--format", "{{.Name}}", "--filter", f"name={name_substring}"]
    try:
        result = run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def remove_volume(runtime: str, volume_name: str, *, run: Runner = subprocess.run,
                   timeout: float = 30.0) -> "subprocess.CompletedProcess[str]":
    """`docker/podman volume rm <volume_name>` directly. A nonzero exit
    (e.g. the volume is already gone, or something outside job-squire's own
    registry still has it mounted) is returned for the caller to report,
    not raised -- mirrors `remove_image`'s own "never let a stubborn
    resource block the rest of a remove/uninstall" rule."""
    argv = [runtime_binary(runtime), "volume", "rm", volume_name]
    try:
        return run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ComposeError(f"Failed to remove volume {volume_name!r}: {exc}") from exc


def pull_image(runtime: str, image: str, *, run: Runner = subprocess.run,
                timeout: float = 300.0) -> "subprocess.CompletedProcess[str]":
    """`docker/podman pull <image>` directly (not via compose), so `update`
    (Prompt C7) can download the target version *before* touching the
    running container -- if the pull fails, nothing about the instance has
    changed yet."""
    argv = [runtime_binary(runtime), "pull", image]
    try:
        return run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ComposeError(f"Failed to pull {image}: {exc}") from exc


def remove_image(runtime: str, image: str, *, run: Runner = subprocess.run,
                  timeout: float = 60.0) -> "subprocess.CompletedProcess[str]":
    """`docker/podman rmi <image>` directly. `compose down` (what
    `remove`/`uninstall` already run) never removes the image itself, only
    the container and network -- this is the only place in the CLI that
    calls `rmi`. Only ever invoked by a caller (ops/lifecycle.py's
    remove_instance/uninstall_everything) that has already confirmed no
    *other* registered instance's compose file still references this
    image, so a shared `:latest` tag used by a sibling instance is never
    pulled out from under it. A nonzero exit here (e.g. something outside
    job-squire's own registry is still using the image) is returned for
    the caller to report, not raised -- a stubborn image should never
    block the rest of a remove/uninstall."""
    argv = [runtime_binary(runtime), "rmi", image]
    try:
        return run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ComposeError(f"Failed to remove image {image!r}: {exc}") from exc


# ── Version movement (update / rollback, Prompt C7) ─────────────────────

_IMAGE_LINE_RE = re.compile(r"^(\s*image:\s*)(\S+)\s*$", re.MULTILINE)


def resolve_image(version: str, *, repo: str | None = None) -> str:
    """A bare tag (`latest`, `0.7.0`, `sha-abc1234`) becomes a full image
    ref against `repo` (default: DEFAULT_IMAGE's own repo). A value that
    already looks like a full ref (contains `/`, i.e. a registry/repo
    path) passes through unchanged -- this is what lets `rollback` feed a
    previously-recorded full ref straight back into `resolve_image`
    without it being mistaken for a bare tag."""
    if "/" in version:
        return version
    base = repo or DEFAULT_IMAGE.rsplit(":", 1)[0]
    return f"{base}:{version}"


def read_image(root: Path) -> str:
    """The `image:` ref currently written into this instance's
    docker-compose.yml."""
    path = paths.compose_path(root)
    match = _IMAGE_LINE_RE.search(path.read_text())
    if not match:
        raise ComposeError(f"No 'image:' line found in {path}.")
    return match.group(2)


def write_image(root: Path, image: str) -> None:
    """Rewrite just the `image:` line in place -- everything else in
    docker-compose.yml (container_name, ports, healthcheck) is left
    untouched, so `update` never has to re-derive loopback/network binding
    or re-guess the container name."""
    path = paths.compose_path(root)
    text = path.read_text()
    new_text, count = _IMAGE_LINE_RE.subn(rf"\g<1>{image}", text, count=1)
    if count == 0:
        raise ComposeError(f"No 'image:' line found in {path}.")
    path.write_text(new_text)


def read_compose_env_value(root: Path, key: str) -> str | None:
    return dotenv.get(paths.compose_env_path(root), key)


def set_compose_env_value(root: Path, key: str, value: str) -> None:
    dotenv.set_line(paths.compose_env_path(root), key, value)


# ── Observing container state (status / remove / surfacing failures) ────


def inspect_state(runtime: str, container_name: str, *, run: Runner = subprocess.run) -> dict | None:
    """`{{json .State}}` for the named container, or None if it doesn't
    exist. Works identically against `docker inspect` and `podman inspect`
    -- both accept the same `--format` Go-template flag and produce the
    same `.State.Status` / `.State.Health.Status` shape.
    """
    argv = [runtime_binary(runtime), "inspect", "--format", "{{json .State}}", container_name]
    try:
        result = run(argv, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        return None


def container_logs(runtime: str, container_name: str, *, run: Runner = subprocess.run,
                    tail: int = 200) -> str:
    argv = [runtime_binary(runtime), "logs", "--tail", str(tail), container_name]
    try:
        result = run(argv, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (result.stdout or "") + (result.stderr or "")


def extract_fatal_lines(log_text: str) -> list[str]:
    """The exact `FATAL: ...` lines app/deploy.py's startup guard prints to
    stderr on an unsafe config (enforce_startup_guard), so the CLI can
    reprint the app's own reason and fix instead of a generic container
    error (PLAN Section 7 "Surfacing failures").
    """
    return [line for line in log_text.splitlines() if line.startswith("FATAL:")]
