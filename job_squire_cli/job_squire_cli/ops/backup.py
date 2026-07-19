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

`backup` produces one self-contained, mandatory-encrypted archive per
instance: the whole instance directory as it would look if it were still
all host-visible (docs/../ops/paths.py -- not just `data/`, the whole
thing, since that's the one directory that is the whole instance) plus a
`backup-manifest.json` describing it, sealed with ops/backup_crypto.py's
Argon2id + AES-256-GCM. `restore` reverses that: decrypt, verify, unpack,
re-register, and bring the instance back up.

Since /data is a named Docker volume, not a host bind mount (see
ops/compose.py's render_compose_yaml), the pieces that live there --
job-squire.db, uploads/, candidate_profile.md, and the other files
app/backup.py's `_SIDE_FILES` names -- are not a path this module can walk
on the host. `create_backup` instead runs `app/backup_cli.py` inside the
instance's own running container via `docker exec` (or `podman exec`) and
reads the resulting WAL-safe .tgz straight off the exec's stdout; that
inner archive's members are folded into the outer one under a `data/`
prefix, exactly matching the relative paths this module used to find by
walking the host directly. This means `create_backup` requires the
instance's container to be running (a live SQLite database is what makes
the Online Backup API snapshot safe in the first place, so this isn't a
new constraint, just a now-unavoidable one). `data/.env` is the one
exception: it's still a real host file (compose's `env_file:` has to read
it before the container or its volume exist at all), so it's still walked
directly like the rest of the instance root.

`restore_instance` mirrors this split: config files extract straight to
the host as before, but anything that belongs under the named volume is
staged to a temp directory and `docker cp`'d into the container's /data
after the container is created but before it's started -- `docker cp`
works against a stopped container's filesystem regardless of whether it's
backed by a bind mount or a named volume, and the image's own
`init-data-dir` s6 service (root/etc/s6-overlay/s6-rc.d/init-data-dir/run)
already recursively re-owns everything under /data to the `abc` account on
every boot, which is exactly what a volume populated by `docker cp` (as
root) needs before the app processes can write to it.

This module never imports the app package (same constraint as every other
ops module -- see ops/crypto_mirror.py's docstring): the schema/migration
point recorded in the manifest is a fingerprint of the live schema
(`sqlite_master.sql`), computed here from the snapshot bytes the container
handed back, not a version number read from app code.
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
    (`job-squire-castelo-20260711T1830Z.tgz`), minute resolution."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%MZ")


def default_backup_dir() -> Path:
    """Backups are written to the user's home folder by default."""
    return Path.home()


def backup_filename(name: str, *, ext: str = "tgz", timestamp: str | None = None) -> str:
    return f"job-squire-{name}-{timestamp or _utc_stamp_minutes()}.{ext}"


# ── Pulling the named volume's contents through the container ───────────

_BACKUP_CLI_ARGV = ["python3", "-m", "app.backup_cli"]


def _snapshot_container_data(
    runtime: str, container_name: str, *, root: Path, run: Runner,
) -> bytes:
    """Run `app/backup_cli.py` inside the running container and return the
    raw .tgz bytes it writes to stdout -- the WAL-safe DB snapshot plus
    uploads/ and the other files named in app/backup.py's `_SIDE_FILES`,
    pulled live out of the named volume backing /data. Requires the
    container to be running (`docker/podman exec` cannot reach a stopped
    one); raises BackupError with a clear, actionable message otherwise
    rather than a raw non-zero exit from exec.
    """
    state = compose.inspect_state(runtime, container_name, run=run)
    if state is None or state.get("Status") != "running":
        raise BackupError(
            f"The {container_name!r} container must be running to create a backup -- "
            f"its data now lives in a Docker volume, only reachable while the container "
            f"is up. Start it first (`job-squire start`)."
        )
    argv = [compose.runtime_binary(runtime), "exec", "-T", container_name, *_BACKUP_CLI_ARGV]
    try:
        result = run(argv, cwd=str(root), capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BackupError(f"Failed to snapshot data from {container_name!r}: {exc}") from exc
    if result.returncode != 0:
        stderr = result.stderr
        stderr = stderr.decode(errors="replace") if isinstance(stderr, bytes) else (stderr or "")
        raise BackupError(f"Snapshotting data from {container_name!r} failed: {stderr.strip()}")
    stdout = result.stdout
    return stdout if isinstance(stdout, bytes) else stdout.encode()


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


# ── Building the archive payload ──────────────────────────────────────────


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
    root: Path, instance: Instance, *, image: str, container_format: int, run: Runner = subprocess.run,
) -> tuple[bytes, dict]:
    """Pull the live data snapshot out of the container, walk the rest of
    the instance directory verbatim, and build the (unencrypted) tar.gz or
    zip payload plus its manifest dict.

    Two sources, folded into one flat `entries` list so the resulting
    archive has exactly the same shape it always did (a `data/` subtree
    alongside docker-compose.yml and the top-level `.env`), even though the
    two are gathered completely differently now:

    - Everything under `root` EXCEPT the contents of `data/` (excluding
      `data/.env` itself, which is still a real host file -- see this
      module's docstring) is walked directly, exactly as before.
    - `data/`'s actual contents -- the database, uploads/, and the other
      files app/backup.py's `_SIDE_FILES` names -- come from
      `_snapshot_container_data`, which runs `app/backup_cli.py` inside the
      running container and returns its own inner .tgz. That inner
      archive's members are unpacked to a temp dir here (so `_write_tar_gz`/
      `_write_zip` can keep working with plain `(arcname, Path)` entries)
      and re-prefixed with `data/` to land at the same relative paths a
      host walk used to find them at.
    """
    data_prefix = f"{paths.DATA_DIRNAME}/"
    data_env_rel = f"{paths.DATA_DIRNAME}/{paths.DATA_ENV_FILENAME}"
    with tempfile.TemporaryDirectory(prefix="job-squire-backup-") as work:
        work_path = Path(work)
        entries: list[tuple[str, Path]] = []
        checksums: dict[str, str] = {}

        for path in sorted(root.rglob("*")):
            if path.is_dir():
                continue
            rel = path.relative_to(root).as_posix()
            if rel.startswith(data_prefix) and rel != data_env_rel:
                continue  # lives in the named volume now -- gathered below instead
            entries.append((rel, path))
            checksums[rel] = _sha256_file(path)

        container_name = derive_compose_project(instance.name)
        snapshot_bytes = _snapshot_container_data(instance.runtime, container_name, root=root, run=run)
        schema_fingerprint = None
        with tarfile.open(fileobj=io.BytesIO(snapshot_bytes), mode="r:gz") as inner:
            for member in inner.getmembers():
                if not member.isfile():
                    continue
                rel = data_prefix + member.name
                dest = work_path / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(inner.extractfile(member).read())
                entries.append((rel, dest))
                checksums[rel] = _sha256_file(dest)
                if member.name == paths.DB_FILENAME:
                    schema_fingerprint = _schema_fingerprint(dest)

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
    run: Runner = subprocess.run,
    argon2_time_cost: int = backup_crypto.DEFAULT_TIME_COST,
    argon2_memory_cost_kib: int = backup_crypto.DEFAULT_MEMORY_COST_KIB,
    argon2_lanes: int = backup_crypto.DEFAULT_LANES,
) -> BackupResult:
    """Write one encrypted archive for `instance` into `dest_dir` (default:
    the user's home folder). Never writes an unencrypted archive to disk --
    the plaintext payload only ever exists in memory. The instance's
    container must be running (see `_snapshot_container_data`)."""
    if ext not in _EXT_TO_FORMAT:
        raise BackupError(f"Unsupported backup format {ext!r} -- expected 'tgz' or 'zip'.")
    root = instance_root(instance.name, data_root) if data_root is not None else Path(instance.data_dir)
    if not root.exists():
        raise BackupError(f"No data directory found for {instance.name!r} at {root}.")

    image = compose.read_image(root) if paths.compose_path(root).exists() else "unknown"
    container_format = _EXT_TO_FORMAT[ext]
    payload, manifest = _gather_and_build(
        root, instance, image=image, container_format=container_format, run=run,
    )

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
    run: Runner = subprocess.run,
    argon2_time_cost: int = backup_crypto.DEFAULT_TIME_COST,
    argon2_memory_cost_kib: int = backup_crypto.DEFAULT_MEMORY_COST_KIB,
    argon2_lanes: int = backup_crypto.DEFAULT_LANES,
) -> list[BackupResult]:
    """One archive per registered instance -- an option to back up every
    registered instance in one run."""
    return [
        create_backup(
            instance, data_root=data_root, dest_dir=dest_dir, passphrase=passphrase, ext=ext, run=run,
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
    """Verify every file's checksum *before* anything is written to disk --
    the restore ordering is decrypt, then verify, then unpack -- so a
    corrupted archive is caught before it touches the target directory
    rather than partway through extraction."""
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


def _extract_instance_payload(
    payload: bytes, container_format: int, *, host_root: Path, volume_staging: Path,
) -> None:
    """Split the archive back into the two places its two halves came from
    (see `_gather_and_build`): `host_root` gets docker-compose.yml, the
    compose-level `.env`, and `data/.env` -- real host files, written
    directly and immediately. Everything else that was under `data/` lands
    in `volume_staging` instead, with that prefix stripped (so
    `data/job-squire.db` -> `<volume_staging>/job-squire.db`), ready for
    `_copy_staged_data_into_container` to `docker cp` into the freshly
    created (but not yet started) container's named volume.
    """
    host_root.mkdir(parents=True, exist_ok=True)
    volume_staging.mkdir(parents=True, exist_ok=True)
    data_prefix = f"{paths.DATA_DIRNAME}/"
    data_env_rel = f"{paths.DATA_DIRNAME}/{paths.DATA_ENV_FILENAME}"
    for rel, data in _iter_payload_members(payload, container_format):
        if rel.startswith(data_prefix) and rel != data_env_rel:
            target = _safe_member_path(volume_staging, rel[len(data_prefix):])
        else:
            target = _safe_member_path(host_root, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "wb") as out:
            out.write(data)


def _copy_staged_data_into_container(
    runtime: str, container_name: str, staging_dir: Path, *, run: Runner,
) -> None:
    """`docker/podman cp <staging_dir>/. <container_name>:/data` -- the
    trailing `/.` on the source copies the *contents* of `staging_dir` into
    `/data` rather than nesting a `staging_dir` directory inside it. Works
    against a container created but not started (`compose_create`), which
    is the point: nothing has read or written the volume yet, so there is
    no race with the app's own first-boot database creation.
    """
    argv = [compose.runtime_binary(runtime), "cp", f"{staging_dir}/.", f"{container_name}:/data"]
    try:
        result = run(argv, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RestoreError(f"Failed to copy restored data into {container_name!r}: {exc}") from exc
    if result.returncode != 0:
        raise RestoreError(f"Copying restored data into {container_name!r} failed: {result.stderr}")


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
    rather than clobbering silently.

    The container is always created (`compose create`) and its restored
    data always copied in, regardless of `bring_up` -- the named volume
    only exists once the container is created, so that step can't be
    deferred to a later `job-squire start` the way starting the app itself
    can. `bring_up=False` only skips the final `compose up`/health wait,
    leaving a fully-populated, not-yet-started container for the operator
    to inspect or adjust before starting it themselves.
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

    staging_ctx = tempfile.TemporaryDirectory(prefix="job-squire-restore-")
    staging_dir = Path(staging_ctx.name)
    try:
        _extract_instance_payload(
            opened.payload, opened.container_format, host_root=root, volume_staging=staging_dir,
        )
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
                umask=compose_env.get("UMASK", "022"),
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

        container_name = derive_compose_project(slug)
        create_result = compose.compose_create(chosen_runtime, root, container_name, run=run)
        if create_result.returncode != 0:
            raise RestoreError(f"Failed to create container {container_name!r}: {create_result.stderr}")
        _copy_staged_data_into_container(chosen_runtime, container_name, staging_dir, run=run)

        health = None
        if bring_up:
            up_result = compose.compose_up(chosen_runtime, root, container_name, run=run)
            if up_result.returncode != 0:
                lc._raise_for_failed_state(chosen_runtime, container_name, None, run=run)
            health = lc.wait_for_state(chosen_runtime, container_name, run=run, sleep=sleep)
            if health is not None and health.get("Status") == "exited":
                lc._raise_for_failed_state(chosen_runtime, container_name, health, run=run)

        return RestoreResult(instance=instance, data_dir=root, health=health)
    finally:
        staging_ctx.cleanup()
