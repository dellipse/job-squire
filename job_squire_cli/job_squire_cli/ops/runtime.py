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
"""Container runtime detection and per-OS install with consent.

Two rules drive everything here:

  1. Detect first. If docker, podman, orbstack, or colima is already on
     PATH *and actually runs*, use it and install nothing -- never install
     a runtime over one that already works.
  2. Only when nothing works does the CLI propose installing the per-OS
     default, and only with the operator's explicit consent. Podman is the
     default on every platform (including macOS), so setup never asks
     about company size or steers anyone toward a paid product. OrbStack
     (macOS) and Docker Desktop (Windows) are opt-in fallbacks whose
     commercial-use thresholds are shown at that point of choice, not
     before.

This module only decides *which* runtime and drives the install; it does
not yet write a per-instance registry entry -- that's C4/C5. Until an
instance exists, `record_runtime_choice`/`load_runtime_choice` persist the
selection in a small machine-wide cache file at the same per-user config
directory the registry will use, so a `create` invocation later in the
same setup session doesn't re-detect from scratch. C4 formalizes this into
the `runtime` field of each instance's registry entry.
"""
from __future__ import annotations

import json
import platform
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

import click

# query.config has zero dependencies beyond the stdlib (json/os/platform/
# pathlib) -- unlike query.commands/mcp_client, importing it does not pull
# in `rich` or `mcp`, so this is safe from the ops (core, click-only) side
# without defeating the query group's lazy-loading (see cli.py's
# _LazyGroup and test_cli_grammar.py's subprocess proof).
from ..query.config import config_dir

RUNTIME_STATE_FILENAME = "runtime.json"

# Licensing thresholds verified 2026-07-11. Podman carries no threshold and is the default everywhere,
# so these are only ever shown at the point OrbStack or Docker Desktop is
# actually offered as an opt-in/fallback, never proactively.
ORBSTACK_LICENSE_NOTICE = (
    "OrbStack is free for personal use. Commercial use over $10k/year in "
    "revenue requires a paid license (https://orbstack.dev/pricing)."
)
DOCKER_DESKTOP_LICENSE_NOTICE = (
    "Docker Desktop is free for personal use, education, small businesses "
    "(under 250 employees AND under $10M/year revenue), and non-commercial "
    "open source projects. Larger companies need a paid subscription "
    "(https://www.docker.com/pricing/faq/)."
)

Runner = Callable[..., "subprocess.CompletedProcess[str]"]
Which = Callable[[str], "str | None"]


class RuntimeSelectionError(RuntimeError):
    """Raised when no working runtime could be detected or installed."""


# ── Detection ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RuntimeCandidate:
    name: str
    binaries: tuple[str, ...]
    check_args: tuple[str, ...]


# Checked in this order -- the first candidate with a binary on PATH whose
# check command actually succeeds wins. This order matches the plan's own
# listing ("look for docker, podman, orbstack, and colima").
CANDIDATES: tuple[RuntimeCandidate, ...] = (
    RuntimeCandidate("docker", ("docker",), ("info",)),
    RuntimeCandidate("podman", ("podman",), ("info",)),
    RuntimeCandidate("orbstack", ("orbctl", "orb"), ("status",)),
    RuntimeCandidate("colima", ("colima",), ("status",)),
)


def _runs_ok(binary: str, args: Sequence[str], run: Runner, timeout: float = 10.0) -> bool:
    try:
        result = run([binary, *args], capture_output=True, timeout=timeout, text=True)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def detect_working_runtime(run: Runner = subprocess.run, which: Which = shutil.which) -> str | None:
    """The name of the first runtime that is on PATH and actually runs.

    None if nothing works, in which case the caller may go on to propose
    installing the per-OS default. Never installs or modifies anything.
    """
    for candidate in CANDIDATES:
        for binary in candidate.binaries:
            if which(binary) is None:
                continue
            if _runs_ok(binary, candidate.check_args, run):
                return candidate.name
    return None


# ── Install plans ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class InstallStep:
    description: str
    command: tuple[str, ...]
    use_sudo: bool = False


@dataclass(frozen=True)
class InstallPlan:
    runtime: str
    summary: str
    steps: tuple[InstallStep, ...]
    license_notice: str | None = None
    post_install_note: str | None = None


def read_os_release(path: Path = Path("/etc/os-release")) -> dict[str, str]:
    """Minimal /etc/os-release parser: enough for ID and ID_LIKE."""
    data: dict[str, str] = {}
    try:
        text = path.read_text()
    except OSError:
        return data
    for line in text.splitlines():
        if "=" not in line or line.strip().startswith("#"):
            continue
        key, _, value = line.partition("=")
        data[key.strip()] = value.strip().strip('"').strip("'")
    return data


def linux_install_plan(os_release: dict[str, str] | None = None) -> InstallPlan:
    """Podman rootless is the only Linux default.

    Docker Engine is used only if already present -- which detection would
    already have found -- and is never auto-installed here. Never Docker
    Desktop on a server.
    """
    if os_release is None:
        os_release = read_os_release()
    os_id = os_release.get("ID", "").lower()
    id_like = os_release.get("ID_LIKE", "").lower()

    if os_id in ("fedora", "rhel", "rocky", "almalinux", "centos") or "rhel" in id_like or "fedora" in id_like:
        steps = (InstallStep("Install Podman via dnf", ("dnf", "install", "-y", "podman"), use_sudo=True),)
    elif os_id in ("debian", "ubuntu") or "debian" in id_like:
        steps = (
            InstallStep("Install Podman via apt-get", ("apt-get", "install", "-y", "podman"), use_sudo=True),
        )
    elif os_id == "arch" or "arch" in id_like:
        steps = (
            InstallStep("Install Podman via pacman", ("pacman", "-S", "--noconfirm", "podman"), use_sudo=True),
        )
    else:
        raise RuntimeSelectionError(
            f"No packaged Podman install path is known for this Linux distribution "
            f"(ID={os_id or 'unknown'}). Install Podman manually: "
            f"https://podman.io/docs/installation -- then re-run job-squire."
        )
    return InstallPlan(
        runtime="podman",
        summary="Podman (rootless) is the default container runtime on Linux.",
        steps=steps,
    )


def macos_install_plan(use_orbstack: bool = False) -> InstallPlan:
    """Podman machine, CLI-automated, is the macOS default.

    OrbStack is an explicit opt-in (`use_orbstack=True`), never the
    default, with its commercial-use threshold surfaced at this point of
    choice via `license_notice`.
    """
    if use_orbstack:
        return InstallPlan(
            runtime="orbstack",
            summary="OrbStack -- opt-in alternative to Podman on macOS.",
            steps=(
                InstallStep("Install OrbStack via Homebrew", ("brew", "install", "--cask", "orbstack")),
                InstallStep("Launch OrbStack to start its Docker engine", ("open", "-a", "OrbStack")),
            ),
            license_notice=ORBSTACK_LICENSE_NOTICE,
            post_install_note="Waiting for the OrbStack Docker engine to come up ...",
        )
    return InstallPlan(
        runtime="podman",
        summary="Podman machine is the default container runtime on macOS.",
        steps=(
            InstallStep("Install Podman via Homebrew", ("brew", "install", "podman")),
            InstallStep("Initialize the Podman machine VM", ("podman", "machine", "init")),
            InstallStep("Start the Podman machine VM", ("podman", "machine", "start")),
        ),
    )


def wsl2_status(run: Runner = subprocess.run) -> bool:
    """True if `wsl --status` succeeds (WSL2 present and reporting healthy)."""
    try:
        result = run(["wsl", "--status"], capture_output=True, timeout=10, text=True)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def check_wsl2(run: Runner = subprocess.run, which: Which = shutil.which) -> tuple[bool, str]:
    """(has_wsl2, guidance). guidance is "" when has_wsl2 is True.

    Both Podman and Docker Desktop run their Linux containers inside WSL2
    on Windows, so this is a shared prerequisite checked before either
    per-OS install plan runs.
    """
    if which("wsl") is None:
        return False, (
            "WSL2 was not found on this machine. Enable it first:\n"
            "  1. Run: wsl --install\n"
            "  2. Reboot.\n"
            "  3. Re-run job-squire to continue installing the container runtime."
        )
    if not wsl2_status(run):
        return False, (
            "WSL2 is present but 'wsl --status' did not report healthy. Run "
            "'wsl --install' plus a reboot if no distro is registered yet, then "
            "re-run job-squire."
        )
    return True, ""


def windows_install_plan(use_docker_desktop: bool = False) -> InstallPlan:
    """Podman on WSL2, CLI-automated, is the Windows default.

    Docker Desktop is the graceful fallback (`use_docker_desktop=True`),
    never the default, with its commercial-use threshold surfaced at this
    point of choice via `license_notice`.
    """
    if use_docker_desktop:
        return InstallPlan(
            runtime="docker",
            summary="Docker Desktop -- graceful fallback on Windows.",
            steps=(
                InstallStep(
                    "Install Docker Desktop via winget",
                    ("winget", "install", "-e", "--id", "Docker.DockerDesktop"),
                ),
            ),
            license_notice=DOCKER_DESKTOP_LICENSE_NOTICE,
            post_install_note=(
                "Launch Docker Desktop from the Start menu and wait for it to report "
                "'Engine running', then re-run job-squire."
            ),
        )
    return InstallPlan(
        runtime="podman",
        summary="Podman on WSL2 is the default container runtime on Windows.",
        steps=(
            InstallStep("Install Podman via winget", ("winget", "install", "-e", "--id", "RedHat.Podman")),
            InstallStep("Initialize the Podman WSL machine", ("podman", "machine", "init")),
            InstallStep("Start the Podman WSL machine", ("podman", "machine", "start")),
        ),
    )


def run_install_plan(plan: InstallPlan, run: Runner = subprocess.run) -> None:
    for step in plan.steps:
        command = [*(("sudo",) if step.use_sudo else ()), *step.command]
        click.echo(f"-> {step.description}: {' '.join(command)}")
        result = run(command, timeout=600)
        if getattr(result, "returncode", 0) != 0:
            raise RuntimeSelectionError(f"Step failed: {step.description}")
    if plan.post_install_note:
        click.echo(plan.post_install_note)


# ── Recording the choice ──────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def record_runtime_choice(runtime: str, *, source: str) -> Path:
    """Persist the chosen runtime so a later command doesn't re-detect it.

    Interim, machine-wide cache -- C4 formalizes this into the `runtime`
    field of each instance's registry entry. `source` is "detected" or
    "installed", for the human reading the file, never anything secret.
    """
    path = config_dir() / RUNTIME_STATE_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"runtime": runtime, "source": source, "recorded_at": _now_iso()}
    path.write_text(json.dumps(payload, indent=2) + "\n")
    return path


def load_runtime_choice() -> dict | None:
    path = config_dir() / RUNTIME_STATE_FILENAME
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


# ── Orchestration ─────────────────────────────────────────────────────────


def ensure_runtime(
    *,
    system: str | None = None,
    confirm: Callable[[str], bool] = click.confirm,
    prefer_orbstack: bool = False,
    prefer_docker_desktop: bool = False,
    run: Runner = subprocess.run,
    which: Which = shutil.which,
) -> str:
    """Detect a working container runtime, or install the per-OS default
    with consent. Never installs over a runtime that already works.

    `prefer_orbstack` / `prefer_docker_desktop` are the operator's explicit
    opt-in into the non-default macOS/Windows fallback; both are ignored
    (Podman stays the plan) unless the operator asked for them.

    Returns the runtime's canonical name and records the choice (see
    `record_runtime_choice`) for later commands / the future C4 registry.
    """
    existing = detect_working_runtime(run=run, which=which)
    if existing:
        record_runtime_choice(existing, source="detected")
        return existing

    system = system or platform.system()
    if system == "Linux":
        plan = linux_install_plan()
    elif system == "Darwin":
        if prefer_orbstack:
            click.echo(ORBSTACK_LICENSE_NOTICE)
        plan = macos_install_plan(use_orbstack=prefer_orbstack)
    elif system == "Windows":
        has_wsl2, guidance = check_wsl2(run=run, which=which)
        if not has_wsl2:
            raise RuntimeSelectionError(guidance)
        if prefer_docker_desktop:
            click.echo(DOCKER_DESKTOP_LICENSE_NOTICE)
        plan = windows_install_plan(use_docker_desktop=prefer_docker_desktop)
    else:
        raise RuntimeSelectionError(f"Unsupported platform: {system}")

    click.echo(plan.summary)
    if not confirm(f"Install {plan.runtime} now?"):
        raise RuntimeSelectionError(
            "No container runtime is available and installation was declined."
        )

    run_install_plan(plan, run=run)

    installed = detect_working_runtime(run=run, which=which)
    if not installed:
        raise RuntimeSelectionError(
            f"{plan.runtime} was installed but is not reporting healthy yet. It may "
            f"need a moment to finish starting, or a fresh terminal/login session -- "
            f"re-run job-squire once it's up."
        )

    record_runtime_choice(installed, source="installed")
    return installed
