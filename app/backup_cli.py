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
"""Container-side backup entrypoint for job-squire-cli.

Since /data may be a named Docker volume rather than a host bind mount
(docs/PLAN-deployment-modes.md), the CLI cannot always read a WAL-safe
snapshot of the database straight off the host filesystem the way it once
did. Instead it runs this module inside the running container --
`docker exec <container> python -m app.backup_cli` (or the `podman`
equivalent) -- and reads the archive bytes directly off the exec's stdout
pipe. See job_squire_cli/ops/backup.py's docstring for the full picture.

Deliberately not a Flask route: this only ever runs as a one-shot process
inside the container the CLI already controls via exec, so there is no
need for the app factory, a request context, or authentication -- the
operator already has to be able to run `docker exec` against this
container to reach it at all, which is an equal or higher bar than the
web UI's own admin-only backup-download route.

Writes nothing to disk itself; the returned bytes are the same
`build_backup_archive` payload the in-app one-click download route
produces, minus the container's own copy of `.env` (`include_env=False`)
-- the CLI already has that file directly from the host (`data/.env`
sits outside the named volume specifically so `env_file:` can read it;
see ops/compose.py), so asking the container to duplicate it here would
just be two copies of the same secret to keep in sync for no benefit.
"""
import os
import sys

from .backup import build_backup_archive


def main() -> int:
    data_dir = os.environ.get("DATA_DIR", "/data")
    upload_dir = os.path.join(data_dir, "uploads")
    try:
        _filename, payload = build_backup_archive(data_dir, upload_dir, include_env=False)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 -- surface any failure to the CLI's stderr, not a traceback dump
        print(f"Backup snapshot failed: {exc}", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
