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
"""Importing basic settings from an existing instance into a new one
(Prompt C5, PLAN Section 4 "Setup and the import prompt").

Two independent sources feed the import, because Job Squire itself splits
config the same way (app/deploy.py's module docstring, and
docs/PLAN-deployment-modes.md Section 3): environment variables in
`data/.env` for deployment shape, and database rows for everything the
running app can change on the fly.

  - Schedule hours and timezone are `data/.env` variables
    (SCHEDULE_TZ/SCHEDULE_WEEKDAY_HOURS/SCHEDULE_WEEKEND_HOURS/
    SCHEDULE_MINUTE) -- `read_schedule_env` reads them as plain text, no
    database involved, and lifecycle.create_instance applies them to the
    *new* instance's env before it ever boots.
  - Search titles/location/radius, enabled providers, SMTP host/port, AI
    provider selection, and interface preferences live in the database
    (app/models.py) -- `copy_db_settings` reads and writes these directly
    with the stdlib `sqlite3` module. This package intentionally does not
    depend on Flask/SQLAlchemy/the app package at all (an operator running
    the CLI has not necessarily cloned the app repo, and the app's stack is
    meant to live inside the container, not on the host) -- so the column
    allowlists below are hand-maintained against app/models.py rather than
    imported from it. Every table is read defensively (a missing table or
    column produces a warning in ImportSummary, not a crash), consistent
    with "additive, never assumed" migrations elsewhere in this project.

Secrets are excluded by default (CLAUDE.md: "ALL stored secrets encrypted
... never plaintext"; PLAN Section 4: "Secrets are excluded by default").
`copy_db_settings(..., copy_keys=True)` is the explicit opt-in, and because
every instance gets its own independently random SECRET_KEY (PLAN Section
4 "Keys and secrets are always independent"), copying an *encrypted*
column verbatim would not decrypt at the destination -- so opting in
decrypts with the source instance's SECRET_KEY and re-encrypts with the
destination's, using the HKDF-SHA256 -> Fernet derivation mirrored from
app/crypto.py in ops/crypto_mirror.py (shared with ops/mcp_token.py,
Prompt C6, so that mirrored contract lives in one place -- see that
module's docstring for why it isn't imported from app/crypto.py directly).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import paths
from .crypto_mirror import decrypt as _mirror_decrypt, encrypt as _mirror_encrypt

_SCHEDULE_ENV_KEYS = (
    "SCHEDULE_TZ", "SCHEDULE_WEEKDAY_HOURS", "SCHEDULE_WEEKEND_HOURS", "SCHEDULE_MINUTE",
)

# (table, key_column, non-secret columns, secret columns) -- key_column is
# None for the two true singleton tables (id=1 rows the app always seeds).
_SEARCH_CONFIG = ("search_config", None,
    ("titles", "location", "country", "radius_miles", "min_salary", "max_age_days",
     "results_per_query", "enabled"), ())
_SMTP_CONFIG = ("smtp_config", None,
    ("enabled", "host", "port", "use_tls", "username", "from_addr", "to_addr", "admin_email"),
    ("password_enc",))
_AI_CONFIG = ("ai_config", None,
    ("api_enabled", "mcp_enabled", "claude_buttons_enabled", "model", "thinking_mode",
     "auto_triage_enabled", "triage_model", "auto_followup_enabled", "auto_weekly_review_enabled",
     "rejection_alert_threshold", "fallback_to_anthropic", "connector_name",
     "mcp_api_key_allow_network"),
    ("api_key_enc", "mcp_api_key_enc"))
_PROVIDER_CREDENTIALS = ("provider_credentials", "provider", ("enabled",), ("secret_blob",))
_USER_PREFS = ("users", "username",
    ("jobs_default_sort", "jobs_default_status", "jobs_default_per_page"), ())
# No natural unique key across a provider chain (the same provider type can
# appear twice at different ranks), so this one is a full replace rather
# than an upsert -- see _copy_full_replace.
_AI_PROVIDER_CONFIGS = ("ai_provider_configs", None,
    ("rank", "provider", "label", "base_url", "model", "triage_model",
     "use_for_triage", "use_for_analysis", "thinking_mode", "enabled"),
    ("api_key_enc",))


class SecretsCopyError(RuntimeError):
    """Raised for a missing/unreadable database -- never for a missing
    table or column inside it, which is a per-table warning instead."""


@dataclass
class ImportSummary:
    tables_copied: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    schedule_vars_copied: list[str] = field(default_factory=list)
    secrets_copied: bool = False


# ── Fernet, delegated to ops/crypto_mirror.py (see module docstring) ────
# Kept as these exact private names -- _decrypt/_encrypt -- because
# tests/test_secrets_copy.py exercises the derivation through them
# directly (including the cross-check against the real app/crypto.py).


def _decrypt(secret_key: str, stored: str) -> str | None:
    return _mirror_decrypt(secret_key, stored)


def _encrypt(secret_key: str, plaintext: str) -> str:
    return _mirror_encrypt(secret_key, plaintext)


def reencrypt(value: str, *, source_secret_key: str, dest_secret_key: str) -> str | None:
    """Decrypt with the source instance's key and re-encrypt with the
    destination's. Returns None if the source value couldn't be decrypted
    (the caller should warn and leave the destination's existing value
    alone rather than overwrite it with garbage)."""
    plaintext = _decrypt(source_secret_key, value)
    if plaintext is None:
        return None
    return _encrypt(dest_secret_key, plaintext)


# ── data/.env schedule variables ─────────────────────────────────────────


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


def read_schedule_env(source_root: Path) -> dict[str, str]:
    """Whitelisted schedule vars from the source instance's `data/.env`,
    skipping any that are blank/unset there (nothing to import)."""
    all_vars = _parse_env_file(paths.data_env_path(source_root))
    return {k: all_vars[k] for k in _SCHEDULE_ENV_KEYS if all_vars.get(k)}


def read_secret_key(instance_root: Path) -> str:
    """The instance's SECRET_KEY, read directly from its `data/.env` --
    only ever called for the *source* instance of a `copy_keys=True`
    import, to derive the Fernet key needed to decrypt its stored secrets
    before re-encrypting them for the destination."""
    value = _parse_env_file(paths.data_env_path(instance_root)).get("SECRET_KEY")
    if not value:
        raise SecretsCopyError(
            f"No SECRET_KEY found in {paths.data_env_path(instance_root)} -- cannot decrypt its stored secrets."
        )
    return value


# ── Database settings ────────────────────────────────────────────────────


def _columns_for(cols: tuple[str, ...], secret_cols: tuple[str, ...], copy_keys: bool) -> tuple[str, ...]:
    return cols + (secret_cols if copy_keys else ())


def _fetch_rows(conn: sqlite3.Connection, table: str, columns: tuple[str, ...]) -> list[sqlite3.Row] | None:
    try:
        return conn.execute(f"SELECT {', '.join(columns)} FROM {table}").fetchall()  # noqa: S608 (fixed allowlist)
    except sqlite3.OperationalError:
        return None  # table or column doesn't exist in this schema version


def _row_values(row: sqlite3.Row, cols: tuple[str, ...], secret_cols: tuple[str, ...], *,
                 copy_keys: bool, source_secret_key: str, dest_secret_key: str,
                 summary: ImportSummary, table: str) -> dict[str, object]:
    values = {c: row[c] for c in cols}
    if copy_keys:
        for c in secret_cols:
            reenc = reencrypt(row[c] or "", source_secret_key=source_secret_key, dest_secret_key=dest_secret_key)
            if reenc is None:
                summary.warnings.append(
                    f"{table}.{c}: could not decrypt with the source instance's SECRET_KEY -- left unset."
                )
                reenc = ""
            values[c] = reenc
    return values


def _upsert_singleton(conn: sqlite3.Connection, table: str, values: dict[str, object]) -> None:
    cols = list(values)
    assignments = ", ".join(f"{c} = ?" for c in cols)
    cur = conn.execute(f"UPDATE {table} SET {assignments} WHERE id = 1", [values[c] for c in cols])  # noqa: S608
    if cur.rowcount == 0:
        placeholders = ", ".join(["?"] * len(cols))
        conn.execute(
            f"INSERT INTO {table} (id, {', '.join(cols)}) VALUES (1, {placeholders})",  # noqa: S608
            [values[c] for c in cols],
        )


def _upsert_by_key(conn: sqlite3.Connection, table: str, key_col: str, key_val: object,
                    values: dict[str, object]) -> None:
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


def _update_only_by_key(conn: sqlite3.Connection, table: str, key_col: str, key_val: object,
                         values: dict[str, object]) -> None:
    """Like _upsert_by_key but never inserts -- for `users`, whose rows are
    only ever created by the app's own account seeding, never by this."""
    cols = list(values)
    assignments = ", ".join(f"{c} = ?" for c in cols)
    conn.execute(
        f"UPDATE {table} SET {assignments} WHERE {key_col} = ?",  # noqa: S608
        [values[c] for c in cols] + [key_val],
    )


def _copy_table(conn_src: sqlite3.Connection, conn_dst: sqlite3.Connection, spec, *,
                 copy_keys: bool, source_secret_key: str, dest_secret_key: str,
                 summary: ImportSummary, insert_new: bool) -> None:
    table, key_col, cols, secret_cols = spec
    columns = _columns_for(cols, secret_cols, copy_keys)
    read_cols = columns if key_col is None else (key_col,) + columns
    rows = _fetch_rows(conn_src, table, read_cols)
    if rows is None:
        summary.warnings.append(f"{table}: not found in the source database (skipped).")
        return
    for row in rows:
        values = _row_values(
            row, cols, secret_cols, copy_keys=copy_keys,
            source_secret_key=source_secret_key, dest_secret_key=dest_secret_key,
            summary=summary, table=table,
        )
        if key_col is None:
            _upsert_singleton(conn_dst, table, values)
        elif insert_new:
            _upsert_by_key(conn_dst, table, key_col, row[key_col], values)
        else:
            _update_only_by_key(conn_dst, table, key_col, row[key_col], values)
    summary.tables_copied.append(table)


def _copy_full_replace(conn_src: sqlite3.Connection, conn_dst: sqlite3.Connection, spec, *,
                        copy_keys: bool, source_secret_key: str, dest_secret_key: str,
                        summary: ImportSummary) -> None:
    table, _key_col, cols, secret_cols = spec
    columns = _columns_for(cols, secret_cols, copy_keys)
    rows = _fetch_rows(conn_src, table, columns)
    if rows is None:
        summary.warnings.append(f"{table}: not found in the source database (skipped).")
        return
    conn_dst.execute(f"DELETE FROM {table}")  # noqa: S608 (fixed table name)
    for row in rows:
        values = _row_values(
            row, cols, secret_cols, copy_keys=copy_keys,
            source_secret_key=source_secret_key, dest_secret_key=dest_secret_key,
            summary=summary, table=table,
        )
        insert_cols = list(values)
        placeholders = ", ".join(["?"] * len(insert_cols))
        conn_dst.execute(
            f"INSERT INTO {table} ({', '.join(insert_cols)}) VALUES ({placeholders})",  # noqa: S608
            [values[c] for c in insert_cols],
        )
    summary.tables_copied.append(table)


def copy_db_settings(*, source_root: Path, dest_root: Path, source_secret_key: str,
                      dest_secret_key: str, copy_keys: bool = False) -> ImportSummary:
    """Copy basic (and, opt-in, secret) settings from one instance's
    database into another's. The destination database must already exist
    -- i.e. the destination instance has booted at least once so the app's
    own schema creation and seeding have run (lifecycle.create_instance
    brings the new instance up before calling this). The caller is
    responsible for the container being stopped for the duration, so this
    never races the app's own writes to the same file.
    """
    summary = ImportSummary()
    source_db = paths.sqlite_db_path(source_root)
    dest_db = paths.sqlite_db_path(dest_root)
    if not source_db.exists():
        summary.warnings.append(f"Source database not found at {source_db} -- nothing imported.")
        return summary
    if not dest_db.exists():
        raise SecretsCopyError(
            f"Destination database not found at {dest_db}. The new instance must be brought up "
            f"at least once (so the app creates its schema) before settings can be imported."
        )

    # as_uri() percent-encodes the path so this is safe even if the data
    # root ever contains spaces or other characters sqlite's URI parser
    # would otherwise choke on.
    conn_src = sqlite3.connect(f"{source_db.resolve().as_uri()}?mode=ro", uri=True)
    conn_src.row_factory = sqlite3.Row
    conn_dst = sqlite3.connect(str(dest_db))
    conn_dst.row_factory = sqlite3.Row
    try:
        kwargs = dict(
            copy_keys=copy_keys, source_secret_key=source_secret_key,
            dest_secret_key=dest_secret_key, summary=summary,
        )
        _copy_table(conn_src, conn_dst, _SEARCH_CONFIG, insert_new=True, **kwargs)
        _copy_table(conn_src, conn_dst, _SMTP_CONFIG, insert_new=True, **kwargs)
        _copy_table(conn_src, conn_dst, _AI_CONFIG, insert_new=True, **kwargs)
        _copy_table(conn_src, conn_dst, _PROVIDER_CREDENTIALS, insert_new=True, **kwargs)
        _copy_table(conn_src, conn_dst, _USER_PREFS, insert_new=False, **kwargs)
        _copy_full_replace(conn_src, conn_dst, _AI_PROVIDER_CONFIGS,
                            copy_keys=copy_keys, source_secret_key=source_secret_key,
                            dest_secret_key=dest_secret_key, summary=summary)
        conn_dst.commit()
    finally:
        conn_src.close()
        conn_dst.close()

    summary.secrets_copied = copy_keys
    return summary
