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
"""Instance lifecycle core: create, start, stop, restart, status, list,
remove (Prompt C5, docs/PLAN-deployment-modes.md Section 7).

This module is the one place that wires together every earlier prompt --
ops.runtime (C3) for the container runtime, ops.registry (C4) for
instance metadata, and this prompt's own ops.paths/ops.ports/ops.compose/
ops.secrets_copy -- into the actual operations `job-squire` exposes.
Every function takes its I/O (subprocess `run`, `confirm`/prompt
callables, `sleep`) as parameters with real defaults, the same injection
pattern ops/runtime.py established, so ops/commands.py's click layer stays
a thin adapter and every operation here is directly unit-testable without
a real container runtime.
"""
from __future__ import annotations

import secrets
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import compose, ports, runtime as runtime_mod, secrets_copy
from .paths import instance_root
from .registry import (
    Drift,
    Instance,
    NameCollisionError,
    ObservedState,
    add_instance,
    check_divergence,
    derive_compose_project,
    derive_cookie_name,
    get_instance,
    list_instances,
    sanitize_slug,
)
from .registry import remove_instance as _registry_remove

Runner = Callable[..., "subprocess.CompletedProcess[str]"]
Which = Callable[[str], "str | None"]
Confirm = Callable[[str], bool]
Sleep = Callable[[float], None]

VALID_MODES = ("local", "network")


class LifecycleError(RuntimeError):
    """Base class for every lifecycle-command failure."""


class InstanceNotFoundError(LifecycleError):
    def __init__(self, name: str):
        super().__init__(f"No instance named {name!r} is registered.")
        self.name = name


class StartupGuardFailure(LifecycleError):
    """The app's own startup safety guard (app/deploy.py) refused to boot
    the instance. `messages` are the exact `FATAL: ...` lines it wrote --
    same reason, same fix, reprinted here instead of a generic container
    error (PLAN Section 7 "Surfacing failures")."""

    def __init__(self, messages: list[str]):
        self.messages = messages
        body = "\n".join(messages) if messages else "(no FATAL lines captured in the container log)"
        super().__init__(f"Instance refused to start:\n{body}")


class NoImportSourceError(LifecycleError):
    pass


def generate_secret_key() -> str:
    """Matches examples/.env.example's own generation command
    (`python -c "import secrets; print(secrets.token_hex(32))"`)."""
    return secrets.token_hex(32)


def generate_password(length: int = 20) -> str:
    return secrets.token_urlsafe(length)


# ── Waiting for the container to report healthy ─────────────────────────


def wait_for_state(
    runtime: str, container_name: str, *, run: Runner = subprocess.run,
    sleep: Sleep = time.sleep, attempts: int = 20, interval: float = 3.0,
) -> dict | None:
    """Poll `docker/podman inspect` until the container is healthy (or has
    no healthcheck and is simply running), has exited, or `attempts` is
    exhausted. Returns the last observed `.State` dict, or None if the
    container was never observed at all.
    """
    state = None
    for attempt in range(attempts):
        state = compose.inspect_state(runtime, container_name, run=run)
        if state is not None:
            health = (state.get("Health") or {}).get("Status")
            status = state.get("Status")
            if health == "healthy" or (health is None and status == "running"):
                return state
            if status == "exited":
                return state
        if attempt < attempts - 1:
            sleep(interval)
    return state


def _guard_failure_from_logs(runtime: str, container_name: str, *, run: Runner) -> StartupGuardFailure | None:
    logs = compose.container_logs(runtime, container_name, run=run)
    fatal = compose.extract_fatal_lines(logs)
    return StartupGuardFailure(fatal) if fatal else None


def _raise_for_failed_state(runtime: str, container_name: str, state: dict | None, *, run: Runner) -> None:
    """Raise StartupGuardFailure if the container exited because of the
    app's startup guard, else a generic LifecycleError with whatever the
    runtime reported. Called after a compose command reports failure, or
    after wait_for_state observes an exited container."""
    guard_failure = _guard_failure_from_logs(runtime, container_name, run=run)
    if guard_failure is not None:
        raise guard_failure
    if state is not None:
        raise LifecycleError(
            f"Container {container_name!r} exited (status={state.get('Status')!r}, "
            f"exit code {state.get('ExitCode')!r}). Check `job-squire status {container_name}` "
            f"or run the runtime's own logs command directly."
        )
    raise LifecycleError(f"Container {container_name!r} did not come up. Check the runtime's own logs directly.")


# ── create ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CreateResult:
    instance: Instance
    admin_username: str
    admin_password: str
    admin_password_generated: bool
    health: dict | None
    import_summary: secrets_copy.ImportSummary | None = None


def create_instance(
    *,
    name: str,
    mode: str,
    hostname: str | None = None,
    mcp_hostname: str | None = None,
    data_root: Path | None = None,
    image: str = compose.DEFAULT_IMAGE,
    admin_username: str = "admin",
    admin_password: str | None = None,
    user_password: str = "",
    import_from: str | None = None,
    copy_keys: bool = False,
    run: Runner = subprocess.run,
    which: Which = shutil.which,
    sleep: Sleep = time.sleep,
    confirm: Confirm = lambda _msg: True,
    prefer_orbstack: bool = False,
    prefer_docker_desktop: bool = False,
) -> CreateResult:
    """Run setup end to end: pick/install a runtime, allocate a port pair
    or record a hostname, generate a fresh SECRET_KEY, write the compose
    and env files, register the instance, bring it up, and -- if
    `import_from` names another registered instance -- copy its basic
    settings in afterward (PLAN Section 7 "Instance lifecycle operations",
    Section 4 "Setup and the import prompt").
    """
    if mode not in VALID_MODES:
        raise LifecycleError(f"mode must be one of {VALID_MODES}, got {mode!r}.")
    if mode == "network" and not hostname:
        raise LifecycleError("Network mode requires a hostname.")

    # Sanitize and collision-check *before* touching the runtime, ports, or
    # disk: add_instance() below would also catch a collision, but only
    # after everything else here had already run (and, worse, after
    # write_instance_files() had already overwritten whatever the existing
    # instance of that name had on disk). Failing fast here means a
    # colliding name never prompts for a runtime install or writes a byte.
    slug = sanitize_slug(name)
    if get_instance(slug) is not None:
        raise NameCollisionError(f"An instance named {slug!r} is already registered.")

    existing = list_instances()
    source: Instance | None = None
    if import_from is not None:
        source = get_instance(import_from)
        if source is None:
            raise NoImportSourceError(f"No instance named {import_from!r} is registered to import from.")

    chosen_runtime = runtime_mod.ensure_runtime(
        confirm=confirm, prefer_orbstack=prefer_orbstack,
        prefer_docker_desktop=prefer_docker_desktop, run=run, which=which,
    )

    app_port, mcp_port = ports.allocate_port_pair(existing)

    secret_key = generate_secret_key()
    generated_password = admin_password is None
    resolved_admin_password = admin_password or generate_password()

    root = instance_root(slug, data_root)
    cookie_name = derive_cookie_name(slug)
    container_name = derive_compose_project(slug)

    if mode == "local":
        public_url = f"http://localhost:{app_port}"
        public_mcp_url = f"http://localhost:{mcp_port}"
        public_mcp_host = "localhost"
    else:
        public_url = f"https://{hostname}"
        resolved_mcp_host = mcp_hostname or f"mcp-{hostname}"
        public_mcp_url = f"https://{resolved_mcp_host}"
        public_mcp_host = resolved_mcp_host

    extra_env: dict[str, str] = {}
    if source is not None:
        extra_env = secrets_copy.read_schedule_env(_instance_root(source, data_root))

    env = compose.InstanceEnv(
        secret_key=secret_key,
        admin_username=admin_username,
        admin_password=resolved_admin_password,
        user_password=user_password,
        instance_name=slug,
        cookie_name=cookie_name,
        deploy_mode=mode,
        public_url=public_url,
        public_mcp_url=public_mcp_url,
        public_mcp_host=public_mcp_host,
        mcp_port=mcp_port,
        extra=extra_env,
    )

    compose.write_instance_files(
        root, container_name=container_name, image=image,
        loopback_only=(mode == "local"), app_port=app_port, mcp_port=mcp_port, env=env,
    )

    instance = add_instance(
        name=slug, mode=mode, runtime=chosen_runtime, data_dir=str(root),
        public_url=public_url, app_port=app_port, mcp_port=mcp_port, cookie_name=cookie_name,
    )

    up_result = compose.compose_up(chosen_runtime, root, container_name, run=run)
    if up_result.returncode != 0:
        _raise_for_failed_state(chosen_runtime, container_name, None, run=run)

    health = wait_for_state(chosen_runtime, container_name, run=run, sleep=sleep)
    if health is not None and health.get("Status") == "exited":
        _raise_for_failed_state(chosen_runtime, container_name, health, run=run)

    import_summary = None
    if source is not None:
        import_summary = _import_settings(
            source=source, dest=instance, data_root=data_root, copy_keys=copy_keys,
            runtime=chosen_runtime, container_name=container_name, run=run, sleep=sleep,
        )

    return CreateResult(
        instance=instance, admin_username=admin_username, admin_password=resolved_admin_password,
        admin_password_generated=generated_password, health=health, import_summary=import_summary,
    )


def _import_settings(
    *, source: Instance, dest: Instance, data_root: Path | None, copy_keys: bool,
    runtime: str, container_name: str, run: Runner, sleep: Sleep,
) -> secrets_copy.ImportSummary:
    """Stop the freshly-created instance, copy database settings directly
    into its (now schema-initialized) sqlite file, and start it back up.
    Stopping first is what keeps this from racing the app's own writes to
    the same file -- the same WAL-safety concern the plan's backup design
    calls out for touching a live instance's database directly."""
    dest_root = _instance_root(dest, data_root)
    source_root = _instance_root(source, data_root)

    compose.compose_stop(runtime, dest_root, container_name, run=run)
    try:
        source_secret_key = secrets_copy.read_secret_key(source_root) if copy_keys else ""
        dest_secret_key = secrets_copy.read_secret_key(dest_root)
        summary = secrets_copy.copy_db_settings(
            source_root=source_root, dest_root=dest_root,
            source_secret_key=source_secret_key, dest_secret_key=dest_secret_key,
            copy_keys=copy_keys,
        )
    finally:
        compose.compose_start(runtime, dest_root, container_name, run=run)
        wait_for_state(runtime, container_name, run=run, sleep=sleep)
    return summary


# ── start / stop / restart ───────────────────────────────────────────────


def _require_instance(name: str) -> Instance:
    instance = get_instance(name)
    if instance is None:
        raise InstanceNotFoundError(name)
    return instance


def _instance_root(instance: Instance, data_root: Path | None) -> Path:
    """Resolve an *already-registered* instance's own directory.

    An explicit `data_root` still wins (matching every lifecycle
    function's own `data_root` parameter -- mainly how tests redirect
    where instances live). Otherwise this is the registry's own recorded
    `data_dir`, not re-derived from `instance_root(name)`: the registry's
    recorded value is the source of truth for where an instance actually
    lives, rather than assuming it always matches the default layout.
    """
    if data_root is not None:
        return instance_root(instance.name, data_root)
    return Path(instance.data_dir)


def start_instance(name: str, *, data_root: Path | None = None, run: Runner = subprocess.run,
                    sleep: Sleep = time.sleep) -> dict | None:
    instance = _require_instance(name)
    root = _instance_root(instance, data_root)
    container_name = derive_compose_project(instance.name)
    result = compose.compose_start(instance.runtime, root, container_name, run=run)
    if result.returncode != 0:
        _raise_for_failed_state(instance.runtime, container_name, None, run=run)
    state = wait_for_state(instance.runtime, container_name, run=run, sleep=sleep)
    if state is not None and state.get("Status") == "exited":
        _raise_for_failed_state(instance.runtime, container_name, state, run=run)
    return state


def stop_instance(name: str, *, data_root: Path | None = None, run: Runner = subprocess.run) -> None:
    instance = _require_instance(name)
    root = _instance_root(instance, data_root)
    container_name = derive_compose_project(instance.name)
    result = compose.compose_stop(instance.runtime, root, container_name, run=run)
    if result.returncode != 0:
        raise LifecycleError(f"Failed to stop {instance.name!r}: {result.stderr or result.stdout}")


def restart_instance(name: str, *, data_root: Path | None = None, run: Runner = subprocess.run,
                      sleep: Sleep = time.sleep) -> dict | None:
    instance = _require_instance(name)
    root = _instance_root(instance, data_root)
    container_name = derive_compose_project(instance.name)
    result = compose.compose_restart(instance.runtime, root, container_name, run=run)
    if result.returncode != 0:
        _raise_for_failed_state(instance.runtime, container_name, None, run=run)
    state = wait_for_state(instance.runtime, container_name, run=run, sleep=sleep)
    if state is not None and state.get("Status") == "exited":
        _raise_for_failed_state(instance.runtime, container_name, state, run=run)
    return state


# ── update / rollback (Prompt C7) ────────────────────────────────────────


@dataclass(frozen=True)
class UpdateResult:
    instance: Instance
    previous_image: str
    new_image: str
    health: dict | None


def update_instance(
    name: str, *, version: str = "latest", image_repo: str | None = None,
    data_root: Path | None = None, run: Runner = subprocess.run, sleep: Sleep = time.sleep,
) -> UpdateResult:
    """Move `name` to `version` (default `latest`; also accepts a pinned
    tag or a full image ref) and recreate its container.

    Order matters for the "never kills the container mid-write" guarantee
    (PLAN Section 7 "Update"): the new image is pulled *first*, with the
    old container still running and untouched -- if the pull fails,
    nothing about the instance has changed. Only once the pull succeeds is
    the container stopped, which is the same `compose stop` (graceful
    SIGTERM, forwarded through s6 to the app so it checkpoints its SQLite
    WAL before exiting) every other lifecycle command already uses for a
    running instance. The image swap only happens after that stop
    succeeds, and the previous image is recorded before the swap so
    `rollback_instance` can undo it.
    """
    instance = _require_instance(name)
    root = _instance_root(instance, data_root)
    container_name = derive_compose_project(instance.name)

    current_image = compose.read_image(root)
    new_image = compose.resolve_image(version, repo=image_repo)

    pull_result = compose.pull_image(instance.runtime, new_image, run=run)
    if pull_result.returncode != 0:
        raise LifecycleError(
            f"Failed to pull {new_image!r}: {(pull_result.stderr or pull_result.stdout).strip()}"
        )

    stop_result = compose.compose_stop(instance.runtime, root, container_name, run=run)
    if stop_result.returncode != 0:
        raise LifecycleError(
            f"Failed to stop {instance.name!r} before updating -- left running on {current_image!r}: "
            f"{(stop_result.stderr or stop_result.stdout).strip()}"
        )

    compose.set_compose_env_value(root, "PREVIOUS_IMAGE", current_image)
    compose.write_image(root, new_image)

    up_result = compose.compose_up(
        instance.runtime, root, container_name, run=run, extra_args=["--force-recreate"],
    )
    if up_result.returncode != 0:
        _raise_for_failed_state(instance.runtime, container_name, None, run=run)

    health = wait_for_state(instance.runtime, container_name, run=run, sleep=sleep)
    if health is not None and health.get("Status") == "exited":
        _raise_for_failed_state(instance.runtime, container_name, health, run=run)

    return UpdateResult(instance=instance, previous_image=current_image, new_image=new_image, health=health)


def rollback_instance(
    name: str, *, data_root: Path | None = None, run: Runner = subprocess.run, sleep: Sleep = time.sleep,
) -> UpdateResult:
    """Move `name` back to the image it was running before its last
    `update_instance` call, recorded as `PREVIOUS_IMAGE` in the instance's
    compose-level `.env`. Each rollback swaps current and previous again,
    so rolling back twice in a row returns to where it started.
    """
    instance = _require_instance(name)
    root = _instance_root(instance, data_root)
    previous = compose.read_compose_env_value(root, "PREVIOUS_IMAGE")
    if not previous:
        raise LifecycleError(
            f"No previous image recorded for {instance.name!r} -- nothing to roll back to yet "
            f"(an instance only gets one after its first `job-squire update`). Use "
            f"`job-squire update {instance.name} --version <tag>` to move to a specific version instead."
        )
    return update_instance(instance.name, version=previous, data_root=data_root, run=run, sleep=sleep)


# ── remove ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RemoveResult:
    name: str
    data_dir: Path
    data_kept: bool
    image: str | None = None
    image_removed: bool = False
    image_kept_reason: str | None = None


def _image_still_in_use(image: str, *, data_root: Path | None) -> bool:
    """True if any *other currently-registered* instance's compose file
    still names `image`. Called only after the instance being removed has
    already been dropped from the registry (see `remove_instance`), so
    `list_instances()` here naturally excludes it -- no self-match to
    guard against.

    Conservative on read failure: an instance whose compose file can't be
    read counts as "still in use" rather than risking `rmi` against an
    image something else might depend on. This is why `remove_image` is
    opt-in rather than the default -- a shared `ghcr.io/dellipse/job-
    squire:latest` tag across instances (the common case: `create` writes
    that same default for every instance unless `--image` overrides it)
    must never be pulled out from under a sibling instance still running
    it.
    """
    for other in list_instances():
        try:
            other_root = _instance_root(other, data_root)
            if compose.read_image(other_root) == image:
                return True
        except (OSError, compose.ComposeError):
            return True
    return False


def remove_instance(
    name: str, *, data_root: Path | None = None, run: Runner = subprocess.run,
    keep_data: bool | None = None, confirm_delete: Confirm | None = None,
    remove_image: bool = False,
) -> RemoveResult:
    """Tear the container down, update the registry, and decide whether to
    keep or delete the data directory. `keep_data` set explicitly skips
    the prompt (for scripted use); left `None`, `confirm_delete` is asked
    -- and if that's *also* not given, the safe default is to keep the
    data, since "removing an instance never silently destroys someone's
    job-search history" (PLAN Section 4) is the one rule that matters more
    here than convenience.

    `remove_image` (default False -- opt-in) additionally runs `rmi`
    against the instance's image once the container is down, but only if
    `_image_still_in_use` finds no other registered instance still
    pointing at it. `compose down` alone never removes the image it was
    running (that's a `docker compose down` default, not a job-squire
    bug we introduced) -- without this flag an instance's image is left
    on disk exactly as it always has been.
    """
    instance = _require_instance(name)
    root = _instance_root(instance, data_root)
    container_name = derive_compose_project(instance.name)

    # root.exists() is the same check `observe()` uses for data_dir_exists --
    # a missing root means there's no compose file and nothing for the
    # runtime to tear down (subprocess.Popen fails outright if `cwd` doesn't
    # exist), so skip straight to clearing the registry entry rather than
    # raising ComposeError for a container that's already gone.
    image: str | None = None
    if root.exists():
        try:
            image = compose.read_image(root)
        except (OSError, compose.ComposeError):
            image = None
        compose.compose_down(instance.runtime, root, container_name, run=run)
    _registry_remove(instance.name)

    if keep_data is None:
        keep_data = True if confirm_delete is None else not confirm_delete(
            f"Delete the data directory for {instance.name!r} at {root}? This permanently deletes "
            f"the database, uploads, and SECRET_KEY -- it cannot be undone."
        )

    if not keep_data:
        shutil.rmtree(root, ignore_errors=True)

    image_removed = False
    image_kept_reason: str | None = None
    if remove_image:
        if image is None:
            image_kept_reason = "couldn't determine the instance's image (no compose file to read)"
        elif _image_still_in_use(image, data_root=data_root):
            image_kept_reason = "still used by another registered instance"
        else:
            rmi_result = compose.remove_image(instance.runtime, image, run=run)
            if rmi_result.returncode == 0:
                image_removed = True
            else:
                image_kept_reason = (rmi_result.stderr or rmi_result.stdout or "rmi failed").strip()

    return RemoveResult(
        name=instance.name, data_dir=root, data_kept=keep_data,
        image=image, image_removed=image_removed, image_kept_reason=image_kept_reason,
    )


# ── status / list ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class InstanceStatus:
    instance: Instance
    observed: ObservedState
    drift: list[Drift] = field(default_factory=list)
    health: str = "unknown"


def observe(instance: Instance, *, run: Runner = subprocess.run) -> ObservedState:
    """What's actually running for `instance`, for check_divergence.

    Port-binding drift isn't checked here: the container is always
    launched from the compose file this CLI generated with the exact
    ports recorded in the registry, so the realistic drift case is a
    renamed or missing container/volume -- exactly what `container_running`
    and `data_dir_exists` below catch -- not a silently different
    published port on a container this same file created.
    """
    container_name = derive_compose_project(instance.name)
    state = compose.inspect_state(instance.runtime, container_name, run=run)
    data_dir_exists = Path(instance.data_dir).exists()
    if state is None:
        return ObservedState(container_running=False, container_name=None, data_dir_exists=data_dir_exists)
    return ObservedState(
        container_running=state.get("Status") == "running",
        container_name=container_name,
        data_dir_exists=data_dir_exists,
    )


def _health_label(state: dict | None) -> str:
    if state is None:
        return "not created"
    health = (state.get("Health") or {}).get("Status")
    if health:
        return health  # "healthy" | "unhealthy" | "starting"
    return state.get("Status") or "unknown"


def status_for(instance: Instance, *, run: Runner = subprocess.run) -> InstanceStatus:
    observed = observe(instance, run=run)
    drift = check_divergence(instance, observed)
    container_name = derive_compose_project(instance.name)
    state = compose.inspect_state(instance.runtime, container_name, run=run)
    return InstanceStatus(instance=instance, observed=observed, drift=drift, health=_health_label(state))


def list_status(*, run: Runner = subprocess.run) -> list[InstanceStatus]:
    return [status_for(instance, run=run) for instance in list_instances()]
