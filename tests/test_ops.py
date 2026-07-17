# Copyright (C) 2026 D. Brandmeyer
# Licensed under the GNU Affero General Public License v3 or later.
"""Tests for operational health surfaces added for the 2026-07 ops gap analysis.

app/worker.py touches DATA_DIR/.worker_heartbeat on startup and on its own
schedule, independent of the search cron jobs. app/main.py's
_worker_heartbeat_status() reads that file to decide whether the worker
container looks alive, and that result is what backs both the Dashboard
banner and the Settings > History status line. These tests exercise the
reader directly against a real (temp) DATA_DIR rather than mocking the
filesystem, since the whole point is "does this file's mtime get interpreted
correctly."
"""
import os
import time

from app.main import _worker_heartbeat_status


def _heartbeat_path(app):
    return os.path.join(app.config["DATA_DIR"], ".worker_heartbeat")


def _login(client, username="admin", password="admin-test-pw"):
    return client.post(
        "/login", data={"username": username, "password": password}, follow_redirects=False,
    )


def test_touch_heartbeat_writes_readable_timestamp(app_context, monkeypatch):
    """app.worker._touch_heartbeat() (the writer side) produces a file
    _worker_heartbeat_status() (the reader side, used by the Docker healthcheck
    and the in-app banner) recognizes as fresh. Exercises the real writer
    rather than hand-writing the file, so the two ends of this contract are
    checked together.
    """
    from app import worker

    monkeypatch.setenv("DATA_DIR", app_context.config["DATA_DIR"])
    path = _heartbeat_path(app_context)
    if os.path.exists(path):
        os.remove(path)

    worker._touch_heartbeat()

    assert os.path.exists(path)
    status = _worker_heartbeat_status()
    assert status["stale"] is False
    assert status["last_seen"] is not None


def test_missing_heartbeat_is_stale(app_context):
    """No heartbeat file at all (worker never started, or a fresh install) reports stale."""
    path = _heartbeat_path(app_context)
    if os.path.exists(path):
        os.remove(path)

    status = _worker_heartbeat_status()
    assert status["stale"] is True
    assert status["last_seen"] is None


def test_fresh_heartbeat_is_not_stale(app_context):
    """A heartbeat written moments ago is not stale."""
    path = _heartbeat_path(app_context)
    with open(path, "w") as f:
        f.write(str(int(time.time())))

    status = _worker_heartbeat_status()
    assert status["stale"] is False
    assert status["last_seen"] is not None


def test_old_heartbeat_is_stale(app_context):
    """A heartbeat older than the threshold reports stale, with its timestamp preserved."""
    path = _heartbeat_path(app_context)
    with open(path, "w") as f:
        f.write("0")
    old_mtime = time.time() - 3600  # 1 hour ago
    os.utime(path, (old_mtime, old_mtime))

    status = _worker_heartbeat_status(max_age_seconds=900)
    assert status["stale"] is True
    assert status["last_seen"] is not None


def test_custom_threshold_is_respected(app_context):
    """A heartbeat within a wider threshold is not flagged stale."""
    path = _heartbeat_path(app_context)
    with open(path, "w") as f:
        f.write("0")
    old_mtime = time.time() - 120  # 2 minutes ago
    os.utime(path, (old_mtime, old_mtime))

    assert _worker_heartbeat_status(max_age_seconds=60)["stale"] is True
    assert _worker_heartbeat_status(max_age_seconds=300)["stale"] is False


def test_dashboard_and_settings_render_worker_status(client, app):
    """Smoke test: the Dashboard banner and Settings > History status line
    (new Jinja blocks) render without a template error, both when the worker
    looks stale and when it looks healthy.
    """
    # test_migrations.py's `mdb` fixture intentionally drop_all()/create_all()s
    # the shared session-scoped DB as part of exercising the migration path,
    # which wipes the seeded accounts if this test runs later in the same
    # session. Re-seed from the same env vars conftest.py used originally so
    # this test doesn't depend on suite ordering.
    from app import _seed_users
    from app.extensions import db
    from app.models import OnboardingState
    with app.app_context():
        _seed_users(app)
        # This test checks the worker-status banner on "/", not the Getting
        # Started walkthrough — dismiss it so a fresh checklist doesn't
        # force-redirect "/" away from the dashboard (see app/onboarding.py).
        state = db.session.get(OnboardingState, 1)
        if state is None:
            state = OnboardingState(id=1)
            db.session.add(state)
        state.dismissed = True
        db.session.commit()

    resp = _login(client)
    assert resp.status_code == 302

    path = os.path.join(app.config["DATA_DIR"], ".worker_heartbeat")
    if os.path.exists(path):
        os.remove(path)

    # Stale/missing heartbeat.
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Automated search worker isn" in resp.data

    resp = client.get("/settings")
    assert resp.status_code == 200
    assert b"Not responding." in resp.data

    # Healthy heartbeat.
    with open(path, "w") as f:
        f.write(str(int(time.time())))

    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Automated search worker isn" not in resp.data

    resp = client.get("/settings")
    assert resp.status_code == 200
    assert b"Not responding." not in resp.data
