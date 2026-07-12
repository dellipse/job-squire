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
"""ops/self_update.py -- updating the running CLI itself, ahead of
`job-squire update` moving any instance. No test here makes a real
network call or a real `pip install`: `http_get` and `run` are both
injected, the same pattern ops/lifecycle.py's FakeRuntime establishes for
the container runtime.
"""
import json
from types import SimpleNamespace

import pytest

from job_squire_cli.ops import self_update as su


def _http_get(latest_tag="v0.8.0", *, latest_status=200, tags_status=200):
    """A fake GitHub API: /releases/latest and /releases/tags/<tag> both
    resolve to `latest_tag`; anything else 404s."""
    def fake(url):
        if url.endswith("/releases/latest"):
            if latest_status != 200:
                return latest_status, b"{}"
            return 200, json.dumps({"tag_name": latest_tag}).encode()
        if "/releases/tags/" in url:
            if tags_status != 200:
                return tags_status, b"{}"
            requested_tag = url.rsplit("/", 1)[-1]
            return 200, json.dumps({"tag_name": requested_tag}).encode()
        if url.endswith("/releases"):
            return 200, json.dumps([{"tag_name": latest_tag}]).encode()
        raise AssertionError(f"unexpected URL: {url}")
    return fake


def _git_ls_remote_run(sha="a" * 40):
    def fake(argv, **kwargs):
        if argv[:2] == ["git", "ls-remote"]:
            tag = argv[3].removeprefix("refs/tags/")
            return SimpleNamespace(returncode=0, stdout=f"{sha}\trefs/tags/{tag}\n", stderr="")
        raise AssertionError(f"unexpected command: {argv}")
    return fake


def _combined_run(sha="a" * 40, *, pip_returncode=0, pip_stderr=""):
    def fake(argv, **kwargs):
        if argv[:2] == ["git", "ls-remote"]:
            tag = argv[3].removeprefix("refs/tags/")
            return SimpleNamespace(returncode=0, stdout=f"{sha}\trefs/tags/{tag}\n", stderr="")
        if "-m" in argv and "pip" in argv:
            return SimpleNamespace(returncode=pip_returncode, stdout="", stderr=pip_stderr)
        raise AssertionError(f"unexpected command: {argv}")
    return fake


@pytest.fixture(autouse=True)
def stub_installed_version(monkeypatch):
    """Pin what `importlib.metadata.version("job-squire-cli")` reports so
    tests control the "already installed" side without a real install."""
    versions = {"before": "0.7.0+deadbee"}

    def fake_version(name):
        if name != su.PACKAGE_NAME:
            raise su.importlib.metadata.PackageNotFoundError(name)
        return versions["current"]

    versions["current"] = versions["before"]
    monkeypatch.setattr(su.importlib.metadata, "version", fake_version)
    return versions


def test_resolves_latest_and_installs_when_out_of_date(stub_installed_version, monkeypatch):
    sha = "b" * 40

    def fake_run(argv, **kwargs):
        if argv[:2] == ["git", "ls-remote"]:
            return SimpleNamespace(returncode=0, stdout=f"{sha}\trefs/tags/v0.8.0\n", stderr="")
        if "pip" in argv:
            stub_installed_version["current"] = f"0.8.0+{sha[:7]}"
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(su.importlib.util, "find_spec", lambda name: None)  # ops-only install
    result = su.self_update(http_get=_http_get("v0.8.0"), run=fake_run)
    assert result.updated is True
    assert result.previous_version == "0.7.0+deadbee"
    assert result.new_version == f"0.8.0+{sha[:7]}"
    assert result.tag == "v0.8.0"


def test_already_at_resolved_commit_skips_pip_install(stub_installed_version, monkeypatch):
    # The currently-installed version's suffix (deadbee) is a prefix of
    # the full sha git ls-remote resolves the tag to -- self_update should
    # recognize that as already up to date and never shell out to pip.
    sha = "deadbeef" + "0" * 32
    pip_calls = []

    def fake_run(argv, **kwargs):
        if argv[:2] == ["git", "ls-remote"]:
            return SimpleNamespace(returncode=0, stdout=f"{sha}\trefs/tags/v0.7.0\n", stderr="")
        pip_calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    result = su.self_update(http_get=_http_get("v0.7.0"), run=fake_run)
    assert result.updated is False
    assert result.previous_version == result.new_version == "0.7.0+deadbee"
    assert pip_calls == []


def test_preserves_query_extra_when_already_installed(stub_installed_version, monkeypatch):
    sha = "c" * 40
    captured_spec = {}

    def fake_run(argv, **kwargs):
        if argv[:2] == ["git", "ls-remote"]:
            return SimpleNamespace(returncode=0, stdout=f"{sha}\trefs/tags/v0.8.0\n", stderr="")
        captured_spec["spec"] = argv[-1]
        stub_installed_version["current"] = f"0.8.0+{sha[:7]}"
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(su.importlib.util, "find_spec", lambda name: object())  # rich + mcp both present
    su.self_update(http_get=_http_get("v0.8.0"), run=fake_run)
    assert captured_spec["spec"].startswith("job-squire-cli[query] @ git+")


def test_omits_query_extra_when_not_installed(stub_installed_version, monkeypatch):
    sha = "d" * 40
    captured_spec = {}

    def fake_run(argv, **kwargs):
        if argv[:2] == ["git", "ls-remote"]:
            return SimpleNamespace(returncode=0, stdout=f"{sha}\trefs/tags/v0.8.0\n", stderr="")
        captured_spec["spec"] = argv[-1]
        stub_installed_version["current"] = f"0.8.0+{sha[:7]}"
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(su.importlib.util, "find_spec", lambda name: None)
    su.self_update(http_get=_http_get("v0.8.0"), run=fake_run)
    assert captured_spec["spec"].startswith("job-squire-cli @ git+")
    assert "[query]" not in captured_spec["spec"]


def test_pinned_version_resolves_v_prefixed_tag(stub_installed_version, monkeypatch):
    monkeypatch.setattr(su.importlib.util, "find_spec", lambda name: None)
    result = su.self_update("0.6.0", http_get=_http_get(), run=_combined_run())
    assert result.tag == "v0.6.0"


def test_unknown_pinned_version_raises_clean_error(stub_installed_version):
    def http_get(url):
        if url.endswith("/releases/tags/v9.9.9"):
            return 404, b"{}"
        raise AssertionError(f"unexpected URL: {url}")

    with pytest.raises(su.SelfUpdateError, match="No published release matches version"):
        su.self_update("9.9.9", http_get=http_get, run=_git_ls_remote_run())


def test_latest_404_falls_back_to_release_list(stub_installed_version, monkeypatch):
    monkeypatch.setattr(su.importlib.util, "find_spec", lambda name: None)
    http_get = _http_get(latest_tag="v0.5.0", latest_status=404)
    result = su.self_update(http_get=http_get, run=_combined_run(sha="e" * 40))
    assert result.tag == "v0.5.0"


def test_no_releases_at_all_raises_clean_error(stub_installed_version):
    def http_get(url):
        if url.endswith("/releases/latest"):
            return 404, b"{}"
        if url.endswith("/releases"):
            return 200, b"[]"
        raise AssertionError(f"unexpected URL: {url}")

    with pytest.raises(su.SelfUpdateError, match="No releases have been published"):
        su.self_update(http_get=http_get, run=_git_ls_remote_run())


def test_git_ls_remote_failure_raises_clean_error(stub_installed_version):
    def fake_run(argv, **kwargs):
        return SimpleNamespace(returncode=0, stdout="", stderr="")  # no matching ref lines

    with pytest.raises(su.SelfUpdateError, match="Could not resolve tag"):
        su.self_update(http_get=_http_get("v0.8.0"), run=fake_run)


def test_pip_install_failure_raises_clean_error(stub_installed_version, monkeypatch):
    monkeypatch.setattr(su.importlib.util, "find_spec", lambda name: None)
    sha = "f" * 40

    def fake_run(argv, **kwargs):
        if argv[:2] == ["git", "ls-remote"]:
            return SimpleNamespace(returncode=0, stdout=f"{sha}\trefs/tags/v0.8.0\n", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="No matching distribution found")

    with pytest.raises(su.SelfUpdateError, match="Failed to update job-squire"):
        su.self_update(http_get=_http_get("v0.8.0"), run=fake_run)


def test_network_error_wrapped_as_self_update_error():
    def http_get(url):
        raise su.SelfUpdateError("Could not reach the GitHub releases API (Name or service not known).")

    with pytest.raises(su.SelfUpdateError, match="Could not reach the GitHub releases API"):
        su.self_update(http_get=http_get, run=lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""))
