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

def test_login_success_redirects(client):
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


def test_logout_clears_session(client):
    """Logout ends the session; protected pages then bounce back to login."""
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
