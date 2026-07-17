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
"""In-app, one-click backup download.

Builds the exact same archive `scripts/backup.sh` produces (WAL-safe DB
snapshot + uploads/ + candidate_profile.md + oauth_tokens.json + optionally
.env), tarred into a single `.tgz`, so a file downloaded here can be restored
with the existing `scripts/restore.sh` with no format differences.

Restore is deliberately NOT implemented as an in-app HTTP action. A safe
restore requires stopping the container before its data is replaced, which
is a host-level operation this container has no way to perform on itself.
`scripts/restore.sh` already does this correctly (stop -> docker cp the
restored data into the (named-volume-backed) /data -> fix ownership on
next start -> restart) — see docs/backup-restore.md. Re-implementing that
dance behind a web request would either not actually stop the container
(silent data race) or would require this container to reach out and manage
its own orchestration, which is a much larger and riskier change for a
two-user app.

`build_backup_archive` is also invoked from inside the container, without
Flask, by `app/backup_cli.py` — that is how job-squire-cli's own `backup`
command gets a WAL-safe snapshot out of a /data that may now be a named
Docker volume rather than a host bind mount: the CLI cannot read a named
volume's contents directly from the host, so it runs this same function
via `docker exec` and reads the resulting bytes off the exec's stdout
instead of walking a host path.
"""
import io
import logging
import os
import sqlite3
import tarfile
import tempfile
import time

log = logging.getLogger(__name__)

# Files (besides the DB and uploads/) that travel with a backup, mirroring
# scripts/backup.sh exactly so archives from either path are interchangeable.
# privacy_vault.json matters as much as the DB itself: without it, any
# {{PII:KIND_digest}} placeholder already written into saved AI analysis
# text (app/privacy.py) can never be rehydrated back to the real value after
# a restore. profile_prompt.md is the user's own edited scoring-guidance
# text, not a regenerable cache.
_SIDE_FILES = ["candidate_profile.md", "profile_prompt.md", "oauth_tokens.json", "privacy_vault.json"]


def _snapshot_db(db_path, dest_path):
    """WAL-safe consistent snapshot via SQLite's own Online Backup API.

    Safe to run against a live, concurrently-written database — this is the
    same mechanism the `sqlite3 .backup` CLI command and scripts/backup.sh use.
    """
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(dest_path)
    try:
        with dst:
            src.backup(dst)
    finally:
        src.close()
        dst.close()

    conn = sqlite3.connect(dest_path)
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()
    if result != "ok":
        raise RuntimeError(f"Backup snapshot failed integrity check: {result}")


def build_backup_archive(data_dir, upload_dir, include_env=True):
    """Return (filename, bytes) for a full backup .tgz of the current data dir.

    Raises FileNotFoundError if there is no DB yet (nothing to back up).
    """
    db_path = os.path.join(data_dir, "job-squire.db")
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"No database found at {db_path}")

    ts = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    filename = f"job-squire-backup-{ts}.tgz"

    with tempfile.TemporaryDirectory(prefix="jobsquire-backup-") as work:
        snapshot_path = os.path.join(work, "job-squire.db")
        _snapshot_db(db_path, snapshot_path)

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            tar.add(snapshot_path, arcname="job-squire.db")

            if os.path.isdir(upload_dir):
                tar.add(upload_dir, arcname="uploads")

            for name in _SIDE_FILES:
                path = os.path.join(data_dir, name)
                if os.path.exists(path):
                    tar.add(path, arcname=name)

            env_path = os.path.join(data_dir, ".env")
            if include_env and os.path.exists(env_path):
                tar.add(env_path, arcname=".env")

        buf.seek(0)
        return filename, buf.getvalue()
