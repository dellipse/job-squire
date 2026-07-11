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
"""Compose/env rendering and the runtime-driven compose invocations
(Prompt C5). Every subprocess call is injected, same pattern as
test_runtime.py, so this never touches a real container runtime.
"""
import json
import stat
from types import SimpleNamespace

import pytest

from job_squire_cli.ops import compose, paths


def fake_run(returncode=0, stdout="", stderr=""):
    calls = []

    def _run(args, **kwargs):
        calls.append({"args": tuple(args), "kwargs": kwargs})
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    _run.calls = calls
    return _run


# ── runtime -> CLI translation ──────────────────────────────────────────


@pytest.mark.parametrize("runtime", ["docker", "orbstack", "colima"])
def test_runtime_binary_docker_like(runtime):
    assert compose.runtime_binary(runtime) == "docker"


def test_runtime_binary_podman():
    assert compose.runtime_binary("podman") == "podman"


def test_runtime_binary_unknown_raises():
    with pytest.raises(compose.ComposeError):
        compose.runtime_binary("bhyve")


@pytest.mark.parametrize("runtime,expected", [
    ("docker", ("docker", "compose")),
    ("orbstack", ("docker", "compose")),
    ("colima", ("docker", "compose")),
    ("podman", ("podman", "compose")),
])
def test_compose_binary(runtime, expected):
    assert compose.compose_binary(runtime) == expected


# ── rendering ────────────────────────────────────────────────────────────


def test_render_compose_yaml_local_mode_binds_loopback():
    yaml_text = compose.render_compose_yaml(
        container_name="job-squire-castelo", image="ghcr.io/dellipse/job-squire:latest", loopback_only=True,
    )
    assert "job-squire-castelo" in yaml_text
    assert "ghcr.io/dellipse/job-squire:latest" in yaml_text
    assert '"127.0.0.1:${APP_HOST_PORT:-8080}:8000"' in yaml_text
    assert '"127.0.0.1:${MCP_HOST_PORT:-9000}' in yaml_text
    assert "0.0.0.0" not in yaml_text
    assert "build:" not in yaml_text  # no repo checkout required to run a CLI-created instance


def test_render_compose_yaml_network_mode_binds_all_interfaces():
    yaml_text = compose.render_compose_yaml(
        container_name="job-squire-castelo", image="ghcr.io/dellipse/job-squire:latest", loopback_only=False,
    )
    assert '"0.0.0.0:${APP_HOST_PORT:-8080}:8000"' in yaml_text


def test_render_compose_env_includes_hostports_when_given():
    env_text = compose.render_compose_env(app_port=8081, mcp_port=9001)
    assert "APP_HOST_PORT=8081" in env_text
    assert "MCP_HOST_PORT=9001" in env_text
    assert "PUID=1000" in env_text
    assert "DATA_HOST_DIR=./data" in env_text


def test_render_compose_env_omits_hostports_when_not_given():
    env_text = compose.render_compose_env(app_port=None, mcp_port=None)
    assert "APP_HOST_PORT" not in env_text
    assert "MCP_HOST_PORT" not in env_text


def _sample_env(**overrides):
    kwargs = dict(
        secret_key="abc123", admin_username="admin", admin_password="hunter2",
        instance_name="castelo", cookie_name="castelo_session", deploy_mode="local",
        public_url="http://localhost:8080", mcp_port=9000,
    )
    kwargs.update(overrides)
    return compose.InstanceEnv(**kwargs)


def test_render_data_env_sets_explicit_session_cookie_name():
    """The one correctness-critical detail: SESSION_COOKIE_NAME must be set
    explicitly, because the app's own INSTANCE_NAME-based derivation
    (app/__init__.py) turns BOTH hyphens and spaces into underscores,
    while the registry's slug allows hyphens -- so for any instance name
    containing a hyphen, the two derivations would silently disagree if
    this weren't set here."""
    env_text = compose.render_data_env(_sample_env(instance_name="job-hunt-2", cookie_name="job-hunt-2_session"))
    assert "SESSION_COOKIE_NAME=job-hunt-2_session" in env_text
    assert "INSTANCE_NAME=job-hunt-2" in env_text


def test_render_data_env_omits_unset_trust_proxy_and_secure_cookie():
    """Leaving these unset lets app/deploy.py's DEPLOY_MODE preset fill
    them in, per PLAN Section 3's precedence rule."""
    env_text = compose.render_data_env(_sample_env())
    assert "TRUST_PROXY" not in env_text
    assert "SESSION_COOKIE_SECURE" not in env_text


def test_render_data_env_includes_explicit_overrides_when_given():
    env_text = compose.render_data_env(_sample_env(trust_proxy=True, session_cookie_secure=True))
    assert "TRUST_PROXY=true" in env_text
    assert "SESSION_COOKIE_SECURE=true" in env_text


def test_render_data_env_appends_extra_lines():
    env_text = compose.render_data_env(_sample_env(extra={"SCHEDULE_TZ": "America/Chicago"}))
    assert "SCHEDULE_TZ=America/Chicago" in env_text


def test_render_data_env_never_leaks_secret_key_of_another_instance():
    """Sanity check that the secret_key field is the only thing written
    under that name -- guards against a copy/paste bug that duplicates it."""
    env_text = compose.render_data_env(_sample_env(secret_key="unique-marker-xyz"))
    assert env_text.count("unique-marker-xyz") == 1


# ── write_instance_files ─────────────────────────────────────────────────


def test_write_instance_files_creates_expected_layout(tmp_path):
    root = tmp_path / "castelo"
    compose.write_instance_files(
        root, container_name="job-squire-castelo", image=compose.DEFAULT_IMAGE,
        loopback_only=True, app_port=8080, mcp_port=9000, env=_sample_env(),
    )
    assert paths.compose_path(root).exists()
    assert paths.compose_env_path(root).exists()
    assert paths.data_env_path(root).exists()
    assert paths.data_dir(root).is_dir()


def test_write_instance_files_restricts_data_env_permissions(tmp_path):
    root = tmp_path / "castelo"
    compose.write_instance_files(
        root, container_name="job-squire-castelo", image=compose.DEFAULT_IMAGE,
        loopback_only=True, app_port=8080, mcp_port=9000, env=_sample_env(),
    )
    mode = stat.S_IMODE(paths.data_env_path(root).stat().st_mode)
    assert mode == 0o600


# ── driving the runtime ───────────────────────────────────────────────────


def test_compose_up_uses_project_directory_and_env_file(tmp_path):
    root = tmp_path / "castelo"
    root.mkdir()
    run = fake_run()
    compose.compose_up("docker", root, "job-squire-castelo", run=run)
    args = run.calls[0]["args"]
    assert args[:2] == ("docker", "compose")
    assert "--project-directory" in args
    assert str(root) in args
    assert "-p" in args and "job-squire-castelo" in args
    assert "up" in args and "-d" in args


def test_compose_stop_start_restart_down_use_podman_binary(tmp_path):
    root = tmp_path / "castelo"
    root.mkdir()
    run = fake_run()
    compose.compose_stop("podman", root, "job-squire-castelo", run=run)
    compose.compose_start("podman", root, "job-squire-castelo", run=run)
    compose.compose_restart("podman", root, "job-squire-castelo", run=run)
    compose.compose_down("podman", root, "job-squire-castelo", run=run)
    subcommands = [c["args"][-1] if c["args"][-1] != "-d" else c["args"][-2] for c in run.calls]
    assert subcommands == ["stop", "start", "restart", "down"]
    assert all(c["args"][:2] == ("podman", "compose") for c in run.calls)


# ── observing container state ────────────────────────────────────────────


def test_inspect_state_parses_json():
    run = fake_run(returncode=0, stdout=json.dumps({"Status": "running", "Health": {"Status": "healthy"}}))
    state = compose.inspect_state("docker", "job-squire-castelo", run=run)
    assert state == {"Status": "running", "Health": {"Status": "healthy"}}


def test_inspect_state_returns_none_on_nonzero_exit():
    run = fake_run(returncode=1, stdout="")
    assert compose.inspect_state("docker", "nonexistent", run=run) is None


def test_inspect_state_returns_none_on_malformed_json():
    run = fake_run(returncode=0, stdout="not json")
    assert compose.inspect_state("docker", "job-squire-castelo", run=run) is None


def test_container_logs_combines_stdout_and_stderr():
    run = fake_run(returncode=0, stdout="normal log\n", stderr="FATAL: bad config\n")
    logs = compose.container_logs("docker", "job-squire-castelo", run=run)
    assert "normal log" in logs
    assert "FATAL: bad config" in logs


def test_extract_fatal_lines_filters_to_fatal_prefix():
    logs = "INFO: booting\nFATAL: PUBLIC_URL='http://x' is unsafe. Fix: use https.\nother line\n"
    fatal = compose.extract_fatal_lines(logs)
    assert fatal == ["FATAL: PUBLIC_URL='http://x' is unsafe. Fix: use https."]


def test_extract_fatal_lines_empty_when_none_present():
    assert compose.extract_fatal_lines("INFO: all good\n") == []
