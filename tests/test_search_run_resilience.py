# Copyright (C) 2026 D. Brandmeyer
# Licensed under the GNU Affero General Public License v3 or later.
"""Regression test for run_search()'s crash resilience.

Bug (reported 2026-07-17): the Getting Started "First Search" step could get
stuck claiming a search was running forever, with no results and no error
shown.

Root cause: _run_search_locked() creates its SearchRun row with
status="running" up front, then only finalizes status to "ok"/"error" at the
very end of the function. Nothing in between was wrapped in a try/except, so
any exception raised downstream -- most plausibly ingest_jobs()'s commit()
exhausting its SQLite retry budget under the 3-container shared /data volume
(see app/db_utils.py) -- propagated straight out of the daemon thread that
runs searches (settings_run/_bg_search in app/main.py). Python's default
thread exception handling swallows that silently: nothing crashes, nothing
surfaces to the UI, but the SearchRun row is left at status="running"
forever.

Fix: wrap the body of _run_search_locked in a try/except that always
finalizes the row, even on failure.
"""
from app.db_utils import commit
from app.extensions import db
from app.models import SearchConfig, SearchRun
from app.search import _run_search_locked


def _configure_search(app):
    """Point the singleton SearchConfig at a valid, enabled search."""
    with app.app_context():
        cfg = db.session.get(SearchConfig, 1)
        cfg.titles = "Engineer"
        cfg.location = "Boise, ID"
        cfg.country = "US"
        cfg.enabled = True
        commit()


def test_mid_run_exception_finalizes_row_as_error(app, monkeypatch):
    """A downstream exception must never leave a SearchRun stuck at "running"."""
    _configure_search(app)

    # No real provider credentials are enabled in this test DB, so the
    # provider loop is a no-op regardless -- but stub search_provider too so
    # this stays true even if a future test seeds a default enabled provider.
    monkeypatch.setattr("app.search.search_provider", lambda *a, **k: ([], None))

    def _boom(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr("app.search.ingest_jobs", _boom)

    with app.app_context():
        result = _run_search_locked(trigger="manual")
        assert result is None  # failure path returns None, same as a normal early-out

        run = SearchRun.query.order_by(SearchRun.id.desc()).first()
        assert run is not None, "a SearchRun row should still exist"
        assert run.status == "error"
        assert run.status != "running"  # the actual regression: must not get stuck here
        assert run.finished_at is not None
        assert "RuntimeError" in run.detail


def test_normal_run_still_finalizes_ok(app, monkeypatch):
    """Control case: an unremarkable run (no providers, nothing found) still
    finalizes cleanly through the same code path, confirming the new
    try/except didn't change happy-path behavior."""
    _configure_search(app)
    monkeypatch.setattr("app.search.search_provider", lambda *a, **k: ([], None))

    with app.app_context():
        result = _run_search_locked(trigger="manual")
        assert result is not None
        assert result.status == "ok"
        assert result.finished_at is not None
