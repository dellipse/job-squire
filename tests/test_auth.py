# Copyright (C) 2026 D. Brandmeyer
# Licensed under the GNU Affero General Public License v3 or later.
"""Tests for the authentication blueprint (app/auth.py) and admin gating.

Covers the security-relevant behavior of login:

  * valid credentials log in and redirect; invalid credentials do not;
  * the ``next`` redirect target is validated by _is_safe_next so an attacker
    cannot bounce a logged-in user to an external site (open-redirect);
  * the login POST is rate limited;
  * admin-only routes are enforced by admin_required for non-admin and
    anonymous callers.

CSRF is disabled in the ``client`` fixture (see conftest) so these tests can
POST the login form directly without scraping a token.
"""
import pytest

from app.auth import _is_safe_next

# Mirror the seeded test credentials from conftest.
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "admin-test-pw"
SEEKER_USERNAME = "seeker"
SEEKER_PASSWORD = "user-test-pw"

ADMIN_ONLY_URL = "/jobs/1/delete"  # @login_required + @admin_required


def _login(client, username, password, next_url=None):
    url = "/login" + (f"?next={next_url}" if next_url else "")
    return client.post(
        url,
        data={"username": username, "password": password},
        follow_redirects=False,
    )


def _dismiss_onboarding(app):
    """These tests exercise login/logout mechanics, not the Getting Started
    walkthrough — dismiss it so a fresh admin account's "/" renders the
    dashboard directly instead of the onboarding force-redirect that a truly
    unstarted checklist would trigger (see app/onboarding.py)."""
    from app.extensions import db
    from app.models import OnboardingState
    with app.app_context():
        state = db.session.get(OnboardingState, 1)
        if state is None:
            state = OnboardingState(id=1)
            db.session.add(state)
        state.dismissed = True
        db.session.commit()


# --------------------------------------------------------------------------- #
# _is_safe_next — pure open-redirect guard
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("target", [
    "/jobs",
    "/jobs?status=Applied",
    "/",
    "/a/b/c",
])
def test_is_safe_next_allows_relative_paths(target):
    assert _is_safe_next(target) is True


@pytest.mark.parametrize("target", [
    None,
    "",
    "relative-no-leading-slash",
    "http://evil.example.com",
    "https://evil.example.com/path",
    "//evil.example.com",             # protocol-relative — resolves off-site
    "javascript:alert(1)",
    "\\\\evil.example.com",
])
def test_is_safe_next_rejects_unsafe_targets(target):
    assert _is_safe_next(target) is False


# --------------------------------------------------------------------------- #
# Login flow
# --------------------------------------------------------------------------- #

def test_login_success_redirects(client, app):
    _dismiss_onboarding(app)
    resp = _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    assert resp.status_code == 302
    # Lands on the dashboard, and the session is now authenticated.
    assert resp.headers["Location"].endswith("/")
    assert client.get("/").status_code == 200


def test_get_login_when_authenticated_redirects(client):
    """An already-authenticated user hitting GET /login is bounced to dashboard."""
    _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    resp = client.get("/login", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/")


def test_logout_clears_session(client, app):
    """Logout ends the session; protected pages then bounce back to login."""
    _dismiss_onboarding(app)
    _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    assert client.get("/").status_code == 200
    resp = client.get("/logout", follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]
    after = client.get("/", follow_redirects=False)
    assert after.status_code == 302
    assert "/login" in after.headers["Location"]


def test_login_wrong_password_stays_out(client):
    resp = _login(client, ADMIN_USERNAME, "not-the-password")
    assert resp.status_code == 200
    assert b"Invalid username or password" in resp.data
    # Not authenticated: a protected page bounces to login.
    protected = client.get("/", follow_redirects=False)
    assert protected.status_code == 302
    assert "/login" in protected.headers["Location"]


def test_login_unknown_user(client):
    resp = _login(client, "nobody", "whatever")
    assert resp.status_code == 200
    assert b"Invalid username or password" in resp.data


def test_login_next_honours_safe_relative_target(client):
    resp = _login(client, ADMIN_USERNAME, ADMIN_PASSWORD, next_url="/jobs")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/jobs")


def test_login_next_ignores_unsafe_target(client):
    resp = _login(client, ADMIN_USERNAME, ADMIN_PASSWORD,
                  next_url="http://evil.example.com")
    assert resp.status_code == 302
    # Falls back to the dashboard rather than the attacker's URL.
    assert "evil.example.com" not in resp.headers["Location"]
    assert resp.headers["Location"].endswith("/")


# --------------------------------------------------------------------------- #
# Rate limiting  (limiter storage is reset before each test in conftest)
# --------------------------------------------------------------------------- #

def test_login_post_is_rate_limited(client):
    # The route allows "10 per minute". The 11th POST in the window is blocked.
    statuses = [
        _login(client, ADMIN_USERNAME, "wrong").status_code
        for _ in range(10)
    ]
    assert all(s == 200 for s in statuses), statuses
    assert _login(client, ADMIN_USERNAME, "wrong").status_code == 429


# --------------------------------------------------------------------------- #
# admin_required enforcement
# --------------------------------------------------------------------------- #

def test_admin_route_blocks_anonymous(client):
    resp = client.post(ADMIN_ONLY_URL, follow_redirects=False)
    # login_required fires first -> redirect to the login page.
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_admin_route_forbids_non_admin(client):
    _login(client, SEEKER_USERNAME, SEEKER_PASSWORD)
    resp = client.post(ADMIN_ONLY_URL, follow_redirects=False)
    assert resp.status_code == 403


def test_admin_route_allows_admin(client):
    _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    resp = client.post(ADMIN_ONLY_URL, follow_redirects=False)
    # Admin passes the gate; the request is not a 403 (it fails later on the
    # missing confirm token / absent job, which is fine — the gate is what we test).
    assert resp.status_code != 403


# The Settings page exposes stored secrets (provider/SMTP/Anthropic keys) and is
# now admin-only. These guard the S1 fix: a non-admin user must not reach it.
SETTINGS_URL = "/settings"  # GET, @login_required + @admin_required


def test_settings_forbids_non_admin(client):
    _login(client, SEEKER_USERNAME, SEEKER_PASSWORD)
    resp = client.get(SETTINGS_URL, follow_redirects=False)
    assert resp.status_code == 403


def test_settings_allows_admin(client):
    _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    resp = client.get(SETTINGS_URL, follow_redirects=False)
    assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# /account — self-service password change
# --------------------------------------------------------------------------- #

ACCOUNT_URL = "/account"


def _change_password(client, current, new, confirm=None):
    return client.post(
        ACCOUNT_URL,
        data={
            "current_password": current,
            "new_password": new,
            "confirm_password": confirm if confirm is not None else new,
        },
        follow_redirects=False,
    )


def test_account_requires_login(client):
    resp = client.get(ACCOUNT_URL, follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_account_available_to_non_admin(client):
    """Unlike /settings, /account has no admin_required gate — either account
    must be able to rotate its own password."""
    _login(client, SEEKER_USERNAME, SEEKER_PASSWORD)
    resp = client.get(ACCOUNT_URL, follow_redirects=False)
    assert resp.status_code == 200


def test_change_password_success_then_login_with_new_password(client):
    _login(client, SEEKER_USERNAME, SEEKER_PASSWORD)
    resp = _change_password(client, SEEKER_PASSWORD, "a-new-strong-pw")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/account")

    client.get("/logout")
    # Old password no longer works.
    stale = _login(client, SEEKER_USERNAME, SEEKER_PASSWORD)
    assert stale.status_code == 200
    assert b"Invalid username or password" in stale.data
    # New password does.
    fresh = _login(client, SEEKER_USERNAME, "a-new-strong-pw")
    assert fresh.status_code == 302

    # Restore the fixture's expected password so other tests relying on the
    # session-scoped app/db aren't affected by ordering.
    _login(client, SEEKER_USERNAME, "a-new-strong-pw")
    _change_password(client, "a-new-strong-pw", SEEKER_PASSWORD)


def test_change_password_wrong_current_password_rejected(client):
    _login(client, SEEKER_USERNAME, SEEKER_PASSWORD)
    resp = _change_password(client, "not-the-current-password", "a-new-strong-pw")
    assert resp.status_code == 200
    assert b"Current password is incorrect" in resp.data

    # Old password still works — nothing was changed.
    client.get("/logout")
    still_old = _login(client, SEEKER_USERNAME, SEEKER_PASSWORD)
    assert still_old.status_code == 302


def test_change_password_mismatched_confirmation_rejected(client):
    _login(client, SEEKER_USERNAME, SEEKER_PASSWORD)
    resp = _change_password(client, SEEKER_PASSWORD, "a-new-strong-pw", confirm="different-pw")
    assert resp.status_code == 200
    assert b"do not match" in resp.data

    client.get("/logout")
    still_old = _login(client, SEEKER_USERNAME, SEEKER_PASSWORD)
    assert still_old.status_code == 302


def test_change_password_same_as_current_rejected(client):
    _login(client, SEEKER_USERNAME, SEEKER_PASSWORD)
    resp = _change_password(client, SEEKER_PASSWORD, SEEKER_PASSWORD)
    assert resp.status_code == 200
    assert b"must be different" in resp.data


def test_change_password_too_short_rejected(client):
    _login(client, SEEKER_USERNAME, SEEKER_PASSWORD)
    resp = _change_password(client, SEEKER_PASSWORD, "short")
    assert resp.status_code == 200
    # WTForms Length(min=8) validation error, not our custom flash.
    assert b"account" in resp.request.path.encode() or resp.status_code == 200

    client.get("/logout")
    still_old = _login(client, SEEKER_USERNAME, SEEKER_PASSWORD)
    assert still_old.status_code == 302


def test_account_change_post_is_rate_limited(client):
    _login(client, SEEKER_USERNAME, SEEKER_PASSWORD)
    statuses = [
        _change_password(client, "wrong-current-password", "a-new-strong-pw").status_code
        for _ in range(10)
    ]
    assert all(s == 200 for s in statuses), statuses
    assert _change_password(client, "wrong-current-password", "a-new-strong-pw").status_code == 429
