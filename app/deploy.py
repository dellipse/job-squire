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
"""
import os

from werkzeug.middleware.proxy_fix import ProxyFix

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
