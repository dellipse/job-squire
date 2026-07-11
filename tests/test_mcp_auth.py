# Copyright (C) 2026 D. Brandmeyer
# Licensed under the GNU Affero General Public License v3 or later.
"""Tests for app/mcp_auth.py — the local static MCP bearer token.

Covers the settled spec directly: token shape (jsq_mcp_ prefix, 256 bits of
entropy), a Fernet round trip through the same encrypt()/decrypt() every
other secret uses, constant-time comparison, the TTL helper, and the
network-reachability gate that ties "is the static token usable here" to
the resolved DEPLOY_MODE from app/deploy.py rather than a raw guess.

Behavior that requires the real MCP request path (last-used timestamp
updates, rotation overwriting the live value) is covered in
tests/test_mcp_server.py instead, against the actual asgi_app.
"""
import base64
from datetime import datetime, timedelta, timezone

from app.crypto import decrypt, encrypt
from app.mcp_auth import (
    TOKEN_PREFIX,
    expires_at_from_ttl_hours,
    generate_token,
    is_network_reachable,
    is_static_token_allowed,
    verify_static_token,
)

SECRET_KEY = "unit-test-secret-key-aaaaaaaaaaaaaaaaaaaaaaaa"


# --- Token shape -------------------------------------------------------------

def test_token_has_expected_prefix():
    token = generate_token()
    assert token.startswith(TOKEN_PREFIX)


def test_token_has_256_bits_of_entropy():
    token = generate_token()
    body = token[len(TOKEN_PREFIX):]
    # token_urlsafe base64-encodes with padding stripped; decode allowing for
    # that to recover the original byte count.
    padded = body + "=" * (-len(body) % 4)
    decoded = base64.urlsafe_b64decode(padded)
    assert len(decoded) == 32  # 256 bits


def test_tokens_are_unique():
    assert generate_token() != generate_token()


# --- Storage: Fernet round trip -----------------------------------------------

def test_token_round_trips_through_fernet():
    token = generate_token()
    stored = encrypt(SECRET_KEY, token)
    assert stored != token, "ciphertext must not equal plaintext"
    assert decrypt(SECRET_KEY, stored) == token


# --- Comparison ---------------------------------------------------------------

def test_verify_accepts_the_right_token():
    token = generate_token()
    stored = encrypt(SECRET_KEY, token)
    assert verify_static_token(token, stored, SECRET_KEY, "local", False) is True


def test_verify_rejects_a_wrong_token():
    token = generate_token()
    stored = encrypt(SECRET_KEY, token)
    assert verify_static_token("jsq_mcp_wrong-token", stored, SECRET_KEY, "local", False) is False


def test_verify_rejects_missing_bearer():
    stored = encrypt(SECRET_KEY, generate_token())
    assert verify_static_token("", stored, SECRET_KEY, "local", False) is False
    assert verify_static_token(None, stored, SECRET_KEY, "local", False) is False


def test_verify_rejects_when_no_token_is_stored():
    assert verify_static_token("anything", "", SECRET_KEY, "local", False) is False


# --- Reachability rule ---------------------------------------------------------

def test_local_mode_is_not_network_reachable():
    assert is_network_reachable("local") is False


def test_network_mode_is_network_reachable():
    assert is_network_reachable("network") is True


def test_static_token_allowed_on_local_regardless_of_flag():
    assert is_static_token_allowed("local", False) is True
    assert is_static_token_allowed("local", True) is True


def test_static_token_requires_explicit_opt_in_on_network():
    assert is_static_token_allowed("network", False) is False
    assert is_static_token_allowed("network", True) is True


def test_verify_rejects_correct_token_on_unopted_network_instance():
    token = generate_token()
    stored = encrypt(SECRET_KEY, token)
    assert verify_static_token(token, stored, SECRET_KEY, "network", False) is False


def test_verify_accepts_correct_token_on_opted_in_network_instance():
    token = generate_token()
    stored = encrypt(SECRET_KEY, token)
    assert verify_static_token(token, stored, SECRET_KEY, "network", True) is True


# --- TTL ------------------------------------------------------------------------

def test_expires_at_from_ttl_hours_none_means_no_expiry():
    assert expires_at_from_ttl_hours(None) is None
    assert expires_at_from_ttl_hours("") is None
    assert expires_at_from_ttl_hours("0") is None
    assert expires_at_from_ttl_hours("-5") is None
    assert expires_at_from_ttl_hours("not-a-number") is None


def test_expires_at_from_ttl_hours_computes_future_time():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    result = expires_at_from_ttl_hours("2", now=now)
    assert result == now + timedelta(hours=2)


def test_verify_rejects_expired_token():
    token = generate_token()
    stored = encrypt(SECRET_KEY, token)
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    assert verify_static_token(token, stored, SECRET_KEY, "local", False, expires_at=past) is False


def test_verify_accepts_token_before_expiry():
    token = generate_token()
    stored = encrypt(SECRET_KEY, token)
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    assert verify_static_token(token, stored, SECRET_KEY, "local", False, expires_at=future) is True


def test_verify_handles_naive_expires_at_from_sqlite_round_trip():
    """SQLite drops tzinfo on read-back; expires_at can arrive naive while
    `now` (computed fresh) is aware. Must not raise, must still compare
    correctly."""
    token = generate_token()
    stored = encrypt(SECRET_KEY, token)
    naive_future = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(tzinfo=None)
    naive_past = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(tzinfo=None)
    assert verify_static_token(token, stored, SECRET_KEY, "local", False, expires_at=naive_future) is True
    assert verify_static_token(token, stored, SECRET_KEY, "local", False, expires_at=naive_past) is False
