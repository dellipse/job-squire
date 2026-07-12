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
"""Reverse-proxy provisioning (Prompt C9).

Every subprocess call is injected, same pattern as test_runtime.py and
test_compose.py, so this never touches a real container runtime or a real
proxy. `FakeRun` dispatches by argv prefix to a canned response instead of
the single-shape fakes those modules use, since this module drives several
different subcommands (`ps`, `inspect` twice over with different
`--format` values, `network create`/`connect`, `exec ... nginx -s reload`,
and `compose up`) against the same fake runtime in one test.
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from job_squire_cli.ops import compose, paths, proxy
from job_squire_cli.ops.registry import Instance


class FakeRun:
    """Matches subprocess calls by argv prefix (longest match wins) against
    a table of canned `(returncode, stdout, stderr)` responses. Any call
    that matches nothing fails the test loudly, same philosophy as
    test_runtime.py's `fake_run` -- a stray unmocked subprocess call should
    never pass silently.
    """

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


def make_instance(**overrides) -> Instance:
    defaults = dict(
        name="castelo", mode="network", runtime="docker", data_dir="/instances/castelo",
        app_port=8081, mcp_port=9001, cookie_name="castelo_session",
        public_url="https://squire.example.com", created="2026-07-11",
    )
    defaults.update(overrides)
    return Instance(**defaults)


@pytest.fixture
def instance_root(tmp_path):
    root = tmp_path / "castelo"
    paths.data_dir(root).mkdir(parents=True)
    compose.write_compose_files(
        root, container_name="job-squire-castelo", image="ghcr.io/dellipse/job-squire:latest",
        loopback_only=False, app_port=8081, mcp_port=9001,
    )
    paths.data_env_path(root).write_text(
        "SECRET_KEY=deadbeef\nPUBLIC_MCP_HOST=mcp-squire.example.com\nMCP_PORT=9000\n"
    )
    return root


# ── conf rendering ───────────────────────────────────────────────────────


def test_hostname_label_takes_leftmost_dns_label():
    assert proxy._hostname_label("squire.example.com") == "squire"
    assert proxy._hostname_label("squire") == "squire"
    assert proxy._hostname_label("") == ""


def test_derive_subdomains_reads_public_mcp_host_from_data_env(instance_root):
    instance = make_instance()
    web, mcp = proxy.derive_subdomains(instance, instance_root)
    assert (web, mcp) == ("squire", "mcp-squire")


def test_derive_subdomains_falls_back_to_mcp_prefix_convention(tmp_path):
    root = tmp_path / "no-mcp-host"
    paths.data_dir(root).mkdir(parents=True)
    paths.data_env_path(root).write_text("SECRET_KEY=x\n")
    instance = make_instance(public_url="https://squire.example.com")
    web, mcp = proxy.derive_subdomains(instance, root)
    assert (web, mcp) == ("squire", "mcp-squire")


def test_conf_filenames_are_namespaced_per_instance():
    web_name, mcp_name = proxy.conf_filenames("castelo")
    assert web_name == "job-squire-castelo.subdomain.conf"
    assert mcp_name == "mcp-job-squire-castelo.subdomain.conf"
    assert web_name != mcp_name


def test_render_web_conf_containerized_resolves_by_name():
    text = proxy.render_web_conf(
        instance_name="castelo", subdomain="squire", proxy_container="swag",
        mcp_port_note=9000, upstream_block=proxy._container_upstream_block("job-squire-castelo", 8000),
    )
    assert "server_name squire.*;" in text
    assert "set $upstream_app job-squire-castelo;" in text
    assert "set $upstream_port 8000;" in text
    assert "resolver 127.0.0.11" in text
    assert "proxy_pass http://127.0.0.1" not in text


def test_render_web_conf_hostport_fallback_has_no_docker_resolution():
    text = proxy.render_web_conf(
        instance_name="castelo", subdomain="squire", proxy_container=None,
        mcp_port_note=9000, upstream_block=proxy._hostport_upstream_block(8081),
    )
    assert "proxy_pass http://127.0.0.1:8081;" in text
    assert "resolver" not in text
    assert "$upstream_app" not in text


def test_render_mcp_conf_uses_mcp_port_and_http2_off():
    text = proxy.render_mcp_conf(
        instance_name="castelo", subdomain="mcp-squire",
        upstream_block=proxy._container_upstream_block("job-squire-castelo", 9000),
    )
    assert "server_name mcp-squire.*;" in text
    assert "http2 off;" in text
    assert "set $upstream_port 9000;" in text


def test_install_confs_containerized_writes_both_files_under_proxy_confs(tmp_path):
    target = proxy.ProxyTarget(config_dir=tmp_path / "swag-config", container_name="swag", kind="swag")
    web_path, mcp_path = proxy.install_confs(
        target, instance_name="castelo", subdomain_web="squire", subdomain_mcp="mcp-squire",
        container_name="job-squire-castelo", app_port=8081, mcp_port_host=9001, mcp_port_internal=9000,
    )
    assert web_path == tmp_path / "swag-config" / "nginx" / "proxy-confs" / "job-squire-castelo.subdomain.conf"
    assert mcp_path.exists()
    assert "job-squire-castelo" in web_path.read_text()
    assert "set $upstream_port 9000;" in mcp_path.read_text()


def test_install_confs_manual_proxy_uses_host_ports(tmp_path):
    target = proxy.ProxyTarget(config_dir=tmp_path / "nginx-confd", container_name=None, kind="manual")
    web_path, mcp_path = proxy.install_confs(
        target, instance_name="castelo", subdomain_web="squire", subdomain_mcp="mcp-squire",
        container_name="job-squire-castelo", app_port=8081, mcp_port_host=9001, mcp_port_internal=9000,
    )
    assert "proxy_pass http://127.0.0.1:8081;" in web_path.read_text()
    assert "proxy_pass http://127.0.0.1:9001;" in mcp_path.read_text()


# ── detection ─────────────────────────────────────────────────────────────


def test_list_running_containers_parses_ps_output():
    run = FakeRun().on(("docker", "ps"), stdout="swag\tlscr.io/linuxserver/swag\njob-squire-castelo\tghcr.io/dellipse/job-squire:latest\n")
    result = proxy.list_running_containers("docker", run=run)
    assert result == [("swag", "lscr.io/linuxserver/swag"), ("job-squire-castelo", "ghcr.io/dellipse/job-squire:latest")]


def test_detect_existing_proxy_finds_swag_by_name_and_config_mount():
    run = (
        FakeRun()
        .on(("docker", "ps"), stdout="swag\tlscr.io/linuxserver/swag\n")
        .on(("docker", "inspect", "--format", "{{json .Mounts}}", "swag"),
            stdout=json.dumps([{"Destination": "/config", "Source": "/home/dan/swag-config"}]))
    )
    target = proxy.detect_existing_proxy("docker", run=run)
    assert target == proxy.ProxyTarget(config_dir=Path("/home/dan/swag-config"), container_name="swag", kind="swag")


def test_detect_existing_proxy_falls_back_to_bare_nginx():
    run = (
        FakeRun()
        .on(("docker", "ps"), stdout="my-nginx\tnginx:latest\n")
        .on(("docker", "inspect", "--format", "{{json .Mounts}}", "my-nginx"),
            stdout=json.dumps([{"Destination": "/etc/nginx/conf.d", "Source": "/srv/nginx/conf.d"}]))
    )
    target = proxy.detect_existing_proxy("docker", run=run)
    assert target == proxy.ProxyTarget(config_dir=Path("/srv/nginx/conf.d"), container_name="my-nginx", kind="nginx")


def test_detect_existing_proxy_returns_none_when_nothing_matches():
    run = FakeRun().on(("docker", "ps"), stdout="job-squire-castelo\tghcr.io/dellipse/job-squire:latest\n")
    assert proxy.detect_existing_proxy("docker", run=run) is None


def test_detect_existing_proxy_ignores_swag_container_with_no_config_mount():
    run = (
        FakeRun()
        .on(("docker", "ps"), stdout="swag\tlscr.io/linuxserver/swag\n")
        .on(("docker", "inspect", "--format", "{{json .Mounts}}", "swag"), stdout="[]")
    )
    assert proxy.detect_existing_proxy("docker", run=run) is None


# ── shared network ────────────────────────────────────────────────────────


def test_ensure_network_ignores_already_exists():
    run = FakeRun().on(("docker", "network", "create", "job-squire-proxy"),
                       returncode=1, stderr="Error: network with name already exists")
    proxy.ensure_network("docker", "job-squire-proxy", run=run)  # does not raise


def test_ensure_network_raises_on_other_failure():
    run = FakeRun().on(("docker", "network", "create", "job-squire-proxy"), returncode=1, stderr="permission denied")
    with pytest.raises(proxy.ProxyError):
        proxy.ensure_network("docker", "job-squire-proxy", run=run)


def test_attach_to_network_ignores_already_attached():
    run = FakeRun().on(("docker", "network", "connect", "job-squire-proxy", "swag"),
                       returncode=1, stderr="already exists in network")
    proxy.attach_to_network("docker", "swag", "job-squire-proxy", run=run)  # does not raise


def test_resolve_shared_network_reuses_proxy_existing_custom_network():
    run = FakeRun().on(
        ("docker", "inspect", "--format", "{{json .NetworkSettings.Networks}}", "swag"),
        stdout=json.dumps({"my-existing-net": {}}),
    )
    target = proxy.ProxyTarget(config_dir=Path("/config"), container_name="swag", kind="swag")
    assert proxy.resolve_shared_network("docker", target, "job-squire-proxy", run=run) == "my-existing-net"


def test_resolve_shared_network_creates_and_attaches_when_only_builtin_network():
    run = (
        FakeRun()
        .on(("docker", "inspect", "--format", "{{json .NetworkSettings.Networks}}", "swag"),
            stdout=json.dumps({"bridge": {}}))
        .on(("docker", "network", "create", "job-squire-proxy"), returncode=0)
        .on(("docker", "network", "connect", "job-squire-proxy", "swag"), returncode=0)
    )
    target = proxy.ProxyTarget(config_dir=Path("/config"), container_name="swag", kind="swag")
    assert proxy.resolve_shared_network("docker", target, "job-squire-proxy", run=run) == "job-squire-proxy"
    assert ("docker", "network", "connect", "job-squire-proxy", "swag") in run.calls


# ── reload ────────────────────────────────────────────────────────────────


def test_reload_proxy_execs_nginx_reload_in_container():
    run = FakeRun().on(("docker", "exec", "swag", "nginx", "-s", "reload"), returncode=0)
    target = proxy.ProxyTarget(config_dir=Path("/config"), container_name="swag", kind="swag")
    proxy.reload_proxy(target, runtime="docker", run=run)  # does not raise


def test_reload_proxy_runs_bare_nginx_reload_when_no_container():
    run = FakeRun().on(("nginx", "-s", "reload"), returncode=0)
    target = proxy.ProxyTarget(config_dir=Path("/etc/nginx/conf.d"), container_name=None, kind="manual")
    proxy.reload_proxy(target, runtime="docker", run=run)  # does not raise


def test_reload_proxy_raises_on_failure():
    run = FakeRun().on(("docker", "exec", "swag", "nginx", "-s", "reload"), returncode=1, stderr="config error")
    target = proxy.ProxyTarget(config_dir=Path("/config"), container_name="swag", kind="swag")
    with pytest.raises(proxy.ProxyError):
        proxy.reload_proxy(target, runtime="docker", run=run)


# ── install_swag ──────────────────────────────────────────────────────────


def test_install_swag_writes_compose_and_brings_it_up(tmp_path):
    run = (
        FakeRun()
        .on(("docker", "network", "create", "job-squire-proxy"), returncode=0)
        .on(("docker", "compose"), returncode=0)
    )
    target = proxy.install_swag(runtime="docker", network="job-squire-proxy", data_root=tmp_path, run=run)
    assert target.container_name == proxy.SWAG_CONTAINER_NAME
    assert target.kind == "swag"
    assert target.config_dir == proxy.swag_root(tmp_path) / "config"
    compose_text = (proxy.swag_root(tmp_path) / "docker-compose.yml").read_text()
    assert "lscr.io/linuxserver/swag" in compose_text
    assert "job-squire-proxy" in compose_text


def test_install_swag_raises_when_compose_up_fails(tmp_path):
    run = (
        FakeRun()
        .on(("docker", "network", "create", "job-squire-proxy"), returncode=0)
        .on(("docker", "compose"), returncode=1, stderr="boom")
    )
    with pytest.raises(proxy.ProxyError):
        proxy.install_swag(runtime="docker", network="job-squire-proxy", data_root=tmp_path, run=run)


# ── provision_instance_proxy (end to end, fake runtime) ──────────────────


def test_provision_instance_proxy_rejects_local_mode(instance_root):
    instance = make_instance(mode="local")
    with pytest.raises(proxy.ProxyError):
        proxy.provision_instance_proxy(instance, root=instance_root, run=FakeRun())


def test_provision_instance_proxy_with_existing_swag(instance_root):
    instance = make_instance()
    run = (
        FakeRun()
        .on(("docker", "ps"), stdout="swag\tlscr.io/linuxserver/swag\n")
        .on(("docker", "inspect", "--format", "{{json .Mounts}}", "swag"),
            stdout=json.dumps([{"Destination": "/config", "Source": str(instance_root.parent / "swag-config")}]))
        .on(("docker", "inspect", "--format", "{{json .NetworkSettings.Networks}}", "swag"),
            stdout=json.dumps({"bridge": {}}))
        .on(("docker", "network", "create", "job-squire-proxy"), returncode=0)
        .on(("docker", "network", "connect", "job-squire-proxy", "swag"), returncode=0)
        .on(("docker", "network", "connect", "job-squire-proxy", "job-squire-castelo"), returncode=0)
        .on(("docker", "compose"), returncode=0)
        .on(("docker", "exec", "swag", "nginx", "-s", "reload"), returncode=0)
    )
    result = proxy.provision_instance_proxy(instance, root=instance_root, run=run)

    assert result.proxy.kind == "swag"
    assert result.network == "job-squire-proxy"
    assert result.installed_swag is False
    assert result.web_conf_path.exists()
    assert result.mcp_conf_path.exists()
    assert "set $upstream_app job-squire-castelo;" in result.web_conf_path.read_text()
    # The compose file was rewritten in place with the new networks: block.
    assert "job-squire-proxy" in paths.compose_path(instance_root).read_text()


def test_provision_instance_proxy_installs_swag_when_none_detected_and_confirmed(instance_root, tmp_path):
    instance = make_instance()
    run = (
        FakeRun()
        .on(("docker", "ps"), stdout="")
        .on(("docker", "network", "create", "job-squire-proxy"), returncode=0)
        .on(("docker", "compose"), returncode=0)
        # install_swag's own compose file already attaches the fresh SWAG
        # container to job-squire-proxy, so resolve_shared_network finds
        # it there and skips re-creating/re-attaching the proxy itself.
        .on(("docker", "inspect", "--format", "{{json .NetworkSettings.Networks}}", proxy.SWAG_CONTAINER_NAME),
            stdout=json.dumps({"job-squire-proxy": {}}))
        .on(("docker", "network", "connect", "job-squire-proxy", "job-squire-castelo"), returncode=0)
        .on(("docker", "exec", proxy.SWAG_CONTAINER_NAME, "test", "-f", "/config/nginx/proxy.conf"), returncode=0)
        .on(("docker", "exec", proxy.SWAG_CONTAINER_NAME, "nginx", "-s", "reload"), returncode=0)
    )
    result = proxy.provision_instance_proxy(
        instance, root=instance_root, data_root=tmp_path, confirm=lambda _msg: True, run=run,
        sleep=lambda _seconds: None,
    )
    assert result.installed_swag is True
    assert result.proxy.container_name == proxy.SWAG_CONTAINER_NAME


def test_provision_instance_proxy_no_install_raises_when_none_detected(instance_root):
    instance = make_instance()
    run = FakeRun().on(("docker", "ps"), stdout="")
    with pytest.raises(proxy.ProxyError):
        proxy.provision_instance_proxy(instance, root=instance_root, install_if_missing=False, run=run)


def test_provision_instance_proxy_declined_install_raises(instance_root):
    instance = make_instance()
    run = FakeRun().on(("docker", "ps"), stdout="")
    with pytest.raises(proxy.ProxyError):
        proxy.provision_instance_proxy(instance, root=instance_root, confirm=lambda _msg: False, run=run)


def test_provision_instance_proxy_manual_config_dir_skips_networking(instance_root, tmp_path):
    instance = make_instance()
    manual_dir = tmp_path / "manual-nginx"
    run = FakeRun().on(("nginx", "-s", "reload"), returncode=0)
    result = proxy.provision_instance_proxy(
        instance, root=instance_root, config_dir=manual_dir, run=run,
    )
    assert result.proxy.kind == "manual"
    assert result.proxy.container_name is None
    assert "proxy_pass http://127.0.0.1:8081;" in result.web_conf_path.read_text()
    # No network/compose calls should have been made for a non-containerized proxy.
    assert not any(call[:2] == ("docker", "network") for call in run.calls)
    # The instance's compose file is untouched (no proxy_network line added).
    assert "job-squire-proxy" not in paths.compose_path(instance_root).read_text()


# ── _await_swag_ready (regression: a fresh SWAG isn't reload-ready the
# instant its container starts -- see the function's own docstring) ──────


def test_await_swag_ready_retries_until_the_marker_file_appears():
    """SWAG's own init takes a beat to populate /config/nginx/proxy.conf;
    the first two polls must find it missing and the third must succeed,
    with a sleep between each failed attempt."""
    attempts = {"n": 0}

    def run(args, **kwargs):
        attempts["n"] += 1
        return SimpleNamespace(returncode=0 if attempts["n"] >= 3 else 1, stdout="", stderr="")

    slept = []
    ready = proxy._await_swag_ready(
        "docker", "job-squire-swag", run=run, sleep=slept.append,
        timeout_seconds=10, poll_interval=1,
    )
    assert ready is True
    assert attempts["n"] == 3
    assert slept == [1, 1]


def test_await_swag_ready_gives_up_after_the_timeout():
    def run(args, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    ready = proxy._await_swag_ready(
        "docker", "job-squire-swag", run=run, sleep=lambda _seconds: None,
        timeout_seconds=3, poll_interval=1,
    )
    assert ready is False


def test_provision_instance_proxy_waits_for_swag_before_reloading(instance_root, tmp_path):
    """End-to-end proof the wait is actually wired into provisioning, not
    just unit-tested in isolation: the fake exec for `test -f
    .../proxy.conf` must be called (and satisfied) before `nginx -s
    reload` is attempted."""
    instance = make_instance()
    run = (
        FakeRun()
        .on(("docker", "ps"), stdout="")
        .on(("docker", "network", "create", "job-squire-proxy"), returncode=0)
        .on(("docker", "compose"), returncode=0)
        .on(("docker", "inspect", "--format", "{{json .NetworkSettings.Networks}}", proxy.SWAG_CONTAINER_NAME),
            stdout=json.dumps({"job-squire-proxy": {}}))
        .on(("docker", "network", "connect", "job-squire-proxy", "job-squire-castelo"), returncode=0)
        .on(("docker", "exec", proxy.SWAG_CONTAINER_NAME, "test", "-f", "/config/nginx/proxy.conf"), returncode=0)
        .on(("docker", "exec", proxy.SWAG_CONTAINER_NAME, "nginx", "-s", "reload"), returncode=0)
    )
    proxy.provision_instance_proxy(
        instance, root=instance_root, data_root=tmp_path, confirm=lambda _msg: True, run=run,
        sleep=lambda _seconds: None,
    )
    ready_check = ("docker", "exec", proxy.SWAG_CONTAINER_NAME, "test", "-f", "/config/nginx/proxy.conf")
    reload_call = ("docker", "exec", proxy.SWAG_CONTAINER_NAME, "nginx", "-s", "reload")
    assert run.calls.index(ready_check) < run.calls.index(reload_call)
