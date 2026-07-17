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
"""Ollama capability detection, guided install, and model recommendation.

Implements docs/PLAN-ollama-assist.md ("PLAN: Ollama Assist -- Capability
Detection, Guided Install, Model Selection", agreed 2026-07-12), CLI side
only (`job-squire ollama check` / `job-squire ollama setup` below, wired in
ops/commands.py). The web app's own onboarding/Settings panel (that plan's
"Integration Points" section) is a separate, larger piece of work and is
not touched here.

**Container Blindness.** Job Squire runs in Docker. What the *container*
can see about the host's RAM/CPU/GPU is frequently wrong: on macOS/Windows
it's the Docker Desktop or Podman machine VM's allocation, not the real
machine's, and Apple Silicon GPUs are never visible to a container at all.
This module runs directly on the host (the CLI already does, for every
other command here) so its detection is authoritative -- see the plan's
"three sources with strict precedence" table. `write_host_capabilities`
writes the result into the instance's `data/` directory specifically
(not the instance root) because `data/` is what's bind-mounted into the
container, so the running app can read this file too.

**This package still depends on nothing but click + cryptography** (see
pyproject.toml and ops/secrets_copy.py's module docstring for why): no
`requests`, no `psutil`. Hardware probes below shell out to platform
tools already present on any Mac/Linux/Windows box (`sysctl`, `/proc`,
`nvidia-smi`, `rocm-smi`, `wmic`) using the same `Runner`/`Which`
injection pattern as ops/runtime.py, and the Ollama API round-trip test
uses `urllib` from the stdlib rather than adding an HTTP client dependency.

**Model tags are data, reviewed periodically.** TIER_TABLE below was last
checked against https://ollama.com/library on 2026-07-16. The plan is
explicit that this table "will age" and must be "re-verified against the
Ollama library at implementation time and reviewed periodically" -- treat
it as a living data structure, not a fixed spec.

**The `num_ctx` gap.** The plan's install flow (step 5) calls for the
provider adapter to set `num_ctx` on the Ollama row so large analysis
prompts aren't silently truncated. `AIProviderConfig` (app/models.py) has
no `num_ctx` column yet -- adding one is an app-side migration + adapter
change, out of scope for this CLI-only pass. `write_provider_config` below
configures everything else the row supports today; the num_ctx gap is
called out again in its docstring and in docs/PLAN-ollama-assist.md so it
isn't lost.
"""
from __future__ import annotations

import json
import platform
import re
import shutil
import sqlite3
import subprocess
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import click

from . import paths
from .runtime import InstallPlan, InstallStep, run_install_plan

Runner = Callable[..., "subprocess.CompletedProcess[str]"]
Which = Callable[[str], "str | None"]

HOST_CAPABILITIES_FILENAME = "host_capabilities.json"
OLLAMA_BINARY = "ollama"
# Where *this CLI process* (running directly on the host, never in a
# container -- see "Container Blindness" above) reaches Ollama for its own
# checks: is_ollama_running(), and the post-setup round-trip test when
# base_url wasn't overridden to point somewhere else. Also what a Modelfile
# derivation, ollama pull, etc. talk to -- all of that is host-side already.
OLLAMA_DEFAULT_HOST = "http://localhost:11434"
# Where the *containerized app* should reach Ollama when it's running
# natively on this same host -- the common case (per docs/PLAN-ollama-
# assist.md, Ollama is a native install, never dockerized itself). Plain
# "localhost" only resolves to the container itself (single-container
# topology, docker-compose.yml), never this host, so that can never
# be a correct default here even though it's OLLAMA_DEFAULT_HOST's value for
# host-side checks above. "host.docker.internal" resolves out of the box on
# Docker Desktop/OrbStack (macOS, Windows); on Linux it requires the
# compose file's `extra_hosts: ["host.docker.internal:host-gateway"]` entry
# (Docker Engine 20.10+), which ops/compose.py's render_compose_yaml() (and
# the repo's own docker-compose.yml, kept in sync by hand) now set
# unconditionally -- so this default is uniform across platforms rather
# than branching on `system`. An operator whose Ollama lives on a different
# machine on the network still overrides this with `--base-url` as before.
OLLAMA_CONTAINER_HOST = "http://host.docker.internal:11434"
PROVIDER_KEY = "ollama"


class OllamaAssistError(RuntimeError):
    """Raised for detection/install/pull/configure/test failures."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Host capability detection ────────────────────────────────────────────


@dataclass(frozen=True)
class HostCapabilities:
    """One detection snapshot. `source` is always "cli-host" here -- the
    plan's other two sources (in-app best-effort, questionnaire fallback)
    are the web app's responsibility, not this module's."""

    detected_at: str
    os: str  # "Darwin" | "Linux" | "Windows" | anything else platform.system() returns
    apple_silicon: bool
    ram_gb: float | None
    cpu_cores: int | None
    gpu_vendor: str | None  # "apple" | "nvidia" | "amd" | None
    gpu_vram_gb: float | None
    ollama_installed: bool
    ollama_running: bool
    source: str = "cli-host"


def _run(cmd: list[str], run: Runner, timeout: float = 10.0) -> subprocess.CompletedProcess | None:
    try:
        return run(cmd, capture_output=True, timeout=timeout, text=True)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _macos_ram_gb(run: Runner) -> float | None:
    result = _run(["sysctl", "-n", "hw.memsize"], run)
    if result is None or result.returncode != 0:
        return None
    try:
        return round(int(result.stdout.strip()) / (1024**3), 1)
    except ValueError:
        return None


def _macos_cpu_cores(run: Runner) -> int | None:
    result = _run(["sysctl", "-n", "hw.ncpu"], run)
    if result is None or result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def _linux_meminfo_gb() -> float | None:
    try:
        text = Path("/proc/meminfo").read_text()
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("MemTotal:"):
            parts = line.split()
            if len(parts) >= 2:
                try:
                    return round(int(parts[1]) / (1024**2), 1)  # kB -> GiB
                except ValueError:
                    return None
    return None


def _linux_cpu_cores() -> int | None:
    try:
        text = Path("/proc/cpuinfo").read_text()
    except OSError:
        return None
    count = sum(1 for line in text.splitlines() if line.startswith("processor"))
    return count or None


def _nvidia_vram_gb(run: Runner, which: Which) -> float | None:
    if which("nvidia-smi") is None:
        return None
    result = _run(["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"], run)
    if result is None or result.returncode != 0:
        return None
    stripped = result.stdout.strip()
    if not stripped:
        return None
    try:
        return round(float(stripped.splitlines()[0].strip()) / 1024, 1)  # MiB -> GiB
    except ValueError:
        return None


def _amd_vram_gb(run: Runner, which: Which) -> float | None:
    if which("rocm-smi") is None:
        return None
    result = _run(["rocm-smi", "--showmeminfo", "vram"], run)
    if result is None or result.returncode != 0:
        return None
    match = re.search(r"(\d+(?:\.\d+)?)\s*(MiB|MB|GiB|GB)", result.stdout, re.IGNORECASE)
    if not match:
        return None
    value = float(match.group(1))
    if match.group(2).upper().startswith("M"):
        value /= 1024
    return round(value, 1)


def _windows_ram_gb(run: Runner) -> float | None:
    result = _run(["wmic", "computersystem", "get", "TotalPhysicalMemory"], run)
    if result is None or result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.isdigit():
            return round(int(line) / (1024**3), 1)
    return None


def _windows_cpu_cores(run: Runner) -> int | None:
    result = _run(["wmic", "cpu", "get", "NumberOfCores"], run)
    if result is None or result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.isdigit():
            return int(line)
    return None


def is_ollama_installed(which: Which = shutil.which) -> bool:
    return which(OLLAMA_BINARY) is not None


def is_ollama_running(host: str = OLLAMA_DEFAULT_HOST, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(f"{host}/api/tags", timeout=timeout) as resp:  # noqa: S310 (fixed local host)
            return resp.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def detect_host_capabilities(
    *, system: str | None = None, run: Runner = subprocess.run, which: Which = shutil.which,
) -> HostCapabilities:
    """CLI host detection -- authoritative per the plan's precedence rule
    (see this module's docstring, "Container Blindness"). Runs on the host,
    never inside the container.
    """
    system = system or platform.system()
    apple_silicon = False
    ram_gb = cpu_cores = None
    gpu_vendor = gpu_vram_gb = None

    if system == "Darwin":
        apple_silicon = platform.machine() == "arm64"
        ram_gb = _macos_ram_gb(run)
        cpu_cores = _macos_cpu_cores(run)
        if apple_silicon:
            # Unified memory *is* the GPU's memory on Apple Silicon -- no
            # separate VRAM figure, so tier classification below uses
            # ram_gb + apple_silicon together instead of gpu_vram_gb.
            gpu_vendor = "apple"
    elif system == "Linux":
        ram_gb = _linux_meminfo_gb()
        cpu_cores = _linux_cpu_cores()
        vram = _nvidia_vram_gb(run, which)
        if vram is not None:
            gpu_vendor, gpu_vram_gb = "nvidia", vram
        else:
            vram = _amd_vram_gb(run, which)
            if vram is not None:
                gpu_vendor, gpu_vram_gb = "amd", vram
    elif system == "Windows":
        ram_gb = _windows_ram_gb(run)
        cpu_cores = _windows_cpu_cores(run)
        vram = _nvidia_vram_gb(run, which)
        if vram is not None:
            gpu_vendor, gpu_vram_gb = "nvidia", vram

    return HostCapabilities(
        detected_at=_now_iso(),
        os=system,
        apple_silicon=apple_silicon,
        ram_gb=ram_gb,
        cpu_cores=cpu_cores,
        gpu_vendor=gpu_vendor,
        gpu_vram_gb=gpu_vram_gb,
        ollama_installed=is_ollama_installed(which),
        ollama_running=is_ollama_running(),
    )


def write_host_capabilities(root: Path, caps: HostCapabilities) -> Path:
    """Write into `data/host_capabilities.json`, not the instance root --
    `data/` is the directory bind-mounted into the container (paths.py),
    so the web app can read this file directly. Carries its own
    `detected_at` timestamp so the app can show when detection last ran."""
    path = paths.data_dir(root) / HOST_CAPABILITIES_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(caps), indent=2) + "\n")
    return path


def read_host_capabilities(root: Path) -> HostCapabilities | None:
    path = paths.data_dir(root) / HOST_CAPABILITIES_FILENAME
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return HostCapabilities(**data)
    except TypeError:
        return None


# ── Capability tiers -> model recommendations ────────────────────────────
# Tags verified against https://ollama.com/library on 2026-07-16. Per the
# plan doc: this table "reflects the mid-2026 landscape and will age" --
# re-check before trusting it more than a few months out. Kept as one data
# structure, not scattered across callers or templates.

TIER_NOT_REASONABLE = "not_reasonable"
TIER_ENTRY = "entry"
TIER_CAPABLE = "capable"
TIER_STRONG = "strong"
TIER_WORKSTATION = "workstation"


@dataclass(frozen=True)
class ModelRecommendation:
    tier: str
    description: str
    triage_model: str
    analysis_model: str
    approx_download_gb: float  # combined, rough, Q4_K_M -- shown before pulling (multi-GB downloads)
    num_ctx: int  # docs/PLAN-ollama-assist.md: "num_ctx (>= 8192, 16384 where RAM allows)"


TIER_TABLE: dict[str, ModelRecommendation] = {
    TIER_ENTRY: ModelRecommendation(
        tier=TIER_ENTRY,
        description=(
            "8 GB RAM, CPU-only. Works, but CPU inference is slow -- expect analysis "
            "responses to take minutes. Consider a free cloud provider for analysis "
            "and keeping only triage local."
        ),
        triage_model="qwen3:4b",
        analysis_model="gemma3:4b",
        approx_download_gb=5.0,
        num_ctx=8192,
    ),
    TIER_CAPABLE: ModelRecommendation(
        tier=TIER_CAPABLE,
        description="16 GB RAM (no GPU), or a GPU with 8 GB+ VRAM. The sweet spot for most self-hosters.",
        triage_model="qwen3:4b",
        analysis_model="qwen3:8b",
        approx_download_gb=8.0,
        num_ctx=8192,
    ),
    TIER_STRONG: ModelRecommendation(
        tier=TIER_STRONG,
        description=(
            "Apple Silicon with 16 GB+ unified memory, a GPU with 12 GB+ VRAM, or 32 GB+ "
            "system RAM. Apple Silicon's unified memory plus Metal makes Macs "
            "disproportionately capable here."
        ),
        triage_model="qwen3:8b",
        analysis_model="gemma4:12b",
        approx_download_gb=13.0,
        num_ctx=16384,
    ),
    TIER_WORKSTATION: ModelRecommendation(
        tier=TIER_WORKSTATION,
        description=(
            "24 GB+ VRAM or 48 GB+ unified memory. Optional -- diminishing returns for "
            "Job Squire's triage/analysis workloads specifically."
        ),
        triage_model="qwen3:8b",
        analysis_model="qwen3.6:27b",
        approx_download_gb=22.0,
        num_ctx=16384,
    ),
}

NOT_REASONABLE_MESSAGE = (
    "Ollama lets you run AI entirely on your own machine, so your data never leaves it. "
    "This machine has {ram} and no GPU we can detect. Models small enough to run here "
    "produce noticeably poor results for resume and cover letter work, and would take "
    "several minutes per response. Ollama remains an option if you upgrade or run it on "
    "another machine on your network (enter its address in the Ollama provider's base "
    "URL field). For now, a free cloud tier (Gemini, Groq, OpenRouter) will give you much "
    "better results -- with automatic identifier redaction protecting your data."
)


def classify_tier(caps: HostCapabilities) -> str:
    ram = caps.ram_gb or 0.0
    vram = caps.gpu_vram_gb or 0.0

    if vram >= 24 or ram >= 48:
        return TIER_WORKSTATION
    if (caps.apple_silicon and ram >= 16) or vram >= 12 or ram >= 32:
        return TIER_STRONG
    if ram >= 16 or vram >= 8:
        return TIER_CAPABLE
    if ram >= 8:
        return TIER_ENTRY
    return TIER_NOT_REASONABLE


def recommend(caps: HostCapabilities) -> ModelRecommendation | None:
    """None means the "not reasonable" tier -- see `not_reasonable_message`."""
    return TIER_TABLE.get(classify_tier(caps))


def not_reasonable_message(caps: HostCapabilities) -> str:
    ram_desc = f"{caps.ram_gb:.0f} GB RAM" if caps.ram_gb else "an undetermined amount of RAM"
    return NOT_REASONABLE_MESSAGE.format(ram=ram_desc)


# ── Install (official channel only, consent required) ────────────────────
# Reuses ops/runtime.py's InstallPlan/InstallStep/run_install_plan as-is --
# those dataclasses and the executor are already generic over "a named
# thing installed via a sequence of shell steps, with an optional license
# notice and post-install note"; Ollama doesn't need a second copy of that
# machinery. The `runtime` field just carries the label "ollama" here.


def macos_install_plan() -> InstallPlan:
    """Homebrew formula (CLI binary + `brew services`), the same official-
    channel precedent ops/runtime.py already uses for Podman. Metal support
    is built into the binary regardless of formula vs. cask -- the
    `ollama-app` cask only adds the menu-bar launcher, which a scripted
    CLI install doesn't need."""
    return InstallPlan(
        runtime="ollama",
        summary="Ollama (Homebrew formula) is the default install on macOS.",
        steps=(
            InstallStep("Install Ollama via Homebrew", ("brew", "install", "ollama")),
            InstallStep("Start the Ollama service", ("brew", "services", "start", "ollama")),
        ),
    )


def linux_install_plan() -> InstallPlan:
    return InstallPlan(
        runtime="ollama",
        summary="Ollama's official install script is the default on Linux.",
        steps=(
            InstallStep(
                "Run Ollama's official install script",
                ("sh", "-c", "curl -fsSL https://ollama.com/install.sh | sh"),
            ),
        ),
        post_install_note="The install script also enables and starts the ollama systemd service.",
    )


def windows_install_plan() -> InstallPlan:
    return InstallPlan(
        runtime="ollama",
        summary="Ollama via winget is the default install on Windows.",
        steps=(InstallStep("Install Ollama via winget", ("winget", "install", "-e", "--id", "Ollama.Ollama")),),
        post_install_note=(
            "Ollama starts automatically after install and registers itself as a background "
            "service -- no separate start step is needed."
        ),
    )


def install_plan_for(system: str | None = None) -> InstallPlan:
    system = system or platform.system()
    if system == "Darwin":
        return macos_install_plan()
    if system == "Linux":
        return linux_install_plan()
    if system == "Windows":
        return windows_install_plan()
    raise OllamaAssistError(f"No known Ollama install path for platform: {system}")


def ensure_ollama_installed(
    *,
    system: str | None = None,
    confirm: Callable[[str], bool] | None = None,
    run: Runner = subprocess.run,
    which: Which = shutil.which,
    dry_run: bool = False,
) -> bool:
    """Detect-first: never reinstalls over a working Ollama (mirrors
    ops/runtime.py's `ensure_runtime`). Returns True once Ollama is
    installed; raises OllamaAssistError if declined or the install doesn't
    come up on PATH."""
    if confirm is None:
        confirm = click.confirm
    if is_ollama_installed(which):
        return True

    plan = install_plan_for(system)
    click.echo(plan.summary)
    if dry_run:
        for step in plan.steps:
            click.echo(f"  (dry-run) {step.description}: {' '.join(step.command)}")
        return False

    if not confirm("Install Ollama now?"):
        raise OllamaAssistError("Ollama is not installed and installation was declined.")

    run_install_plan(plan, run=run)

    if not is_ollama_installed(which):
        raise OllamaAssistError(
            "Ollama was installed but isn't on PATH yet -- open a new terminal/login "
            "session and re-run `job-squire ollama setup`."
        )
    return True


# ── Pulling models ────────────────────────────────────────────────────────


def pull_model(tag: str, run: Runner = subprocess.run) -> None:
    click.echo(f"Pulling {tag} (this can take a while on a first download) ...")
    result = run([OLLAMA_BINARY, "pull", tag], timeout=1800)
    if getattr(result, "returncode", 0) != 0:
        raise OllamaAssistError(f"`ollama pull {tag}` failed.")


def pull_recommended_models(
    rec: ModelRecommendation, run: Runner = subprocess.run, dry_run: bool = False,
) -> list[str]:
    tags = sorted({rec.triage_model, rec.analysis_model})  # dedupe if one tag covers both roles
    pulled: list[str] = []
    for tag in tags:
        if dry_run:
            click.echo(f"  (dry-run) ollama pull {tag}")
            continue
        pull_model(tag, run=run)
        pulled.append(tag)
    return pulled


# ── Context-window derivation (docs/PLAN-ollama-assist.md) ───────────────
# Ollama's OpenAI-compatible endpoint (what app/ai.py calls) has no
# per-request way to set context size -- confirmed against
# https://docs.ollama.com/api/openai-compatibility, which is explicit that
# "The OpenAI API does not have a way of setting the context size for a
# model" and prescribes exactly one method: bake it into the model itself
# via a Modelfile, then reference the derived model's name. That's what
# `derive_context_model` below does, so `setup` can hand app/ai.py a model
# name that's already sized correctly rather than a value it has nowhere to
# send.


def modelfile_for(base_model: str, num_ctx: int) -> str:
    return f"FROM {base_model}\nPARAMETER num_ctx {num_ctx}\n"


def context_model_name(base_model: str, num_ctx: int) -> str:
    """e.g. ("qwen3:8b", 16384) -> "qwen3:8b-ctx16384". Ollama model names
    treat everything after the first ':' as the tag, so appending here stays
    a single valid tag string."""
    return f"{base_model}-ctx{num_ctx}"


def derive_context_model(base_model: str, num_ctx: int, run: Runner = subprocess.run) -> str:
    """`ollama create <base_model>-ctx<num_ctx>` from a temp Modelfile.
    Returns the derived model name. Idempotent in effect -- re-running
    `ollama create` with the same name and Modelfile just recreates the same
    derived model, which is what a repeat `setup` run (e.g. after a
    hardware/tier change) is expected to do.
    """
    import tempfile

    derived_name = context_model_name(base_model, num_ctx)
    with tempfile.NamedTemporaryFile("w", suffix=".Modelfile", delete=False) as f:
        f.write(modelfile_for(base_model, num_ctx))
        modelfile_path = f.name
    try:
        click.echo(f"Creating {derived_name} (num_ctx={num_ctx}) ...")
        result = run([OLLAMA_BINARY, "create", derived_name, "-f", modelfile_path], timeout=600)
        if getattr(result, "returncode", 0) != 0:
            raise OllamaAssistError(f"`ollama create {derived_name}` failed.")
    finally:
        Path(modelfile_path).unlink(missing_ok=True)
    return derived_name


def derive_context_models(
    rec: ModelRecommendation, run: Runner = subprocess.run, dry_run: bool = False,
) -> dict[str, str]:
    """Derive a context-sized model for each distinct base tag in `rec`.
    Returns {base_tag: derived_name} so the caller can map triage_model/
    analysis_model onto their derived equivalents even when both roles
    share one base tag."""
    derived: dict[str, str] = {}
    for base_tag in sorted({rec.triage_model, rec.analysis_model}):
        if dry_run:
            click.echo(f"  (dry-run) ollama create {context_model_name(base_tag, rec.num_ctx)} "
                       f"-f <Modelfile: FROM {base_tag}, PARAMETER num_ctx {rec.num_ctx}>")
            derived[base_tag] = context_model_name(base_tag, rec.num_ctx)
            continue
        derived[base_tag] = derive_context_model(base_tag, rec.num_ctx, run=run)
    return derived


# ── Writing the provider row ──────────────────────────────────────────────


def write_provider_config(
    root: Path,
    *,
    base_url: str,
    triage_model: str,
    analysis_model: str,
    num_ctx: int | None = None,
    rank: int | None = None,
    enabled: bool = True,
    enable_automatic_features: bool = True,
) -> bool:
    """Write (or update) the `ai_provider_configs` row for provider="ollama".

    Mirrors ops/secrets_copy.py's raw-`sqlite3` approach to this exact
    table -- this package never depends on Flask/SQLAlchemy/the app package
    (see that module's docstring). No `api_key_enc` is set (Ollama needs
    none by default).

    `num_ctx` requires the app-side migration that adds this column to
    `ai_provider_configs` (app/__init__.py's _run_migrations) -- older app
    versions without it will reject this write with "no such column: num_ctx";
    that's a real, actionable error rather than a silent no-op, which is
    preferable to guessing at the schema. Pass the *base* model tags'
    context size here, not a per-request value: `AIProviderConfig.num_ctx` is
    metadata describing what `triage_model`/`analysis_model` were actually
    built with (see `derive_context_model` above) -- app/ai.py uses it to
    estimate whether a prompt will fit before calling this provider, not to
    send as a request parameter Ollama's compatible endpoint has no field for.

    `enable_automatic_features` additionally flips `ai_config.api_enabled`
    (the singleton row's "Automatic Features" toggle, app/models.py) to 1.
    Writing a provider row alone is not enough to make auto-triage/follow-up
    drafts/weekly review actually run -- app/worker.py's scheduled jobs all
    gate on this flag independently of whether a provider chain exists, so
    without this a freshly-configured Ollama provider just sits unused until
    the operator finds the "Automatic Features" checkbox in Settings by hand.
    Returns whether that flag was actually flipped (False if `ai_config` has
    no seeded row yet -- reported as a warning, not raised, since the
    provider row write above already succeeded and is the more important of
    the two).
    """
    db_path = paths.sqlite_db_path(root)
    if not db_path.exists():
        raise OllamaAssistError(
            f"Database not found at {db_path}. Bring the instance up at least once "
            f"(`job-squire start NAME`) so the app creates its schema, then re-run this."
        )
    conn = sqlite3.connect(str(db_path))
    try:
        existing = conn.execute(
            "SELECT id, rank FROM ai_provider_configs WHERE provider = ?", (PROVIDER_KEY,)
        ).fetchone()

        if rank is None:
            if existing is not None:
                rank = existing[1]
            else:
                max_rank = conn.execute("SELECT MAX(rank) FROM ai_provider_configs").fetchone()[0]
                rank = (max_rank or 0) + 1

        try:
            if existing is not None:
                conn.execute(
                    "UPDATE ai_provider_configs SET rank = ?, label = ?, base_url = ?, model = ?, "
                    "triage_model = ?, num_ctx = ?, use_for_triage = 1, use_for_analysis = 1, "
                    "enabled = ? WHERE provider = ?",
                    (rank, "Ollama (local)", base_url, analysis_model, triage_model, num_ctx,
                     int(enabled), PROVIDER_KEY),
                )
            else:
                conn.execute(
                    "INSERT INTO ai_provider_configs (rank, provider, label, api_key_enc, base_url, "
                    "model, triage_model, num_ctx, use_for_triage, use_for_analysis, thinking_mode, "
                    "enabled) VALUES (?, ?, ?, '', ?, ?, ?, ?, 1, 1, NULL, ?)",
                    (rank, PROVIDER_KEY, "Ollama (local)", base_url, analysis_model, triage_model,
                     num_ctx, int(enabled)),
                )
        except sqlite3.OperationalError as exc:
            if "num_ctx" in str(exc):
                raise OllamaAssistError(
                    f"{db_path} has no num_ctx column on ai_provider_configs yet -- this instance's "
                    f"job-squire image predates that migration. Update the instance "
                    f"(`job-squire update NAME`) so it boots at least once with the newer schema, "
                    f"then re-run `job-squire ollama setup`."
                ) from exc
            raise

        automatic_features_enabled = False
        if enable_automatic_features:
            # Defensive like every other read/write in this module (module
            # docstring's "additive, never assumed"): a missing `ai_config`
            # row (schema present but not yet seeded) or even a missing
            # table entirely (an instance old enough to predate this
            # feature) both just warn here rather than crash -- the
            # provider row above is already written and is the more
            # important of the two writes.
            try:
                cur = conn.execute("UPDATE ai_config SET api_enabled = 1 WHERE id = 1")
                automatic_features_enabled = cur.rowcount > 0
            except sqlite3.OperationalError:
                automatic_features_enabled = False
            if not automatic_features_enabled:
                click.echo(
                    "Warning: couldn't enable Automatic AI Features (no ai_config row found, or "
                    "this instance's schema predates it). Bring the instance up at least once "
                    "(`job-squire start NAME`), then re-run `job-squire ollama setup`, or turn on "
                    "'Automatic Features' by hand in Settings."
                )

        conn.commit()
    finally:
        conn.close()

    return automatic_features_enabled


# ── End-to-end round-trip test ────────────────────────────────────────────


def test_roundtrip(base_url: str, model: str, timeout: float = 60.0) -> tuple[bool, str]:
    """A minimal generation round-trip straight against the Ollama API.

    This is a lighter stand-in for the plan's "one round-trip prompt
    through the app's provider adapter" -- the real adapter (app/ai.py)
    runs inside the container, which this host-side CLI intentionally does
    not import (see module docstring). The in-app "Test Ollama" button in
    Settings remains the authoritative end-to-end check once the instance
    is running; this just confirms Ollama itself answers before you get
    that far.
    """
    payload = json.dumps({"model": model, "prompt": "Reply with the single word: ok", "stream": False}).encode()
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/generate", data=payload, headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (operator-controlled base_url)
            body = json.loads(resp.read().decode())
            return True, body.get("response", "").strip()
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        return False, str(exc)


# ── Orchestration (the `setup` command's full chain) ──────────────────────


@dataclass
class SetupResult:
    capabilities: HostCapabilities
    tier: str
    recommendation: ModelRecommendation | None
    host_capabilities_path: Path | None
    models_pulled: list[str]
    models_derived: dict[str, str]  # base tag -> context-sized derived model name
    num_ctx: int | None
    base_url: str  # the effective value -- reflects the OLLAMA_CONTAINER_HOST default when the caller passed None
    provider_configured: bool
    automatic_features_enabled: bool
    roundtrip_ok: bool | None
    roundtrip_detail: str | None


def run_setup(
    root: Path,
    *,
    base_url: str | None = None,
    triage_model: str | None = None,
    analysis_model: str | None = None,
    num_ctx: int | None = None,
    rank: int | None = None,
    enable_automatic_features: bool = True,
    confirm: Callable[[str], bool] | None = None,
    run: Runner = subprocess.run,
    which: Which = shutil.which,
    dry_run: bool = False,
    skip_pull: bool = False,
    skip_derive: bool = False,
    skip_test: bool = False,
    system: str | None = None,
) -> SetupResult:
    """The full chain from docs/PLAN-ollama-assist.md's "CLI (automation
    with consent)" flow: check -> install -> pull -> derive -> configure ->
    test. Each ops-layer step already asks its own confirmation where needed
    (`ensure_ollama_installed`); this only sequences them and is the thing
    the `setup` click command calls, per this package's "thin adapter"
    convention (ops/commands.py's module docstring).

    The "derive" step matters: Ollama's OpenAI-compatible endpoint (what
    app/ai.py calls) has no per-request way to set context size, so a base
    tag like "qwen3:8b" is pulled at Ollama's own default (2048 tokens)
    unless a Modelfile says otherwise. This bakes the tier's recommended
    num_ctx into a derived model (e.g. "qwen3:8b-ctx16384") via
    `derive_context_model`, and *that* derived name -- not the base tag --
    is what gets written into the instance's provider row and used for the
    round-trip test below.

    `base_url=None` (the default) resolves to `OLLAMA_CONTAINER_HOST`
    ("http://host.docker.internal:11434") rather than plain "localhost" --
    the container can never reach Ollama on this host via "localhost" (that
    name resolves to the container itself), which used to be exactly the
    failure mode this default silently walked operators into. Pass an
    explicit `--base-url` only when Ollama lives somewhere else (another
    machine on the network).

    `enable_automatic_features=True` (the default) also turns on the app's
    "Automatic Features" toggle (`ai_config.api_enabled`) once the provider
    row is written, so auto-triage/follow-up drafts/weekly review actually
    start running instead of the provider sitting configured-but-idle. Pass
    `False` to configure Ollama for manual/MCP-only use without touching
    that toggle.
    """
    if confirm is None:
        confirm = click.confirm
    if base_url is None:
        base_url = OLLAMA_CONTAINER_HOST

    caps = detect_host_capabilities(system=system, run=run, which=which)
    tier = classify_tier(caps)
    rec = TIER_TABLE.get(tier)
    if rec is None:
        raise OllamaAssistError(not_reasonable_message(caps))

    base_triage = triage_model or rec.triage_model
    base_analysis = analysis_model or rec.analysis_model
    effective_num_ctx = num_ctx or rec.num_ctx
    # A caller-supplied triage/analysis model overrides the tag to pull, but the
    # tier's num_ctx (or an explicit --num-ctx) is still what gets baked in --
    # re-derive with whatever base tag is actually in play here, not the tier's.
    effective_rec = ModelRecommendation(
        tier=rec.tier, description=rec.description, triage_model=base_triage,
        analysis_model=base_analysis, approx_download_gb=rec.approx_download_gb,
        num_ctx=effective_num_ctx,
    )

    host_caps_path = None if dry_run else write_host_capabilities(root, caps)

    ensure_ollama_installed(system=system, confirm=confirm, run=run, which=which, dry_run=dry_run)

    models_pulled: list[str] = []
    if not skip_pull:
        tags = sorted({base_triage, base_analysis})
        for tag in tags:
            if dry_run:
                click.echo(f"  (dry-run) ollama pull {tag}")
                continue
            pull_model(tag, run=run)
            models_pulled.append(tag)

    models_derived: dict[str, str] = {}
    if not skip_derive:
        models_derived = derive_context_models(effective_rec, run=run, dry_run=dry_run)
    effective_triage = models_derived.get(base_triage, base_triage)
    effective_analysis = models_derived.get(base_analysis, base_analysis)

    provider_configured = False
    automatic_features_enabled = False
    if dry_run:
        click.echo(
            f"  (dry-run) write ai_provider_configs row: base_url={base_url}, "
            f"triage_model={effective_triage}, analysis_model={effective_analysis}, "
            f"num_ctx={effective_num_ctx}"
        )
        if enable_automatic_features:
            click.echo("  (dry-run) enable Automatic AI Features (ai_config.api_enabled = 1)")
    else:
        automatic_features_enabled = write_provider_config(
            root, base_url=base_url, triage_model=effective_triage, analysis_model=effective_analysis,
            num_ctx=effective_num_ctx if not skip_derive else None, rank=rank,
            enable_automatic_features=enable_automatic_features,
        )
        provider_configured = True

    roundtrip_ok = roundtrip_detail = None
    if not dry_run and not skip_test:
        # This test runs from the CLI/host process, not the container -- so
        # it must probe wherever *this host* actually reaches Ollama, not
        # base_url when that's the container-only "host.docker.internal"
        # default (unresolvable from bare host in the common case). A
        # caller-supplied base_url pointing at a real network address (a
        # different machine) is assumed reachable from here too and is
        # tested as given.
        test_target = OLLAMA_DEFAULT_HOST if base_url == OLLAMA_CONTAINER_HOST else base_url
        roundtrip_ok, roundtrip_detail = test_roundtrip(test_target, effective_triage)

    return SetupResult(
        capabilities=caps,
        tier=tier,
        recommendation=rec,
        host_capabilities_path=host_caps_path,
        models_pulled=models_pulled,
        models_derived=models_derived,
        num_ctx=effective_num_ctx if not skip_derive else None,
        base_url=base_url,
        provider_configured=provider_configured,
        automatic_features_enabled=automatic_features_enabled,
        roundtrip_ok=roundtrip_ok,
        roundtrip_detail=roundtrip_detail,
    )
