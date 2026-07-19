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
"""Container runtime detection and per-OS install.

Detection and install must never touch a real subprocess or the real
filesystem PATH -- every test injects a fake `run`/`which` pair. The
recording tests pin config_dir() to a temp XDG directory the same way
test_query_config.py does, since runtime.py reuses that exact helper.
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from job_squire_cli.ops import runtime as rt
from job_squire_cli.query import config as query_config_module


@pytest.fixture(autouse=True)
def force_linux_config_dir(monkeypatch):
    """Pin config_dir() to its Linux/XDG branch, as test_query_config.py does.

    (query.config's `platform` name *is* the stdlib platform module, so
    patching it here also affects runtime.py's own `platform.system()`
    calls in ensure_runtime -- each OS-specific test overrides that
    directly via the `system=` kwarg instead of relying on this fixture.)
    """
    monkeypatch.setattr(query_config_module.platform, "system", lambda: "Linux")


@pytest.fixture
def tmp_config_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return tmp_path / "job-squire"


def fake_run(ok_prefixes=(), fail_prefixes=(), raise_prefixes=()):
    """Build a fake `subprocess.run` replacement.

    ok_prefixes / fail_prefixes / raise_prefixes are tuples of argv
    prefixes (as tuples of str). Any call whose argv starts with one of
    ok_prefixes returns returncode 0; fail_prefixes returns returncode 1;
    raise_prefixes raises FileNotFoundError (binary not actually runnable).
    Anything unmatched fails the test loudly via AssertionError so a stray
    subprocess call in test setup is never silently forgiving.
    """
    calls = []

    def _run(args, **kwargs):
        calls.append(tuple(args))
        for prefix in raise_prefixes:
            if tuple(args[: len(prefix)]) == tuple(prefix):
                raise FileNotFoundError(args[0])
        for prefix in ok_prefixes:
            if tuple(args[: len(prefix)]) == tuple(prefix):
                return SimpleNamespace(returncode=0)
        for prefix in fail_prefixes:
            if tuple(args[: len(prefix)]) == tuple(prefix):
                return SimpleNamespace(returncode=1)
        raise AssertionError(f"unexpected subprocess call in test: {args}")

    _run.calls = calls
    return _run


def which_map(present: dict):
    return lambda name: present.get(name)


# ── detect_working_runtime ────────────────────────────────────────────────


def test_detect_reuses_existing_docker_and_checks_nothing_else_first():
    which = which_map({"docker": "/usr/bin/docker"})
    run = fake_run(ok_prefixes=[("docker", "info")])
    assert rt.detect_working_runtime(run=run, which=which) == "docker"


def test_detect_skips_present_but_not_running_binary_and_tries_next():
    # docker is on PATH but its daemon isn't up; podman is on PATH and works.
    which = which_map({"docker": "/usr/bin/docker", "podman": "/usr/bin/podman"})
    run = fake_run(ok_prefixes=[("podman", "info")], fail_prefixes=[("docker", "info")])
    assert rt.detect_working_runtime(run=run, which=which) == "podman"


def test_detect_tries_both_orbstack_binary_aliases():
    which = which_map({"orb": "/usr/local/bin/orb"})
    run = fake_run(ok_prefixes=[("orb", "status")])
    assert rt.detect_working_runtime(run=run, which=which) == "orbstack"


def test_detect_returns_none_when_nothing_works():
    which = which_map({})
    run = fake_run()
    assert rt.detect_working_runtime(run=run, which=which) is None


def test_detect_never_installs_anything():
    """The whole point of detect-and-reuse: if a runtime already works,
    no install command is ever issued."""
    which = which_map({"podman": "/usr/bin/podman"})
    run = fake_run(ok_prefixes=[("podman", "info")])
    rt.detect_working_runtime(run=run, which=which)
    assert run.calls == [("podman", "info")]


# ── Linux install plan ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "os_release,expect_manager",
    [
        ({"ID": "ubuntu", "ID_LIKE": "debian"}, "apt-get"),
        ({"ID": "debian"}, "apt-get"),
        ({"ID": "fedora"}, "dnf"),
        ({"ID": "rocky", "ID_LIKE": "rhel fedora"}, "dnf"),
        ({"ID": "arch"}, "pacman"),
    ],
)
def test_linux_install_plan_picks_package_manager_by_os_release(os_release, expect_manager):
    plan = rt.linux_install_plan(os_release)
    assert plan.runtime == "podman"
    assert any(expect_manager in step.command for step in plan.steps)
    assert all(step.use_sudo for step in plan.steps)


def test_linux_install_plan_never_targets_docker():
    for os_release in ({"ID": "ubuntu"}, {"ID": "fedora"}, {"ID": "arch"}):
        plan = rt.linux_install_plan(os_release)
        assert plan.runtime == "podman"
        assert all("docker" not in step.command for step in plan.steps)


def test_linux_install_plan_raises_for_unknown_distro():
    with pytest.raises(rt.RuntimeSelectionError, match="podman.io"):
        rt.linux_install_plan({"ID": "solaris"})


def test_read_os_release_parses_real_shaped_file(tmp_path):
    p = tmp_path / "os-release"
    p.write_text('ID=ubuntu\nID_LIKE=debian\nPRETTY_NAME="Ubuntu 24.04 LTS"\n# a comment\n')
    data = rt.read_os_release(p)
    assert data["ID"] == "ubuntu"
    assert data["ID_LIKE"] == "debian"
    assert data["PRETTY_NAME"] == "Ubuntu 24.04 LTS"


def test_read_os_release_missing_file_returns_empty():
    assert rt.read_os_release(Path("/nonexistent/os-release-for-test")) == {}


# ── macOS install plan ─────────────────────────────────────────────────────


def test_macos_default_is_podman_no_license_notice():
    plan = rt.macos_install_plan()
    assert plan.runtime == "podman"
    assert plan.license_notice is None
    assert any("machine" in step.command for step in plan.steps)


def test_macos_orbstack_is_opt_in_with_license_notice():
    plan = rt.macos_install_plan(use_orbstack=True)
    assert plan.runtime == "orbstack"
    assert plan.license_notice == rt.ORBSTACK_LICENSE_NOTICE
    assert "commercial" in plan.license_notice.lower()


# ── Windows: WSL2 prerequisite ─────────────────────────────────────────────


def test_check_wsl2_missing_binary_guides_install():
    has_wsl2, guidance = rt.check_wsl2(run=fake_run(), which=which_map({}))
    assert has_wsl2 is False
    assert "wsl --install" in guidance


def test_check_wsl2_present_but_unhealthy():
    which = which_map({"wsl": "C:\\Windows\\System32\\wsl.exe"})
    run = fake_run(fail_prefixes=[("wsl", "--status")])
    has_wsl2, guidance = rt.check_wsl2(run=run, which=which)
    assert has_wsl2 is False
    assert "wsl --install" in guidance


def test_check_wsl2_healthy():
    which = which_map({"wsl": "C:\\Windows\\System32\\wsl.exe"})
    run = fake_run(ok_prefixes=[("wsl", "--status")])
    has_wsl2, guidance = rt.check_wsl2(run=run, which=which)
    assert has_wsl2 is True
    assert guidance == ""


# ── Windows install plan ────────────────────────────────────────────────────


def test_windows_default_is_podman_no_license_notice():
    plan = rt.windows_install_plan()
    assert plan.runtime == "podman"
    assert plan.license_notice is None


def test_windows_docker_desktop_is_fallback_with_license_notice():
    plan = rt.windows_install_plan(use_docker_desktop=True)
    assert plan.runtime == "docker"
    assert plan.license_notice == rt.DOCKER_DESKTOP_LICENSE_NOTICE
    assert "250 employees" in plan.license_notice


# ── record / load runtime choice ────────────────────────────────────────────


def test_record_and_load_runtime_choice_round_trips(tmp_config_dir):
    path = rt.record_runtime_choice("podman", source="detected")
    assert path == tmp_config_dir / rt.RUNTIME_STATE_FILENAME
    loaded = rt.load_runtime_choice()
    assert loaded["runtime"] == "podman"
    assert loaded["source"] == "detected"
    assert "recorded_at" in loaded


def test_load_runtime_choice_missing_file_returns_none(tmp_config_dir):
    assert rt.load_runtime_choice() is None


def test_record_runtime_choice_never_writes_a_secret_looking_field(tmp_config_dir):
    path = rt.record_runtime_choice("docker", source="installed")
    payload = json.loads(path.read_text())
    assert set(payload.keys()) == {"runtime", "source", "recorded_at"}


# ── ensure_runtime orchestration ────────────────────────────────────────────


def test_ensure_runtime_reuses_existing_and_installs_nothing(tmp_config_dir):
    which = which_map({"podman": "/usr/bin/podman"})
    run = fake_run(ok_prefixes=[("podman", "info")])
    result = rt.ensure_runtime(system="Linux", run=run, which=which, confirm=lambda _: True)
    assert result == "podman"
    # Only the detection probe ran -- no install command was ever issued.
    assert run.calls == [("podman", "info")]
    assert rt.load_runtime_choice()["source"] == "detected"


def test_ensure_runtime_linux_installs_with_consent(tmp_config_dir, monkeypatch):
    monkeypatch.setattr(rt, "read_os_release", lambda path=None: {"ID": "ubuntu"})
    run = fake_run(ok_prefixes=[("sudo", "apt-get", "install", "-y", "podman"), ("podman", "info")])

    def which_progression(name):
        # No runtime exists until after the install step has actually run.
        installed = any(c[:4] == ("sudo", "apt-get", "install", "-y") for c in run.calls)
        return "/usr/bin/podman" if (name == "podman" and installed) else None

    result = rt.ensure_runtime(
        system="Linux",
        run=run,
        which=which_progression,
        confirm=lambda _: True,
    )
    assert result == "podman"
    assert run.calls[0] == ("sudo", "apt-get", "install", "-y", "podman")
    assert rt.load_runtime_choice()["source"] == "installed"


def test_ensure_runtime_linux_declines_consent_installs_nothing(tmp_config_dir, monkeypatch):
    monkeypatch.setattr(rt, "read_os_release", lambda path=None: {"ID": "ubuntu"})
    which = which_map({})
    run = fake_run()  # any subprocess call here is a test failure

    with pytest.raises(rt.RuntimeSelectionError, match="declined"):
        rt.ensure_runtime(system="Linux", run=run, which=which, confirm=lambda _: False)
    assert run.calls == []
    assert rt.load_runtime_choice() is None


def test_ensure_runtime_windows_missing_wsl2_never_prompts_or_installs(tmp_config_dir):
    which = which_map({})
    run = fake_run()

    with pytest.raises(rt.RuntimeSelectionError, match="wsl --install"):
        rt.ensure_runtime(
            system="Windows", run=run, which=which, confirm=lambda _: True
        )
    assert run.calls == []
    assert rt.load_runtime_choice() is None


def test_ensure_runtime_macos_orbstack_opt_in_shows_license_and_records_choice(
    tmp_config_dir, capsys
):
    # Detection must find orbstack only after both install steps have run.
    state = {"installed": False}

    def run_and_mark(args, **kwargs):
        if tuple(args[:2]) in (("brew", "install"), ("open", "-a")):
            state["installed"] = True
        return SimpleNamespace(returncode=0)

    def which_after_install(name):
        if state["installed"] and name == "orbctl":
            return "/usr/local/bin/orbctl"
        return None

    result = rt.ensure_runtime(
        system="Darwin",
        prefer_orbstack=True,
        run=run_and_mark,
        which=which_after_install,
        confirm=lambda _: True,
    )
    assert result == "orbstack"
    out = capsys.readouterr().out
    assert rt.ORBSTACK_LICENSE_NOTICE in out
    assert rt.load_runtime_choice()["runtime"] == "orbstack"


def test_ensure_runtime_raises_if_install_reports_success_but_still_unhealthy(tmp_config_dir):
    which = which_map({})
    run = fake_run(ok_prefixes=[("brew", "install"), ("podman", "machine")])

    with pytest.raises(rt.RuntimeSelectionError, match="not reporting healthy"):
        rt.ensure_runtime(system="Darwin", run=run, which=which, confirm=lambda _: True)
