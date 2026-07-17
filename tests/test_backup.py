# Copyright (C) 2026 D. Brandmeyer
# Licensed under the GNU Affero General Public License v3 or later.
"""Tests for the in-app backup download (app/backup.py + /settings/backup/download).

Covers:
  * build_backup_archive() produces a valid, WAL-safe tar.gz containing the DB
    snapshot and uploads/, and honors include_env.
  * the route is admin-only (mirrors the rest of /settings) and streams the
    archive with the right content type / filename.

Restore is intentionally not covered here — it is a host-level CLI operation
(scripts/restore.sh), not an app route. See app/backup.py's module docstring.
"""
import io
import os
import sqlite3
import subprocess
import sys
import tarfile

import pytest

from app.backup import build_backup_archive

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "admin-test-pw"
SEEKER_USERNAME = "seeker"
SEEKER_PASSWORD = "user-test-pw"

BACKUP_URL = "/settings/backup/download"


def _login(client, username, password):
    return client.post(
        "/login", data={"username": username, "password": password}, follow_redirects=False
    )


# --------------------------------------------------------------------------- #
# build_backup_archive
# --------------------------------------------------------------------------- #

def test_build_backup_archive_contains_db_and_uploads(app, app_context):
    data_dir = app.config["DATA_DIR"]
    upload_dir = app.config["UPLOAD_DIR"]

    # Sanity: the session-scoped app fixture has already created the real DB.
    assert os.path.exists(os.path.join(data_dir, "job-squire.db"))

    filename, blob = build_backup_archive(data_dir, upload_dir, include_env=True)
    assert filename.startswith("job-squire-backup-")
    assert filename.endswith(".tgz")

    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        names = tar.getnames()
        assert "job-squire.db" in names
        # uploads/ dir is added even if empty (tarfile still records the dir entry).
        assert any(n == "uploads" or n.startswith("uploads/") for n in names)

        # The bundled DB snapshot must itself be a valid, intact SQLite file —
        # this is the whole point of using the Online Backup API instead of a
        # raw file copy of a WAL-mode DB.
        member = tar.extractfile("job-squire.db")
        snapshot_bytes = member.read()

    tmp_path = os.path.join(data_dir, "_test_snapshot_check.db")
    with open(tmp_path, "wb") as f:
        f.write(snapshot_bytes)
    try:
        conn = sqlite3.connect(tmp_path)
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        conn.close()
        assert result == "ok"
    finally:
        os.remove(tmp_path)


def test_build_backup_archive_excludes_env_when_not_requested(app, app_context):
    data_dir = app.config["DATA_DIR"]
    upload_dir = app.config["UPLOAD_DIR"]
    env_path = os.path.join(data_dir, ".env")

    with open(env_path, "w") as f:
        f.write("SECRET_KEY=whatever\n")
    try:
        _, with_env = build_backup_archive(data_dir, upload_dir, include_env=True)
        with tarfile.open(fileobj=io.BytesIO(with_env), mode="r:gz") as tar:
            assert ".env" in tar.getnames()

        _, without_env = build_backup_archive(data_dir, upload_dir, include_env=False)
        with tarfile.open(fileobj=io.BytesIO(without_env), mode="r:gz") as tar:
            assert ".env" not in tar.getnames()
    finally:
        os.remove(env_path)


def test_build_backup_archive_missing_db_raises(app, app_context, tmp_path):
    empty_dir = str(tmp_path / "no-db-here")
    os.makedirs(empty_dir, exist_ok=True)
    with pytest.raises(FileNotFoundError):
        build_backup_archive(empty_dir, os.path.join(empty_dir, "uploads"))


def test_build_backup_archive_includes_privacy_vault_and_profile_prompt(app, app_context):
    """Regression guard: job-squire-cli's create_backup (once /data may be a
    named Docker volume) relies entirely on this function's _SIDE_FILES list
    to capture anything besides the DB and uploads/ -- a file missing here is
    a file silently absent from every CLI backup, not just the in-app one."""
    data_dir = app.config["DATA_DIR"]
    upload_dir = app.config["UPLOAD_DIR"]
    vault_path = os.path.join(data_dir, "privacy_vault.json")
    prompt_path = os.path.join(data_dir, "profile_prompt.md")

    with open(vault_path, "w") as f:
        f.write("{}")
    with open(prompt_path, "w") as f:
        f.write("# scoring guidance\n")
    try:
        _, blob = build_backup_archive(data_dir, upload_dir, include_env=False)
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
            names = tar.getnames()
            assert "privacy_vault.json" in names
            assert "profile_prompt.md" in names
    finally:
        os.remove(vault_path)
        os.remove(prompt_path)


# --------------------------------------------------------------------------- #
# app/backup_cli.py -- the container-side entrypoint job-squire-cli execs
# --------------------------------------------------------------------------- #

def test_backup_cli_writes_archive_bytes_to_stdout(app, app_context):
    data_dir = app.config["DATA_DIR"]
    env = dict(os.environ, DATA_DIR=data_dir)
    result = subprocess.run(
        [sys.executable, "-m", "app.backup_cli"], env=env, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:gz") as tar:
        names = tar.getnames()
        assert "job-squire.db" in names
        assert ".env" not in names  # never duplicates the host's data/.env


def test_backup_cli_missing_db_exits_nonzero_with_stderr_message(tmp_path):
    empty_dir = tmp_path / "no-db-here"
    empty_dir.mkdir()
    env = dict(os.environ, DATA_DIR=str(empty_dir))
    result = subprocess.run(
        [sys.executable, "-m", "app.backup_cli"], env=env, capture_output=True, timeout=30,
    )
    assert result.returncode == 1
    assert b"No database found" in result.stderr


# --------------------------------------------------------------------------- #
# /settings/backup/download route
# --------------------------------------------------------------------------- #

def test_backup_download_requires_login(client):
    resp = client.get(BACKUP_URL, follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_backup_download_forbids_non_admin(client):
    _login(client, SEEKER_USERNAME, SEEKER_PASSWORD)
    resp = client.get(BACKUP_URL, follow_redirects=False)
    assert resp.status_code == 403


def test_backup_download_allows_admin(client):
    _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    resp = client.get(BACKUP_URL, follow_redirects=False)
    assert resp.status_code == 200
    assert resp.mimetype == "application/gzip"
    assert "attachment; filename=job-squire-backup-" in resp.headers["Content-Disposition"]

    with tarfile.open(fileobj=io.BytesIO(resp.data), mode="r:gz") as tar:
        assert "job-squire.db" in tar.getnames()


def test_backup_download_include_env_false_via_query_param(client):
    _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    resp = client.get(BACKUP_URL + "?include_env=0", follow_redirects=False)
    assert resp.status_code == 200
    with tarfile.open(fileobj=io.BytesIO(resp.data), mode="r:gz") as tar:
        assert ".env" not in tar.getnames()
