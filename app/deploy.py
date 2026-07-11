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
"""DEPLOY_MODE preset resolution.

DEPLOY_MODE is a convenience preset over a small set of granular flags, not
a thing the running app branches on directly -- create_app() reads only the
resolved flags (trust_proxy, secure_cookie), never the mode string itself.
That is the one rule that matters here: it's what keeps this from becoming
another scattered-conditional AIConfig.mode enum.

The two genuinely new environment variables are DEPLOY_MODE and
TRUST_PROXY. SESSION_COOKIE_SECURE already existed; this only gives it a
mode-aware default.

Precedence, per flag: an explicitly set environment variable always wins.
If unset, the preset default for the resolved DEPLOY_MODE fills it in.
DEPLOY_MODE itself is consulted only to pick which preset table applies.

This module also runs the startup safety guard (evaluate_startup_guard /
enforce_startup_guard) that turns two unsafe DEPLOY_MODE/PUBLIC_URL/
TRUST_PROXY combinations into loud, early signals rather than silent
problems. Note what the guard can and can't see: the app has no way to
observe the actual host-level network exposure of its own container (Docker
always binds 0.0.0.0 internally regardless of whether the host publishes
that to loopback or to the world), so "bound to a non-loopback interface"
is approximated by a self-consistency check against PUBLIC_URL -- an
operator-declared value already read elsewhere in the app -- rather than by
inspecting a socket that can't actually be inspected from in here.
"""
import logging
import os
import sys
from ipaddress import ip_address
from urllib.parse import urlsplit

from werkzeug.middleware.proxy_fix import ProxyFix

log = logging.getLogger(__name__)

DEFAULT_MODE = "local"

# local:   loopback is the trust boundary. No proxy in front, so forwarded
#          headers must not be trusted, and plain HTTP means secure cookies
#          would be silently dropped by the browser.
# network: an external reverse proxy is the trust boundary. It terminates
#          TLS and sets X-Forwarded-*, so those headers are trusted and
#          cookies require HTTPS.
_PRESETS = {
    "local": {"trust_proxy": False, "secure_cookie": False},
    "network": {"trust_proxy": True, "secure_cookie": True},
}


def _bool_env(name, default):
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def resolve_deploy_mode():
    """Return the effective DEPLOY_MODE, defaulting to and falling back to 'local'."""
    mode = os.environ.get("DEPLOY_MODE", DEFAULT_MODE).strip().lower()
    return mode if mode in _PRESETS else DEFAULT_MODE


def resolve_deploy_flags():
    """Resolve DEPLOY_MODE into the granular flags the app actually reads.

    Returns {"mode": str, "trust_proxy": bool, "secure_cookie": bool}.
    """
    mode = resolve_deploy_mode()
    preset = _PRESETS[mode]
    return {
        "mode": mode,
        "trust_proxy": _bool_env("TRUST_PROXY", preset["trust_proxy"]),
        "secure_cookie": _bool_env("SESSION_COOKIE_SECURE", preset["secure_cookie"]),
    }


def apply_proxy_trust(app, trust_proxy):
    """Wrap app.wsgi_app in ProxyFix iff trust_proxy is true.

    Applying ProxyFix unconditionally would let anything that can reach the
    app directly (e.g. another local process, or a request that reaches an
    untrusted network instance without going through the real proxy) forge
    X-Forwarded-* headers and spoof scheme, host, or client IP.
    """
    if trust_proxy:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)
    return app


# --- Startup safety guard --------------------------------------------------

def _is_loopback_url(url):
    """True if url's host is a loopback address, or the url is empty/unparseable.

    An empty/missing PUBLIC_URL is treated as loopback (nothing to flag) --
    it's optional, and its absence carries no evidence of exposure either way.
    """
    host = (urlsplit(url).hostname or "").strip().lower()
    if not host:
        return True
    if host == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False  # a real hostname, not "localhost" or a loopback IP


def format_issue(issue):
    """Render one guard issue as a single line naming the variable, its
    value, why it's unsafe, and the fix -- the one place this wording is
    generated, so the log, stderr, and in-app banner never drift apart."""
    return (
        f"{issue['variable']}={issue['value']!r} is unsafe for DEPLOY_MODE={issue['mode']!r}: "
        f"{issue['reason']} Fix: {issue['fix']}"
    )


def evaluate_startup_guard(deploy_flags):
    """Check the resolved deploy flags (plus PUBLIC_URL) for the two unsafe
    combinations the design calls out, and return a list of issue dicts:
    {"severity": "fatal"|"warning", "mode", "variable", "value", "reason", "fix"}.

    Fatal (network mode only): PUBLIC_URL isn't HTTPS, or trust_proxy
    resolved off (only possible if TRUST_PROXY was explicitly overridden,
    since the network preset defaults it on).

    Warning (local mode only): PUBLIC_URL is set to a non-loopback host,
    contradicting local mode's loopback-only assumption -- see the module
    docstring for why this, rather than a socket check, is what's used.
    """
    mode = deploy_flags["mode"]
    public_url = os.environ.get("PUBLIC_URL", "").strip()
    issues = []

    if mode == "network":
        if not public_url.lower().startswith("https://"):
            issues.append({
                "severity": "fatal",
                "mode": mode,
                "variable": "PUBLIC_URL",
                "value": public_url or "(unset)",
                "reason": (
                    "network mode assumes an external reverse proxy terminates TLS in "
                    "front of this instance, so PUBLIC_URL must be an https:// URL. "
                    "Running network mode over plain HTTP exposes session cookies and "
                    "credentials to anyone on the network path."
                ),
                "fix": (
                    "Set PUBLIC_URL=https://<your-domain>, or set DEPLOY_MODE=local if "
                    "this instance is not actually reachable from the network."
                ),
            })
        if not deploy_flags["trust_proxy"]:
            issues.append({
                "severity": "fatal",
                "mode": mode,
                "variable": "TRUST_PROXY",
                "value": os.environ.get("TRUST_PROXY", "(unset)"),
                "reason": (
                    "network mode expects the app to trust one hop of X-Forwarded-* "
                    "headers from the reverse proxy in front of it. With trust_proxy "
                    "off, client IPs (rate limiting) and the request scheme (secure "
                    "redirects) are read from the proxy's own connection instead of "
                    "the real client's, defeating the point of the proxy."
                ),
                "fix": "Remove the TRUST_PROXY override to accept the network-mode default (on).",
            })
    elif mode == "local" and public_url and not _is_loopback_url(public_url):
        issues.append({
            "severity": "warning",
            "mode": mode,
            "variable": "PUBLIC_URL",
            "value": public_url,
            "reason": (
                "local mode assumes this instance is reached only via a loopback "
                "address (localhost/127.0.0.1) with no reverse proxy in front, and "
                "runs plain HTTP with secure cookies off on that assumption. A "
                "non-loopback PUBLIC_URL contradicts that -- if this instance really "
                "is reachable at that address, it is being served insecurely."
            ),
            "fix": (
                "Set DEPLOY_MODE=network (with a TLS-terminating reverse proxy actually "
                "in front of the app) if this instance is meant to be reached at that "
                "address, or change PUBLIC_URL to a loopback URL (or unset it) if this "
                "instance really is local-only."
            ),
        })

    return issues


def enforce_startup_guard(deploy_flags):
    """Run the guard, print/log fatal issues and exit(1) if any exist, and
    print/log warning issues. Returns the formatted warning messages (for
    the in-app banner) -- an empty list means nothing to show.

    Fatal messages are written with an explicit, unambiguous shape (a
    "FATAL:"-prefixed line on stderr, one per issue) rather than left to an
    uncaught-exception traceback, so a wrapping process -- namely the future
    job-squire CLI -- can catch the non-zero exit and reprint the exact same
    reason and fix rather than a generic "container exited" message.
    """
    issues = evaluate_startup_guard(deploy_flags)
    fatal = [i for i in issues if i["severity"] == "fatal"]
    warnings = [i for i in issues if i["severity"] == "warning"]

    for issue in fatal:
        text = format_issue(issue)
        log.error(text)
        print(f"FATAL: {text}", file=sys.stderr)
    if fatal:
        sys.exit(1)

    warning_messages = [format_issue(issue) for issue in warnings]
    for text in warning_messages:
        log.warning(text)
        print(f"WARNING: {text}", file=sys.stderr)

    return warning_messages
