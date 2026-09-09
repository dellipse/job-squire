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
"""Regression test for CLI-01/TEST-03 (2026-09-08 pre-prod audit).

Background: the 2026-07-17 volume migration moved `/data` from a host bind
mount to a named Docker volume (ops/compose.py) -- `<instance_root>/data/
job-squire.db` has not existed on the host since then, only
`<instance_root>/data/.env` does. Two CLI modules were never ported off
the old host-path access pattern: ops/mcp_token.py's `_connect` (backing
`job-squire configure NAME --mcp-token generate|rotate|revoke`) and
ops/secrets_copy.py's `copy_db_settings` (backing `job-squire create
--import-from`) both kept doing `sqlite3.connect()` against that now-
nonexistent host path, so both commands failed against every instance the
CLI actually creates. CI stayed green because the *other* tests for these
modules built their fixture databases directly at
`paths.sqlite_db_path(root)` -- the very host path the bug made
unreachable in production -- so they never noticed the path was wrong
(TEST-03: the same defect from a testing-hygiene angle).

This test builds an instance layout that only has the pieces that
genuinely exist on the host post-migration -- `data/.env` and nothing else
under `data/` -- and keeps each instance's real "database" in a
completely separate location that stands in for its named Docker volume,
reachable only through the faked `docker/podman exec` boundary (mirroring
tests/test_mcp_token.py's and tests/test_secrets_copy.py's own
`container_fake_run` helpers). If either module regressed to opening
`paths.sqlite_db_path(root)` directly again, these tests would fail with
the exact `McpTokenError`/`SecretsCopyError` "hasn't booted yet" /
"database not found" messages that shipped to every real instance.
"""
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from job_squire_cli.ops import mcp_token as mt
from job_squire_cli.ops import paths
from job_squire_cli.ops import secrets_copy as sc
from job_squire_cli.ops.crypto_mirror import decrypt

from tests.test_mcp_token import _AI_CONFIG_SCHEMA, _handle_request_like_container_cli
from tests.test_secrets_copy import (
    _SCHEMA,
    _apply_like_container_cli,
    _dump_like_container_cli,
    _seed_dest_defaults,
    _seed_source,
)

_CONTAINER = "job-squire-castelo"


def _named_volume_instance_root(tmp_path, name: str) -> Path:
    """Only `data/.env` -- exactly what's actually left on the host for a
    real instance post-migration. No `data/job-squire.db` is ever created
    here; that's the whole point."""
    root = tmp_path / name
    paths.data_dir(root).mkdir(parents=True)
    paths.data_env_path(root).write_text(f"SECRET_KEY=secret-{name}\nINSTANCE_NAME={name}\n")
    return root


def test_mcp_token_write_and_read_never_touch_the_dead_host_path(tmp_path):
    root = _named_volume_instance_root(tmp_path, "castelo")

    # Sanity check on the premise: the old host path genuinely doesn't
    # exist. If it did, this test would prove nothing.
    assert not paths.sqlite_db_path(root).exists()

    # The instance's *actual* database -- what a real named Docker volume
    # would contain -- lives somewhere else entirely, reachable only
    # through the faked container exec below.
    volume_db_path = tmp_path / "named-volume" / "job-squire.db"
    volume_db_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(str(volume_db_path))
    conn.executescript(_AI_CONFIG_SCHEMA)
    conn.execute(
        "INSERT INTO ai_config (id, mode, api_enabled, mcp_api_key_enc, mcp_api_key_allow_network) "
        "VALUES (1, 'manual', 0, '', 0)"
    )
    conn.commit()
    conn.close()

    def fake_run(args, **kwargs):
        args = list(args)
        if args[1] == "inspect":
            return SimpleNamespace(returncode=0, stdout=json.dumps({"Status": "running"}), stderr="")
        if args[1] == "exec":
            payload = json.loads(kwargs["input"])
            response = _handle_request_like_container_cli(volume_db_path, payload)
            return SimpleNamespace(returncode=0, stdout=json.dumps(response), stderr="")
        raise AssertionError(f"unexpected call: {args}")

    token = mt.write_new_token(root, "secret-castelo", runtime="docker", container_name=_CONTAINER, run=fake_run)

    # The real proof: a fresh read of the *volume's* database sees the
    # token -- while the host-path file the old implementation used to
    # open still doesn't exist at all.
    assert not paths.sqlite_db_path(root).exists()
    conn = sqlite3.connect(str(volume_db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT mcp_api_key_enc FROM ai_config WHERE id = 1").fetchone()
    conn.close()
    assert decrypt("secret-castelo", row["mcp_api_key_enc"]) == token

    state = mt.read_state(root, runtime="docker", container_name=_CONTAINER, run=fake_run)
    assert state.active is True
    assert state.usable is True


def test_copy_db_settings_never_touches_either_dead_host_path(tmp_path):
    source_root = _named_volume_instance_root(tmp_path, "source")
    dest_root = _named_volume_instance_root(tmp_path, "dest")
    assert not paths.sqlite_db_path(source_root).exists()
    assert not paths.sqlite_db_path(dest_root).exists()

    source_volume_db = tmp_path / "source-volume" / "job-squire.db"
    dest_volume_db = tmp_path / "dest-volume" / "job-squire.db"
    source_volume_db.parent.mkdir(parents=True)
    dest_volume_db.parent.mkdir(parents=True)

    sconn = sqlite3.connect(str(source_volume_db))
    sconn.executescript(_SCHEMA)
    _seed_source(sconn, "secret-source")
    sconn.close()

    dconn = sqlite3.connect(str(dest_volume_db))
    dconn.executescript(_SCHEMA)
    _seed_dest_defaults(dconn)
    dconn.close()

    def fake_run(args, **kwargs):
        args = list(args)
        if args[1] == "inspect":
            return SimpleNamespace(returncode=0, stdout=json.dumps({"Status": "running"}), stderr="")
        if args[1] == "exec":
            container_name = args[3] if args[2] == "-i" else args[2]
            db_path = source_volume_db if container_name == "job-squire-source" else dest_volume_db
            payload = json.loads(kwargs["input"])
            if payload["op"] == "dump":
                response = _dump_like_container_cli(db_path, payload["tables"])
            else:
                response = _apply_like_container_cli(db_path, payload["tables"])
            return SimpleNamespace(returncode=0, stdout=json.dumps(response), stderr="")
        raise AssertionError(f"unexpected call: {args}")

    summary = sc.copy_db_settings(
        source_root=source_root, dest_root=dest_root,
        source_secret_key="secret-source", dest_secret_key="secret-dest", copy_keys=False,
        source_runtime="docker", source_container_name="job-squire-source",
        dest_runtime="docker", dest_container_name="job-squire-dest",
        run=fake_run,
    )

    assert not summary.warnings
    assert "search_config" in summary.tables_copied

    # The real proof: a fresh read of the *destination volume's* database
    # sees the imported settings -- while neither instance's host-path
    # file (what the old implementation used to open) was ever created.
    assert not paths.sqlite_db_path(source_root).exists()
    assert not paths.sqlite_db_path(dest_root).exists()
    conn = sqlite3.connect(str(dest_volume_db))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT titles, location FROM search_config WHERE id = 1").fetchone()
    conn.close()
    assert row["titles"] == "Engineer"
    assert row["location"] == "Austin, TX"
