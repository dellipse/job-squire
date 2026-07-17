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
"""Retry helper for transient SQLite errors.

Job Squire's three containers (web, worker, mcp) share one SQLite database
via a bind-mounted host data directory (see CLAUDE.md). On some container
runtimes that bind mount is backed by a virtualized filesystem bridge (e.g.
OrbStack or Docker Desktop's file-sharing layer on macOS), which can
occasionally surface a transient `disk I/O error` or `database is locked`
from SQLite's WAL-mode locking under concurrent access -- with nothing
actually wrong with the data or the query. That's environmental flakiness,
not a bug to fix outright, so instead of letting it surface as a raw 500,
retry the operation a couple of times with a short backoff before giving up.
"""
import logging
import time

from sqlalchemy.exc import OperationalError

from .extensions import db

log = logging.getLogger(__name__)

_TRANSIENT_MARKERS = ("disk i/o error", "database is locked", "database is busy")


def _is_transient(exc: OperationalError) -> bool:
    return any(marker in str(exc).lower() for marker in _TRANSIENT_MARKERS)


def with_db_retry(fn, *, attempts: int = 3, base_delay: float = 0.15):
    """Call `fn()`, retrying up to `attempts` times on a transient SQLite error.

    Rolls back the session between attempts so the next try starts from a
    clean transaction. Re-raises immediately on any non-transient
    OperationalError, or once attempts are exhausted -- callers still see a
    real failure if the underlying storage is actually broken, this just
    absorbs one-off hiccups.
    """
    last_exc: OperationalError | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except OperationalError as exc:
            last_exc = exc
            if attempt == attempts or not _is_transient(exc):
                raise
            log.warning(
                "with_db_retry: transient SQLite error (attempt %d/%d), retrying: %s",
                attempt, attempts, exc,
            )
            db.session.rollback()
            time.sleep(base_delay * attempt)
    raise last_exc  # pragma: no cover — unreachable, loop always returns or raises


def commit(*, attempts: int = 3, base_delay: float = 0.15) -> None:
    """`db.session.commit()`, retried like everything else in this module.

    Use this in place of a bare `db.session.commit()` everywhere in the app.
    Centralizing it here means new code gets transient-error protection for
    free, without every call site needing to remember to import and wrap
    with `with_db_retry` itself -- and it gives us one place to grep for
    (`db.session.commit(` outside this file is a regression -- see
    tests/test_no_raw_commits.py) if this convention needs to change again.
    """
    with_db_retry(db.session.commit, attempts=attempts, base_delay=base_delay)
