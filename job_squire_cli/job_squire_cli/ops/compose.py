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
`docker-compose.single.yml` need a YAML library, and adding one only for
this would be new dependency weight for a fixed, small, well-tested shape.

The generated compose file is deliberately NOT the repo's own
docker-compose.single.yml copied verbatim: that file has a `build:` block
(for local development from a checkout) which a CLI-created instance must
not have -- an operator using the CLI never clones the app repo, so there
is nothing for `build:` to point at, and CLI-created instances always run
the published `ghcr.io/dellipse/job-squire` image. The two files describe
the same runtime shape (PLAN Section 2's single s6-supervised container,
one bind-mounted /data, one aggregated healthcheck) and are kept in sync
by hand; drift between them is a one-file diff to check.

PLAN Section 7's "direct runtime access remains available" is why every
generated instance is a complete, ordinary compose project in its own
directory (see ops/paths.py): `cd` into it and run `docker compose ...` or
`podman compose ...` directly, with no CLI-specific machinery required to
read the files it left behind.
"""
from __future__ import annotations

import json
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


def render_compose_yaml(*, container_name: str, image: str, loopback_only: bool) -> str:
    """The instance's docker-compose.single.yml.

    `loopback_only` mirrors PLAN Section 3's "Host publish interface" row:
    local mode always binds 127.0.0.1 only; network mode (until Prompt C9
    wires up a shared proxy network) binds 0.0.0.0 behind the operator's
    own firewall/proxy, per that same table's documented alternative.
    """
    bind_host = "127.0.0.1" if loopback_only else "0.0.0.0"
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
      - ${{DATA_HOST_DIR:-./data}}:/data
    healthcheck:
      test: ["CMD", "/etc/s6-overlay/scripts/healthcheck"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 45s
    ports:
      - "{bind_host}:${{APP_HOST_PORT:-8080}}:8000"
      - "{bind_host}:${{MCP_HOST_PORT:-9000}}:${{MCP_PORT:-9000}}"
"""


def render_compose_env(
    *, puid: int = 1000, pgid: int = 1000, umask: str = "022", data_host_dir: str = "./data",
    app_port: int | None, mcp_port: int | None,
) -> str:
    """Compose-level `.env` (variable substitution for the compose file
    itself, read by `docker compose`/`podman compose` -- NOT forwarded into
    the container; that's `data/.env`, rendered by `render_data_env`).

    `data_host_dir` defaults to `./data` (a fresh `create`-made instance's
    own layout, ops/paths.py). `adopt` (Prompt C7) passes the existing
    install's own `DATA_HOST_DIR` instead -- often an absolute path
    written by install.sh -- so the bind mount keeps pointing at the same
    data it always has.
    """
    lines = [
        "# Generated by job-squire -- compose-level variables only.",
        f"PUID={puid}",
        f"PGID={pgid}",
        f"UMASK={umask}",
        f"DATA_HOST_DIR={data_host_dir}",
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


def write_compose_files(
    root: Path, *, container_name: str, image: str, loopback_only: bool,
    app_port: int | None, mcp_port: int | None,
    puid: int = 1000, pgid: int = 1000, umask: str = "022", data_host_dir: str = "./data",
) -> None:
    """Write docker-compose.single.yml and the compose-level .env under
    `root`. Never touches `data/.env` -- split out from
    `write_instance_files` so `adopt` (Prompt C7) can generate just these
    two files alongside an *existing* install's `data/.env`, which it must
    leave alone beyond its own two additive lines (ops/lifecycle.py's
    adopt_instance).
    """
    root.mkdir(parents=True, exist_ok=True)
    paths.compose_path(root).write_text(
        render_compose_yaml(container_name=container_name, image=image, loopback_only=loopback_only)
    )
    paths.compose_env_path(root).write_text(
        render_compose_env(
            puid=puid, pgid=pgid, umask=umask, data_host_dir=data_host_dir,
            app_port=app_port, mcp_port=mcp_port,
        )
    )


def write_instance_files(root: Path, *, container_name: str, image: str, loopback_only: bool,
                          app_port: int | None, mcp_port: int | None, env: InstanceEnv) -> None:
    """Write docker-compose.single.yml, .env, and data/.env under `root`,
    creating `root/data` (the bind-mount target). Does not touch the
    registry or start anything -- see ops/lifecycle.py for orchestration.
    """
    data = paths.data_dir(root)
    data.mkdir(parents=True, exist_ok=True)
    write_compose_files(
        root, container_name=container_name, image=image, loopback_only=loopback_only,
        app_port=app_port, mcp_port=mcp_port,
    )
    data_env = paths.data_env_path(root)
    data_env.write_text(render_data_env(env))
    try:
        data_env.chmod(0o600)  # holds SECRET_KEY and ADMIN_PASSWORD in plaintext
    except OSError:
        pass


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
                  timeout: float = 60.0) -> "subprocess.CompletedProcess[str]":
    return _run_compose(runtime, root, project, ["down"], run=run, timeout=timeout)


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
    docker-compose.single.yml."""
    path = paths.compose_path(root)
    match = _IMAGE_LINE_RE.search(path.read_text())
    if not match:
        raise ComposeError(f"No 'image:' line found in {path}.")
    return match.group(2)


def write_image(root: Path, image: str) -> None:
    """Rewrite just the `image:` line in place -- everything else in
    docker-compose.single.yml (container_name, ports, healthcheck) is left
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
