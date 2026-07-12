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
"""query.config -- multi-instance mcp.json (Prompt C6), and no ~/.hermes/
anywhere in the resolution path.

config_dir() branches on platform.system(), so tests pin that function
directly rather than relying on whatever OS happens to run pytest --
otherwise the XDG_CONFIG_HOME-only tests would silently no-op on macOS,
where config_dir() never consults it.
"""
import json
import os

import pytest

from job_squire_cli.query import config as config_module
from job_squire_cli.query.config import (
    QueryConfigError,
    clear_token,
    config_path,
    load_query_config,
    load_raw_config,
    remove_instance_config,
    set_instance,
)


@pytest.fixture(autouse=True)
def force_linux_config_dir(monkeypatch):
    """Pin config_dir() to its Linux/XDG branch for every test in this file."""
    monkeypatch.setattr(config_module.platform, "system", lambda: "Linux")


@pytest.fixture(autouse=True)
def tmp_config_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("JOB_SQUIRE_MCP_URL", raising=False)
    monkeypatch.delenv("JOB_SQUIRE_MCP_TOKEN", raising=False)


def test_env_vars_take_precedence(monkeypatch):
    monkeypatch.setenv("JOB_SQUIRE_MCP_URL", "http://localhost:9000/")
    monkeypatch.setenv("JOB_SQUIRE_MCP_TOKEN", "jsq_mcp_abc")
    cfg = load_query_config()
    assert cfg.endpoint == "http://localhost:9000"  # trailing slash stripped
    assert cfg.token == "jsq_mcp_abc"
    assert cfg.instance is None


def test_missing_config_raises_actionable_error():
    with pytest.raises(QueryConfigError) as exc_info:
        load_query_config()
    message = str(exc_info.value)
    assert "JOB_SQUIRE_MCP_URL" in message
    assert "job-squire configure" in message
    assert "hermes" not in message.lower()


# ── set_instance / resolution ─────────────────────────────────────────────


def test_set_instance_writes_endpoint_and_token_and_becomes_default():
    set_instance("castelo", endpoint="http://localhost:9000", token="jsq_mcp_xyz")
    cfg = load_query_config()
    assert cfg.endpoint == "http://localhost:9000"
    assert cfg.token == "jsq_mcp_xyz"
    assert cfg.instance == "castelo"

    data = load_raw_config()
    assert data["default"] == "castelo"


def test_second_instance_does_not_steal_default():
    set_instance("castelo", endpoint="http://localhost:9000", token="t1")
    set_instance("segundo", endpoint="http://localhost:9001", token="t2")
    data = load_raw_config()
    assert data["default"] == "castelo"
    # No default override requested -> resolving without --instance still
    # needs an explicit choice once there's more than one candidate that
    # isn't the default... but the default *is* set, so it wins.
    assert load_query_config().instance == "castelo"
    assert load_query_config("segundo").instance == "segundo"


def test_sole_instance_resolves_without_a_default_set():
    set_instance("castelo", endpoint="http://localhost:9000", token="t1", make_default=False)
    data = load_raw_config()
    assert data["default"] is None
    cfg = load_query_config()  # only one instance -> unambiguous even with no default
    assert cfg.instance == "castelo"


def test_multiple_instances_without_default_requires_explicit_choice():
    set_instance("castelo", endpoint="http://localhost:9000", token="t1", make_default=False)
    set_instance("segundo", endpoint="http://localhost:9001", token="t2", make_default=False)
    with pytest.raises(QueryConfigError, match="none is the default"):
        load_query_config()
    assert load_query_config("segundo").endpoint == "http://localhost:9001"


def test_unknown_instance_name_lists_configured():
    set_instance("castelo", endpoint="http://localhost:9000", token="t1")
    with pytest.raises(QueryConfigError, match="castelo"):
        load_query_config("ghost")


def test_set_default_true_overrides_existing_default():
    set_instance("castelo", endpoint="http://localhost:9000", token="t1")
    set_instance("segundo", endpoint="http://localhost:9001", token="t2", make_default=True)
    assert load_raw_config()["default"] == "segundo"


def test_set_default_false_clears_default_if_it_was_this_instance():
    set_instance("castelo", endpoint="http://localhost:9000", token="t1")
    set_instance("castelo", endpoint="http://localhost:9000", token="t1", make_default=False)
    assert load_raw_config()["default"] is None


def test_config_file_missing_endpoint_field_is_rejected():
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "default": "castelo", "instances": {"castelo": {}}}))
    with pytest.raises(QueryConfigError, match="endpoint"):
        load_query_config()


# ── clear_token / remove_instance_config ─────────────────────────────────


def test_clear_token_removes_token_but_keeps_endpoint():
    set_instance("castelo", endpoint="http://localhost:9000", token="jsq_mcp_xyz")
    clear_token("castelo")
    cfg = load_query_config()
    assert cfg.endpoint == "http://localhost:9000"
    assert cfg.token is None


def test_clear_token_on_unconfigured_instance_is_a_no_op():
    clear_token("ghost")  # must not raise
    assert load_raw_config()["instances"] == {}


def test_remove_instance_config_reassigns_default():
    set_instance("castelo", endpoint="http://localhost:9000", token="t1")
    set_instance("segundo", endpoint="http://localhost:9001", token="t2", make_default=False)
    remove_instance_config("castelo")
    data = load_raw_config()
    assert "castelo" not in data["instances"]
    assert data["default"] == "segundo"


# ── file permissions and shape ────────────────────────────────────────────


def test_config_file_is_written_with_restrictive_permissions():
    set_instance("castelo", endpoint="http://localhost:9000", token="jsq_mcp_xyz")
    if os.name != "nt":
        mode = config_path().stat().st_mode & 0o777
        assert mode == 0o600


def test_linux_config_dir_honors_xdg_and_never_mentions_hermes():
    path = config_path()
    assert path.name == "mcp.json"
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
