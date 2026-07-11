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
"""The local static MCP bearer token: shape, storage, and the loopback-only
reachability rule.

OAuth 2.0/PKCE is the primary, untouched MCP auth flow everywhere. This
token is the sanctioned escape hatch for headless/non-browser clients that
can't complete OAuth's browser redirect (scripts, `jobsquire-cli`,
`mcp-remote` bridges). Per the settled spec (PLAN-deployment-modes.md
Section 8):

  * 256 bits of cryptographically random data, URL-safe base64, prefixed
    "jsq_mcp_" so it's recognizable in logs and by secret scanners.
  * Stored Fernet-encrypted at rest (AIConfig.mcp_api_key_enc), like every
    other secret -- never plaintext, never in any CLI-facing registry.
  * Compared in constant time on every request.
  * Scoped to the full MCP tool set for the instance's single user -- no
    per-tool subdivision, since each instance is single-tenant.
  * Exactly one token active at a time; rotating regenerates and
    immediately invalidates the previous value (both live in the same
    AIConfig.mcp_api_key_enc column, so generating a new one already does
    this structurally).
  * Loopback-only unless the operator explicitly opts in on a
    network-reachable instance. "Network-reachable" is the resolved
    DEPLOY_MODE from app/deploy.py (Prompt 4), not a raw guess -- see that
    module's docstring for why the app can't actually detect its own
    socket exposure and doesn't try to.
  * No forced expiry by default; an optional TTL is supported.
"""
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from .crypto import decrypt

TOKEN_PREFIX = "jsq_mcp_"
TOKEN_ENTROPY_BYTES = 32  # 256 bits


def generate_token():
    """Return a new bearer token: TOKEN_PREFIX + 256 bits of URL-safe base64."""
    return TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_ENTROPY_BYTES)


def expires_at_from_ttl_hours(ttl_hours, now=None):
    """Return an aware expiry datetime ttl_hours from now, or None for no TTL.

    ttl_hours may be None, an empty string, or a non-positive/non-numeric
    value, all of which mean "no expiry".
    """
    if ttl_hours in (None, ""):
        return None
    try:
        hours = float(ttl_hours)
    except (TypeError, ValueError):
        return None
    if hours <= 0:
        return None
    now = now or datetime.now(timezone.utc)
    return now + timedelta(hours=hours)


def is_network_reachable(deploy_mode):
    """True if the resolved DEPLOY_MODE (app/deploy.py) means this instance
    may be reachable over the network, rather than loopback-only."""
    return deploy_mode == "network"


def is_static_token_allowed(deploy_mode, allow_network):
    """The static token may be used on a loopback (local-mode) instance
    unconditionally, and on a network-reachable instance only if the
    operator has explicitly opted in (AIConfig.mcp_api_key_allow_network)."""
    return (not is_network_reachable(deploy_mode)) or bool(allow_network)


def verify_static_token(bearer, stored_encrypted, secret_key, deploy_mode,
                         allow_network, expires_at=None, now=None):
    """True iff bearer matches the stored token, the token hasn't expired,
    and the reachability rule permits using it on this instance.

    Every failure mode (no bearer, no stored token, mismatch, expired,
    disallowed on a network-reachable instance) returns False uniformly --
    callers should not distinguish "wrong token" from "not allowed here" in
    the response, so as not to leak which case applies to an unauthenticated
    caller.
    """
    if not bearer or not stored_encrypted:
        return False
    if not is_static_token_allowed(deploy_mode, allow_network):
        return False
    stored = decrypt(secret_key, stored_encrypted)
    if not stored:
        return False
    if not hmac.compare_digest(bearer, stored):
        return False
    if expires_at is not None:
        now = now or datetime.now(timezone.utc)
        # SQLite drops tzinfo on round-trip (this codebase's established
        # convention throughout models.py: write aware UTC, compare naive
        # UTC), so expires_at loaded from the DB is naive while a value
        # fresh out of expires_at_from_ttl_hours() is aware. Normalize both
        # to naive UTC before comparing so either source works.
        if expires_at.tzinfo is not None:
            expires_at = expires_at.replace(tzinfo=None)
        if now.tzinfo is not None:
            now = now.replace(tzinfo=None)
        if now > expires_at:
            return False
    return True
