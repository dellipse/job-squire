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
"""DNS and TLS provisioning for the CLI-installed SWAG proxy.

Three tiers:

  1. **DuckDNS (fully automated).** `configure_duckdns` collects the
     operator's DuckDNS subdomain and account token, rewrites the SWAG
     compose file into DuckDNS validation mode, recreates the container,
     and polls its logs for Let's Encrypt to report success. DuckDNS's own
     tradeoff carries straight through: SWAG can obtain the main subdomain
     via HTTP-01 (`VALIDATION=http`, port 80 must be reachable) or a
     wildcard via DNS-01 (`VALIDATION=duckdns`, no inbound port needed),
     never both from one SWAG config at once -- `wildcard=` picks which.
  2. **Cloudflare DNS-01 (semi-automated).** `configure_cloudflare` takes
     the domain and API token the operator already owns and brings with
     them, writes SWAG's `dns-conf/cloudflare.ini` credentials file,
     rewrites the compose file into Cloudflare DNS-01 mode
     (`DNSPLUGIN=cloudflare`), recreates the container, and polls for the
     wildcard certificate the same way. The domain and token are the one
     manual input; nothing else about the operator's Cloudflare account is
     touched.
  3. **Everything else is documented only.** Cloudflare Tunnel and the
     long tail of other SWAG DNS plugins (Route53, Google Domains, Porkbin,
     ...) are not wired to any function here -- see docs/job-squire-cli.md's
     "DNS and TLS provisioning" section. They use a different topology
     (Tunnel) or open-ended provider-specific credentials this module does
     not try to enumerate.

**Scope.** Both automated paths only ever touch the CLI's *own* SWAG
install -- the one `job-squire proxy` creates at `proxy.swag_root()` when
no existing proxy was found (`_managed_swag_target` refuses anything
else). If the operator's `job-squire proxy` run instead found and reused
an existing third-party SWAG or nginx, that proxy's DNS/TLS setup already
predates job-squire and is the operator's own to manage; this module does
not reach into it. Neither path can conjure a domain and working DNS: the
operator must already hold the DuckDNS subdomain (registered free at
duckdns.org) or
the Cloudflare-managed domain and API token before running either
command. This is a network-mode-only concern; a local install uses
loopback and needs none of it.
"""
from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import compose
from . import proxy as proxy_ops

Runner = proxy_ops.Runner
Sleep = Callable[[float], None]

DUCKDNS_SUFFIX = "duckdns.org"

# Certbot/SWAG's own log wording for a successful vs. failed ACME order.
# Matched loosely (case-insensitive substrings via regex) since the exact
# phrasing has shifted across SWAG releases; if LinuxServer ever changes it
# again, only these two tuples need updating -- everything else in this
# module works off the boolean/log-tail result, not the wording itself.
#
# The failure list was verified, not just guessed, against a real SWAG
# container's own log output for a rejected DuckDNS token (`docker logs`
# during this prompt's manual end-to-end check): SWAG's own
# `renew-cert.sh`/`update-cert.sh` wrapper prints "ERROR: Cert does not
# exist! ..." on any failed order regardless of which DNS/HTTP validation
# plugin was in play, which turned out to be the reliable, SWAG-version-
# stable marker -- raw certbot ACME wording like "challenge failed for
# domain" is HTTP-01-specific phrasing that a DNS-01 failure (a rejected
# DuckDNS/Cloudflare token, a TXT record that never propagates) never
# actually emits, so that guess alone would have silently timed out
# instead of surfacing the real reason. Both are kept: the SWAG-level
# marker as the primary, reliable signal, the certbot-level ones as a
# fallback for whatever specific ACME error text ends up in the log too.
_CERT_SUCCESS_PATTERNS = (
    re.compile(r"congratulations.*certificate and chain have been saved", re.IGNORECASE),
    re.compile(r"\bnew certificate deployed\b", re.IGNORECASE),
)
_CERT_FAILURE_PATTERNS = (
    re.compile(r"ERROR: Cert does not exist", re.IGNORECASE),
    re.compile(r"challenge failed for domain", re.IGNORECASE),
    re.compile(r"urn:ietf:params:acme:error", re.IGNORECASE),
    re.compile(r"some challenges have failed", re.IGNORECASE),
    re.compile(r"could not be set", re.IGNORECASE),  # a rejected DuckDNS/Cloudflare token's TXT-update failure
)


class DnsError(RuntimeError):
    """Raised for any DNS/TLS configuration or provisioning failure."""


@dataclass(frozen=True)
class CertResult:
    """The outcome of polling SWAG's logs for certificate issuance.

    `issued=False` with no exception means the timeout was hit without
    ever seeing a success *or* failure marker -- a live SWAG can easily
    still be mid-negotiation (DNS propagation, Let's Encrypt rate limits),
    so this is reported as "not yet", not treated as an error.
    """

    issued: bool
    log_tail: str


@dataclass(frozen=True)
class DnsProvisionResult:
    mode: str  # "duckdns-http" | "duckdns-wildcard" | "cloudflare-dns01"
    url: str
    subdomains: str
    proxy: proxy_ops.ProxyTarget
    cert: CertResult


# ── Scope guard: only the CLI's own SWAG ─────────────────────────────────


def _managed_swag_target(data_root: Path | None = None) -> proxy_ops.ProxyTarget:
    """The CLI-installed SWAG's ProxyTarget, or a clear failure if
    `job-squire proxy` was never run to install one (or an existing
    third-party proxy was reused instead -- see the module docstring's
    "Scope" note for why that case is deliberately out of bounds here)."""
    root = proxy_ops.swag_root(data_root)
    compose_path = root / "docker-compose.yml"
    if not compose_path.exists():
        raise DnsError(
            f"No CLI-installed SWAG container found at {root}. Run `job-squire proxy NAME` first so "
            "SWAG exists to configure DNS/TLS for. If that instance's proxy run instead detected and "
            "reused an existing SWAG or nginx you already had running, this command does not touch "
            "it -- configure DNS/TLS on it directly (see docs/job-squire-cli.md's DNS and TLS section)."
        )
    return proxy_ops.ProxyTarget(config_dir=root / "config", container_name=proxy_ops.SWAG_CONTAINER_NAME, kind="swag")


def _bare_label(hostname: str, suffix: str) -> str:
    """Strip a trailing `.<suffix>` if the operator typed the full
    hostname rather than just the label, e.g. both "castelo" and
    "castelo.duckdns.org" resolve to "castelo"."""
    hostname = hostname.strip().rstrip(".")
    dotted_suffix = f".{suffix}"
    if hostname.lower() == suffix.lower():
        return ""
    if hostname.lower().endswith(dotted_suffix.lower()):
        return hostname[: -len(dotted_suffix)]
    return hostname


# ── Rewriting and recreating the CLI's SWAG ──────────────────────────────


def _rewrite_and_recreate(
    *, runtime: str, network: str, timezone: str, url: str, validation: str,
    subdomains: str, duckdns_token: str = "", dnsplugin: str = "",
    data_root: Path | None = None, run: Runner,
) -> None:
    root = proxy_ops.swag_root(data_root)
    compose_path = root / "docker-compose.yml"
    compose_path.write_text(proxy_ops.render_swag_compose(
        network=network, timezone=timezone, url=url, validation=validation,
        subdomains=subdomains, duckdns_token=duckdns_token, dnsplugin=dnsplugin,
    ))
    try:
        compose_path.chmod(0o600)
    except OSError:
        pass  # best-effort; some filesystems (e.g. certain CI/CD bind mounts) don't support chmod

    argv = [*compose.compose_binary(runtime), "--project-directory", str(root),
            "-f", str(compose_path), "-p", "job-squire-proxy", "up", "-d", "--force-recreate"]
    try:
        result = run(argv, cwd=str(root), capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DnsError(f"Failed to recreate SWAG with the new DNS/TLS configuration: {exc}") from exc
    if result.returncode != 0:
        raise DnsError(f"Failed to recreate SWAG with the new DNS/TLS configuration: "
                        f"{(result.stderr or result.stdout).strip()}")


def _write_cloudflare_credentials(config_dir: Path, api_token: str) -> Path:
    """Certbot's Cloudflare DNS-01 plugin credentials file, at the
    conventional `dns-conf/cloudflare.ini` path SWAG's entrypoint expects
    when `DNSPLUGIN=cloudflare`. Scoped-API-token form (`dns_cloudflare_api_token`)
    rather than the legacy global-key form, matching Cloudflare's own current
    recommendation for a least-privilege token."""
    dns_conf_dir = config_dir / "dns-conf"
    dns_conf_dir.mkdir(parents=True, exist_ok=True)
    ini_path = dns_conf_dir / "cloudflare.ini"
    ini_path.write_text(
        "# Written by `job-squire dns cloudflare` (ops/dns.py) -- certbot's Cloudflare DNS-01 plugin\n"
        "# credentials. Re-written in place on every run; safe to delete if DNS/TLS is reconfigured.\n"
        f"dns_cloudflare_api_token = {api_token}\n"
    )
    try:
        ini_path.chmod(0o600)
    except OSError:
        pass
    return ini_path


# ── Polling for certificate issuance ─────────────────────────────────────


def _await_certificate(
    runtime: str, container_name: str | None, *, run: Runner, sleep: Sleep,
    timeout_seconds: float, poll_interval: float,
) -> CertResult:
    """Poll `container_logs` for a success or failure marker from
    certbot's ACME order, sleeping `poll_interval` seconds between
    attempts up to `timeout_seconds` total. Attempt-counted rather than
    wall-clock-timed so this is deterministic and fast under test with an
    injected `sleep` that doesn't actually sleep.

    Raises `DnsError` on an explicit failure marker (a wrong token or DNS
    record is something the operator should hear about immediately, not
    silently time out on); returns `CertResult(issued=False, ...)` if the
    timeout is hit with no marker either way, since a live SWAG can still
    be mid-negotiation (DNS propagation, Let's Encrypt rate limits).
    """
    if not container_name or timeout_seconds <= 0 or poll_interval <= 0:
        return CertResult(issued=False, log_tail="")

    attempts = max(1, int(timeout_seconds // poll_interval) + 1)
    log_tail = ""
    for attempt in range(attempts):
        log_tail = compose.container_logs(runtime, container_name, run=run, tail=200)
        if any(p.search(log_tail) for p in _CERT_SUCCESS_PATTERNS):
            return CertResult(issued=True, log_tail=log_tail)
        if any(p.search(log_tail) for p in _CERT_FAILURE_PATTERNS):
            raise DnsError(
                "SWAG reported a certificate validation failure -- double-check the DuckDNS/Cloudflare "
                f"subdomain, token, and DNS records. Recent SWAG log output:\n{log_tail[-2000:]}"
            )
        if attempt < attempts - 1:
            sleep(poll_interval)
    return CertResult(issued=False, log_tail=log_tail)


# ── Orchestration: DuckDNS (fully automated) ─────────────────────────────


def configure_duckdns(
    *, subdomain: str, token: str, wildcard: bool = True, runtime: str,
    network: str = proxy_ops.DEFAULT_PROXY_NETWORK, timezone: str = "Etc/UTC",
    data_root: Path | None = None, wait_for_cert: bool = True,
    timeout_seconds: float = 300.0, poll_interval: float = 10.0,
    run: Runner = subprocess.run, sleep: Sleep = time.sleep,
) -> DnsProvisionResult:
    if not subdomain or not token:
        raise DnsError("Both a DuckDNS subdomain and an account token are required.")

    proxy = _managed_swag_target(data_root)
    url = f"{_bare_label(subdomain, DUCKDNS_SUFFIX)}.{DUCKDNS_SUFFIX}"

    # DuckDNS's own tradeoff: the wildcard needs DNS-01
    # (SWAG's native `VALIDATION=duckdns` mode, which drives DuckDNS's own
    # TXT-record API -- no inbound port), while the main subdomain alone
    # can use ordinary HTTP-01 (port 80 must be reachable). Not both from
    # one SWAG config at once.
    if wildcard:
        validation, subdomains_val, mode = "duckdns", "wildcard", "duckdns-wildcard"
    else:
        validation, subdomains_val, mode = "http", "", "duckdns-http"

    _rewrite_and_recreate(
        runtime=runtime, network=network, timezone=timezone, url=url, validation=validation,
        subdomains=subdomains_val, duckdns_token=token, data_root=data_root, run=run,
    )
    cert = (
        _await_certificate(
            runtime, proxy.container_name, run=run, sleep=sleep,
            timeout_seconds=timeout_seconds, poll_interval=poll_interval,
        )
        if wait_for_cert else CertResult(issued=False, log_tail="")
    )
    return DnsProvisionResult(mode=mode, url=url, subdomains=subdomains_val, proxy=proxy, cert=cert)


# ── Orchestration: Cloudflare DNS-01 (semi-automated) ────────────────────


def configure_cloudflare(
    *, domain: str, api_token: str, runtime: str,
    network: str = proxy_ops.DEFAULT_PROXY_NETWORK, timezone: str = "Etc/UTC",
    data_root: Path | None = None, wait_for_cert: bool = True,
    timeout_seconds: float = 300.0, poll_interval: float = 10.0,
    run: Runner = subprocess.run, sleep: Sleep = time.sleep,
) -> DnsProvisionResult:
    if not domain or not api_token:
        raise DnsError("Both a domain and a Cloudflare API token are required -- the CLI cannot "
                        "conjure either; the operator must already own the domain on Cloudflare "
                        "and have created the token.")

    proxy = _managed_swag_target(data_root)
    _write_cloudflare_credentials(proxy.config_dir, api_token)
    _rewrite_and_recreate(
        runtime=runtime, network=network, timezone=timezone, url=domain, validation="dns",
        subdomains="wildcard", dnsplugin="cloudflare", data_root=data_root, run=run,
    )
    cert = (
        _await_certificate(
            runtime, proxy.container_name, run=run, sleep=sleep,
            timeout_seconds=timeout_seconds, poll_interval=poll_interval,
        )
        if wait_for_cert else CertResult(issued=False, log_tail="")
    )
    return DnsProvisionResult(mode="cloudflare-dns01", url=domain, subdomains="wildcard", proxy=proxy, cert=cert)
