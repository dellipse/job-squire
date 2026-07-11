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
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from job_squire_cli.ops import lifecycle as lc, paths
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
        self.calls: list[tuple] = []

    def run(self, args, **kwargs):
        args = list(args)
        self.calls.append(tuple(args))

        if args[:2] in (["docker", "info"], ["podman", "info"]):
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        if len(args) >= 2 and args[1] == "compose":
            project = args[args.index("-p") + 1]
            cwd = kwargs.get("cwd")
            if args[-2:] == ["up", "-d"]:
                self._on_up(project, cwd)
            elif args[-1] == "stop":
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
