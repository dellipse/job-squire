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
from job_squire_cli.ops import backup as bk
from job_squire_cli.ops import dns as dns_ops
from job_squire_cli.ops import lifecycle as lc
from job_squire_cli.ops import proxy as proxy_ops
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


# ── backup / restore (Prompt C8) ─────────────────────────────────────────
# ops/backup.py's own behavior (real encryption, real files) is covered in
# tests/test_backup.py; these only prove the click adapter's argument
# parsing, prompting, and error rendering, with ops/backup.py stubbed out.


def test_backup_requires_name_or_all(runner):
    result = runner.invoke(main, ["backup"])
    assert result.exit_code == 1
    assert "Specify an instance NAME" in result.output


def test_backup_rejects_name_and_all_together(runner):
    result = runner.invoke(main, ["backup", "castelo", "--all"])
    assert result.exit_code == 1
    assert "not both" in result.output


def test_backup_unregistered_instance_fails_cleanly(runner):
    result = runner.invoke(main, ["backup", "ghost"])
    assert result.exit_code == 1
    assert "No instance named 'ghost'" in result.output
    assert "Traceback" not in result.output


def test_backup_prompts_for_passphrase_with_confirmation_and_warns_it_cannot_be_recovered(
    runner, monkeypatch, tmp_path,
):
    reg.add_instance(
        name="castelo", mode="local", runtime="docker", data_dir=str(tmp_path),
        public_url="http://localhost:8080", app_port=8080, mcp_port=9000,
    )
    captured = {}

    def fake_create_backup(instance, *, dest_dir, passphrase, ext):
        captured.update(name=instance.name, passphrase=passphrase, ext=ext)
        return bk.BackupResult(instance_name=instance.name, archive_path=tmp_path / "archive.tgz", manifest={})

    monkeypatch.setattr(bk, "create_backup", fake_create_backup)
    result = runner.invoke(main, ["backup", "castelo"], input="s3cr3t!\ns3cr3t!\n")
    assert result.exit_code == 0
    assert captured == {"name": "castelo", "passphrase": "s3cr3t!", "ext": "tgz"}
    assert "lost passphrase means a lost backup" in result.output
    assert "backed up to" in result.output


def test_backup_passphrase_flag_skips_the_prompt(runner, monkeypatch, tmp_path):
    reg.add_instance(
        name="castelo", mode="local", runtime="docker", data_dir=str(tmp_path),
        public_url="http://localhost:8080", app_port=8080, mcp_port=9000,
    )
    captured = {}
    monkeypatch.setattr(
        bk, "create_backup",
        lambda instance, *, dest_dir, passphrase, ext: captured.update(passphrase=passphrase)
        or bk.BackupResult(instance_name=instance.name, archive_path=tmp_path / "a.tgz", manifest={}),
    )
    result = runner.invoke(main, ["backup", "castelo", "--passphrase", "pw-from-flag"])
    assert result.exit_code == 0
    assert captured["passphrase"] == "pw-from-flag"


def test_backup_all_backs_up_every_registered_instance(runner, monkeypatch, tmp_path):
    reg.add_instance(
        name="one", mode="local", runtime="docker", data_dir=str(tmp_path / "one"),
        public_url="http://localhost:8080", app_port=8080, mcp_port=9000,
    )
    reg.add_instance(
        name="two", mode="local", runtime="docker", data_dir=str(tmp_path / "two"),
        public_url="http://localhost:8081", app_port=8081, mcp_port=9001,
    )
    seen = []
    monkeypatch.setattr(
        bk, "create_backup",
        lambda instance, *, dest_dir, passphrase, ext: seen.append(instance.name)
        or bk.BackupResult(instance_name=instance.name, archive_path=tmp_path / f"{instance.name}.tgz", manifest={}),
    )
    result = runner.invoke(main, ["backup", "--all", "--passphrase", "pw"])
    assert result.exit_code == 0
    assert sorted(seen) == ["one", "two"]


def test_restore_wrong_passphrase_fails_cleanly(runner, monkeypatch, tmp_path):
    archive = tmp_path / "archive.tgz"
    archive.write_bytes(b"stand-in archive bytes")

    def fake_open_backup(path, passphrase):
        raise bk.WrongPassphraseError("Wrong passphrase, or the archive is corrupted or was tampered with.")

    monkeypatch.setattr(bk, "open_backup", fake_open_backup)
    result = runner.invoke(main, ["restore", str(archive)], input="wrong-pw\n")
    assert result.exit_code == 1
    assert "Wrong passphrase" in result.output
    assert "Traceback" not in result.output


def test_restore_no_collision_skips_the_rename_prompt(runner, monkeypatch, tmp_path):
    archive = tmp_path / "archive.tgz"
    archive.write_bytes(b"stand-in")

    def fake_open_backup(path, passphrase):
        return bk.OpenedBackup(
            archive_path=path, payload=b"", container_format=0,
            manifest={"instance": {"name": "castelo"}, "checksums": {}},
        )

    captured = {}

    def fake_restore_instance(opened, *, target_name, overwrite, image, bring_up, confirm):
        captured.update(target_name=target_name, overwrite=overwrite)
        inst = reg.Instance(
            name="castelo", mode="local", runtime="docker", data_dir=str(tmp_path / "restored"),
            app_port=8080, mcp_port=9000, cookie_name="castelo_session",
            public_url="http://localhost:8080", created="2026-07-11",
        )
        return bk.RestoreResult(instance=inst, data_dir=tmp_path / "restored", health={"Status": "running"})

    monkeypatch.setattr(bk, "open_backup", fake_open_backup)
    monkeypatch.setattr(bk, "restore_instance", fake_restore_instance)
    result = runner.invoke(main, ["restore", str(archive)], input="s3cr3t!\n")
    assert result.exit_code == 0
    assert captured == {"target_name": None, "overwrite": False}
    assert "castelo" in result.output
    assert "already registered" not in result.output


def test_restore_collision_prompts_rename_and_passes_new_name_through(runner, monkeypatch, tmp_path):
    reg.add_instance(
        name="castelo", mode="local", runtime="docker", data_dir=str(tmp_path / "existing"),
        public_url="http://localhost:8080", app_port=8080, mcp_port=9000,
    )
    archive = tmp_path / "archive.tgz"
    archive.write_bytes(b"stand-in")

    def fake_open_backup(path, passphrase):
        return bk.OpenedBackup(
            archive_path=path, payload=b"", container_format=0,
            manifest={"instance": {"name": "castelo"}, "checksums": {}},
        )

    captured = {}

    def fake_restore_instance(opened, *, target_name, overwrite, image, bring_up, confirm):
        captured.update(target_name=target_name, overwrite=overwrite)
        inst = reg.Instance(
            name=target_name, mode="local", runtime="docker", data_dir=str(tmp_path / "restored"),
            app_port=8081, mcp_port=9001, cookie_name=f"{target_name}_session",
            public_url="http://localhost:8081", created="2026-07-11",
        )
        return bk.RestoreResult(instance=inst, data_dir=tmp_path / "restored", health={"Status": "running"})

    monkeypatch.setattr(bk, "open_backup", fake_open_backup)
    monkeypatch.setattr(bk, "restore_instance", fake_restore_instance)
    result = runner.invoke(main, ["restore", str(archive)], input="s3cr3t!\nrename\ncastelo-2\n")
    assert result.exit_code == 0
    assert "already registered" in result.output
    assert captured == {"target_name": "castelo-2", "overwrite": False}
    assert "castelo-2" in result.output


def test_restore_collision_prompts_overwrite(runner, monkeypatch, tmp_path):
    reg.add_instance(
        name="castelo", mode="local", runtime="docker", data_dir=str(tmp_path / "existing"),
        public_url="http://localhost:8080", app_port=8080, mcp_port=9000,
    )
    archive = tmp_path / "archive.tgz"
    archive.write_bytes(b"stand-in")

    def fake_open_backup(path, passphrase):
        return bk.OpenedBackup(
            archive_path=path, payload=b"", container_format=0,
            manifest={"instance": {"name": "castelo"}, "checksums": {}},
        )

    captured = {}

    def fake_restore_instance(opened, *, target_name, overwrite, image, bring_up, confirm):
        captured.update(target_name=target_name, overwrite=overwrite)
        inst = reg.Instance(
            name="castelo", mode="local", runtime="docker", data_dir=str(tmp_path / "restored"),
            app_port=8080, mcp_port=9000, cookie_name="castelo_session",
            public_url="http://localhost:8080", created="2026-07-11",
        )
        return bk.RestoreResult(instance=inst, data_dir=tmp_path / "restored", health={"Status": "running"})

    monkeypatch.setattr(bk, "open_backup", fake_open_backup)
    monkeypatch.setattr(bk, "restore_instance", fake_restore_instance)
    result = runner.invoke(main, ["restore", str(archive)], input="s3cr3t!\noverwrite\n")
    assert result.exit_code == 0
    assert captured == {"target_name": None, "overwrite": True}


def test_restore_collision_prompts_abort(runner, monkeypatch, tmp_path):
    reg.add_instance(
        name="castelo", mode="local", runtime="docker", data_dir=str(tmp_path / "existing"),
        public_url="http://localhost:8080", app_port=8080, mcp_port=9000,
    )
    archive = tmp_path / "archive.tgz"
    archive.write_bytes(b"stand-in")

    def fake_open_backup(path, passphrase):
        return bk.OpenedBackup(
            archive_path=path, payload=b"", container_format=0,
            manifest={"instance": {"name": "castelo"}, "checksums": {}},
        )

    restore_called = []
    monkeypatch.setattr(bk, "open_backup", fake_open_backup)
    monkeypatch.setattr(bk, "restore_instance", lambda *a, **k: restore_called.append(1))
    result = runner.invoke(main, ["restore", str(archive)], input="s3cr3t!\nabort\n")
    assert result.exit_code == 1
    assert "cancelled" in result.output
    assert restore_called == []


# ── proxy (Prompt C9) ────────────────────────────────────────────────────
# Thin adapter tests only, same philosophy as backup/restore above:
# ops/proxy.py's own behavior is covered exhaustively in test_proxy.py with
# a fully injected fake runtime, so `provision_instance_proxy` is stubbed
# here rather than re-driven through a fake subprocess at this layer.


def test_proxy_unregistered_instance_fails_cleanly(runner):
    result = runner.invoke(main, ["proxy", "ghost"])
    assert result.exit_code == 1
    assert "No instance named 'ghost'" in result.output
    assert "Traceback" not in result.output


def test_proxy_rejects_local_mode_instance(runner, tmp_path):
    reg.add_instance(
        name="castelo", mode="local", runtime="docker", data_dir=str(tmp_path),
        public_url="http://localhost:8080", app_port=8080, mcp_port=9000,
    )
    result = runner.invoke(main, ["proxy", "castelo"])
    assert result.exit_code == 1
    assert "local" in result.output
    assert "only applies to network-mode instances" in result.output


def test_proxy_happy_path_prints_result(runner, monkeypatch, tmp_path):
    reg.add_instance(
        name="castelo", mode="network", runtime="docker", data_dir=str(tmp_path),
        public_url="https://squire.example.com", app_port=None, mcp_port=None,
    )
    captured = {}

    def fake_provision(instance, *, root, proxy_container, config_dir, network, install_if_missing,
                        swag_timezone, swag_url, swag_validation, confirm):
        captured.update(name=instance.name, network=network, install_if_missing=install_if_missing)
        target = proxy_ops.ProxyTarget(config_dir=tmp_path / "swag-config", container_name="swag", kind="swag")
        return proxy_ops.ProxyProvisionResult(
            proxy=target, network=network,
            web_conf_path=tmp_path / "web.conf", mcp_conf_path=tmp_path / "mcp.conf",
            installed_swag=False,
        )

    monkeypatch.setattr(proxy_ops, "provision_instance_proxy", fake_provision)
    result = runner.invoke(main, ["proxy", "castelo"])
    assert result.exit_code == 0
    assert captured == {"name": "castelo", "network": proxy_ops.DEFAULT_PROXY_NETWORK, "install_if_missing": True}
    assert "provisioned for 'castelo'" in result.output
    assert "Proxy reloaded." in result.output


def test_proxy_no_install_flag_disables_swag_fallback(runner, monkeypatch, tmp_path):
    reg.add_instance(
        name="castelo", mode="network", runtime="docker", data_dir=str(tmp_path),
        public_url="https://squire.example.com", app_port=None, mcp_port=None,
    )
    captured = {}

    def fake_provision(instance, *, root, proxy_container, config_dir, network, install_if_missing,
                        swag_timezone, swag_url, swag_validation, confirm):
        captured["install_if_missing"] = install_if_missing
        raise proxy_ops.ProxyError("no proxy available")

    monkeypatch.setattr(proxy_ops, "provision_instance_proxy", fake_provision)
    result = runner.invoke(main, ["proxy", "castelo", "--no-install"])
    assert result.exit_code == 1
    assert captured == {"install_if_missing": False}
    assert "no proxy available" in result.output


# ── dns (Prompt C10) ─────────────────────────────────────────────────────
# Thin adapter tests only, same philosophy as proxy above: ops/dns.py's own
# behavior (compose rewriting, credentials files, certificate polling) is
# covered exhaustively in test_dns.py with a fully injected fake runtime;
# here the point is just proving the click commands parse their options,
# reject a local-mode instance, and surface ops/dns.py's exceptions as a
# clean exit(1) rather than a traceback.


def _register_network_instance(tmp_path, name="castelo"):
    reg.add_instance(
        name=name, mode="network", runtime="docker", data_dir=str(tmp_path),
        public_url="https://squire.example.com", app_port=None, mcp_port=None,
    )


def test_dns_duckdns_unregistered_instance_fails_cleanly(runner):
    result = runner.invoke(main, ["dns", "duckdns", "ghost", "--subdomain", "castelo", "--token", "tok"])
    assert result.exit_code == 1
    assert "No instance named 'ghost'" in result.output
    assert "Traceback" not in result.output


def test_dns_duckdns_rejects_local_mode_instance(runner, tmp_path):
    reg.add_instance(
        name="castelo", mode="local", runtime="docker", data_dir=str(tmp_path),
        public_url="http://localhost:8080", app_port=8080, mcp_port=9000,
    )
    result = runner.invoke(main, ["dns", "duckdns", "castelo", "--subdomain", "castelo", "--token", "tok"])
    assert result.exit_code == 1
    assert "local" in result.output
    assert "only applies to network-mode instances" in result.output


def test_dns_duckdns_happy_path_prints_result(runner, monkeypatch, tmp_path):
    _register_network_instance(tmp_path)
    captured = {}

    def fake_configure(*, subdomain, token, wildcard, runtime, network, timezone, wait_for_cert, timeout_seconds):
        captured.update(subdomain=subdomain, token=token, wildcard=wildcard, runtime=runtime)
        target = proxy_ops.ProxyTarget(config_dir=tmp_path / "swag-config", container_name="swag", kind="swag")
        return dns_ops.DnsProvisionResult(
            mode="duckdns-wildcard", url="castelo.duckdns.org", subdomains="wildcard",
            proxy=target, cert=dns_ops.CertResult(issued=True, log_tail="Congratulations!"),
        )

    monkeypatch.setattr(dns_ops, "configure_duckdns", fake_configure)
    result = runner.invoke(main, ["dns", "duckdns", "castelo", "--subdomain", "castelo", "--token", "tok123"])
    assert result.exit_code == 0
    assert captured == {"subdomain": "castelo", "token": "tok123", "wildcard": True, "runtime": "docker"}
    assert "duckdns-wildcard" in result.output
    assert "Certificate issued" in result.output


def test_dns_duckdns_not_yet_issued_prints_follow_up_hint(runner, monkeypatch, tmp_path):
    _register_network_instance(tmp_path)

    def fake_configure(*, subdomain, token, wildcard, runtime, network, timezone, wait_for_cert, timeout_seconds):
        target = proxy_ops.ProxyTarget(config_dir=tmp_path / "swag-config", container_name="swag", kind="swag")
        return dns_ops.DnsProvisionResult(
            mode="duckdns-wildcard", url="castelo.duckdns.org", subdomains="wildcard",
            proxy=target, cert=dns_ops.CertResult(issued=False, log_tail=""),
        )

    monkeypatch.setattr(dns_ops, "configure_duckdns", fake_configure)
    result = runner.invoke(main, ["dns", "duckdns", "castelo", "--subdomain", "castelo", "--token", "tok123"])
    assert result.exit_code == 0
    assert "not yet confirmed issued" in result.output
    assert "docker logs swag" in result.output


def test_dns_duckdns_propagates_dns_error_as_clean_exit(runner, monkeypatch, tmp_path):
    _register_network_instance(tmp_path)

    def fake_configure(**kwargs):
        raise dns_ops.DnsError("no CLI-installed SWAG found")

    monkeypatch.setattr(dns_ops, "configure_duckdns", fake_configure)
    result = runner.invoke(main, ["dns", "duckdns", "castelo", "--subdomain", "castelo", "--token", "tok123"])
    assert result.exit_code == 1
    assert "no CLI-installed SWAG found" in result.output
    assert "Traceback" not in result.output


def test_dns_cloudflare_happy_path_prints_result(runner, monkeypatch, tmp_path):
    _register_network_instance(tmp_path)
    captured = {}

    def fake_configure(*, domain, api_token, runtime, network, timezone, wait_for_cert, timeout_seconds):
        captured.update(domain=domain, api_token=api_token, runtime=runtime)
        target = proxy_ops.ProxyTarget(config_dir=tmp_path / "swag-config", container_name="swag", kind="swag")
        return dns_ops.DnsProvisionResult(
            mode="cloudflare-dns01", url="example.com", subdomains="wildcard",
            proxy=target, cert=dns_ops.CertResult(issued=True, log_tail="Congratulations!"),
        )

    monkeypatch.setattr(dns_ops, "configure_cloudflare", fake_configure)
    result = runner.invoke(
        main, ["dns", "cloudflare", "castelo", "--domain", "example.com", "--token", "cf-tok"],
    )
    assert result.exit_code == 0
    assert captured == {"domain": "example.com", "api_token": "cf-tok", "runtime": "docker"}
    assert "cloudflare-dns01" in result.output
    assert "Certificate issued" in result.output


def test_dns_cloudflare_rejects_local_mode_instance(runner, tmp_path):
    reg.add_instance(
        name="castelo", mode="local", runtime="docker", data_dir=str(tmp_path),
        public_url="http://localhost:8080", app_port=8080, mcp_port=9000,
    )
    result = runner.invoke(
        main, ["dns", "cloudflare", "castelo", "--domain", "example.com", "--token", "cf-tok"],
    )
    assert result.exit_code == 1
    assert "only applies to network-mode instances" in result.output


def test_dns_cloudflare_propagates_dns_error_as_clean_exit(runner, monkeypatch, tmp_path):
    _register_network_instance(tmp_path)

    def fake_configure(**kwargs):
        raise dns_ops.DnsError("domain and a Cloudflare API token are required")

    monkeypatch.setattr(dns_ops, "configure_cloudflare", fake_configure)
    result = runner.invoke(
        main, ["dns", "cloudflare", "castelo", "--domain", "example.com", "--token", "cf-tok"],
    )
    assert result.exit_code == 1
    assert "domain and a Cloudflare API token are required" in result.output
    assert "Traceback" not in result.output
