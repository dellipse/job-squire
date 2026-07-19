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
"""ops/mcp_token.py -- the jsq_mcp_ static token, written directly into an
instance's database.

Builds its own minimal ai_config table (mirroring tests/test_secrets_copy.py's
_SCHEMA) rather than importing it, since some tests here deliberately start
from a table with *no* id=1 row -- the "never visited the Settings page"
case ops/mcp_token.py's _ensure_row must handle.
"""
import sqlite3

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


def _make_db(root, *, with_row: bool) -> None:
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


# ── missing database ──────────────────────────────────────────────────────


def test_missing_database_raises_actionable_error(tmp_path):
    root = tmp_path / "castelo"
    with pytest.raises(mt.McpTokenError, match="hasn't booted yet"):
        mt.read_state(root)


# ── write_new_token / read_state / revoke / set_allow_network ───────────


def test_write_new_token_round_trips_through_fernet(tmp_path):
    root = tmp_path / "castelo"
    _make_db(root, with_row=True)
    token = mt.write_new_token(root, "instance-secret-key")
    row = _read_row(root)
    assert decrypt("instance-secret-key", row["mcp_api_key_enc"]) == token
    assert row["mcp_api_key_created_at"] is not None
    assert row["mcp_api_key_last_used_at"] is None
    assert row["mcp_api_key_expires_at"] is None


def test_write_new_token_creates_row_when_none_exists(tmp_path):
    root = tmp_path / "castelo"
    _make_db(root, with_row=False)  # never visited Settings -- no id=1 row yet
    token = mt.write_new_token(root, "k")
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
    mt.write_new_token(root, "k", ttl_hours=1)
    row = _read_row(root)
    assert row["mcp_api_key_expires_at"] is not None


def test_rotating_overwrites_the_previous_token(tmp_path):
    root = tmp_path / "castelo"
    _make_db(root, with_row=True)
    first = mt.write_new_token(root, "k")
    second = mt.write_new_token(root, "k")
    assert first != second
    row = _read_row(root)
    assert decrypt("k", row["mcp_api_key_enc"]) == second
    assert decrypt("k", row["mcp_api_key_enc"]) != first


def test_read_state_reflects_written_token(tmp_path):
    root = tmp_path / "castelo"
    _make_db(root, with_row=True)
    assert mt.read_state(root).active is False
    assert mt.read_state(root).usable is False
    mt.write_new_token(root, "k")
    state = mt.read_state(root)
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
    mt.write_new_token(root, "k", ttl_hours=1)

    conn = sqlite3.connect(str(paths.sqlite_db_path(root)))
    conn.execute(
        "UPDATE ai_config SET mcp_api_key_expires_at = '2000-01-01 00:00:00.000000' WHERE id = 1"
    )
    conn.commit()
    conn.close()

    state = mt.read_state(root)
    assert state.active is True
    assert state.usable is False


def test_revoke_clears_all_token_columns(tmp_path):
    root = tmp_path / "castelo"
    _make_db(root, with_row=True)
    mt.write_new_token(root, "k", ttl_hours=1)
    mt.revoke(root)
    row = _read_row(root)
    assert row["mcp_api_key_enc"] == ""
    assert row["mcp_api_key_created_at"] is None
    assert row["mcp_api_key_last_used_at"] is None
    assert row["mcp_api_key_expires_at"] is None
    assert mt.read_state(root).active is False


def test_revoke_creates_row_when_none_exists(tmp_path):
    root = tmp_path / "castelo"
    _make_db(root, with_row=False)
    mt.revoke(root)  # must not raise
    assert mt.read_state(root).active is False


def test_set_allow_network_toggles_the_column(tmp_path):
    root = tmp_path / "castelo"
    _make_db(root, with_row=True)
    assert mt.read_state(root).allow_network is False
    mt.set_allow_network(root, True)
    assert mt.read_state(root).allow_network is True
    mt.set_allow_network(root, False)
    assert mt.read_state(root).allow_network is False
