# Copyright (C) 2026 D. Brandmeyer
# Licensed under the GNU Affero General Public License v3 or later.
"""Tests for POST /settings/provider/<provider>/pull.

Behavior under test:
  * the route returns immediately (redirect to the task-status page) instead
    of blocking on search_provider() -- the fix for the gunicorn-timeout 500
    a multi-title config used to trigger, since search_provider() throttles
    between titles for tens of seconds at a time.
  * the actual search/ingest work still happens, just in a background thread:
    the poll endpoint eventually reports "done" with the fetched/created/
    skipped counts, and the SearchRun row is updated to "ok".
  * a search_provider() error is surfaced the same way (SearchRun "error",
    poll endpoint "error") instead of silently vanishing.
"""
import time

import pytest

from app.extensions import db
from app.models import SearchRun
from tests.conftest import login_admin as _login_admin

PULL_URL = "/settings/provider/jobicy/pull"


@pytest.fixture(autouse=True)
def clean_search_runs(app_context):
    """SearchRun rows persist across tests on the session-scoped app/DB."""
    def _reset():
        SearchRun.query.delete()
        db.session.commit()
    _reset()
    yield
    _reset()


def _poll_until_done(client, run_id, task, timeout=5.0):
    poll_url = f"/ai/task/{run_id}/poll?task={task}"
    deadline = time.monotonic() + timeout
    data = None
    while time.monotonic() < deadline:
        resp = client.get(poll_url)
        data = resp.get_json()
        if data.get("status") in ("done", "error"):
            return data
        time.sleep(0.05)
    raise AssertionError(f"task {run_id} did not finish within {timeout}s: {data}")


def test_pull_returns_immediately_instead_of_blocking_on_the_search(client, monkeypatch):
    """The original bug: search_provider() throttles between titles for tens of
    seconds, and the route used to call it inline on the request thread. Confirm
    the request now comes back fast even when search_provider() is slow, using a
    real background thread (not a faked one) so this actually exercises the fix."""
    _login_admin(client)

    def _slow_search_provider(provider, creds, titles, cfg):
        time.sleep(0.3)
        return [{"title": "Warehouse Associate", "company": "Acme", "source": "jobicy",
                  "external_id": "job-1", "url": "https://example.com/job-1"}], None

    monkeypatch.setattr("app.settings.search_provider", _slow_search_provider)
    monkeypatch.setattr("app.settings.ingest_jobs", lambda results, created_by: ([1], 0))

    started = time.monotonic()
    resp = client.post(PULL_URL, follow_redirects=False)
    elapsed = time.monotonic() - started

    assert resp.status_code == 302
    assert "/ai/task/" in resp.headers["Location"]
    assert elapsed < 0.3, f"request blocked for {elapsed}s instead of returning immediately"

    run_id = resp.headers["Location"].rsplit("/ai/task/", 1)[1].split("/")[0]
    data = _poll_until_done(client, run_id, task="pull_jobicy")

    assert data["status"] == "done"
    assert data["result"]["found"] == 1
    assert data["result"]["created"] == 1
    assert data["result"]["skipped"] == 0


def test_pull_updates_search_run_to_ok(client, app_context, monkeypatch):
    _login_admin(client)
    monkeypatch.setattr(
        "app.settings.search_provider",
        lambda provider, creds, titles, cfg: ([{"title": "x"}], None),
    )
    monkeypatch.setattr("app.settings.ingest_jobs", lambda results, created_by: ([1], 0))

    resp = client.post(PULL_URL, follow_redirects=False)
    run_id = resp.headers["Location"].rsplit("/ai/task/", 1)[1].split("/")[0]
    _poll_until_done(client, run_id, task="pull_jobicy")

    run = SearchRun.query.filter_by(providers="jobicy").one()
    assert run.status == "ok"
    assert run.found == 1
    assert run.created == 1
    assert run.skipped == 0
    assert run.finished_at is not None


def test_pull_error_is_surfaced_via_poll_and_search_run(client, app_context, monkeypatch):
    _login_admin(client)
    monkeypatch.setattr(
        "app.settings.search_provider",
        lambda provider, creds, titles, cfg: ([], "jobicy: boom"),
    )

    resp = client.post(PULL_URL, follow_redirects=False)
    run_id = resp.headers["Location"].rsplit("/ai/task/", 1)[1].split("/")[0]
    data = _poll_until_done(client, run_id, task="pull_jobicy")

    assert data["status"] == "error"
    assert "boom" in data["error"]

    run = SearchRun.query.filter_by(providers="jobicy").one()
    assert run.status == "error"
    assert "boom" in run.detail
