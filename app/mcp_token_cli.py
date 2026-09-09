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
"""Container-side entrypoint for `job-squire configure NAME --mcp-token ...`
(job_squire_cli's ops/mcp_token.py).

Same reasoning as `app/backup_cli.py` and `app/ollama_provider_cli.py`:
since /data is a named Docker volume rather than a host bind mount,
job_squire_cli cannot read or write the `ai_config` row by opening
`<instance_root>/data/job-squire.db` directly from the host anymore --
that path only ever held the live database back when /data was still
bind-mounted (see ops/compose.py and ops/paths.py). This module runs the
handful of statements ops/mcp_token.py needs inside the running container
instead -- `docker exec <container> python -m app.mcp_token_cli` (or the
`podman` equivalent), fed a JSON request on stdin -- where
`os.environ["DATA_DIR"]` (default `/data`) correctly resolves to the
volume's mount point.

Token generation, TTL math, and the Fernet encryption of the token itself
all stay on the host side in ops/mcp_token.py (it already has the
instance's SECRET_KEY, read straight from the still-host-resident
`data/.env` -- see ops/secrets_copy.py's `read_secret_key`) -- this module
only ever sees an already-encrypted blob to store, or an encrypted blob's
metadata to report back. It never imports ops/crypto_mirror.py or
anything else from job_squire_cli, matching every other container-side
one-shot's dependency boundary.

Deliberately not a Flask route, same as backup_cli.py/ollama_provider_cli.py:
this only ever runs as a one-shot process inside a container the CLI
already controls via exec, so there's no need for the app factory, a
request context, or authentication.

Protocol: reads one JSON object from stdin --

    {"op": "read_state" | "write_token" | "revoke" | "set_allow_network",
     ...op-specific fields}

  - "read_state": no extra fields.
  - "write_token": {"mcp_api_key_enc": str, "created_at": str, "expires_at": str | null}
        -- mcp_api_key_enc is already Fernet-encrypted (host-side);
           created_at/expires_at are already formatted to match
           SQLAlchemy's sqlite DATETIME storage (ops/mcp_token.py's
           `_DT_FORMAT`).
  - "revoke": no extra fields.
  - "set_allow_network": {"allow": bool}

-- writes one JSON object to stdout on success --

    {"active": bool, "created_at": str | null, "last_used_at": str | null,
     "expires_at": str | null, "allow_network": bool}

(the row's post-operation state, in every case -- ops/mcp_token.py already
knows how to turn this into a TokenState) -- or a plain-text error to
stderr and a nonzero exit on failure. Uses raw sqlite3 directly against
`<DATA_DIR>/job-squire.db`, the same way ops/mcp_token.py's now-removed
`_connect`/`_ensure_row` used to, rather than importing the app
factory/SQLAlchemy -- this only ever touches one row via a handful of
statements, and the app package's own migrations already guarantee the
schema this expects exists.
"""
import json
import os
import sqlite3
import sys

# Full AIConfig column defaults (app/models.py), hand-maintained the same
# way ops/mcp_token.py's own copy of this table used to be (and the way
# ops/secrets_copy.py hand-maintains its column allowlists) -- this module
# has no SQLAlchemy model to introspect. Used only to seed a brand new row
# when one doesn't exist yet -- db.create_all() does not emit SQL-level
# DEFAULTs for plain Column(default=...) fields (only server_default=
# would), so a bare `INSERT INTO ai_config (id)` would otherwise leave
# every other AI setting NULL instead of matching what the app's own
# _singleton() helper (app/main.py) would have created the first time
# anyone visited the Settings page. Must stay in lockstep with
# ops/mcp_token.py's own tests, which exercise the equivalent logic
# against a fake to keep this container-side copy honest.
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


class McpTokenCliError(RuntimeError):
    """Raised for a malformed request or a sqlite-level failure."""


def _connect(db_path: str) -> sqlite3.Connection:
    if not os.path.exists(db_path):
        raise McpTokenCliError(
            f"Database not found at {db_path} inside the container. This shouldn't happen if the "
            f"container is up -- the app creates its schema on first boot."
        )
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")  # tolerate a brief lock from the running app
    return conn


def _ensure_row(conn: sqlite3.Connection) -> None:
    try:
        exists = conn.execute("SELECT 1 FROM ai_config WHERE id = 1").fetchone() is not None
    except sqlite3.OperationalError as exc:
        raise McpTokenCliError(f"ai_config table not found in this instance's database: {exc}") from exc
    if exists:
        return
    columns = ["id", *_FRESH_ROW_DEFAULTS.keys()]
    placeholders = ", ".join(["?"] * len(columns))
    values = [1, *_FRESH_ROW_DEFAULTS.values()]
    conn.execute(f"INSERT INTO ai_config ({', '.join(columns)}) VALUES ({placeholders})", values)  # noqa: S608


def _read_row_state(conn: sqlite3.Connection) -> dict:
    """Only ever returns metadata *about* the token (whether one is set,
    its timestamps, the network opt-in) -- never the token's own
    ciphertext. The three timestamp columns are aliased to their bare
    names below (`created_at` rather than `mcp_api_key_created_at`, etc.)
    so the response dict this builds -- which does get printed to stdout
    as the operation's result -- never carries a Python value read out
    from a column whose *name* pattern-matches a credential (CodeQL's
    py/clear-text-logging-sensitive-data flagged these three specifically;
    they're plain DATETIME strings, not secret material, but the shared
    `mcp_api_key_` column prefix is what a naive name-based heuristic
    keys off, so the alias is the actual fix rather than a suppression)."""
    row = conn.execute(
        "SELECT mcp_api_key_enc, mcp_api_key_created_at AS created_at, "
        "mcp_api_key_last_used_at AS last_used_at, mcp_api_key_expires_at AS expires_at, "
        "mcp_api_key_allow_network AS allow_network FROM ai_config WHERE id = 1"
    ).fetchone()
    return {
        "active": bool(row["mcp_api_key_enc"]),
        "created_at": row["created_at"],
        "last_used_at": row["last_used_at"],
        "expires_at": row["expires_at"],
        "allow_network": bool(row["allow_network"]),
    }


def handle_request(db_path: str, payload: dict) -> dict:
    op = payload.get("op")
    conn = _connect(db_path)
    try:
        _ensure_row(conn)

        if op == "read_state":
            pass  # nothing further to do -- state read below

        elif op == "write_token":
            conn.execute(
                "UPDATE ai_config SET mcp_api_key_enc = ?, mcp_api_key_created_at = ?, "
                "mcp_api_key_last_used_at = NULL, mcp_api_key_expires_at = ? WHERE id = 1",
                (payload["mcp_api_key_enc"], payload["created_at"], payload.get("expires_at")),
            )

        elif op == "revoke":
            conn.execute(
                "UPDATE ai_config SET mcp_api_key_enc = '', mcp_api_key_created_at = NULL, "
                "mcp_api_key_last_used_at = NULL, mcp_api_key_expires_at = NULL WHERE id = 1"
            )

        elif op == "set_allow_network":
            conn.execute(
                "UPDATE ai_config SET mcp_api_key_allow_network = ? WHERE id = 1",
                (1 if payload["allow"] else 0,),
            )

        else:
            raise McpTokenCliError(f"Unknown op: {op!r}")

        conn.commit()
        return _read_row_state(conn)
    finally:
        conn.close()


def main() -> int:
    data_dir = os.environ.get("DATA_DIR", "/data")
    db_path = os.path.join(data_dir, "job-squire.db")
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        print(f"Malformed request on stdin: {exc}", file=sys.stderr)
        return 1
    try:
        response = handle_request(db_path, payload)
    except McpTokenCliError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 -- surface any failure to the CLI's stderr, not a traceback dump
        print(f"MCP token operation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(response))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
