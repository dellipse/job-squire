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
"""The click layer for create/start/stop/restart/status/list/remove
(Prompt C5) -- thin adapter tests only. ops/lifecycle.py's own behavior is
covered exhaustively in tests/test_lifecycle.py with a fully injected
FakeRuntime; here the point is just proving the click commands parse
their options correctly, prompt when a value is missing, and surface
ops/lifecycle.py's exceptions as a clean exit(1) with the right message
on stdout/stderr -- never a traceback.
"""
import click.testing
import pytest

from job_squire_cli.cli import main
from job_squire_cli.ops import lifecycle as lc
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


def test_list_with_no_instances(runner):
    result = runner.invoke(main, ["list"])
    assert result.exit_code == 0
    assert "No instances registered" in result.output


def test_status_unknown_instance_fails_cleanly(runner):
    result = runner.invoke(main, ["status", "ghost"])
    assert result.exit_code == 1
    assert "No instance named 'ghost'" in result.output


def test_start_unregistered_instance_fails_cleanly_not_a_traceback(runner):
    result = runner.invoke(main, ["start", "ghost"])
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "No instance named 'ghost'" in result.output


def test_create_reports_name_collision_cleanly(runner, monkeypatch, tmp_path):
    # Register an instance directly (bypassing the real create flow, which
    # needs a container runtime) so `create` hits the collision path.
    reg.add_instance(
        name="castelo", mode="local", runtime="docker", data_dir=str(tmp_path),
        public_url="http://localhost:8080", app_port=8080, mcp_port=9000,
    )
    result = runner.invoke(main, ["create", "castelo", "--mode", "local", "--yes"])
    assert result.exit_code == 1
    assert "already registered" in result.output
    assert "Traceback" not in result.output


def test_create_network_mode_prompts_for_hostname_when_missing(runner, monkeypatch):
    """create_instance itself is stubbed out here (it would otherwise try
    a real container runtime on whatever machine runs this suite) --
    the point of this test is only that omitting --hostname in network
    mode prompts for one and passes it through."""
    captured = {}

    def fake_create_instance(**kwargs):
        captured.update(kwargs)
        raise lc.LifecycleError("stub: stopped right after prompting, before touching a runtime")

    monkeypatch.setattr(lc, "create_instance", fake_create_instance)
    result = runner.invoke(
        main, ["create", "castelo", "--mode", "network", "--yes"], input="squire.example.com\n",
    )
    assert "Public hostname" in result.output
    assert result.exit_code == 1
    assert captured["hostname"] == "squire.example.com"


def test_remove_reports_not_found_cleanly(runner):
    result = runner.invoke(main, ["remove", "ghost"])
    assert result.exit_code == 1
    assert "No instance named 'ghost'" in result.output


def test_create_surfaces_guard_failure_lines_on_stderr(runner, monkeypatch, tmp_path):
    """Wires a fake create_instance that raises StartupGuardFailure, to
    check the click layer's error rendering in isolation from the real
    (and much larger) create_instance -- that function's own behavior is
    tested directly in test_lifecycle.py."""
    def fake_create_instance(**kwargs):
        raise lc.StartupGuardFailure(["FATAL: PUBLIC_URL='' is unsafe. Fix: set PUBLIC_URL=https://..."])

    monkeypatch.setattr(lc, "create_instance", fake_create_instance)
    result = runner.invoke(main, ["create", "castelo", "--mode", "local", "--yes"])
    assert result.exit_code == 1
    assert "FATAL: PUBLIC_URL" in result.output
    assert "Traceback" not in result.output


# ── update / rollback (Prompt C7) ────────────────────────────────────────


def test_update_unregistered_instance_fails_cleanly(runner):
    result = runner.invoke(main, ["update", "ghost"])
    assert result.exit_code == 1
    assert "No instance named 'ghost'" in result.output
    assert "Traceback" not in result.output


def test_update_reports_the_image_move(runner, monkeypatch):
    captured = {}

    def fake_update_instance(name, *, version):
        captured["name"] = name
        captured["version"] = version
        return lc.UpdateResult(
            instance=None, previous_image="ghcr.io/dellipse/job-squire:latest",
            new_image="ghcr.io/dellipse/job-squire:0.7.0", health={"Status": "running"},
        )

    monkeypatch.setattr(lc, "update_instance", fake_update_instance)
    result = runner.invoke(main, ["update", "castelo", "--version", "0.7.0"])
    assert result.exit_code == 0
    assert captured == {"name": "castelo", "version": "0.7.0"}
    assert "latest -> " in result.output and "0.7.0" in result.output


def test_update_defaults_version_to_latest(runner, monkeypatch):
    captured = {}

    def fake_update_instance(name, *, version):
        captured["version"] = version
        return lc.UpdateResult(instance=None, previous_image="a", new_image="b", health=None)

    monkeypatch.setattr(lc, "update_instance", fake_update_instance)
    result = runner.invoke(main, ["update", "castelo"])
    assert result.exit_code == 0
    assert captured["version"] == "latest"


def test_update_rollback_flag_calls_rollback_not_update(runner, monkeypatch):
    calls = []
    monkeypatch.setattr(lc, "update_instance", lambda *a, **k: calls.append(("update", a, k)))
    monkeypatch.setattr(
        lc, "rollback_instance",
        lambda name: lc.UpdateResult(instance=None, previous_image="new", new_image="old", health=None),
    )
    result = runner.invoke(main, ["update", "castelo", "--rollback"])
    assert result.exit_code == 0
    assert calls == []  # update_instance never called
    assert "rolled back" in result.output


def test_update_rejects_version_and_rollback_together(runner):
    result = runner.invoke(main, ["update", "castelo", "--version", "0.7.0", "--rollback"])
    assert result.exit_code == 1
    assert "not both" in result.output


def test_update_surfaces_lifecycle_error_cleanly(runner, monkeypatch):
    def fake_update_instance(name, *, version):
        raise lc.LifecycleError("Failed to pull 'ghcr.io/dellipse/job-squire:bogus': not found")

    monkeypatch.setattr(lc, "update_instance", fake_update_instance)
    result = runner.invoke(main, ["update", "castelo", "--version", "bogus"])
    assert result.exit_code == 1
    assert "Failed to pull" in result.output
    assert "Traceback" not in result.output


# ── adopt (Prompt C7) ────────────────────────────────────────────────────


def test_adopt_missing_install_dir_fails_cleanly(runner, tmp_path):
    result = runner.invoke(main, ["adopt", str(tmp_path / "does-not-exist")])
    assert result.exit_code != 0
    assert "Traceback" not in result.output


def test_adopt_reports_success_and_env_changes(runner, monkeypatch, tmp_path):
    captured = {}

    def fake_adopt_instance(install_dir, *, name, image, bring_up, confirm):
        captured.update(install_dir=install_dir, name=name, bring_up=bring_up)
        inst = reg.Instance(
            name="castelo", mode="local", runtime="docker", data_dir=str(install_dir),
            app_port=8080, mcp_port=9000, cookie_name="castelo_session",
            public_url="http://localhost:8080", created="2026-07-11",
        )
        return lc.AdoptResult(
            instance=inst, cookie_name="castelo_session",
            env_appended=["TRUST_PROXY=1"], env_backup=tmp_path / "install" / "data" / ".env.bak.x",
            health=None,
        )

    monkeypatch.setattr(lc, "adopt_instance", fake_adopt_instance)
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    result = runner.invoke(main, ["adopt", str(install_dir), "--no-up"])
    assert result.exit_code == 0
    assert captured["bring_up"] is False
    assert "adopted from" in result.output
    assert "castelo_session" in result.output
    assert "TRUST_PROXY=1" in result.output
    assert "Not brought up yet" in result.output


def test_adopt_surfaces_not_a_legacy_install_error_cleanly(runner, monkeypatch, tmp_path):
    def fake_adopt_instance(install_dir, *, name, image, bring_up, confirm):
        raise lc.NotALegacyInstallError(f"No data/.env found at {install_dir}/data/.env")

    monkeypatch.setattr(lc, "adopt_instance", fake_adopt_instance)
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    result = runner.invoke(main, ["adopt", str(install_dir), "--no-up"])
    assert result.exit_code == 1
    assert "No data/.env found" in result.output
    assert "Traceback" not in result.output
