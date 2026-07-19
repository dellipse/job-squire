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
"""CLI-side management of the `jsq_mcp_` local static MCP bearer token.

The app's own generate/rotate/revoke logic (app/mcp_auth.py's
generate_token()/expires_at_from_ttl_hours(), and app/main.py's
settings_mcp_api_key() route, which writes AIConfig.mcp_api_key_enc and
its lifecycle-metadata columns) is reachable *only* from an authenticated,
CSRF-protected browser session against the running app's Settings page --
there is no Flask CLI command, admin API route, or management script this
package can call into instead. So, following the exact precedent
ops/secrets_copy.py already established for the app's other Fernet-
encrypted columns, this module writes the `ai_config` row directly with
the stdlib sqlite3 module: the same HKDF-SHA256 -> Fernet derivation as
app/crypto.py (via ops/crypto_mirror.py, shared with secrets_copy.py), and
the exact same token shape as app/mcp_auth.py's generate_token() /
expires_at_from_ttl_hours() (mirrored, not imported, for the same
host/container dependency-boundary reason documented in
ops/secrets_copy.py's module docstring -- this package does not depend on
Flask/SQLAlchemy/the app package at all).

Writing here is safe with the instance's container still running, not
just tolerated as a fallback for a stopped one: app/mcp_server.py
re-fetches AIConfig fresh inside a new Flask app context on every MCP
request (see its asgi_app dispatcher), so a change lands on the very next
call with no restart needed -- exactly like the in-app Settings-page flow
it mirrors. `_connect` sets a busy_timeout as the one concession to
touching a database the app might be writing to concurrently, rather than
bracketing every write in a compose stop/start the way the heavier,
multi-table `create --import-from` copy does in ops/lifecycle.py.
"""
from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import paths
from .crypto_mirror import encrypt as _encrypt

# Must stay byte-for-byte identical to app/mcp_auth.py's TOKEN_PREFIX /
# TOKEN_ENTROPY_BYTES -- these values, not the code, are the compatibility
# contract app/mcp_auth.py's verify_static_token() checks against.
TOKEN_PREFIX = "jsq_mcp_"
TOKEN_ENTROPY_BYTES = 32  # 256 bits

# Matches SQLAlchemy's sqlite DATETIME storage format (what app/models.py's
# DateTime columns actually persist as via db.create_all()), so a timestamp
# written here reads back correctly through the app's own ORM later.
_DT_FORMAT = "%Y-%m-%d %H:%M:%S.%f"


class McpTokenError(RuntimeError):
    """Raised for a missing instance database or a sqlite-level failure."""


def generate_token() -> str:
    """A new bearer token: TOKEN_PREFIX + 256 bits of URL-safe base64 --
    byte-for-byte the same shape as app/mcp_auth.py's generate_token()."""
    return TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_ENTROPY_BYTES)


def expires_at_from_ttl_hours(ttl_hours: float | None, now: datetime | None = None) -> datetime | None:
    """Mirrors app/mcp_auth.py's function of the same name: None/zero/
    negative all mean "no expiry"."""
    if ttl_hours is None or ttl_hours <= 0:
        return None
    return (now or datetime.now(timezone.utc)) + timedelta(hours=ttl_hours)


def is_static_token_allowed(mode: str, allow_network: bool) -> bool:
    """Mirrors app/mcp_auth.py's is_static_token_allowed(): usable
    unconditionally on a loopback (local-mode) instance, and on a
    network-reachable one only with the explicit opt-in.

    Takes the registry's `Instance.mode` in place of the app's resolved
    DEPLOY_MODE -- lifecycle.create_instance writes DEPLOY_MODE=mode
    verbatim into the instance's data/.env (ops/compose.py's
    render_data_env), so the two are the same value by construction, which
    is what matches this check to the resolved deployment posture from the
    app set, rather than guessing at it independently.
    """
    return mode != "network" or bool(allow_network)


@dataclass(frozen=True)
class TokenState:
    active: bool  # a token is stored, regardless of whether its TTL has passed
    usable: bool  # active AND not expired -- what "has an active MCP token" means
    created_at: str | None
    last_used_at: str | None
    expires_at: str | None
    allow_network: bool


# Full AIConfig column defaults (app/models.py), hand-maintained the same
# way ops/secrets_copy.py hand-maintains its column allowlists, and for the
# same reason: this package has no SQLAlchemy model to introspect. Used
# only to seed a brand new row when one doesn't exist yet -- db.create_all()
# does not emit SQL-level DEFAULTs for plain Column(default=...) fields
# (only server_default= would), so a bare `INSERT INTO ai_config (id)`
# would otherwise leave every other AI setting NULL instead of matching
# what the app's own _singleton() helper (app/main.py) would have created
# the first time anyone visited the Settings page.
_FRESH_ROW_DEFAULTS: dict[str, object] = {
    "mode": "manual",
    "api_enabled": 0,
    "mcp_enabled": 0,
    "claude_buttons_enabled": 0,
    "api_key_enc": "",
    "model": "claude-sonnet-4-6",
    "mcp_token_enc": "",
    "mcp_api_key_enc": "",
    "connector_name": "job-squire",
    "thinking_mode": "disabled",
    "auto_triage_enabled": 0,
    "triage_model": "claude-haiku-4-5",
    "auto_followup_enabled": 0,
    "auto_weekly_review_enabled": 0,
    "rejection_alert_threshold": 5,
    "fallback_to_anthropic": 1,
    "mcp_api_key_allow_network": 0,
}


def _fmt(dt: datetime) -> str:
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime(_DT_FORMAT)


def _is_expired(expires_at: str | None, now: datetime | None = None) -> bool:
    if not expires_at:
        return False
    try:
        parsed = datetime.strptime(expires_at, _DT_FORMAT)
    except ValueError:
        return False
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is not None:
        now = now.astimezone(timezone.utc).replace(tzinfo=None)
    return now > parsed


def _connect(instance_root: Path) -> sqlite3.Connection:
    db_path = paths.sqlite_db_path(instance_root)
    if not db_path.exists():
        raise McpTokenError(
            f"No database found at {db_path} -- this instance hasn't booted yet. "
            f"Run `job-squire start <name>` (or `create`) first."
        )
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")  # tolerate a brief lock from the running app
    return conn


def _ensure_row(conn: sqlite3.Connection) -> None:
    try:
        exists = conn.execute("SELECT 1 FROM ai_config WHERE id = 1").fetchone() is not None
    except sqlite3.OperationalError as exc:
        raise McpTokenError(f"ai_config table not found in this instance's database: {exc}") from exc
    if exists:
        return
    columns = ["id", *_FRESH_ROW_DEFAULTS.keys()]
    placeholders = ", ".join(["?"] * len(columns))
    values = [1, *_FRESH_ROW_DEFAULTS.values()]
    conn.execute(f"INSERT INTO ai_config ({', '.join(columns)}) VALUES ({placeholders})", values)  # noqa: S608


def read_state(instance_root: Path) -> TokenState:
    conn = _connect(instance_root)
    try:
        _ensure_row(conn)
        conn.commit()
        row = conn.execute(
            "SELECT mcp_api_key_enc, mcp_api_key_created_at, mcp_api_key_last_used_at, "
            "mcp_api_key_expires_at, mcp_api_key_allow_network FROM ai_config WHERE id = 1"
        ).fetchone()
    finally:
        conn.close()
    active = bool(row["mcp_api_key_enc"])
    return TokenState(
        active=active,
        usable=active and not _is_expired(row["mcp_api_key_expires_at"]),
        created_at=row["mcp_api_key_created_at"],
        last_used_at=row["mcp_api_key_last_used_at"],
        expires_at=row["mcp_api_key_expires_at"],
        allow_network=bool(row["mcp_api_key_allow_network"]),
    )


def write_new_token(instance_root: Path, secret_key: str, *, ttl_hours: float | None = None) -> str:
    """Generate a fresh token, store it Fernet-encrypted, and return the
    plaintext -- the caller shows it once, exactly like the app's own
    settings-page flash message never shows it again either.

    This *is* rotation as well as generation: the app only ever has one
    `mcp_api_key_enc` column, so overwriting it already invalidates
    whatever was there before (app/mcp_auth.py's module docstring makes
    the same point) -- there's no separate rotate code path to mirror.
    """
    token = generate_token()
    now = datetime.now(timezone.utc)
    expires_at = expires_at_from_ttl_hours(ttl_hours, now=now)
    conn = _connect(instance_root)
    try:
        _ensure_row(conn)
        conn.execute(
            "UPDATE ai_config SET mcp_api_key_enc = ?, mcp_api_key_created_at = ?, "
            "mcp_api_key_last_used_at = NULL, mcp_api_key_expires_at = ? WHERE id = 1",
            (_encrypt(secret_key, token), _fmt(now), _fmt(expires_at) if expires_at else None),
        )
        conn.commit()
    finally:
        conn.close()
    return token


def revoke(instance_root: Path) -> None:
    conn = _connect(instance_root)
    try:
        _ensure_row(conn)
        conn.execute(
            "UPDATE ai_config SET mcp_api_key_enc = '', mcp_api_key_created_at = NULL, "
            "mcp_api_key_last_used_at = NULL, mcp_api_key_expires_at = NULL WHERE id = 1"
        )
        conn.commit()
    finally:
        conn.close()


def set_allow_network(instance_root: Path, allow: bool) -> None:
    conn = _connect(instance_root)
    try:
        _ensure_row(conn)
        conn.execute("UPDATE ai_config SET mcp_api_key_allow_network = ? WHERE id = 1", (1 if allow else 0,))
        conn.commit()
    finally:
        conn.close()
