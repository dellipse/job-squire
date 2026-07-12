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
"""Tailscale Serve for private remote access (Prompt C11).

Every subprocess call is injected, same pattern as test_proxy.py/
test_dns.py, so this never touches a real `tailscale` binary or a real
container runtime.
"""
import json
from types import SimpleNamespace

import pytest

from job_squire_cli.ops import compose, dotenv, paths, tailscale
from job_squire_cli.ops.registry import Instance, add_instance, get_instance
from job_squire_cli.query import config as query_config_module


class FakeRun:
    """Matches subprocess calls by argv prefix (longest match wins), same
    philosophy as test_proxy.py's FakeRun -- a stray unmocked call fails
    the test loudly rather than silently doing nothing."""

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


STATUS_JSON = json.dumps({"Self": {"DNSName": "castelo.tail1234.ts.net."}})


def make_instance(**overrides) -> Instance:
    defaults = dict(
        name="castelo", mode="local", runtime="docker", data_dir="/instances/castelo",
        app_port=8080, mcp_port=9000, cookie_name="castelo_session",
        public_url="http://localhost:8080", created="2026-07-11",
    )
    defaults.update(overrides)
    return Instance(**defaults)


@pytest.fixture
def instance_root(tmp_path):
    root = tmp_path / "castelo"
    compose.write_instance_files(
        root, container_name="job-squire-castelo", image="ghcr.io/dellipse/job-squire:latest",
        loopback_only=True, app_port=8080, mcp_port=9000,
        env=compose.InstanceEnv(
            secret_key="deadbeef", admin_username="admin", admin_password="pw",
            instance_name="castelo", cookie_name="castelo_session", deploy_mode="local",
            public_url="http://localhost:8080", public_mcp_url="http://localhost:9000",
            public_mcp_host="localhost", mcp_port=9000,
        ),
    )
    return root


@pytest.fixture(autouse=True)
def force_linux_config_dir(monkeypatch):
    # query/config.py's config_dir() only honors XDG_CONFIG_HOME on the
    # Linux branch -- without this, it resolves to the real macOS/Windows
    # per-user path regardless of the env var below, which would make
    # add_instance() below write into the real, live registry instead of
    # a test tmpdir. Same fixture name/pattern as test_configure.py and
    # test_ops_commands.py.
    monkeypatch.setattr(query_config_module.platform, "system", lambda: "Linux")


@pytest.fixture(autouse=True)
def tmp_registry_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))


# ── device_dns_name ──────────────────────────────────────────────────────


def test_device_dns_name_strips_trailing_dot():
    run = FakeRun().on(("tailscale", "status", "--json"), stdout=STATUS_JSON)
    assert tailscale.device_dns_name(run=run) == "castelo.tail1234.ts.net"


def test_device_dns_name_raises_when_tailscale_not_logged_in():
    run = FakeRun().on(("tailscale", "status", "--json"), returncode=1, stderr="not logged in")
    with pytest.raises(tailscale.TailscaleError, match="tailscale up"):
        tailscale.device_dns_name(run=run)


def test_device_dns_name_raises_when_dns_name_missing():
    run = FakeRun().on(("tailscale", "status", "--json"), stdout=json.dumps({"Self": {"DNSName": ""}}))
    with pytest.raises(tailscale.TailscaleError, match="MagicDNS"):
        tailscale.device_dns_name(run=run)


# ── enable_serve_port / disable_serve_port ───────────────────────────────


def test_enable_serve_port_rejects_a_port_outside_the_allowed_set():
    with pytest.raises(tailscale.TailscaleError, match="supported HTTPS ports"):
        tailscale.enable_serve_port(9999, 8080, run=FakeRun())


def test_enable_serve_port_never_funnels():
    """Only ever invokes `tailscale serve`, never `tailscale funnel`."""
    run = FakeRun().on(("tailscale", "serve"), returncode=0)
    tailscale.enable_serve_port(443, 8080, run=run)
    assert run.calls == [("tailscale", "serve", "--bg", "--https=443", "http://127.0.0.1:8080")]
    assert all(call[1] != "funnel" for call in run.calls)


def test_disable_serve_port_is_idempotent_when_nothing_was_served():
    run = FakeRun().on(("tailscale", "serve"), returncode=1, stderr="not serving on that port")
    tailscale.disable_serve_port(443, run=run)  # does not raise


def test_disable_serve_port_raises_on_a_real_failure():
    run = FakeRun().on(("tailscale", "serve"), returncode=1, stderr="permission denied")
    with pytest.raises(tailscale.TailscaleError, match="permission denied"):
        tailscale.disable_serve_port(443, run=run)


# ── enable_tailscale_serve ────────────────────────────────────────────────


def test_enable_rejects_network_mode_instance(tmp_path):
    instance = make_instance(mode="network", app_port=None, mcp_port=None)
    with pytest.raises(tailscale.TailscaleError, match="only applies to local instances"):
        tailscale.enable_tailscale_serve(instance, root=tmp_path, run=FakeRun())


def test_enable_rejects_identical_web_and_mcp_ports(tmp_path):
    instance = make_instance()
    with pytest.raises(tailscale.TailscaleError, match="must be different"):
        tailscale.enable_tailscale_serve(instance, root=tmp_path, web_port=443, mcp_port=443, run=FakeRun())


def test_enable_happy_path_rewrites_env_and_registry_and_state(instance_root, tmp_path):
    add_instance(
        name="castelo", mode="local", runtime="docker", data_dir=str(instance_root),
        public_url="http://localhost:8080", app_port=8080, mcp_port=9000,
    )
    instance = get_instance("castelo")

    run = (
        FakeRun()
        .on(("tailscale", "status", "--json"), stdout=STATUS_JSON)
        .on(("tailscale", "serve"), returncode=0)
        .on(("docker", "compose"), returncode=0)
        .on(("docker", "inspect"), returncode=0, stdout=json.dumps({"Status": "running", "Health": {"Status": "healthy"}}))
    )

    result = tailscale.enable_tailscale_serve(instance, root=instance_root, run=run)

    assert result.hostname == "castelo.tail1234.ts.net"
    assert result.public_url == "https://castelo.tail1234.ts.net"
    assert result.public_mcp_url == "https://castelo.tail1234.ts.net:8443"
    assert "WARNING banner" in result.expected_warning

    # Both Serve ports enabled, never funnel.
    assert ("tailscale", "serve", "--bg", "--https=443", "http://127.0.0.1:8080") in run.calls
    assert ("tailscale", "serve", "--bg", "--https=8443", "http://127.0.0.1:9000") in run.calls

    env = dotenv.parse(paths.data_env_path(instance_root))
    assert env["TRUST_PROXY"] == "true"
    assert env["SESSION_COOKIE_SECURE"] == "true"
    assert env["PUBLIC_URL"] == "https://castelo.tail1234.ts.net"
    assert env["PUBLIC_MCP_URL"] == "https://castelo.tail1234.ts.net:8443"
    assert env["PUBLIC_MCP_HOST"] == "castelo.tail1234.ts.net:8443"
    # DEPLOY_MODE and everything else already in data/.env untouched.
    assert env["DEPLOY_MODE"] == "local"
    assert env["SECRET_KEY"] == "deadbeef"

    # The container was recreated so the app picks up the new env.
    assert any("--force-recreate" in call for call in run.calls)

    updated = get_instance("castelo")
    assert updated.public_url == "https://castelo.tail1234.ts.net"
    assert updated.mode == "local"  # PLAN: stays local, not promoted to network

    state = tailscale.read_state(instance_root)
    assert state.enabled is True
    assert state.hostname == "castelo.tail1234.ts.net"
    assert state.web_port == 443
    assert state.mcp_port == 8443
    assert state.enabled_at is not None


def test_enable_rolls_back_web_port_if_mcp_port_fails(instance_root, tmp_path):
    add_instance(
        name="castelo", mode="local", runtime="docker", data_dir=str(instance_root),
        public_url="http://localhost:8080", app_port=8080, mcp_port=9000,
    )
    instance = get_instance("castelo")

    calls = []

    def run(args, **kwargs):
        args = tuple(args)
        calls.append(args)
        if args[:2] == ("tailscale", "status"):
            return SimpleNamespace(returncode=0, stdout=STATUS_JSON, stderr="")
        if args[:3] == ("tailscale", "serve", "--bg") and "--https=8443" in args:
            return SimpleNamespace(returncode=1, stdout="", stderr="boom")
        if args[0] == "tailscale":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected call: {args}")

    with pytest.raises(tailscale.TailscaleError, match="boom"):
        tailscale.enable_tailscale_serve(instance, root=instance_root, run=run)

    # The web port that succeeded first was turned back off during rollback.
    assert ("tailscale", "serve", "--https=443", "off") in calls
    # data/.env was never touched since the failure happened before any writes.
    env = dotenv.parse(paths.data_env_path(instance_root))
    assert env["PUBLIC_URL"] == "http://localhost:8080"
    assert tailscale.read_state(instance_root).enabled is False


def test_enable_surfaces_recreate_failure(instance_root):
    add_instance(
        name="castelo", mode="local", runtime="docker", data_dir=str(instance_root),
        public_url="http://localhost:8080", app_port=8080, mcp_port=9000,
    )
    instance = get_instance("castelo")
    run = (
        FakeRun()
        .on(("tailscale", "status", "--json"), stdout=STATUS_JSON)
        .on(("tailscale", "serve"), returncode=0)
        .on(("docker", "compose"), returncode=1, stderr="recreate failed")
    )
    with pytest.raises(tailscale.TailscaleError, match="recreate failed"):
        tailscale.enable_tailscale_serve(instance, root=instance_root, run=run)


# ── disable_tailscale_serve ───────────────────────────────────────────────


def test_disable_raises_when_not_enabled(instance_root):
    instance = make_instance(data_dir=str(instance_root))
    with pytest.raises(tailscale.TailscaleError, match="does not have Tailscale Serve enabled"):
        tailscale.disable_tailscale_serve(instance, root=instance_root, run=FakeRun())


def test_disable_happy_path_reverts_env_and_registry(instance_root):
    add_instance(
        name="castelo", mode="local", runtime="docker", data_dir=str(instance_root),
        public_url="http://localhost:8080", app_port=8080, mcp_port=9000,
    )
    instance = get_instance("castelo")

    run = (
        FakeRun()
        .on(("tailscale", "status", "--json"), stdout=STATUS_JSON)
        .on(("tailscale", "serve"), returncode=0)
        .on(("docker", "compose"), returncode=0)
        .on(("docker", "inspect"), returncode=0, stdout=json.dumps({"Status": "running", "Health": {"Status": "healthy"}}))
    )
    tailscale.enable_tailscale_serve(instance, root=instance_root, run=run)
    instance = get_instance("castelo")

    result = tailscale.disable_tailscale_serve(instance, root=instance_root, run=run)

    assert result.public_url == "http://localhost:8080"
    assert ("tailscale", "serve", "--https=443", "off") in run.calls
    assert ("tailscale", "serve", "--https=8443", "off") in run.calls

    env = dotenv.parse(paths.data_env_path(instance_root))
    assert env["TRUST_PROXY"] == "false"
    assert env["SESSION_COOKIE_SECURE"] == "false"
    assert env["PUBLIC_URL"] == "http://localhost:8080"
    assert env["PUBLIC_MCP_URL"] == "http://localhost:9000"
    assert env["PUBLIC_MCP_HOST"] == "localhost"

    updated = get_instance("castelo")
    assert updated.public_url == "http://localhost:8080"

    state = tailscale.read_state(instance_root)
    assert state.enabled is False


# ── is_tailnet_reachable ──────────────────────────────────────────────────


def test_is_tailnet_reachable_false_with_no_manifest(tmp_path):
    assert tailscale.is_tailnet_reachable(tmp_path) is False


def test_is_tailnet_reachable_true_once_enabled(instance_root):
    add_instance(
        name="castelo", mode="local", runtime="docker", data_dir=str(instance_root),
        public_url="http://localhost:8080", app_port=8080, mcp_port=9000,
    )
    instance = get_instance("castelo")
    run = (
        FakeRun()
        .on(("tailscale", "status", "--json"), stdout=STATUS_JSON)
        .on(("tailscale", "serve"), returncode=0)
        .on(("docker", "compose"), returncode=0)
        .on(("docker", "inspect"), returncode=0, stdout=json.dumps({"Status": "running"}))
    )
    tailscale.enable_tailscale_serve(instance, root=instance_root, run=run)
    assert tailscale.is_tailnet_reachable(instance_root) is True
