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

    # Start each test with an empty on-disk token store.
    try:
        os.remove(m._token_store_path())
    except OSError:
        pass

    # Ensure no static MCP key leaks between tests.
    _set_static_key(m, "")

    return m


def _set_static_key(m, plaintext):
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
        db.session.commit()


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
