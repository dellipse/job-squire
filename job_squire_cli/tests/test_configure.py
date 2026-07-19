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
"""The `configure` click command: MCP authentication and the
query-group token-config plumbing, end to end through the CLI (no real
container runtime involved -- the instance's data directory and database
are built by hand, the same way tests/test_lifecycle.py's FakeRuntime does
for its --import-from tests).
"""
import sqlite3

import click.testing
import pytest

from job_squire_cli.cli import main
from job_squire_cli.ops import mcp_token as mt
from job_squire_cli.ops import paths
from job_squire_cli.ops import registry as reg
from job_squire_cli.ops import tailscale as tailscale_ops
from job_squire_cli.query import config as query_config_module

# Not tests/test_secrets_copy.py's _SCHEMA: that one is intentionally
# narrower (only the columns ops/secrets_copy.py touches) and is missing
# the mcp_api_key_created_at/last_used_at/expires_at columns ops/mcp_token.py
# needs. tests/test_mcp_token.py's schema has the full set.
from tests.test_mcp_token import _AI_CONFIG_SCHEMA


@pytest.fixture(autouse=True)
def force_linux_config_dir(monkeypatch):
    monkeypatch.setattr(query_config_module.platform, "system", lambda: "Linux")


@pytest.fixture(autouse=True)
def tmp_homes(monkeypatch, tmp_path):
    """Both the registry/mcp.json home (XDG_CONFIG_HOME) and the instance
    data-directory home (JOB_SQUIRE_HOME) redirected to tmp_path, exactly
    as ops/commands.py's `configure` resolves them for real (it calls
    paths.instance_root(name) with no explicit data_root)."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("JOB_SQUIRE_HOME", str(tmp_path / "instances"))


@pytest.fixture
def runner():
    return click.testing.CliRunner()


def _make_instance(name: str, *, mode: str = "local", mcp_port: int = 9000, secret_key: str = "sk-1"):
    root = paths.instance_root(name)
    paths.data_dir(root).mkdir(parents=True)
    paths.data_env_path(root).write_text(f"SECRET_KEY={secret_key}\nINSTANCE_NAME={name}\n")
    conn = sqlite3.connect(str(paths.sqlite_db_path(root)))
    conn.executescript(_AI_CONFIG_SCHEMA)
    conn.execute(
        "INSERT INTO ai_config (id, mode, api_enabled, mcp_api_key_enc, mcp_api_key_allow_network) "
        "VALUES (1, 'manual', 0, '', 0)"
    )
    conn.commit()
    conn.close()
    return reg.add_instance(
        name=name, mode=mode, runtime="docker", data_dir=str(root),
        public_url=("http://localhost:8080" if mode == "local" else "https://squire.example.com"),
        app_port=8080 if mode == "local" else None, mcp_port=mcp_port if mode == "local" else None,
    )


# ── generate / rotate / revoke ────────────────────────────────────────────


def test_generate_prints_token_and_wires_query_config(runner):
    _make_instance("castelo")
    result = runner.invoke(main, ["configure", "castelo", "--mcp-token", "generate"])
    assert result.exit_code == 0, result.output
    assert "MCP token generated for 'castelo'" in result.output

    cfg = query_config_module.load_query_config()
    assert cfg.instance == "castelo"
    assert cfg.endpoint == "http://localhost:9000"
    assert cfg.token is not None
    assert cfg.token.startswith("jsq_mcp_")

    state = mt.read_state(paths.instance_root("castelo"))
    assert state.active is True


def test_generate_refuses_to_clobber_an_active_token(runner):
    _make_instance("castelo")
    runner.invoke(main, ["configure", "castelo", "--mcp-token", "generate"])
    result = runner.invoke(main, ["configure", "castelo", "--mcp-token", "generate"])
    assert result.exit_code == 1
    assert "already has an active MCP token" in result.output


def test_rotate_requires_an_existing_token(runner):
    _make_instance("castelo")
    result = runner.invoke(main, ["configure", "castelo", "--mcp-token", "rotate"])
    assert result.exit_code == 1
    assert "No active MCP token" in result.output


def test_generate_is_not_blocked_by_an_expired_token(runner):
    """An expired token still has ciphertext in mcp_api_key_enc (TokenState.
    active), but the app itself would already refuse it, so `generate`
    must key off usability, not raw presence, or an operator whose token
    expired naturally gets wrongly told to use `rotate` instead."""
    _make_instance("castelo")
    runner.invoke(main, ["configure", "castelo", "--mcp-token", "generate", "--ttl-hours", "1"])
    root = paths.instance_root("castelo")
    conn = sqlite3.connect(str(paths.sqlite_db_path(root)))
    conn.execute("UPDATE ai_config SET mcp_api_key_expires_at = '2000-01-01 00:00:00.000000' WHERE id = 1")
    conn.commit()
    conn.close()

    result = runner.invoke(main, ["configure", "castelo", "--mcp-token", "generate"])
    assert result.exit_code == 0, result.output
    assert mt.read_state(root).usable is True


def test_negative_ttl_hours_does_not_claim_an_expiry_was_set(runner):
    _make_instance("castelo")
    result = runner.invoke(main, ["configure", "castelo", "--mcp-token", "generate", "--ttl-hours", "-5"])
    assert result.exit_code == 0, result.output
    assert "Expires in" not in result.output
    assert mt.read_state(paths.instance_root("castelo")).expires_at is None


def test_revoke_with_endpoint_applies_it_instead_of_silently_dropping_it(runner):
    _make_instance("castelo")
    runner.invoke(main, ["configure", "castelo", "--mcp-token", "generate"])
    result = runner.invoke(
        main, ["configure", "castelo", "--mcp-token", "revoke", "--endpoint", "http://localhost:9999"],
    )
    assert result.exit_code == 0, result.output
    cfg = query_config_module.load_query_config()
    assert cfg.endpoint == "http://localhost:9999"
    assert cfg.token is None


def test_rotate_replaces_the_previous_token(runner):
    _make_instance("castelo")
    runner.invoke(main, ["configure", "castelo", "--mcp-token", "generate"])
    first_token = query_config_module.load_query_config().token

    result = runner.invoke(main, ["configure", "castelo", "--mcp-token", "rotate"])
    assert result.exit_code == 0, result.output
    second_token = query_config_module.load_query_config().token
    assert second_token != first_token
    assert "MCP token rotated for 'castelo'" in result.output


def test_revoke_clears_the_token_everywhere(runner):
    _make_instance("castelo")
    runner.invoke(main, ["configure", "castelo", "--mcp-token", "generate"])

    result = runner.invoke(main, ["configure", "castelo", "--mcp-token", "revoke"])
    assert result.exit_code == 0, result.output
    assert "MCP token revoked for 'castelo'" in result.output

    assert mt.read_state(paths.instance_root("castelo")).active is False
    assert query_config_module.load_query_config().token is None


# ── network-mode reachability rule ────────────────────────────────────────


def test_network_instance_refuses_static_token_without_opt_in(runner):
    _make_instance("remoto", mode="network")
    result = runner.invoke(main, ["configure", "remoto", "--mcp-token", "generate"])
    assert result.exit_code == 1
    assert "network-reachable" in result.output
    assert "--allow-network" in result.output


def test_network_instance_allows_static_token_with_explicit_opt_in(runner):
    _make_instance("remoto", mode="network")
    result = runner.invoke(
        main, ["configure", "remoto", "--mcp-token", "generate", "--allow-network"],
    )
    assert result.exit_code == 0, result.output
    state = mt.read_state(paths.instance_root("remoto"))
    assert state.active is True
    assert state.allow_network is True
    # Network mode: no mcp_port on the registry entry -- derived from the
    # public_url hostname via the same mcp-<hostname> convention `create
    # --mcp-hostname` defaults to.
    assert query_config_module.load_query_config().endpoint == "https://mcp-squire.example.com"


# ── tailnet reachability rule ──────────────────────────────────────────
# A Tailscale-Serve-fronted instance stays mode="local" by design (ops/
# tailscale.py's module docstring), so Instance.mode alone can't tell
# `configure` it's reachable beyond this machine -- the state manifest
# ops/tailscale.py writes is what closes that gap. The gate itself reuses
# the exact same --allow-network opt-in as the network-mode rule above,
# never a separate flag.


def test_tailnet_reachable_instance_refuses_static_token_without_opt_in(runner):
    _make_instance("castelo")
    root = paths.instance_root("castelo")
    tailscale_ops._write_state(root, tailscale_ops.TailscaleState(
        enabled=True, hostname="castelo.tail1234.ts.net", web_port=443, mcp_port=8443,
        enabled_at="2026-07-11T12:00:00Z",
    ))
    result = runner.invoke(main, ["configure", "castelo", "--mcp-token", "generate"])
    assert result.exit_code == 1
    assert "reachable over your tailnet" in result.output
    assert "--allow-network" in result.output
    assert mt.read_state(root).active is False


def test_tailnet_reachable_instance_allows_static_token_with_explicit_opt_in(runner):
    _make_instance("castelo")
    root = paths.instance_root("castelo")
    tailscale_ops._write_state(root, tailscale_ops.TailscaleState(
        enabled=True, hostname="castelo.tail1234.ts.net", web_port=443, mcp_port=8443,
        enabled_at="2026-07-11T12:00:00Z",
    ))
    result = runner.invoke(
        main, ["configure", "castelo", "--mcp-token", "generate", "--allow-network"],
    )
    assert result.exit_code == 0, result.output
    state = mt.read_state(root)
    assert state.active is True
    assert state.allow_network is True


def test_instance_without_tailscale_enabled_is_unaffected_by_the_gate(runner):
    _make_instance("castelo")
    result = runner.invoke(main, ["configure", "castelo", "--mcp-token", "generate"])
    assert result.exit_code == 0, result.output


def test_allow_network_flag_alone_toggles_without_generating(runner):
    _make_instance("remoto", mode="network")
    result = runner.invoke(main, ["configure", "remoto", "--allow-network"])
    assert result.exit_code == 0, result.output
    assert mt.read_state(paths.instance_root("remoto")).allow_network is True
    assert mt.read_state(paths.instance_root("remoto")).active is False  # no token minted


# ── manual endpoint/token (OAuth escape hatch) ────────────────────────────


def test_manual_token_and_endpoint_do_not_touch_the_static_token(runner):
    _make_instance("castelo")
    result = runner.invoke(
        main, ["configure", "castelo", "--token", "oauth-access-token-abc",
               "--endpoint", "http://localhost:9999"],
    )
    assert result.exit_code == 0, result.output
    cfg = query_config_module.load_query_config()
    assert cfg.token == "oauth-access-token-abc"
    assert cfg.endpoint == "http://localhost:9999"
    assert mt.read_state(paths.instance_root("castelo")).active is False


def test_mcp_token_and_manual_token_are_mutually_exclusive(runner):
    _make_instance("castelo")
    result = runner.invoke(
        main, ["configure", "castelo", "--mcp-token", "generate", "--token", "abc"],
    )
    assert result.exit_code == 1
    assert "not both" in result.output


# ── show / defaults ────────────────────────────────────────────────────────


def test_show_prints_status_without_mutating_anything(runner):
    _make_instance("castelo")
    result = runner.invoke(main, ["configure", "castelo", "--show"])
    assert result.exit_code == 0, result.output
    assert "OAuth 2.0/PKCE is the default" in result.output
    assert mt.read_state(paths.instance_root("castelo")).active is False


def test_bare_configure_with_no_flags_shows_status(runner):
    _make_instance("castelo")
    result = runner.invoke(main, ["configure", "castelo"])
    assert result.exit_code == 0, result.output
    assert "Instance: castelo" in result.output


def test_unregistered_instance_fails_cleanly(runner):
    result = runner.invoke(main, ["configure", "ghost", "--mcp-token", "generate"])
    assert result.exit_code == 1
    assert "No instance named 'ghost'" in result.output
    assert "Traceback" not in result.output


def test_second_configured_instance_is_not_default(runner):
    _make_instance("castelo")
    _make_instance("segundo", mcp_port=9001)
    runner.invoke(main, ["configure", "castelo", "--mcp-token", "generate"])
    runner.invoke(main, ["configure", "segundo", "--mcp-token", "generate"])
    data = query_config_module.load_raw_config()
    assert data["default"] == "castelo"
    assert query_config_module.load_query_config("segundo").instance == "segundo"
