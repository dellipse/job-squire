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
"""Cross-platform instance registry.

Pins config_dir() to its Linux/XDG branch the same way test_runtime.py and
test_query_config.py do, since registry.py reuses that exact helper for
its per-user config location.
"""
import json

import pytest

from job_squire_cli.ops import registry as reg
from job_squire_cli.query import config as query_config_module


@pytest.fixture(autouse=True)
def force_linux_config_dir(monkeypatch):
    monkeypatch.setattr(query_config_module.platform, "system", lambda: "Linux")


@pytest.fixture(autouse=True)
def tmp_registry(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return tmp_path / "job-squire" / reg.REGISTRY_FILENAME


# ── Slug sanitization ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Castelo", "castelo"),
        ("  My Instance  ", "my-instance"),
        ("some_name_here", "some-name-here"),
        ("Weird!!Chars??", "weirdchars"),
        ("a---b", "a-b"),
        ("-leading-and-trailing-", "leading-and-trailing"),
        ("Ünïcödé", "ncd"),
    ],
)
def test_sanitize_slug(raw, expected):
    assert reg.sanitize_slug(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "!!!", "___", "---"])
def test_sanitize_slug_rejects_names_with_nothing_usable(raw):
    with pytest.raises(reg.InvalidNameError):
        reg.sanitize_slug(raw)


# ── Derived values ────────────────────────────────────────────────────────


def test_derived_values_are_deterministic_and_unique_per_name():
    castelo_cookie = reg.derive_cookie_name("castelo")
    castelo_project = reg.derive_compose_project("castelo")
    dan_cookie = reg.derive_cookie_name("dan")
    dan_project = reg.derive_compose_project("dan")

    # Deterministic: same input always yields the same output.
    assert reg.derive_cookie_name("castelo") == castelo_cookie == "castelo_session"
    assert reg.derive_compose_project("castelo") == castelo_project == "job-squire-castelo"

    # Unique: two different instance names never collide on derived values.
    assert castelo_cookie != dan_cookie
    assert castelo_project != dan_project


def test_add_instance_creates_two_fake_records_with_unique_derived_values():
    castelo = reg.add_instance(
        name="Castelo",
        mode="local",
        runtime="podman",
        data_dir="/data/castelo",
        public_url="http://localhost:8000",
        app_port=8000,
        mcp_port=9000,
    )
    dan = reg.add_instance(
        name="Dan",
        mode="local",
        runtime="podman",
        data_dir="/data/dan",
        public_url="http://localhost:8001",
        app_port=8001,
        mcp_port=9001,
    )

    assert castelo.name == "castelo"
    assert castelo.cookie_name == "castelo_session"
    assert reg.derive_compose_project(castelo.name) == "job-squire-castelo"

    assert dan.name == "dan"
    assert dan.cookie_name == "dan_session"
    assert reg.derive_compose_project(dan.name) == "job-squire-dan"

    assert castelo.cookie_name != dan.cookie_name
    assert reg.derive_compose_project(castelo.name) != reg.derive_compose_project(dan.name)


# ── Collision rejection ──────────────────────────────────────────────────


def test_add_instance_rejects_exact_name_collision():
    reg.add_instance(
        name="castelo", mode="local", runtime="podman", data_dir="/data/castelo",
        public_url="http://localhost:8000",
    )
    with pytest.raises(reg.NameCollisionError):
        reg.add_instance(
            name="castelo", mode="local", runtime="podman", data_dir="/data/other",
            public_url="http://localhost:8001",
        )


def test_add_instance_rejects_collision_after_sanitizing():
    reg.add_instance(
        name="castelo", mode="local", runtime="podman", data_dir="/data/castelo",
        public_url="http://localhost:8000",
    )
    with pytest.raises(reg.NameCollisionError):
        reg.add_instance(
            name="  Castelo  ", mode="local", runtime="podman", data_dir="/data/other",
            public_url="http://localhost:8001",
        )


# ── Round-trip read/write ─────────────────────────────────────────────────


def test_round_trip_read_write(tmp_registry):
    reg.add_instance(
        name="castelo", mode="local", runtime="podman", data_dir="/data/castelo",
        public_url="http://localhost:8000", app_port=8000, mcp_port=9000,
    )
    reg.add_instance(
        name="dan", mode="network", runtime="docker", data_dir="/data/dan",
        public_url="https://dan.example.com",
    )

    on_disk = json.loads(tmp_registry.read_text())
    assert on_disk["version"] == reg.REGISTRY_VERSION
    assert {row["name"] for row in on_disk["instances"]} == {"castelo", "dan"}

    reloaded = reg.list_instances()
    assert {i.name for i in reloaded} == {"castelo", "dan"}
    castelo = reg.get_instance("castelo")
    assert castelo.app_port == 8000
    assert castelo.mcp_port == 9000
    assert castelo.mode == "local"


def test_load_registry_with_no_file_returns_empty_shape():
    data = reg.load_registry()
    assert data == {"version": reg.REGISTRY_VERSION, "instances": []}


# ── No secret ever serialized ────────────────────────────────────────────


def test_add_instance_has_no_parameter_for_any_secret():
    with pytest.raises(TypeError):
        reg.add_instance(
            name="castelo", mode="local", runtime="podman", data_dir="/data/castelo",
            public_url="http://localhost:8000", secret_key="super-secret",  # type: ignore[call-arg]
        )


def test_update_instance_rejects_unknown_field_such_as_a_secret():
    reg.add_instance(
        name="castelo", mode="local", runtime="podman", data_dir="/data/castelo",
        public_url="http://localhost:8000",
    )
    with pytest.raises(reg.RegistryError):
        reg.update_instance("castelo", secret_key="super-secret")


def test_registry_file_on_disk_never_contains_a_secret_looking_field(tmp_registry):
    reg.add_instance(
        name="castelo", mode="local", runtime="podman", data_dir="/data/castelo",
        public_url="http://localhost:8000", app_port=8000, mcp_port=9000,
    )
    raw = tmp_registry.read_text()
    for forbidden in ("secret", "SECRET_KEY", "api_key", "password", "token"):
        assert forbidden not in raw


# ── Update / remove ───────────────────────────────────────────────────────


def test_update_instance_changes_allowed_fields():
    reg.add_instance(
        name="castelo", mode="local", runtime="podman", data_dir="/data/castelo",
        public_url="http://localhost:8000", app_port=8000, mcp_port=9000,
    )
    updated = reg.update_instance("castelo", app_port=8010, mcp_port=9010)
    assert updated.app_port == 8010
    assert updated.mcp_port == 9010
    assert reg.get_instance("castelo").app_port == 8010


def test_update_instance_unregistered_name_raises():
    with pytest.raises(reg.RegistryError):
        reg.update_instance("nonexistent", app_port=8000)


def test_remove_instance():
    reg.add_instance(
        name="castelo", mode="local", runtime="podman", data_dir="/data/castelo",
        public_url="http://localhost:8000",
    )
    assert reg.remove_instance("castelo") is True
    assert reg.get_instance("castelo") is None
    assert reg.remove_instance("castelo") is False


# ── Divergence check and reconcile ────────────────────────────────────────


def test_check_divergence_reports_renamed_container():
    instance = reg.add_instance(
        name="castelo", mode="local", runtime="podman", data_dir="/data/castelo",
        public_url="http://localhost:8000", app_port=8000, mcp_port=9000,
    )
    observed = reg.ObservedState(container_running=True, container_name="something-else")
    drifts = reg.check_divergence(instance, observed)
    assert any(d.field == "container_name" for d in drifts)


def test_check_divergence_reports_changed_ports():
    instance = reg.add_instance(
        name="castelo", mode="local", runtime="podman", data_dir="/data/castelo",
        public_url="http://localhost:8000", app_port=8000, mcp_port=9000,
    )
    observed = reg.ObservedState(
        container_running=True,
        container_name="job-squire-castelo",
        app_port=8123,
        mcp_port=9000,
    )
    drifts = reg.check_divergence(instance, observed)
    assert len(drifts) == 1
    assert drifts[0].field == "app_port"
    assert drifts[0].expected == 8000
    assert drifts[0].actual == 8123


def test_check_divergence_reports_deleted_volume():
    instance = reg.add_instance(
        name="castelo", mode="local", runtime="podman", data_dir="/data/castelo",
        public_url="http://localhost:8000", app_port=8000, mcp_port=9000,
    )
    observed = reg.ObservedState(
        container_running=True,
        container_name="job-squire-castelo",
        data_dir_exists=False,
    )
    drifts = reg.check_divergence(instance, observed)
    assert any(d.field == "data_dir" for d in drifts)


def test_check_divergence_reports_missing_container():
    instance = reg.add_instance(
        name="castelo", mode="local", runtime="podman", data_dir="/data/castelo",
        public_url="http://localhost:8000",
    )
    observed = reg.ObservedState(container_running=False)
    drifts = reg.check_divergence(instance, observed)
    assert any(d.field == "container" and d.actual is None for d in drifts)


def test_check_divergence_reports_nothing_when_everything_matches():
    instance = reg.add_instance(
        name="castelo", mode="local", runtime="podman", data_dir="/data/castelo",
        public_url="http://localhost:8000", app_port=8000, mcp_port=9000,
    )
    observed = reg.ObservedState(
        container_running=True,
        container_name="job-squire-castelo",
        app_port=8000,
        mcp_port=9000,
        data_dir_exists=True,
    )
    assert reg.check_divergence(instance, observed) == []


def test_reconcile_instance_syncs_port_drift():
    reg.add_instance(
        name="castelo", mode="local", runtime="podman", data_dir="/data/castelo",
        public_url="http://localhost:8000", app_port=8000, mcp_port=9000,
    )
    observed = reg.ObservedState(container_running=True, app_port=8123, mcp_port=9123)
    reconciled = reg.reconcile_instance("castelo", observed)
    assert reconciled.app_port == 8123
    assert reconciled.mcp_port == 9123


def test_reconcile_instance_with_nothing_to_sync_raises():
    reg.add_instance(
        name="castelo", mode="local", runtime="podman", data_dir="/data/castelo",
        public_url="http://localhost:8000", app_port=8000, mcp_port=9000,
    )
    observed = reg.ObservedState(container_running=True)
    with pytest.raises(reg.RegistryError):
        reg.reconcile_instance("castelo", observed)
