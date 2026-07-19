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
"""The click layer for create/start/stop/restart/status/list/remove --
thin adapter tests only. ops/lifecycle.py's own behavior is
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
from job_squire_cli.ops import ollama_assist
from job_squire_cli.ops import proxy as proxy_ops
from job_squire_cli.ops import registry as reg
from job_squire_cli.ops import self_update as su
from job_squire_cli.ops import tailscale as tailscale_ops
from job_squire_cli.query import config as query_config_module


@pytest.fixture(autouse=True)
def force_linux_config_dir(monkeypatch):
    monkeypatch.setattr(query_config_module.platform, "system", lambda: "Linux")


@pytest.fixture(autouse=True)
def tmp_registry(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))


@pytest.fixture(autouse=True)
def stub_self_update(monkeypatch):
    """`update` now self-updates the CLI before touching any instance
    (see ops/self_update.py) -- stub it out everywhere by default so the
    rest of this file's `update` tests (about instance movement, not CLI
    self-update) don't make a real GitHub API call. The dedicated
    self-update behavior below overrides this per-test where it matters.
    """
    from job_squire_cli.ops import commands as cmds
    monkeypatch.setattr(
        cmds.self_update, "self_update",
        lambda version=None: su.SelfUpdateResult(
            updated=False, previous_version="0.7.0+abc123", new_version="0.7.0+abc123", tag="v0.7.0",
        ),
    )


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


def _register_local_castelo():
    """`remove` now looks the instance up in the real registry (via
    `_require_instance`) before ever calling `lc.remove_instance`, so it's
    no longer enough to just monkeypatch `lc.remove_instance` -- these
    tests need a real registry entry for that early lookup to find. Local
    mode is deliberate: it keeps these tests (which are about the image/
    volume summary lines, not the proxy) from also tripping the new
    network-mode-only proxy/DNS cleanup offer below."""
    reg.add_instance(
        name="castelo", mode="local", runtime="docker", data_dir="/data/castelo",
        public_url="http://localhost:8080", app_port=8080, mcp_port=9000,
    )


def test_remove_without_remove_image_flag_never_mentions_image(runner, monkeypatch):
    """Default behavior (no --remove-image): the summary says nothing about
    the image at all, matching what plain `compose down` has always done."""
    _register_local_castelo()
    monkeypatch.setattr(
        lc, "remove_instance",
        lambda name, **kwargs: lc.RemoveResult(name=name, data_dir="/data/castelo", data_kept=True),
    )
    result = runner.invoke(main, ["remove", "castelo", "--yes"])
    assert result.exit_code == 0
    assert "image" not in result.output.lower()


def test_remove_image_flag_reports_when_image_was_removed(runner, monkeypatch):
    _register_local_castelo()
    captured = {}

    def fake_remove_instance(name, **kwargs):
        captured.update(kwargs)
        return lc.RemoveResult(
            name=name, data_dir="/data/castelo", data_kept=True,
            image="ghcr.io/dellipse/job-squire:latest", image_removed=True, image_kept_reason=None,
        )

    monkeypatch.setattr(lc, "remove_instance", fake_remove_instance)
    result = runner.invoke(main, ["remove", "castelo", "--yes", "--remove-image"])
    assert result.exit_code == 0
    assert captured["remove_image"] is True
    assert "Image removed: ghcr.io/dellipse/job-squire:latest" in result.output


def test_remove_deleted_data_reports_the_volume_that_was_removed(runner, monkeypatch):
    _register_local_castelo()
    monkeypatch.setattr(
        lc, "remove_instance",
        lambda name, **kwargs: lc.RemoveResult(
            name=name, data_dir="/data/castelo", data_kept=False,
            volumes_removed=["job-squire-castelo-data"],
        ),
    )
    result = runner.invoke(main, ["remove", "castelo", "--yes", "--delete-data"])
    assert result.exit_code == 0
    assert "Data volume(s) removed: job-squire-castelo-data" in result.output


def test_remove_deleted_data_reports_when_no_volume_was_found(runner, monkeypatch):
    _register_local_castelo()
    monkeypatch.setattr(
        lc, "remove_instance",
        lambda name, **kwargs: lc.RemoveResult(name=name, data_dir="/data/castelo", data_kept=False),
    )
    result = runner.invoke(main, ["remove", "castelo", "--yes", "--delete-data"])
    assert result.exit_code == 0
    assert "No data volume found to remove" in result.output


def test_remove_kept_data_never_mentions_volumes(runner, monkeypatch):
    _register_local_castelo()
    monkeypatch.setattr(
        lc, "remove_instance",
        lambda name, **kwargs: lc.RemoveResult(name=name, data_dir="/data/castelo", data_kept=True),
    )
    result = runner.invoke(main, ["remove", "castelo", "--yes"])
    assert result.exit_code == 0
    assert "volume" not in result.output.lower()


def test_remove_image_flag_reports_the_kept_reason_when_shared(runner, monkeypatch):
    _register_local_castelo()
    monkeypatch.setattr(
        lc, "remove_instance",
        lambda name, **kwargs: lc.RemoveResult(
            name=name, data_dir="/data/castelo", data_kept=True,
            image="ghcr.io/dellipse/job-squire:latest", image_removed=False,
            image_kept_reason="still used by another registered instance",
        ),
    )
    result = runner.invoke(main, ["remove", "castelo", "--yes", "--remove-image"])
    assert result.exit_code == 0
    assert "Image kept (ghcr.io/dellipse/job-squire:latest): still used by another registered instance" \
        in result.output


# ── remove: proxy/DNS cleanup offer for network-mode instances ──────────
# The reverse of `create`'s automatic proxy-setup offer (see the earlier
# "automatic reverse-proxy offer at the tail of `create`" section):
# `remove` now offers to delete this instance's confs from whatever proxy
# is running (`_offer_proxy_removal` in commands.py), and -- only if that
# proxy is the CLI's own managed SWAG install and nothing else still uses
# it -- offers to tear the whole thing down, DNS/TLS configuration
# included. These tests register a real network-mode instance (unlike the
# image/volume tests above, which stay local-mode to avoid this offer
# entirely) and stub proxy_ops directly, same style as the `create` offer
# tests' proxy_ops stubs.


def _register_network_castelo(data_dir="/data/castelo"):
    reg.add_instance(
        name="castelo", mode="network", runtime="docker", data_dir=data_dir,
        public_url="https://squire.example.com", app_port=8080, mcp_port=9000,
    )


def _stub_remove_instance(monkeypatch):
    """Stubs `lc.remove_instance` but still drops the instance from the
    real registry, the same as the real function does -- `_offer_proxy_
    removal`'s own "is any other registered network-mode instance still
    around?" check needs this instance actually gone to behave like a real
    run would, not just its container-teardown side stubbed out."""
    def fake_remove_instance(name, **kwargs):
        reg.remove_instance(name)
        return lc.RemoveResult(name=name, data_dir="/data/castelo", data_kept=True)
    monkeypatch.setattr(lc, "remove_instance", fake_remove_instance)


def test_remove_local_mode_never_checks_for_a_proxy(runner, monkeypatch):
    reg.add_instance(
        name="castelo", mode="local", runtime="docker", data_dir="/data/castelo",
        public_url="http://localhost:8080", app_port=8080, mcp_port=9000,
    )
    _stub_remove_instance(monkeypatch)

    def boom(*args, **kwargs):
        raise AssertionError("detect_existing_proxy should not run for a local-mode instance")
    monkeypatch.setattr(proxy_ops, "detect_existing_proxy", boom)

    result = runner.invoke(main, ["remove", "castelo", "--yes"])
    assert result.exit_code == 0
    assert "reverse proxy" not in result.output.lower()


def test_remove_network_mode_says_nothing_when_no_proxy_is_running(runner, monkeypatch):
    _register_network_castelo()
    _stub_remove_instance(monkeypatch)
    monkeypatch.setattr(proxy_ops, "detect_existing_proxy", lambda runtime, **kwargs: None)

    result = runner.invoke(main, ["remove", "castelo", "--yes"])
    assert result.exit_code == 0
    assert "reverse proxy" not in result.output.lower()


def test_remove_network_mode_says_nothing_when_this_instance_was_never_configured_into_the_proxy(runner, monkeypatch, tmp_path):
    _register_network_castelo()
    _stub_remove_instance(monkeypatch)
    # A proxy is running, but it has no confs for *this* instance (e.g. the
    # `create`-time offer was declined) -- nothing to clean up.
    monkeypatch.setattr(
        proxy_ops, "detect_existing_proxy",
        lambda runtime, **kwargs: proxy_ops.ProxyTarget(config_dir=tmp_path, container_name="swag", kind="swag"),
    )

    def boom(*args, **kwargs):
        raise AssertionError("remove_confs should not run when this instance has no confs to remove")
    monkeypatch.setattr(proxy_ops, "remove_confs", boom)

    result = runner.invoke(main, ["remove", "castelo", "--yes"])
    assert result.exit_code == 0
    assert "reverse proxy" not in result.output.lower()


def _write_fake_confs(tmp_path, *names):
    confs_dir = tmp_path / "nginx" / "proxy-confs"
    confs_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        (confs_dir / name).write_text("# fake conf\n")
    return confs_dir


def test_remove_network_mode_offers_to_remove_confs_and_declines(runner, monkeypatch, tmp_path):
    _register_network_castelo()
    _stub_remove_instance(monkeypatch)
    _write_fake_confs(tmp_path, "job-squire-castelo.subdomain.conf", "mcp-job-squire-castelo.subdomain.conf")
    monkeypatch.setattr(
        proxy_ops, "detect_existing_proxy",
        lambda runtime, **kwargs: proxy_ops.ProxyTarget(config_dir=tmp_path, container_name="swag", kind="swag"),
    )

    def boom(*args, **kwargs):
        raise AssertionError("remove_confs should not run once the offer is declined")
    monkeypatch.setattr(proxy_ops, "remove_confs", boom)

    result = runner.invoke(main, ["remove", "castelo"], input="n\n")
    assert result.exit_code == 0
    assert "Remove the reverse-proxy configuration for 'castelo' from the running swag proxy now?" in result.output
    assert "Skipped." in result.output


def test_remove_network_mode_removes_confs_reloads_and_leaves_shared_swag_when_another_instance_remains(
    runner, monkeypatch, tmp_path,
):
    _register_network_castelo()
    _stub_remove_instance(monkeypatch)
    confs_dir = _write_fake_confs(
        tmp_path, "job-squire-castelo.subdomain.conf", "mcp-job-squire-castelo.subdomain.conf",
        # A sibling instance's own confs -- still there after castelo's are removed.
        "job-squire-other.subdomain.conf", "mcp-job-squire-other.subdomain.conf",
    )
    monkeypatch.setattr(
        proxy_ops, "detect_existing_proxy",
        lambda runtime, **kwargs: proxy_ops.ProxyTarget(config_dir=tmp_path, container_name="swag", kind="swag"),
    )
    monkeypatch.setattr(proxy_ops, "reload_proxy", lambda *a, **k: None)
    monkeypatch.setattr(proxy_ops, "is_managed_swag", lambda *a, **k: True)

    def boom(*args, **kwargs):
        raise AssertionError("remove_managed_swag should not run while another instance still uses it")
    monkeypatch.setattr(proxy_ops, "remove_managed_swag", boom)

    result = runner.invoke(main, ["remove", "castelo", "--yes"])
    assert result.exit_code == 0
    assert not (confs_dir / "job-squire-castelo.subdomain.conf").exists()
    assert not (confs_dir / "mcp-job-squire-castelo.subdomain.conf").exists()
    assert (confs_dir / "job-squire-other.subdomain.conf").exists()
    assert "Proxy reloaded." in result.output
    assert "SWAG" not in result.output


def test_remove_network_mode_offers_full_swag_teardown_when_last_instance(runner, monkeypatch, tmp_path):
    _register_network_castelo()
    _stub_remove_instance(monkeypatch)
    _write_fake_confs(tmp_path, "job-squire-castelo.subdomain.conf", "mcp-job-squire-castelo.subdomain.conf")
    monkeypatch.setattr(
        proxy_ops, "detect_existing_proxy",
        lambda runtime, **kwargs: proxy_ops.ProxyTarget(config_dir=tmp_path, container_name="swag", kind="swag"),
    )
    monkeypatch.setattr(proxy_ops, "reload_proxy", lambda *a, **k: None)
    monkeypatch.setattr(proxy_ops, "is_managed_swag", lambda *a, **k: True)
    removed = []
    monkeypatch.setattr(proxy_ops, "remove_managed_swag", lambda runtime, **kwargs: removed.append(runtime))

    result = runner.invoke(main, ["remove", "castelo"], input="y\ny\n")
    assert result.exit_code == 0
    assert "remove it entirely too" in result.output
    assert "Removed the CLI-installed SWAG proxy and its DNS/TLS configuration." in result.output
    assert removed == ["docker"]


def test_remove_network_mode_declines_full_swag_teardown(runner, monkeypatch, tmp_path):
    _register_network_castelo()
    _stub_remove_instance(monkeypatch)
    _write_fake_confs(tmp_path, "job-squire-castelo.subdomain.conf", "mcp-job-squire-castelo.subdomain.conf")
    monkeypatch.setattr(
        proxy_ops, "detect_existing_proxy",
        lambda runtime, **kwargs: proxy_ops.ProxyTarget(config_dir=tmp_path, container_name="swag", kind="swag"),
    )
    monkeypatch.setattr(proxy_ops, "reload_proxy", lambda *a, **k: None)
    monkeypatch.setattr(proxy_ops, "is_managed_swag", lambda *a, **k: True)

    def boom(*args, **kwargs):
        raise AssertionError("remove_managed_swag should not run once the second offer is declined")
    monkeypatch.setattr(proxy_ops, "remove_managed_swag", boom)

    result = runner.invoke(main, ["remove", "castelo"], input="y\nn\n")
    assert result.exit_code == 0
    assert "remove it entirely too" in result.output
    assert "Skipped. Remove it later by hand" in result.output


def test_remove_network_mode_never_tears_down_a_third_party_proxy(runner, monkeypatch, tmp_path):
    _register_network_castelo()
    _stub_remove_instance(monkeypatch)
    _write_fake_confs(tmp_path, "job-squire-castelo.subdomain.conf", "mcp-job-squire-castelo.subdomain.conf")
    monkeypatch.setattr(
        proxy_ops, "detect_existing_proxy",
        lambda runtime, **kwargs: proxy_ops.ProxyTarget(config_dir=tmp_path, container_name="swag", kind="swag"),
    )
    monkeypatch.setattr(proxy_ops, "reload_proxy", lambda *a, **k: None)
    monkeypatch.setattr(proxy_ops, "is_managed_swag", lambda *a, **k: False)

    def boom(*args, **kwargs):
        raise AssertionError("remove_managed_swag should never be offered for a third-party proxy")
    monkeypatch.setattr(proxy_ops, "remove_managed_swag", boom)

    # Only one prompt (conf removal) -- a second "y" would be left unread if
    # a teardown prompt wrongly appeared, but click.confirm would then hit
    # EOF and fail loudly rather than silently passing.
    result = runner.invoke(main, ["remove", "castelo"], input="y\n")
    assert result.exit_code == 0
    assert "remove it entirely too" not in result.output


def test_remove_skip_proxy_cleanup_flag_skips_the_offer_entirely(runner, monkeypatch, tmp_path):
    _register_network_castelo()
    _stub_remove_instance(monkeypatch)

    def boom(*args, **kwargs):
        raise AssertionError("detect_existing_proxy should not run with --skip-proxy-cleanup")
    monkeypatch.setattr(proxy_ops, "detect_existing_proxy", boom)

    result = runner.invoke(main, ["remove", "castelo", "--yes", "--skip-proxy-cleanup"])
    assert result.exit_code == 0
    assert "reverse proxy" not in result.output.lower()


def test_remove_yes_flag_answers_both_proxy_prompts_without_stdin(runner, monkeypatch, tmp_path):
    _register_network_castelo()
    _stub_remove_instance(monkeypatch)
    _write_fake_confs(tmp_path, "job-squire-castelo.subdomain.conf", "mcp-job-squire-castelo.subdomain.conf")
    monkeypatch.setattr(
        proxy_ops, "detect_existing_proxy",
        lambda runtime, **kwargs: proxy_ops.ProxyTarget(config_dir=tmp_path, container_name="swag", kind="swag"),
    )
    monkeypatch.setattr(proxy_ops, "reload_proxy", lambda *a, **k: None)
    monkeypatch.setattr(proxy_ops, "is_managed_swag", lambda *a, **k: True)
    removed = []
    monkeypatch.setattr(proxy_ops, "remove_managed_swag", lambda runtime, **kwargs: removed.append(runtime))

    # No `input=` at all -- --yes must answer both prompts without reading stdin.
    result = runner.invoke(main, ["remove", "castelo", "--yes"])
    assert result.exit_code == 0
    assert removed == ["docker"]


# ── remove: Tailscale Serve cleanup offer for local-mode instances ──────
# Unlike the proxy/DNS offer, there's no "shared install"/"last instance"
# question -- Serve is entirely per-instance (ops/tailscale.py: each
# enabled instance picks its own dedicated port pair), and job-squire
# never installs the Tailscale client itself, only the per-instance Serve
# mappings -- so this is a single yes/no: was Serve on for this instance,
# and if so turn it off.


def _enabled_tailscale_state(web_port=443, mcp_port=8443):
    return tailscale_ops.TailscaleState(
        enabled=True, hostname="my-device.tailnet.ts.net", web_port=web_port, mcp_port=mcp_port,
        enabled_at="2026-07-17T00:00:00Z",
    )


def test_remove_local_mode_says_nothing_when_tailscale_was_never_enabled(runner, monkeypatch):
    _register_local_castelo()
    _stub_remove_instance(monkeypatch)
    monkeypatch.setattr(tailscale_ops, "read_state", lambda root: tailscale_ops.TailscaleState(enabled=False))

    def boom(*args, **kwargs):
        raise AssertionError("disable_serve_port should not run when Tailscale was never enabled")
    monkeypatch.setattr(tailscale_ops, "disable_serve_port", boom)

    result = runner.invoke(main, ["remove", "castelo", "--yes"])
    assert result.exit_code == 0
    assert "Tailscale" not in result.output


def test_remove_local_mode_offers_to_turn_off_tailscale_and_confirms(runner, monkeypatch):
    _register_local_castelo()
    _stub_remove_instance(monkeypatch)
    monkeypatch.setattr(tailscale_ops, "read_state", lambda root: _enabled_tailscale_state())
    turned_off = []
    monkeypatch.setattr(tailscale_ops, "disable_serve_port", lambda port, **kwargs: turned_off.append(port))

    result = runner.invoke(main, ["remove", "castelo"], input="y\n")
    assert result.exit_code == 0
    assert "Tailscale Serve is still on for 'castelo' (tailnet ports 443/8443)" in result.output
    assert turned_off == [443, 8443]
    assert "Turned off Tailscale Serve on ports 443 and 8443." in result.output


def test_remove_local_mode_declines_turning_off_tailscale(runner, monkeypatch):
    _register_local_castelo()
    _stub_remove_instance(monkeypatch)
    monkeypatch.setattr(tailscale_ops, "read_state", lambda root: _enabled_tailscale_state())

    def boom(*args, **kwargs):
        raise AssertionError("disable_serve_port should not run once the offer is declined")
    monkeypatch.setattr(tailscale_ops, "disable_serve_port", boom)

    result = runner.invoke(main, ["remove", "castelo"], input="n\n")
    assert result.exit_code == 0
    assert "Skipped. Turn it off later by hand" in result.output


def test_remove_skip_tailscale_cleanup_flag_skips_the_offer_entirely(runner, monkeypatch):
    _register_local_castelo()
    _stub_remove_instance(monkeypatch)

    def boom(*args, **kwargs):
        raise AssertionError("read_state should not run with --skip-tailscale-cleanup")
    monkeypatch.setattr(tailscale_ops, "read_state", boom)

    result = runner.invoke(main, ["remove", "castelo", "--yes", "--skip-tailscale-cleanup"])
    assert result.exit_code == 0
    assert "Tailscale" not in result.output


def test_remove_yes_flag_turns_off_tailscale_without_stdin(runner, monkeypatch):
    _register_local_castelo()
    _stub_remove_instance(monkeypatch)
    monkeypatch.setattr(tailscale_ops, "read_state", lambda root: _enabled_tailscale_state())
    turned_off = []
    monkeypatch.setattr(tailscale_ops, "disable_serve_port", lambda port, **kwargs: turned_off.append(port))

    result = runner.invoke(main, ["remove", "castelo", "--yes"])
    assert result.exit_code == 0
    assert turned_off == [443, 8443]


def test_remove_network_mode_instance_never_checks_tailscale(runner, monkeypatch):
    """Tailscale Serve only ever applies to local-mode instances
    (ops/tailscale.py's own `enable_tailscale_serve` refuses network mode
    outright) -- a network-mode removal should never even read for it."""
    _register_network_castelo()
    _stub_remove_instance(monkeypatch)
    monkeypatch.setattr(proxy_ops, "detect_existing_proxy", lambda runtime, **kwargs: None)

    def boom(*args, **kwargs):
        raise AssertionError("read_state should not run for a network-mode instance")
    monkeypatch.setattr(tailscale_ops, "read_state", boom)

    result = runner.invoke(main, ["remove", "castelo", "--yes"])
    assert result.exit_code == 0
    assert "Tailscale" not in result.output


# ── remove: offer to uninstall Tailscale itself once nothing needs it ───
# Mirrors the SWAG case's own install/remove symmetry: only offered once
# Serve has actually been turned off for this instance (declining that
# leaves the mapping "still in use," same as the SWAG conf-removal
# decline blocks its own teardown offer), no other registered instance
# still has Tailscale enabled, and job-squire is the one that installed
# the client in the first place (`ops/tailscale.py`'s `load_tailscale_
# choice`, set by `ensure_tailscale_ready` -- never true for a client the
# operator already had running).


def test_remove_local_mode_offers_to_uninstall_tailscale_when_last_instance_and_installed_by_cli(runner, monkeypatch):
    _register_local_castelo()
    _stub_remove_instance(monkeypatch)
    monkeypatch.setattr(tailscale_ops, "read_state", lambda root: _enabled_tailscale_state())
    monkeypatch.setattr(tailscale_ops, "disable_serve_port", lambda port, **kwargs: None)
    monkeypatch.setattr(tailscale_ops, "load_tailscale_choice", lambda: {"source": "installed"})
    removed = []
    monkeypatch.setattr(tailscale_ops, "remove_tailscale", lambda **kwargs: removed.append(True))

    result = runner.invoke(main, ["remove", "castelo"], input="y\ny\n")
    assert result.exit_code == 0
    assert "remove it entirely too" in result.output
    assert "Removed Tailscale." in result.output
    assert removed == [True]


def test_remove_local_mode_declines_tailscale_uninstall(runner, monkeypatch):
    _register_local_castelo()
    _stub_remove_instance(monkeypatch)
    monkeypatch.setattr(tailscale_ops, "read_state", lambda root: _enabled_tailscale_state())
    monkeypatch.setattr(tailscale_ops, "disable_serve_port", lambda port, **kwargs: None)
    monkeypatch.setattr(tailscale_ops, "load_tailscale_choice", lambda: {"source": "installed"})

    def boom(*args, **kwargs):
        raise AssertionError("remove_tailscale should not run once declined")
    monkeypatch.setattr(tailscale_ops, "remove_tailscale", boom)

    result = runner.invoke(main, ["remove", "castelo"], input="y\nn\n")
    assert result.exit_code == 0
    assert "Skipped. Remove it later by hand" in result.output


def test_remove_local_mode_skips_tailscale_uninstall_when_another_instance_still_uses_it(runner, monkeypatch):
    _register_local_castelo()
    reg.add_instance(
        name="other", mode="local", runtime="docker", data_dir="/data/other",
        public_url="http://localhost:8081", app_port=8081, mcp_port=9001,
    )
    _stub_remove_instance(monkeypatch)
    monkeypatch.setattr(tailscale_ops, "read_state", lambda root: _enabled_tailscale_state())
    monkeypatch.setattr(tailscale_ops, "disable_serve_port", lambda port, **kwargs: None)
    monkeypatch.setattr(tailscale_ops, "load_tailscale_choice", lambda: {"source": "installed"})

    def boom(*args, **kwargs):
        raise AssertionError("remove_tailscale should not run while another instance still uses it")
    monkeypatch.setattr(tailscale_ops, "remove_tailscale", boom)

    result = runner.invoke(main, ["remove", "castelo"], input="y\n")
    assert result.exit_code == 0
    assert "remove it entirely too" not in result.output


def test_remove_local_mode_never_offers_tailscale_uninstall_when_not_installed_by_cli(runner, monkeypatch):
    _register_local_castelo()
    _stub_remove_instance(monkeypatch)
    monkeypatch.setattr(tailscale_ops, "read_state", lambda root: _enabled_tailscale_state())
    monkeypatch.setattr(tailscale_ops, "disable_serve_port", lambda port, **kwargs: None)
    monkeypatch.setattr(tailscale_ops, "load_tailscale_choice", lambda: {"source": "detected"})

    def boom(*args, **kwargs):
        raise AssertionError("remove_tailscale should never be offered when job-squire didn't install it")
    monkeypatch.setattr(tailscale_ops, "remove_tailscale", boom)

    result = runner.invoke(main, ["remove", "castelo"], input="y\n")
    assert result.exit_code == 0
    assert "remove it entirely too" not in result.output


def test_remove_local_mode_declining_serve_off_blocks_the_uninstall_offer_too(runner, monkeypatch):
    _register_local_castelo()
    _stub_remove_instance(monkeypatch)
    monkeypatch.setattr(tailscale_ops, "read_state", lambda root: _enabled_tailscale_state())
    monkeypatch.setattr(tailscale_ops, "load_tailscale_choice", lambda: {"source": "installed"})

    def boom(*args, **kwargs):
        raise AssertionError("neither disable_serve_port nor remove_tailscale should run")
    monkeypatch.setattr(tailscale_ops, "disable_serve_port", boom)
    monkeypatch.setattr(tailscale_ops, "remove_tailscale", boom)

    result = runner.invoke(main, ["remove", "castelo"], input="n\n")
    assert result.exit_code == 0
    assert "remove it entirely too" not in result.output


# ── uninstall ─────────────────────────────────────────────────────────────


def test_uninstall_with_no_instances_and_no_bootstrap_venv(runner):
    """Run for real (no mocking of ops/uninstall.py): an empty registry and
    pytest's own interpreter, which never matches looks_like_bootstrap_venv,
    so this exercises the real fallback path end to end -- nothing removed,
    a clean pip-uninstall pointer printed instead of a traceback."""
    result = runner.invoke(main, ["uninstall", "--yes"])
    assert result.exit_code == 0
    assert "Traceback" not in result.output
    assert "No instances are registered" in result.output
    assert "pip uninstall job-squire-cli" in result.output
    assert "Runtime left in place" in result.output


def test_uninstall_reports_each_removed_instance(runner, monkeypatch, tmp_path):
    reg.add_instance(
        name="castelo", mode="local", runtime="docker", data_dir=str(tmp_path),
        public_url="http://localhost:8080", app_port=8080, mcp_port=9000,
    )

    def fake_uninstall_everything(**kwargs):
        from job_squire_cli.ops import uninstall as un
        return un.UninstallResult(
            instances_removed=["castelo"], data_kept={"castelo": True},
            runtime_removed=None, cli_removed=None, rc_files_updated=[],
        )

    from job_squire_cli.ops import commands as cmds
    monkeypatch.setattr(cmds.uninstall_ops, "uninstall_everything", fake_uninstall_everything)

    result = runner.invoke(main, ["uninstall", "--yes"])
    assert result.exit_code == 0
    assert "castelo: data kept" in result.output


def test_uninstall_reports_removed_volumes_for_deleted_instances(runner, monkeypatch, tmp_path):
    reg.add_instance(
        name="castelo", mode="local", runtime="docker", data_dir=str(tmp_path),
        public_url="http://localhost:8080", app_port=8080, mcp_port=9000,
    )

    def fake_uninstall_everything(**kwargs):
        from job_squire_cli.ops import uninstall as un
        return un.UninstallResult(
            instances_removed=["castelo"], data_kept={"castelo": False},
            runtime_removed=None, cli_removed=None, rc_files_updated=[],
            volumes_removed={"castelo": ["job-squire-castelo-data"]},
        )

    from job_squire_cli.ops import commands as cmds
    monkeypatch.setattr(cmds.uninstall_ops, "uninstall_everything", fake_uninstall_everything)

    result = runner.invoke(main, ["uninstall", "--yes", "--delete-data"])
    assert result.exit_code == 0
    assert "castelo: data deleted, volume(s) removed: job-squire-castelo-data" in result.output


def test_uninstall_reports_runtime_removed_when_the_orchestration_says_so(runner, monkeypatch):
    def fake_uninstall_everything(**kwargs):
        from job_squire_cli.ops import uninstall as un
        return un.UninstallResult(
            instances_removed=[], data_kept={}, runtime_removed="podman",
            cli_removed=None, rc_files_updated=[],
        )

    from job_squire_cli.ops import commands as cmds
    monkeypatch.setattr(cmds.uninstall_ops, "uninstall_everything", fake_uninstall_everything)

    result = runner.invoke(main, ["uninstall", "--yes", "--remove-runtime"])
    assert result.exit_code == 0
    assert "Runtime removed: podman" in result.output


def test_uninstall_without_yes_prompts_and_defaults_to_not_uninstalling(runner, monkeypatch):
    """Pressing Enter at the confirmation prompt (empty input) must decline
    -- uninstall_everything must never run, and nothing should look like it
    was torn down."""
    called = []
    from job_squire_cli.ops import commands as cmds
    monkeypatch.setattr(
        cmds.uninstall_ops, "uninstall_everything", lambda **kwargs: called.append(kwargs),
    )

    result = runner.invoke(main, ["uninstall"], input="\n")
    assert result.exit_code == 0
    assert called == []
    assert "Aborted -- nothing was uninstalled" in result.output


def test_uninstall_without_yes_declines_on_explicit_no(runner, monkeypatch):
    called = []
    from job_squire_cli.ops import commands as cmds
    monkeypatch.setattr(
        cmds.uninstall_ops, "uninstall_everything", lambda **kwargs: called.append(kwargs),
    )

    result = runner.invoke(main, ["uninstall"], input="n\n")
    assert result.exit_code == 0
    assert called == []
    assert "Aborted -- nothing was uninstalled" in result.output


def test_uninstall_without_yes_proceeds_when_confirmed(runner, monkeypatch):
    """Two prompts now stack without --yes: the top-level "Uninstall
    job-squire?" confirmation, then "Keep the container image(s)...?" --
    the second line of input answers that one (empty = its default, No)."""
    from job_squire_cli.ops import uninstall as un
    from job_squire_cli.ops import commands as cmds
    monkeypatch.setattr(
        cmds.uninstall_ops, "uninstall_everything",
        lambda **kwargs: un.UninstallResult(
            instances_removed=[], data_kept={}, runtime_removed=None, cli_removed=None, rc_files_updated=[],
        ),
    )

    result = runner.invoke(main, ["uninstall"], input="y\n\n")
    assert result.exit_code == 0
    assert "Aborted" not in result.output


def test_uninstall_yes_flag_skips_the_confirmation_prompt(runner):
    """--yes must still bypass the top-level confirmation -- covered
    implicitly by every other --yes test in this file actually reaching
    uninstall_everything, but asserted directly here too."""
    result = runner.invoke(main, ["uninstall", "--yes"])
    assert result.exit_code == 0
    assert "Uninstall job-squire?" not in result.output


# ── uninstall: image removal defaults to on (unlike `remove`) ────────────


def _fake_uninstall_result(**overrides):
    from job_squire_cli.ops import uninstall as un
    defaults = dict(
        instances_removed=["castelo"], data_kept={"castelo": True},
        runtime_removed=None, cli_removed=None, rc_files_updated=[],
        image_removed={"castelo": False}, image_kept_reason={"castelo": None},
    )
    defaults.update(overrides)
    return un.UninstallResult(**defaults)


def test_uninstall_yes_flag_defaults_to_removing_the_image_without_prompting(runner, monkeypatch, tmp_path):
    reg.add_instance(
        name="castelo", mode="local", runtime="docker", data_dir=str(tmp_path),
        public_url="http://localhost:8080", app_port=8080, mcp_port=9000,
    )
    captured = {}

    def fake_uninstall_everything(**kwargs):
        captured.update(kwargs)
        return _fake_uninstall_result(image_removed={"castelo": True})

    from job_squire_cli.ops import commands as cmds
    monkeypatch.setattr(cmds.uninstall_ops, "uninstall_everything", fake_uninstall_everything)

    result = runner.invoke(main, ["uninstall", "--yes"])
    assert result.exit_code == 0
    assert captured["remove_image"] is True
    assert "Keep the container image" not in result.output  # --yes never prompts
    assert "castelo: data kept, image removed" in result.output


def test_uninstall_without_yes_prompts_to_keep_image_defaulting_to_no(runner, monkeypatch, tmp_path):
    """Pressing Enter at the keep-image prompt (its default, No) must still
    result in removal -- the prompt asks whether to *keep* the image, and
    "No" to that means remove it, matching --remove-image being the
    overall default for uninstall."""
    reg.add_instance(
        name="castelo", mode="local", runtime="docker", data_dir=str(tmp_path),
        public_url="http://localhost:8080", app_port=8080, mcp_port=9000,
    )
    captured = {}

    def fake_uninstall_everything(**kwargs):
        captured.update(kwargs)
        return _fake_uninstall_result(image_removed={"castelo": True})

    from job_squire_cli.ops import commands as cmds
    monkeypatch.setattr(cmds.uninstall_ops, "uninstall_everything", fake_uninstall_everything)

    result = runner.invoke(main, ["uninstall"], input="y\n\n")
    assert result.exit_code == 0
    assert "Keep the container image" in result.output
    assert captured["remove_image"] is True
    assert "castelo: data kept, image removed" in result.output


def test_uninstall_without_yes_keeps_the_image_when_answered_yes(runner, monkeypatch, tmp_path):
    reg.add_instance(
        name="castelo", mode="local", runtime="docker", data_dir=str(tmp_path),
        public_url="http://localhost:8080", app_port=8080, mcp_port=9000,
    )
    captured = {}

    def fake_uninstall_everything(**kwargs):
        captured.update(kwargs)
        return _fake_uninstall_result()  # image_removed False for castelo

    from job_squire_cli.ops import commands as cmds
    monkeypatch.setattr(cmds.uninstall_ops, "uninstall_everything", fake_uninstall_everything)

    result = runner.invoke(main, ["uninstall"], input="y\ny\n")
    assert result.exit_code == 0
    assert captured["remove_image"] is False
    assert "castelo: data kept, image kept" in result.output


def test_uninstall_explicit_remove_image_flag_skips_the_keep_image_prompt(runner, monkeypatch, tmp_path):
    reg.add_instance(
        name="castelo", mode="local", runtime="docker", data_dir=str(tmp_path),
        public_url="http://localhost:8080", app_port=8080, mcp_port=9000,
    )
    captured = {}

    def fake_uninstall_everything(**kwargs):
        captured.update(kwargs)
        return _fake_uninstall_result(image_removed={"castelo": True})

    from job_squire_cli.ops import commands as cmds
    monkeypatch.setattr(cmds.uninstall_ops, "uninstall_everything", fake_uninstall_everything)

    # Only one line of input (for the top-level confirm) -- if the
    # keep-image prompt fired anyway, this would hit EOF and abort.
    result = runner.invoke(main, ["uninstall", "--remove-image"], input="y\n")
    assert result.exit_code == 0
    assert "Keep the container image" not in result.output
    assert captured["remove_image"] is True


def test_uninstall_explicit_keep_image_flag_skips_the_keep_image_prompt(runner, monkeypatch, tmp_path):
    reg.add_instance(
        name="castelo", mode="local", runtime="docker", data_dir=str(tmp_path),
        public_url="http://localhost:8080", app_port=8080, mcp_port=9000,
    )
    captured = {}

    def fake_uninstall_everything(**kwargs):
        captured.update(kwargs)
        return _fake_uninstall_result()

    from job_squire_cli.ops import commands as cmds
    monkeypatch.setattr(cmds.uninstall_ops, "uninstall_everything", fake_uninstall_everything)

    result = runner.invoke(main, ["uninstall", "--keep-image"], input="y\n")
    assert result.exit_code == 0
    assert "Keep the container image" not in result.output
    assert captured["remove_image"] is False
    assert "castelo: data kept, image kept" in result.output


def test_uninstall_reports_kept_reason_for_a_shared_image_even_though_removal_is_the_default(
    runner, monkeypatch, tmp_path,
):
    reg.add_instance(
        name="one", mode="local", runtime="docker", data_dir=str(tmp_path / "one"),
        public_url="http://localhost:8080", app_port=8080, mcp_port=9000,
    )
    reg.add_instance(
        name="two", mode="local", runtime="docker", data_dir=str(tmp_path / "two"),
        public_url="http://localhost:8081", app_port=8081, mcp_port=9001,
    )

    def fake_uninstall_everything(**kwargs):
        from job_squire_cli.ops import uninstall as un
        return un.UninstallResult(
            instances_removed=["one", "two"], data_kept={"one": True, "two": True},
            runtime_removed=None, cli_removed=None, rc_files_updated=[],
            image_removed={"one": False, "two": True},
            image_kept_reason={"one": "still used by another registered instance", "two": None},
        )

    from job_squire_cli.ops import commands as cmds
    monkeypatch.setattr(cmds.uninstall_ops, "uninstall_everything", fake_uninstall_everything)

    result = runner.invoke(main, ["uninstall", "--yes"])
    assert result.exit_code == 0
    assert "one: data kept, image kept (still used by another registered instance)" in result.output
    assert "two: data kept, image removed" in result.output


def test_uninstall_reports_when_no_path_entry_was_found_despite_cli_removal(runner, monkeypatch, tmp_path):
    """cli_removed can be truthy while rc_files_updated is empty -- e.g. the
    venv layout matched but no rc file actually carried job-squire's PATH
    line. The summary must not claim a PATH change happened in that case."""
    from job_squire_cli.ops import uninstall as un
    from job_squire_cli.ops import commands as cmds
    monkeypatch.setattr(
        cmds.uninstall_ops, "uninstall_everything",
        lambda **kwargs: un.UninstallResult(
            instances_removed=[], data_kept={}, runtime_removed=None,
            cli_removed=tmp_path / "job-squire", rc_files_updated=[],
        ),
    )

    result = runner.invoke(main, ["uninstall", "--yes"])
    assert result.exit_code == 0
    assert "No PATH entry was found to remove" in result.output
    assert "Open a new terminal" not in result.output


# ── uninstall: proxy/DNS cleanup offer for network-mode instances ───────
# Mirrors `remove`'s own offer (see "remove: proxy/DNS cleanup offer"
# above), but for the whole batch at once: one combined conf-removal
# prompt covering every network-mode instance uninstall just removed, then
# the same full-SWAG-teardown gate (CLI-managed, nothing else using it).


def _stub_uninstall_everything(monkeypatch, *, instances_removed):
    from job_squire_cli.ops import uninstall as un
    from job_squire_cli.ops import commands as cmds
    monkeypatch.setattr(
        cmds.uninstall_ops, "uninstall_everything",
        lambda **kwargs: un.UninstallResult(
            instances_removed=instances_removed,
            data_kept={name: True for name in instances_removed},
            runtime_removed=None, cli_removed=None, rc_files_updated=[],
        ),
    )


def test_uninstall_with_only_local_instances_never_checks_for_a_proxy(runner, monkeypatch, tmp_path):
    reg.add_instance(
        name="castelo", mode="local", runtime="docker", data_dir=str(tmp_path),
        public_url="http://localhost:8080", app_port=8080, mcp_port=9000,
    )
    _stub_uninstall_everything(monkeypatch, instances_removed=["castelo"])

    def boom(*args, **kwargs):
        raise AssertionError("detect_existing_proxy should not run when nothing was network-mode")
    monkeypatch.setattr(proxy_ops, "detect_existing_proxy", boom)

    result = runner.invoke(main, ["uninstall", "--yes"])
    assert result.exit_code == 0
    assert "reverse proxy" not in result.output.lower()


def test_uninstall_offers_combined_conf_removal_and_then_full_swag_teardown(runner, monkeypatch, tmp_path):
    reg.add_instance(
        name="castelo", mode="network", runtime="docker", data_dir=str(tmp_path),
        public_url="https://squire.example.com", app_port=8080, mcp_port=9000,
    )
    _stub_uninstall_everything(monkeypatch, instances_removed=["castelo"])
    _write_fake_confs(tmp_path, "job-squire-castelo.subdomain.conf", "mcp-job-squire-castelo.subdomain.conf")
    monkeypatch.setattr(
        proxy_ops, "detect_existing_proxy",
        lambda runtime, **kwargs: proxy_ops.ProxyTarget(config_dir=tmp_path, container_name="swag", kind="swag"),
    )
    monkeypatch.setattr(proxy_ops, "reload_proxy", lambda *a, **k: None)
    monkeypatch.setattr(proxy_ops, "is_managed_swag", lambda *a, **k: True)
    removed = []
    monkeypatch.setattr(proxy_ops, "remove_managed_swag", lambda runtime, **kwargs: removed.append(runtime))

    # --yes answers every prompt (the big "uninstall everything?" gate, the
    # "keep the image?" gate, and both new proxy/DNS prompts) without stdin.
    result = runner.invoke(main, ["uninstall", "--yes"])
    assert result.exit_code == 0
    assert "castelo" in result.output
    assert "Removed the CLI-installed SWAG proxy and its DNS/TLS configuration." in result.output
    assert removed == ["docker"]


def test_uninstall_declining_conf_removal_leaves_confs_and_never_offers_swag_teardown(runner, monkeypatch, tmp_path):
    reg.add_instance(
        name="castelo", mode="network", runtime="docker", data_dir=str(tmp_path),
        public_url="https://squire.example.com", app_port=8080, mcp_port=9000,
    )
    _stub_uninstall_everything(monkeypatch, instances_removed=["castelo"])
    confs_dir = _write_fake_confs(tmp_path, "job-squire-castelo.subdomain.conf", "mcp-job-squire-castelo.subdomain.conf")
    monkeypatch.setattr(
        proxy_ops, "detect_existing_proxy",
        lambda runtime, **kwargs: proxy_ops.ProxyTarget(config_dir=tmp_path, container_name="swag", kind="swag"),
    )
    monkeypatch.setattr(proxy_ops, "is_managed_swag", lambda *a, **k: True)

    def boom(*args, **kwargs):
        raise AssertionError("remove_managed_swag should not run -- the declined confs are still on disk")
    monkeypatch.setattr(proxy_ops, "remove_managed_swag", boom)

    # --keep-image skips the "keep the image?" prompt so only two prompts
    # remain in order: "uninstall everything?" (y) and the new combined
    # conf-removal offer (n). Declining leaves castelo's confs in place, so
    # the SWAG-teardown gate's own "anything else still using it?" check
    # finds them and never even reaches a third prompt.
    result = runner.invoke(main, ["uninstall", "--keep-image"], input="y\nn\n")
    assert result.exit_code == 0
    assert "Skipped. Their confs are still at" in result.output
    assert "remove it entirely too" not in result.output
    assert (confs_dir / "job-squire-castelo.subdomain.conf").exists()


def test_uninstall_never_tears_down_a_third_party_proxy(runner, monkeypatch, tmp_path):
    reg.add_instance(
        name="castelo", mode="network", runtime="docker", data_dir=str(tmp_path),
        public_url="https://squire.example.com", app_port=8080, mcp_port=9000,
    )
    _stub_uninstall_everything(monkeypatch, instances_removed=["castelo"])
    _write_fake_confs(tmp_path, "job-squire-castelo.subdomain.conf", "mcp-job-squire-castelo.subdomain.conf")
    monkeypatch.setattr(
        proxy_ops, "detect_existing_proxy",
        lambda runtime, **kwargs: proxy_ops.ProxyTarget(config_dir=tmp_path, container_name="swag", kind="swag"),
    )
    monkeypatch.setattr(proxy_ops, "reload_proxy", lambda *a, **k: None)
    monkeypatch.setattr(proxy_ops, "is_managed_swag", lambda *a, **k: False)

    def boom(*args, **kwargs):
        raise AssertionError("remove_managed_swag should never be offered for a third-party proxy")
    monkeypatch.setattr(proxy_ops, "remove_managed_swag", boom)

    result = runner.invoke(main, ["uninstall", "--yes"])
    assert result.exit_code == 0
    assert "remove it entirely too" not in result.output


def test_uninstall_skip_proxy_cleanup_flag_skips_the_offer_entirely(runner, monkeypatch, tmp_path):
    reg.add_instance(
        name="castelo", mode="network", runtime="docker", data_dir=str(tmp_path),
        public_url="https://squire.example.com", app_port=8080, mcp_port=9000,
    )
    _stub_uninstall_everything(monkeypatch, instances_removed=["castelo"])

    def boom(*args, **kwargs):
        raise AssertionError("detect_existing_proxy should not run with --skip-proxy-cleanup")
    monkeypatch.setattr(proxy_ops, "detect_existing_proxy", boom)

    result = runner.invoke(main, ["uninstall", "--yes", "--skip-proxy-cleanup"])
    assert result.exit_code == 0
    assert "reverse proxy" not in result.output.lower()


# ── uninstall: Tailscale Serve cleanup offer for local-mode instances ───
# Mirrors `remove`'s own Tailscale offer, but for the whole batch: one
# combined prompt covering every local-mode instance that had Serve on.


def test_uninstall_with_only_local_instance_no_tailscale_says_nothing(runner, monkeypatch, tmp_path):
    reg.add_instance(
        name="castelo", mode="local", runtime="docker", data_dir=str(tmp_path),
        public_url="http://localhost:8080", app_port=8080, mcp_port=9000,
    )
    _stub_uninstall_everything(monkeypatch, instances_removed=["castelo"])
    monkeypatch.setattr(tailscale_ops, "read_state", lambda root: tailscale_ops.TailscaleState(enabled=False))

    def boom(*args, **kwargs):
        raise AssertionError("disable_serve_port should not run when nothing had Tailscale enabled")
    monkeypatch.setattr(tailscale_ops, "disable_serve_port", boom)

    result = runner.invoke(main, ["uninstall", "--yes"])
    assert result.exit_code == 0
    assert "Tailscale" not in result.output


def test_uninstall_offers_combined_tailscale_cleanup_and_confirms(runner, monkeypatch, tmp_path):
    reg.add_instance(
        name="castelo", mode="local", runtime="docker", data_dir=str(tmp_path),
        public_url="http://localhost:8080", app_port=8080, mcp_port=9000,
    )
    _stub_uninstall_everything(monkeypatch, instances_removed=["castelo"])
    monkeypatch.setattr(tailscale_ops, "read_state", lambda root: _enabled_tailscale_state())
    turned_off = []
    monkeypatch.setattr(tailscale_ops, "disable_serve_port", lambda port, **kwargs: turned_off.append(port))

    result = runner.invoke(main, ["uninstall", "--yes"])
    assert result.exit_code == 0
    assert "Turned off Tailscale Serve for 'castelo' (ports 443 and 8443)." in result.output
    assert turned_off == [443, 8443]


def test_uninstall_declines_combined_tailscale_cleanup(runner, monkeypatch, tmp_path):
    reg.add_instance(
        name="castelo", mode="local", runtime="docker", data_dir=str(tmp_path),
        public_url="http://localhost:8080", app_port=8080, mcp_port=9000,
    )
    _stub_uninstall_everything(monkeypatch, instances_removed=["castelo"])
    monkeypatch.setattr(tailscale_ops, "read_state", lambda root: _enabled_tailscale_state())

    def boom(*args, **kwargs):
        raise AssertionError("disable_serve_port should not run once declined")
    monkeypatch.setattr(tailscale_ops, "disable_serve_port", boom)

    # --keep-image skips the "keep the image?" prompt, and this instance is
    # local-mode only (no proxy prompt either), so exactly two prompts
    # remain: "uninstall everything?" (y) and the combined Tailscale offer (n).
    result = runner.invoke(main, ["uninstall", "--keep-image"], input="y\nn\n")
    assert result.exit_code == 0
    assert "Skipped. Turn it off later by hand" in result.output


def test_uninstall_skip_tailscale_cleanup_flag_skips_the_offer_entirely(runner, monkeypatch, tmp_path):
    reg.add_instance(
        name="castelo", mode="local", runtime="docker", data_dir=str(tmp_path),
        public_url="http://localhost:8080", app_port=8080, mcp_port=9000,
    )
    _stub_uninstall_everything(monkeypatch, instances_removed=["castelo"])

    def boom(*args, **kwargs):
        raise AssertionError("read_state should not run with --skip-tailscale-cleanup")
    monkeypatch.setattr(tailscale_ops, "read_state", boom)

    result = runner.invoke(main, ["uninstall", "--yes", "--skip-tailscale-cleanup"])
    assert result.exit_code == 0
    assert "Tailscale" not in result.output


# ── uninstall: offer to remove Tailscale itself if job-squire installed it ──
# Unlike `remove`'s equivalent offer, this one is unconditional on whether
# any instance currently has Serve enabled -- `uninstall` has already
# removed every registered instance by the time this runs, so "is
# anything else still using it" is trivially settled; the only gate left
# is `load_tailscale_choice` showing job-squire is the one who installed it.


def test_uninstall_offers_to_remove_tailscale_when_installed_by_cli(runner, monkeypatch, tmp_path):
    reg.add_instance(
        name="castelo", mode="local", runtime="docker", data_dir=str(tmp_path),
        public_url="http://localhost:8080", app_port=8080, mcp_port=9000,
    )
    _stub_uninstall_everything(monkeypatch, instances_removed=["castelo"])
    # No instance currently has Serve enabled -- the offer must still fire,
    # since it's driven by load_tailscale_choice, not tailscale_states.
    monkeypatch.setattr(tailscale_ops, "read_state", lambda root: tailscale_ops.TailscaleState(enabled=False))
    monkeypatch.setattr(tailscale_ops, "load_tailscale_choice", lambda: {"source": "installed"})
    removed = []
    monkeypatch.setattr(tailscale_ops, "remove_tailscale", lambda **kwargs: removed.append(True))

    result = runner.invoke(main, ["uninstall", "--yes"])
    assert result.exit_code == 0
    assert "Removed Tailscale." in result.output
    assert removed == [True]


def test_uninstall_declines_removing_tailscale(runner, monkeypatch, tmp_path):
    reg.add_instance(
        name="castelo", mode="local", runtime="docker", data_dir=str(tmp_path),
        public_url="http://localhost:8080", app_port=8080, mcp_port=9000,
    )
    _stub_uninstall_everything(monkeypatch, instances_removed=["castelo"])
    monkeypatch.setattr(tailscale_ops, "read_state", lambda root: tailscale_ops.TailscaleState(enabled=False))
    monkeypatch.setattr(tailscale_ops, "load_tailscale_choice", lambda: {"source": "installed"})

    def boom(*args, **kwargs):
        raise AssertionError("remove_tailscale should not run once declined")
    monkeypatch.setattr(tailscale_ops, "remove_tailscale", boom)

    # --keep-image skips the "keep the image?" prompt, and nothing here is
    # network-mode, so exactly two prompts remain: "uninstall everything?"
    # (y) and "remove Tailscale entirely?" (n).
    result = runner.invoke(main, ["uninstall", "--keep-image"], input="y\nn\n")
    assert result.exit_code == 0
    assert "Skipped. Remove it later by hand" in result.output


def test_uninstall_never_offers_to_remove_tailscale_when_not_installed_by_cli(runner, monkeypatch, tmp_path):
    reg.add_instance(
        name="castelo", mode="local", runtime="docker", data_dir=str(tmp_path),
        public_url="http://localhost:8080", app_port=8080, mcp_port=9000,
    )
    _stub_uninstall_everything(monkeypatch, instances_removed=["castelo"])
    monkeypatch.setattr(tailscale_ops, "read_state", lambda root: tailscale_ops.TailscaleState(enabled=False))
    monkeypatch.setattr(tailscale_ops, "load_tailscale_choice", lambda: None)

    def boom(*args, **kwargs):
        raise AssertionError("remove_tailscale should never be offered when job-squire didn't install it")
    monkeypatch.setattr(tailscale_ops, "remove_tailscale", boom)

    result = runner.invoke(main, ["uninstall", "--yes"])
    assert result.exit_code == 0
    assert "Tailscale" not in result.output


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


# ── automatic Ollama check/offer at the tail of `create` ────────────────
# `create` now runs ops/ollama_assist.py's capability check on every
# successful instance creation (bootstrap.sh hands off to `job-squire
# create`, so this is where "check after the container is up" lands) and
# offers to install/configure Ollama when the machine can reasonably run
# local models. These tests stub both lc.create_instance (a real one needs
# a container runtime) and ollama_assist.detect_host_capabilities/run_setup
# so only the click-layer wiring in commands.py is under test.


def _fake_create_result(name="castelo", data_dir=".", mode="local", public_url="http://localhost:8080"):
    inst = reg.Instance(
        name=name, mode=mode, runtime="docker", data_dir=data_dir,
        app_port=8080, mcp_port=9000, cookie_name="job_squire_session",
        public_url=public_url, created="2026-07-17T00:00:00Z",
    )
    return lc.CreateResult(
        instance=inst, admin_username="admin", admin_password="generated-pw",
        admin_password_generated=True, health=None, import_summary=None,
    )


def _caps(*, ollama_installed, ram_gb=16.0):
    return ollama_assist.HostCapabilities(
        detected_at="2026-07-17T00:00:00Z", os="Linux", apple_silicon=False,
        ram_gb=ram_gb, cpu_cores=8, gpu_vendor=None, gpu_vram_gb=None,
        ollama_installed=ollama_installed, ollama_running=False,
    )


# ── automatic reverse-proxy offer at the tail of `create` ───────────────
# For a network-mode instance, `create` now also offers to provision a
# reverse proxy right after the container comes up -- configuring an
# existing SWAG/nginx proxy if one is detected, or offering to install a
# fresh SWAG container if not (see `_offer_proxy_setup` in commands.py,
# and the standalone `job-squire proxy NAME` command it mirrors). Added
# because bootstrap.sh hands off straight into `create`, and an operator
# following that path had no reason to know `job-squire proxy` was a
# separate step they still needed to run -- `create` alone never touched
# ops/proxy.py at all before this. These tests stub lc.create_instance,
# proxy_ops.detect_existing_proxy/provision_instance_proxy, and
# ollama_assist.detect_host_capabilities (kept silent/uncapable so the
# unrelated Ollama offer doesn't add noise to the assertions) so only the
# click-layer wiring in commands.py is under test.


def _silence_ollama(monkeypatch):
    monkeypatch.setattr(ollama_assist, "detect_host_capabilities", lambda **kwargs: _caps(ollama_installed=False, ram_gb=4.0))


def test_create_local_mode_never_checks_for_a_proxy(runner, monkeypatch, tmp_path):
    monkeypatch.setattr(lc, "create_instance", lambda **kwargs: _fake_create_result(data_dir=str(tmp_path), mode="local"))
    _silence_ollama(monkeypatch)

    def boom(*args, **kwargs):
        raise AssertionError("detect_existing_proxy should not run for a local-mode instance")
    monkeypatch.setattr(proxy_ops, "detect_existing_proxy", boom)

    result = runner.invoke(main, ["create", "castelo", "--mode", "local", "--yes"])
    assert result.exit_code == 0
    assert "reverse proxy" not in result.output.lower()


def test_create_network_mode_offers_to_configure_an_existing_proxy(runner, monkeypatch, tmp_path):
    monkeypatch.setattr(
        lc, "create_instance",
        lambda **kwargs: _fake_create_result(data_dir=str(tmp_path), mode="network", public_url="https://squire.example.com"),
    )
    _silence_ollama(monkeypatch)
    monkeypatch.setattr(
        proxy_ops, "detect_existing_proxy",
        lambda runtime, **kwargs: proxy_ops.ProxyTarget(config_dir=tmp_path, container_name="swag", kind="swag"),
    )
    web_path, mcp_path = tmp_path / "job-squire-castelo.subdomain.conf", tmp_path / "mcp-job-squire-castelo.subdomain.conf"

    def fake_provision(instance, **kwargs):
        return proxy_ops.ProxyProvisionResult(
            proxy=proxy_ops.ProxyTarget(config_dir=tmp_path, container_name="swag", kind="swag"),
            network="job-squire-proxy", web_conf_path=web_path, mcp_conf_path=mcp_path, installed_swag=False,
        )
    monkeypatch.setattr(proxy_ops, "provision_instance_proxy", fake_provision)

    result = runner.invoke(
        main, ["create", "castelo", "--mode", "network", "--hostname", "squire.example.com"], input="y\n",
    )
    assert result.exit_code == 0
    assert "An existing reverse proxy (swag) was found on this machine" in result.output
    assert "Reverse proxy provisioned (swag)" in result.output
    assert f"Web conf installed: {web_path}" in result.output
    assert f"MCP conf installed: {mcp_path}" in result.output
    assert "Installed a new SWAG container" not in result.output


def test_create_network_mode_declines_configuring_existing_proxy(runner, monkeypatch, tmp_path):
    monkeypatch.setattr(
        lc, "create_instance",
        lambda **kwargs: _fake_create_result(data_dir=str(tmp_path), mode="network", public_url="https://squire.example.com"),
    )
    _silence_ollama(monkeypatch)
    monkeypatch.setattr(
        proxy_ops, "detect_existing_proxy",
        lambda runtime, **kwargs: proxy_ops.ProxyTarget(config_dir=tmp_path, container_name="swag", kind="swag"),
    )

    def boom(*args, **kwargs):
        raise AssertionError("provision_instance_proxy should not run once the offer is declined")
    monkeypatch.setattr(proxy_ops, "provision_instance_proxy", boom)

    result = runner.invoke(
        main, ["create", "castelo", "--mode", "network", "--hostname", "squire.example.com"], input="n\n",
    )
    assert result.exit_code == 0
    assert "Skipped. Run `job-squire proxy castelo` later" in result.output


def test_create_network_mode_installs_swag_when_none_detected_and_confirmed(runner, monkeypatch, tmp_path):
    monkeypatch.setattr(
        lc, "create_instance",
        lambda **kwargs: _fake_create_result(data_dir=str(tmp_path), mode="network", public_url="https://squire.example.com"),
    )
    _silence_ollama(monkeypatch)
    monkeypatch.setattr(proxy_ops, "detect_existing_proxy", lambda runtime, **kwargs: None)
    web_path, mcp_path = tmp_path / "job-squire-castelo.subdomain.conf", tmp_path / "mcp-job-squire-castelo.subdomain.conf"

    def fake_provision(instance, **kwargs):
        return proxy_ops.ProxyProvisionResult(
            proxy=proxy_ops.ProxyTarget(config_dir=tmp_path / "_proxy" / "config", container_name="job-squire-swag", kind="swag"),
            network="job-squire-proxy", web_conf_path=web_path, mcp_conf_path=mcp_path, installed_swag=True,
        )
    monkeypatch.setattr(proxy_ops, "provision_instance_proxy", fake_provision)

    result = runner.invoke(
        main, ["create", "castelo", "--mode", "network", "--hostname", "squire.example.com"], input="y\n",
    )
    assert result.exit_code == 0
    assert "No reverse proxy was found on this machine. Install a LinuxServer SWAG" in result.output
    assert "Installed a new SWAG container" in result.output
    assert "job-squire dns duckdns" in result.output


def test_create_network_mode_proxy_setup_failure_does_not_fail_create(runner, monkeypatch, tmp_path):
    monkeypatch.setattr(
        lc, "create_instance",
        lambda **kwargs: _fake_create_result(data_dir=str(tmp_path), mode="network", public_url="https://squire.example.com"),
    )
    _silence_ollama(monkeypatch)
    monkeypatch.setattr(proxy_ops, "detect_existing_proxy", lambda runtime, **kwargs: None)

    def fake_provision(instance, **kwargs):
        raise proxy_ops.ProxyError("Failed to bring up SWAG: boom")
    monkeypatch.setattr(proxy_ops, "provision_instance_proxy", fake_provision)

    result = runner.invoke(
        main, ["create", "castelo", "--mode", "network", "--hostname", "squire.example.com"], input="y\n",
    )
    assert result.exit_code == 0
    assert "Reverse proxy setup failed: Failed to bring up SWAG: boom" in result.output
    assert "Re-run later with `job-squire proxy castelo`" in result.output


def test_create_skip_proxy_setup_flag_skips_the_offer_entirely(runner, monkeypatch, tmp_path):
    monkeypatch.setattr(
        lc, "create_instance",
        lambda **kwargs: _fake_create_result(data_dir=str(tmp_path), mode="network", public_url="https://squire.example.com"),
    )
    _silence_ollama(monkeypatch)

    def boom(*args, **kwargs):
        raise AssertionError("detect_existing_proxy should not run with --skip-proxy-setup")
    monkeypatch.setattr(proxy_ops, "detect_existing_proxy", boom)

    result = runner.invoke(
        main, ["create", "castelo", "--mode", "network", "--hostname", "squire.example.com", "--skip-proxy-setup", "--yes"],
    )
    assert result.exit_code == 0
    assert "reverse proxy" not in result.output.lower()


def test_create_yes_flag_skips_the_proxy_prompt_too(runner, monkeypatch, tmp_path):
    monkeypatch.setattr(
        lc, "create_instance",
        lambda **kwargs: _fake_create_result(data_dir=str(tmp_path), mode="network", public_url="https://squire.example.com"),
    )
    _silence_ollama(monkeypatch)
    monkeypatch.setattr(
        proxy_ops, "detect_existing_proxy",
        lambda runtime, **kwargs: proxy_ops.ProxyTarget(config_dir=tmp_path, container_name="swag", kind="swag"),
    )
    web_path, mcp_path = tmp_path / "web.conf", tmp_path / "mcp.conf"
    monkeypatch.setattr(
        proxy_ops, "provision_instance_proxy",
        lambda instance, **kwargs: proxy_ops.ProxyProvisionResult(
            proxy=proxy_ops.ProxyTarget(config_dir=tmp_path, container_name="swag", kind="swag"),
            network="job-squire-proxy", web_conf_path=web_path, mcp_conf_path=mcp_path, installed_swag=False,
        ),
    )

    # No `input=` at all -- --yes must answer the proxy prompt without reading stdin.
    result = runner.invoke(main, ["create", "castelo", "--mode", "network", "--hostname", "squire.example.com", "--yes"])
    assert result.exit_code == 0
    assert "Reverse proxy provisioned (swag)" in result.output


def test_create_offers_ollama_install_when_capable_and_not_installed(runner, monkeypatch, tmp_path):
    monkeypatch.setattr(lc, "create_instance", lambda **kwargs: _fake_create_result(data_dir=str(tmp_path)))
    monkeypatch.setattr(ollama_assist, "detect_host_capabilities", lambda **kwargs: _caps(ollama_installed=False))

    result = runner.invoke(main, ["create", "castelo", "--mode", "local"], input="n\n")
    assert result.exit_code == 0
    assert "This machine can run local AI models via Ollama" in result.output
    assert "Install Ollama and configure 'castelo'" in result.output
    assert "Skipped. Run `job-squire ollama setup castelo`" in result.output


def test_create_configures_ollama_when_already_installed_and_confirmed(runner, monkeypatch, tmp_path):
    monkeypatch.setattr(lc, "create_instance", lambda **kwargs: _fake_create_result(data_dir=str(tmp_path)))
    monkeypatch.setattr(ollama_assist, "detect_host_capabilities", lambda **kwargs: _caps(ollama_installed=True))

    def fake_run_setup(root, **kwargs):
        rec = ollama_assist.TIER_TABLE[ollama_assist.TIER_CAPABLE]
        return ollama_assist.SetupResult(
            capabilities=_caps(ollama_installed=True), tier=ollama_assist.TIER_CAPABLE, recommendation=rec,
            host_capabilities_path=None, models_pulled=[rec.triage_model], models_derived={},
            num_ctx=rec.num_ctx, base_url="http://host.docker.internal:11434/v1",
            provider_configured=True, automatic_features_enabled=True, roundtrip_ok=True, roundtrip_detail="ok",
        )
    monkeypatch.setattr(ollama_assist, "run_setup", fake_run_setup)

    result = runner.invoke(main, ["create", "castelo", "--mode", "local"], input="y\n")
    assert result.exit_code == 0
    assert "Ollama is already installed" in result.output
    assert "Configured Ollama provider for 'castelo'" in result.output
    assert "Enabled Automatic AI Features" in result.output
    assert "Round-trip test: ok" in result.output


def test_create_says_nothing_about_ollama_when_tier_not_reasonable(runner, monkeypatch, tmp_path):
    monkeypatch.setattr(lc, "create_instance", lambda **kwargs: _fake_create_result(data_dir=str(tmp_path)))
    monkeypatch.setattr(
        ollama_assist, "detect_host_capabilities", lambda **kwargs: _caps(ollama_installed=False, ram_gb=4.0),
    )

    result = runner.invoke(main, ["create", "castelo", "--mode", "local", "--yes"])
    assert result.exit_code == 0
    assert "Ollama" not in result.output


def test_create_skip_ollama_check_flag_skips_the_check_entirely(runner, monkeypatch, tmp_path):
    monkeypatch.setattr(lc, "create_instance", lambda **kwargs: _fake_create_result(data_dir=str(tmp_path)))

    def boom(**kwargs):
        raise AssertionError("detect_host_capabilities should not run with --skip-ollama-check")
    monkeypatch.setattr(ollama_assist, "detect_host_capabilities", boom)

    result = runner.invoke(main, ["create", "castelo", "--mode", "local", "--yes", "--skip-ollama-check"])
    assert result.exit_code == 0
    assert "Ollama" not in result.output


# ── update / rollback ──────────────────────────────────────────────────


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


def test_update_with_all_flag_updates_every_instance(runner, monkeypatch, tmp_path):
    reg.add_instance(
        name="castelo", mode="local", runtime="docker", data_dir=str(tmp_path / "castelo"),
        public_url="http://localhost:8080", app_port=8080, mcp_port=9000,
    )
    reg.add_instance(
        name="segunda", mode="local", runtime="docker", data_dir=str(tmp_path / "segunda"),
        public_url="http://localhost:8081", app_port=8081, mcp_port=9001,
    )
    calls = []

    def fake_update_instance(name, *, version):
        calls.append(name)
        return lc.UpdateResult(instance=None, previous_image="old", new_image="new", health=None)

    monkeypatch.setattr(lc, "update_instance", fake_update_instance)

    result = runner.invoke(main, ["update", "--all"])
    assert result.exit_code == 0
    assert calls == ["castelo", "segunda"]
    assert "'castelo'" in result.output and "'segunda'" in result.output


def test_update_rejects_name_and_all_together(runner):
    result = runner.invoke(main, ["update", "castelo", "--all"])
    assert result.exit_code == 1
    assert "not both" in result.output


def test_update_all_with_no_instances_fails_cleanly(runner):
    result = runner.invoke(main, ["update", "--all"])
    assert result.exit_code == 1
    assert "nothing to update" in result.output


def test_bare_update_only_self_updates_and_touches_no_instance(runner, monkeypatch):
    calls = []
    monkeypatch.setattr(lc, "update_instance", lambda *a, **k: calls.append(a))
    result = runner.invoke(main, ["update"])
    assert result.exit_code == 0
    assert calls == []
    assert "already up to date" in result.output


# ── self-update (job-squire update itself, before any instance) ─────────


def test_update_reports_self_update_before_instance_move(runner, monkeypatch):
    from job_squire_cli.ops import commands as cmds
    monkeypatch.setattr(
        cmds.self_update, "self_update",
        lambda version=None: su.SelfUpdateResult(
            updated=True, previous_version="0.6.0+aaa", new_version="0.7.0+bbb", tag="v0.7.0",
        ),
    )
    monkeypatch.setattr(
        lc, "update_instance",
        lambda name, *, version: lc.UpdateResult(instance=None, previous_image="a", new_image="b", health=None),
    )
    result = runner.invoke(main, ["update", "castelo"])
    assert result.exit_code == 0
    self_update_line = result.output.index("0.6.0+aaa -> 0.7.0+bbb")
    instance_line = result.output.index("Instance 'castelo'")
    assert self_update_line < instance_line  # self-update happens first, unconditionally


def test_update_self_update_failure_is_a_warning_not_fatal(runner, monkeypatch):
    from job_squire_cli.ops import commands as cmds

    def fake_self_update(version=None):
        raise su.SelfUpdateError("Could not reach the GitHub releases API (offline).")

    monkeypatch.setattr(cmds.self_update, "self_update", fake_self_update)
    monkeypatch.setattr(
        lc, "update_instance",
        lambda name, *, version: lc.UpdateResult(instance=None, previous_image="a", new_image="b", health=None),
    )
    result = runner.invoke(main, ["update", "castelo"])
    assert result.exit_code == 0  # instance update still succeeds
    assert "Warning: could not update the job-squire CLI itself" in result.output
    assert "Instance 'castelo' updated" in result.output


def test_update_skip_self_update_never_calls_self_update(runner, monkeypatch):
    from job_squire_cli.ops import commands as cmds
    called = []
    monkeypatch.setattr(cmds.self_update, "self_update", lambda version=None: called.append(version))
    monkeypatch.setattr(
        lc, "update_instance",
        lambda name, *, version: lc.UpdateResult(instance=None, previous_image="a", new_image="b", health=None),
    )
    result = runner.invoke(main, ["update", "castelo", "--skip-self-update"])
    assert result.exit_code == 0
    assert called == []


def test_update_cli_version_passed_through_to_self_update(runner, monkeypatch):
    from job_squire_cli.ops import commands as cmds
    captured = {}

    def fake_self_update(version=None):
        captured["version"] = version
        return su.SelfUpdateResult(updated=False, previous_version="x", new_version="x", tag="v0.6.0")

    monkeypatch.setattr(cmds.self_update, "self_update", fake_self_update)
    result = runner.invoke(main, ["update", "--cli-version", "0.6.0"])
    assert result.exit_code == 0
    assert captured["version"] == "0.6.0"


# ── backup / restore ──────────────────────────────────────────────────
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


# ── proxy ──────────────────────────────────────────────────────────────
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


# ── dns ────────────────────────────────────────────────────────────────
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


# ── tailscale ─────────────────────────────────────────────────────────
# Thin adapter tests only, same philosophy as proxy/dns above: ops/
# tailscale.py's own behavior (Serve invocations, data/.env rewriting,
# registry/state updates) is covered exhaustively in test_tailscale.py with
# a fully injected fake runtime; here the point is just proving the click
# commands parse their options and surface ops/tailscale.py's exceptions
# and results cleanly.


def _register_local_instance(tmp_path, name="castelo"):
    reg.add_instance(
        name=name, mode="local", runtime="docker", data_dir=str(tmp_path),
        public_url="http://localhost:8080", app_port=8080, mcp_port=9000,
    )


def test_tailscale_enable_unregistered_instance_fails_cleanly(runner):
    result = runner.invoke(main, ["tailscale", "enable", "ghost"])
    assert result.exit_code == 1
    assert "No instance named 'ghost'" in result.output
    assert "Traceback" not in result.output


def test_tailscale_enable_happy_path_prints_result(runner, monkeypatch, tmp_path):
    _register_local_instance(tmp_path)
    monkeypatch.setattr(
        tailscale_ops, "ensure_tailscale_ready",
        lambda **kwargs: tailscale_ops.TailscaleReadiness(installed_by_cli=False, hostname="castelo.tail1234.ts.net"),
    )
    captured = {}

    def fake_enable(instance, *, root, web_port, mcp_port):
        captured.update(name=instance.name, web_port=web_port, mcp_port=mcp_port)
        return tailscale_ops.TailscaleEnableResult(
            hostname="castelo.tail1234.ts.net", web_port=web_port, mcp_port=mcp_port,
            public_url="https://castelo.tail1234.ts.net",
            public_mcp_url="https://castelo.tail1234.ts.net:8443",
            health={"Status": "running", "Health": {"Status": "healthy"}},
            expected_warning="expect a WARNING banner about PUBLIC_URL -- that's normal here.",
        )

    monkeypatch.setattr(tailscale_ops, "enable_tailscale_serve", fake_enable)
    result = runner.invoke(main, ["tailscale", "enable", "castelo"])
    assert result.exit_code == 0, result.output
    assert captured == {"name": "castelo", "web_port": 443, "mcp_port": 8443}
    assert "Tailscale Serve enabled for 'castelo'" in result.output
    assert "https://castelo.tail1234.ts.net" in result.output
    assert "prefer OAuth" in result.output


def test_tailscale_enable_custom_ports(runner, monkeypatch, tmp_path):
    _register_local_instance(tmp_path)
    monkeypatch.setattr(
        tailscale_ops, "ensure_tailscale_ready",
        lambda **kwargs: tailscale_ops.TailscaleReadiness(installed_by_cli=False, hostname="castelo.tail1234.ts.net"),
    )
    captured = {}

    def fake_enable(instance, *, root, web_port, mcp_port):
        captured.update(web_port=web_port, mcp_port=mcp_port)
        return tailscale_ops.TailscaleEnableResult(
            hostname="castelo.tail1234.ts.net", web_port=web_port, mcp_port=mcp_port,
            public_url="https://castelo.tail1234.ts.net:8443",
            public_mcp_url="https://castelo.tail1234.ts.net:10000",
            health=None, expected_warning="",
        )

    monkeypatch.setattr(tailscale_ops, "enable_tailscale_serve", fake_enable)
    result = runner.invoke(
        main, ["tailscale", "enable", "castelo", "--web-port", "8443", "--mcp-port", "10000"],
    )
    assert result.exit_code == 0, result.output
    assert captured == {"web_port": 8443, "mcp_port": 10000}


def test_tailscale_enable_rejects_an_unsupported_port(runner, tmp_path):
    _register_local_instance(tmp_path)
    result = runner.invoke(main, ["tailscale", "enable", "castelo", "--web-port", "9999"])
    assert result.exit_code != 0
    assert "Traceback" not in result.output


def test_tailscale_enable_propagates_tailscale_error_as_clean_exit(runner, monkeypatch, tmp_path):
    _register_local_instance(tmp_path)
    monkeypatch.setattr(
        tailscale_ops, "ensure_tailscale_ready",
        lambda **kwargs: tailscale_ops.TailscaleReadiness(installed_by_cli=False, hostname="castelo.tail1234.ts.net"),
    )

    def fake_enable(instance, *, root, web_port, mcp_port):
        raise tailscale_ops.TailscaleError("tailscale status failed")

    monkeypatch.setattr(tailscale_ops, "enable_tailscale_serve", fake_enable)
    result = runner.invoke(main, ["tailscale", "enable", "castelo"])
    assert result.exit_code == 1
    assert "tailscale status failed" in result.output
    assert "Traceback" not in result.output


def test_tailscale_enable_propagates_readiness_error_as_clean_exit(runner, monkeypatch, tmp_path):
    """A distinct failure point from the above: `ensure_tailscale_ready` itself can fail
    (e.g. install declined, or `tailscale up` never completed) before `enable_tailscale_serve`
    is ever reached."""
    _register_local_instance(tmp_path)

    def fake_ready(**kwargs):
        raise tailscale_ops.TailscaleError("Tailscale is not installed, and installation was declined.")

    monkeypatch.setattr(tailscale_ops, "ensure_tailscale_ready", fake_ready)

    def boom(*args, **kwargs):
        raise AssertionError("enable_tailscale_serve should not run once ensure_tailscale_ready fails")
    monkeypatch.setattr(tailscale_ops, "enable_tailscale_serve", boom)

    result = runner.invoke(main, ["tailscale", "enable", "castelo"])
    assert result.exit_code == 1
    assert "installation was declined" in result.output
    assert "Traceback" not in result.output


def test_tailscale_disable_unregistered_instance_fails_cleanly(runner):
    result = runner.invoke(main, ["tailscale", "disable", "ghost"])
    assert result.exit_code == 1
    assert "No instance named 'ghost'" in result.output


def test_tailscale_disable_happy_path_prints_result(runner, monkeypatch, tmp_path):
    _register_local_instance(tmp_path)

    def fake_disable(instance, *, root):
        return tailscale_ops.TailscaleDisableResult(
            public_url="http://localhost:8080", health={"Status": "running"},
        )

    monkeypatch.setattr(tailscale_ops, "disable_tailscale_serve", fake_disable)
    result = runner.invoke(main, ["tailscale", "disable", "castelo"])
    assert result.exit_code == 0, result.output
    assert "Tailscale Serve disabled for 'castelo'" in result.output
    assert "http://localhost:8080" in result.output


def test_tailscale_disable_propagates_error_when_not_enabled(runner, monkeypatch, tmp_path):
    _register_local_instance(tmp_path)

    def fake_disable(instance, *, root):
        raise tailscale_ops.TailscaleError("does not have Tailscale Serve enabled")

    monkeypatch.setattr(tailscale_ops, "disable_tailscale_serve", fake_disable)
    result = runner.invoke(main, ["tailscale", "disable", "castelo"])
    assert result.exit_code == 1
    assert "does not have Tailscale Serve enabled" in result.output


def test_tailscale_status_not_enabled(runner, tmp_path):
    _register_local_instance(tmp_path)
    result = runner.invoke(main, ["tailscale", "status", "castelo"])
    assert result.exit_code == 0
    assert "is not enabled" in result.output


def test_tailscale_status_enabled(runner, monkeypatch, tmp_path):
    _register_local_instance(tmp_path)
    monkeypatch.setattr(
        tailscale_ops, "read_state",
        lambda root: tailscale_ops.TailscaleState(
            enabled=True, hostname="castelo.tail1234.ts.net", web_port=443, mcp_port=8443,
            enabled_at="2026-07-11T12:00:00Z",
        ),
    )
    result = runner.invoke(main, ["tailscale", "status", "castelo"])
    assert result.exit_code == 0
    assert "enabled for 'castelo'" in result.output
    assert "castelo.tail1234.ts.net" in result.output
