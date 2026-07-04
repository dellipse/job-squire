# Copyright (C) 2026 D. Brandmeyer
# Licensed under the GNU Affero General Public License v3 or later.
"""Shared pytest fixtures.

The Flask app reads all of its configuration from environment variables at
``create_app()`` time, so the fixtures below set a safe, self-contained test
environment (temp data dir, throwaway SECRET_KEY, seeded test passwords) BEFORE
importing and building the app. Nothing here touches a real database or a real
credential.
"""
import os
import shutil
import tempfile

import pytest

# Test credentials — deliberately weak; only ever used against the temp DB.
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "admin-test-pw"
USER_USERNAME = "seeker"
USER_PASSWORD = "user-test-pw"


@pytest.fixture(scope="session")
def app():
    """A fully-initialised app on a temp SQLite DB.

    Session-scoped because the app's Flask extensions (db, login, csrf, limiter)
    are module-level singletons — building the app once avoids re-initialising
    them repeatedly.

    IMPORTANT: this fixture deliberately does NOT push a long-lived app context.
    Flask-Login caches the current user on ``g`` (bound to the app context), so a
    persistent context would leak the first logged-in user into every later
    request. Tests that need a context get one per-test: the test client pushes
    its own per-request context, and DB tests use the ``app_context`` fixture.
    """
    tmp = tempfile.mkdtemp(prefix="jobsquire-test-")
    os.environ.update(
        SECRET_KEY="test-secret-key-not-for-production-use",
        DATA_DIR=tmp,
        ADMIN_USERNAME=ADMIN_USERNAME,
        ADMIN_PASSWORD=ADMIN_PASSWORD,
        USER_USERNAME=USER_USERNAME,
        USER_PASSWORD=USER_PASSWORD,
        SESSION_COOKIE_SECURE="false",
        # Keep the scheduler/timezone lookups deterministic and offline.
        SCHEDULE_TZ="America/Los_Angeles",
    )

    from app import create_app

    application = create_app()
    application.config.update(TESTING=True)
    try:
        yield application
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def app_context(app):
    """Push a fresh app context for a single test (for direct DB / model work)."""
    with app.app_context():
        yield app


@pytest.fixture
def client(app):
    """A test client with CSRF disabled so form POSTs don't need a token.

    CSRF protection itself is Flask-WTF's concern and is exercised in the real
    app; disabling it here lets the auth tests focus on login/redirect/rate-limit
    logic without scraping a token out of every GET response.
    """
    app.config["WTF_CSRF_ENABLED"] = False
    return app.test_client()


@pytest.fixture(autouse=True)
def limiter_reset(app):
    """Clear the shared rate-limiter storage before every test.

    Flask-Limiter uses in-memory storage that otherwise persists across tests in
    the same session, which would make the rate-limit test order-dependent.
    """
    from app.extensions import limiter
    with app.app_context():
        try:
            limiter.reset()
        except Exception:  # noqa: BLE001 - storage backend may not support reset
            pass
    yield
