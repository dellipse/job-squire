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
"""`job-squire uninstall`: remove every instance, optionally the runtime
job-squire itself installed, and the CLI's own venv/PATH entry.

Mirrors test_runtime.py's and test_lifecycle.py's own conventions: no real
subprocess, no real filesystem PATH, and XDG_CONFIG_HOME/JOB_SQUIRE_HOME
both redirected to a tmp_path so nothing here can touch a real machine.
"""
from types import SimpleNamespace

import pytest

from job_squire_cli.ops import registry as reg
from job_squire_cli.ops import runtime as rt
from job_squire_cli.ops import uninstall as un
from job_squire_cli.query import config as query_config_module


@pytest.fixture(autouse=True)
def force_linux_config_dir(monkeypatch):
    monkeypatch.setattr(query_config_module.platform, "system", lambda: "Linux")


@pytest.fixture
def tmp_config_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return tmp_path / "job-squire"


@pytest.fixture
def data_root(tmp_path):
    return tmp_path / "instances"


def which_map(present: dict):
    return lambda name: present.get(name)


def fake_run(ok_prefixes=(), fail_prefixes=()):
    calls = []

    def _run(args, **kwargs):
        calls.append(tuple(args))
        for prefix in ok_prefixes:
            if tuple(args[: len(prefix)]) == tuple(prefix):
                return SimpleNamespace(returncode=0, stdout="", stderr="")
        for prefix in fail_prefixes:
            if tuple(args[: len(prefix)]) == tuple(prefix):
                return SimpleNamespace(returncode=1, stdout="", stderr="boom")
        raise AssertionError(f"unexpected subprocess call in test: {args}")

    _run.calls = calls
    return _run


# ── runtime_uninstall_plan: the reverse of ops/runtime.py's install plans ──


def test_linux_uninstall_plan_matches_the_distro_family_that_installed_it():
    plan = un.runtime_uninstall_plan("podman", system="Linux", os_release={"ID": "ubuntu"})
    assert any("apt-get" in step.command for step in plan.steps)
    assert all(step.use_sudo for step in plan.steps)

    plan = un.runtime_uninstall_plan("podman", system="Linux", os_release={"ID": "fedora"})
    assert any("dnf" in step.command for step in plan.steps)


def test_linux_uninstall_plan_rejects_unknown_distro():
    with pytest.raises(un.UninstallError, match="No packaged Podman uninstall path"):
        un.runtime_uninstall_plan("podman", system="Linux", os_release={"ID": "gentoo"})


def test_linux_uninstall_plan_rejects_a_runtime_it_never_installs():
    with pytest.raises(un.UninstallError, match="docker"):
        un.runtime_uninstall_plan("docker", system="Linux", os_release={"ID": "ubuntu"})


def test_macos_uninstall_plan_podman_stops_and_removes_the_machine_first():
    plan = un.runtime_uninstall_plan("podman", system="Darwin")
    descriptions = [step.description for step in plan.steps]
    assert descriptions.index("Stop the Podman machine VM") < descriptions.index("Remove the Podman machine VM")
    assert descriptions.index("Remove the Podman machine VM") < descriptions.index("Uninstall Podman via Homebrew")


def test_macos_uninstall_plan_orbstack_uses_brew_cask():
    plan = un.runtime_uninstall_plan("orbstack", system="Darwin")
    assert any(("brew", "uninstall", "--cask", "orbstack") == step.command for step in plan.steps)


def test_windows_uninstall_plan_docker_desktop_uses_winget():
    plan = un.runtime_uninstall_plan("docker", system="Windows")
    assert any("winget" in step.command and "Docker.DockerDesktop" in step.command for step in plan.steps)


def test_windows_uninstall_plan_podman_stops_machine_then_winget():
    plan = un.runtime_uninstall_plan("podman", system="Windows")
    descriptions = [step.description for step in plan.steps]
    assert descriptions[0] == "Stop the Podman WSL machine"
    assert descriptions[-1] == "Uninstall Podman via winget"


def test_uninstall_plan_rejects_unsupported_platform():
    with pytest.raises(un.UninstallError, match="Unsupported platform"):
        un.runtime_uninstall_plan("podman", system="Plan9")


# ── looks_like_bootstrap_venv: the self-delete safety gate ─────────────────


def test_looks_like_bootstrap_venv_matches_the_real_layout(tmp_path):
    venv_dir = tmp_path / ".job-squire" / "cli"
    venv_dir.mkdir(parents=True)
    (venv_dir / "pyvenv.cfg").write_text("home = /usr/bin\n")
    assert un.looks_like_bootstrap_venv(venv_dir) is True


def test_looks_like_bootstrap_venv_accepts_undotted_install_dir_name(tmp_path):
    # Windows' %LOCALAPPDATA%\job-squire\cli has no leading dot.
    venv_dir = tmp_path / "job-squire" / "cli"
    venv_dir.mkdir(parents=True)
    (venv_dir / "pyvenv.cfg").write_text("home = C:\\Python312\n")
    assert un.looks_like_bootstrap_venv(venv_dir) is True


def test_looks_like_bootstrap_venv_rejects_missing_pyvenv_cfg(tmp_path):
    # A directory that merely *looks* right by name but isn't a real venv
    # (e.g. someone's unrelated folder) must never be proposed for deletion.
    venv_dir = tmp_path / ".job-squire" / "cli"
    venv_dir.mkdir(parents=True)
    assert un.looks_like_bootstrap_venv(venv_dir) is False


def test_looks_like_bootstrap_venv_rejects_a_system_python(tmp_path):
    venv_dir = tmp_path / "usr" / "lib" / "python3.11"
    venv_dir.mkdir(parents=True)
    (venv_dir / "pyvenv.cfg").write_text("home = /usr/bin\n")
    assert un.looks_like_bootstrap_venv(venv_dir) is False


def test_looks_like_bootstrap_venv_rejects_a_dev_checkout_venv(tmp_path):
    # A developer's `python -m venv .venv` inside a job-squire checkout has
    # the wrong directory name (".venv", not "cli") and must never match.
    venv_dir = tmp_path / "job-squire" / ".venv"
    venv_dir.mkdir(parents=True)
    (venv_dir / "pyvenv.cfg").write_text("home = /usr/bin\n")
    assert un.looks_like_bootstrap_venv(venv_dir) is False


# ── strip_path_line: only ever removes job-squire's own marked line ────────


def test_strip_path_line_removes_only_the_marked_line(tmp_path):
    bin_dir = tmp_path / ".job-squire" / "cli" / "bin"
    rc_file = tmp_path / ".zshrc"
    rc_file.write_text(
        "export EDITOR=vim\n"
        f"export PATH=\"{bin_dir}:$PATH\"  # added by job-squire bootstrap\n"
        "export OTHER=1\n"
    )
    changed = un.strip_path_line(rc_file, bin_dir)
    assert changed is True
    remaining = rc_file.read_text()
    assert "job-squire bootstrap" not in remaining
    assert str(bin_dir) not in remaining
    assert "export EDITOR=vim" in remaining
    assert "export OTHER=1" in remaining


def test_strip_path_line_leaves_an_unrelated_line_naming_the_same_dir_alone(tmp_path):
    bin_dir = tmp_path / ".job-squire" / "cli" / "bin"
    rc_file = tmp_path / ".profile"
    rc_file.write_text(f'export PATH="{bin_dir}:$PATH"  # added by hand, not bootstrap\n')
    changed = un.strip_path_line(rc_file, bin_dir)
    assert changed is False
    assert str(bin_dir) in rc_file.read_text()


def test_strip_path_line_missing_file_is_a_no_op(tmp_path):
    assert un.strip_path_line(tmp_path / "does-not-exist", tmp_path / "bin") is False


def test_strip_path_line_matches_through_a_symlink_when_the_literal_path_differs(tmp_path):
    """`bin_dir` is always derived from `sys.executable`, which can come
    back through a symlink's real target on some platforms/Python builds
    rather than the literal path bootstrap.sh wrote into the rc file. The
    literal-string check alone would silently no-op here; the resolved-path
    fallback must still catch it."""
    real_bin_dir = tmp_path / "real" / ".job-squire" / "cli" / "bin"
    real_bin_dir.mkdir(parents=True)
    symlinked_bin_dir = tmp_path / "linked-bin"
    symlinked_bin_dir.symlink_to(real_bin_dir)

    rc_file = tmp_path / ".zshrc"
    # bootstrap.sh wrote the *symlinked* path literally; sys.executable at
    # uninstall time resolves to the real target instead.
    rc_file.write_text(f'export PATH="{symlinked_bin_dir}:$PATH"  # added by job-squire bootstrap\n')

    changed = un.strip_path_line(rc_file, real_bin_dir)
    assert changed is True
    assert "job-squire bootstrap" not in rc_file.read_text()


def test_strip_path_line_resolved_fallback_still_requires_the_marker(tmp_path):
    real_bin_dir = tmp_path / "real" / ".job-squire" / "cli" / "bin"
    real_bin_dir.mkdir(parents=True)
    symlinked_bin_dir = tmp_path / "linked-bin"
    symlinked_bin_dir.symlink_to(real_bin_dir)

    rc_file = tmp_path / ".profile"
    rc_file.write_text(f'export PATH="{symlinked_bin_dir}:$PATH"  # added by hand, not bootstrap\n')

    changed = un.strip_path_line(rc_file, real_bin_dir)
    assert changed is False
    assert "added by hand" in rc_file.read_text()


# ── uninstall_everything: orchestration ─────────────────────────────────────


def _register(name, data_dir, runtime="docker"):
    reg.add_instance(
        name=name, mode="local", runtime=runtime, data_dir=str(data_dir),
        public_url="http://localhost:8080", app_port=8080, mcp_port=9000,
    )


def test_uninstall_everything_removes_every_instance_and_honors_keep_data(tmp_config_dir, tmp_path):
    one_dir = tmp_path / "one"
    two_dir = tmp_path / "two"
    one_dir.mkdir()
    two_dir.mkdir()
    _register("one", one_dir)
    _register("two", two_dir)

    run = fake_run(ok_prefixes=[("docker", "compose")])
    result = un.uninstall_everything(keep_data=True, run=run, venv_dir=tmp_path / "not-a-venv")

    assert sorted(result.instances_removed) == ["one", "two"]
    assert result.data_kept == {"one": True, "two": True}
    assert one_dir.exists() and two_dir.exists()
    assert reg.list_instances() == []


def test_uninstall_everything_deletes_data_when_keep_data_is_false(tmp_config_dir, tmp_path):
    one_dir = tmp_path / "one"
    one_dir.mkdir()
    _register("one", one_dir)

    run = fake_run(ok_prefixes=[("docker", "compose")])
    result = un.uninstall_everything(keep_data=False, run=run, venv_dir=tmp_path / "not-a-venv")

    assert result.data_kept == {"one": False}
    assert not one_dir.exists()


def test_uninstall_everything_never_touches_runtime_by_default(tmp_config_dir, tmp_path):
    rt.record_runtime_choice("podman", source="installed")
    run = fake_run()  # any subprocess call at all fails the test

    un.uninstall_everything(keep_data=True, run=run, venv_dir=tmp_path / "not-a-venv")
    assert run.calls == []


def test_uninstall_everything_never_removes_a_runtime_it_only_detected(tmp_config_dir, tmp_path):
    rt.record_runtime_choice("podman", source="detected")
    run = fake_run()

    result = un.uninstall_everything(
        keep_data=True, remove_runtime=True, run=run, venv_dir=tmp_path / "not-a-venv",
    )
    assert result.runtime_removed is None
    assert run.calls == []


def test_uninstall_everything_removes_a_runtime_it_installed_when_asked_and_confirmed(tmp_config_dir, tmp_path):
    rt.record_runtime_choice("podman", source="installed")
    run = fake_run(ok_prefixes=[
        ("podman", "machine", "stop"), ("podman", "machine", "rm", "-f"), ("brew", "uninstall", "podman"),
    ])

    result = un.uninstall_everything(
        keep_data=True, remove_runtime=True, confirm_runtime=lambda _msg: True,
        run=run, system="Darwin", venv_dir=tmp_path / "not-a-venv",
    )
    assert result.runtime_removed == "podman"
    assert ("brew", "uninstall", "podman") in run.calls


def test_uninstall_everything_skips_runtime_removal_when_declined(tmp_config_dir, tmp_path):
    rt.record_runtime_choice("podman", source="installed")
    run = fake_run()

    result = un.uninstall_everything(
        keep_data=True, remove_runtime=True, confirm_runtime=lambda _msg: False,
        run=run, system="Darwin", venv_dir=tmp_path / "not-a-venv",
    )
    assert result.runtime_removed is None
    assert run.calls == []


def test_uninstall_everything_clears_the_cli_config_dir(tmp_config_dir, tmp_path):
    rt.record_runtime_choice("docker", source="detected")
    assert tmp_config_dir.exists()

    un.uninstall_everything(keep_data=True, run=fake_run(), venv_dir=tmp_path / "not-a-venv")
    assert not tmp_config_dir.exists()


def test_uninstall_everything_skips_cli_removal_for_a_non_bootstrap_venv(tmp_config_dir, tmp_path):
    result = un.uninstall_everything(keep_data=True, run=fake_run(), venv_dir=tmp_path / "not-a-venv")
    assert result.cli_removed is None
    assert result.rc_files_updated == []


def test_uninstall_everything_removes_its_own_venv_and_path_lines(tmp_config_dir, tmp_path, monkeypatch):
    tmp_config_dir.mkdir(parents=True)  # something for the final cfg-dir cleanup to actually remove

    venv_dir = tmp_path / "home" / ".job-squire" / "cli"
    bin_dir = venv_dir / "bin"
    bin_dir.mkdir(parents=True)
    (venv_dir / "pyvenv.cfg").write_text("home = /usr/bin\n")
    monkeypatch.setattr(un.sys, "executable", str(bin_dir / "python3"))

    rc_file = tmp_path / "home" / ".zshrc"
    rc_file.parent.mkdir(parents=True, exist_ok=True)
    rc_file.write_text(f'export PATH="{bin_dir}:$PATH"  # added by job-squire bootstrap\n')
    monkeypatch.setattr(un.Path, "home", classmethod(lambda cls: tmp_path / "home"))

    removed = []
    result = un.uninstall_everything(
        keep_data=True, run=fake_run(), venv_dir=venv_dir,
        rmtree=lambda p: removed.append(p),
    )

    assert result.cli_removed == venv_dir.parent
    assert removed == [venv_dir.parent, tmp_config_dir]
    assert result.rc_files_updated == [rc_file]
    assert "job-squire bootstrap" not in rc_file.read_text()


def test_uninstall_everything_result_reports_data_kept_per_instance(tmp_config_dir, tmp_path):
    a_dir, b_dir = tmp_path / "a", tmp_path / "b"
    a_dir.mkdir()
    b_dir.mkdir()
    _register("a", a_dir)
    _register("b", b_dir)

    seen = []

    def confirm_delete(msg):
        seen.append(msg)
        return "'a'" in msg  # delete a's data, keep b's

    run = fake_run(ok_prefixes=[("docker", "compose")])
    result = un.uninstall_everything(
        keep_data=None, confirm_delete_data=confirm_delete, run=run, venv_dir=tmp_path / "not-a-venv",
    )
    assert result.data_kept == {"a": False, "b": True}
    assert not a_dir.exists()
    assert b_dir.exists()
