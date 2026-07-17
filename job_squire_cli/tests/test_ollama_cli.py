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
"""The click layer for `job-squire ollama check`/`setup` (docs/PLAN-ollama-
assist.md). Thin-adapter tests only, same convention as
test_ops_commands.py -- ops/ollama_assist.py's own behavior is covered
exhaustively in tests/test_ollama_assist.py with fully injected run/which.
"""
import click.testing
import pytest

from job_squire_cli.cli import main
from job_squire_cli.ops import commands as cmds
from job_squire_cli.ops import ollama_assist as oa
from job_squire_cli.ops import registry as reg
from job_squire_cli.query import config as query_config_module


@pytest.fixture(autouse=True)
def force_linux_config_dir(monkeypatch):
    monkeypatch.setattr(query_config_module.platform, "system", lambda: "Linux")


@pytest.fixture(autouse=True)
def tmp_registry(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))


@pytest.fixture
def runner():
    return click.testing.CliRunner()


def _fake_caps(**overrides):
    base = dict(
        detected_at="2026-07-16T00:00:00Z", os="Darwin", apple_silicon=True, ram_gb=16.0,
        cpu_cores=10, gpu_vendor="apple", gpu_vram_gb=None, ollama_installed=True, ollama_running=True,
    )
    base.update(overrides)
    return oa.HostCapabilities(**base)


def test_ollama_check_prints_capabilities_and_tier(runner, monkeypatch):
    monkeypatch.setattr(cmds.ollama_assist, "detect_host_capabilities", lambda: _fake_caps())
    result = runner.invoke(main, ["ollama", "check"])
    assert result.exit_code == 0
    assert "Darwin" in result.output
    assert "Apple Silicon" in result.output
    assert "Tier: strong" in result.output
    assert "gemma4:12b" in result.output


def test_ollama_check_not_reasonable_shows_explanation_not_a_tier(runner, monkeypatch):
    monkeypatch.setattr(
        cmds.ollama_assist, "detect_host_capabilities",
        lambda: _fake_caps(apple_silicon=False, ram_gb=4.0, gpu_vendor=None, ollama_installed=False,
                            ollama_running=False),
    )
    result = runner.invoke(main, ["ollama", "check"])
    assert result.exit_code == 0
    assert "Tier:" not in result.output
    assert "cloud tier" in result.output.lower()


def test_ollama_check_with_name_writes_host_capabilities(runner, monkeypatch, tmp_path):
    reg.add_instance(
        name="castelo", mode="local", runtime="docker", data_dir=str(tmp_path),
        public_url="http://localhost:8080", app_port=8080, mcp_port=9000,
    )
    monkeypatch.setattr(cmds.ollama_assist, "detect_host_capabilities", lambda: _fake_caps())
    result = runner.invoke(main, ["ollama", "check", "castelo"])
    assert result.exit_code == 0
    assert "Wrote" in result.output
    assert (tmp_path / "data" / oa.HOST_CAPABILITIES_FILENAME).exists()


def test_ollama_check_unknown_instance_fails_cleanly(runner, monkeypatch):
    monkeypatch.setattr(cmds.ollama_assist, "detect_host_capabilities", lambda: _fake_caps())
    result = runner.invoke(main, ["ollama", "check", "nope"])
    assert result.exit_code == 1
    assert "No instance named" in result.output
    assert "Traceback" not in result.output


def test_ollama_setup_dry_run_calls_run_setup_with_dry_run_true(runner, monkeypatch, tmp_path):
    reg.add_instance(
        name="castelo", mode="local", runtime="docker", data_dir=str(tmp_path),
        public_url="http://localhost:8080", app_port=8080, mcp_port=9000,
    )
    captured = {}

    def fake_run_setup(root, **kwargs):
        captured["root"] = root
        captured.update(kwargs)
        return oa.SetupResult(
            capabilities=_fake_caps(), tier=oa.TIER_STRONG, recommendation=oa.TIER_TABLE[oa.TIER_STRONG],
            host_capabilities_path=None, models_pulled=[], models_derived={}, num_ctx=None,
            provider_configured=False, roundtrip_ok=None, roundtrip_detail=None,
        )

    monkeypatch.setattr(cmds.ollama_assist, "run_setup", fake_run_setup)
    result = runner.invoke(main, ["ollama", "setup", "castelo", "--dry-run"])
    assert result.exit_code == 0
    assert captured["dry_run"] is True
    assert str(captured["root"]) == str(tmp_path)
    assert "Dry run only" in result.output


def test_ollama_setup_reports_failed_roundtrip(runner, monkeypatch, tmp_path):
    reg.add_instance(
        name="castelo", mode="local", runtime="docker", data_dir=str(tmp_path),
        public_url="http://localhost:8080", app_port=8080, mcp_port=9000,
    )

    def fake_run_setup(root, **kwargs):
        return oa.SetupResult(
            capabilities=_fake_caps(), tier=oa.TIER_STRONG, recommendation=oa.TIER_TABLE[oa.TIER_STRONG],
            host_capabilities_path=tmp_path / "data" / "host_capabilities.json",
            models_pulled=["qwen3:8b", "gemma4:12b"],
            models_derived={"qwen3:8b": "qwen3:8b-ctx16384", "gemma4:12b": "gemma4:12b-ctx16384"},
            num_ctx=16384, provider_configured=True,
            roundtrip_ok=False, roundtrip_detail="connection refused",
        )

    monkeypatch.setattr(cmds.ollama_assist, "run_setup", fake_run_setup)
    result = runner.invoke(main, ["ollama", "setup", "castelo"])
    assert result.exit_code == 0
    assert "Configured Ollama provider for 'castelo'" in result.output
    assert "Created qwen3:8b-ctx16384 (num_ctx=16384, from qwen3:8b)" in result.output
    assert "FAILED -- connection refused" in result.output


def test_ollama_setup_surfaces_ollama_assist_error_as_clean_exit(runner, monkeypatch, tmp_path):
    reg.add_instance(
        name="castelo", mode="local", runtime="docker", data_dir=str(tmp_path),
        public_url="http://localhost:8080", app_port=8080, mcp_port=9000,
    )

    def fake_run_setup(root, **kwargs):
        raise oa.OllamaAssistError("Ollama is not installed and installation was declined.")

    monkeypatch.setattr(cmds.ollama_assist, "run_setup", fake_run_setup)
    result = runner.invoke(main, ["ollama", "setup", "castelo", "--yes"])
    assert result.exit_code == 1
    assert "installation was declined" in result.output
    assert "Traceback" not in result.output


def test_ollama_setup_unknown_instance_fails_cleanly(runner):
    result = runner.invoke(main, ["ollama", "setup", "nope"])
    assert result.exit_code == 1
    assert "No instance named" in result.output


def test_ollama_help_lists_check_and_setup(runner):
    result = runner.invoke(main, ["ollama", "--help"])
    assert result.exit_code == 0
    assert "check" in result.output
    assert "setup" in result.output
