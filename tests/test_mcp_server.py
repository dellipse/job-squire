# Copyright (C) 2026 D. Brandmeyer
# Licensed under the GNU Affero General Public License v3 or later.
"""Tests for the remote MCP OAuth server (``app/mcp_server.py``).

This is the highest-risk untested surface: a public server that grants live
read/write access to the whole pipeline. The tests here assert *behaviour* at
the boundary — PKCE enforcement, redirect_uri validation, single-use/expiring
codes, token TTL, the static-key path, revocation, and refusal of
unauthenticated calls — rather than poking at internals where a behavioural
assertion is possible.

The ASGI handlers are async. Instead of standing up uvicorn we drive
``asgi_app`` directly through a tiny in-process ASGI transport (``_call``),
which is faster and fully offline. The real MCP inner app (``_inner``) is
monkeypatched with a sentinel in the auth tests so we assert the *auth
decision* (accept vs. 401) without dragging in the MCP protocol machinery.

Importing ``app.mcp_server`` runs module-level code that builds a Flask app and
reads env vars, so every test depends (transitively) on the session ``app``
fixture from conftest, which sets SECRET_KEY / DATA_DIR / seed passwords first.
"""
import asyncio
import base64
import hashlib
import json
import os
import time

import pytest


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

@pytest.fixture
def mcp(app):
    """Import the MCP server module and reset its in-memory + on-disk state.

    ``app`` (session fixture) guarantees the test env vars are set before the
    module is first imported. The module builds its own Flask app that shares
    the same temp DATA_DIR / SQLite DB as the conftest app, so seeded users and
    the AIConfig row are reachable from both.
    """
    import app.mcp_server as m

    m._clients.clear()
    m._codes.clear()
    m._tokens.clear()
    m._login_failures.clear()
    m._register_attempts.clear()
    # Force the /mcp bearer path's token-store cache to reload on next use
    # (SEC-06) so each test starts from a known, unstale state.
    m._tokens_cache_time = 0.0
    # Same for the static-key last_used_at write throttle (PERF-04) -- a
    # 5-minute in-memory throttle would otherwise silently suppress the
    # write in every test after the first one in a given pytest session.
    m._last_used_write_time = 0.0

    # Start each test with an empty on-disk token store.
    try:
        os.remove(m._token_store_path())
    except OSError:
        pass

    # Ensure no static MCP key leaks between tests.
    _set_static_key(m, "")

    # The MCP Connector toggle gates the whole surface (SEC-03); these tests
    # exercise that surface, so default it on. test_mcp_disabled_* below
    # flips it off explicitly to cover the gate itself.
    _set_mcp_enabled(m, True)

    return m


def _set_mcp_enabled(m, enabled):
    from app.extensions import db
    from app.models import AIConfig
    with m.flask_app.app_context():
        cfg = db.session.get(AIConfig, 1)
        if cfg is None:
            cfg = AIConfig(id=1)
            db.session.add(cfg)
        cfg.mcp_enabled = enabled
        db.session.commit()


def _set_static_key(m, plaintext, *, allow_network=False, expires_at=None):
    """Set (or clear) AIConfig.mcp_api_key_enc for the static-key auth path."""
    from app.crypto import encrypt
    from app.extensions import db
    from app.models import AIConfig
    with m.flask_app.app_context():
        cfg = db.session.get(AIConfig, 1)
        if cfg is None:
            cfg = AIConfig(id=1)
            db.session.add(cfg)
        secret = m.flask_app.config["SECRET_KEY"]
        cfg.mcp_api_key_enc = encrypt(secret, plaintext) if plaintext else ""
        cfg.mcp_api_key_allow_network = allow_network
        cfg.mcp_api_key_expires_at = expires_at
        cfg.mcp_api_key_last_used_at = None
        db.session.commit()


def _static_key_last_used_at(m):
    from app.extensions import db
    from app.models import AIConfig
    with m.flask_app.app_context():
        return db.session.get(AIConfig, 1).mcp_api_key_last_used_at


def _call(m, method, path, *, headers=None, body=b"", query_string=b"",
          client=("127.0.0.1", 55555)):
    """Drive ``asgi_app`` once and return (status, headers_dict, body_bytes)."""
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": query_string,
        "headers": headers or [],
        "client": client,
    }
    sent = []
    state = {"body_sent": False}

    async def receive():
        if state["body_sent"]:
            return {"type": "http.disconnect"}
        state["body_sent"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(msg):
        sent.append(msg)

    asyncio.run(m.asgi_app(scope, receive, send))

    status = None
    resp_headers = {}
    out = b""
    for msg in sent:
        if msg["type"] == "http.response.start":
            status = msg["status"]
            resp_headers = {k.decode().lower(): v.decode() for k, v in msg["headers"]}
        elif msg["type"] == "http.response.body":
            out += msg.get("body", b"")
    return status, resp_headers, out


def _pkce():
    """Return (verifier, S256 challenge) matching the server's verification."""
    import secrets
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def _sentinel_inner():
    """An ASGI app standing in for FastMCP's _inner; records that it was hit."""
    hits = {"count": 0}

    async def inner(scope, receive, send):
        hits["count"] += 1
        payload = b'{"ok": true}'
        await send({
            "type": "http.response.start", "status": 200,
            "headers": [(b"content-type", b"application/json")],
        })
        await send({"type": "http.response.body", "body": payload})

    return inner, hits


def _register_client(m, redirect_uris):
    """Register an OAuth client and return its client_id."""
    body = json.dumps({"client_name": "Test Client",
                       "redirect_uris": redirect_uris}).encode()
    status, _, out = _call(m, "POST", "/oauth/register", body=body)
    assert status == 201
    return json.loads(out)["client_id"]


def _seed_code(m, client_id, redirect_uri, challenge, *, exp_offset=600):
    """Insert an authorization code directly into the in-memory code store."""
    code = "test-code-" + base64.urlsafe_b64encode(os.urandom(6)).decode().rstrip("=")
    m._codes[code] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "exp": time.time() + exp_offset,
    }
    return code


def _token_body(code, verifier):
    from urllib.parse import urlencode
    return urlencode({"code": code, "code_verifier": verifier,
                      "grant_type": "authorization_code"}).encode()


# ---------------------------------------------------------------------------
# 1. PKCE is required and verified
# ---------------------------------------------------------------------------

def test_pkce_wrong_verifier_rejected(mcp):
    verifier, challenge = _pkce()
    cid = _register_client(mcp, ["https://claude.ai/cb"])
    code = _seed_code(mcp, cid, "https://claude.ai/cb", challenge)

    status, _, out = _call(mcp, "POST", "/oauth/token",
                           body=_token_body(code, "not-the-right-verifier"))

    assert status == 400
    assert json.loads(out)["error"] == "invalid_grant"
    # No token was minted.
    assert mcp._tokens == {}


def test_pkce_correct_verifier_succeeds(mcp):
    verifier, challenge = _pkce()
    cid = _register_client(mcp, ["https://claude.ai/cb"])
    code = _seed_code(mcp, cid, "https://claude.ai/cb", challenge)

    status, _, out = _call(mcp, "POST", "/oauth/token",
                           body=_token_body(code, verifier))

    assert status == 200
    data = json.loads(out)
    assert data["token_type"] == "bearer"
    token = data["access_token"]
    assert token in mcp._tokens
    # Token was persisted to the on-disk store.
    assert token in mcp._load_tokens()


# ---------------------------------------------------------------------------
# 2. redirect_uri validation
# ---------------------------------------------------------------------------

def test_authorize_get_rejects_unregistered_redirect_uri(mcp):
    _, challenge = _pkce()
    cid = _register_client(mcp, ["https://claude.ai/cb"])
    qs = (f"client_id={cid}&redirect_uri=https://evil.example/cb"
          f"&code_challenge={challenge}&code_challenge_method=S256").encode()

    status, _, out = _call(mcp, "GET", "/oauth/authorize", query_string=qs)

    assert status == 400
    assert b"redirect_uri" in out


def test_authorize_get_accepts_registered_redirect_uri(mcp):
    _, challenge = _pkce()
    cid = _register_client(mcp, ["https://claude.ai/cb"])
    qs = (f"client_id={cid}&redirect_uri=https://claude.ai/cb"
          f"&code_challenge={challenge}&code_challenge_method=S256").encode()

    status, headers, out = _call(mcp, "GET", "/oauth/authorize", query_string=qs)

    assert status == 200
    assert "text/html" in headers.get("content-type", "")
    assert b"Authorize" in out


def test_authorize_post_rejects_unregistered_redirect_uri(mcp):
    from urllib.parse import urlencode
    _, challenge = _pkce()
    cid = _register_client(mcp, ["https://claude.ai/cb"])
    body = urlencode({
        "username": "admin", "password": "admin-test-pw",
        "client_id": cid, "redirect_uri": "https://evil.example/cb",
        "code_challenge": challenge, "code_challenge_method": "S256",
    }).encode()

    status, _, out = _call(mcp, "POST", "/oauth/authorize", body=body)

    assert status == 400
    assert b"redirect_uri" in out
    # No code was issued for the bogus redirect.
    assert mcp._codes == {}


def test_authorize_get_requires_s256_pkce(mcp):
    cid = _register_client(mcp, ["https://claude.ai/cb"])
    # No code_challenge at all.
    qs = f"client_id={cid}&redirect_uri=https://claude.ai/cb".encode()
    status, _, out = _call(mcp, "GET", "/oauth/authorize", query_string=qs)
    assert status == 400
    assert b"PKCE" in out


# ---------------------------------------------------------------------------
# 3. Authorization codes are single-use and expire
# ---------------------------------------------------------------------------

def test_code_is_single_use(mcp):
    verifier, challenge = _pkce()
    cid = _register_client(mcp, ["https://claude.ai/cb"])
    code = _seed_code(mcp, cid, "https://claude.ai/cb", challenge)

    first, _, _ = _call(mcp, "POST", "/oauth/token",
                        body=_token_body(code, verifier))
    assert first == 200

    # Second exchange of the same code must fail — it was consumed.
    second, _, out = _call(mcp, "POST", "/oauth/token",
                           body=_token_body(code, verifier))
    assert second == 400
    assert json.loads(out)["error"] == "invalid_grant"


def test_expired_code_rejected(mcp):
    verifier, challenge = _pkce()
    cid = _register_client(mcp, ["https://claude.ai/cb"])
    # Code already 60s past its expiry.
    code = _seed_code(mcp, cid, "https://claude.ai/cb", challenge, exp_offset=-60)

    status, _, out = _call(mcp, "POST", "/oauth/token",
                           body=_token_body(code, verifier))

    assert status == 400
    assert json.loads(out)["error"] == "invalid_grant"
    assert mcp._tokens == {}


# ---------------------------------------------------------------------------
# 4. Token TTL / expiry through the on-disk store
# ---------------------------------------------------------------------------

def test_load_tokens_prunes_expired(mcp):
    now = time.time()
    path = mcp._token_store_path()
    with open(path, "w") as fh:
        json.dump({
            "live-token": {"client_id": "c", "exp": now + 3600},
            "dead-token": {"client_id": "c", "exp": now - 3600},
        }, fh)

    loaded = mcp._load_tokens()

    assert "live-token" in loaded
    assert "dead-token" not in loaded


def test_token_store_is_encrypted_at_rest(mcp):
    """The on-disk OAuth token store must not contain raw bearer tokens.

    Guards the fix for the plaintext-token-store finding: _save_tokens writes an
    encrypted blob, and _load_tokens round-trips it back.
    """
    now = time.time()
    mcp._tokens["super-secret-raw-token"] = {"client_id": "c", "exp": now + 3600}
    mcp._save_tokens(mcp._tokens)

    raw = open(mcp._token_store_path()).read()
    assert raw.startswith("enc:"), "token store is not encrypted on disk"
    assert "super-secret-raw-token" not in raw, "raw token leaked in plaintext"

    loaded = mcp._load_tokens()
    assert "super-secret-raw-token" in loaded


def test_expired_bearer_rejected_at_mcp(mcp, monkeypatch):
    inner, hits = _sentinel_inner()
    monkeypatch.setattr(mcp, "_inner", inner)

    now = time.time()
    with open(mcp._token_store_path(), "w") as fh:
        json.dump({"stale": {"client_id": "c", "exp": now - 10}}, fh)

    status, _, out = _call(mcp, "POST", "/mcp",
                           headers=[(b"authorization", b"Bearer stale")])

    assert status == 401
    assert json.loads(out)["error"] == "unauthorized"
    assert hits["count"] == 0


def test_live_bearer_accepted_at_mcp(mcp, monkeypatch):
    inner, hits = _sentinel_inner()
    monkeypatch.setattr(mcp, "_inner", inner)

    now = time.time()
    mcp._tokens["good"] = {"client_id": "c", "exp": now + 3600}
    mcp._save_tokens(mcp._tokens)

    status, _, _ = _call(mcp, "POST", "/mcp",
                         headers=[(b"authorization", b"Bearer good")])

    assert status == 200
    assert hits["count"] == 1


# ---------------------------------------------------------------------------
# 5. Static-key path (stored encrypted, exercised through the real decrypt)
# ---------------------------------------------------------------------------

def test_static_key_accepted(mcp, monkeypatch):
    inner, hits = _sentinel_inner()
    monkeypatch.setattr(mcp, "_inner", inner)
    _set_static_key(mcp, "s3cr3t-static-key")

    status, _, _ = _call(mcp, "POST", "/mcp",
                         headers=[(b"authorization", b"Bearer s3cr3t-static-key")])

    assert status == 200
    assert hits["count"] == 1


def test_wrong_static_key_rejected(mcp, monkeypatch):
    inner, hits = _sentinel_inner()
    monkeypatch.setattr(mcp, "_inner", inner)
    _set_static_key(mcp, "s3cr3t-static-key")

    status, _, out = _call(mcp, "POST", "/mcp",
                           headers=[(b"authorization", b"Bearer wrong-key")])

    assert status == 401
    assert json.loads(out)["error"] == "unauthorized"
    assert hits["count"] == 0


def test_static_key_updates_last_used_at(mcp, monkeypatch):
    inner, hits = _sentinel_inner()
    monkeypatch.setattr(mcp, "_inner", inner)
    _set_static_key(mcp, "s3cr3t-static-key")
    assert _static_key_last_used_at(mcp) is None

    status, _, _ = _call(mcp, "POST", "/mcp",
                         headers=[(b"authorization", b"Bearer s3cr3t-static-key")])

    assert status == 200
    assert _static_key_last_used_at(mcp) is not None


def test_static_key_last_used_at_write_is_throttled(mcp, monkeypatch):
    """PERF-04 (2026-09-08 audit): the write only needs minute-level
    precision for an operator-facing "last used" timestamp -- a second
    call within the throttle window must not re-commit."""
    inner, hits = _sentinel_inner()
    monkeypatch.setattr(mcp, "_inner", inner)
    _set_static_key(mcp, "s3cr3t-static-key")
    headers = [(b"authorization", b"Bearer s3cr3t-static-key")]

    status, _, _ = _call(mcp, "POST", "/mcp", headers=headers)
    assert status == 200
    first_seen = _static_key_last_used_at(mcp)
    assert first_seen is not None

    status, _, _ = _call(mcp, "POST", "/mcp", headers=headers)
    assert status == 200
    assert _static_key_last_used_at(mcp) == first_seen, \
        "a second call inside the throttle window must not update last_used_at"

    # Simulate the throttle window having elapsed.
    mcp._last_used_write_time = 0.0
    status, _, _ = _call(mcp, "POST", "/mcp", headers=headers)
    assert status == 200
    assert _static_key_last_used_at(mcp) is not None


def test_static_key_rejected_on_network_reachable_by_default(mcp, monkeypatch):
    """A network-reachable instance (DEPLOY_MODE=network) refuses the static
    key unless the operator has explicitly opted in — the key alone is not
    enough. Rejection looks identical to a wrong key: 401, no info leak."""
    inner, hits = _sentinel_inner()
    monkeypatch.setattr(mcp, "_inner", inner)
    monkeypatch.setitem(mcp.flask_app.config, "DEPLOY_MODE", "network")
    _set_static_key(mcp, "s3cr3t-static-key", allow_network=False)

    status, _, out = _call(mcp, "POST", "/mcp",
                           headers=[(b"authorization", b"Bearer s3cr3t-static-key")])

    assert status == 401
    assert json.loads(out)["error"] == "unauthorized"
    assert hits["count"] == 0


def test_static_key_accepted_on_network_reachable_when_explicitly_allowed(mcp, monkeypatch):
    inner, hits = _sentinel_inner()
    monkeypatch.setattr(mcp, "_inner", inner)
    monkeypatch.setitem(mcp.flask_app.config, "DEPLOY_MODE", "network")
    _set_static_key(mcp, "s3cr3t-static-key", allow_network=True)

    status, _, _ = _call(mcp, "POST", "/mcp",
                         headers=[(b"authorization", b"Bearer s3cr3t-static-key")])

    assert status == 200
    assert hits["count"] == 1


def test_static_key_rejected_when_expired(mcp, monkeypatch):
    from datetime import datetime, timedelta, timezone
    inner, hits = _sentinel_inner()
    monkeypatch.setattr(mcp, "_inner", inner)
    _set_static_key(mcp, "s3cr3t-static-key",
                     expires_at=datetime.now(timezone.utc) - timedelta(hours=1))

    status, _, out = _call(mcp, "POST", "/mcp",
                           headers=[(b"authorization", b"Bearer s3cr3t-static-key")])

    assert status == 401
    assert json.loads(out)["error"] == "unauthorized"
    assert hits["count"] == 0


def test_static_key_accepted_when_not_yet_expired(mcp, monkeypatch):
    from datetime import datetime, timedelta, timezone
    inner, hits = _sentinel_inner()
    monkeypatch.setattr(mcp, "_inner", inner)
    _set_static_key(mcp, "s3cr3t-static-key",
                     expires_at=datetime.now(timezone.utc) + timedelta(hours=1))

    status, _, _ = _call(mcp, "POST", "/mcp",
                         headers=[(b"authorization", b"Bearer s3cr3t-static-key")])

    assert status == 200
    assert hits["count"] == 1


def test_rotation_invalidates_old_static_key(mcp, monkeypatch):
    """Rotating (generating a new key) is exactly overwriting the single
    mcp_api_key_enc value — there is no multi-token list, so the previous
    value stops working the moment the new one is stored."""
    inner, hits = _sentinel_inner()
    monkeypatch.setattr(mcp, "_inner", inner)
    _set_static_key(mcp, "old-key")

    before, _, _ = _call(mcp, "POST", "/mcp",
                         headers=[(b"authorization", b"Bearer old-key")])
    assert before == 200

    _set_static_key(mcp, "new-key")  # rotate

    old_status, _, out = _call(mcp, "POST", "/mcp",
                               headers=[(b"authorization", b"Bearer old-key")])
    assert old_status == 401
    assert json.loads(out)["error"] == "unauthorized"

    new_status, _, _ = _call(mcp, "POST", "/mcp",
                             headers=[(b"authorization", b"Bearer new-key")])
    assert new_status == 200


# ---------------------------------------------------------------------------
# 6. Revocation (RFC 7009)
# ---------------------------------------------------------------------------

def test_revocation_invalidates_token(mcp, monkeypatch):
    from urllib.parse import urlencode
    inner, hits = _sentinel_inner()
    monkeypatch.setattr(mcp, "_inner", inner)

    now = time.time()
    mcp._tokens["revoke-me"] = {"client_id": "c", "exp": now + 3600}
    mcp._save_tokens(mcp._tokens)

    # Works before revocation.
    before, _, _ = _call(mcp, "POST", "/mcp",
                         headers=[(b"authorization", b"Bearer revoke-me")])
    assert before == 200

    # Revoke — RFC 7009 always returns 200.
    rev, _, _ = _call(mcp, "POST", "/oauth/revoke",
                      body=urlencode({"token": "revoke-me"}).encode())
    assert rev == 200

    # No longer authenticates.
    after, _, out = _call(mcp, "POST", "/mcp",
                          headers=[(b"authorization", b"Bearer revoke-me")])
    assert after == 401
    assert json.loads(out)["error"] == "unauthorized"


# ---------------------------------------------------------------------------
# 7. Unauthenticated MCP call is refused
# ---------------------------------------------------------------------------

def test_mcp_requires_auth_no_bearer(mcp, monkeypatch):
    inner, hits = _sentinel_inner()
    monkeypatch.setattr(mcp, "_inner", inner)

    status, _, out = _call(mcp, "POST", "/mcp")

    assert status == 401
    assert json.loads(out)["error"] == "unauthorized"
    assert hits["count"] == 0


def test_mcp_requires_auth_bogus_bearer(mcp, monkeypatch):
    inner, hits = _sentinel_inner()
    monkeypatch.setattr(mcp, "_inner", inner)

    status, _, out = _call(mcp, "POST", "/mcp",
                           headers=[(b"authorization", b"Bearer nonsense")])

    assert status == 401
    assert hits["count"] == 0


# ---------------------------------------------------------------------------
# Full end-to-end OAuth flow: register -> authorize -> token -> MCP call
# ---------------------------------------------------------------------------

def test_full_oauth_flow_end_to_end(mcp, monkeypatch):
    from urllib.parse import urlencode, urlparse, parse_qs
    inner, hits = _sentinel_inner()
    monkeypatch.setattr(mcp, "_inner", inner)

    verifier, challenge = _pkce()
    redirect_uri = "https://claude.ai/api/mcp/auth_callback"
    cid = _register_client(mcp, [redirect_uri])

    # Authorize with valid seeded admin credentials -> 302 redirect with code.
    body = urlencode({
        "username": "admin", "password": "admin-test-pw",
        "client_id": cid, "redirect_uri": redirect_uri, "state": "xyz",
        "code_challenge": challenge, "code_challenge_method": "S256",
    }).encode()
    status, headers, _ = _call(mcp, "POST", "/oauth/authorize", body=body)
    assert status == 302
    location = headers["location"]
    code = parse_qs(urlparse(location).query)["code"][0]
    assert parse_qs(urlparse(location).query)["state"][0] == "xyz"

    # Exchange the code for a token.
    status, _, out = _call(mcp, "POST", "/oauth/token",
                           body=_token_body(code, verifier))
    assert status == 200
    token = json.loads(out)["access_token"]

    # Use the token on the MCP endpoint.
    status, _, _ = _call(mcp, "POST", "/mcp",
                         headers=[(b"authorization", f"Bearer {token}".encode())])
    assert status == 200
    assert hits["count"] == 1


def test_authorize_post_bad_password_no_code(mcp):
    from urllib.parse import urlencode
    _, challenge = _pkce()
    cid = _register_client(mcp, ["https://claude.ai/cb"])
    body = urlencode({
        "username": "admin", "password": "wrong-password",
        "client_id": cid, "redirect_uri": "https://claude.ai/cb",
        "code_challenge": challenge, "code_challenge_method": "S256",
    }).encode()

    status, headers, out = _call(mcp, "POST", "/oauth/authorize", body=body)

    # Re-renders the login page (200 HTML) with an error, and issues no code.
    assert status == 200
    assert b"Incorrect username or password" in out
    assert mcp._codes == {}


# ---------------------------------------------------------------------------
# 8. SEC-01: auth page escaping and response security headers
# ---------------------------------------------------------------------------

def test_authorize_get_escapes_injected_state(mcp):
    """A malicious `state` (or client_id/redirect_uri) must not break out of
    the hidden input it's rendered into — the reflected-XSS finding."""
    _, challenge = _pkce()
    cid = _register_client(mcp, ["https://claude.ai/cb"])
    payload = "\"><script>alert(1)</script>"
    from urllib.parse import quote
    qs = (f"client_id={cid}&redirect_uri=https://claude.ai/cb"
          f"&code_challenge={challenge}&code_challenge_method=S256"
          f"&state={quote(payload)}").encode()

    status, _, out = _call(mcp, "GET", "/oauth/authorize", query_string=qs)

    assert status == 200
    assert b"<script>alert(1)</script>" not in out
    assert b"&quot;&gt;&lt;script&gt;" in out


def test_auth_page_shows_client_name_and_redirect_host(mcp):
    _, challenge = _pkce()
    cid = _register_client(mcp, ["https://claude.ai/cb"])
    qs = (f"client_id={cid}&redirect_uri=https://claude.ai/cb"
          f"&code_challenge={challenge}&code_challenge_method=S256").encode()

    status, _, out = _call(mcp, "GET", "/oauth/authorize", query_string=qs)

    assert status == 200
    assert b"Test Client" in out
    assert b"claude.ai" in out


def test_responses_carry_security_headers(mcp):
    status, headers, _ = _call(mcp, "GET", "/.well-known/oauth-authorization-server")
    assert status == 200
    assert headers.get("x-frame-options") == "DENY"
    assert headers.get("x-content-type-options") == "nosniff"
    assert "default-src 'none'" in headers.get("content-security-policy", "")


# ---------------------------------------------------------------------------
# 9. SEC-02: registration validates redirect_uris and is rate-limited
# ---------------------------------------------------------------------------

def test_register_rejects_non_https_redirect_uri(mcp):
    body = json.dumps({"client_name": "Evil Client",
                       "redirect_uris": ["javascript:alert(1)"]}).encode()
    status, _, out = _call(mcp, "POST", "/oauth/register", body=body)
    assert status == 400
    assert mcp._clients == {}


def test_register_rejects_uri_with_fragment(mcp):
    body = json.dumps({"client_name": "Client",
                       "redirect_uris": ["https://claude.ai/cb#frag"]}).encode()
    status, _, out = _call(mcp, "POST", "/oauth/register", body=body)
    assert status == 400
    assert mcp._clients == {}


def test_register_rate_limited_per_ip(mcp):
    for _ in range(mcp._REGISTER_MAX_PER_WINDOW):
        status, _, _ = _call(mcp, "POST", "/oauth/register",
                             body=json.dumps({"redirect_uris": ["https://claude.ai/cb"]}).encode())
        assert status == 201

    status, _, out = _call(mcp, "POST", "/oauth/register",
                           body=json.dumps({"redirect_uris": ["https://claude.ai/cb"]}).encode())
    assert status == 429


# ---------------------------------------------------------------------------
# 10. SEC-03: mcp_enabled gates the whole OAuth/MCP surface
# ---------------------------------------------------------------------------

def test_mcp_disabled_returns_404_for_well_known(mcp):
    _set_mcp_enabled(mcp, False)
    status, _, _ = _call(mcp, "GET", "/.well-known/oauth-authorization-server")
    assert status == 404


def test_mcp_disabled_returns_404_for_register(mcp):
    _set_mcp_enabled(mcp, False)
    status, _, _ = _call(mcp, "POST", "/oauth/register",
                         body=json.dumps({"redirect_uris": ["https://claude.ai/cb"]}).encode())
    assert status == 404


def test_mcp_disabled_returns_404_for_mcp_path_even_with_valid_bearer(mcp, monkeypatch):
    inner, hits = _sentinel_inner()
    monkeypatch.setattr(mcp, "_inner", inner)
    now = time.time()
    mcp._tokens["good"] = {"client_id": "c", "exp": now + 3600}
    mcp._save_tokens(mcp._tokens)

    _set_mcp_enabled(mcp, False)
    status, _, _ = _call(mcp, "POST", "/mcp",
                         headers=[(b"authorization", b"Bearer good")])

    assert status == 404
    assert hits["count"] == 0


def test_mcp_disabled_returns_404_for_mcp_path_even_with_valid_static_key(mcp, monkeypatch):
    inner, hits = _sentinel_inner()
    monkeypatch.setattr(mcp, "_inner", inner)
    _set_static_key(mcp, "s3cr3t-static-key")

    _set_mcp_enabled(mcp, False)
    status, _, _ = _call(mcp, "POST", "/mcp",
                         headers=[(b"authorization", b"Bearer s3cr3t-static-key")])

    assert status == 404


# ---------------------------------------------------------------------------
# 11. SEC-06: OAuth endpoints run outside FastMCP's TransportSecuritySettings
#     middleware, so they need their own hardening -- body-size cap on the
#     hand-rolled /oauth/* POST handlers, no spoofable X-Forwarded-For
#     fallback for the rate-limit key, and a bounded-staleness cache instead
#     of decrypting the token store on literally every /mcp request.
# ---------------------------------------------------------------------------

def test_oversized_oauth_body_rejected(mcp):
    """A body over _MAX_OAUTH_BODY_BYTES must not be accumulated forever --
    _read_body raises and the handler responds 413, matching the file's
    JSON-error house style."""
    oversized = b"a" * (mcp._MAX_OAUTH_BODY_BYTES + 1)

    status, _, out = _call(mcp, "POST", "/oauth/revoke", body=oversized)

    assert status == 413
    assert json.loads(out)["error"] == "payload_too_large"


def test_body_exactly_at_cap_still_works(mcp):
    """A body right at the cap is normal-sized and must not be rejected --
    the happy path for OAuth POSTs (e.g. revoke) keeps working."""
    body = b"token=" + b"x" * (mcp._MAX_OAUTH_BODY_BYTES - len(b"token="))
    assert len(body) == mcp._MAX_OAUTH_BODY_BYTES

    status, _, out = _call(mcp, "POST", "/oauth/revoke", body=body)

    assert status == 200
    assert json.loads(out) == {}


def test_get_client_ip_ignores_spoofed_xff(mcp):
    """A private/loopback direct peer with only X-Forwarded-For (no
    X-Real-IP) must not have that header trusted as the client IP -- SWAG
    always sets X-Real-IP, so XFF is unnecessary, spoofable attack surface
    for the login/registration rate-limit key."""
    scope = {
        "client": ("192.168.1.50", 12345),
        "headers": [(b"x-forwarded-for", b"1.2.3.4, 192.168.1.50")],
    }

    ip = mcp._get_client_ip(scope)

    assert ip != "1.2.3.4"
    assert ip == "192.168.1.50"  # falls back to the direct TCP peer


def test_get_client_ip_still_trusts_x_real_ip(mcp):
    """X-Real-IP (set by SWAG) is still trusted -- only the XFF fallback was
    removed."""
    scope = {
        "client": ("192.168.1.50", 12345),
        "headers": [(b"x-real-ip", b"9.9.9.9"),
                    (b"x-forwarded-for", b"1.2.3.4, 192.168.1.50")],
    }

    assert mcp._get_client_ip(scope) == "9.9.9.9"


def test_get_client_ip_public_peer_ignores_headers(mcp):
    """A public direct peer is used as-is -- headers are only consulted for
    private/loopback peers (i.e. behind a proxy)."""
    scope = {
        "client": ("203.0.113.5", 12345),
        "headers": [(b"x-forwarded-for", b"1.2.3.4"), (b"x-real-ip", b"6.6.6.6")],
    }

    assert mcp._get_client_ip(scope) == "203.0.113.5"


def test_bearer_token_store_not_reloaded_on_every_request(mcp, monkeypatch):
    """The /mcp bearer path must not re-decrypt oauth_tokens.json on every
    request -- only when the short cache window has elapsed."""
    inner, hits = _sentinel_inner()
    monkeypatch.setattr(mcp, "_inner", inner)

    now = time.time()
    mcp._tokens["good"] = {"client_id": "c", "exp": now + 3600}
    mcp._save_tokens(mcp._tokens)

    real_load = mcp._load_tokens
    calls = {"count": 0}

    def counting_load():
        calls["count"] += 1
        return real_load()

    monkeypatch.setattr(mcp, "_load_tokens", counting_load)

    for _ in range(5):
        status, _, _ = _call(mcp, "POST", "/mcp",
                             headers=[(b"authorization", b"Bearer good")])
        assert status == 200

    assert calls["count"] == 1, "token store was re-decrypted more than once within the cache TTL"
    assert hits["count"] == 5


def test_bearer_revocation_still_takes_effect_once_cache_is_stale(mcp, monkeypatch):
    """Caching the token store (SEC-06) must not break the property the
    reload exists for: a revocation made by the main Flask web process --
    which edits oauth_tokens.json directly, not through this module's
    in-memory _tokens dict -- still takes effect without an MCP restart, once
    the cache goes stale."""
    inner, hits = _sentinel_inner()
    monkeypatch.setattr(mcp, "_inner", inner)

    now = time.time()
    mcp._tokens["good"] = {"client_id": "c", "exp": now + 3600}
    mcp._save_tokens(mcp._tokens)

    # First call populates the cache.
    status, _, _ = _call(mcp, "POST", "/mcp",
                         headers=[(b"authorization", b"Bearer good")])
    assert status == 200

    # Simulate the main web process revoking by editing the on-disk store
    # directly, bypassing this module's in-memory _tokens dict entirely.
    on_disk = mcp._load_tokens()
    del on_disk["good"]
    mcp._save_tokens(on_disk)

    # Still within the cache TTL: the (now stale relative to disk) in-memory
    # cache still accepts it -- this is the documented, bounded tradeoff.
    status, _, _ = _call(mcp, "POST", "/mcp",
                         headers=[(b"authorization", b"Bearer good")])
    assert status == 200

    # Force the cache stale (equivalent to waiting past _TOKENS_CACHE_TTL).
    mcp._tokens_cache_time = 0.0

    status, _, out = _call(mcp, "POST", "/mcp",
                           headers=[(b"authorization", b"Bearer good")])
    assert status == 401
    assert json.loads(out)["error"] == "unauthorized"


def test_bearer_revocation_via_own_endpoint_is_immediate_despite_cache(mcp, monkeypatch):
    """Revocation through this process's own /oauth/revoke handler mutates
    _tokens directly, so it must be visible immediately even while the
    reload cache is still fresh -- the cache must never clobber that
    in-memory mutation with stale disk data."""
    from urllib.parse import urlencode
    inner, hits = _sentinel_inner()
    monkeypatch.setattr(mcp, "_inner", inner)

    now = time.time()
    mcp._tokens["revoke-me"] = {"client_id": "c", "exp": now + 3600}
    mcp._save_tokens(mcp._tokens)

    # Populate the cache.
    before, _, _ = _call(mcp, "POST", "/mcp",
                         headers=[(b"authorization", b"Bearer revoke-me")])
    assert before == 200

    rev, _, _ = _call(mcp, "POST", "/oauth/revoke",
                      body=urlencode({"token": "revoke-me"}).encode())
    assert rev == 200

    # Immediately after, still well within the cache TTL.
    after, _, out = _call(mcp, "POST", "/mcp",
                          headers=[(b"authorization", b"Bearer revoke-me")])
    assert after == 401
    assert json.loads(out)["error"] == "unauthorized"
    assert hits["count"] == 1, "only the pre-revocation call should have reached the inner app"


def test_get_kit_instructions_returns_the_real_kit_prompt(mcp):
    """Regression test: get_kit_instructions() used to import KIT_PROMPT from
    app.main, which silently broke (ImportError at call time, not at module
    import time) when the QUAL-01 kits-blueprint split (PR #49) moved
    KIT_PROMPT to app.kits without updating this lazy import. Nothing
    exercised this tool in CI, so it shipped broken."""
    from app.kits import KIT_PROMPT

    result = mcp.get_kit_instructions()
    assert result == KIT_PROMPT
    assert "FIT ASSESSMENT" in result
