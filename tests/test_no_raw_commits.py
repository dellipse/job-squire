# Copyright (C) 2026 D. Brandmeyer
# Licensed under the GNU Affero General Public License v3 or later.
"""Regression guard for the commit()/expire_on_commit=False convention
(docs: app/db_utils.py, app/extensions.py).

Every commit in the app is supposed to go through app.db_utils.commit()
instead of a bare db.session.commit(), so that transient SQLite errors
(disk I/O error / database is locked -- see app/db_utils.py's own docstring)
get retried instead of surfacing as a raw 500. It's an easy convention to
accidentally break by pasting a bare db.session.commit() into new code, so
this test greps the source tree for that pattern and fails loudly if it
finds one anywhere other than db_utils.py's own definition (and this test
file's docstring, above).
"""
import re
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent / "app"

# db_utils.py is where commit() is defined in terms of db.session.commit --
# that one occurrence is the whole point of the helper, not a violation.
_ALLOWED_FILES = {"db_utils.py"}

_RAW_COMMIT_RE = re.compile(r"db\.session\.commit\s*\(")


def _iter_py_files():
    for path in sorted(APP_DIR.rglob("*.py")):
        if path.name in _ALLOWED_FILES:
            continue
        yield path


def test_no_raw_db_session_commit_outside_db_utils():
    offenders = []
    for path in _iter_py_files():
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            # Skip comments and docstring-ish lines quoting the pattern for
            # explanatory purposes (e.g. extensions.py's expire_on_commit
            # rationale) -- only flag it as *code*.
            if stripped.startswith("#"):
                continue
            if _RAW_COMMIT_RE.search(line) and "`db.session.commit()`" not in line:
                offenders.append(f"{path.relative_to(APP_DIR.parent)}:{lineno}: {stripped}")

    assert not offenders, (
        "Found raw db.session.commit() call(s) outside app/db_utils.py. "
        "Use app.db_utils.commit() instead so transient SQLite errors get "
        "retried (see app/db_utils.py's docstring):\n" + "\n".join(offenders)
    )
