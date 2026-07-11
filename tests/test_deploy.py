# Copyright (C) 2026 D. Brandmeyer
# Licensed under the GNU Affero General Public License v3 or later.
"""Tests for app/deploy.py — DEPLOY_MODE preset resolution.

The property under test is the precedence rule: an explicitly set env var
always wins; an unset one takes the resolved DEPLOY_MODE's preset default;
DEPLOY_MODE itself is only consulted to pick which preset table applies.
Also covers apply_proxy_trust(), which is what makes that resolution
actually change app behavior (ProxyFix on or off).

These are pure functions operating on os.environ / a bare Flask app, so no
database or full create_app() is needed.
"""
import pytest
from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

from app.deploy import apply_proxy_trust, resolve_deploy_flags, resolve_deploy_mode

DEPLOY_ENV_VARS = ("DEPLOY_MODE", "TRUST_PROXY", "SESSION_COOKIE_SECURE")


@pytest.fixture(autouse=True)
def _clean_deploy_env(monkeypatch):
    """Every test starts with none of the three deploy-related vars set."""
    for name in DEPLOY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_default_mode_is_local():
    assert resolve_deploy_mode() == "local"


def test_unknown_mode_falls_back_to_local(monkeypatch):
    monkeypatch.setenv("DEPLOY_MODE", "something-nonsensical")
    assert resolve_deploy_mode() == "local"


def test_mode_is_case_and_whitespace_insensitive(monkeypatch):
    monkeypatch.setenv("DEPLOY_MODE", "  Network  ")
    assert resolve_deploy_mode() == "network"


def test_local_preset_defaults(monkeypatch):
    monkeypatch.setenv("DEPLOY_MODE", "local")
    flags = resolve_deploy_flags()
    assert flags == {"mode": "local", "trust_proxy": False, "secure_cookie": False}


def test_network_preset_defaults(monkeypatch):
    monkeypatch.setenv("DEPLOY_MODE", "network")
    flags = resolve_deploy_flags()
    assert flags == {"mode": "network", "trust_proxy": True, "secure_cookie": True}


def test_unset_deploy_mode_takes_local_preset():
    flags = resolve_deploy_flags()
    assert flags["mode"] == "local"
    assert flags["trust_proxy"] is False
    assert flags["secure_cookie"] is False


def test_explicit_trust_proxy_overrides_local_preset(monkeypatch):
    monkeypatch.setenv("DEPLOY_MODE", "local")
    monkeypatch.setenv("TRUST_PROXY", "1")
    flags = resolve_deploy_flags()
    assert flags["trust_proxy"] is True
    # secure_cookie is untouched — still takes the local preset.
    assert flags["secure_cookie"] is False


def test_explicit_trust_proxy_overrides_network_preset(monkeypatch):
    monkeypatch.setenv("DEPLOY_MODE", "network")
    monkeypatch.setenv("TRUST_PROXY", "0")
    flags = resolve_deploy_flags()
    assert flags["trust_proxy"] is False
    assert flags["secure_cookie"] is True


def test_explicit_secure_cookie_overrides_local_preset(monkeypatch):
    """An existing .env that pins SESSION_COOKIE_SECURE=true must keep secure
    cookies even if DEPLOY_MODE resolves (or defaults) to local."""
    monkeypatch.setenv("DEPLOY_MODE", "local")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "true")
    flags = resolve_deploy_flags()
    assert flags["secure_cookie"] is True
    assert flags["trust_proxy"] is False


def test_explicit_secure_cookie_overrides_network_preset(monkeypatch):
    monkeypatch.setenv("DEPLOY_MODE", "network")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    flags = resolve_deploy_flags()
    assert flags["secure_cookie"] is False
    assert flags["trust_proxy"] is True


@pytest.mark.parametrize("mode", ["local", "network"])
def test_all_flags_explicit_always_win(monkeypatch, mode):
    monkeypatch.setenv("DEPLOY_MODE", mode)
    monkeypatch.setenv("TRUST_PROXY", "1")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    flags = resolve_deploy_flags()
    assert flags == {"mode": mode, "trust_proxy": True, "secure_cookie": False}


def test_apply_proxy_trust_wraps_wsgi_app_when_true():
    app = Flask(__name__)
    original = app.wsgi_app
    apply_proxy_trust(app, True)
    assert isinstance(app.wsgi_app, ProxyFix)
    assert app.wsgi_app is not original


def test_apply_proxy_trust_leaves_wsgi_app_alone_when_false():
    app = Flask(__name__)
    apply_proxy_trust(app, False)
    # Untouched: still Flask's own bound method, never reassigned to a
    # ProxyFix wrapper (or anything else) in app.__dict__.
    assert "wsgi_app" not in app.__dict__
    assert not isinstance(app.wsgi_app, ProxyFix)
