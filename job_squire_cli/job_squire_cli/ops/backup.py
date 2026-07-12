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
"""Instance backup and restore (Prompt C8, docs/PLAN-deployment-modes.md
Section 7 "Backup and restore").

`backup` produces one self-contained, mandatory-encrypted archive per
instance: the entire instance directory (docs/../ops/paths.py -- not just
`data/`, the whole thing, since that's the one directory that is the whole
instance) plus a `backup-manifest.json` describing it, sealed with
ops/backup_crypto.py's Argon2id + AES-256-GCM. `restore` reverses that:
decrypt, verify, unpack, re-register, and bring the instance back up.

The SQLite database is captured through the same WAL-safe snapshot
mechanism app/backup.py already uses for the in-app one-click download
(SQLite's own Online Backup API, `Connection.backup()`, followed by an
integrity check) -- safe to run against a live, concurrently-written
database, so `create_backup` never needs to stop the instance's container.
Everything else in the directory is added verbatim; the WAL/SHM/journal
sidecar files next to the live database are skipped since the snapshot
already captures their effect.

This module never imports the app package (same constraint as every other
ops module -- see ops/crypto_mirror.py's docstring): the schema/migration
point recorded in the manifest is a fingerprint of the live schema
(`sqlite_master.sql`) taken directly with the stdlib `sqlite3` module, not
a version number read from app code.
"""
from __future__ import annotations

import hashlib
import io
import json
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import time
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .. import __version__ as _cli_version
from . import backup_crypto, compose, dotenv, paths, ports
from . import lifecycle as lc
from .paths import instance_root
from .registry import (
    Instance,
    NameCollisionError,
    add_instance,
    derive_compose_project,
    derive_cookie_name,
    get_instance,
    list_instances,
    sanitize_slug,
)
from .registry import remove_instance as _registry_remove

Runner = lc.Runner
Which = lc.Which
Confirm = lc.Confirm
Sleep = lc.Sleep

BACKUP_FORMAT_VERSION = 1
MANIFEST_FILENAME = "backup-manifest.json"
_INSTANCE_PREFIX = "instance/"

_FORMAT_TAR = backup_crypto.CONTAINER_TAR_GZ
_FORMAT_ZIP = backup_crypto.CONTAINER_ZIP
_EXT_TO_FORMAT = {"tgz": _FORMAT_TAR, "zip": _FORMAT_ZIP}


class BackupError(lc.LifecycleError):
    """Raised for any backup-creation failure."""


class RestoreError(lc.LifecycleError):
    """Raised for any restore failure that isn't a wrong passphrase."""


class WrongPassphraseError(RestoreError):
    """The supplied passphrase could not decrypt the archive."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_stamp_minutes() -> str:
    """`YYYYMMDDTHHMMZ` -- matches the plan's own example filename
    (`job-squire-castelo-20260711T1830Z.tgz`), minute resolution, not the
    seconds resolution lifecycle._utc_stamp() uses for adopt's env backups."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%MZ")


def default_backup_dir() -> Path:
    """The user's home folder (PLAN Section 7: "written to the user's home
    folder")."""
    return Path.home()


def backup_filename(name: str, *, ext: str = "tgz", timestamp: str | None = None) -> str:
    return f"job-squire-{name}-{timestamp or _utc_stamp_minutes()}.{ext}"


# ── WAL-safe database snapshot (mirrors app/backup.py's _snapshot_db) ────


def _snapshot_sqlite_db(db_path: Path, dest_path: Path) -> None:
    src = sqlite3.connect(str(db_path))
    dst = sqlite3.connect(str(dest_path))
    try:
        with dst:
            src.backup(dst)
    finally:
        src.close()
        dst.close()

    conn = sqlite3.connect(str(dest_path))
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()
    if result != "ok":
        raise BackupError(f"Database snapshot of {db_path} failed integrity check: {result}")


def _schema_fingerprint(db_path: Path) -> str | None:
    """sha256 of the live schema (every non-null `sqlite_master.sql`,
    sorted by name) -- a stand-in for a migration/schema version number,
    since this package deliberately doesn't import app code to read one."""
    if not db_path.exists():
        return None
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name").fetchall()
    finally:
        conn.close()
    blob = "\n".join(row[0] for row in rows).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ── Building the archive payload (Prompt C8 step 1-2) ────────────────────


def _write_tar_gz(entries: list[tuple[str, Path]], manifest_bytes: bytes) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for arcname, path in entries:
            tar.add(path, arcname=_INSTANCE_PREFIX + arcname)
        info = tarfile.TarInfo(MANIFEST_FILENAME)
        info.size = len(manifest_bytes)
        info.mtime = int(time.time())
        tar.addfile(info, io.BytesIO(manifest_bytes))
    return buf.getvalue()


def _write_zip(entries: list[tuple[str, Path]], manifest_bytes: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for arcname, path in entries:
            zf.write(path, arcname=_INSTANCE_PREFIX + arcname)
        zf.writestr(MANIFEST_FILENAME, manifest_bytes)
    return buf.getvalue()


def _gather_and_build(
    root: Path, instance: Instance, *, image: str, container_format: int,
) -> tuple[bytes, dict]:
    """Snapshot the database, walk the rest of the instance directory
    verbatim, and build the (unencrypted) tar.gz or zip payload plus its
    manifest dict. The WAL/SHM/journal sidecars next to the live database
    are skipped -- the snapshot already captures a consistent point-in-time
    copy of what they represent."""
    with tempfile.TemporaryDirectory(prefix="job-squire-backup-") as work:
        work_path = Path(work)
        entries: list[tuple[str, Path]] = []
        checksums: dict[str, str] = {}
        db_path = paths.sqlite_db_path(root)
        schema_fingerprint = None
        skip = {db_path.with_name(db_path.name + suffix) for suffix in ("-wal", "-shm", "-journal")}

        if db_path.exists():
            snapshot_path = work_path / paths.DB_FILENAME
            _snapshot_sqlite_db(db_path, snapshot_path)
            schema_fingerprint = _schema_fingerprint(snapshot_path)
            rel = db_path.relative_to(root).as_posix()
            entries.append((rel, snapshot_path))
            checksums[rel] = _sha256_file(snapshot_path)
            skip.add(db_path)

        for path in sorted(root.rglob("*")):
            if path.is_dir() or path in skip:
                continue
            rel = path.relative_to(root).as_posix()
            entries.append((rel, path))
            checksums[rel] = _sha256_file(path)

        manifest = {
            "backup_format_version": BACKUP_FORMAT_VERSION,
            "created_at": _now_iso(),
            "instance": asdict(instance),
            "image": image,
            "schema_fingerprint": schema_fingerprint,
            "cli_version": _cli_version,
            "checksums": checksums,
        }
        manifest_bytes = json.dumps(manifest, indent=2).encode("utf-8")
        if container_format == _FORMAT_TAR:
            payload = _write_tar_gz(entries, manifest_bytes)
        else:
            payload = _write_zip(entries, manifest_bytes)
        return payload, manifest


# ── backup ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BackupResult:
    instance_name: str
    archive_path: Path
    manifest: dict


def create_backup(
    instance: Instance,
    *,
    data_root: Path | None = None,
    dest_dir: Path | None = None,
    passphrase: str,
    ext: str = "tgz",
    argon2_time_cost: int = backup_crypto.DEFAULT_TIME_COST,
    argon2_memory_cost_kib: int = backup_crypto.DEFAULT_MEMORY_COST_KIB,
    argon2_lanes: int = backup_crypto.DEFAULT_LANES,
) -> BackupResult:
    """Write one encrypted archive for `instance` into `dest_dir` (default:
    the user's home folder). Never writes an unencrypted archive to disk --
    the plaintext payload only ever exists in memory."""
    if ext not in _EXT_TO_FORMAT:
        raise BackupError(f"Unsupported backup format {ext!r} -- expected 'tgz' or 'zip'.")
    root = instance_root(instance.name, data_root) if data_root is not None else Path(instance.data_dir)
    if not root.exists():
        raise BackupError(f"No data directory found for {instance.name!r} at {root}.")

    image = compose.read_image(root) if paths.compose_path(root).exists() else "unknown"
    container_format = _EXT_TO_FORMAT[ext]
    payload, manifest = _gather_and_build(root, instance, image=image, container_format=container_format)

    try:
        sealed = backup_crypto.seal(
            payload, passphrase, container_format=container_format,
            time_cost=argon2_time_cost, memory_cost_kib=argon2_memory_cost_kib, lanes=argon2_lanes,
        )
    except backup_crypto.BackupCryptoError as exc:
        raise BackupError(str(exc)) from exc

    dest_dir = Path(dest_dir) if dest_dir is not None else default_backup_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    archive_path = dest_dir / backup_filename(instance.name, ext=ext)
    archive_path.write_bytes(sealed)
    try:
        archive_path.chmod(0o600)  # contains the instance's SECRET_KEY, even though it's encrypted
    except OSError:
        pass

    return BackupResult(instance_name=instance.name, archive_path=archive_path, manifest=manifest)


def create_all_backups(
    *,
    dest_dir: Path | None = None,
    passphrase: str,
    ext: str = "tgz",
    data_root: Path | None = None,
    argon2_time_cost: int = backup_crypto.DEFAULT_TIME_COST,
    argon2_memory_cost_kib: int = backup_crypto.DEFAULT_MEMORY_COST_KIB,
    argon2_lanes: int = backup_crypto.DEFAULT_LANES,
) -> list[BackupResult]:
    """One archive per registered instance (PLAN Section 7: "an option can
    back up every registered instance in one run")."""
    return [
        create_backup(
            instance, data_root=data_root, dest_dir=dest_dir, passphrase=passphrase, ext=ext,
            argon2_time_cost=argon2_time_cost, argon2_memory_cost_kib=argon2_memory_cost_kib,
            argon2_lanes=argon2_lanes,
        )
        for instance in list_instances()
    ]


# ── restore, phase 1: open and verify ────────────────────────────────────


def _iter_payload_members(payload: bytes, container_format: int):
    """Yield (path-relative-to-instance-root, bytes) for every file stored
    under the archive's `instance/` prefix, skipping the manifest itself."""
    if container_format == _FORMAT_TAR:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
            for member in tar.getmembers():
                if not member.isfile() or not member.name.startswith(_INSTANCE_PREFIX):
                    continue
                rel = member.name[len(_INSTANCE_PREFIX) :]
                if rel:
                    yield rel, tar.extractfile(member).read()
    else:
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            for info in zf.infolist():
                if info.is_dir() or not info.filename.startswith(_INSTANCE_PREFIX):
                    continue
                rel = info.filename[len(_INSTANCE_PREFIX) :]
                if rel:
                    yield rel, zf.read(info)


def _read_manifest(payload: bytes, container_format: int) -> dict:
    if container_format == _FORMAT_TAR:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
            try:
                member = tar.getmember(MANIFEST_FILENAME)
            except KeyError as exc:
                raise RestoreError(f"Archive is missing {MANIFEST_FILENAME} -- not a valid backup.") from exc
            return json.loads(tar.extractfile(member).read())
    else:
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            try:
                return json.loads(zf.read(MANIFEST_FILENAME))
            except KeyError as exc:
                raise RestoreError(f"Archive is missing {MANIFEST_FILENAME} -- not a valid backup.") from exc


def _verify_checksums(payload: bytes, container_format: int, checksums: dict[str, str]) -> None:
    """Verify every file's checksum *before* anything is written to disk
    (PLAN Section 7's restore ordering: decrypt, then verify, then unpack),
    so a corrupted archive is caught before it touches the target
    directory rather than partway through extraction."""
    seen = set()
    for rel, data in _iter_payload_members(payload, container_format):
        seen.add(rel)
        expected = checksums.get(rel)
        if expected is None:
            continue  # tolerate an extra file not recorded in the manifest
        actual = hashlib.sha256(data).hexdigest()
        if actual != expected:
            raise RestoreError(f"Checksum mismatch for {rel!r} in the archive -- it may be corrupted.")
    missing = sorted(set(checksums) - seen)
    if missing:
        raise RestoreError(
            f"Archive is missing {len(missing)} file(s) recorded in its manifest: {missing[:5]}"
            + (" ..." if len(missing) > 5 else "")
        )


@dataclass(frozen=True)
class OpenedBackup:
    archive_path: Path
    payload: bytes
    container_format: int
    manifest: dict

    @property
    def instance_name(self) -> str:
        return self.manifest["instance"]["name"]

    @property
    def checksums(self) -> dict[str, str]:
        return self.manifest.get("checksums", {})


def open_backup(archive_path: Path, passphrase: str) -> OpenedBackup:
    """Decrypt `archive_path`, parse its manifest, check format
    compatibility, and verify every file's checksum -- everything PLAN
    Section 7's restore does before it unpacks anything. Raises
    WrongPassphraseError on a bad passphrase (or a corrupted/tampered
    archive -- AES-GCM cannot tell those apart) and RestoreError for
    anything else that makes the archive unusable.
    """
    archive_path = Path(archive_path)
    try:
        sealed = archive_path.read_bytes()
    except OSError as exc:
        raise RestoreError(f"Cannot read backup archive at {archive_path}: {exc}") from exc

    try:
        opened = backup_crypto.open_sealed(sealed, passphrase)
    except backup_crypto.WrongPassphraseError as exc:
        raise WrongPassphraseError(str(exc)) from exc
    except backup_crypto.BackupCryptoError as exc:
        raise RestoreError(str(exc)) from exc

    manifest = _read_manifest(opened.plaintext, opened.container_format)
    version = manifest.get("backup_format_version")
    if version != BACKUP_FORMAT_VERSION:
        raise RestoreError(
            f"Backup archive format version {version!r} is not supported by this CLI "
            f"(supports {BACKUP_FORMAT_VERSION}). Restore it with a matching job-squire-cli version."
        )
    _verify_checksums(opened.plaintext, opened.container_format, manifest.get("checksums", {}))

    return OpenedBackup(
        archive_path=archive_path, payload=opened.plaintext,
        container_format=opened.container_format, manifest=manifest,
    )


# ── restore, phase 2: unpack, register, bring up ─────────────────────────


def _safe_member_path(dest_root: Path, arcname: str) -> Path:
    """Reject a path that would escape `dest_root` (zip-slip defense) --
    cheap insurance against a corrupted or maliciously crafted archive,
    even though only someone holding the correct passphrase could produce
    one that decrypts at all."""
    resolved_root = dest_root.resolve()
    candidate = (dest_root / arcname).resolve()
    if candidate != resolved_root and resolved_root not in candidate.parents:
        raise RestoreError(f"Archive contains an unsafe path: {arcname!r}")
    return candidate


def _extract_payload(payload: bytes, container_format: int, dest_root: Path) -> None:
    dest_root.mkdir(parents=True, exist_ok=True)
    if container_format == _FORMAT_TAR:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
            for member in tar.getmembers():
                if not member.isfile() or not member.name.startswith(_INSTANCE_PREFIX):
                    continue
                rel = member.name[len(_INSTANCE_PREFIX) :]
                if not rel:
                    continue
                target = _safe_member_path(dest_root, rel)
                target.parent.mkdir(parents=True, exist_ok=True)
                with open(target, "wb") as out:
                    out.write(tar.extractfile(member).read())
    else:
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            for info in zf.infolist():
                if info.is_dir() or not info.filename.startswith(_INSTANCE_PREFIX):
                    continue
                rel = info.filename[len(_INSTANCE_PREFIX) :]
                if not rel:
                    continue
                target = _safe_member_path(dest_root, rel)
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as out:
                    out.write(src.read())


@dataclass(frozen=True)
class RestoreResult:
    instance: Instance
    data_dir: Path
    health: dict | None = None


def restore_instance(
    opened: OpenedBackup,
    *,
    target_name: str | None = None,
    overwrite: bool = False,
    data_root: Path | None = None,
    image: str | None = None,
    bring_up: bool = True,
    run: Runner = subprocess.run,
    which: Which = shutil.which,
    sleep: Sleep = time.sleep,
    confirm: Confirm = lambda _msg: True,
    prefer_orbstack: bool = False,
    prefer_docker_desktop: bool = False,
) -> RestoreResult:
    """Recreate an instance from an already-opened backup (see
    `open_backup`). If an instance of the target name is already
    registered, pass either `target_name` (a different name -- the caller,
    typically the click layer, resolves the rename interactively) or
    `overwrite=True`; leaving both unset/False raises NameCollisionError
    rather than clobbering silently (PLAN Section 7).
    """
    manifest_instance: dict = opened.manifest["instance"]
    slug = sanitize_slug(target_name or manifest_instance["name"])
    renamed = slug != manifest_instance["name"]

    existing = get_instance(slug)
    if existing is not None and not overwrite:
        raise NameCollisionError(
            f"An instance named {slug!r} is already registered. Restore with a different "
            f"target_name, or overwrite=True to replace it."
        )

    root = instance_root(slug, data_root)
    if existing is not None:
        old_root = Path(existing.data_dir)
        compose.compose_down(existing.runtime, old_root, derive_compose_project(existing.name), run=run)
        _registry_remove(existing.name)
        if old_root != root:
            shutil.rmtree(old_root, ignore_errors=True)
    if root.exists():
        shutil.rmtree(root)

    _extract_payload(opened.payload, opened.container_format, root)
    try:
        paths.data_env_path(root).chmod(0o600)  # holds SECRET_KEY/ADMIN_PASSWORD; matches create's own chmod
    except OSError:
        pass

    mode = manifest_instance.get("mode", "local")
    registered = [i for i in list_instances() if i.name != slug]
    app_port = manifest_instance.get("app_port")
    mcp_port = manifest_instance.get("mcp_port")
    ports_changed = False
    if mode == "local":
        used_app = {i.app_port for i in registered if i.app_port is not None}
        used_mcp = {i.mcp_port for i in registered if i.mcp_port is not None}
        collides = (
            app_port is None or mcp_port is None
            or app_port in used_app or mcp_port in used_mcp
            or not ports.default_port_free(app_port) or not ports.default_port_free(mcp_port)
        )
        if collides:
            app_port, mcp_port = ports.allocate_port_pair(registered)
            ports_changed = True

    chosen_runtime = lc.runtime_mod.ensure_runtime(
        confirm=confirm, prefer_orbstack=prefer_orbstack, prefer_docker_desktop=prefer_docker_desktop,
        run=run, which=which,
    )

    if renamed or ports_changed:
        compose_env = dotenv.parse(paths.compose_env_path(root))
        compose.write_compose_files(
            root, container_name=derive_compose_project(slug), image=(image or compose.read_image(root)),
            loopback_only=(mode == "local"), app_port=app_port, mcp_port=mcp_port,
            puid=int(compose_env.get("PUID", 1000)), pgid=int(compose_env.get("PGID", 1000)),
            umask=compose_env.get("UMASK", "022"), data_host_dir=compose_env.get("DATA_HOST_DIR", "./data"),
        )
    elif image is not None:
        compose.write_image(root, image)

    if renamed:
        cookie_name = derive_cookie_name(slug)
        dotenv.set_line(paths.data_env_path(root), "INSTANCE_NAME", slug)
        dotenv.set_line(paths.data_env_path(root), "SESSION_COOKIE_NAME", cookie_name)
    else:
        cookie_name = manifest_instance.get("cookie_name") or derive_cookie_name(slug)

    if mode == "local":
        public_url = f"http://localhost:{app_port}"
    else:
        public_url = manifest_instance.get("public_url", "")

    instance = add_instance(
        name=slug, mode=mode, runtime=chosen_runtime, data_dir=str(root), public_url=public_url,
        app_port=app_port, mcp_port=mcp_port, cookie_name=cookie_name, created=manifest_instance.get("created"),
    )

    health = None
    if bring_up:
        container_name = derive_compose_project(slug)
        up_result = compose.compose_up(chosen_runtime, root, container_name, run=run)
        if up_result.returncode != 0:
            lc._raise_for_failed_state(chosen_runtime, container_name, None, run=run)
        health = lc.wait_for_state(chosen_runtime, container_name, run=run, sleep=sleep)
        if health is not None and health.get("Status") == "exited":
            lc._raise_for_failed_state(chosen_runtime, container_name, health, run=run)

    return RestoreResult(instance=instance, data_dir=root, health=health)
