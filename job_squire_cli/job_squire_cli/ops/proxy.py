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
"""Reverse-proxy provisioning for network-mode instances.

Two cases:

  1. **An existing proxy.** `detect_existing_proxy` looks for a running
     SWAG or bare-nginx container and its host config directory; the
     generated confs are dropped in and the proxy is reloaded. No second
     proxy is ever installed.
  2. **No proxy.** `install_swag` stands up a LinuxServer SWAG container
     (bundling nginx, certbot, fail2ban) at a fixed per-user location
     (`swag_root`), sibling to the per-instance directories in ops/paths.py
     but not itself a registered instance.

Either way, the instance's own container is attached to whatever Docker
network the proxy is on (`resolve_shared_network`/`attach_to_network`) so
nginx can resolve it by container name rather than guessing at a host IP
from inside its own network namespace -- the shared-network pattern the
app repo's now-retired three-container `docker-compose.swag.yml` used to
document, for this CLI's single container instead.

The proxy stays a separate, independently maintained component: nothing
here is baked into the Job Squire image, TLS still terminates at the
proxy, and DNS/certificate validation (DuckDNS, Cloudflare DNS-01, ...) is
the `dns` commands' job, not this one -- `install_swag` only brings up a
plain SWAG container with whatever URL/validation the operator already
has in hand (or empty placeholders the `dns` commands fill in later).

**Why the nginx conf text is a hand-rolled template here rather than read
from examples/nginx/ at runtime**: exactly the reason compose.py's own
docstring gives for not reading docker-compose.yml from the repo --
this package is `pip install`-able as `job-squire-cli` from PyPI/GitHub
with no repo checkout on disk, and `pyproject.toml` only ships the
`job_squire_cli` package itself, not the repo's `examples/` directory. The
two are kept in sync by hand; the app repo's examples/nginx/*.subdomain.conf
files are the source of truth for what a *manually* configured proxy needs,
and `_WEB_CONF_TEMPLATE`/`_MCP_CONF_TEMPLATE` below mirror them, adapted for
one difference the single-container architecture requires: both templates
originally named two different upstream containers (`job-squire` on 8000,
`job-squire-mcp` on 9000) from the old three-container topology; here both
point at the *same* container (this CLI only ever creates one), just on
two different ports, and the filename is namespaced per instance so more
than one CLI-managed instance can share a proxy.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from . import compose, dotenv, paths
from .registry import Instance, derive_compose_project

Runner = Callable[..., "subprocess.CompletedProcess[str]"]
Confirm = Callable[[str], bool]
Sleep = Callable[[float], None]

DEFAULT_PROXY_NETWORK = "job-squire-proxy"
PROXY_ROOT_DIRNAME = "_proxy"
SWAG_CONTAINER_NAME = "job-squire-swag"
SWAG_IMAGE = "lscr.io/linuxserver/swag"
PROXY_CONFS_SUBPATH = ("nginx", "proxy-confs")

# Docker/Podman's own built-in network names -- never usable for container
# name resolution (no embedded DNS on these), so a proxy attached only to
# one of these still needs a real shared network created for it.
_BUILTIN_NETWORK_NAMES = {"bridge", "podman", "host", "none"}

_SWAG_HINTS = ("swag", "linuxserver/swag")
_NGINX_HINTS = ("nginx",)


class ProxyError(RuntimeError):
    """Raised for any proxy-detection, install, or provisioning failure."""


# ── Target description ──────────────────────────────────────────────────


@dataclass(frozen=True)
class ProxyTarget:
    """A reverse proxy the CLI can drop instance confs into and reload.

    `container_name` is None only for a manually-specified bare-nginx
    config directory with no known container to `exec ... nginx -s
    reload` -- the operator is told to reload it themselves in that case.
    """

    config_dir: Path
    container_name: str | None
    kind: str  # "swag" | "nginx" | "manual"


@dataclass(frozen=True)
class ProxyProvisionResult:
    proxy: ProxyTarget
    network: str
    web_conf_path: Path
    mcp_conf_path: Path
    installed_swag: bool


# ── nginx conf templates (mirrors examples/nginx/*.subdomain.conf) ──────


_WEB_CONF_TEMPLATE = """\
# nginx reverse-proxy config for the Job Squire web app -- instance {instance_name!r}.
#
# Generated by `job-squire proxy {instance_name}` (see ops/proxy.py). Mirrors
# examples/nginx/job-squire.subdomain.conf in the app repo, adapted for the
# single-container image: the same container serves both the web app (this
# file, port 8000) and MCP (mcp-{instance_name}.subdomain.conf, port
# {mcp_port}).
#
# Reload after installing:
#   docker exec {proxy_container} nginx -s reload    # SWAG / containerized nginx
#   nginx -s reload                                  # bare nginx on the host

server {{
    listen 443 ssl;
    listen [::]:443 ssl;

    server_name {subdomain}.*;

    include /config/nginx/ssl.conf;

    client_max_body_size 12m;

    location / {{
        include /config/nginx/proxy.conf;
{upstream_block}
        # Never proxy-cache responses from this app. Session cookies carry
        # per-user CSRF tokens; a cached login page served to a different
        # user causes "CSRF session token is missing" on form submit.
        proxy_cache off;
        proxy_cache_bypass 1;
        proxy_no_cache 1;
    }}
}}
"""

_MCP_CONF_TEMPLATE = """\
# nginx reverse-proxy config for the Job Squire MCP server -- instance {instance_name!r}.
#
# Generated by `job-squire proxy {instance_name}` (see ops/proxy.py). Mirrors
# examples/nginx/mcp-squire.subdomain.conf in the app repo, adapted for the
# single-container image: the same container serves both the web app
# (job-squire-{instance_name}.subdomain.conf, port 8000) and MCP (this file).
#
# Add this as a custom connector in Claude using the base URL:
#   https://{subdomain}.<yourdomain>
#
# MCP Streamable HTTP can hold long-lived streaming responses, so buffering is
# off and timeouts are generous.
#
# NOTE: SWAG's proxy.conf already sets proxy_http_version, the Connection header,
# buffering, and timeouts. Do NOT redeclare any proxy_* directive here; nginx
# rejects duplicates. If using bare nginx (not SWAG), add those directives
# manually -- see the nginx docs for proxy_pass configuration.

server {{
    listen 443 ssl;
    listen [::]:443 ssl;

    server_name {subdomain}.*;

    include /config/nginx/ssl.conf;

    # Serve this endpoint over HTTP/1.1. MCP uses unbuffered streaming/SSE, which
    # triggers nginx HTTP/2 framing errors (curl 92 / PROTOCOL_ERROR). HTTP/1.1 is
    # the correct transport for SSE and avoids the problem. (nginx >= 1.25.1)
    http2 off;

    client_max_body_size 12m;

    location / {{
        include /config/nginx/proxy.conf;
{upstream_block}
        # MCP responses must never be proxy-cached.
        proxy_cache off;
        proxy_cache_bypass 1;
        proxy_no_cache 1;
    }}
}}
"""


def _container_upstream_block(container_name: str, port: int) -> str:
    """Resolve the upstream by Docker container name over the shared proxy
    network (docker's embedded DNS at 127.0.0.11) -- used whenever the
    proxy is itself a container so it can share that network with this
    instance's container (`resolve_shared_network`/`attach_to_network`)."""
    return (
        "        resolver 127.0.0.11 valid=30s;\n"
        f"        set $upstream_app {container_name};\n"
        f"        set $upstream_port {port};\n"
        "        set $upstream_proto http;\n"
        "        proxy_pass $upstream_proto://$upstream_app:$upstream_port;\n"
    )


def _hostport_upstream_block(port: int) -> str:
    """Proxy straight to the instance's published host port -- used when
    the proxy is *not* containerized (a bare `nginx -s reload`-managed
    install running directly on the host OS), which shares the host's
    loopback interface directly and has no Docker network to join. Matches
    the fallback the app repo's own examples/nginx template documents
    ("If using host-port mode instead ... proxy_pass http://<host-ip>:8080;")."""
    return f"        proxy_pass http://127.0.0.1:{port};\n"


def conf_filenames(instance_name: str) -> tuple[str, str]:
    """Per-instance filenames, namespaced so more than one CLI-managed
    instance can drop confs into the same proxy without clobbering each
    other -- unlike the app repo's single-install example filenames."""
    return f"job-squire-{instance_name}.subdomain.conf", f"mcp-job-squire-{instance_name}.subdomain.conf"


def _hostname_label(hostname: str) -> str:
    """The leftmost DNS label, e.g. `squire` from `squire.example.com` --
    what SWAG's own `server_name <label>.*;` convention expects (matching
    any base domain/TLD SWAG is actually configured with)."""
    return hostname.split(".")[0] if hostname else hostname


def derive_subdomains(instance: Instance, root: Path) -> tuple[str, str]:
    """(web_subdomain, mcp_subdomain) for `instance`.

    The web hostname comes from the registry's own `public_url`. The MCP
    hostname is *not* in the registry (Instance has no public_mcp_host
    field -- see ops/commands.py's `_derive_mcp_endpoint`), so this reads
    the actual `PUBLIC_MCP_HOST` `create` wrote into data/.env, falling
    back to the same `mcp-<hostname>` convention `create --mcp-hostname`
    defaults to if that key is somehow absent (e.g. a hand-edited env).
    """
    web_host = urlparse(instance.public_url).hostname or instance.public_url
    mcp_host = dotenv.get(paths.data_env_path(root), "PUBLIC_MCP_HOST") or f"mcp-{web_host}"
    return _hostname_label(web_host), _hostname_label(mcp_host)


def render_web_conf(*, instance_name: str, subdomain: str, proxy_container: str | None,
                     mcp_port_note: int, upstream_block: str) -> str:
    return _WEB_CONF_TEMPLATE.format(
        instance_name=instance_name, subdomain=subdomain, mcp_port=mcp_port_note,
        proxy_container=proxy_container or "<proxy>", upstream_block=upstream_block,
    )


def render_mcp_conf(*, instance_name: str, subdomain: str, upstream_block: str) -> str:
    return _MCP_CONF_TEMPLATE.format(instance_name=instance_name, subdomain=subdomain, upstream_block=upstream_block)


def install_confs(
    proxy: ProxyTarget, *, instance_name: str, subdomain_web: str, subdomain_mcp: str,
    container_name: str, app_port: int, mcp_port_host: int, mcp_port_internal: int,
) -> tuple[Path, Path]:
    """Render and write both confs into `proxy.config_dir`'s proxy-confs
    subdirectory, returning their paths. Overwrites a previous run's confs
    for the same instance in place (re-provisioning is idempotent).

    The upstream form depends on whether the proxy is itself a container
    (`proxy.container_name` set): containerized, it resolves this
    instance's container by name over the shared Docker network;
    otherwise (a bare `nginx -s reload`-managed install on the host) it
    proxies straight to the instance's published host ports, since a
    non-containerized proxy has no Docker network to join in the first
    place (see `_container_upstream_block`/`_hostport_upstream_block`).
    """
    confs_dir = proxy.config_dir.joinpath(*PROXY_CONFS_SUBPATH)
    confs_dir.mkdir(parents=True, exist_ok=True)
    web_name, mcp_name = conf_filenames(instance_name)
    web_path = confs_dir / web_name
    mcp_path = confs_dir / mcp_name

    if proxy.container_name:
        web_upstream = _container_upstream_block(container_name, 8000)
        mcp_upstream = _container_upstream_block(container_name, mcp_port_internal)
        mcp_note_port = mcp_port_internal
    else:
        web_upstream = _hostport_upstream_block(app_port)
        mcp_upstream = _hostport_upstream_block(mcp_port_host)
        mcp_note_port = mcp_port_host

    web_path.write_text(render_web_conf(
        instance_name=instance_name, subdomain=subdomain_web, proxy_container=proxy.container_name,
        mcp_port_note=mcp_note_port, upstream_block=web_upstream,
    ))
    mcp_path.write_text(render_mcp_conf(
        instance_name=instance_name, subdomain=subdomain_mcp, upstream_block=mcp_upstream,
    ))
    return web_path, mcp_path


def remove_confs(proxy: ProxyTarget, *, instance_name: str) -> list[Path]:
    """The reverse of `install_confs`: delete `instance_name`'s web/MCP
    confs from `proxy`'s proxy-confs directory, if present. Returns the
    paths actually removed -- empty if neither existed (e.g. `job-squire
    proxy`/`create`'s own proxy offer was never run for this instance, or
    they were already cleaned up). Does not reload the proxy; the caller
    (`remove` in ops/commands.py) does that once, after this and whatever
    else it's doing to the proxy for this instance."""
    confs_dir = proxy.config_dir.joinpath(*PROXY_CONFS_SUBPATH)
    web_name, mcp_name = conf_filenames(instance_name)
    removed: list[Path] = []
    for filename in (web_name, mcp_name):
        path = confs_dir / filename
        if path.exists():
            path.unlink()
            removed.append(path)
    return removed


def is_managed_swag(proxy: ProxyTarget, *, data_root: Path | None = None) -> bool:
    """True only if `proxy` is the SWAG container `job-squire proxy`
    itself installs at `swag_root` -- as opposed to a third-party SWAG (or
    bare nginx) the operator already had running that `detect_existing_proxy`
    merely found and reused. Mirrors `ops/dns.py`'s own `_managed_swag_target`
    scope guard: only a CLI-managed SWAG is ever a candidate for
    `remove_managed_swag` below. Matching on both the container name and
    the actual resolved config directory (not just `kind == "swag"`) is
    deliberate -- an operator could plausibly run their own SWAG under the
    same container name, and confusing that for the CLI's own install
    would make `remove` delete someone else's proxy and DNS/TLS setup."""
    if proxy.kind != "swag" or proxy.container_name != SWAG_CONTAINER_NAME:
        return False
    try:
        return proxy.config_dir.resolve() == (swag_root(data_root) / "config").resolve()
    except OSError:
        return False


def remove_managed_swag(runtime: str, *, data_root: Path | None = None, run: Runner = subprocess.run) -> None:
    """Stop and remove the CLI's own SWAG container (`compose down`) and
    delete its entire config directory -- including whatever DNS/TLS state
    `job-squire dns duckdns`/`cloudflare` wrote into it: the
    DUCKDNSTOKEN/DNSPLUGIN compose environment variables, `dns-conf/
    cloudflare.ini`, and the Let's Encrypt certificate SWAG itself
    obtained. There is no separate "per-instance DNS config" to remove --
    `ops/dns.py` configures this one shared SWAG install for every
    network-mode instance behind it, so this is only ever called once no
    other registered instance still has confs sitting in it (see
    `_offer_proxy_removal` in ops/commands.py). Only ever call this against
    a `ProxyTarget` `is_managed_swag` has already confirmed -- there is no
    internal check here against accidentally pointing `runtime`/`data_root`
    at a third-party install.
    """
    root = swag_root(data_root)
    compose_path = root / "docker-compose.yml"
    if compose_path.exists():
        argv = [*compose.compose_binary(runtime), "--project-directory", str(root),
                "-f", str(compose_path), "-p", "job-squire-proxy", "down"]
        try:
            result = run(argv, cwd=str(root), capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProxyError(f"Failed to stop the SWAG proxy: {exc}") from exc
        if result.returncode != 0:
            raise ProxyError(f"Failed to stop the SWAG proxy: {(result.stderr or result.stdout).strip()}")
    shutil.rmtree(root, ignore_errors=True)


# ── Detecting an existing proxy ──────────────────────────────────────────


def list_running_containers(runtime: str, *, run: Runner = subprocess.run) -> list[tuple[str, str]]:
    """`[(name, image), ...]` for every running container, via `docker/podman
    ps` -- used to spot a container that looks like an existing SWAG or
    bare-nginx proxy without the operator having to name it."""
    argv = [compose.runtime_binary(runtime), "ps", "--format", "{{.Names}}\t{{.Image}}"]
    try:
        result = run(argv, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    rows = []
    for line in (result.stdout or "").splitlines():
        if "\t" not in line:
            continue
        name, image = line.split("\t", 1)
        rows.append((name.strip(), image.strip()))
    return rows


def inspect_mounts(runtime: str, container_name: str, *, run: Runner = subprocess.run) -> list[dict]:
    argv = [compose.runtime_binary(runtime), "inspect", "--format", "{{json .Mounts}}", container_name]
    try:
        result = run(argv, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    try:
        return json.loads(result.stdout) or []
    except (json.JSONDecodeError, TypeError):
        return []


def inspect_networks(runtime: str, container_name: str, *, run: Runner = subprocess.run) -> list[str]:
    argv = [compose.runtime_binary(runtime), "inspect", "--format", "{{json .NetworkSettings.Networks}}", container_name]
    try:
        result = run(argv, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    try:
        parsed = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        return []
    return list(parsed.keys()) if isinstance(parsed, dict) else []


def _mount_source(mounts: list[dict], destination: str) -> str | None:
    for mount in mounts:
        if mount.get("Destination") == destination:
            return mount.get("Source")
    return None


def _matches(name: str, image: str, hints: tuple[str, ...]) -> bool:
    low_name, low_image = name.lower(), image.lower()
    return any(h in low_name for h in hints) or any(h in low_image for h in hints)


def detect_existing_proxy(
    runtime: str, *, run: Runner = subprocess.run, container_hint: str | None = None,
) -> ProxyTarget | None:
    """Best-effort: find a running SWAG or bare-nginx container and its
    host config directory, so `provision_instance_proxy` can drop the
    generated confs in and reload it without installing a second proxy --
    if the machine already runs SWAG or another nginx-based proxy, no
    second proxy is ever installed.

    SWAG is checked first (more specific: its `/config` bind mount is
    where `nginx/proxy-confs/` lives). Bare nginx is checked second, using
    the conventional `/etc/nginx/conf.d` (falling back to `/etc/nginx`)
    mount destination. Returns None if nothing recognizable is running --
    the caller then falls back to installing SWAG or the operator's
    explicit `--config-dir`.
    """
    containers = list_running_containers(runtime, run=run)
    if container_hint:
        hinted = [(n, i) for (n, i) in containers if n == container_hint]
        containers = hinted or containers

    for name, image in containers:
        if _matches(name, image, _SWAG_HINTS):
            config_src = _mount_source(inspect_mounts(runtime, name, run=run), "/config")
            if config_src:
                return ProxyTarget(config_dir=Path(config_src), container_name=name, kind="swag")

    for name, image in containers:
        if _matches(name, image, _NGINX_HINTS):
            mounts = inspect_mounts(runtime, name, run=run)
            config_src = _mount_source(mounts, "/etc/nginx/conf.d") or _mount_source(mounts, "/etc/nginx")
            if config_src:
                return ProxyTarget(config_dir=Path(config_src), container_name=name, kind="nginx")

    return None


# ── Shared Docker network ────────────────────────────────────────────────


def ensure_network(runtime: str, network: str, *, run: Runner = subprocess.run) -> None:
    """`docker/podman network create <network>` -- idempotent: an
    "already exists" failure is not an error here."""
    argv = [compose.runtime_binary(runtime), "network", "create", network]
    result = run(argv, capture_output=True, text=True, timeout=30)
    if result.returncode != 0 and "already exists" not in (result.stderr or "").lower():
        raise ProxyError(f"Failed to create network {network!r}: {(result.stderr or result.stdout).strip()}")


def attach_to_network(runtime: str, container_name: str, network: str, *, run: Runner = subprocess.run) -> None:
    """`docker/podman network connect <network> <container>` -- idempotent:
    a container already on that network is not an error here."""
    argv = [compose.runtime_binary(runtime), "network", "connect", network, container_name]
    result = run(argv, capture_output=True, text=True, timeout=30)
    stderr_low = (result.stderr or "").lower()
    if result.returncode != 0 and "already" not in stderr_low:
        raise ProxyError(
            f"Failed to attach {container_name!r} to network {network!r}: "
            f"{(result.stderr or result.stdout).strip()}"
        )


def resolve_shared_network(
    runtime: str, proxy: ProxyTarget, preferred: str, *, run: Runner = subprocess.run,
) -> str:
    """The Docker network both the proxy and the instance's container need
    to share for container-name resolution to work.

    If the proxy is already attached to a real (non-default) network,
    reuse it -- this is the common case for an operator's existing SWAG
    setup, and it means the CLI doesn't invent a second network alongside
    one that already works. Otherwise (a proxy on the builtin bridge
    network, a freshly `install_swag`-ed one, or a manually specified
    `--config-dir` with no known container) create `preferred` and attach
    the proxy to it too, so both sides end up on the same network.
    """
    if proxy.container_name:
        existing = [n for n in inspect_networks(runtime, proxy.container_name, run=run)
                    if n not in _BUILTIN_NETWORK_NAMES]
        if existing:
            return existing[0]
    ensure_network(runtime, preferred, run=run)
    if proxy.container_name:
        attach_to_network(runtime, proxy.container_name, preferred, run=run)
    return preferred


# ── Reloading the proxy ──────────────────────────────────────────────────


def reload_proxy(proxy: ProxyTarget, *, runtime: str, run: Runner = subprocess.run) -> None:
    if proxy.container_name:
        argv = [compose.runtime_binary(runtime), "exec", proxy.container_name, "nginx", "-s", "reload"]
    else:
        argv = ["nginx", "-s", "reload"]
    try:
        result = run(argv, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProxyError(f"Failed to reload the proxy: {exc}") from exc
    if result.returncode != 0:
        raise ProxyError(f"Proxy reload failed: {(result.stderr or result.stdout).strip()}")


# ── Installing SWAG (no proxy present) ───────────────────────────────────


def swag_root(data_root: Path | None = None) -> Path:
    """Where the CLI's own SWAG install lives -- sibling to per-instance
    directories under the same data root (ops/paths.py), but never
    registered as an instance itself; it isn't one."""
    return (data_root if data_root is not None else paths.default_data_root()) / PROXY_ROOT_DIRNAME


def render_swag_compose(
    *, network: str, timezone: str, url: str, validation: str,
    subdomains: str = "wildcard", duckdns_token: str = "", dnsplugin: str = "",
) -> str:
    """A minimal, standalone SWAG compose file.

    DNS/certificate validation is split across two prompts: this function
    just renders whatever `url`/`validation`/`subdomains` (plus the
    optional `duckdns_token`/`dnsplugin`) it's given, as SWAG's own
    required env vars. `provision_instance_proxy` (this prompt, C9) calls
    it with the instance's own hostname and a "http" placeholder validation
    when no real domain is in hand yet -- `url` can never be truly empty
    here, because SWAG's `init-require-url` service hangs forever
    (`sleep infinity`) rather than finishing boot without one, which would
    leave nginx's real config never generated from its `.sample`
    templates. `ops/dns.py`'s `_rewrite_and_recreate` calls
    this again with the real DuckDNS or Cloudflare values once the
    operator supplies them, then recreates the container so SWAG picks up
    the change. Network mode is still not considered configured without a
    working proxy *and* a real certificate in front, and
    a bare SWAG container with a placeholder domain is exactly that --
    present and serving, but not yet finished.
    """
    extra_env = ""
    if duckdns_token:
        extra_env += f'\n      DUCKDNSTOKEN: "{duckdns_token}"'
    if dnsplugin:
        extra_env += f'\n      DNSPLUGIN: "{dnsplugin}"'
    return f"""\
# Generated by `job-squire proxy` -- a standalone LinuxServer SWAG
# container (nginx + certbot + fail2ban), used when no reverse proxy was
# already running on this machine. Not a Job Squire instance; do not
# `job-squire remove` it.
#
# May contain a DNS provider token in plain text (DUCKDNSTOKEN/DNSPLUGIN
# credentials) once `job-squire dns duckdns`/`dns cloudflare`
# has run -- SWAG's own entrypoint scripts read these directly from the
# environment, the same way this repo's own data/.env carries SECRET_KEY
# in plain text. Kept out of the instance registry (never a secret store)
# and permissioned 0600 by the writer in ops/dns.py.
services:
  swag:
    image: {SWAG_IMAGE}
    container_name: {SWAG_CONTAINER_NAME}
    restart: unless-stopped
    cap_add:
      - NET_ADMIN
    environment:
      PUID: "1000"
      PGID: "1000"
      TZ: "{timezone}"
      URL: "{url}"
      VALIDATION: "{validation}"
      SUBDOMAINS: "{subdomains}"{extra_env}
    volumes:
      - ./config:/config
    ports:
      - "0.0.0.0:80:80"
      - "0.0.0.0:443:443"
    networks:
      - {network}

networks:
  {network}:
    external: true
"""


def _await_swag_ready(
    runtime: str, container_name: str, *, run: Runner, sleep: Sleep,
    timeout_seconds: float = 60.0, poll_interval: float = 2.0,
) -> bool:
    """Poll until SWAG's own first-boot init has populated
    `/config/nginx/proxy.conf`, the bundled snippet every generated
    subdomain conf `include`s.

    `compose_up` in `install_swag` only waits for the container to start,
    not for its entrypoint to finish -- SWAG's init (self-signed key
    generation, copying its default nginx templates into the bind-mounted
    `/config`) takes a few seconds on a fresh install, and calling
    `install_confs`/`reload_proxy` before it's done fails with `nginx:
    [emerg] open() ".../proxy.conf" failed (2: No such file or
    directory)` -- caught during this module's own end-to-end network-mode
    dry run. Attempt-counted rather than
    wall-clock-timed so this is deterministic and fast under test with an
    injected `sleep` that doesn't actually sleep, matching ops/dns.py's
    `_await_certificate`. Returns whether the marker file was found; never
    raises on a timeout, since `reload_proxy` right after this call will
    surface a clear error of its own if SWAG genuinely isn't ready.
    """
    attempts = max(1, int(timeout_seconds // poll_interval) + 1)
    argv = [compose.runtime_binary(runtime), "exec", container_name,
            "test", "-f", "/config/nginx/proxy.conf"]
    for attempt in range(attempts):
        try:
            result = run(argv, capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            result = None
        if result is not None and result.returncode == 0:
            return True
        if attempt < attempts - 1:
            sleep(poll_interval)
    return False


def install_swag(
    *, runtime: str, network: str = DEFAULT_PROXY_NETWORK, timezone: str = "Etc/UTC",
    url: str = "", validation: str = "http", data_root: Path | None = None,
    run: Runner = subprocess.run,
) -> ProxyTarget:
    """Stand up a LinuxServer SWAG container at `swag_root`, on `network`
    (created if it doesn't exist), and bring it up on the given runtime.
    Returns the ProxyTarget the caller then installs confs into."""
    root = swag_root(data_root)
    (root / "config").mkdir(parents=True, exist_ok=True)

    ensure_network(runtime, network, run=run)

    compose_path = root / "docker-compose.yml"
    compose_path.write_text(render_swag_compose(network=network, timezone=timezone, url=url, validation=validation))

    argv = [*compose.compose_binary(runtime), "--project-directory", str(root),
            "-f", str(compose_path), "-p", "job-squire-proxy", "up", "-d"]
    try:
        result = run(argv, cwd=str(root), capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProxyError(f"Failed to bring up SWAG: {exc}") from exc
    if result.returncode != 0:
        raise ProxyError(f"Failed to bring up SWAG: {(result.stderr or result.stdout).strip()}")

    return ProxyTarget(config_dir=root / "config", container_name=SWAG_CONTAINER_NAME, kind="swag")


# ── Orchestration ─────────────────────────────────────────────────────────


def provision_instance_proxy(
    instance: Instance,
    *,
    root: Path,
    proxy_container: str | None = None,
    config_dir: Path | None = None,
    network: str = DEFAULT_PROXY_NETWORK,
    install_if_missing: bool = True,
    swag_timezone: str = "Etc/UTC",
    swag_url: str = "",
    swag_validation: str = "http",
    data_root: Path | None = None,
    confirm: Confirm = lambda _msg: True,
    run: Runner = subprocess.run,
    sleep: Sleep = time.sleep,
) -> ProxyProvisionResult:
    """Provision a reverse proxy for a network-mode instance end to end:
    detect an existing proxy or install SWAG, attach the instance's
    container to the shared network, generate and install its web/MCP
    confs, and reload the proxy.

    `root` is the instance's own directory (`Path(instance.data_dir)` in
    the common case -- passed explicitly, same as ops/commands.py's own
    `_print_mcp_config`, rather than re-deriving it here, since an adopted
    instance's data_dir does not necessarily live under the default
    per-user data root).
    """
    if instance.mode != "network":
        raise ProxyError(
            f"Instance {instance.name!r} is in {instance.mode!r} mode -- reverse-proxy provisioning "
            f"only applies to network-mode instances (local modes use loopback only)."
        )

    container_name = derive_compose_project(instance.name)
    subdomain_web, subdomain_mcp = derive_subdomains(instance, root)
    mcp_port_internal = int(dotenv.get(paths.data_env_path(root), "MCP_PORT", "9000"))
    image = compose.read_image(root)

    if config_dir is not None:
        proxy = ProxyTarget(config_dir=config_dir, container_name=proxy_container, kind="manual")
    else:
        proxy = detect_existing_proxy(instance.runtime, run=run, container_hint=proxy_container)

    installed_swag = False
    if proxy is None:
        if not install_if_missing:
            raise ProxyError(
                "No existing reverse proxy was detected on this machine, and install_if_missing is False."
            )
        if not confirm(
            "No existing reverse proxy was detected. Install a LinuxServer SWAG container now?"
        ):
            raise ProxyError("No reverse proxy is available, and installing SWAG was declined.")
        # SWAG's own init-require-url service (`sleep infinity` if URL is
        # unset -- see _await_swag_ready's docstring) never finishes
        # booting on a truly empty URL, so nginx's real config is never
        # generated from its .sample templates and every later reload
        # fails forever, not just until DNS/TLS is configured (caught by
        # this module's own end-to-end network-mode dry run). The
        # instance's own hostname is already known at this point and is
        # the right value regardless -- `job-squire dns duckdns`/
        # `cloudflare` still owns getting a real certificate
        # for it; this only unblocks SWAG's init so there's an nginx to
        # reload in the meantime.
        resolved_swag_url = swag_url or (urlparse(instance.public_url).hostname or instance.name)
        proxy = install_swag(
            runtime=instance.runtime, network=network, timezone=swag_timezone,
            url=resolved_swag_url, validation=swag_validation, data_root=data_root, run=run,
        )
        installed_swag = True
        _await_swag_ready(instance.runtime, proxy.container_name, run=run, sleep=sleep)

    resolved_network = network
    if proxy.container_name:
        # Containerized proxy: share a Docker network with this instance's
        # container so nginx can resolve it by name (see
        # resolve_shared_network's own docstring for why this reuses the
        # proxy's existing network when it has one). A non-containerized
        # proxy has no Docker network to join at all, so this whole branch
        # is skipped for it -- install_confs falls back to a host-port
        # proxy_pass instead (see its own docstring).
        resolved_network = resolve_shared_network(instance.runtime, proxy, network, run=run)
        attach_to_network(instance.runtime, container_name, resolved_network, run=run)

        compose.write_compose_files(
            root, container_name=container_name, image=image, loopback_only=False,
            app_port=instance.app_port, mcp_port=instance.mcp_port, proxy_network=resolved_network,
        )
        up_result = compose.compose_up(
            instance.runtime, root, container_name, run=run, extra_args=["--force-recreate"],
        )
        if up_result.returncode != 0:
            raise ProxyError(
                f"Attached {instance.name!r} to network {resolved_network!r} but recreating the container "
                f"to pick it up failed: {(up_result.stderr or up_result.stdout).strip()}"
            )

    web_path, mcp_path = install_confs(
        proxy, instance_name=instance.name, subdomain_web=subdomain_web, subdomain_mcp=subdomain_mcp,
        container_name=container_name, app_port=instance.app_port or 0,
        mcp_port_host=instance.mcp_port or 0, mcp_port_internal=mcp_port_internal,
    )
    reload_proxy(proxy, runtime=instance.runtime, run=run)

    return ProxyProvisionResult(
        proxy=proxy, network=resolved_network, web_conf_path=web_path, mcp_conf_path=mcp_path,
        installed_swag=installed_swag,
    )
