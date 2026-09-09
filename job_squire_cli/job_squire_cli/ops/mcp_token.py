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
package can call into instead. So this module writes the `ai_config` row
with the exact same HKDF-SHA256 -> Fernet derivation as app/crypto.py (via
ops/crypto_mirror.py, shared with ops/secrets_copy.py) and the exact same
token shape as app/mcp_auth.py's generate_token() /
expires_at_from_ttl_hours() (mirrored, not imported, for the same
host/container dependency-boundary reason documented in
ops/secrets_copy.py's module docstring -- this package does not depend on
Flask/SQLAlchemy/the app package at all).

**The actual write no longer touches a host path.** Since the
2026-07-17 volume migration, `/data` is a named Docker volume, not a host
bind mount (see ops/compose.py) -- `<instance_root>/data/job-squire.db`
(what `paths.sqlite_db_path` used to point `_connect`'s `sqlite3.connect()`
at) has not existed on the host since that migration; that path only ever
holds `data/.env` now. Mirrors ops/backup.py's `_snapshot_container_data`
and ops/ollama_assist.py's `write_provider_config`: the actual read/write
runs inside the instance's own running container via
`docker exec`/`podman exec` (`app/mcp_token_cli.py`, fed a JSON request on
stdin), where `DATA_DIR` correctly resolves to the volume's mount point.
Token generation, TTL math, and the Fernet encryption of the token itself
stay here on the host -- only the raw row read/write crosses the exec
boundary, so `app/mcp_token_cli.py` never needs this instance's
SECRET_KEY at all.

Writing is safe with the instance's container still running, not just
tolerated as a fallback for a stopped one: app/mcp_server.py re-fetches
AIConfig fresh inside a new Flask app context on every MCP request (see
its asgi_app dispatcher), so a change lands on the very next call with no
restart needed -- exactly like the in-app Settings-page flow it mirrors.
`app/mcp_token_cli.py`'s connection sets a busy_timeout as the one
concession to touching a database the app might be writing to
concurrently, rather than bracketing every write in a compose stop/start
the way the heavier, multi-table `create --import-from` copy does in
ops/lifecycle.py. Since the container must now be running for exec to
reach it at all, every function here raises McpTokenError up front if it
isn't -- there is no more "stopped but still readable" case a host path
used to allow.
"""
from __future__ import annotations

import json
import secrets
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from . import compose

Runner = Callable[..., "subprocess.CompletedProcess[str]"]

# Must stay byte-for-byte identical to app/mcp_auth.py's TOKEN_PREFIX /
# TOKEN_ENTROPY_BYTES -- these values, not the code, are the compatibility
# contract app/mcp_auth.py's verify_static_token() checks against.
TOKEN_PREFIX = "jsq_mcp_"
TOKEN_ENTROPY_BYTES = 32  # 256 bits

# Matches SQLAlchemy's sqlite DATETIME storage format (what app/models.py's
# DateTime columns actually persist as via db.create_all()), so a timestamp
# written here reads back correctly through the app's own ORM later.
_DT_FORMAT = "%Y-%m-%d %H:%M:%S.%f"

_MCP_TOKEN_CLI_ARGV = ["python3", "-m", "app.mcp_token_cli"]


class McpTokenError(RuntimeError):
    """Raised for a missing/not-running instance container or a
    container-side sqlite failure."""


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


def _state_from_response(response: dict) -> TokenState:
    active = bool(response["active"])
    return TokenState(
        active=active,
        usable=active and not _is_expired(response["expires_at"]),
        created_at=response["created_at"],
        last_used_at=response["last_used_at"],
        expires_at=response["expires_at"],
        allow_network=bool(response["allow_network"]),
    )


def _exec(
    instance_root: Path, *, runtime: str, container_name: str, payload: dict, run: Runner,
) -> TokenState:
    """`docker/podman exec -i <container> python -m app.mcp_token_cli`, fed
    `payload` as JSON on stdin -- see app/mcp_token_cli.py's module
    docstring for the protocol. Mirrors ops/ollama_assist.py's
    `write_provider_config`: an up-front `inspect` to give a clear,
    actionable error rather than a raw non-zero exit from exec when the
    container isn't running.
    """
    state = compose.inspect_state(runtime, container_name, run=run)
    if state is None or state.get("Status") != "running":
        raise McpTokenError(
            f"The {container_name!r} container must be running to manage its MCP token -- its "
            f"database now lives in a Docker volume, only reachable while the container is up. "
            f"Start it first (`job-squire start`)."
        )
    argv = [compose.runtime_binary(runtime), "exec", "-i", container_name, *_MCP_TOKEN_CLI_ARGV]
    try:
        result = run(argv, input=json.dumps(payload), capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise McpTokenError(f"Failed to reach the MCP token store inside {container_name!r}: {exc}") from exc
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise McpTokenError(f"MCP token operation inside {container_name!r} failed: {stderr}")
    try:
        response = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise McpTokenError(
            f"MCP token operation inside {container_name!r} returned an unexpected response: "
            f"{result.stdout!r}"
        ) from exc
    return _state_from_response(response)


def read_state(
    instance_root: Path, *, runtime: str, container_name: str, run: Runner = subprocess.run,
) -> TokenState:
    return _exec(instance_root, runtime=runtime, container_name=container_name, payload={"op": "read_state"}, run=run)


def write_new_token(
    instance_root: Path, secret_key: str, *, runtime: str, container_name: str,
    ttl_hours: float | None = None, run: Runner = subprocess.run,
) -> str:
    """Generate a fresh token, store it Fernet-encrypted, and return the
    plaintext -- the caller shows it once, exactly like the app's own
    settings-page flash message never shows it again either.

    This *is* rotation as well as generation: the app only ever has one
    `mcp_api_key_enc` column, so overwriting it already invalidates
    whatever was there before (app/mcp_auth.py's module docstring makes
    the same point) -- there's no separate rotate code path to mirror.
    Encryption happens here, host-side, with `secret_key` (read from this
    instance's still-host-resident `data/.env` by the caller -- see
    ops/secrets_copy.py's `read_secret_key`) -- `app/mcp_token_cli.py`
    only ever receives the already-encrypted blob, never the key.
    """
    from .crypto_mirror import encrypt as _encrypt  # local import: only this function needs it

    token = generate_token()
    now = datetime.now(timezone.utc)
    expires_at = expires_at_from_ttl_hours(ttl_hours, now=now)
    payload = {
        "op": "write_token",
        "mcp_api_key_enc": _encrypt(secret_key, token),
        "created_at": _fmt(now),
        "expires_at": _fmt(expires_at) if expires_at else None,
    }
    _exec(instance_root, runtime=runtime, container_name=container_name, payload=payload, run=run)
    return token


def revoke(instance_root: Path, *, runtime: str, container_name: str, run: Runner = subprocess.run) -> None:
    _exec(instance_root, runtime=runtime, container_name=container_name, payload={"op": "revoke"}, run=run)


def set_allow_network(
    instance_root: Path, allow: bool, *, runtime: str, container_name: str, run: Runner = subprocess.run,
) -> None:
    _exec(
        instance_root, runtime=runtime, container_name=container_name,
        payload={"op": "set_allow_network", "allow": allow}, run=run,
    )
