# Copyright (C) 2026 D. Brandmeyer
# Licensed under the GNU Affero General Public License v3 or later.
"""Regression test for adopting an existing three-container install onto
the single-container image (Prompt 7 / docs/adopt-single-container.md).

Simulates the actual sequence an adopted install goes through: a data
directory and .env already exist (SECRET_KEY, INSTANCE_NAME, an encrypted
provider secret, no DEPLOY_MODE or TRUST_PROXY -- those didn't exist yet),
and the app boots against that same directory again under the new image.
The properties that matter: it starts at all, the cookie name is derived
the same way it always was, and a secret encrypted before the migration
still decrypts with the retained SECRET_KEY.

create_app() is called twice against the same DATA_DIR here, independent
of conftest's session-scoped `app` fixture -- verified safe: Flask-SQLAlchemy
and friends bind to `current_app` via context, not a hardwired reference, so
two independently-built Flask apps over the same on-disk SQLite DB behave
exactly like two successive process boots (which is what this is standing
in for), as long as each app's own app_context() is used for its DB access.
"""
import shutil
import tempfile

import pytest

from app.crypto import decrypt, encrypt


@pytest.fixture
def legacy_data_dir():
    tmp = tempfile.mkdtemp(prefix="jobsquire-adopt-test-")
    try:
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


LEGACY_SECRET_KEY = "legacy-install-secret-key-do-not-lose-aaaaaaaa"
LEGACY_INSTANCE_NAME = "castelo"
LEGACY_PROVIDER_SECRET = "sk-legacy-provider-key-12345"


def _legacy_env(monkeypatch, data_dir):
    """Set env vars matching what a real pre-Prompt-4 install's .env has:
    SECRET_KEY, INSTANCE_NAME, SESSION_COOKIE_SECURE=true (what install.sh
    writes for a production install) -- and deliberately no DEPLOY_MODE or
    TRUST_PROXY, since those didn't exist before this design."""
    for name in ("DEPLOY_MODE", "TRUST_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SECRET_KEY", LEGACY_SECRET_KEY)
    monkeypatch.setenv("DATA_DIR", data_dir)
    monkeypatch.setenv("ADMIN_PASSWORD", "legacy-admin-pw")
    monkeypatch.setenv("INSTANCE_NAME", LEGACY_INSTANCE_NAME)
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "true")


def test_adopted_install_boots_with_preserved_cookie_name_and_secrets(
    monkeypatch, legacy_data_dir
):
    # --- Phase 1: the "existing install", pre-migration ---------------------
    _legacy_env(monkeypatch, legacy_data_dir)
    from app import create_app

    legacy_app = create_app()
    assert legacy_app.config["SESSION_COOKIE_NAME"] == "castelo_session"

    with legacy_app.app_context():
        from app.extensions import db
        from app.models import AIConfig

        # AIConfig's singleton row is created lazily (via main._singleton())
        # on first Settings-page touch, not seeded at boot -- mirror that.
        cfg = db.session.get(AIConfig, 1) or AIConfig(id=1)
        cfg.api_key_enc = encrypt(LEGACY_SECRET_KEY, LEGACY_PROVIDER_SECRET)
        db.session.add(cfg)
        db.session.commit()
        stored_ciphertext = cfg.api_key_enc

    # --- Phase 2: same data dir, same .env, boot again under the new image --
    # (DEPLOY_MODE/TRUST_PROXY are still unset here -- exactly what an
    # adopted .env looks like before the operator adds anything.)
    adopted_app = create_app()

    # It starts: no exception, no SystemExit from the Prompt 5 startup guard.
    assert adopted_app is not None

    # Cookie name is derived identically -- same INSTANCE_NAME, same formula.
    assert adopted_app.config["SESSION_COOKIE_NAME"] == "castelo_session"

    # DEPLOY_MODE resolves to the safe default; this is the one documented,
    # intentional behavior change (see docs/adopt-single-container.md) --
    # TRUST_PROXY still resolves false here because the adopt helper (not
    # exercised in this pure-Python test) is what appends TRUST_PROXY=1.
    assert adopted_app.config["DEPLOY_MODE"] == "local"

    # But the explicit SESSION_COOKIE_SECURE=true the legacy .env already
    # had is honored regardless of mode -- no regression there.
    assert adopted_app.config["SESSION_COOKIE_SECURE"] is True

    # The secret encrypted before migration still decrypts with the
    # retained SECRET_KEY -- nothing was re-encrypted or lost.
    with adopted_app.app_context():
        from app.extensions import db
        from app.models import AIConfig

        cfg = db.session.get(AIConfig, 1)
        assert cfg.api_key_enc == stored_ciphertext, "secret was rewritten, not preserved"
        assert decrypt(LEGACY_SECRET_KEY, cfg.api_key_enc) == LEGACY_PROVIDER_SECRET


def test_adopted_install_with_trust_proxy_set_matches_old_unconditional_proxyfix(
    monkeypatch, legacy_data_dir
):
    """After running the adopt helper (which appends TRUST_PROXY=1), the
    resolved posture matches the old code's unconditional ProxyFix even
    though DEPLOY_MODE is still unset/local."""
    _legacy_env(monkeypatch, legacy_data_dir)
    from app import create_app

    create_app()  # legacy boot, establishes the DB

    monkeypatch.setenv("TRUST_PROXY", "1")  # what the adopt helper appends
    adopted_app = create_app()

    assert adopted_app.config["DEPLOY_MODE"] == "local"
    assert adopted_app.config["TRUST_PROXY"] is True
    assert adopted_app.config["SESSION_COOKIE_SECURE"] is True

    from werkzeug.middleware.proxy_fix import ProxyFix
    assert isinstance(adopted_app.wsgi_app, ProxyFix)
