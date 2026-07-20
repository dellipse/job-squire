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
"""Tailscale install/login/uninstall (ensure_tailscale_ready and friends).

Detection, install, and login must never touch a real subprocess or the
real filesystem PATH -- every test injects a fake `run`/`which` pair, same
philosophy as test_runtime.py (which this module's install/uninstall
plumbing directly mirrors) and test_proxy.py (whose prefix-matching
`FakeRun` this file reuses, since `device_dns_name` needs real JSON
stdout, not just a bare returncode the way test_runtime.py's simpler fake
gets away with).
"""
import json
from types import SimpleNamespace

import pytest

from job_squire_cli.ops import tailscale as ts
from job_squire_cli.query import config as query_config_module


@pytest.fixture(autouse=True)
def force_linux_config_dir(monkeypatch):
    """Pin config_dir() to its Linux/XDG branch -- see test_runtime.py's
    identical fixture for why patching query.config's `platform` also
    covers this module's own `platform.system()` calls (each OS-specific
    test overrides that directly via `system=` instead)."""
    monkeypatch.setattr(query_config_module.platform, "system", lambda: "Linux")


@pytest.fixture
def tmp_config_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return tmp_path / "job-squire"


class FakeRun:
    """Matches subprocess calls by argv prefix (longest match wins) against
    canned `(returncode, stdout, stderr)` responses -- same as test_proxy.py's
    own FakeRun. Any unmatched call fails the test loudly."""

    def __init__(self):
        self.responses: list[tuple[tuple[str, ...], SimpleNamespace]] = []
        self.calls: list[tuple[str, ...]] = []

    def on(self, prefix, *, returncode=0, stdout="", stderr=""):
        self.responses.append((tuple(prefix), SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)))
        return self

    def __call__(self, args, **kwargs):
        args = tuple(args)
        self.calls.append(args)
        best = None
        for prefix, response in self.responses:
            if args[: len(prefix)] == prefix and (best is None or len(prefix) > len(best[0])):
                best = (prefix, response)
        if best is None:
            raise AssertionError(f"unexpected subprocess call in test: {args}")
        return best[1]


def which_map(present: dict):
    return lambda name: present.get(name)


def _status_json(dns_name="castelo.tail1234.ts.net."):
    return json.dumps({"Self": {"DNSName": dns_name}})


# ── is_tailscale_installed ───────────────────────────────────────────────


def test_is_tailscale_installed_true_when_on_path():
    assert ts.is_tailscale_installed(which=which_map({"tailscale": "/usr/bin/tailscale"})) is True


def test_is_tailscale_installed_false_when_not_on_path():
    assert ts.is_tailscale_installed(which=which_map({})) is False


# ── record / load tailscale choice ───────────────────────────────────────


def test_record_and_load_tailscale_choice_round_trips(tmp_config_dir):
    path = ts.record_tailscale_choice(source="installed")
    assert path == tmp_config_dir / ts.INSTALL_STATE_FILENAME
    loaded = ts.load_tailscale_choice()
    assert loaded["source"] == "installed"
    assert "recorded_at" in loaded


def test_load_tailscale_choice_missing_file_returns_none(tmp_config_dir):
    assert ts.load_tailscale_choice() is None


def test_record_tailscale_choice_if_unset_writes_when_nothing_recorded(tmp_config_dir):
    ts.record_tailscale_choice_if_unset(source="detected")
    assert ts.load_tailscale_choice()["source"] == "detected"


def test_record_tailscale_choice_if_unset_never_downgrades_an_installed_record(tmp_config_dir):
    """The one deliberate divergence from ops/runtime.py's own
    `record_runtime_choice`, which unconditionally re-stamps "detected"
    every time a working runtime is found -- see this function's own
    docstring for why that would silently lose the "installed by
    job-squire" fact `remove`/`uninstall` depend on."""
    ts.record_tailscale_choice(source="installed")
    ts.record_tailscale_choice_if_unset(source="detected")
    assert ts.load_tailscale_choice()["source"] == "installed"


# ── install / uninstall plans ────────────────────────────────────────────


def test_tailscale_install_plan_macos_uses_homebrew():
    plan = ts.tailscale_install_plan("Darwin")
    assert plan.steps[0].command == ("brew", "install", "--cask", "tailscale")


def test_tailscale_install_plan_linux_uses_official_script():
    plan = ts.tailscale_install_plan("Linux")
    assert plan.steps[0].command == ("sh", "-c", "curl -fsSL https://tailscale.com/install.sh | sh")


def test_tailscale_install_plan_windows_uses_winget():
    plan = ts.tailscale_install_plan("Windows")
    assert plan.steps[0].command == ("winget", "install", "-e", "--id", "tailscale.tailscale")


def test_tailscale_install_plan_unsupported_platform_raises():
    with pytest.raises(ts.TailscaleError, match="Unsupported platform"):
        ts.tailscale_install_plan("Plan9")


def test_tailscale_uninstall_plan_macos_uses_homebrew():
    plan = ts.tailscale_uninstall_plan("Darwin")
    assert plan.steps[0].command == ("brew", "uninstall", "--cask", "tailscale")


def test_tailscale_uninstall_plan_windows_uses_winget():
    plan = ts.tailscale_uninstall_plan("Windows")
    assert plan.steps[0].command == ("winget", "uninstall", "-e", "--id", "tailscale.tailscale")


def test_tailscale_uninstall_plan_linux_dispatches_by_distro():
    # tailscale_uninstall_plan("Linux") has no os_release override here --
    # read_os_release() reads the real /etc/os-release, which may or may not
    # exist in the test environment, so exercise the distro branching
    # directly instead via _linux_uninstall_plan.
    plan = ts._linux_uninstall_plan({"ID": "ubuntu"})
    assert plan.steps[0].command == ("apt-get", "remove", "-y", "tailscale")
    assert plan.steps[0].use_sudo is True

    plan = ts._linux_uninstall_plan({"ID": "fedora"})
    assert plan.steps[0].command == ("dnf", "remove", "-y", "tailscale")

    plan = ts._linux_uninstall_plan({"ID": "arch"})
    assert plan.steps[0].command == ("pacman", "-R", "--noconfirm", "tailscale")


def test_tailscale_uninstall_plan_linux_unknown_distro_raises():
    with pytest.raises(ts.TailscaleError, match="No packaged Tailscale uninstall path"):
        ts._linux_uninstall_plan({"ID": "gentoo"})


# ── remove_tailscale ──────────────────────────────────────────────────────


def test_remove_tailscale_runs_the_uninstall_plan():
    run = FakeRun().on(("brew", "uninstall", "--cask", "tailscale"), returncode=0)
    ts.remove_tailscale(system="Darwin", run=run)
    assert ("brew", "uninstall", "--cask", "tailscale") in run.calls


def test_remove_tailscale_raises_tailscale_error_on_step_failure():
    """`run_install_plan` (reused from ops/runtime.py) raises its own
    `RuntimeSelectionError` on a failed step -- `remove_tailscale` must
    translate that to `TailscaleError`, since ops/commands.py's try/except
    around it only ever catches that (see `_run_install_plan`'s docstring)."""
    run = FakeRun().on(("brew", "uninstall", "--cask", "tailscale"), returncode=1, stderr="boom")
    with pytest.raises(ts.TailscaleError):
        ts.remove_tailscale(system="Darwin", run=run)


# ── ensure_tailscale_ready ────────────────────────────────────────────────


def test_ensure_tailscale_ready_already_installed_and_logged_in(tmp_config_dir):
    which = which_map({"tailscale": "/usr/bin/tailscale"})
    run = FakeRun().on(("tailscale", "status", "--json"), stdout=_status_json())

    result = ts.ensure_tailscale_ready(confirm=lambda _: False, run=run, which=which)
    assert result.hostname == "castelo.tail1234.ts.net"
    assert result.installed_by_cli is False
    # Only the status probe ran -- no install, no `tailscale up`.
    assert run.calls == [("tailscale", "status", "--json")]
    assert ts.load_tailscale_choice()["source"] == "detected"


def test_ensure_tailscale_ready_does_not_downgrade_a_prior_installed_record(tmp_config_dir):
    ts.record_tailscale_choice(source="installed")
    which = which_map({"tailscale": "/usr/bin/tailscale"})
    run = FakeRun().on(("tailscale", "status", "--json"), stdout=_status_json())

    result = ts.ensure_tailscale_ready(confirm=lambda _: False, run=run, which=which)
    assert result.installed_by_cli is True
    assert ts.load_tailscale_choice()["source"] == "installed"


def test_ensure_tailscale_ready_installs_with_consent_then_is_already_logged_in(tmp_config_dir):
    run = (
        FakeRun()
        .on(("sh", "-c", "curl -fsSL https://tailscale.com/install.sh | sh"), returncode=0)
        .on(("sudo", "tailscale", "set"), returncode=0)
        .on(("tailscale", "status", "--json"), stdout=_status_json())
    )

    def which_progression(name):
        installed = any(c[:2] == ("sh", "-c") for c in run.calls)
        return "/usr/bin/tailscale" if (name == "tailscale" and installed) else None

    result = ts.ensure_tailscale_ready(system="Linux", confirm=lambda _: True, run=run, which=which_progression)
    assert result.installed_by_cli is True
    assert result.hostname == "castelo.tail1234.ts.net"
    assert ts.load_tailscale_choice()["source"] == "installed"


def test_ensure_tailscale_ready_declines_install_raises(tmp_config_dir):
    run = FakeRun()  # any subprocess call is a test failure
    with pytest.raises(ts.TailscaleError, match="installation was declined"):
        ts.ensure_tailscale_ready(system="Linux", confirm=lambda _: False, run=run, which=which_map({}))
    assert run.calls == []
    assert ts.load_tailscale_choice() is None


def test_ensure_tailscale_ready_install_reports_success_but_still_not_on_path(tmp_config_dir):
    run = FakeRun().on(("sh", "-c", "curl -fsSL https://tailscale.com/install.sh | sh"), returncode=0)
    with pytest.raises(ts.TailscaleError, match="isn't on PATH yet"):
        ts.ensure_tailscale_ready(system="Linux", confirm=lambda _: True, run=run, which=which_map({}))


def test_ensure_tailscale_ready_installed_but_not_logged_in_walks_through_tailscale_up(tmp_config_dir):
    which = which_map({"tailscale": "/usr/bin/tailscale"})
    run = FakeRun()
    calls_before_up = {"done": False}

    def dispatch(args, **kwargs):
        args = tuple(args)
        run.calls.append(args)
        if args == ("tailscale", "status", "--json"):
            if calls_before_up["done"]:
                return SimpleNamespace(returncode=0, stdout=_status_json(), stderr="")
            return SimpleNamespace(returncode=1, stdout="", stderr="not logged in")
        if args == ("tailscale", "up"):
            calls_before_up["done"] = True
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected subprocess call in test: {args}")

    result = ts.ensure_tailscale_ready(confirm=lambda _: True, run=dispatch, which=which)
    assert result.hostname == "castelo.tail1234.ts.net"
    assert ("tailscale", "up") in run.calls


def test_ensure_tailscale_ready_declines_login_raises(tmp_config_dir):
    which = which_map({"tailscale": "/usr/bin/tailscale"})
    run = FakeRun().on(("tailscale", "status", "--json"), returncode=1, stderr="not logged in")

    def boom(*args, **kwargs):
        raise AssertionError("`tailscale up` should not run once the login offer is declined")

    with pytest.raises(ts.TailscaleError, match="logged into a tailnet"):
        ts.ensure_tailscale_ready(confirm=lambda _: False, run=run, which=which)


# ── check_client_health ──────────────────────────────────────────────────
# Exists to give an operator (via `job-squire tailscale status`, no NAME
# required) a direct read of every layer `enable_serve_port` depends on --
# see ops/tailscale.py's own docstring on the 2026-07-19 incident that
# prompted it: a failed `enable` offered only the last CLI error, with no
# way to see whether the daemon itself was even reachable.


def _full_status_json(*, backend_state="Running", online=True, dns_name="castelo.tail1234.ts.net.",
                       magicdns=True, cert_domains=("castelo.tail1234.ts.net",)):
    return json.dumps({
        "BackendState": backend_state,
        "Self": {"Online": online, "DNSName": dns_name},
        "CurrentTailnet": {"MagicDNSEnabled": magicdns},
        "CertDomains": list(cert_domains) if cert_domains is not None else None,
    })


def test_check_client_health_not_installed():
    health = ts.check_client_health(which=which_map({}))
    assert health.installed is False
    assert health.daemon_reachable is False
    assert "not installed" in health.detail


def test_check_client_health_daemon_unreachable_reports_detail_and_hint():
    def boom(*args, **kwargs):
        raise OSError("lost connection to tailscaled: unexpected EOF")

    health = ts.check_client_health(run=boom, which=which_map({"tailscale": "/usr/bin/tailscale"}))
    assert health.installed is True
    assert health.daemon_reachable is False
    assert "lost connection to tailscaled" in health.detail
    assert "System Settings" in health.detail


def test_check_client_health_happy_path_reports_every_field():
    run = (
        FakeRun()
        .on(("tailscale", "status", "--json"), stdout=_full_status_json())
        .on(("tailscale", "serve", "status"), returncode=0)
    )
    health = ts.check_client_health(run=run, which=which_map({"tailscale": "/usr/bin/tailscale"}))
    assert health.installed is True
    assert health.daemon_reachable is True
    assert health.backend_state == "Running"
    assert health.self_online is True
    assert health.dns_name == "castelo.tail1234.ts.net"
    assert health.magicdns_enabled is True
    assert health.https_certs_enabled is True
    assert health.operator_ok is True
    assert health.detail is None


def test_check_client_health_flags_https_certs_disabled():
    run = (
        FakeRun()
        .on(("tailscale", "status", "--json"), stdout=_full_status_json(cert_domains=()))
        .on(("tailscale", "serve", "status"), returncode=0)
    )
    health = ts.check_client_health(run=run, which=which_map({"tailscale": "/usr/bin/tailscale"}))
    assert health.https_certs_enabled is False
    assert "HTTPS Certificates are not enabled" in health.detail


def test_check_client_health_flags_operator_permission_missing():
    run = (
        FakeRun()
        .on(("tailscale", "status", "--json"), stdout=_full_status_json())
        .on(("tailscale", "serve", "status"), returncode=1, stderr="access denied: not an operator")
    )
    health = ts.check_client_health(run=run, which=which_map({"tailscale": "/usr/bin/tailscale"}))
    assert health.operator_ok is False
    assert "operator" in health.detail.lower()


def test_check_client_health_not_running_reports_backend_state_in_detail():
    run = (
        FakeRun()
        .on(("tailscale", "status", "--json"), stdout=_full_status_json(backend_state="Stopped"))
        .on(("tailscale", "serve", "status"), returncode=0)
    )
    health = ts.check_client_health(run=run, which=which_map({"tailscale": "/usr/bin/tailscale"}))
    assert health.backend_state == "Stopped"
    assert "'Stopped'" in health.detail


# ── wait_for_stable_backend ───────────────────────────────────────────────


def test_wait_for_stable_backend_returns_health_when_stable_and_ready():
    run = (
        FakeRun()
        .on(("tailscale", "status", "--json"), stdout=_full_status_json())
        .on(("tailscale", "serve", "status"), returncode=0)
    )
    sleeps = []
    health = ts.wait_for_stable_backend(
        run=run, which=which_map({"tailscale": "/usr/bin/tailscale"}), sleep=sleeps.append,
        attempts=3, interval=1.0,
    )
    assert health.backend_state == "Running"
    assert sleeps == [1.0, 1.0]  # slept between attempts, not after the last


def test_wait_for_stable_backend_raises_when_backend_flaps():
    """The exact 2026-07-19 shape: reachable and 'Running' on some polls,
    unreachable on others -- requiring every one of `attempts` tries to be
    healthy is what catches this instead of just the last one."""
    calls = {"n": 0}

    def dispatch(args, **kwargs):
        args = tuple(args)
        if args == ("tailscale", "status", "--json"):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("lost connection to tailscaled: unexpected EOF")
            return SimpleNamespace(returncode=0, stdout=_full_status_json(), stderr="")
        if args == ("tailscale", "serve", "status"):
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected call: {args}")

    with pytest.raises(ts.TailscaleError, match="lost connection to tailscaled"):
        ts.wait_for_stable_backend(
            run=dispatch, sleep=lambda _s: None,
            which=which_map({"tailscale": "/usr/bin/tailscale"}),
        )


def test_wait_for_stable_backend_raises_when_https_certs_disabled():
    run = (
        FakeRun()
        .on(("tailscale", "status", "--json"), stdout=_full_status_json(cert_domains=()))
        .on(("tailscale", "serve", "status"), returncode=0)
    )
    with pytest.raises(ts.TailscaleError, match="HTTPS Certificates are not enabled"):
        ts.wait_for_stable_backend(
            run=run, which=which_map({"tailscale": "/usr/bin/tailscale"}), sleep=lambda _s: None,
        )


# ── provision_cert ────────────────────────────────────────────────────────
# Replaced an earlier CertDomains-presence heuristic that missed a real
# 2026-07-19 case: an account with HTTPS Certificates never enabled, where
# `tailscale status --json` simply omitted the CertDomains key rather than
# returning an empty list, so the heuristic read it as "unknown, proceed."
# `tailscale cert <hostname>` is the direct, authoritative probe instead.


def test_provision_cert_succeeds_silently():
    run = FakeRun().on(("tailscale", "cert", "castelo.tail1234.ts.net"), returncode=0)
    ts.provision_cert("castelo.tail1234.ts.net", run=run)  # no raise


def test_provision_cert_raises_with_actionable_message_when_account_lacks_https_certs():
    run = FakeRun().on(
        ("tailscale", "cert", "castelo.tail1234.ts.net"), returncode=1,
        stderr="500 Internal Server Error: your Tailscale account does not support getting TLS certs",
    )
    with pytest.raises(ts.TailscaleError, match="does not support getting TLS certs"):
        ts.provision_cert("castelo.tail1234.ts.net", run=run)
    # The remediation (admin console URL) is included, not just the raw error.
    try:
        ts.provision_cert("castelo.tail1234.ts.net", run=run)
    except ts.TailscaleError as exc:
        assert "login.tailscale.com/admin/dns" in str(exc)


def test_provision_cert_raises_on_timeout():
    def boom(*args, **kwargs):
        raise ts.subprocess.TimeoutExpired(cmd="tailscale cert", timeout=90.0)

    with pytest.raises(ts.TailscaleError, match="did not finish within"):
        ts.provision_cert("castelo.tail1234.ts.net", run=boom)


def test_wait_for_stable_backend_raises_when_operator_permission_missing():
    run = (
        FakeRun()
        .on(("tailscale", "status", "--json"), stdout=_full_status_json())
        .on(("tailscale", "serve", "status"), returncode=1, stderr="access denied: not an operator")
    )
    with pytest.raises(ts.TailscaleError, match="operator"):
        ts.wait_for_stable_backend(
            run=run, which=which_map({"tailscale": "/usr/bin/tailscale"}), sleep=lambda _s: None,
        )
