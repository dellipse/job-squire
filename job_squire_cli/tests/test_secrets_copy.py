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
"""Importing basic settings between instances.

The Fernet-derivation cross-check against the real app/crypto.py (loaded
directly from its file, bypassing app/__init__.py's Flask-only imports so
this suite never needs Flask installed) lives in this file's
test_fernet_derivation_matches_app_crypto -- it is the one guarantee that
`copy_keys=True` actually produces something the app can decrypt.
"""
import importlib.util
import sqlite3
from pathlib import Path

import pytest

from job_squire_cli.ops import paths, secrets_copy as sc

_APP_CRYPTO_PATH = Path(__file__).resolve().parents[2] / "app" / "crypto.py"


def _load_app_crypto():
    spec = importlib.util.spec_from_file_location("app_crypto_ref", _APP_CRYPTO_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── Fernet derivation ────────────────────────────────────────────────────


@pytest.mark.skipif(not _APP_CRYPTO_PATH.exists(), reason="only runs inside the job-squire monorepo checkout")
def test_fernet_derivation_matches_app_crypto():
    app_crypto = _load_app_crypto()
    key, plaintext = "test-secret-key-1234567890", "hunter2-api-key"

    app_encrypted = app_crypto.encrypt(key, plaintext)
    assert sc._decrypt(key, app_encrypted) == plaintext

    mine_encrypted = sc._encrypt(key, plaintext)
    assert app_crypto.decrypt(key, mine_encrypted) == plaintext


def test_decrypt_wrong_key_returns_none():
    encrypted = sc._encrypt("key-one", "secret-value")
    assert sc._decrypt("key-two", encrypted) is None


def test_decrypt_empty_value_is_empty_not_none():
    assert sc._decrypt("any-key", "") == ""


def test_decrypt_tolerates_legacy_unprefixed_value():
    assert sc._decrypt("any-key", "plaintext-legacy-value") == "plaintext-legacy-value"


def test_reencrypt_round_trips_across_different_keys():
    original = "super-secret-api-key"
    encrypted = sc._encrypt("source-key", original)
    reencrypted = sc.reencrypt(encrypted, source_secret_key="source-key", dest_secret_key="dest-key")
    assert sc._decrypt("dest-key", reencrypted) == original


def test_reencrypt_returns_none_when_source_key_is_wrong():
    encrypted = sc._encrypt("source-key", "value")
    assert sc.reencrypt(encrypted, source_secret_key="wrong-key", dest_secret_key="dest-key") is None


# ── schedule env / secret key extraction ─────────────────────────────────


def test_read_schedule_env_reads_whitelisted_keys_only(tmp_path):
    root = tmp_path / "source"
    paths.data_dir(root).mkdir(parents=True)
    paths.data_env_path(root).write_text(
        "SECRET_KEY=abc\nSCHEDULE_TZ=America/Chicago\nSCHEDULE_WEEKDAY_HOURS=8,13,17\n"
        "ADMIN_PASSWORD=hunter2\n# a comment\n\nSCHEDULE_MINUTE=0\n"
    )
    result = sc.read_schedule_env(root)
    assert result == {
        "SCHEDULE_TZ": "America/Chicago",
        "SCHEDULE_WEEKDAY_HOURS": "8,13,17",
        "SCHEDULE_MINUTE": "0",
    }


def test_read_schedule_env_skips_blank_values(tmp_path):
    root = tmp_path / "source"
    paths.data_dir(root).mkdir(parents=True)
    paths.data_env_path(root).write_text("SCHEDULE_TZ=\nSCHEDULE_WEEKDAY_HOURS=8,13,17\n")
    assert sc.read_schedule_env(root) == {"SCHEDULE_WEEKDAY_HOURS": "8,13,17"}


def test_read_schedule_env_missing_file_returns_empty(tmp_path):
    assert sc.read_schedule_env(tmp_path / "nowhere") == {}


def test_read_secret_key_round_trips(tmp_path):
    root = tmp_path / "source"
    paths.data_dir(root).mkdir(parents=True)
    paths.data_env_path(root).write_text("SECRET_KEY=my-secret-value\nOTHER=1\n")
    assert sc.read_secret_key(root) == "my-secret-value"


def test_read_secret_key_missing_raises(tmp_path):
    root = tmp_path / "source"
    paths.data_dir(root).mkdir(parents=True)
    paths.data_env_path(root).write_text("OTHER=1\n")
    with pytest.raises(sc.SecretsCopyError):
        sc.read_secret_key(root)


# ── database settings copy ────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE search_config (
    id INTEGER PRIMARY KEY, titles TEXT, location TEXT, country TEXT,
    radius_miles INTEGER, min_salary INTEGER, max_age_days INTEGER,
    results_per_query INTEGER, enabled BOOLEAN
);
CREATE TABLE smtp_config (
    id INTEGER PRIMARY KEY, enabled BOOLEAN, host TEXT, port INTEGER, use_tls BOOLEAN,
    username TEXT, password_enc TEXT, from_addr TEXT, to_addr TEXT, admin_email TEXT
);
CREATE TABLE ai_config (
    id INTEGER PRIMARY KEY, api_enabled BOOLEAN, mcp_enabled BOOLEAN,
    claude_buttons_enabled BOOLEAN, api_key_enc TEXT, model TEXT, mcp_api_key_enc TEXT,
    thinking_mode TEXT, auto_triage_enabled BOOLEAN, triage_model TEXT,
    auto_followup_enabled BOOLEAN, auto_weekly_review_enabled BOOLEAN,
    rejection_alert_threshold INTEGER, fallback_to_anthropic BOOLEAN,
    connector_name TEXT, mcp_api_key_allow_network BOOLEAN
);
CREATE TABLE provider_credentials (
    id INTEGER PRIMARY KEY, provider TEXT UNIQUE, enabled BOOLEAN, secret_blob TEXT
);
CREATE TABLE users (
    id INTEGER PRIMARY KEY, username TEXT UNIQUE, jobs_default_sort TEXT,
    jobs_default_status TEXT, jobs_default_per_page INTEGER
);
CREATE TABLE ai_provider_configs (
    id INTEGER PRIMARY KEY, rank INTEGER, provider TEXT, label TEXT, api_key_enc TEXT,
    base_url TEXT, model TEXT, triage_model TEXT, use_for_triage BOOLEAN,
    use_for_analysis BOOLEAN, thinking_mode TEXT, enabled BOOLEAN
);
"""


def _make_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def _seed_source(conn: sqlite3.Connection, secret_key: str) -> None:
    conn.execute(
        "INSERT INTO search_config (id, titles, location, country, radius_miles, min_salary, "
        "max_age_days, results_per_query, enabled) VALUES (1, 'Engineer', 'Austin, TX', 'US', 40, "
        "90000, 14, 25, 1)"
    )
    conn.execute(
        "INSERT INTO smtp_config (id, enabled, host, port, use_tls, username, password_enc, "
        "from_addr, to_addr, admin_email) VALUES (1, 1, 'smtp.example.com', 587, 1, 'me', ?, "
        "'me@example.com', 'me@example.com', 'admin@example.com')",
        (sc._encrypt(secret_key, "smtp-password"),),
    )
    conn.execute(
        "INSERT INTO ai_config (id, api_enabled, mcp_enabled, claude_buttons_enabled, api_key_enc, "
        "model, mcp_api_key_enc, thinking_mode, auto_triage_enabled, triage_model, "
        "auto_followup_enabled, auto_weekly_review_enabled, rejection_alert_threshold, "
        "fallback_to_anthropic, connector_name, mcp_api_key_allow_network) VALUES "
        "(1, 1, 0, 1, ?, 'claude-sonnet-4-6', ?, 'low', 1, 'claude-haiku-4-5', 1, 1, 5, 1, "
        "'job-squire', 0)",
        (sc._encrypt(secret_key, "anthropic-api-key"), sc._encrypt(secret_key, "mcp-static-token")),
    )
    conn.execute(
        "INSERT INTO provider_credentials (provider, enabled, secret_blob) VALUES ('dice', 1, ?)",
        (sc._encrypt(secret_key, '{"api_key": "dice-key"}'),),
    )
    conn.execute(
        "INSERT INTO users (username, jobs_default_sort, jobs_default_status, jobs_default_per_page) "
        "VALUES ('user', 'created_at desc', 'Applied', 50)"
    )
    conn.execute(
        "INSERT INTO ai_provider_configs (rank, provider, label, api_key_enc, base_url, model, "
        "triage_model, use_for_triage, use_for_analysis, thinking_mode, enabled) VALUES "
        "(1, 'openrouter', 'Free tier', ?, '', 'gpt-oss-120b', 'gpt-oss-20b', 1, 1, NULL, 1)",
        (sc._encrypt(secret_key, "openrouter-key"),),
    )
    conn.commit()


def _seed_dest_defaults(conn: sqlite3.Connection) -> None:
    """What the app itself would have seeded on the new instance's first boot."""
    conn.execute(
        "INSERT INTO search_config (id, titles, location, country, radius_miles, min_salary, "
        "max_age_days, results_per_query, enabled) VALUES (1, '', '', 'US', 40, NULL, 14, 25, 1)"
    )
    conn.execute(
        "INSERT INTO smtp_config (id, enabled, host, port, use_tls, username, password_enc, "
        "from_addr, to_addr, admin_email) VALUES (1, 0, '', 587, 1, '', '', '', '', '')"
    )
    conn.execute(
        "INSERT INTO ai_config (id, api_enabled, mcp_enabled, claude_buttons_enabled, api_key_enc, "
        "model, mcp_api_key_enc, thinking_mode, auto_triage_enabled, triage_model, "
        "auto_followup_enabled, auto_weekly_review_enabled, rejection_alert_threshold, "
        "fallback_to_anthropic, connector_name, mcp_api_key_allow_network) VALUES "
        "(1, 0, 0, 0, '', 'claude-sonnet-4-6', '', 'disabled', 0, 'claude-haiku-4-5', 0, 0, 5, 1, "
        "'job-squire', 0)"
    )
    conn.execute("INSERT INTO users (username, jobs_default_sort, jobs_default_status, jobs_default_per_page) "
                  "VALUES ('admin', NULL, NULL, NULL)")
    conn.execute("INSERT INTO users (username, jobs_default_sort, jobs_default_status, jobs_default_per_page) "
                  "VALUES ('user', NULL, NULL, NULL)")
    conn.commit()


@pytest.fixture
def source_root(tmp_path):
    root = tmp_path / "source"
    conn = _make_db(paths.sqlite_db_path(root))
    _seed_source(conn, "source-secret-key")
    conn.close()
    return root


@pytest.fixture
def dest_root(tmp_path):
    root = tmp_path / "dest"
    conn = _make_db(paths.sqlite_db_path(root))
    _seed_dest_defaults(conn)
    conn.close()
    return root


def test_copy_without_keys_copies_nonsecret_fields_only(source_root, dest_root):
    summary = sc.copy_db_settings(
        source_root=source_root, dest_root=dest_root,
        source_secret_key="source-secret-key", dest_secret_key="dest-secret-key", copy_keys=False,
    )
    assert not summary.warnings
    assert set(summary.tables_copied) == {
        "search_config", "smtp_config", "ai_config", "provider_credentials",
        "users", "ai_provider_configs",
    }
    assert summary.secrets_copied is False

    conn = sqlite3.connect(str(paths.sqlite_db_path(dest_root)))
    conn.row_factory = sqlite3.Row

    search = conn.execute("SELECT * FROM search_config WHERE id = 1").fetchone()
    assert search["titles"] == "Engineer"
    assert search["location"] == "Austin, TX"
    assert search["radius_miles"] == 40

    smtp = conn.execute("SELECT * FROM smtp_config WHERE id = 1").fetchone()
    assert smtp["host"] == "smtp.example.com"
    assert smtp["password_enc"] == ""  # secret excluded by default

    ai = conn.execute("SELECT * FROM ai_config WHERE id = 1").fetchone()
    assert ai["api_enabled"] == 1
    assert ai["model"] == "claude-sonnet-4-6"
    assert ai["api_key_enc"] == ""  # secret excluded by default

    provider = conn.execute("SELECT * FROM provider_credentials WHERE provider = 'dice'").fetchone()
    assert provider["enabled"] == 1
    # This is a fresh INSERT (dest has no prior row for this provider) and
    # copy_keys=False excludes secret_blob from the write entirely, so it's
    # left at the column's SQL-level default (NULL here) rather than "" --
    # functionally identical, since app/crypto.decrypt() treats any falsy
    # stored value the same way.
    assert not provider["secret_blob"]

    user = conn.execute("SELECT * FROM users WHERE username = 'user'").fetchone()
    assert user["jobs_default_sort"] == "created_at desc"
    assert user["jobs_default_per_page"] == 50
    admin = conn.execute("SELECT * FROM users WHERE username = 'admin'").fetchone()
    assert admin["jobs_default_sort"] is None  # never inserted a new/unrelated row

    provider_chain = conn.execute("SELECT * FROM ai_provider_configs").fetchall()
    assert len(provider_chain) == 1
    assert provider_chain[0]["provider"] == "openrouter"
    assert not provider_chain[0]["api_key_enc"]  # excluded (full-replace insert, copy_keys=False)
    conn.close()


def test_copy_with_keys_reencrypts_for_destination(source_root, dest_root):
    sc.copy_db_settings(
        source_root=source_root, dest_root=dest_root,
        source_secret_key="source-secret-key", dest_secret_key="dest-secret-key", copy_keys=True,
    )
    conn = sqlite3.connect(str(paths.sqlite_db_path(dest_root)))
    conn.row_factory = sqlite3.Row

    smtp = conn.execute("SELECT password_enc FROM smtp_config WHERE id = 1").fetchone()
    assert sc._decrypt("dest-secret-key", smtp["password_enc"]) == "smtp-password"

    ai = conn.execute("SELECT api_key_enc, mcp_api_key_enc FROM ai_config WHERE id = 1").fetchone()
    assert sc._decrypt("dest-secret-key", ai["api_key_enc"]) == "anthropic-api-key"
    assert sc._decrypt("dest-secret-key", ai["mcp_api_key_enc"]) == "mcp-static-token"

    provider = conn.execute("SELECT secret_blob FROM provider_credentials WHERE provider = 'dice'").fetchone()
    assert sc._decrypt("dest-secret-key", provider["secret_blob"]) == '{"api_key": "dice-key"}'

    chain = conn.execute("SELECT api_key_enc FROM ai_provider_configs").fetchone()
    assert sc._decrypt("dest-secret-key", chain["api_key_enc"]) == "openrouter-key"
    conn.close()


def test_copy_missing_dest_database_raises(source_root, tmp_path):
    with pytest.raises(sc.SecretsCopyError):
        sc.copy_db_settings(
            source_root=source_root, dest_root=tmp_path / "nowhere",
            source_secret_key="a", dest_secret_key="b", copy_keys=False,
        )


def test_copy_missing_source_database_warns_without_raising(tmp_path, dest_root):
    summary = sc.copy_db_settings(
        source_root=tmp_path / "nowhere", dest_root=dest_root,
        source_secret_key="a", dest_secret_key="b", copy_keys=False,
    )
    assert not summary.tables_copied
    assert "nothing imported" in summary.warnings[0]


def test_copy_warns_and_continues_when_a_table_is_missing(tmp_path):
    source = tmp_path / "source"
    source_db = paths.sqlite_db_path(source)
    source_db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(source_db))
    # A schema with only search_config -- everything else is "missing".
    conn.execute(
        "CREATE TABLE search_config (id INTEGER PRIMARY KEY, titles TEXT, location TEXT, "
        "country TEXT, radius_miles INTEGER, min_salary INTEGER, max_age_days INTEGER, "
        "results_per_query INTEGER, enabled BOOLEAN)"
    )
    conn.execute(
        "INSERT INTO search_config VALUES (1, 'Engineer', 'Austin, TX', 'US', 40, NULL, 14, 25, 1)"
    )
    conn.commit()
    conn.close()

    dest = tmp_path / "dest"
    dconn = _make_db(paths.sqlite_db_path(dest))
    _seed_dest_defaults(dconn)
    dconn.close()

    summary = sc.copy_db_settings(
        source_root=source, dest_root=dest,
        source_secret_key="a", dest_secret_key="b", copy_keys=False,
    )
    assert "search_config" in summary.tables_copied
    assert any("smtp_config" in w for w in summary.warnings)
    assert any("not found" in w for w in summary.warnings)
