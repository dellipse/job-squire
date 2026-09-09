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
"""Container-side entrypoint for `job-squire create --import-from`
(job_squire_cli's ops/secrets_copy.py).

Same reasoning as `app/backup_cli.py`, `app/ollama_provider_cli.py`, and
`app/mcp_token_cli.py`: since /data is a named Docker volume rather than a
host bind mount, job_squire_cli cannot read or write
`<instance_root>/data/job-squire.db` directly from the host anymore. This
module runs the two raw-sqlite operations ops/secrets_copy.py's
`copy_db_settings` needs inside the *running* container instead -- `docker
exec <container> python -m app.secrets_copy_cli` (or the `podman`
equivalent), fed a JSON request on stdin -- and `copy_db_settings` calls it
twice: once against the *source* instance's container (`op: "dump"`, a
read-only SELECT per table) and once against the *destination* instance's
container (`op: "apply"`, the INSERT/UPDATE/DELETE upsert logic that used
to run directly against `conn_dst`).

**Column allowlists and upsert strategy are hand-duplicated from
ops/secrets_copy.py** (the `_SEARCH_CONFIG`/`_SMTP_CONFIG`/etc. tuples and
the `_upsert_singleton`/`_upsert_by_key`/`_update_only_by_key`/
`_copy_full_replace` dispatch), the same way `app/mcp_token_cli.py`
hand-duplicates `_FRESH_ROW_DEFAULTS` and `app/ollama_provider_cli.py`
hand-duplicates the `ai_provider_configs` statements -- this package
deliberately never imports job_squire_cli (module docstring's dependency
boundary, mirrored in ops/secrets_copy.py's own docstring). Every table is
still read/written defensively (a missing table or column produces a
warning back to the caller, not a crash), consistent with "additive,
never assumed" migrations elsewhere in this project.

**All secret handling (decrypt-with-source-key / re-encrypt-with-dest-key)
stays on the host**, in ops/secrets_copy.py's `reencrypt()`. This module
only ever sees whatever's already in the requested columns on `dump`
(including still-encrypted secret blobs, verbatim) and whatever the host
has already finalized (already re-encrypted, or blanked out) on `apply` --
it never needs either instance's SECRET_KEY.

Protocol: reads one JSON object from stdin --

    {"op": "dump", "tables": [{"table": str, "columns": [str, ...]}, ...]}

        -- writes {"tables": {table: [{col: val, ...}, ...] | null}} to
           stdout, where null means the table (or one of the requested
           columns) doesn't exist in this schema version.

    {"op": "apply", "tables": [
        {"table": str, "strategy": "singleton" | "by_key" | "update_only_by_key" | "full_replace",
         "key_col": str | null, "rows": [{col: val, ...}, ...]},
        ...
    ]}

        -- applies every table's rows in one transaction (one commit at
           the end, matching what ops/secrets_copy.py's `copy_db_settings`
           used to do against a single local `conn_dst`) and writes
           {"applied": [table, ...]} to stdout.

-- or a plain-text error to stderr and a nonzero exit on failure (a
missing database, or a `dump`/`apply` table request whose table exists but
some other statement fails unexpectedly).
"""
import json
import os
import sqlite3
import sys


class SecretsCopyCliError(RuntimeError):
    """Raised for a missing database inside the container."""


def _connect(db_path: str) -> sqlite3.Connection:
    if not os.path.exists(db_path):
        raise SecretsCopyCliError(f"Database not found at {db_path} inside the container.")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


# ── dump (read-only, run against the source instance's container) ───────


def _dump(conn: sqlite3.Connection, tables: list[dict]) -> dict:
    result: dict[str, list[dict] | None] = {}
    for spec in tables:
        table, columns = spec["table"], spec["columns"]
        try:
            rows = conn.execute(f"SELECT {', '.join(columns)} FROM {table}").fetchall()  # noqa: S608
        except sqlite3.OperationalError:
            result[table] = None  # table or column doesn't exist in this schema version
            continue
        result[table] = [dict(row) for row in rows]
    return {"tables": result}


# ── apply (run against the destination instance's container) ────────────


def _upsert_singleton(conn: sqlite3.Connection, table: str, values: dict) -> None:
    cols = list(values)
    assignments = ", ".join(f"{c} = ?" for c in cols)
    cur = conn.execute(f"UPDATE {table} SET {assignments} WHERE id = 1", [values[c] for c in cols])  # noqa: S608
    if cur.rowcount == 0:
        placeholders = ", ".join(["?"] * len(cols))
        conn.execute(
            f"INSERT INTO {table} (id, {', '.join(cols)}) VALUES (1, {placeholders})",  # noqa: S608
            [values[c] for c in cols],
        )


def _upsert_by_key(conn: sqlite3.Connection, table: str, key_col: str, key_val: object, values: dict) -> None:
    cols = list(values)
    assignments = ", ".join(f"{c} = ?" for c in cols)
    cur = conn.execute(
        f"UPDATE {table} SET {assignments} WHERE {key_col} = ?",  # noqa: S608
        [values[c] for c in cols] + [key_val],
    )
    if cur.rowcount == 0:
        all_cols = [key_col] + cols
        placeholders = ", ".join(["?"] * len(all_cols))
        conn.execute(
            f"INSERT INTO {table} ({', '.join(all_cols)}) VALUES ({placeholders})",  # noqa: S608
            [key_val] + [values[c] for c in cols],
        )


def _update_only_by_key(conn: sqlite3.Connection, table: str, key_col: str, key_val: object, values: dict) -> None:
    cols = list(values)
    assignments = ", ".join(f"{c} = ?" for c in cols)
    conn.execute(
        f"UPDATE {table} SET {assignments} WHERE {key_col} = ?",  # noqa: S608
        [values[c] for c in cols] + [key_val],
    )


def _full_replace(conn: sqlite3.Connection, table: str, rows: list[dict]) -> None:
    conn.execute(f"DELETE FROM {table}")  # noqa: S608 (table name from a fixed allowlist on the host side)
    for values in rows:
        cols = list(values)
        placeholders = ", ".join(["?"] * len(cols))
        conn.execute(
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})",  # noqa: S608
            [values[c] for c in cols],
        )


def _apply(conn: sqlite3.Connection, tables: list[dict]) -> dict:
    applied = []
    for spec in tables:
        table, strategy, key_col, rows = spec["table"], spec["strategy"], spec.get("key_col"), spec["rows"]
        if strategy == "full_replace":
            _full_replace(conn, table, rows)
        else:
            for values in rows:
                if strategy == "singleton":
                    _upsert_singleton(conn, table, values)
                elif strategy == "by_key":
                    _upsert_by_key(conn, table, key_col, values[key_col], {k: v for k, v in values.items() if k != key_col})
                elif strategy == "update_only_by_key":
                    _update_only_by_key(conn, table, key_col, values[key_col], {k: v for k, v in values.items() if k != key_col})
                else:
                    raise SecretsCopyCliError(f"Unknown apply strategy: {strategy!r}")
        applied.append(table)
    conn.commit()
    return {"applied": applied}


def handle_request(db_path: str, payload: dict) -> dict:
    op = payload.get("op")
    conn = _connect(db_path)
    try:
        if op == "dump":
            return _dump(conn, payload["tables"])
        if op == "apply":
            return _apply(conn, payload["tables"])
        raise SecretsCopyCliError(f"Unknown op: {op!r}")
    finally:
        conn.close()


def main() -> int:
    data_dir = os.environ.get("DATA_DIR", "/data")
    db_path = os.path.join(data_dir, "job-squire.db")
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        print(f"Malformed request on stdin: {exc}", file=sys.stderr)
        return 1
    try:
        response = handle_request(db_path, payload)
    except SecretsCopyCliError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 -- surface any failure to the CLI's stderr, not a traceback dump
        print(f"Settings copy operation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(response))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
