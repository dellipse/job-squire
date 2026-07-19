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
"""Full CLI uninstall: `job-squire uninstall`.

Added after the rest of the CLI landed, because getting job-squire *off* a machine cleanly matters
as much as getting it on. Three independent things this undoes, each with
its own opt-out so nothing is destroyed silently:

  1. Every registered instance, via `ops/lifecycle.py`'s `remove_instance`
     (one call per instance) -- same keep-or-delete-data prompt and same
     safe "keep by default" fallback as `job-squire remove` uses for a
     single instance, so uninstalling everything never silently destroys a
     job search's history any more than removing one instance would. That
     same keep-or-delete decision now also governs each instance's named
     Docker volume (where its database/uploads actually live), not just
     its host data directory. Each call also takes the same `remove_image`
     flag `remove_instance` does --
     `compose down` alone never removes the container image it was
     running. `uninstall_everything` itself defaults this to False (an
     opt-in library default, matching `remove_instance`'s own); it's the
     `job-squire uninstall` *command* (ops/commands.py) that flips the
     operator-facing default to "remove," on the reasoning that an
     uninstall is normally a full teardown and `remove`'s per-instance
     caution doesn't carry over. The command resolves its own default
     before calling this function, prompting "Keep the image(s)?"
     (defaulting that prompt to No) when neither `--remove-image` nor
     `--keep-image` was given.
  2. The container runtime (Podman/OrbStack/Docker Desktop) -- but *only*
     if `ops/runtime.py` recorded that job-squire itself installed it
     (`runtime.json`'s `source == "installed"`), and only when the operator
     opts in with `--remove-runtime`. A runtime job-squire found already
     working (`source == "detected"`) is never touched, mirroring
     `ensure_runtime`'s "never install over one that already works" rule
     in reverse: never uninstall one job-squire didn't put there.
  3. The CLI's own venv and the PATH entry `bootstrap.sh`/`bootstrap.ps1`
     added. This module writes no install manifest and needs none: the
     venv location is derived from `sys.prefix` of the *running*
     interpreter (the running process is its own proof of where it lives),
     gated by `looks_like_bootstrap_venv` so a directory is only ever
     proposed for deletion when it actually matches the layout
     bootstrap.sh/.ps1 create (`<home>/.job-squire/cli` or
     `%LOCALAPPDATA%\\job-squire\\cli`, with a real `pyvenv.cfg` inside) --
     never a system Python or a `pip install -e` developer checkout.
"""
from __future__ import annotations

import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .lifecycle import LifecycleError, remove_instance
from .registry import list_instances
from .runtime import (
    InstallPlan,
    InstallStep,
    RUNTIME_STATE_FILENAME,
    load_runtime_choice,
    read_os_release,
    run_install_plan,
)
from ..query.config import config_dir

Runner = Callable[..., "subprocess.CompletedProcess[str]"]
Confirm = Callable[[str], bool]

PATH_MARKER = "added by job-squire bootstrap"
VENV_DIRNAME = "cli"
INSTALL_DIRNAMES = ("job-squire", ".job-squire")


class UninstallError(RuntimeError):
    """Raised for an uninstall failure that isn't a LifecycleError."""


# ── Runtime removal plans (the reverse of ops/runtime.py's install plans) ──


def _linux_runtime_uninstall_plan(runtime: str, os_release: dict[str, str] | None = None) -> InstallPlan:
    if runtime != "podman":
        raise UninstallError(f"No packaged uninstall path is known for runtime {runtime!r} on Linux.")
    os_release = os_release if os_release is not None else read_os_release()
    os_id = os_release.get("ID", "").lower()
    id_like = os_release.get("ID_LIKE", "").lower()

    if os_id in ("fedora", "rhel", "rocky", "almalinux", "centos") or "rhel" in id_like or "fedora" in id_like:
        steps = (InstallStep("Remove Podman via dnf", ("dnf", "remove", "-y", "podman"), use_sudo=True),)
    elif os_id in ("debian", "ubuntu") or "debian" in id_like:
        steps = (InstallStep("Remove Podman via apt-get", ("apt-get", "remove", "-y", "podman"), use_sudo=True),)
    elif os_id == "arch" or "arch" in id_like:
        steps = (InstallStep("Remove Podman via pacman", ("pacman", "-R", "--noconfirm", "podman"), use_sudo=True),)
    else:
        raise UninstallError(
            f"No packaged Podman uninstall path is known for this Linux distribution "
            f"(ID={os_id or 'unknown'}). Remove it manually if you no longer want it: "
            f"https://podman.io/docs/installation"
        )
    return InstallPlan(runtime=runtime, summary="Remove Podman (installed earlier by job-squire).", steps=steps)


def _macos_runtime_uninstall_plan(runtime: str) -> InstallPlan:
    if runtime == "orbstack":
        return InstallPlan(
            runtime=runtime,
            summary="Remove OrbStack (installed earlier by job-squire).",
            steps=(InstallStep("Uninstall OrbStack via Homebrew", ("brew", "uninstall", "--cask", "orbstack")),),
        )
    if runtime == "podman":
        return InstallPlan(
            runtime=runtime,
            summary="Remove the Podman machine and package (installed earlier by job-squire).",
            steps=(
                InstallStep("Stop the Podman machine VM", ("podman", "machine", "stop")),
                InstallStep("Remove the Podman machine VM", ("podman", "machine", "rm", "-f")),
                InstallStep("Uninstall Podman via Homebrew", ("brew", "uninstall", "podman")),
            ),
        )
    raise UninstallError(f"No packaged uninstall path is known for runtime {runtime!r} on macOS.")


def _windows_runtime_uninstall_plan(runtime: str) -> InstallPlan:
    if runtime == "docker":
        return InstallPlan(
            runtime=runtime,
            summary="Remove Docker Desktop (installed earlier by job-squire).",
            steps=(
                InstallStep(
                    "Uninstall Docker Desktop via winget",
                    ("winget", "uninstall", "-e", "--id", "Docker.DockerDesktop"),
                ),
            ),
        )
    if runtime == "podman":
        return InstallPlan(
            runtime=runtime,
            summary="Remove the Podman WSL machine and package (installed earlier by job-squire).",
            steps=(
                InstallStep("Stop the Podman WSL machine", ("podman", "machine", "stop")),
                InstallStep("Remove the Podman WSL machine", ("podman", "machine", "rm", "-f")),
                InstallStep("Uninstall Podman via winget", ("winget", "uninstall", "-e", "--id", "RedHat.Podman")),
            ),
        )
    raise UninstallError(f"No packaged uninstall path is known for runtime {runtime!r} on Windows.")


def runtime_uninstall_plan(
    runtime: str, *, system: str | None = None, os_release: dict[str, str] | None = None,
) -> InstallPlan:
    """The reverse of `ops/runtime.py`'s per-OS install plans. Raises
    UninstallError for a runtime/OS combination with no known packaged
    uninstall path (mirroring `linux_install_plan`'s own unknown-distro
    error) rather than guessing at a command."""
    system = system or platform.system()
    if system == "Linux":
        return _linux_runtime_uninstall_plan(runtime, os_release)
    if system == "Darwin":
        return _macos_runtime_uninstall_plan(runtime)
    if system == "Windows":
        return _windows_runtime_uninstall_plan(runtime)
    raise UninstallError(f"Unsupported platform: {system}")


# ── The CLI's own venv + PATH entry ─────────────────────────────────────


def looks_like_bootstrap_venv(venv_dir: Path) -> bool:
    """True only for a directory bootstrap.sh/.ps1 would plausibly have
    created: `<install_dir>/cli`, where `install_dir` is named `job-squire`
    or `.job-squire` and the directory is an actual venv (has
    `pyvenv.cfg`). Deliberately conservative: this gates deleting an entire
    directory tree, so a system Python or a developer's `pip install -e`
    checkout must never match.
    """
    return (
        venv_dir.name == VENV_DIRNAME
        and venv_dir.parent.name in INSTALL_DIRNAMES
        and (venv_dir / "pyvenv.cfg").is_file()
    )


def _candidate_rc_files(home: Path | None = None) -> list[Path]:
    home = home if home is not None else Path.home()
    return [home / ".zshrc", home / ".bashrc", home / ".profile"]


_PATH_LINE_DIR_RE = re.compile(r'PATH="([^:"]+):')


def strip_path_line(rc_file: Path, bin_dir: Path) -> bool:
    """Remove job-squire's PATH line -- and only that line -- from
    `rc_file`. Matches by *both* the bootstrap marker comment and
    `bin_dir`, so an unrelated PATH line an operator added by hand (even
    one naming the same directory, or carrying the same marker text for
    some other tool) is never touched. Returns True if the file changed.

    The literal-string comparison is tried first; if it doesn't match, the
    directory named in the line is parsed out and compared to `bin_dir`
    after resolving both through `Path.resolve()`. This exists because
    `bin_dir` here is always derived from `sys.executable` (the *running*
    interpreter's own path), and on some platforms/Python builds that can
    come back through a symlink's real target rather than the literal
    `$HOME/.job-squire/cli/bin` string bootstrap.sh wrote -- a mismatch
    that silently no-oped this function while `uninstall` still reported
    the CLI itself as removed, leaving a stale PATH line with no error.
    """
    if not rc_file.is_file():
        return False
    lines = rc_file.read_text().splitlines(keepends=True)

    def _matches(ln: str) -> bool:
        if PATH_MARKER not in ln:
            return False
        if str(bin_dir) in ln:
            return True
        match = _PATH_LINE_DIR_RE.search(ln)
        if not match:
            return False
        try:
            return Path(match.group(1)).resolve() == bin_dir.resolve()
        except OSError:
            return False

    kept = [ln for ln in lines if not _matches(ln)]
    if len(kept) == len(lines):
        return False
    rc_file.write_text("".join(kept))
    return True


def _spawn_windows_deferred_delete(install_dir: Path, run: Runner) -> None:
    """Windows won't let a running process delete its own directory
    synchronously (no FILE_SHARE_DELETE on the running interpreter), so a
    detached shell finishes the job after this process exits: wait a
    couple of seconds, then remove the tree. Best-effort -- if spawning
    fails, the caller still reports the CLI as "removed" from the
    registry's perspective, but the directory may need a manual delete.
    """
    run(
        ["cmd", "/c", f'timeout /t 2 /nobreak >nul & rmdir /s /q "{install_dir}"'],
        creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )


def _remove_own_install(
    venv_dir: Path, *, system: str, rmtree: Callable[[Path], None], run: Runner,
) -> Path:
    install_dir = venv_dir.parent
    if system == "Windows":
        _spawn_windows_deferred_delete(install_dir, run)
    else:
        rmtree(install_dir)
    return install_dir


# ── Orchestration ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class UninstallResult:
    instances_removed: list[str]
    data_kept: dict[str, bool]
    runtime_removed: str | None
    cli_removed: Path | None
    rc_files_updated: list[Path]
    image_removed: dict[str, bool] = field(default_factory=dict)
    image_kept_reason: dict[str, str | None] = field(default_factory=dict)
    volumes_removed: dict[str, list[str]] = field(default_factory=dict)


def _default_rmtree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def uninstall_everything(
    *,
    keep_data: bool | None = None,
    confirm_delete_data: Confirm | None = None,
    remove_runtime: bool = False,
    confirm_runtime: Confirm | None = None,
    remove_image: bool = False,
    run: Runner = subprocess.run,
    system: str | None = None,
    venv_dir: Path | None = None,
    rmtree: Callable[[Path], None] = _default_rmtree,
) -> UninstallResult:
    """Tear down every registered instance, then (opt-in) the runtime job-
    squire installed, then the CLI itself.

    `venv_dir` defaults to `Path(sys.prefix)` -- the running interpreter's
    own venv -- and is only overridable for tests; there is deliberately no
    way for an operator to point this at an arbitrary directory from the
    command line, since a wrong answer there would delete the wrong thing.

    `remove_image` (opt-in, default False) is forwarded to each instance's
    own `remove_instance` call unchanged, in registry order. Because each
    call re-checks "is any *still-registered* instance using this image"
    (ops/lifecycle.py's `_image_still_in_use`) against whatever's left in
    the registry at that moment, instances sharing the default `:latest`
    tag work out correctly without any special-casing here: the image is
    kept while a sibling still needs it, and only the last instance
    referencing it actually triggers `rmi`.
    """
    system = system or platform.system()

    instances_removed: list[str] = []
    data_kept: dict[str, bool] = {}
    image_removed: dict[str, bool] = {}
    image_kept_reason: dict[str, str | None] = {}
    volumes_removed: dict[str, list[str]] = {}
    for instance in list_instances():
        try:
            result = remove_instance(
                instance.name, run=run, keep_data=keep_data, confirm_delete=confirm_delete_data,
                remove_image=remove_image,
            )
        except LifecycleError:
            raise
        instances_removed.append(result.name)
        data_kept[result.name] = result.data_kept
        image_removed[result.name] = result.image_removed
        image_kept_reason[result.name] = result.image_kept_reason
        volumes_removed[result.name] = result.volumes_removed

    runtime_removed: str | None = None
    if remove_runtime:
        choice = load_runtime_choice()
        if choice and choice.get("source") == "installed" and choice.get("runtime"):
            rt_name = choice["runtime"]
            proceed = True if confirm_runtime is None else confirm_runtime(
                f"Remove {rt_name} (installed earlier by job-squire)? Anything else on this machine "
                f"using {rt_name} will stop working."
            )
            if proceed:
                plan = runtime_uninstall_plan(rt_name, system=system)
                run_install_plan(plan, run=run)
                runtime_removed = rt_name

    cli_removed: Path | None = None
    rc_files_updated: list[Path] = []
    venv_dir = venv_dir if venv_dir is not None else Path(sys.prefix)
    if looks_like_bootstrap_venv(venv_dir):
        bin_dir = Path(sys.executable).parent
        for rc_file in _candidate_rc_files():
            if strip_path_line(rc_file, bin_dir):
                rc_files_updated.append(rc_file)
        cli_removed = _remove_own_install(venv_dir, system=system, rmtree=rmtree, run=run)

    # The CLI's own config directory (instance registry, MCP query config,
    # recorded runtime choice) is metadata about a CLI that, by this point,
    # is either fully uninstalled or -- if looks_like_bootstrap_venv
    # declined to touch it -- about to be uninstalled by hand per the
    # fallback message the `uninstall` command prints. Either way nothing
    # in it is "data" in the job-search sense (that's each instance's own
    # data_dir, already handled above via keep_data), so it's always
    # cleared rather than left behind as orphaned state.
    cfg_dir = config_dir()
    if cfg_dir.exists():
        rmtree(cfg_dir)

    return UninstallResult(
        instances_removed=instances_removed,
        data_kept=data_kept,
        runtime_removed=runtime_removed,
        cli_removed=cli_removed,
        rc_files_updated=rc_files_updated,
        image_removed=image_removed,
        image_kept_reason=image_kept_reason,
        volumes_removed=volumes_removed,
    )


__all__ = [
    "UninstallError",
    "UninstallResult",
    "runtime_uninstall_plan",
    "looks_like_bootstrap_venv",
    "strip_path_line",
    "uninstall_everything",
    "RUNTIME_STATE_FILENAME",
]
