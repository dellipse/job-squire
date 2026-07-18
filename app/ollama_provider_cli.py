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
"""Container-side entrypoint for `job-squire ollama setup` (job-squire-cli).

Same reasoning as `app/backup_cli.py`: since /data is a named Docker volume
rather than a host bind mount, job-squire-cli cannot write the
`ai_provider_configs` row by opening `<instance_root>/data/job-squire.db`
directly from the host anymore -- that path only ever held the live
database back when /data was still bind-mounted (see ops/compose.py and
ops/paths.py). This module runs the exact write inside the running
container instead -- `docker exec <container> python -m
app.ollama_provider_cli` (or the `podman` equivalent), fed a JSON payload
on stdin -- where `os.environ["DATA_DIR"]` (default `/data`) correctly
resolves to the volume's mount point. See
job_squire_cli/ops/ollama_assist.py's `write_provider_config()`, which
used to do this write itself via a raw host-side `sqlite3.connect()` and
now just constructs this payload and execs into the container instead.

Deliberately not a Flask route, same as backup_cli.py: this only ever runs
as a one-shot process inside a container the CLI already controls via
exec, so there's no need for the app factory, a request context, or
authentication.

Protocol: reads one JSON object from stdin --

    {"base_url": str, "triage_model": str, "analysis_model": str,
     "num_ctx": int | null, "rank": int | null, "enabled": bool,
     "enable_automatic_features": bool}

-- writes one JSON object to stdout on success --

    {"automatic_features_enabled": bool}

-- or a plain-text error to stderr and a nonzero exit on failure. Uses raw
sqlite3 directly against `<DATA_DIR>/job-squire.db`, the same way
ops/secrets_copy.py and the old write_provider_config() did, rather than
importing the app factory/SQLAlchemy -- this only ever touches one table
via a handful of statements, and the app package's own migrations already
guarantee the schema this expects exists (this module never creates
tables itself, matching backup_cli.py's read-only-of-schema stance).
"""
import json
import os
import sqlite3
import sys

PROVIDER_KEY = "ollama"


class OllamaProviderCliError(RuntimeError):
    """Raised for a malformed request or a write failure."""


def write_provider_row(db_path: str, payload: dict) -> bool:
    """Same statements job_squire_cli's write_provider_config() used to run
    directly against a host path -- moved here unchanged except for where
    the database file is found. Returns whether ai_config.api_enabled was
    actually flipped (see that function's original docstring for why a
    missing/unseeded ai_config row only warns rather than raising)."""
    if not os.path.exists(db_path):
        raise OllamaProviderCliError(
            f"Database not found at {db_path} inside the container. This shouldn't happen if the "
            f"container is up -- the app creates its schema on first boot."
        )
    base_url = payload["base_url"]
    triage_model = payload["triage_model"]
    analysis_model = payload["analysis_model"]
    num_ctx = payload.get("num_ctx")
    rank = payload.get("rank")
    enabled = payload.get("enabled", True)
    enable_automatic_features = payload.get("enable_automatic_features", True)

    conn = sqlite3.connect(db_path)
    try:
        existing = conn.execute(
            "SELECT id, rank FROM ai_provider_configs WHERE provider = ?", (PROVIDER_KEY,)
        ).fetchone()

        if rank is None:
            if existing is not None:
                rank = existing[1]
            else:
                max_rank = conn.execute("SELECT MAX(rank) FROM ai_provider_configs").fetchone()[0]
                rank = (max_rank or 0) + 1

        try:
            if existing is not None:
                conn.execute(
                    "UPDATE ai_provider_configs SET rank = ?, label = ?, base_url = ?, model = ?, "
                    "triage_model = ?, num_ctx = ?, use_for_triage = 1, use_for_analysis = 1, "
                    "enabled = ? WHERE provider = ?",
                    (rank, "Ollama (local)", base_url, analysis_model, triage_model, num_ctx,
                     int(enabled), PROVIDER_KEY),
                )
            else:
                conn.execute(
                    "INSERT INTO ai_provider_configs (rank, provider, label, api_key_enc, base_url, "
                    "model, triage_model, num_ctx, use_for_triage, use_for_analysis, thinking_mode, "
                    "enabled) VALUES (?, ?, ?, '', ?, ?, ?, ?, 1, 1, NULL, ?)",
                    (rank, PROVIDER_KEY, "Ollama (local)", base_url, analysis_model, triage_model,
                     num_ctx, int(enabled)),
                )
        except sqlite3.OperationalError as exc:
            if "num_ctx" in str(exc):
                raise OllamaProviderCliError(
                    f"{db_path} has no num_ctx column on ai_provider_configs yet -- this instance's "
                    f"job-squire image predates that migration. Update the instance "
                    f"(`job-squire update NAME`) so it boots at least once with the newer schema, "
                    f"then re-run `job-squire ollama setup`."
                ) from exc
            raise

        automatic_features_enabled = False
        if enable_automatic_features:
            try:
                cur = conn.execute("UPDATE ai_config SET api_enabled = 1 WHERE id = 1")
                automatic_features_enabled = cur.rowcount > 0
            except sqlite3.OperationalError:
                automatic_features_enabled = False

        conn.commit()
    finally:
        conn.close()

    return automatic_features_enabled


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
        automatic_features_enabled = write_provider_row(db_path, payload)
    except OllamaProviderCliError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 -- surface any failure to the CLI's stderr, not a traceback dump
        print(f"Writing the Ollama provider row failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"automatic_features_enabled": automatic_features_enabled}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
