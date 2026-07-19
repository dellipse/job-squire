# Copyright (C) 2026 D. Brandmeyer
# Licensed under the GNU Affero General Public License v3 or later.
"""Tests for POST /settings/smtp and POST /settings/test-email.

Behavior under test:
  * saving persists all fields, and a blank password keeps the existing
    encrypted one rather than clearing it.
  * both routes honor a relative `next` form field (so the Getting Started
    notifications step can reuse them and land back on itself) and ignore
    an absolute one, same `_safe_next` contract already covered for
    /settings/search in test_onboarding.py::TestSafeNext.
  * the test-email route refuses to send without a saved host/recipient,
    and reports success/failure from send_email without ever making a
    real network call (send_email is monkeypatched throughout).
"""
import pytest

from app.extensions import db
from app.models import SmtpConfig

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "admin-test-pw"

SMTP_URL = "/settings/smtp"
TEST_EMAIL_URL = "/settings/test-email"


@pytest.fixture(autouse=True)
def clean_smtp_config(app_context):
    """Reset the SmtpConfig singleton before and after each test — the app
    fixture and its DB are session-scoped, so a row saved by one test would
    otherwise still be there (host/to_addr set) when the next test expects
    a blank slate, e.g. the "warns without host/recipient" case."""
    def _reset():
        row = db.session.get(SmtpConfig, 1)
        if row:
            db.session.delete(row)
            db.session.commit()
    _reset()
    yield
    _reset()


def _login_admin(client, app):
    # See test_search_settings.py's identical helper for why re-seeding is
    # needed regardless of suite ordering.
    from app import _seed_users
    with app.app_context():
        _seed_users(app)
    return client.post(
        "/login",
        data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
        follow_redirects=False,
    )


def _post_smtp(client, **fields):
    data = {
        "enabled": "on", "host": "mail.smtp2go.com", "port": "587",
        "username": "smtp2go-user", "password": "s3cret", "from_addr": "me@example.com",
        "to_addr": "me@example.com",
    }
    data.update(fields)
    return client.post(SMTP_URL, data=data, follow_redirects=True)


def _current_smtp(app):
    with app.app_context():
        return db.session.get(SmtpConfig, 1)


def test_save_persists_all_fields(client, app):
    _login_admin(client, app)
    r = _post_smtp(client)
    assert b"Email settings saved" in r.data
    row = _current_smtp(app)
    assert row.enabled is True
    assert row.host == "mail.smtp2go.com"
    assert row.port == 587
    assert row.username == "smtp2go-user"
    assert row.from_addr == "me@example.com"
    assert row.to_addr == "me@example.com"
    assert row.password_enc  # something was encrypted and stored


def test_blank_password_keeps_existing(client, app):
    _login_admin(client, app)
    _post_smtp(client, password="first-secret")
    first_enc = _current_smtp(app).password_enc
    _post_smtp(client, password="")
    assert _current_smtp(app).password_enc == first_enc


def test_relative_next_honored_absolute_rejected(client, app):
    _login_admin(client, app)
    r = client.post(SMTP_URL, data={
        "enabled": "on", "host": "mail.smtp2go.com", "to_addr": "me@example.com",
        "next": "/getting-started/notifications",
    }, follow_redirects=False)
    assert r.headers["Location"].endswith("/getting-started/notifications")

    r = client.post(SMTP_URL, data={
        "enabled": "on", "host": "mail.smtp2go.com", "to_addr": "me@example.com",
        "next": "https://evil.example/x",
    }, follow_redirects=False)
    assert "evil.example" not in r.headers["Location"]


def test_test_email_warns_without_host_or_recipient(client, app):
    _login_admin(client, app)
    r = client.post(TEST_EMAIL_URL, data={}, follow_redirects=True)
    assert b"Save the SMTP host and recipient first" in r.data


def test_test_email_success_reports_recipient(client, app, monkeypatch):
    _login_admin(client, app)
    _post_smtp(client)
    sent = []
    monkeypatch.setattr("app.main.send_email", lambda *a, **kw: sent.append(a))
    r = client.post(TEST_EMAIL_URL, data={}, follow_redirects=True)
    assert b"Test email sent to me@example.com" in r.data
    assert len(sent) == 1


def test_test_email_failure_reports_error(client, app, monkeypatch):
    _login_admin(client, app)
    _post_smtp(client)

    def boom(*a, **kw):
        raise OSError("Connection refused")
    monkeypatch.setattr("app.main.send_email", boom)
    r = client.post(TEST_EMAIL_URL, data={}, follow_redirects=True)
    assert b"Test failed" in r.data
    assert b"Connection refused" in r.data


def test_test_email_honors_next(client, app, monkeypatch):
    _login_admin(client, app)
    _post_smtp(client)
    monkeypatch.setattr("app.main.send_email", lambda *a, **kw: None)
    r = client.post(TEST_EMAIL_URL, data={"next": "/getting-started/notifications"},
                    follow_redirects=False)
    assert r.headers["Location"].endswith("/getting-started/notifications")
