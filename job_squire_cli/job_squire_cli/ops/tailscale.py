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
"""Tailscale Serve for private remote access to a local instance (Prompt
C11, docs/PLAN-deployment-modes.md Section 5 "Reaching a local instance
from your own devices (Tailscale)" and Section 7's touchpoints).

**Serve, never Funnel.** `enable` only ever calls `tailscale serve`. Serve
terminates real HTTPS with a `device.tailnet.ts.net` certificate Tailscale
provisions and forwards to a loopback target on the same machine -- it is
reachable only by devices already in the operator's own tailnet. Funnel is
public exposure and is out of scope here entirely; nothing in this module
ever invokes it.

**The instance stays local, not a fourth mode.** Per the plan: "It is
therefore best thought of as local mode with a private Serve front door
rather than a separate mode." So `enable` never touches the registry's
`Instance.mode` (it stays `"local"`) or `DEPLOY_MODE` in `data/.env` (it
stays `local`). What it *does* flip, matching the plan's "adopts the
network-mode application flags for those sessions", are the three
individual overrides the app's own `DEPLOY_MODE` preset resolution
(app/deploy.py) already supports independently of the mode string:
`TRUST_PROXY=1` (so it honors Serve's forwarded scheme/host), `SESSION_
COOKIE_SECURE=true`, and `PUBLIC_URL`/`PUBLIC_MCP_URL`/`PUBLIC_MCP_HOST`
set to the tailnet hostname. The compose file itself is never touched --
unlike ops/proxy.py's network-mode provisioning, there is no shared Docker
network to join here: Serve runs as a host-level daemon and reaches the
instance the same way any other host process would, through the loopback
host port `create` already published (`127.0.0.1:<app_port>` /
`127.0.0.1:<mcp_port>`), which is exactly "the app never leaves loopback."

**A known, expected app-side warning.** `app/deploy.py`'s startup guard
flags `DEPLOY_MODE=local` combined with a non-loopback `PUBLIC_URL` as a
*warning* (not fatal -- the container still starts): "local mode assumes
this instance is reached only via a loopback address ... A non-loopback
PUBLIC_URL contradicts that." A Tailscale-enabled instance is exactly this
combination by design, so that in-app banner is expected while Serve is
enabled, not a sign anything is actually wrong; `enable_tailscale_serve`
says so in its result so the CLI layer can tell the operator up front
instead of leaving them to discover it as a surprise banner.

**Where the on/off state lives.** Not the registry (Prompt C4's `Instance`
dataclass is a fixed, non-secret schema with no room for this without a
migration, and this is a toggle on an *existing* field's meaning, not new
instance identity). Instead, a small `tailscale.json` manifest sits beside
`docker-compose.single.yml` in the instance's own root -- the same
per-instance-directory precedent `ops/mcp_token.py`'s module docstring
already establishes for state that belongs to one instance but has no
natural home in the fixed registry schema. `read_state`/`is_tailnet_
reachable` are what Prompt C6's reachability rule (`ops/mcp_token.py`'s
`is_static_token_allowed`, keyed on `Instance.mode`) cannot see on its
own: `Instance.mode` stays `"local"` here by design (above), so
`configure`'s static-token gate additionally consults this manifest so a
Tailscale-reachable instance gets the same explicit-opt-in treatment a
`mode="network"` instance gets, never an implicit allow.
"""
from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import compose, dotenv, lifecycle, paths
from .registry import Instance, derive_compose_project, update_instance

Runner = Callable[..., "subprocess.CompletedProcess[str]"]
Sleep = Callable[[float], None]

TAILSCALE_BINARY = "tailscale"
STATE_FILENAME = "tailscale.json"

# Tailscale Serve only issues a valid HTTPS certificate on these three
# ports -- a Serve-specific constraint, distinct from Funnel's separate
# three-funnel cap (PLAN Section 5's "caps at three funnels" is about
# Funnel, not this). An operator running more than one Tailscale-enabled
# instance on the same machine picks distinct ports from this same set of
# three for each instance's web/MCP pair.
ALLOWED_SERVE_PORTS = (443, 8443, 10000)
DEFAULT_WEB_SERVE_PORT = 443
DEFAULT_MCP_SERVE_PORT = 8443


class TailscaleError(RuntimeError):
    """Raised for any Tailscale CLI, state, or provisioning failure."""


# ── State manifest ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class TailscaleState:
    enabled: bool
    hostname: str | None = None
    web_port: int | None = None
    mcp_port: int | None = None
    enabled_at: str | None = None


def state_path(root: Path) -> Path:
    return root / STATE_FILENAME


def read_state(root: Path) -> TailscaleState:
    path = state_path(root)
    if not path.exists():
        return TailscaleState(enabled=False)
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise TailscaleError(f"Cannot read {path}: {exc}") from exc
    return TailscaleState(
        enabled=bool(data.get("enabled")),
        hostname=data.get("hostname"),
        web_port=data.get("web_port"),
        mcp_port=data.get("mcp_port"),
        enabled_at=data.get("enabled_at"),
    )


def _write_state(root: Path, state: TailscaleState) -> None:
    state_path(root).write_text(json.dumps({
        "enabled": state.enabled,
        "hostname": state.hostname,
        "web_port": state.web_port,
        "mcp_port": state.mcp_port,
        "enabled_at": state.enabled_at,
    }, indent=2) + "\n")


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_tailnet_reachable(root: Path) -> bool:
    """Whether the instance rooted at `root` currently has Tailscale Serve
    fronting it -- see the module docstring's "Where the on/off state
    lives" for why `configure` (ops/commands.py) needs this alongside
    (not instead of) `mcp_token.is_static_token_allowed`."""
    return read_state(root).enabled


# ── Device identity ──────────────────────────────────────────────────────


def device_dns_name(*, run: Runner = subprocess.run) -> str:
    """This machine's `<device>.<tailnet>.ts.net` name, from `tailscale
    status --json`'s `Self.DNSName` (trailing dot stripped -- Tailscale's
    own JSON reports it fully qualified)."""
    argv = [TAILSCALE_BINARY, "status", "--json"]
    try:
        result = run(argv, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TailscaleError(
            f"Failed to run `tailscale status`: {exc}. Is the Tailscale client installed?"
        ) from exc
    if result.returncode != 0:
        raise TailscaleError(
            f"`tailscale status` failed: {(result.stderr or result.stdout).strip()} -- make sure "
            f"Tailscale is installed and this device is logged into a tailnet (`tailscale up`)."
        )
    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise TailscaleError(f"Could not parse `tailscale status --json` output: {exc}") from exc
    dns_name = ((data.get("Self") or {}).get("DNSName") or "").rstrip(".")
    if not dns_name:
        raise TailscaleError(
            "Tailscale reported no DNSName for this device -- make sure MagicDNS is enabled for "
            "your tailnet (in the Tailscale admin console, under DNS) and this device is logged in."
        )
    return dns_name


# ── Driving `tailscale serve` ────────────────────────────────────────────


def _check_port(port: int) -> None:
    if port not in ALLOWED_SERVE_PORTS:
        raise TailscaleError(
            f"{port} is not one of Tailscale Serve's supported HTTPS ports {ALLOWED_SERVE_PORTS} "
            f"-- choose one of those instead."
        )


def enable_serve_port(port: int, target_port: int, *, run: Runner = subprocess.run) -> None:
    """`tailscale serve --bg --https=<port> http://127.0.0.1:<target_port>`
    -- Serve, never Funnel, and always forwarding to loopback."""
    _check_port(port)
    argv = [TAILSCALE_BINARY, "serve", "--bg", f"--https={port}", f"http://127.0.0.1:{target_port}"]
    try:
        result = run(argv, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TailscaleError(f"Failed to run `{' '.join(argv)}`: {exc}") from exc
    if result.returncode != 0:
        raise TailscaleError(
            f"`tailscale serve --https={port}` failed: {(result.stderr or result.stdout).strip()}"
        )


def disable_serve_port(port: int, *, run: Runner = subprocess.run) -> None:
    """`tailscale serve --https=<port> off` -- idempotent: turning off a
    port that isn't currently served is not an error, same precedent as
    ops/proxy.py's `ensure_network`/`attach_to_network`."""
    argv = [TAILSCALE_BINARY, "serve", f"--https={port}", "off"]
    try:
        result = run(argv, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TailscaleError(f"Failed to run `{' '.join(argv)}`: {exc}") from exc
    stderr_low = (result.stderr or "").lower()
    if result.returncode != 0 and "not" not in stderr_low:
        raise TailscaleError(f"Failed to turn off Tailscale Serve on port {port}: "
                              f"{(result.stderr or result.stdout).strip()}")


# ── Orchestration ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TailscaleEnableResult:
    hostname: str
    web_port: int
    mcp_port: int
    public_url: str
    public_mcp_url: str
    health: dict | None
    expected_warning: str


def enable_tailscale_serve(
    instance: Instance,
    *,
    root: Path,
    web_port: int = DEFAULT_WEB_SERVE_PORT,
    mcp_port: int = DEFAULT_MCP_SERVE_PORT,
    run: Runner = subprocess.run,
    sleep: Sleep = time.sleep,
) -> TailscaleEnableResult:
    """Front `instance`'s loopback web and MCP ports with Tailscale Serve
    end to end: resolve this device's tailnet hostname, turn Serve on for
    both ports, flip the instance's `data/.env` to the network-mode
    application flags (leaving `DEPLOY_MODE`/`Instance.mode` at `local` --
    see the module docstring), recreate the container so the new env takes
    effect, update the registry's `public_url` so `status`/`list` reflect
    the tailnet address while enabled, and record the on state.
    """
    if instance.mode != "local":
        raise TailscaleError(
            f"Instance {instance.name!r} is in {instance.mode!r} mode -- Tailscale Serve only applies "
            f"to local instances (PLAN Section 5: it is a private remote-access path for a local "
            f"install, not a substitute for network mode's own reverse proxy)."
        )
    if instance.app_port is None or instance.mcp_port is None:
        raise TailscaleError(f"Instance {instance.name!r} has no recorded loopback ports to serve.")
    if web_port == mcp_port:
        raise TailscaleError("--web-port and --mcp-port must be different Serve ports.")
    _check_port(web_port)
    _check_port(mcp_port)

    hostname = device_dns_name(run=run)
    public_url = f"https://{hostname}" if web_port == 443 else f"https://{hostname}:{web_port}"
    public_mcp_host = hostname if mcp_port == 443 else f"{hostname}:{mcp_port}"
    public_mcp_url = f"https://{public_mcp_host}"

    enable_serve_port(web_port, instance.app_port, run=run)
    try:
        enable_serve_port(mcp_port, instance.mcp_port, run=run)
    except TailscaleError:
        disable_serve_port(web_port, run=run)
        raise

    env_path = paths.data_env_path(root)
    dotenv.set_line(env_path, "TRUST_PROXY", "true")
    dotenv.set_line(env_path, "SESSION_COOKIE_SECURE", "true")
    dotenv.set_line(env_path, "PUBLIC_URL", public_url)
    dotenv.set_line(env_path, "PUBLIC_MCP_URL", public_mcp_url)
    dotenv.set_line(env_path, "PUBLIC_MCP_HOST", public_mcp_host)

    container_name = derive_compose_project(instance.name)
    up_result = compose.compose_up(
        instance.runtime, root, container_name, run=run, extra_args=["--force-recreate"],
    )
    if up_result.returncode != 0:
        raise TailscaleError(
            f"Tailscale Serve is on, but recreating {instance.name!r} to pick up the new PUBLIC_URL/"
            f"TRUST_PROXY failed: {(up_result.stderr or up_result.stdout).strip()}"
        )
    health = lifecycle.wait_for_state(instance.runtime, container_name, run=run, sleep=sleep)

    update_instance(instance.name, public_url=public_url)
    _write_state(root, TailscaleState(
        enabled=True, hostname=hostname, web_port=web_port, mcp_port=mcp_port,
        enabled_at=_utc_stamp(),
    ))

    return TailscaleEnableResult(
        hostname=hostname, web_port=web_port, mcp_port=mcp_port,
        public_url=public_url, public_mcp_url=public_mcp_url, health=health,
        expected_warning=(
            "The app's own startup guard will show a WARNING banner about PUBLIC_URL not being a "
            "loopback address while DEPLOY_MODE stays 'local' -- expected here (Tailscale Serve is "
            "deliberately local mode with a private front door, not network mode), not a sign of "
            "misconfiguration."
        ),
    )


@dataclass(frozen=True)
class TailscaleDisableResult:
    public_url: str
    health: dict | None


def disable_tailscale_serve(
    instance: Instance, *, root: Path, run: Runner = subprocess.run, sleep: Sleep = time.sleep,
) -> TailscaleDisableResult:
    """Turn Serve off for both ports, revert `data/.env` to exactly the
    local-mode defaults `lifecycle.create_instance` itself would have
    written (loopback PUBLIC_URL/PUBLIC_MCP_URL/PUBLIC_MCP_HOST, TRUST_
    PROXY/SESSION_COOKIE_SECURE off), recreate the container, restore the
    registry's `public_url`, and clear the state manifest.
    """
    state = read_state(root)
    if not state.enabled:
        raise TailscaleError(f"Instance {instance.name!r} does not have Tailscale Serve enabled.")

    if state.web_port is not None:
        disable_serve_port(state.web_port, run=run)
    if state.mcp_port is not None:
        disable_serve_port(state.mcp_port, run=run)

    public_url = f"http://localhost:{instance.app_port}"
    public_mcp_url = f"http://localhost:{instance.mcp_port}"

    env_path = paths.data_env_path(root)
    dotenv.set_line(env_path, "TRUST_PROXY", "false")
    dotenv.set_line(env_path, "SESSION_COOKIE_SECURE", "false")
    dotenv.set_line(env_path, "PUBLIC_URL", public_url)
    dotenv.set_line(env_path, "PUBLIC_MCP_URL", public_mcp_url)
    dotenv.set_line(env_path, "PUBLIC_MCP_HOST", "localhost")

    container_name = derive_compose_project(instance.name)
    up_result = compose.compose_up(
        instance.runtime, root, container_name, run=run, extra_args=["--force-recreate"],
    )
    if up_result.returncode != 0:
        raise TailscaleError(
            f"Tailscale Serve is off, but recreating {instance.name!r} to drop back to loopback "
            f"PUBLIC_URL failed: {(up_result.stderr or up_result.stdout).strip()}"
        )
    health = lifecycle.wait_for_state(instance.runtime, container_name, run=run, sleep=sleep)

    update_instance(instance.name, public_url=public_url)
    _write_state(root, TailscaleState(enabled=False))

    return TailscaleDisableResult(public_url=public_url, health=health)
