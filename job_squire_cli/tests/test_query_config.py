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
"""query.config -- no ~/.hermes/ anywhere in the resolution path.

config_dir() branches on platform.system(), so tests pin that function
directly rather than relying on whatever OS happens to run pytest --
otherwise the XDG_CONFIG_HOME-only tests would silently no-op on macOS,
where config_dir() never consults it.
"""
import json

import pytest

from job_squire_cli.query import config as config_module
from job_squire_cli.query.config import QueryConfigError, config_path, load_query_config


@pytest.fixture(autouse=True)
def force_linux_config_dir(monkeypatch):
    """Pin config_dir() to its Linux/XDG branch for every test in this file."""
    monkeypatch.setattr(config_module.platform, "system", lambda: "Linux")


def test_env_vars_take_precedence(monkeypatch):
    monkeypatch.setenv("JOB_SQUIRE_MCP_URL", "http://localhost:9000/")
    monkeypatch.setenv("JOB_SQUIRE_MCP_TOKEN", "jsq_mcp_abc")
    cfg = load_query_config()
    assert cfg.endpoint == "http://localhost:9000"  # trailing slash stripped
    assert cfg.token == "jsq_mcp_abc"


def test_missing_config_raises_actionable_error(monkeypatch, tmp_path):
    monkeypatch.delenv("JOB_SQUIRE_MCP_URL", raising=False)
    monkeypatch.delenv("JOB_SQUIRE_MCP_TOKEN", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    with pytest.raises(QueryConfigError) as exc_info:
        load_query_config()
    assert "JOB_SQUIRE_MCP_URL" in str(exc_info.value)
    assert "hermes" not in str(exc_info.value).lower()


def test_reads_from_config_file(monkeypatch, tmp_path):
    monkeypatch.delenv("JOB_SQUIRE_MCP_URL", raising=False)
    monkeypatch.delenv("JOB_SQUIRE_MCP_TOKEN", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"endpoint": "http://localhost:9000", "token": "jsq_mcp_xyz"}))

    cfg = load_query_config()
    assert cfg.endpoint == "http://localhost:9000"
    assert cfg.token == "jsq_mcp_xyz"


def test_config_file_missing_endpoint_field_is_rejected(monkeypatch, tmp_path):
    monkeypatch.delenv("JOB_SQUIRE_MCP_URL", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"token": "jsq_mcp_xyz"}))

    with pytest.raises(QueryConfigError, match="endpoint"):
        load_query_config()


def test_linux_config_dir_honors_xdg_and_never_mentions_hermes(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = config_path()
    assert path == tmp_path / "job-squire" / "mcp.json"
    assert ".hermes" not in str(path)


def test_macos_config_dir(monkeypatch):
    monkeypatch.setattr(config_module.platform, "system", lambda: "Darwin")
    path = config_module.config_dir()
    assert str(path).endswith("Library/Application Support/job-squire")


def test_windows_config_dir(monkeypatch, tmp_path):
    # pathlib.Path is bound to the *host* OS's flavor regardless of what
    # platform.system() returns, so a literal backslash path can't
    # round-trip correctly unless the test actually runs on Windows.
    # Assert the join by parts instead of a hardcoded separator string, so
    # this exercises config.py's Windows branch identically on any host.
    monkeypatch.setattr(config_module.platform, "system", lambda: "Windows")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    path = config_module.config_dir()
    assert path.name == "job-squire"
    assert path.parent == tmp_path
