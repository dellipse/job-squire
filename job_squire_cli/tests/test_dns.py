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
"""DNS and TLS provisioning.

Every subprocess call is injected, same philosophy as test_proxy.py and
test_runtime.py, so this never touches a real container runtime or real
DNS/certbot infrastructure. `SequencedFakeRun` extends test_proxy.py's
prefix-matching `FakeRun` idea with a queue per prefix, since certificate
polling calls the *same* `logs` command repeatedly and needs to see
different canned output on successive calls (e.g. "still negotiating"
then "Congratulations...").
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from job_squire_cli.ops import dns, proxy


class SequencedFakeRun:
    """Matches subprocess calls by argv prefix (longest match wins). Each
    prefix holds a queue of canned responses; each call pops the next one
    and the last response registered for a prefix repeats once exhausted.
    A call matching nothing fails the test loudly.
    """

    def __init__(self):
        self.responses: list[tuple[tuple[str, ...], list[SimpleNamespace]]] = []
        self.calls: list[tuple[str, ...]] = []

    def on(self, prefix, *, returncode=0, stdout="", stderr=""):
        response = SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
        prefix = tuple(prefix)
        for existing_prefix, queue in self.responses:
            if existing_prefix == prefix:
                queue.append(response)
                return self
        self.responses.append((prefix, [response]))
        return self

    def __call__(self, args, **kwargs):
        args = tuple(args)
        self.calls.append(args)
        best = None
        for prefix, queue in self.responses:
            if args[: len(prefix)] == prefix and (best is None or len(prefix) > len(best[0])):
                best = (prefix, queue)
        if best is None:
            raise AssertionError(f"unexpected subprocess call in test: {args}")
        _, queue = best
        return queue.pop(0) if len(queue) > 1 else queue[0]


def fake_sleep_recorder():
    calls = []

    def sleep(seconds):
        calls.append(seconds)

    return sleep, calls


def make_managed_swag(tmp_path: Path) -> Path:
    """A CLI-installed SWAG already present at swag_root, matching what a
    prior `job-squire proxy` run would have left behind -- the state
    `_managed_swag_target` requires before either dns command will touch
    anything."""
    root = proxy.swag_root(tmp_path)
    (root / "config").mkdir(parents=True)
    (root / "docker-compose.yml").write_text(
        proxy.render_swag_compose(network="job-squire-proxy", timezone="Etc/UTC", url="", validation="http")
    )
    return root


# ── _bare_label ───────────────────────────────────────────────────────────


def test_bare_label_strips_suffix_case_insensitively():
    assert dns._bare_label("castelo.DuckDNS.org", "duckdns.org") == "castelo"


def test_bare_label_leaves_a_bare_label_alone():
    assert dns._bare_label("castelo", "duckdns.org") == "castelo"


def test_bare_label_exact_suffix_match_yields_empty():
    assert dns._bare_label("duckdns.org", "duckdns.org") == ""


# ── _managed_swag_target ─────────────────────────────────────────────────


def test_managed_swag_target_raises_when_swag_was_never_installed(tmp_path):
    with pytest.raises(dns.DnsError, match="Run `job-squire proxy NAME` first"):
        dns._managed_swag_target(tmp_path)


def test_managed_swag_target_found_when_installed(tmp_path):
    make_managed_swag(tmp_path)
    target = dns._managed_swag_target(tmp_path)
    assert target.container_name == proxy.SWAG_CONTAINER_NAME
    assert target.kind == "swag"


# ── configure_duckdns ────────────────────────────────────────────────────


def test_configure_duckdns_requires_swag_already_installed(tmp_path):
    run = SequencedFakeRun()
    sleep, _ = fake_sleep_recorder()
    with pytest.raises(dns.DnsError, match="Run `job-squire proxy NAME` first"):
        dns.configure_duckdns(
            subdomain="castelo", token="tok123", runtime="docker",
            data_root=tmp_path, run=run, sleep=sleep,
        )


def test_configure_duckdns_missing_inputs_raises_before_touching_swag(tmp_path):
    with pytest.raises(dns.DnsError, match="subdomain and an account token"):
        dns.configure_duckdns(subdomain="", token="", runtime="docker", data_root=tmp_path)


def test_configure_duckdns_wildcard_writes_dns01_mode_and_polls_success(tmp_path):
    make_managed_swag(tmp_path)
    run = (
        SequencedFakeRun()
        .on(("docker", "compose"), returncode=0)
        .on(("docker", "logs", "--tail", "200", proxy.SWAG_CONTAINER_NAME),
            stdout="Requesting a certificate...\n")
        .on(("docker", "logs", "--tail", "200", proxy.SWAG_CONTAINER_NAME),
            stdout="Congratulations! Your certificate and chain have been saved.\n")
    )
    sleep, sleep_calls = fake_sleep_recorder()

    result = dns.configure_duckdns(
        subdomain="castelo.duckdns.org", token="tok123", wildcard=True, runtime="docker",
        data_root=tmp_path, timeout_seconds=30, poll_interval=10, run=run, sleep=sleep,
    )

    assert result.mode == "duckdns-wildcard"
    assert result.url == "castelo.duckdns.org"
    assert result.subdomains == "wildcard"
    assert result.cert.issued is True
    assert sleep_calls == [10]  # slept once between the two log polls

    compose_text = (proxy.swag_root(tmp_path) / "docker-compose.yml").read_text()
    assert 'VALIDATION: "duckdns"' in compose_text
    assert 'SUBDOMAINS: "wildcard"' in compose_text
    assert 'DUCKDNSTOKEN: "tok123"' in compose_text
    assert 'URL: "castelo.duckdns.org"' in compose_text
    assert ("docker", "compose", "--project-directory", str(proxy.swag_root(tmp_path)),
            "-f", str(proxy.swag_root(tmp_path) / "docker-compose.yml"),
            "-p", "job-squire-proxy", "up", "-d", "--force-recreate") in run.calls


def test_configure_duckdns_main_only_uses_http_validation_and_blank_subdomains(tmp_path):
    make_managed_swag(tmp_path)
    run = SequencedFakeRun().on(("docker", "compose"), returncode=0)

    result = dns.configure_duckdns(
        subdomain="castelo", token="tok123", wildcard=False, runtime="docker",
        data_root=tmp_path, wait_for_cert=False, run=run,
    )

    assert result.mode == "duckdns-http"
    assert result.subdomains == ""
    compose_text = (proxy.swag_root(tmp_path) / "docker-compose.yml").read_text()
    assert 'VALIDATION: "http"' in compose_text
    assert 'SUBDOMAINS: ""' in compose_text


def test_configure_duckdns_no_wait_skips_polling_entirely(tmp_path):
    make_managed_swag(tmp_path)
    run = SequencedFakeRun().on(("docker", "compose"), returncode=0)
    result = dns.configure_duckdns(
        subdomain="castelo", token="tok123", runtime="docker",
        data_root=tmp_path, wait_for_cert=False, run=run,
    )
    assert result.cert.issued is False
    assert not any(call[1:2] == ("logs",) for call in run.calls)


def test_configure_duckdns_failure_marker_raises_immediately(tmp_path):
    make_managed_swag(tmp_path)
    run = (
        SequencedFakeRun()
        .on(("docker", "compose"), returncode=0)
        .on(("docker", "logs", "--tail", "200", proxy.SWAG_CONTAINER_NAME),
            stdout="Challenge failed for domain castelo.duckdns.org\n")
    )
    sleep, sleep_calls = fake_sleep_recorder()
    with pytest.raises(dns.DnsError, match="certificate validation failure"):
        dns.configure_duckdns(
            subdomain="castelo", token="tok123", runtime="docker",
            data_root=tmp_path, timeout_seconds=60, poll_interval=10, run=run, sleep=sleep,
        )
    assert sleep_calls == []  # failed on the very first poll, no sleep needed


def test_configure_duckdns_timeout_without_any_marker_reports_not_issued(tmp_path):
    make_managed_swag(tmp_path)
    run = (
        SequencedFakeRun()
        .on(("docker", "compose"), returncode=0)
        .on(("docker", "logs", "--tail", "200", proxy.SWAG_CONTAINER_NAME), stdout="still negotiating...\n")
    )
    sleep, sleep_calls = fake_sleep_recorder()
    result = dns.configure_duckdns(
        subdomain="castelo", token="tok123", runtime="docker",
        data_root=tmp_path, timeout_seconds=25, poll_interval=10, run=run, sleep=sleep,
    )
    assert result.cert.issued is False
    assert "still negotiating" in result.cert.log_tail
    assert sleep_calls == [10, 10]  # 3 attempts (25 // 10 + 1), sleeping between each


def test_configure_duckdns_recreate_failure_raises(tmp_path):
    make_managed_swag(tmp_path)
    run = SequencedFakeRun().on(("docker", "compose"), returncode=1, stderr="boom")
    with pytest.raises(dns.DnsError, match="boom"):
        dns.configure_duckdns(subdomain="castelo", token="tok123", runtime="docker", data_root=tmp_path, run=run)


# ── configure_cloudflare ─────────────────────────────────────────────────


def test_configure_cloudflare_missing_inputs_raises(tmp_path):
    with pytest.raises(dns.DnsError, match="domain and a Cloudflare API token"):
        dns.configure_cloudflare(domain="", api_token="", runtime="docker", data_root=tmp_path)


def test_configure_cloudflare_requires_swag_already_installed(tmp_path):
    with pytest.raises(dns.DnsError, match="Run `job-squire proxy NAME` first"):
        dns.configure_cloudflare(
            domain="example.com", api_token="cf-tok", runtime="docker", data_root=tmp_path,
        )


def test_configure_cloudflare_writes_credentials_and_dns01_mode(tmp_path):
    make_managed_swag(tmp_path)
    run = (
        SequencedFakeRun()
        .on(("docker", "compose"), returncode=0)
        .on(("docker", "logs", "--tail", "200", proxy.SWAG_CONTAINER_NAME),
            stdout="Congratulations! Your certificate and chain have been saved.\n")
    )
    sleep, _ = fake_sleep_recorder()

    result = dns.configure_cloudflare(
        domain="example.com", api_token="cf-tok-secret", runtime="docker",
        data_root=tmp_path, run=run, sleep=sleep,
    )

    assert result.mode == "cloudflare-dns01"
    assert result.url == "example.com"
    assert result.subdomains == "wildcard"
    assert result.cert.issued is True

    ini_path = proxy.swag_root(tmp_path) / "config" / "dns-conf" / "cloudflare.ini"
    assert "dns_cloudflare_api_token = cf-tok-secret" in ini_path.read_text()

    compose_text = (proxy.swag_root(tmp_path) / "docker-compose.yml").read_text()
    assert 'VALIDATION: "dns"' in compose_text
    assert 'DNSPLUGIN: "cloudflare"' in compose_text
    assert 'SUBDOMAINS: "wildcard"' in compose_text
    assert 'URL: "example.com"' in compose_text


def test_configure_cloudflare_credentials_file_is_permissioned_600(tmp_path):
    make_managed_swag(tmp_path)
    run = SequencedFakeRun().on(("docker", "compose"), returncode=0)
    dns.configure_cloudflare(
        domain="example.com", api_token="cf-tok", runtime="docker",
        data_root=tmp_path, wait_for_cert=False, run=run,
    )
    ini_path = proxy.swag_root(tmp_path) / "config" / "dns-conf" / "cloudflare.ini"
    assert (ini_path.stat().st_mode & 0o777) == 0o600
