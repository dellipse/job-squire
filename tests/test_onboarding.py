# Copyright (C) 2026 D. Brandmeyer
# Licensed under the GNU Affero General Public License v3 or later.
"""Tests for the Getting Started walkthrough (``app/onboarding.py``).

The contract (docs/PLAN-onboarding.md, Phase 1): completion is derived from
real app state so the checklist can't drift; steps are skippable and
revisitable; the dashboard card hides on dismissal; account creation is
admin-only and validated; the remote-only board gate respects
``SearchConfig.include_remote``.
"""
import os

import pytest

from app.extensions import db
from app.models import (CandidateAsset, OnboardingState, ProviderCredential,
                        SearchConfig, SearchRun, User)
from tests.conftest import ADMIN_PASSWORD, ADMIN_USERNAME


def _login_admin(client, app):
    from app import _seed_users
    with app.app_context():
        _seed_users(app)
    return client.post("/login",
                       data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
                       follow_redirects=False)


def _login_user(client, app):
    from app import _seed_users
    from tests.conftest import USER_USERNAME, USER_PASSWORD
    with app.app_context():
        _seed_users(app)
    return client.post("/login",
                       data={"username": USER_USERNAME, "password": USER_PASSWORD},
                       follow_redirects=False)


@pytest.fixture
def clean_state(app_context):
    """Reset onboarding state and the data it derives completion from."""
    def _reset():
        state = db.session.get(OnboardingState, 1)
        if state:
            db.session.delete(state)
        CandidateAsset.query.delete()
        SearchRun.query.delete()
        ProviderCredential.query.delete()
        # test_migrations' mdb fixture drop_all()s the shared DB mid-suite,
        # wiping seeded singletons — recreate rather than assume (same pattern
        # as _login_admin re-seeding accounts).
        cfg = db.session.get(SearchConfig, 1)
        if cfg is None:
            cfg = SearchConfig(id=1)
            db.session.add(cfg)
        cfg.titles, cfg.location = "", ""
        cfg.enabled = False
        db.session.commit()
    _reset()
    from flask import current_app
    profile = os.path.join(current_app.config["DATA_DIR"], "candidate_profile.md")
    if os.path.exists(profile):
        os.unlink(profile)
    yield app_context
    _reset()


class TestChecklistDerivation:
    def test_fresh_install_everything_todo(self, clean_state):
        from app.onboarding import build_checklist
        checklist = build_checklist()
        assert all(item["status"] == "todo" for item in checklist
                   if item["key"] != "accounts")  # seeded 2nd account may exist

    def test_completion_follows_real_data(self, clean_state):
        from app.onboarding import build_checklist, get_state

        state = get_state()
        state.persona = "self"
        db.session.add(CandidateAsset(kind="Resume", original_name="r.pdf",
                                      stored_name="x.pdf"))
        cfg = db.session.get(SearchConfig, 1)
        cfg.titles, cfg.location = "Ops Manager", "Henderson, NV"
        db.session.add(ProviderCredential(provider="dice", enabled=True))
        db.session.add(SearchRun(status="ok"))
        db.session.commit()

        status = {i["key"]: i["status"] for i in build_checklist()}
        for key in ("persona", "profile", "search", "providers", "first_search"):
            assert status[key] == "done", key
        assert status["ai"] == "todo"   # nothing AI-ish configured

    def test_skip_persists_and_revisit_allowed(self, clean_state, client, app):
        _login_admin(client, app)
        r = client.post("/getting-started/ai/skip", follow_redirects=False)
        assert r.status_code == 302
        from app.onboarding import build_checklist
        status = {i["key"]: i["status"] for i in build_checklist()}
        assert status["ai"] == "skipped"
        # Revisit: the step page still renders and can be completed.
        assert client.get("/getting-started/ai").status_code == 200

    def test_derived_done_beats_stored_skip(self, clean_state):
        from app.onboarding import build_checklist, get_state
        state = get_state()
        state.set_step("search", "skipped")
        cfg = db.session.get(SearchConfig, 1)
        cfg.titles, cfg.location = "Ops Manager", "Henderson, NV"
        db.session.commit()
        status = {i["key"]: i["status"] for i in build_checklist()}
        assert status["search"] == "done"   # reality wins over the stored skip


class TestDashboardCard:
    def test_card_shows_then_hides_on_dismiss(self, clean_state, client, app):
        _login_admin(client, app)
        assert b"Getting started" in client.get("/").data
        r = client.post("/getting-started/dismiss", follow_redirects=False)
        assert r.status_code == 302
        assert b"Open walkthrough" not in client.get("/").data
        # The full page stays reachable from the nav even when dismissed.
        assert client.get("/getting-started").status_code == 200


class TestAccountsStep:
    def test_create_second_account(self, clean_state, client, app):
        _login_admin(client, app)
        User.query.filter(User.role != "admin").delete()
        db.session.commit()
        r = client.post("/getting-started/accounts",
                        data={"username": "jordan", "display_name": "Jordan",
                              "password": "hunter2hunter2", "confirm": "hunter2hunter2"},
                        follow_redirects=False)
        assert r.status_code == 302
        created = User.query.filter_by(username="jordan").first()
        assert created is not None and created.role == "user"
        assert created.check_password("hunter2hunter2")
        db.session.delete(created)
        db.session.commit()

    @pytest.mark.parametrize("data,fragment", [
        ({"username": "x", "password": "hunter2hunter2", "confirm": "hunter2hunter2"},
         "Username"),
        ({"username": "jordan2", "password": "short", "confirm": "short"},
         "at least 8"),
        ({"username": "jordan2", "password": "hunter2hunter2", "confirm": "different"},
         "do not match"),
    ])
    def test_validation_rejects(self, clean_state, client, app, data, fragment):
        _login_admin(client, app)
        r = client.post("/getting-started/accounts", data=data, follow_redirects=True)
        assert fragment.encode() in r.data
        assert User.query.filter_by(username=data["username"]).first() is None

    def test_non_admin_blocked(self, clean_state, client, app):
        _login_user(client, app)
        assert client.get("/getting-started").status_code == 403
        r = client.post("/getting-started/accounts",
                        data={"username": "sneaky", "password": "hunter2hunter2",
                              "confirm": "hunter2hunter2"})
        assert r.status_code == 403
        assert User.query.filter_by(username="sneaky").first() is None


class TestAiStep:
    def test_no_ai_marks_done_and_warns(self, clean_state, client, app):
        _login_admin(client, app)
        r = client.post("/getting-started/ai", data={"action": "no_ai"},
                        follow_redirects=True)
        assert b"Continuing without AI" in r.data
        from app.onboarding import build_checklist
        status = {i["key"]: i["status"] for i in build_checklist()}
        assert status["ai"] == "done"


class TestProfileStep:
    def test_profile_links_append_to_candidate_profile(self, clean_state, client, app):
        from flask import current_app
        _login_admin(client, app)
        path = os.path.join(current_app.config["DATA_DIR"], "candidate_profile.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write("# Existing profile")
        try:
            client.post("/getting-started/profile-links",
                        data={"links": "https://www.linkedin.com/in/jordan\nhttps://github.com/jordan"},
                        follow_redirects=True)
            with open(path, encoding="utf-8") as f:
                text = f.read()
            assert "# Existing profile" in text
            assert "## Online profiles" in text
            assert "- https://www.linkedin.com/in/jordan" in text
        finally:
            os.unlink(path)


class TestRemoteGate:
    def _run(self, monkeypatch, include_remote):
        import app.search as search_mod
        cfg = db.session.get(SearchConfig, 1)
        cfg.titles, cfg.location = "Ops Manager", "Henderson, NV"
        cfg.enabled = True
        cfg.include_remote = include_remote
        db.session.add(ProviderCredential(provider="jobicy", enabled=True))
        db.session.add(ProviderCredential(provider="dice", enabled=True))
        db.session.commit()
        called = []

        def fake_search(provider, creds, titles, cfg_dict):
            called.append(provider)
            return [], None

        monkeypatch.setattr(search_mod, "search_provider", fake_search)
        monkeypatch.setattr(search_mod, "_maybe_email_digest",
                            lambda *a, **k: None, raising=False)
        search_mod.run_search(trigger="manual")
        return called

    def test_remote_only_board_skipped_when_off(self, clean_state, monkeypatch):
        called = self._run(monkeypatch, include_remote=False)
        assert "jobicy" not in called
        assert "dice" in called

    def test_remote_only_board_runs_when_on(self, clean_state, monkeypatch):
        called = self._run(monkeypatch, include_remote=True)
        assert "jobicy" in called


class TestSafeNext:
    def test_relative_honored_absolute_rejected(self, clean_state, client, app):
        _login_admin(client, app)
        r = client.post("/settings/search",
                        data={"titles": "Ops", "location": "Henderson, NV",
                              "country": "US", "next": "/getting-started/search"},
                        follow_redirects=False)
        assert r.headers["Location"].endswith("/getting-started/search")
        r = client.post("/settings/search",
                        data={"titles": "Ops", "location": "Henderson, NV",
                              "country": "US", "next": "https://evil.example/x"},
                        follow_redirects=False)
        assert "evil.example" not in r.headers["Location"]
