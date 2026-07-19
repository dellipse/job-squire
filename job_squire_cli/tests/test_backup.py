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
"""Instance backup and restore.

Reuses test_lifecycle.py's FakeRuntime and fixtures (same rationale as
test_lifecycle.py importing from test_secrets_copy.py: one FakeRuntime,
not a second copy of it) so `create_instance` here produces a real
instance directory -- real compose/env files, a real sqlite database via
FakeRuntime's schema seeding on `up` -- for `create_backup`/`restore_
instance` to operate on for real. Only the container runtime itself is
fake; the encryption, tar/zip building, checksum verification, and file
I/O in ops/backup.py all run for real against tmp_path directories.

Argon2id parameters are cheap (time_cost=1, memory_cost=8 MiB, lanes=1)
throughout, purely for test speed -- test_backup_crypto.py is where the
crypto primitives themselves are exercised in isolation.
"""
import sqlite3

import pytest

from job_squire_cli.ops import backup as bk
from job_squire_cli.ops import lifecycle as lc
from job_squire_cli.ops import paths
from job_squire_cli.ops import registry as reg

from tests.test_lifecycle import (  # noqa: F401 -- autouse fixtures re-exposed by import
    FakeRuntime, create_kwargs, force_linux_config_dir, tmp_registry,
)

_CHEAP_ARGON2 = dict(argon2_time_cost=1, argon2_memory_cost_kib=8192, argon2_lanes=1)


@pytest.fixture
def fake():
    return FakeRuntime()


@pytest.fixture
def data_root(tmp_path):
    return tmp_path / "instances"


def _make_instance(fake_runtime, data_root, name="castelo", **create_kw):
    lc.create_instance(name=name, mode="local", data_root=data_root, **create_kwargs(fake_runtime, **create_kw))
    return reg.get_instance(name)


# ── create_backup ────────────────────────────────────────────────────────


def test_create_backup_writes_encrypted_archive_with_restrictive_permissions(fake, data_root, tmp_path):
    instance = _make_instance(fake, data_root)
    dest_dir = tmp_path / "backups"

    result = bk.create_backup(instance, dest_dir=dest_dir, passphrase="s3cr3t!", run=fake.run, **_CHEAP_ARGON2)

    assert result.archive_path.exists()
    assert result.archive_path.parent == dest_dir
    import re

    assert re.fullmatch(r"job-squire-castelo-\d{8}T\d{4}Z\.tgz", result.archive_path.name)

    raw = result.archive_path.read_bytes()
    assert not raw.startswith(b"\x1f\x8b")  # never a plaintext gzip -- always sealed first
    assert raw[:4] == b"JSQB"

    import stat

    mode = stat.S_IMODE(result.archive_path.stat().st_mode)
    assert mode == 0o600

    assert result.manifest["instance"]["name"] == "castelo"
    assert result.manifest["backup_format_version"] == bk.BACKUP_FORMAT_VERSION
    assert "data/job-squire.db" in result.manifest["checksums"]
    assert result.manifest["schema_fingerprint"]  # a real schema was captured
    assert result.manifest["image"]


def test_create_backup_zip_format_round_trips(fake, data_root, tmp_path):
    instance = _make_instance(fake, data_root)
    result = bk.create_backup(
        instance, dest_dir=tmp_path / "backups", passphrase="pw", ext="zip", run=fake.run, **_CHEAP_ARGON2
    )
    assert result.archive_path.suffix == ".zip"
    opened = bk.open_backup(result.archive_path, "pw")
    assert opened.container_format == bk._FORMAT_ZIP
    assert opened.instance_name == "castelo"


def test_create_backup_no_data_directory_raises(tmp_path):
    instance = reg.add_instance(
        name="ghost", mode="local", runtime="docker", data_dir=str(tmp_path / "nowhere"),
        public_url="http://localhost:8080", app_port=8080, mcp_port=9000,
    )
    with pytest.raises(bk.BackupError, match="No data directory"):
        bk.create_backup(instance, dest_dir=tmp_path, passphrase="pw", **_CHEAP_ARGON2)


def test_create_all_backups_writes_one_archive_per_instance(fake, data_root, tmp_path):
    _make_instance(fake, data_root, name="one")
    _make_instance(fake, data_root, name="two")

    results = bk.create_all_backups(dest_dir=tmp_path / "backups", passphrase="pw", run=fake.run, **_CHEAP_ARGON2)

    assert sorted(r.instance_name for r in results) == ["one", "two"]
    assert len({r.archive_path for r in results}) == 2
    assert all(r.archive_path.exists() for r in results)


# ── open_backup ──────────────────────────────────────────────────────────


def test_open_backup_wrong_passphrase_fails_clearly(fake, data_root, tmp_path):
    instance = _make_instance(fake, data_root)
    result = bk.create_backup(instance, dest_dir=tmp_path, passphrase="correct-horse", run=fake.run, **_CHEAP_ARGON2)

    with pytest.raises(bk.WrongPassphraseError):
        bk.open_backup(result.archive_path, "wrong-horse")


def test_open_backup_verifies_checksums_and_reads_manifest(fake, data_root, tmp_path):
    instance = _make_instance(fake, data_root)
    result = bk.create_backup(instance, dest_dir=tmp_path, passphrase="pw", run=fake.run, **_CHEAP_ARGON2)

    opened = bk.open_backup(result.archive_path, "pw")

    assert opened.instance_name == "castelo"
    assert opened.checksums == result.manifest["checksums"]


# ── restore_instance: happy path on a "fresh machine" ────────────────────


def test_restore_round_trip_preserves_whole_directory_and_registers_instance(fake, data_root, tmp_path):
    instance = _make_instance(fake, data_root)
    root = paths.instance_root("castelo", data_root)

    # Files the app doesn't manage, at both the instance root and inside
    # data/ -- proving the whole directory is captured verbatim, not just
    # what compose.py itself writes.
    (root / "notes.txt").write_text("hand-written notes\n")
    (paths.data_dir(root) / "candidate_profile.md").write_text("# Candidate\n")

    result = bk.create_backup(
        instance, dest_dir=tmp_path / "backups", passphrase="s3cr3t!", run=fake.run, **_CHEAP_ARGON2
    )

    # Simulate restoring on a machine/registry that has never heard of
    # "castelo" -- the archive is the only thing that travels.
    reg.remove_instance("castelo")

    opened = bk.open_backup(result.archive_path, "s3cr3t!")
    new_root_base = tmp_path / "restored-instances"
    restore_result = bk.restore_instance(opened, data_root=new_root_base, **create_kwargs(fake))

    assert restore_result.instance.name == "castelo"
    assert restore_result.health is not None
    assert restore_result.health["Status"] == "running"

    restored_root = paths.instance_root("castelo", new_root_base)
    assert restore_result.data_dir == restored_root
    assert (restored_root / "notes.txt").read_text() == "hand-written notes\n"
    assert (paths.data_dir(restored_root) / "candidate_profile.md").read_text() == "# Candidate\n"
    assert paths.sqlite_db_path(restored_root).exists()

    registered = reg.get_instance("castelo")
    assert registered is not None
    assert registered.data_dir == str(restored_root)
    assert registered.cookie_name == "castelo_session"


def test_restore_preserves_stored_secret_key_so_encrypted_settings_still_decrypt(fake, data_root, tmp_path):
    """The archive's whole point is carrying SECRET_KEY along -- confirm
    data/.env's SECRET_KEY survives the round trip unchanged."""
    instance = _make_instance(fake, data_root)
    root = paths.instance_root("castelo", data_root)
    original_secret_key = [
        line.split("=", 1)[1] for line in paths.data_env_path(root).read_text().splitlines()
        if line.startswith("SECRET_KEY=")
    ][0]

    result = bk.create_backup(instance, dest_dir=tmp_path, passphrase="pw", run=fake.run, **_CHEAP_ARGON2)
    reg.remove_instance("castelo")
    opened = bk.open_backup(result.archive_path, "pw")
    restore_result = bk.restore_instance(opened, data_root=tmp_path / "restored", **create_kwargs(fake))

    restored_env = paths.data_env_path(restore_result.data_dir).read_text()
    restored_secret_key = [
        line.split("=", 1)[1] for line in restored_env.splitlines() if line.startswith("SECRET_KEY=")
    ][0]
    assert restored_secret_key == original_secret_key


# ── restore_instance: name collision handling ─────────────────────────────


def test_restore_collision_without_resolution_raises_before_touching_anything(fake, data_root, tmp_path):
    instance = _make_instance(fake, data_root)
    result = bk.create_backup(instance, dest_dir=tmp_path, passphrase="pw", run=fake.run, **_CHEAP_ARGON2)
    opened = bk.open_backup(result.archive_path, "pw")

    calls_before = len(fake.calls)
    with pytest.raises(reg.NameCollisionError):
        bk.restore_instance(opened, data_root=data_root, **create_kwargs(fake))
    assert len(fake.calls) == calls_before  # fails fast, before any runtime/compose call


def test_restore_with_rename_registers_under_new_name_and_original_untouched(fake, data_root, tmp_path):
    instance = _make_instance(fake, data_root)
    result = bk.create_backup(instance, dest_dir=tmp_path, passphrase="pw", run=fake.run, **_CHEAP_ARGON2)
    opened = bk.open_backup(result.archive_path, "pw")

    restore_result = bk.restore_instance(
        opened, target_name="castelo-2", data_root=data_root, **create_kwargs(fake)
    )

    assert restore_result.instance.name == "castelo-2"
    assert restore_result.instance.cookie_name == "castelo-2_session"
    assert reg.get_instance("castelo") is not None  # original left alone
    assert reg.get_instance("castelo-2") is not None

    restored_env = paths.data_env_path(restore_result.data_dir).read_text()
    assert "INSTANCE_NAME=castelo-2" in restored_env
    assert "SESSION_COOKIE_NAME=castelo-2_session" in restored_env
    compose_yaml = paths.compose_path(restore_result.data_dir).read_text()
    assert "container_name: job-squire-castelo-2" in compose_yaml


def test_restore_with_overwrite_replaces_existing_instance_cleanly(fake, data_root, tmp_path):
    instance = _make_instance(fake, data_root)
    result = bk.create_backup(instance, dest_dir=tmp_path, passphrase="pw", run=fake.run, **_CHEAP_ARGON2)

    # Mutate the *live* instance after the backup was taken, so overwrite
    # restoring from the (now older) archive should wipe this away.
    root = paths.instance_root("castelo", data_root)
    (root / "written-after-backup.txt").write_text("should not survive an overwrite restore\n")

    opened = bk.open_backup(result.archive_path, "pw")
    restore_result = bk.restore_instance(
        opened, overwrite=True, data_root=data_root, **create_kwargs(fake)
    )

    assert restore_result.instance.name == "castelo"
    assert not (restore_result.data_dir / "written-after-backup.txt").exists()
    assert len([i for i in reg.list_instances() if i.name == "castelo"]) == 1


# ── restore_instance: port reallocation ───────────────────────────────────


def test_restore_reallocates_ports_when_original_ports_are_taken(fake, data_root, tmp_path):
    instance = _make_instance(fake, data_root, name="one")
    original_app_port, original_mcp_port = instance.app_port, instance.mcp_port

    result = bk.create_backup(instance, dest_dir=tmp_path, passphrase="pw", run=fake.run, **_CHEAP_ARGON2)
    reg.remove_instance("one")  # simulate a fresh machine that never had "one"

    # But the target machine already has something else parked on those
    # exact ports -- restore must not collide with it.
    reg.add_instance(
        name="blocker", mode="local", runtime="docker", data_dir=str(tmp_path / "blocker"),
        public_url=f"http://localhost:{original_app_port}",
        app_port=original_app_port, mcp_port=original_mcp_port,
    )

    opened = bk.open_backup(result.archive_path, "pw")
    restore_result = bk.restore_instance(opened, data_root=data_root, **create_kwargs(fake))

    assert (restore_result.instance.app_port, restore_result.instance.mcp_port) != (
        original_app_port, original_mcp_port,
    )
    assert restore_result.instance.public_url == f"http://localhost:{restore_result.instance.app_port}"
    compose_env = paths.compose_env_path(restore_result.data_dir).read_text()
    assert f"APP_HOST_PORT={restore_result.instance.app_port}" in compose_env


# ── checksum verification (pure functions, no runtime needed) ────────────


def _bare_instance_root(tmp_path) -> "tuple":
    """A bare-bones instance directory plus a FakeRuntime standing in for
    its container -- `_gather_and_build` now always needs a running
    container to pull the data half of the archive from (see
    ops/backup.py's docstring), even for these otherwise-pure
    checksum/tamper tests."""
    root = tmp_path / "bare-instance"
    data_dir = root / "data"
    data_dir.mkdir(parents=True)
    db_path = paths.sqlite_db_path(root)
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()
    (root / "docker-compose.yml").write_text("image: ghcr.io/dellipse/job-squire:latest\n")
    (root / ".env").write_text("PUID=1000\n")
    paths.data_env_path(root).write_text("SECRET_KEY=abc\n")
    instance = reg.Instance(
        name="bare", mode="local", runtime="docker", data_dir=str(root), app_port=8080, mcp_port=9000,
        cookie_name="bare_session", public_url="http://localhost:8080", created="2026-07-11",
    )
    container_name = reg.derive_compose_project("bare")
    fake = FakeRuntime()
    fake.containers[container_name] = {"Status": "running", "Health": {"Status": "healthy"}}
    fake.data_dirs[container_name] = data_dir
    return root, instance, fake


def test_verify_checksums_passes_for_an_unmodified_payload(tmp_path):
    root, instance, fake = _bare_instance_root(tmp_path)
    payload, manifest = bk._gather_and_build(
        root, instance, image="img", container_format=bk._FORMAT_TAR, run=fake.run,
    )
    bk._verify_checksums(payload, bk._FORMAT_TAR, manifest["checksums"])  # does not raise


def test_verify_checksums_detects_a_mismatched_checksum(tmp_path):
    root, instance, fake = _bare_instance_root(tmp_path)
    payload, manifest = bk._gather_and_build(
        root, instance, image="img", container_format=bk._FORMAT_TAR, run=fake.run,
    )
    corrupted = dict(manifest["checksums"])
    key = next(iter(corrupted))
    corrupted[key] = "0" * 64
    with pytest.raises(bk.RestoreError, match="Checksum mismatch"):
        bk._verify_checksums(payload, bk._FORMAT_TAR, corrupted)


def test_verify_checksums_detects_a_missing_file(tmp_path):
    root, instance, fake = _bare_instance_root(tmp_path)
    payload, manifest = bk._gather_and_build(
        root, instance, image="img", container_format=bk._FORMAT_TAR, run=fake.run,
    )
    incomplete = dict(manifest["checksums"])
    incomplete["a-file-that-was-never-included.txt"] = "0" * 64
    with pytest.raises(bk.RestoreError, match="missing"):
        bk._verify_checksums(payload, bk._FORMAT_TAR, incomplete)


def test_open_backup_end_to_end_rejects_tampered_archive(tmp_path):
    root, instance, fake = _bare_instance_root(tmp_path)
    from job_squire_cli.ops import backup_crypto

    payload, manifest = bk._gather_and_build(
        root, instance, image="img", container_format=bk._FORMAT_TAR, run=fake.run,
    )
    sealed = backup_crypto.seal(payload, "pw", **{k[len("argon2_"):]: v for k, v in _CHEAP_ARGON2.items()})
    archive_path = tmp_path / "tampered.tgz"
    tampered = bytearray(sealed)
    tampered[-1] ^= 0xFF
    archive_path.write_bytes(bytes(tampered))

    with pytest.raises(bk.WrongPassphraseError):
        bk.open_backup(archive_path, "pw")
