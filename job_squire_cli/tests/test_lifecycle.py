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
"""Instance lifecycle core: create/start/stop/restart/status/remove
(Prompt C5).

FakeRuntime stands in for `docker`/`podman` end to end: it answers
`docker info` (runtime detection), `docker compose ... up/stop/start/
restart/down` (flipping an in-memory container state keyed by compose
project name), `docker inspect --format {{json .State}}` (reading that
state back), and `docker logs` (a per-container log buffer, the channel
the startup guard's FATAL lines travel through). On a successful `up`, it
also creates a minimal sqlite database at the instance's data dir --
standing in for the app's own first-boot schema creation -- so the
`--import-from` tests can exercise the real ops/secrets_copy.py path
against actual files, not a mock.

No test here touches a real container runtime, a real socket bind (see
ops/ports.py -- `port_free` isn't injectable through create_instance, so
these tests rely on high, almost-certainly-free port numbers never
colliding with whatever's actually listening on the test machine), or the
real per-user registry (XDG_CONFIG_HOME is redirected to a tmp_path, same
as test_registry.py).
"""
import json
import shutil
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from job_squire_cli.ops import compose, crypto_mirror, lifecycle as lc, paths
from job_squire_cli.ops import registry as reg
from job_squire_cli.query import config as query_config_module

from tests.test_secrets_copy import _SCHEMA, _seed_dest_defaults, _seed_source


@pytest.fixture(autouse=True)
def force_linux_config_dir(monkeypatch):
    monkeypatch.setattr(query_config_module.platform, "system", lambda: "Linux")


@pytest.fixture(autouse=True)
def tmp_registry(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return tmp_path / "job-squire" / reg.REGISTRY_FILENAME


@pytest.fixture
def data_root(tmp_path):
    return tmp_path / "instances"


def which_map(present):
    return lambda name: present.get(name)


class FakeRuntime:
    """See module docstring. `fail_projects` marks compose projects whose
    container "exits" immediately after up/start/restart with a FATAL log
    line, simulating app/deploy.py's startup guard refusing to boot."""

    def __init__(self):
        self.containers: dict[str, dict] = {}
        self.logs: dict[str, str] = {}
        self.fail_projects: set[str] = set()
        self.fail_pulls: set[str] = set()
        self.fail_stops: set[str] = set()
        self.calls: list[tuple] = []

    def run(self, args, **kwargs):
        args = list(args)
        self.calls.append(tuple(args))

        if args[:2] in (["docker", "info"], ["podman", "info"]):
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        if len(args) == 3 and args[1] == "pull":
            image = args[2]
            if image in self.fail_pulls:
                return SimpleNamespace(returncode=1, stdout="", stderr=f"error pulling {image}")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        if len(args) >= 2 and args[1] == "compose":
            project = args[args.index("-p") + 1]
            cwd = kwargs.get("cwd")
            if "up" in args and "-d" in args:
                self._on_up(project, cwd)
            elif args[-1] == "stop":
                if project in self.fail_stops:
                    return SimpleNamespace(returncode=1, stdout="", stderr=f"error stopping {project}")
                self._on_stop(project)
            elif args[-1] in ("start", "restart"):
                self._on_up(project, cwd)
            elif args[-1] == "down":
                self.containers.pop(project, None)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        if len(args) >= 2 and args[1] == "inspect":
            state = self.containers.get(args[-1])
            if state is None:
                return SimpleNamespace(returncode=1, stdout="", stderr="no such container")
            return SimpleNamespace(returncode=0, stdout=json.dumps(state), stderr="")

        if len(args) >= 2 and args[1] == "logs":
            return SimpleNamespace(returncode=0, stdout="", stderr=self.logs.get(args[-1], ""))

        raise AssertionError(f"unexpected call in test: {args}")

    def _on_up(self, project, cwd):
        if project in self.fail_projects:
            self.containers[project] = {"Status": "exited", "ExitCode": 1}
            self.logs[project] = (
                "INFO: booting\n"
                "FATAL: PUBLIC_URL='' is unsafe for DEPLOY_MODE='network': network mode "
                "assumes an external reverse proxy terminates TLS. Fix: set "
                "PUBLIC_URL=https://<your-domain>, or set DEPLOY_MODE=local.\n"
            )
            return
        self.containers[project] = {"Status": "running", "Health": {"Status": "healthy"}}
        if cwd is not None:
            db_path = paths.sqlite_db_path(Path(cwd))
            if not db_path.exists():
                db_path.parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(str(db_path))
                conn.executescript(_SCHEMA)
                _seed_dest_defaults(conn)
                conn.close()

    def _on_stop(self, project):
        state = self.containers.get(project)
        if state is not None:
            state["Status"] = "exited"
            state.pop("Health", None)


@pytest.fixture
def fake():
    return FakeRuntime()


def create_kwargs(fake, **overrides):
    kwargs = dict(
        run=fake.run, which=which_map({"docker": "/usr/bin/docker"}),
        sleep=lambda _s: None, confirm=lambda _msg: True,
    )
    kwargs.update(overrides)
    return kwargs


# ── create: local mode end to end ────────────────────────────────────────


def test_create_local_instance_end_to_end(fake, data_root):
    result = lc.create_instance(name="Castelo", mode="local", data_root=data_root, **create_kwargs(fake))

    inst = result.instance
    assert inst.name == "castelo"
    assert inst.mode == "local"
    assert inst.runtime == "docker"
    assert reg.get_instance("castelo") == inst  # registered
    assert result.health["Status"] == "running"
    assert result.health["Health"]["Status"] == "healthy"

    # Only localhost/127.0.0.1 links -- never a LAN IP (PLAN Section 5).
    assert inst.public_url.startswith("http://localhost:")
    assert "127.0.0.1" in paths.compose_path(paths.instance_root("castelo", data_root)).read_text()

    # Admin credentials were generated and reported back for display.
    assert result.admin_password_generated is True
    assert len(result.admin_password) > 8


def test_create_second_local_instance_gets_distinct_ports_and_cookie_names(fake, data_root):
    first = lc.create_instance(name="one", mode="local", data_root=data_root, **create_kwargs(fake))
    second = lc.create_instance(name="two", mode="local", data_root=data_root, **create_kwargs(fake))

    assert first.instance.app_port != second.instance.app_port
    assert first.instance.mcp_port != second.instance.mcp_port
    assert first.instance.cookie_name != second.instance.cookie_name
    assert first.instance.cookie_name == "one_session"
    assert second.instance.cookie_name == "two_session"


def test_create_writes_session_cookie_name_explicitly_for_hyphenated_names(fake, data_root):
    result = lc.create_instance(name="job-hunt-2", mode="local", data_root=data_root, **create_kwargs(fake))
    data_env = paths.data_env_path(paths.instance_root("job-hunt-2", data_root)).read_text()
    assert "SESSION_COOKIE_NAME=job-hunt-2_session" in data_env
    assert result.instance.cookie_name == "job-hunt-2_session"


def test_create_never_prompts_for_runtime_install_when_one_already_works(fake, data_root):
    lc.create_instance(name="castelo", mode="local", data_root=data_root, **create_kwargs(fake))
    assert ("docker", "info") in fake.calls
    assert not any("brew" in call or "apt-get" in call for call in fake.calls)


# ── create: name collisions fail fast, before any side effects ──────────


def test_create_collision_raises_before_touching_runtime_or_disk(fake, data_root):
    lc.create_instance(name="castelo", mode="local", data_root=data_root, **create_kwargs(fake))
    calls_after_first = len(fake.calls)

    with pytest.raises(reg.NameCollisionError):
        lc.create_instance(name="Castelo", mode="local", data_root=data_root, **create_kwargs(fake))

    # No new runtime/compose calls were made for the rejected second create.
    assert len(fake.calls) == calls_after_first


# ── create: network mode and the startup guard ───────────────────────────


def test_create_network_mode_requires_hostname(fake, data_root):
    with pytest.raises(lc.LifecycleError, match="hostname"):
        lc.create_instance(name="castelo", mode="network", data_root=data_root, **create_kwargs(fake))


def test_create_surfaces_startup_guard_failure_verbatim(fake, data_root):
    fake.fail_projects.add("job-squire-castelo")

    with pytest.raises(lc.StartupGuardFailure) as excinfo:
        lc.create_instance(
            name="castelo", mode="network", hostname="squire.example.com",
            data_root=data_root, **create_kwargs(fake),
        )

    messages = excinfo.value.messages
    assert len(messages) == 1
    assert messages[0].startswith("FATAL:")
    assert "PUBLIC_URL" in messages[0]
    assert "Fix:" in messages[0]

    # The instance is still registered even though it failed to come up --
    # `status`/`remove` need to be able to see and clean up a failed create.
    assert reg.get_instance("castelo") is not None


def test_create_network_mode_success_binds_all_interfaces_not_loopback(fake, data_root):
    result = lc.create_instance(
        name="castelo", mode="network", hostname="squire.example.com",
        data_root=data_root, **create_kwargs(fake),
    )
    compose_yaml = paths.compose_path(paths.instance_root("castelo", data_root)).read_text()
    assert "0.0.0.0:" in compose_yaml
    assert result.instance.public_url == "https://squire.example.com"


# ── start / stop / restart ───────────────────────────────────────────────


def test_start_stop_restart_roundtrip(fake, data_root):
    lc.create_instance(name="castelo", mode="local", data_root=data_root, **create_kwargs(fake))

    lc.stop_instance("castelo", data_root=data_root, run=fake.run)
    assert fake.containers["job-squire-castelo"]["Status"] == "exited"

    state = lc.start_instance("castelo", data_root=data_root, run=fake.run, sleep=lambda _s: None)
    assert state["Status"] == "running"

    state = lc.restart_instance("castelo", data_root=data_root, run=fake.run, sleep=lambda _s: None)
    assert state["Status"] == "running"


def test_start_unregistered_instance_raises_not_found(fake, data_root):
    with pytest.raises(lc.InstanceNotFoundError):
        lc.start_instance("ghost", data_root=data_root, run=fake.run)


def test_start_surfaces_guard_failure(fake, data_root):
    lc.create_instance(name="castelo", mode="local", data_root=data_root, **create_kwargs(fake))
    lc.stop_instance("castelo", data_root=data_root, run=fake.run)
    fake.fail_projects.add("job-squire-castelo")

    with pytest.raises(lc.StartupGuardFailure):
        lc.start_instance("castelo", data_root=data_root, run=fake.run, sleep=lambda _s: None)


# ── remove: keep-or-delete-data prompt ───────────────────────────────────


def test_remove_deletes_data_when_confirmed(fake, data_root):
    lc.create_instance(name="castelo", mode="local", data_root=data_root, **create_kwargs(fake))
    root = paths.instance_root("castelo", data_root)
    assert root.exists()

    result = lc.remove_instance(
        "castelo", data_root=data_root, run=fake.run, confirm_delete=lambda _msg: True,
    )
    assert result.data_kept is False
    assert not root.exists()
    assert reg.get_instance("castelo") is None
    assert "job-squire-castelo" not in fake.containers


def test_remove_keeps_data_when_declined(fake, data_root):
    lc.create_instance(name="castelo", mode="local", data_root=data_root, **create_kwargs(fake))
    root = paths.instance_root("castelo", data_root)

    result = lc.remove_instance(
        "castelo", data_root=data_root, run=fake.run, confirm_delete=lambda _msg: False,
    )
    assert result.data_kept is True
    assert root.exists()
    assert reg.get_instance("castelo") is None


def test_remove_defaults_to_keeping_data_when_nothing_asks(fake, data_root):
    """No confirm_delete and no explicit keep_data: the safe default wins,
    per PLAN Section 4 ("removing an instance never silently destroys
    someone's job-search history")."""
    lc.create_instance(name="castelo", mode="local", data_root=data_root, **create_kwargs(fake))
    root = paths.instance_root("castelo", data_root)

    result = lc.remove_instance("castelo", data_root=data_root, run=fake.run)
    assert result.data_kept is True
    assert root.exists()


def test_remove_explicit_keep_data_skips_the_prompt_entirely(fake, data_root):
    lc.create_instance(name="castelo", mode="local", data_root=data_root, **create_kwargs(fake))

    def exploding_confirm(_msg):
        raise AssertionError("should never be asked when keep_data is explicit")

    result = lc.remove_instance(
        "castelo", data_root=data_root, run=fake.run, keep_data=False, confirm_delete=exploding_confirm,
    )
    assert result.data_kept is False


def test_remove_instance_with_already_missing_root_skips_compose_down(fake, data_root):
    """A registry entry can outlive its root directory -- e.g. someone rm
    -rf'd a scratch/verify data_root, or a prior uninstall died partway
    through. subprocess.Popen raises FileNotFoundError outright when `cwd`
    doesn't exist, so there's nothing for `docker/podman compose down` to
    do; remove_instance must not call it, and must still clear the
    registry entry rather than blowing up (which would otherwise abort a
    multi-instance `uninstall_everything` loop partway through)."""
    lc.create_instance(name="castelo", mode="local", data_root=data_root, **create_kwargs(fake))
    root = paths.instance_root("castelo", data_root)
    shutil.rmtree(root)
    assert not root.exists()

    result = lc.remove_instance("castelo", data_root=data_root, run=fake.run, keep_data=True)

    assert result.data_kept is True
    assert reg.get_instance("castelo") is None
    assert not any(call[-1] == "down" for call in fake.calls)


# ── status / list: health and drift ──────────────────────────────────────


def test_status_for_healthy_instance(fake, data_root):
    lc.create_instance(name="castelo", mode="local", data_root=data_root, **create_kwargs(fake))
    status = lc.status_for(reg.get_instance("castelo"), run=fake.run)
    assert status.health == "healthy"
    assert status.observed.container_running is True
    assert status.drift == []


def test_status_reports_drift_when_container_missing_outside_the_cli(fake, data_root):
    lc.create_instance(name="castelo", mode="local", data_root=data_root, **create_kwargs(fake))
    # Simulate the container having been removed by a direct `docker rm`
    # outside the CLI (PLAN Section 7 "If a divergence does happen").
    del fake.containers["job-squire-castelo"]

    status = lc.status_for(reg.get_instance("castelo"), run=fake.run)
    assert status.health == "not created"
    assert status.observed.container_running is False
    assert any(d.field == "container" for d in status.drift)


def test_list_status_covers_every_registered_instance(fake, data_root):
    lc.create_instance(name="one", mode="local", data_root=data_root, **create_kwargs(fake))
    lc.create_instance(name="two", mode="local", data_root=data_root, **create_kwargs(fake))
    statuses = lc.list_status(run=fake.run)
    assert {s.instance.name for s in statuses} == {"one", "two"}


# ── create --import-from ──────────────────────────────────────────────────


def test_create_import_from_copies_schedule_env_and_db_settings(fake, data_root):
    lc.create_instance(name="source", mode="local", data_root=data_root, **create_kwargs(fake))
    source_root = paths.instance_root("source", data_root)

    # Seed the source instance's schedule vars (as if set by hand/settings)
    # and its database (as the real app would have it after use).
    with paths.data_env_path(source_root).open("a") as fh:
        fh.write("\nSCHEDULE_TZ=America/Chicago\nSCHEDULE_WEEKDAY_HOURS=8,13,17\n")
    source_secret_key = lc.secrets_copy.read_secret_key(source_root)
    conn = sqlite3.connect(str(paths.sqlite_db_path(source_root)))
    conn.executescript("DELETE FROM search_config; DELETE FROM smtp_config; DELETE FROM ai_config; "
                        "DELETE FROM users;")
    _seed_source(conn, source_secret_key)
    conn.close()

    dest_result = lc.create_instance(
        name="dest", mode="local", data_root=data_root, import_from="source",
        **create_kwargs(fake),
    )

    dest_root = paths.instance_root("dest", data_root)
    dest_env = paths.data_env_path(dest_root).read_text()
    assert "SCHEDULE_TZ=America/Chicago" in dest_env
    assert "SCHEDULE_WEEKDAY_HOURS=8,13,17" in dest_env

    dconn = sqlite3.connect(str(paths.sqlite_db_path(dest_root)))
    dconn.row_factory = sqlite3.Row
    search = dconn.execute("SELECT * FROM search_config WHERE id = 1").fetchone()
    assert search["titles"] == "Engineer"
    assert search["location"] == "Austin, TX"
    dconn.close()

    assert dest_result.import_summary is not None
    assert "search_config" in dest_result.import_summary.tables_copied
    assert dest_result.import_summary.secrets_copied is False

    # The instance was stopped and restarted around the direct db write,
    # and ends up running again.
    assert fake.containers["job-squire-dest"]["Status"] == "running"


def test_create_import_from_unknown_instance_raises(fake, data_root):
    with pytest.raises(lc.NoImportSourceError):
        lc.create_instance(
            name="dest", mode="local", data_root=data_root, import_from="nonexistent",
            **create_kwargs(fake),
        )


# ── update / rollback (Prompt C7) ────────────────────────────────────────


def test_update_pulls_stops_swaps_and_recreates_in_order(fake, data_root):
    lc.create_instance(name="castelo", mode="local", data_root=data_root, **create_kwargs(fake))
    root = paths.instance_root("castelo", data_root)
    assert compose.read_image(root) == compose.DEFAULT_IMAGE

    result = lc.update_instance(
        "castelo", version="0.7.0", data_root=data_root, run=fake.run, sleep=lambda _s: None,
    )

    assert result.previous_image == compose.DEFAULT_IMAGE
    assert result.new_image == "ghcr.io/dellipse/job-squire:0.7.0"
    assert compose.read_image(root) == "ghcr.io/dellipse/job-squire:0.7.0"
    assert result.health["Status"] == "running"

    # Order: pull, then stop, then up --force-recreate.
    kinds = []
    for call in fake.calls:
        if call[1] == "pull":
            kinds.append("pull")
        elif call[1] == "compose" and call[-1] == "stop":
            kinds.append("stop")
        elif call[1] == "compose" and "up" in call and "-d" in call:
            kinds.append("up")
    first_pull = kinds.index("pull")
    first_stop = kinds.index("stop")
    last_up = len(kinds) - 1 - kinds[::-1].index("up")
    assert first_pull < first_stop < last_up


def test_update_records_previous_image_in_compose_env(fake, data_root):
    lc.create_instance(name="castelo", mode="local", data_root=data_root, **create_kwargs(fake))
    root = paths.instance_root("castelo", data_root)

    lc.update_instance("castelo", version="0.7.0", data_root=data_root, run=fake.run, sleep=lambda _s: None)
    assert compose.read_compose_env_value(root, "PREVIOUS_IMAGE") == compose.DEFAULT_IMAGE


def test_update_defaults_to_latest(fake, data_root):
    lc.create_instance(name="castelo", mode="local", data_root=data_root, **create_kwargs(fake))
    root = paths.instance_root("castelo", data_root)
    compose.write_image(root, "ghcr.io/dellipse/job-squire:0.6.0")

    result = lc.update_instance("castelo", data_root=data_root, run=fake.run, sleep=lambda _s: None)
    assert result.new_image == "ghcr.io/dellipse/job-squire:latest"


def test_update_failed_pull_never_touches_the_running_container(fake, data_root):
    """The core WAL-safety guarantee: if the pull fails, the container is
    never stopped and the compose file's image line never changes."""
    lc.create_instance(name="castelo", mode="local", data_root=data_root, **create_kwargs(fake))
    root = paths.instance_root("castelo", data_root)
    fake.fail_pulls.add("ghcr.io/dellipse/job-squire:0.7.0")

    with pytest.raises(lc.LifecycleError, match="Failed to pull"):
        lc.update_instance("castelo", version="0.7.0", data_root=data_root, run=fake.run, sleep=lambda _s: None)

    assert fake.containers["job-squire-castelo"]["Status"] == "running"
    assert compose.read_image(root) == compose.DEFAULT_IMAGE
    assert compose.read_compose_env_value(root, "PREVIOUS_IMAGE") is None
    assert not any(call[1] == "compose" and call[-1] == "stop" for call in fake.calls)


def test_update_failed_stop_leaves_image_unswapped(fake, data_root):
    lc.create_instance(name="castelo", mode="local", data_root=data_root, **create_kwargs(fake))
    root = paths.instance_root("castelo", data_root)
    fake.fail_stops.add("job-squire-castelo")

    with pytest.raises(lc.LifecycleError, match="Failed to stop"):
        lc.update_instance("castelo", version="0.7.0", data_root=data_root, run=fake.run, sleep=lambda _s: None)

    assert compose.read_image(root) == compose.DEFAULT_IMAGE
    assert compose.read_compose_env_value(root, "PREVIOUS_IMAGE") is None


def test_update_surfaces_startup_guard_failure_on_the_new_image(fake, data_root):
    lc.create_instance(name="castelo", mode="local", data_root=data_root, **create_kwargs(fake))
    fake.fail_projects.add("job-squire-castelo")

    with pytest.raises(lc.StartupGuardFailure):
        lc.update_instance("castelo", version="0.7.0", data_root=data_root, run=fake.run, sleep=lambda _s: None)


def test_update_unregistered_instance_raises_not_found(fake, data_root):
    with pytest.raises(lc.InstanceNotFoundError):
        lc.update_instance("ghost", data_root=data_root, run=fake.run)


def test_rollback_returns_to_the_previous_image(fake, data_root):
    lc.create_instance(name="castelo", mode="local", data_root=data_root, **create_kwargs(fake))
    root = paths.instance_root("castelo", data_root)

    lc.update_instance("castelo", version="0.7.0", data_root=data_root, run=fake.run, sleep=lambda _s: None)
    result = lc.rollback_instance("castelo", data_root=data_root, run=fake.run, sleep=lambda _s: None)

    assert result.new_image == compose.DEFAULT_IMAGE
    assert compose.read_image(root) == compose.DEFAULT_IMAGE
    # A second rollback swaps forward again -- no data lost either direction.
    result2 = lc.rollback_instance("castelo", data_root=data_root, run=fake.run, sleep=lambda _s: None)
    assert result2.new_image == "ghcr.io/dellipse/job-squire:0.7.0"


def test_rollback_without_a_prior_update_raises(fake, data_root):
    lc.create_instance(name="castelo", mode="local", data_root=data_root, **create_kwargs(fake))
    with pytest.raises(lc.LifecycleError, match="nothing to roll back"):
        lc.rollback_instance("castelo", data_root=data_root, run=fake.run, sleep=lambda _s: None)


# ── adopt (Prompt C7) ────────────────────────────────────────────────────


def _write_legacy_install(
    root: Path, *, instance_name="castelo", app_port=None, mcp_port=None,
    public_url=None, secret_key="legacy-secret-key-0123456789", extra_lines=(),
):
    data_dir = root / "data"
    data_dir.mkdir(parents=True)
    lines = [f"SECRET_KEY={secret_key}", f"INSTANCE_NAME={instance_name}"]
    if app_port is not None:
        lines.append(f"APP_HOST_PORT={app_port}")
    if mcp_port is not None:
        lines.append(f"MCP_HOST_PORT={mcp_port}")
    if public_url is not None:
        lines.append(f"PUBLIC_URL={public_url}")
    lines.append(f"DATA_HOST_DIR={data_dir}")
    lines.extend(extra_lines)
    (data_dir / ".env").write_text("\n".join(lines) + "\n")
    return root


def test_adopt_registers_instance_with_derived_slug_and_legacy_cookie_name(fake, tmp_path):
    install_dir = _write_legacy_install(tmp_path / "install", instance_name="Castelo HQ")

    result = lc.adopt_instance(install_dir, **create_kwargs(fake))

    assert result.instance.name == "castelo-hq"  # sanitize_slug: lowercase, space -> hyphen
    assert result.cookie_name == "castelo_hq_session"  # app's own derivation: space -> underscore
    assert reg.get_instance("castelo-hq") is not None
    assert reg.get_instance("castelo-hq").data_dir == str(install_dir)


def test_adopt_never_rewrites_secret_key_and_a_stored_secret_still_decrypts(fake, tmp_path):
    secret_key = "legacy-secret-key-0123456789"
    plaintext = "sk-super-secret-provider-key"
    # Simulate a value the app previously encrypted and stored (in its real
    # database, not the .env -- but the property under test is just that
    # SECRET_KEY itself survives adopt untouched, which is what makes any
    # such stored value still decryptable).
    encrypted = crypto_mirror.encrypt(secret_key, plaintext)

    install_dir = _write_legacy_install(tmp_path / "install", secret_key=secret_key)
    lc.adopt_instance(install_dir, **create_kwargs(fake))

    data_env = paths.data_env_path(install_dir).read_text()
    assert f"SECRET_KEY={secret_key}" in data_env
    assert crypto_mirror.decrypt(secret_key, encrypted) == plaintext


def test_adopt_appends_trust_proxy_and_secure_cookie_only_when_absent(fake, tmp_path):
    install_dir = _write_legacy_install(tmp_path / "install")

    result = lc.adopt_instance(install_dir, **create_kwargs(fake))

    data_env = paths.data_env_path(install_dir).read_text()
    assert "TRUST_PROXY=1" in data_env
    assert "SESSION_COOKIE_SECURE=true" in data_env
    assert set(result.env_appended) == {"TRUST_PROXY=1", "SESSION_COOKIE_SECURE=true"}
    assert result.env_backup.exists()
    assert "SECRET_KEY=legacy-secret-key-0123456789" in result.env_backup.read_text()


def test_adopt_does_not_append_when_already_explicitly_set(fake, tmp_path):
    install_dir = _write_legacy_install(
        tmp_path / "install", extra_lines=["TRUST_PROXY=0", "SESSION_COOKIE_SECURE=false"],
    )

    result = lc.adopt_instance(install_dir, **create_kwargs(fake))

    data_env = paths.data_env_path(install_dir).read_text()
    assert data_env.count("TRUST_PROXY=") == 1  # never duplicated
    assert "TRUST_PROXY=0" in data_env  # the operator's explicit value survives
    assert result.env_appended == []


def test_adopt_preserves_existing_host_ports(fake, tmp_path):
    install_dir = _write_legacy_install(tmp_path / "install", app_port=8123, mcp_port=9123)

    result = lc.adopt_instance(install_dir, **create_kwargs(fake))

    assert result.instance.app_port == 8123
    assert result.instance.mcp_port == 9123
    compose_env = paths.compose_env_path(install_dir).read_text()
    assert "APP_HOST_PORT=8123" in compose_env
    assert "MCP_HOST_PORT=9123" in compose_env


def test_adopt_writes_compose_files_without_touching_data_env_contents(fake, tmp_path):
    install_dir = _write_legacy_install(tmp_path / "install")
    before = paths.data_env_path(install_dir).read_text()

    lc.adopt_instance(install_dir, **create_kwargs(fake))

    assert paths.compose_path(install_dir).exists()
    assert paths.compose_env_path(install_dir).exists()
    after = paths.data_env_path(install_dir).read_text()
    # Only the two additive lines were appended -- every original line
    # (in the original order) is still there untouched.
    for line in before.splitlines():
        assert line in after


def test_adopt_rejects_a_directory_with_no_data_env(fake, tmp_path):
    empty_dir = tmp_path / "not-an-install"
    empty_dir.mkdir()
    with pytest.raises(lc.NotALegacyInstallError):
        lc.adopt_instance(empty_dir, **create_kwargs(fake))


def test_adopt_rejects_a_data_env_with_no_secret_key(fake, tmp_path):
    install_dir = tmp_path / "install"
    (install_dir / "data").mkdir(parents=True)
    (install_dir / "data" / ".env").write_text("INSTANCE_NAME=castelo\n")
    with pytest.raises(lc.NotALegacyInstallError):
        lc.adopt_instance(install_dir, **create_kwargs(fake))


def test_adopt_rejects_name_collision(fake, data_root, tmp_path):
    lc.create_instance(name="castelo", mode="local", data_root=data_root, **create_kwargs(fake))
    install_dir = _write_legacy_install(tmp_path / "install", instance_name="castelo")
    with pytest.raises(reg.NameCollisionError):
        lc.adopt_instance(install_dir, **create_kwargs(fake))


def test_adopt_rejects_port_collision_with_another_registered_instance(fake, data_root, tmp_path):
    lc.create_instance(name="one", mode="local", data_root=data_root, **create_kwargs(fake))
    used_port = reg.get_instance("one").app_port
    install_dir = _write_legacy_install(tmp_path / "install", instance_name="two", app_port=used_port)
    with pytest.raises(lc.LifecycleError, match="already used"):
        lc.adopt_instance(install_dir, **create_kwargs(fake))


def test_adopt_rejects_shared_network_topology(fake, tmp_path):
    install_dir = _write_legacy_install(tmp_path / "install", extra_lines=["SWAG_NETWORK=proxynet"])
    with pytest.raises(lc.LifecycleError, match="shared-Docker-network"):
        lc.adopt_instance(install_dir, **create_kwargs(fake))


def test_adopt_name_override_does_not_change_the_derived_cookie_name(fake, tmp_path):
    """--name only affects the registry slug -- the cookie the *app* will
    actually emit is still governed by the untouched INSTANCE_NAME in
    data/.env, so the two must be allowed to diverge."""
    install_dir = _write_legacy_install(tmp_path / "install", instance_name="castelo")
    result = lc.adopt_instance(install_dir, name="renamed", **create_kwargs(fake))
    assert result.instance.name == "renamed"
    assert result.cookie_name == "castelo_session"


def test_adopt_with_bring_up_starts_the_single_container_and_reports_health(fake, tmp_path):
    install_dir = _write_legacy_install(tmp_path / "install")
    result = lc.adopt_instance(install_dir, bring_up=True, **create_kwargs(fake))
    assert result.health["Status"] == "running"
    assert fake.containers["job-squire-castelo"]["Status"] == "running"


def test_adopt_with_bring_up_refuses_while_legacy_container_still_running(fake, tmp_path):
    install_dir = _write_legacy_install(tmp_path / "install", instance_name="castelo")
    # Simulate the old three-container stack's `container_name: castelo`
    # still up (docker-compose.yml).
    fake.containers["castelo"] = {"Status": "running"}

    with pytest.raises(lc.LifecycleError, match="still running"):
        lc.adopt_instance(install_dir, bring_up=True, **create_kwargs(fake))

    # Registration still happened -- only the bring-up step refused.
    assert reg.get_instance("castelo") is not None
    assert "job-squire-castelo" not in fake.containers


def test_adopt_without_bring_up_does_not_start_anything(fake, tmp_path):
    install_dir = _write_legacy_install(tmp_path / "install")
    result = lc.adopt_instance(install_dir, bring_up=False, **create_kwargs(fake))
    assert result.health is None
    assert "job-squire-castelo" not in fake.containers


def test_lifecycle_commands_find_an_adopted_instance_by_its_registered_data_dir(fake, tmp_path, monkeypatch):
    """Regression test: start/stop/update/remove must resolve an existing
    instance's directory from the registry's own `data_dir`, not by
    re-deriving `<default_data_root>/<name>` from the name -- those two
    only coincide for a `create`-made instance. `adopt_instance` registers
    a directory that lives wherever the operator's install already was
    (here, well outside the default data root), so calling `update`/
    `start`/`stop` with *no* `data_root` override -- exactly how the real
    CLI invokes them -- must still find the right place.
    """
    # Redirect the default data root somewhere that must NOT be touched by
    # this test, so a wrong resolution back to instance_root(name) would
    # write/read there instead of failing loudly.
    monkeypatch.setenv("JOB_SQUIRE_HOME", str(tmp_path / "unrelated-default-root"))

    install_dir = _write_legacy_install(tmp_path / "somewhere" / "else" / "castelo-data")
    lc.adopt_instance(install_dir, bring_up=True, **create_kwargs(fake))

    wrong_root = tmp_path / "unrelated-default-root" / "castelo"
    assert not wrong_root.exists()

    result = lc.update_instance(
        "castelo", version="0.7.0", run=fake.run, sleep=lambda _s: None,
    )
    assert result.new_image == "ghcr.io/dellipse/job-squire:0.7.0"
    assert compose.read_image(install_dir) == "ghcr.io/dellipse/job-squire:0.7.0"
    assert not wrong_root.exists()  # never touched the wrong location

    lc.stop_instance("castelo", run=fake.run)
    state = lc.start_instance("castelo", run=fake.run, sleep=lambda _s: None)
    assert state["Status"] == "running"
