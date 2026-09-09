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
"""Importing basic settings from an existing instance into a new one.

Two independent sources feed the import, because Job Squire itself splits
config the same way (see app/deploy.py's module docstring): environment variables in
`data/.env` for deployment shape, and database rows for everything the
running app can change on the fly.

  - Schedule hours and timezone are `data/.env` variables
    (SCHEDULE_TZ/SCHEDULE_WEEKDAY_HOURS/SCHEDULE_WEEKEND_HOURS/
    SCHEDULE_MINUTE) -- `read_schedule_env` reads them as plain text, no
    database involved, and lifecycle.create_instance applies them to the
    *new* instance's env before it ever boots.
  - Search titles/location/radius, enabled providers, SMTP host/port, AI
    provider selection, and interface preferences live in the database
    (app/models.py). `/data` is a named Docker volume, not a host bind
    mount (ops/compose.py), so `copy_db_settings` can no longer open either
    instance's `job-squire.db` directly from the host -- the actual reads
    (from the source instance) and writes (to the destination instance)
    each run inside that instance's own running container via
    `docker exec`/`podman exec` (`app/secrets_copy_cli.py`, fed a JSON
    request on stdin), mirroring ops/backup.py's `_snapshot_container_data`
    and ops/mcp_token.py's `_exec`. This package still intentionally does
    not depend on Flask/SQLAlchemy/the app package at all (an operator
    running the CLI has not necessarily cloned the app repo, and the app's
    stack is meant to live inside the container, not on the host) -- so the
    column allowlists below, and their mirror in app/secrets_copy_cli.py,
    are hand-maintained against app/models.py rather than imported from it.
    Every table is read defensively (a missing table or column produces a
    warning in ImportSummary, not a crash), consistent with "additive,
    never assumed" migrations elsewhere in this project.

Secrets are excluded by default (CLAUDE.md: "ALL stored secrets encrypted
... never plaintext"). `copy_db_settings(..., copy_keys=True)` is the
explicit opt-in, and because every instance gets its own independently
random SECRET_KEY, copying an *encrypted*
column verbatim would not decrypt at the destination -- so opting in
decrypts with the source instance's SECRET_KEY and re-encrypts with the
destination's, using the HKDF-SHA256 -> Fernet derivation mirrored from
app/crypto.py in ops/crypto_mirror.py (shared with ops/mcp_token.py, so
that mirrored contract lives in one place -- see that module's docstring
for why it isn't imported from app/crypto.py directly).
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import compose, paths
from .crypto_mirror import decrypt as _mirror_decrypt, encrypt as _mirror_encrypt

Runner = Callable[..., "subprocess.CompletedProcess[str]"]

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
#
# Since the 2026-07-17 volume migration, `/data` is a named Docker volume,
# not a host bind mount (ops/compose.py) -- `paths.sqlite_db_path(root)` has
# not pointed at a real host file since then. The actual reads/writes below
# run inside each instance's own running container via `docker exec`/`podman
# exec` (`app/secrets_copy_cli.py`, fed a JSON request on stdin), mirroring
# ops/backup.py's `_snapshot_container_data` and ops/mcp_token.py's `_exec`:
# a `dump` request against the *source* container returns each table's raw
# rows (still-encrypted secret columns included, verbatim), and an `apply`
# request against the *destination* container performs the same
# INSERT/UPDATE/DELETE upsert logic that used to run directly against a
# local `conn_dst`. All re-encryption (`reencrypt`, above) and the resulting
# per-column warnings stay right here on the host -- `app/secrets_copy_cli.py`
# only ever sees values already finalized for its column, never either
# instance's SECRET_KEY.

_SECRETS_COPY_CLI_ARGV = ["python3", "-m", "app.secrets_copy_cli"]

# (spec, insert_new) -- insert_new mirrors the old _copy_table's own
# parameter: False only for `users`, whose rows are only ever created by
# the app's own account seeding, never by this import.
_UPSERT_SPECS = (
    (_SEARCH_CONFIG, True),
    (_SMTP_CONFIG, True),
    (_AI_CONFIG, True),
    (_PROVIDER_CREDENTIALS, True),
    (_USER_PREFS, False),
)


def _columns_for(cols: tuple[str, ...], secret_cols: tuple[str, ...], copy_keys: bool) -> tuple[str, ...]:
    return cols + (secret_cols if copy_keys else ())


def _row_values(row: dict, cols: tuple[str, ...], secret_cols: tuple[str, ...], *,
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


def _exec(runtime: str, container_name: str, payload: dict, run: Runner) -> dict:
    """`docker/podman exec -i <container> python -m app.secrets_copy_cli`,
    fed `payload` as JSON on stdin -- see app/secrets_copy_cli.py's module
    docstring for the "dump"/"apply" protocol."""
    argv = [compose.runtime_binary(runtime), "exec", "-i", container_name, *_SECRETS_COPY_CLI_ARGV]
    try:
        result = run(argv, input=json.dumps(payload), capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SecretsCopyError(f"Failed to reach the settings store inside {container_name!r}: {exc}") from exc
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise SecretsCopyError(f"Settings copy operation inside {container_name!r} failed: {stderr}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SecretsCopyError(
            f"Settings copy operation inside {container_name!r} returned an unexpected response: "
            f"{result.stdout!r}"
        ) from exc


def copy_db_settings(*, source_root: Path, dest_root: Path, source_secret_key: str,
                      dest_secret_key: str, source_runtime: str, source_container_name: str,
                      dest_runtime: str, dest_container_name: str, copy_keys: bool = False,
                      run: Runner = subprocess.run) -> ImportSummary:
    """Copy basic (and, opt-in, secret) settings from one instance's
    database into another's.

    Both instances' containers must be running -- `/data` is a named
    Docker volume, only reachable via `docker/podman exec` while the
    container is up (see this module's docstring above), so there is no
    more "stopped but still readable" case a host path used to allow.
    A source that isn't running is treated the same as the old "database
    not found" case (warn, import nothing) since it's the more common,
    less surprising failure mode for a source instance the operator simply
    hasn't started; a destination that isn't running is a hard error, same
    as the old "destination database not found" -- the destination is the
    instance this call is actually supposed to modify.
    """
    summary = ImportSummary()

    source_state = compose.inspect_state(source_runtime, source_container_name, run=run)
    if source_state is None or source_state.get("Status") != "running":
        summary.warnings.append(
            f"Source instance's container ({source_container_name!r}) isn't running -- nothing imported. "
            f"Start it first (`job-squire start`) to import from it."
        )
        return summary

    dest_state = compose.inspect_state(dest_runtime, dest_container_name, run=run)
    if dest_state is None or dest_state.get("Status") != "running":
        raise SecretsCopyError(
            f"Destination instance's container ({dest_container_name!r}) must be running to import "
            f"settings into it -- its database now lives in a Docker volume, only reachable while the "
            f"container is up."
        )

    all_specs = (*(spec for spec, _ in _UPSERT_SPECS), _AI_PROVIDER_CONFIGS)
    dump_tables = []
    for table, key_col, cols, secret_cols in all_specs:
        columns = _columns_for(cols, secret_cols, copy_keys)
        read_cols = columns if key_col is None else (key_col, *columns)
        dump_tables.append({"table": table, "columns": list(read_cols)})

    dump_response = _exec(source_runtime, source_container_name, {"op": "dump", "tables": dump_tables}, run)
    dumped = dump_response.get("tables", {})

    apply_tables = []
    for (table, key_col, cols, secret_cols), insert_new in _UPSERT_SPECS:
        rows = dumped.get(table)
        if rows is None:
            summary.warnings.append(f"{table}: not found in the source database (skipped).")
            continue
        applied_rows = []
        for raw_row in rows:
            values = _row_values(
                raw_row, cols, secret_cols, copy_keys=copy_keys,
                source_secret_key=source_secret_key, dest_secret_key=dest_secret_key,
                summary=summary, table=table,
            )
            if key_col is not None:
                values[key_col] = raw_row[key_col]
            applied_rows.append(values)
        strategy = "singleton" if key_col is None else ("by_key" if insert_new else "update_only_by_key")
        apply_tables.append({"table": table, "strategy": strategy, "key_col": key_col, "rows": applied_rows})
        summary.tables_copied.append(table)

    # ai_provider_configs: full replace, no natural unique key across a
    # provider chain (the same provider type can appear twice at different
    # ranks) -- see _AI_PROVIDER_CONFIGS's own comment above.
    table, _key_col, cols, secret_cols = _AI_PROVIDER_CONFIGS
    rows = dumped.get(table)
    if rows is None:
        summary.warnings.append(f"{table}: not found in the source database (skipped).")
    else:
        applied_rows = [
            _row_values(
                raw_row, cols, secret_cols, copy_keys=copy_keys,
                source_secret_key=source_secret_key, dest_secret_key=dest_secret_key,
                summary=summary, table=table,
            )
            for raw_row in rows
        ]
        apply_tables.append({"table": table, "strategy": "full_replace", "key_col": None, "rows": applied_rows})
        summary.tables_copied.append(table)

    if apply_tables:
        _exec(dest_runtime, dest_container_name, {"op": "apply", "tables": apply_tables}, run)

    summary.secrets_copied = copy_keys
    return summary
