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
"""ops/mcp_token.py -- the jsq_mcp_ static token, written via `docker/podman
exec` into an instance's own running container (CLI-01/TEST-03).

`/data` is a named Docker volume, not a host bind mount (ops/compose.py),
so ops/mcp_token.py no longer opens `<instance_root>/data/job-squire.db`
directly -- it execs `app/mcp_token_cli.py` inside the instance's
container instead. This package never imports the app package (see
ops/secrets_copy.py's module docstring), so `container_fake_run` below
duplicates just enough of app/mcp_token_cli.py's logic against a real
sqlite file standing in for the container's volume -- the same convention
test_ollama_assist.py's `container_fake_run` already established for
ops/ollama_assist.py's own exec-based write, and test_lifecycle.py's
FakeRuntime uses for backup/restore. Builds its own minimal ai_config
table (mirroring tests/test_secrets_copy.py's _SCHEMA) rather than
importing it, since some tests here deliberately start from a table with
*no* id=1 row -- the "never visited the Settings page" case
app/mcp_token_cli.py's `_ensure_row` must handle.
"""
import json
import sqlite3
from types import SimpleNamespace

import pytest

from job_squire_cli.ops import mcp_token as mt
from job_squire_cli.ops import paths
from job_squire_cli.ops.crypto_mirror import decrypt

_AI_CONFIG_SCHEMA = """
CREATE TABLE ai_config (
    id INTEGER PRIMARY KEY, mode TEXT, api_enabled BOOLEAN, mcp_enabled BOOLEAN,
    claude_buttons_enabled BOOLEAN, api_key_enc TEXT, model TEXT, mcp_token_enc TEXT,
    mcp_api_key_enc TEXT, mcp_api_key_created_at DATETIME, mcp_api_key_last_used_at DATETIME,
    mcp_api_key_expires_at DATETIME, mcp_api_key_allow_network BOOLEAN, connector_name TEXT,
    thinking_mode TEXT, auto_triage_enabled BOOLEAN, triage_model TEXT,
    auto_followup_enabled BOOLEAN, auto_weekly_review_enabled BOOLEAN,
    rejection_alert_threshold INTEGER, fallback_to_anthropic BOOLEAN
);
"""

_CONTAINER_NAME = "job-squire-castelo"

# Duplicated from app/mcp_token_cli.py's own copy (see that module's
# docstring) -- kept in lockstep by the same shared behavioral tests over
# ops/mcp_token.py's public functions that would fail if the two drifted.
_FRESH_ROW_DEFAULTS = {
    "mode": "manual", "api_enabled": 0, "mcp_enabled": 0, "claude_buttons_enabled": 0,
    "api_key_enc": "", "model": "claude-sonnet-4-6", "mcp_token_enc": "", "mcp_api_key_enc": "",
    "connector_name": "job-squire", "thinking_mode": "disabled", "auto_triage_enabled": 0,
    "triage_model": "claude-haiku-4-5", "auto_followup_enabled": 0, "auto_weekly_review_enabled": 0,
    "rejection_alert_threshold": 5, "fallback_to_anthropic": 1, "mcp_api_key_allow_network": 0,
}


def _make_db(root, *, with_row: bool) -> None:
    """Stands in for "what the container's named volume contains" -- see
    test_lifecycle.py's FakeRuntime docstring for the same convention.
    Production code never reads this path anymore; it's only convenient
    disk space for the fake `exec` handler below to operate on."""
    db_path = paths.sqlite_db_path(root)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_AI_CONFIG_SCHEMA)
    if with_row:
        conn.execute(
            "INSERT INTO ai_config (id, mode, api_enabled, mcp_api_key_enc, mcp_api_key_allow_network) "
            "VALUES (1, 'manual', 0, '', 0)"
        )
    conn.commit()
    conn.close()


def _read_row(root):
    conn = sqlite3.connect(str(paths.sqlite_db_path(root)))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM ai_config WHERE id = 1").fetchone()
    conn.close()
    return row


def _handle_request_like_container_cli(db_path, payload):
    """Reimplements app/mcp_token_cli.py::handle_request against `db_path`,
    standing in for the real exec call."""
    import os

    if not os.path.exists(db_path):
        raise mt.McpTokenError(f"Database not found at {db_path} inside the container.")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        exists = conn.execute("SELECT 1 FROM ai_config WHERE id = 1").fetchone() is not None
        if not exists:
            columns = ["id", *_FRESH_ROW_DEFAULTS.keys()]
            placeholders = ", ".join(["?"] * len(columns))
            conn.execute(
                f"INSERT INTO ai_config ({', '.join(columns)}) VALUES ({placeholders})",  # noqa: S608
                [1, *_FRESH_ROW_DEFAULTS.values()],
            )

        op = payload.get("op")
        if op == "read_state":
            pass
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
            raise mt.McpTokenError(f"Unknown op: {op!r}")

        conn.commit()
        row = conn.execute(
            "SELECT mcp_api_key_enc, mcp_api_key_created_at, mcp_api_key_last_used_at, "
            "mcp_api_key_expires_at, mcp_api_key_allow_network FROM ai_config WHERE id = 1"
        ).fetchone()
        return {
            "active": bool(row["mcp_api_key_enc"]),
            "created_at": row["mcp_api_key_created_at"],
            "last_used_at": row["mcp_api_key_last_used_at"],
            "expires_at": row["mcp_api_key_expires_at"],
            "allow_network": bool(row["mcp_api_key_allow_network"]),
        }
    finally:
        conn.close()


def container_fake_run(root, *, container_name=_CONTAINER_NAME, running=True):
    """Fakes the two docker/podman calls ops/mcp_token.py's `_exec` makes:
    `inspect` (is the container up?) and `exec ... python -m
    app.mcp_token_cli` (perform the operation, via
    `_handle_request_like_container_cli` against this instance's fake
    volume contents)."""
    calls = []
    db_path = paths.sqlite_db_path(root)

    def _run(args, **kwargs):
        args = list(args)
        calls.append(tuple(args))
        if len(args) >= 2 and args[1] == "inspect":
            if not running:
                return SimpleNamespace(returncode=1, stdout="", stderr="no such container")
            return SimpleNamespace(returncode=0, stdout=json.dumps({"Status": "running"}), stderr="")
        if len(args) >= 2 and args[1] == "exec":
            payload = json.loads(kwargs["input"])
            try:
                response = _handle_request_like_container_cli(db_path, payload)
            except mt.McpTokenError as exc:
                return SimpleNamespace(returncode=1, stdout="", stderr=str(exc))
            return SimpleNamespace(returncode=0, stdout=json.dumps(response), stderr="")
        raise AssertionError(f"unexpected call in test: {args}")

    _run.calls = calls
    return _run


def _call_kwargs(run, container_name=_CONTAINER_NAME):
    return dict(runtime="docker", container_name=container_name, run=run)


# ── token/TTL shape (mirrors app/mcp_auth.py) ────────────────────────────


def test_generate_token_shape():
    token = mt.generate_token()
    assert token.startswith(mt.TOKEN_PREFIX)
    # base64.urlsafe_b64encode of 32 bytes with no padding -> 43 chars.
    assert len(token) == len(mt.TOKEN_PREFIX) + 43


def test_generate_token_is_random():
    assert mt.generate_token() != mt.generate_token()


@pytest.mark.parametrize("ttl", [None, 0, -1])
def test_expires_at_none_zero_or_negative_means_no_expiry(ttl):
    assert mt.expires_at_from_ttl_hours(ttl) is None


def test_expires_at_positive_ttl_is_in_the_future():
    import datetime as _dt

    now = _dt.datetime(2026, 7, 11, tzinfo=_dt.timezone.utc)
    result = mt.expires_at_from_ttl_hours(6, now=now)
    assert result == now + _dt.timedelta(hours=6)


# ── reachability rule (mirrors app/mcp_auth.py) ──────────────────────────


def test_static_token_allowed_on_local_regardless_of_allow_network():
    assert mt.is_static_token_allowed("local", False) is True
    assert mt.is_static_token_allowed("local", True) is True


def test_static_token_requires_explicit_opt_in_on_network():
    assert mt.is_static_token_allowed("network", False) is False
    assert mt.is_static_token_allowed("network", True) is True


# ── container not running ────────────────────────────────────────────────


def test_container_not_running_raises_actionable_error(tmp_path):
    root = tmp_path / "castelo"
    run = container_fake_run(root, running=False)
    with pytest.raises(mt.McpTokenError, match="must be running"):
        mt.read_state(root, **_call_kwargs(run))


# ── write_new_token / read_state / revoke / set_allow_network ───────────


def test_write_new_token_round_trips_through_fernet(tmp_path):
    root = tmp_path / "castelo"
    _make_db(root, with_row=True)
    run = container_fake_run(root)
    token = mt.write_new_token(root, "instance-secret-key", **_call_kwargs(run))
    row = _read_row(root)
    assert decrypt("instance-secret-key", row["mcp_api_key_enc"]) == token
    assert row["mcp_api_key_created_at"] is not None
    assert row["mcp_api_key_last_used_at"] is None
    assert row["mcp_api_key_expires_at"] is None


def test_write_new_token_creates_row_when_none_exists(tmp_path):
    root = tmp_path / "castelo"
    _make_db(root, with_row=False)  # never visited Settings -- no id=1 row yet
    run = container_fake_run(root)
    token = mt.write_new_token(root, "k", **_call_kwargs(run))
    row = _read_row(root)
    assert row is not None
    assert decrypt("k", row["mcp_api_key_enc"]) == token
    # Fresh-row defaults applied for the columns this module doesn't itself
    # set, so the app's own Settings page doesn't render a blank/None model.
    assert row["model"] == "claude-sonnet-4-6"
    assert row["connector_name"] == "job-squire"
    assert row["fallback_to_anthropic"] == 1


def test_write_new_token_with_ttl_sets_expiry(tmp_path):
    root = tmp_path / "castelo"
    _make_db(root, with_row=True)
    run = container_fake_run(root)
    mt.write_new_token(root, "k", ttl_hours=1, **_call_kwargs(run))
    row = _read_row(root)
    assert row["mcp_api_key_expires_at"] is not None


def test_rotating_overwrites_the_previous_token(tmp_path):
    root = tmp_path / "castelo"
    _make_db(root, with_row=True)
    run = container_fake_run(root)
    first = mt.write_new_token(root, "k", **_call_kwargs(run))
    second = mt.write_new_token(root, "k", **_call_kwargs(run))
    assert first != second
    row = _read_row(root)
    assert decrypt("k", row["mcp_api_key_enc"]) == second
    assert decrypt("k", row["mcp_api_key_enc"]) != first


def test_read_state_reflects_written_token(tmp_path):
    root = tmp_path / "castelo"
    _make_db(root, with_row=True)
    run = container_fake_run(root)
    assert mt.read_state(root, **_call_kwargs(run)).active is False
    assert mt.read_state(root, **_call_kwargs(run)).usable is False
    mt.write_new_token(root, "k", **_call_kwargs(run))
    state = mt.read_state(root, **_call_kwargs(run))
    assert state.active is True
    assert state.usable is True
    assert state.created_at is not None
    assert state.last_used_at is None
    assert state.allow_network is False


def test_expired_token_is_active_but_not_usable(tmp_path):
    """active means "a token is stored"; usable means "and it still works" --
    the app's own verify_static_token() rejects an expired token, so the
    CLI's generate/rotate preconditions (ops/commands.py) must key off
    usable, not active, or an expired token wrongly blocks `generate`."""
    root = tmp_path / "castelo"
    _make_db(root, with_row=True)
    run = container_fake_run(root)
    mt.write_new_token(root, "k", ttl_hours=1, **_call_kwargs(run))

    conn = sqlite3.connect(str(paths.sqlite_db_path(root)))
    conn.execute(
        "UPDATE ai_config SET mcp_api_key_expires_at = '2000-01-01 00:00:00.000000' WHERE id = 1"
    )
    conn.commit()
    conn.close()

    state = mt.read_state(root, **_call_kwargs(run))
    assert state.active is True
    assert state.usable is False


def test_revoke_clears_all_token_columns(tmp_path):
    root = tmp_path / "castelo"
    _make_db(root, with_row=True)
    run = container_fake_run(root)
    mt.write_new_token(root, "k", ttl_hours=1, **_call_kwargs(run))
    mt.revoke(root, **_call_kwargs(run))
    row = _read_row(root)
    assert row["mcp_api_key_enc"] == ""
    assert row["mcp_api_key_created_at"] is None
    assert row["mcp_api_key_last_used_at"] is None
    assert row["mcp_api_key_expires_at"] is None
    assert mt.read_state(root, **_call_kwargs(run)).active is False


def test_revoke_creates_row_when_none_exists(tmp_path):
    root = tmp_path / "castelo"
    _make_db(root, with_row=False)
    run = container_fake_run(root)
    mt.revoke(root, **_call_kwargs(run))  # must not raise
    assert mt.read_state(root, **_call_kwargs(run)).active is False


def test_set_allow_network_toggles_the_column(tmp_path):
    root = tmp_path / "castelo"
    _make_db(root, with_row=True)
    run = container_fake_run(root)
    assert mt.read_state(root, **_call_kwargs(run)).allow_network is False
    mt.set_allow_network(root, True, **_call_kwargs(run))
    assert mt.read_state(root, **_call_kwargs(run)).allow_network is True
    mt.set_allow_network(root, False, **_call_kwargs(run))
    assert mt.read_state(root, **_call_kwargs(run)).allow_network is False
