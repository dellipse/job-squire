# Copyright (C) 2026 D. Brandmeyer
# Licensed under the GNU Affero General Public License v3 or later.
"""Regression test for run_search()'s live progress reporting.

Reported again at 0.7.13 (after the "stuck at running forever" fix in
0.7.12): the Getting Started "First Search" step still looked stuck, with no
visible sign of life. Root cause this time isn't a crash -- it's that a real
run can legitimately take a long time (providers.py throttles 60-120s between
every title on a given provider, and providers run one at a time), but
SearchRun.found/detail were only ever written once, at the very end of the
run. So a run that's working correctly but slowly looked identical, from the
outside, to one that was actually stuck: a static "still running" message
with zero information for the whole duration.

Fix: _mark_progress() in app/search.py now commits a live one-line status
(current provider, running total found) after every provider, so a fresh
read of the SearchRun row mid-run shows real, moving state -- not just at
the end.

This test proves the write really lands on disk mid-run (not just held in
the ORM session) by reading the row back through a completely separate
sqlite3 connection from inside a stubbed search_provider(), before the next
provider is even queried.
"""
import os
import sqlite3

import pytest

from app.db_utils import commit
from app.extensions import db
from app.models import ProviderCredential, SearchConfig, SearchRun
from app.search import _run_search_locked


@pytest.fixture
def two_providers_enabled(app):
    """Enable exactly themuse+jobicy and force include_remote on, snapshotting
    and restoring both afterward.

    SearchConfig and ProviderCredential are shared, session-scoped singletons
    (see test_search_settings.py's _login_admin comment on the same pattern) —
    other test files leave them in whatever state their own tests need (e.g.
    test_onboarding.py's remote-jobs-off case sets include_remote=False and
    leaves it that way). Snapshot/restore here so this test's outcome doesn't
    depend on suite ordering, and so it doesn't leak "themuse"/"jobicy"
    enabled=True forward into unrelated tests that run after it.
    """
    with app.app_context():
        cfg = db.session.get(SearchConfig, 1)
        prev_include_remote = cfg.include_remote
        prev_enabled = {pc.provider: pc.enabled for pc in ProviderCredential.query.all()}

        cfg.titles = "Engineer"
        cfg.location = "Boise, ID"
        cfg.country = "US"
        cfg.enabled = True
        cfg.include_remote = True
        for name in ("themuse", "jobicy"):
            pc = ProviderCredential.query.filter_by(provider=name).first()
            if not pc:
                pc = ProviderCredential(provider=name)
                db.session.add(pc)
            pc.enabled = True
        # Nothing else should be enabled, so the run has exactly these two.
        for pc in ProviderCredential.query.filter(
            ProviderCredential.provider.notin_(["themuse", "jobicy"])
        ).all():
            pc.enabled = False
        commit()

    yield

    with app.app_context():
        cfg = db.session.get(SearchConfig, 1)
        cfg.include_remote = prev_include_remote
        for pc in ProviderCredential.query.all():
            pc.enabled = prev_enabled.get(pc.provider, False)
        commit()


def test_progress_is_committed_incrementally_mid_run(app, monkeypatch, two_providers_enabled):
    """A second provider must never start until the first one's contribution
    is already durably visible outside the ORM session -- proving a mid-run
    poll of the Getting Started page would see real progress, not a static
    message, no matter how long the run takes."""
    db_path = os.path.join(app.config["DATA_DIR"], "job-squire.db")

    call_log = []

    def _fake_search_provider(provider, creds, titles, cfg):
        if call_log:
            # A fresh connection, independent of the app's SQLAlchemy
            # session -- this is what a separate page-load/poll would see.
            raw = sqlite3.connect(db_path)
            try:
                row = raw.execute(
                    "SELECT detail, found, status FROM search_runs ORDER BY id DESC LIMIT 1"
                ).fetchone()
            finally:
                raw.close()
            assert row is not None, "no SearchRun row committed yet"
            detail, found, status = row
            assert status == "running"
            assert found == len(call_log), (
                f"expected {len(call_log)} job(s) already committed before "
                f"provider #{len(call_log) + 1} started, got found={found} "
                f"(live detail was {detail!r})"
            )
            assert detail, "detail should never be blank once a provider has run"
        call_log.append(provider)
        return (
            [{"title": f"{provider} job", "company": f"Acme {provider}",
              "location": "Boise, ID"}],
            None,
        )

    monkeypatch.setattr("app.search.search_provider", _fake_search_provider)

    with app.app_context():
        result = _run_search_locked(trigger="manual")
        assert result is not None
        assert result.status == "ok"
        assert len(call_log) == 2  # both providers actually ran
        assert result.found == 2


def test_manual_run_executes_with_automated_search_off(
    app, monkeypatch, two_providers_enabled
):
    """Regression: the "Run first search now" button (trigger="manual") must
    create a SearchRun and execute even when the "Automated search (3x/day)"
    toggle (SearchConfig.enabled) is off. Previously run_search() returned None
    immediately here, so no "running" row was ever created and the Getting
    Started page polled forever -- no stopwatch, no update, no finish."""
    monkeypatch.setattr(
        "app.search.search_provider",
        lambda provider, creds, titles, cfg: ([], None),
    )

    with app.app_context():
        cfg = db.session.get(SearchConfig, 1)
        cfg.enabled = False  # automated schedule OFF
        commit()

        result = _run_search_locked(trigger="manual")
        assert result is not None, "manual run must not be gated on the schedule toggle"
        assert result.status == "ok"


def test_scheduled_run_still_respects_automated_search_toggle(
    app, monkeypatch, two_providers_enabled
):
    """The scheduler must keep honoring SearchConfig.enabled: with automated
    search off, a scheduled trigger is a no-op (returns None, no row)."""
    called = []
    monkeypatch.setattr(
        "app.search.search_provider",
        lambda provider, creds, titles, cfg: called.append(provider) or ([], None),
    )

    with app.app_context():
        cfg = db.session.get(SearchConfig, 1)
        cfg.enabled = False
        commit()

        result = _run_search_locked(trigger="scheduled")
        assert result is None, "scheduled run must skip when automated search is off"
        assert called == [], "no provider should be queried on a skipped scheduled run"


def test_progress_message_names_current_provider(app, monkeypatch, two_providers_enabled):
    """The live detail shown mid-run should be a human-readable one-liner
    naming the provider currently being searched, not a generic placeholder,
    so a poll during a long run actually tells the user something."""
    seen_details = []

    def _fake_search_provider(provider, creds, titles, cfg):
        with db.session.no_autoflush:
            row = SearchRun.query.order_by(SearchRun.id.desc()).first()
            seen_details.append(row.detail)
        return [], None

    monkeypatch.setattr("app.search.search_provider", _fake_search_provider)

    with app.app_context():
        _run_search_locked(trigger="manual")

    assert len(seen_details) == 2
    assert any("themuse" in d for d in seen_details)
    assert any("jobicy" in d for d in seen_details)
