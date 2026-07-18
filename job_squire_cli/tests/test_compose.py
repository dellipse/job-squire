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


def test_render_compose_yaml_uses_named_volume_not_a_data_host_dir_bind_mount():
    """/data is a named volume scoped to this container, not a configurable
    host path -- see this module's docstring for why (WAL-mode SQLite over
    a bind mount bridged through OrbStack/Docker Desktop's VM filesystem
    layer). Only data/.env stays a plain host file, since env_file: has to
    read it before the volume even exists."""
    yaml_text = compose.render_compose_yaml(
        container_name="job-squire-castelo", image="ghcr.io/dellipse/job-squire:latest", loopback_only=True,
    )
    assert "job-squire-castelo-data:/data" in yaml_text
    assert "./data/.env:/data/.env:ro" in yaml_text
    assert "DATA_HOST_DIR" not in yaml_text
    assert "\n  job-squire-castelo-data:\n" in yaml_text  # the top-level volumes: block's own key


def test_render_compose_yaml_volume_has_explicit_name_avoiding_doubled_project_prefix():
    """Without an explicit `name:`, Compose's default naming prefixes the
    volume key with the project name -- and since `container_name` here is
    used both as `-p` (the project) and as the volume key's own
    `{container_name}-data` prefix, the *actual* volume Docker/Podman
    materializes would otherwise be doubled:
    `job-squire-castelo_job-squire-castelo-data`, not the plain
    `job-squire-castelo-data` every other part of this CLI (including the
    leftover-volume check in ops/lifecycle.py's `create_instance`) expects.
    The explicit `name:` line pins it to the plain form."""
    yaml_text = compose.render_compose_yaml(
        container_name="job-squire-castelo", image="ghcr.io/dellipse/job-squire:latest", loopback_only=True,
    )
    assert "    name: job-squire-castelo-data" in yaml_text


def test_data_volume_key_matches_render_compose_yaml_convention():
    assert compose.data_volume_key("job-squire-castelo") == "job-squire-castelo-data"


def test_render_compose_yaml_network_mode_binds_all_interfaces():
    yaml_text = compose.render_compose_yaml(
        container_name="job-squire-castelo", image="ghcr.io/dellipse/job-squire:latest", loopback_only=False,
    )
    assert '"0.0.0.0:${APP_HOST_PORT:-8080}:8000"' in yaml_text
    assert "networks:" not in yaml_text


def test_render_compose_yaml_proxy_network_adds_networks_block_without_dropping_ports():
    """Prompt C9: attaching a network-mode instance to a reverse proxy's
    shared Docker network is additive -- host-port publishing (still
    useful for direct/troubleshooting access) stays exactly as it was."""
    yaml_text = compose.render_compose_yaml(
        container_name="job-squire-castelo", image="ghcr.io/dellipse/job-squire:latest",
        loopback_only=False, proxy_network="job-squire-proxy",
    )
    assert '"0.0.0.0:${APP_HOST_PORT:-8080}:8000"' in yaml_text
    assert "    networks:\n      - job-squire-proxy" in yaml_text
    assert "networks:\n  job-squire-proxy:\n    external: true" in yaml_text


def test_render_compose_env_includes_hostports_when_given():
    env_text = compose.render_compose_env(app_port=8081, mcp_port=9001)
    assert "APP_HOST_PORT=8081" in env_text
    assert "MCP_HOST_PORT=9001" in env_text
    assert "PUID=1000" in env_text
    assert "DATA_HOST_DIR" not in env_text  # /data is a named volume now, not a configurable host path


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


def test_compose_down_default_never_passes_dash_v(tmp_path):
    """Every existing caller (update/rollback's stop-then-recreate, restore's
    teardown-before-replace) must keep behaving exactly as before
    `remove_volumes` was added -- default False, no `-v`."""
    root = tmp_path / "castelo"
    root.mkdir()
    run = fake_run()
    compose.compose_down("docker", root, "job-squire-castelo", run=run)
    args = run.calls[0]["args"]
    assert args[-1] == "down"
    assert "-v" not in args


def test_compose_down_remove_volumes_appends_dash_v(tmp_path):
    root = tmp_path / "castelo"
    root.mkdir()
    run = fake_run()
    compose.compose_down("docker", root, "job-squire-castelo", run=run, remove_volumes=True)
    args = run.calls[0]["args"]
    assert args[-2:] == ("down", "-v")


# ── volume lookup/removal (leftover-volume check on create, cleanup on remove) ──


def test_list_matching_volumes_parses_newline_separated_names():
    run = fake_run(returncode=0, stdout="job-squire-testdb-data\nsome-other-testdb-volume\n")
    volumes = compose.list_matching_volumes("docker", "testdb-data", run=run)
    assert volumes == ["job-squire-testdb-data", "some-other-testdb-volume"]
    args = run.calls[0]["args"]
    assert args[:3] == ("docker", "volume", "ls")
    assert "--filter" in args
    assert "name=testdb-data" in args


def test_list_matching_volumes_uses_podman_binary():
    run = fake_run(returncode=0, stdout="")
    compose.list_matching_volumes("podman", "testdb-data", run=run)
    assert run.calls[0]["args"][0] == "podman"


def test_list_matching_volumes_returns_empty_on_nonzero_exit():
    run = fake_run(returncode=1, stdout="", stderr="error talking to daemon")
    assert compose.list_matching_volumes("docker", "testdb-data", run=run) == []


def test_list_matching_volumes_returns_empty_on_no_output():
    run = fake_run(returncode=0, stdout="")
    assert compose.list_matching_volumes("docker", "testdb-data", run=run) == []


def test_remove_volume_invokes_runtime_volume_rm():
    run = fake_run()
    compose.remove_volume("docker", "job-squire-testdb-data", run=run)
    assert run.calls[0]["args"] == ("docker", "volume", "rm", "job-squire-testdb-data")


def test_remove_volume_uses_podman_binary():
    run = fake_run()
    compose.remove_volume("podman", "job-squire-testdb-data", run=run)
    assert run.calls[0]["args"][0] == "podman"


def test_remove_volume_returns_nonzero_result_rather_than_raising_on_failure():
    """Mirrors remove_image's own contract: a stubborn volume is reported
    back, not raised -- ops/lifecycle.py decides whether that's fatal."""
    run = fake_run(returncode=1, stderr="Error: volume is in use")
    result = compose.remove_volume("docker", "job-squire-testdb-data", run=run)
    assert result.returncode == 1
    assert "in use" in result.stderr


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


# ── update / rollback support (Prompt C7) ────────────────────────────────


def test_pull_image_invokes_runtime_pull(tmp_path):
    run = fake_run()
    compose.pull_image("docker", "ghcr.io/dellipse/job-squire:0.7.0", run=run)
    assert run.calls[0]["args"] == ("docker", "pull", "ghcr.io/dellipse/job-squire:0.7.0")


def test_pull_image_uses_podman_binary():
    run = fake_run()
    compose.pull_image("podman", "ghcr.io/dellipse/job-squire:0.7.0", run=run)
    assert run.calls[0]["args"][0] == "podman"


def test_remove_image_invokes_runtime_rmi():
    run = fake_run()
    compose.remove_image("docker", "ghcr.io/dellipse/job-squire:latest", run=run)
    assert run.calls[0]["args"] == ("docker", "rmi", "ghcr.io/dellipse/job-squire:latest")


def test_remove_image_uses_podman_binary():
    run = fake_run()
    compose.remove_image("podman", "ghcr.io/dellipse/job-squire:latest", run=run)
    assert run.calls[0]["args"][0] == "podman"


def test_remove_image_returns_nonzero_result_rather_than_raising_on_failure():
    """A failed `rmi` (e.g. the image is still in use by something outside
    job-squire's own registry) is reported back to the caller as a normal
    CompletedProcess, not raised -- ops/lifecycle.py's remove_instance is
    the one that decides whether that's fatal."""
    run = fake_run(returncode=1, stderr="Error: image is in use")
    result = compose.remove_image("docker", "ghcr.io/dellipse/job-squire:latest", run=run)
    assert result.returncode == 1
    assert "in use" in result.stderr


@pytest.mark.parametrize("version,expected", [
    ("latest", "ghcr.io/dellipse/job-squire:latest"),
    ("0.7.0", "ghcr.io/dellipse/job-squire:0.7.0"),
    ("sha-abc1234", "ghcr.io/dellipse/job-squire:sha-abc1234"),
    ("ghcr.io/someone-else/job-squire:1.2.3", "ghcr.io/someone-else/job-squire:1.2.3"),
])
def test_resolve_image(version, expected):
    assert compose.resolve_image(version) == expected


def test_resolve_image_respects_custom_repo():
    assert compose.resolve_image("0.7.0", repo="ghcr.io/mine/job-squire") == "ghcr.io/mine/job-squire:0.7.0"


def test_read_image_and_write_image_round_trip(tmp_path):
    root = tmp_path / "castelo"
    compose.write_compose_files(
        root, container_name="job-squire-castelo", image="ghcr.io/dellipse/job-squire:latest",
        loopback_only=True, app_port=8080, mcp_port=9000,
    )
    assert compose.read_image(root) == "ghcr.io/dellipse/job-squire:latest"

    compose.write_image(root, "ghcr.io/dellipse/job-squire:0.7.0")
    assert compose.read_image(root) == "ghcr.io/dellipse/job-squire:0.7.0"

    # Nothing else in the file moved -- container_name and ports untouched.
    yaml_text = paths.compose_path(root).read_text()
    assert "job-squire-castelo" in yaml_text
    assert '"127.0.0.1:${APP_HOST_PORT:-8080}:8000"' in yaml_text


def test_read_image_raises_when_no_image_line(tmp_path):
    root = tmp_path / "castelo"
    root.mkdir()
    paths.compose_path(root).write_text("services:\n  job-squire:\n    container_name: x\n")
    with pytest.raises(compose.ComposeError):
        compose.read_image(root)


def test_compose_env_value_round_trip(tmp_path):
    root = tmp_path / "castelo"
    compose.write_compose_files(
        root, container_name="job-squire-castelo", image=compose.DEFAULT_IMAGE,
        loopback_only=True, app_port=8080, mcp_port=9000,
    )
    assert compose.read_compose_env_value(root, "PREVIOUS_IMAGE") is None
    compose.set_compose_env_value(root, "PREVIOUS_IMAGE", "ghcr.io/dellipse/job-squire:0.6.0")
    assert compose.read_compose_env_value(root, "PREVIOUS_IMAGE") == "ghcr.io/dellipse/job-squire:0.6.0"
    # PUID/ports untouched by the targeted set.
    assert compose.read_compose_env_value(root, "APP_HOST_PORT") == "8080"


def test_compose_up_passes_through_extra_args(tmp_path):
    root = tmp_path / "castelo"
    root.mkdir()
    run = fake_run()
    compose.compose_up("docker", root, "job-squire-castelo", run=run, extra_args=["--force-recreate"])
    assert run.calls[0]["args"][-3:] == ("up", "-d", "--force-recreate")


# ── write_compose_files ──────────────────────────────────────────────────


def test_write_compose_files_never_touches_data_env(tmp_path):
    """write_compose_files must not create or write data/.env, unlike
    write_instance_files (which owns a fresh instance's data/.env) -- a
    rewrite of just the compose/env files (e.g. attaching a proxy network)
    must never risk the container-level secrets in data/.env."""
    root = tmp_path / "existing-install"
    root.mkdir()
    data_dir = root / "data"
    data_dir.mkdir()
    (data_dir / ".env").write_text("SECRET_KEY=untouched\n")

    compose.write_compose_files(
        root, container_name="job-squire-castelo", image=compose.DEFAULT_IMAGE,
        loopback_only=True, app_port=8080, mcp_port=9000,
    )

    assert (data_dir / ".env").read_text() == "SECRET_KEY=untouched\n"
    assert paths.compose_path(root).exists()
    assert paths.compose_env_path(root).exists()


def test_write_compose_files_preserves_custom_puid_pgid(tmp_path):
    root = tmp_path / "existing-install"
    compose.write_compose_files(
        root, container_name="job-squire-castelo", image=compose.DEFAULT_IMAGE,
        loopback_only=True, app_port=8080, mcp_port=9000,
        puid=2000, pgid=2001,
    )
    env_text = paths.compose_env_path(root).read_text()
    assert "PUID=2000" in env_text
    assert "PGID=2001" in env_text
    assert "DATA_HOST_DIR" not in env_text
