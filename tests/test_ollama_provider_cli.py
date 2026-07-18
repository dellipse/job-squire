# Copyright (C) 2026 D. Brandmeyer
# Licensed under the GNU Affero General Public License v3 or later.
"""Tests for app/ollama_provider_cli.py, the container-side entrypoint
`job-squire ollama setup` execs into via `docker/podman exec` now that
/data is a named Docker volume rather than a host path the CLI can open
directly (see job_squire_cli/ops/ollama_assist.py's write_provider_config
and this module's own docstring).

Deliberately self-contained -- write_provider_row() only ever touches
`ai_provider_configs`/`ai_config` via raw sqlite3, so these tests build a
minimal schema directly rather than pulling in the full Flask `app`
fixture (this module is deliberately not a Flask route; see its module
docstring).
"""
import json
import sqlite3
import subprocess
import sys

import pytest

from app.ollama_provider_cli import OllamaProviderCliError, main, write_provider_row

_SCHEMA = """
CREATE TABLE ai_provider_configs (
    id INTEGER PRIMARY KEY, rank INTEGER, provider TEXT, label TEXT, api_key_enc TEXT,
    base_url TEXT, model TEXT, triage_model TEXT, num_ctx INTEGER, use_for_triage BOOLEAN,
    use_for_analysis BOOLEAN, thinking_mode TEXT, enabled BOOLEAN
);
CREATE TABLE ai_config (id INTEGER PRIMARY KEY, api_enabled BOOLEAN DEFAULT 0);
INSERT INTO ai_config (id, api_enabled) VALUES (1, 0);
"""

_PAYLOAD = {
    "base_url": "http://host.docker.internal:11434",
    "triage_model": "qwen3:8b",
    "analysis_model": "gemma4:12b",
    "num_ctx": 16384,
    "rank": None,
    "enabled": True,
    "enable_automatic_features": True,
}


def _make_db(tmp_path, schema=_SCHEMA):
    db_path = tmp_path / "job-squire.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(schema)
    conn.commit()
    conn.close()
    return db_path


def test_write_provider_row_raises_if_db_missing(tmp_path):
    with pytest.raises(OllamaProviderCliError, match="Database not found"):
        write_provider_row(str(tmp_path / "job-squire.db"), _PAYLOAD)


def test_write_provider_row_inserts_new_row(tmp_path):
    db_path = _make_db(tmp_path)
    automatic_features_enabled = write_provider_row(str(db_path), _PAYLOAD)
    assert automatic_features_enabled is True

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM ai_provider_configs WHERE provider = 'ollama'").fetchone()
    assert row["base_url"] == _PAYLOAD["base_url"]
    assert row["triage_model"] == "qwen3:8b"
    assert row["model"] == "gemma4:12b"
    assert row["num_ctx"] == 16384
    assert row["api_key_enc"] == ""
    assert row["rank"] == 1
    ai_cfg = conn.execute("SELECT api_enabled FROM ai_config WHERE id = 1").fetchone()
    assert ai_cfg[0] == 1


def test_write_provider_row_updates_existing_row_in_place(tmp_path):
    db_path = _make_db(tmp_path)
    write_provider_row(str(db_path), _PAYLOAD)
    updated = dict(_PAYLOAD, triage_model="qwen3:4b", analysis_model="gemma3:4b")
    write_provider_row(str(db_path), updated)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM ai_provider_configs WHERE provider = 'ollama'").fetchall()
    assert len(rows) == 1
    assert rows[0]["triage_model"] == "qwen3:4b"


def test_write_provider_row_missing_num_ctx_column_raises_actionable_error(tmp_path):
    schema_without_num_ctx = """
        CREATE TABLE ai_provider_configs (
            id INTEGER PRIMARY KEY, rank INTEGER, provider TEXT, label TEXT, api_key_enc TEXT,
            base_url TEXT, model TEXT, triage_model TEXT, use_for_triage BOOLEAN,
            use_for_analysis BOOLEAN, thinking_mode TEXT, enabled BOOLEAN
        );
    """
    db_path = _make_db(tmp_path, schema=schema_without_num_ctx)
    with pytest.raises(OllamaProviderCliError, match="num_ctx column"):
        write_provider_row(str(db_path), _PAYLOAD)


def test_write_provider_row_can_skip_enabling_automatic_features(tmp_path):
    db_path = _make_db(tmp_path)
    payload = dict(_PAYLOAD, enable_automatic_features=False)
    enabled = write_provider_row(str(db_path), payload)
    assert enabled is False
    conn = sqlite3.connect(str(db_path))
    row = conn.execute("SELECT api_enabled FROM ai_config WHERE id = 1").fetchone()
    assert row[0] == 0  # untouched


def test_write_provider_row_warns_without_crashing_when_ai_config_missing(tmp_path):
    schema_without_ai_config = """
        CREATE TABLE ai_provider_configs (
            id INTEGER PRIMARY KEY, rank INTEGER, provider TEXT, label TEXT, api_key_enc TEXT,
            base_url TEXT, model TEXT, triage_model TEXT, num_ctx INTEGER, use_for_triage BOOLEAN,
            use_for_analysis BOOLEAN, thinking_mode TEXT, enabled BOOLEAN
        );
    """
    db_path = _make_db(tmp_path, schema=schema_without_ai_config)
    enabled = write_provider_row(str(db_path), _PAYLOAD)
    assert enabled is False  # doesn't crash -- ai_config table missing entirely

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM ai_provider_configs WHERE provider = 'ollama'").fetchone()
    assert row["base_url"] == _PAYLOAD["base_url"]  # provider row still written


# ── main() -- the stdin/stdout process boundary docker/podman exec drives ──


def test_main_reads_stdin_writes_json_result(tmp_path, monkeypatch, capsys):
    db_path = _make_db(tmp_path)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "stdin", _FakeStdin(json.dumps(_PAYLOAD)))

    exit_code = main()

    assert exit_code == 0
    result = json.loads(capsys.readouterr().out)
    assert result == {"automatic_features_enabled": True}
    conn = sqlite3.connect(str(db_path))
    row = conn.execute("SELECT base_url FROM ai_provider_configs WHERE provider = 'ollama'").fetchone()
    assert row[0] == _PAYLOAD["base_url"]


def test_main_reports_malformed_stdin_on_stderr(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "stdin", _FakeStdin("not json"))

    exit_code = main()

    assert exit_code == 1
    assert "Malformed request" in capsys.readouterr().err


def test_main_reports_write_failure_on_stderr(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))  # no db created -- write_provider_row will raise
    monkeypatch.setattr(sys, "stdin", _FakeStdin(json.dumps(_PAYLOAD)))

    exit_code = main()

    assert exit_code == 1
    assert "Database not found" in capsys.readouterr().err


class _FakeStdin:
    def __init__(self, text):
        self._text = text

    def read(self):
        return self._text


def test_module_invocable_as_subprocess(tmp_path):
    """Sanity check that `python -m app.ollama_provider_cli` (the exact
    invocation job_squire_cli's write_provider_config execs into the
    container with) actually works end to end, not just via direct
    imports above."""
    _make_db(tmp_path)
    env = {"DATA_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"}
    result = subprocess.run(
        [sys.executable, "-m", "app.ollama_provider_cli"],
        input=json.dumps(_PAYLOAD), capture_output=True, text=True, env=env, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"automatic_features_enabled": True}
