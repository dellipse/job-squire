# Copyright (C) 2026 D. Brandmeyer
# Licensed under the GNU Affero General Public License v3 or later.
"""HTTP-layer smoke tests for the destructive/admin routes flagged by the
2026-09-08 pre-prod audit (TEST-01) as having zero client-level coverage:

  /jobs/<id>/delete                    (admin)
  /jobs/bulk-update                    (any logged-in user)
  /interviews/<id>/delete              (any logged-in user)
  /contacts/<id>/delete                (any logged-in user)
  /submissions/<id>/delete             (any logged-in user)
  /attachments/<id>/delete             (any logged-in user)
  /settings/mcp-revoke-token           (admin)
  /settings/mcp-revoke-all             (admin)
  /settings/ai/providers/<id>/delete   (admin)
  /api/ingest                          (API-key, no session auth)
  /assets/<id>/delete                  (admin)

Each admin-gated route gets: an auth-gate test (anonymous -> redirected, not
200), a wrong-role test (logged-in non-admin -> a real 403, not merely
"not 403" -- see TEST-04 on why that assertion shape is banned), and a
happy-path test asserting the actual DB mutation. Each login-only route skips
the wrong-role case (there is no role gate to test) but still gets auth-gate
+ happy-path coverage.

/jobs/<id>/delete's auth-gate and wrong-role behavior is already exercised by
tests/test_auth.py's ADMIN_ONLY_URL constant (which points at this exact
route as the generic admin_required example) -- not duplicated here, only
the happy-path DB mutation is added.

/assets/<id>/delete's happy path (deleting the base resume variant promotes
the newest remaining one) is already covered by
tests/test_onboarding.py::TestResumeSetBaseAndDelete -- not duplicated here,
only the auth-gate and wrong-role cases are added.
"""
import hashlib
import os
import time

import pytest

from app.crypto import dump_encrypted_json, load_encrypted_json
from app.extensions import db
from app.models import (
    AIProviderConfig, AITaskConfig, Attachment, CandidateAsset, Contact,
    Interview, Job, Submission,
)
from tests.conftest import ADMIN_PASSWORD, ADMIN_USERNAME, USER_PASSWORD, USER_USERNAME


def _login(client, username, password):
    return client.post(
        "/login", data={"username": username, "password": password}, follow_redirects=False,
    )


def _login_admin(client, app):
    from app import _seed_users
    with app.app_context():
        _seed_users(app)
    return _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)


def _login_user(client, app):
    from app import _seed_users
    with app.app_context():
        _seed_users(app)
    return _login(client, USER_USERNAME, USER_PASSWORD)


@pytest.fixture
def job(app_context):
    j = Job(company="Acme", title="Engineer", status="Applied")
    db.session.add(j)
    db.session.commit()
    return j


# --------------------------------------------------------------------------- #
# /jobs/<id>/delete -- admin_required. Gate coverage: test_auth.py.
# --------------------------------------------------------------------------- #

def test_job_delete_happy_path_removes_job_and_unlinks_submissions(client, app, app_context):
    j = Job(company="Acme", title="Engineer", status="Applied")
    db.session.add(j)
    db.session.commit()
    sub = Submission(job_id=j.id, company="Acme", role_title="Engineer")
    db.session.add(sub)
    db.session.commit()
    job_id, sub_id = j.id, sub.id

    _login_admin(client, app)
    resp = client.post(f"/jobs/{job_id}/delete", data={}, follow_redirects=False)
    assert resp.status_code == 302

    assert db.session.get(Job, job_id) is None
    # Submissions are kept, but unlinked from the deleted job.
    kept = db.session.get(Submission, sub_id)
    assert kept is not None
    assert kept.job_id is None


# --------------------------------------------------------------------------- #
# /jobs/bulk-update -- login_required only, no admin gate.
# --------------------------------------------------------------------------- #

def test_jobs_bulk_update_requires_login(client, job):
    resp = client.post("/jobs/bulk-update",
                       data={"job_ids": [str(job.id)], "action": "withdrawn"},
                       follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_jobs_bulk_update_happy_path_non_admin_can_update(client, app, job):
    _login_user(client, app)
    resp = client.post("/jobs/bulk-update",
                       data={"job_ids": [str(job.id)], "action": "withdrawn"},
                       follow_redirects=False)
    assert resp.status_code == 302
    db.session.refresh(job)
    assert job.status == "Withdrawn"


def test_jobs_bulk_update_set_status_rejects_invalid_status(client, app, job):
    _login_user(client, app)
    resp = client.post("/jobs/bulk-update",
                       data={"job_ids": [str(job.id)], "action": "set_status",
                             "status": "Not-A-Real-Status"},
                       follow_redirects=False)
    assert resp.status_code == 302
    db.session.refresh(job)
    assert job.status == "Applied"  # unchanged


# --------------------------------------------------------------------------- #
# /interviews/<id>/delete -- login_required only.
# --------------------------------------------------------------------------- #

def test_interview_delete_requires_login(client, job):
    iv = Interview(job_id=job.id, round_type="Phone screen")
    db.session.add(iv)
    db.session.commit()
    resp = client.post(f"/interviews/{iv.id}/delete", data={}, follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]
    assert db.session.get(Interview, iv.id) is not None


def test_interview_delete_happy_path(client, app, job):
    iv = Interview(job_id=job.id, round_type="Phone screen")
    db.session.add(iv)
    db.session.commit()
    iv_id = iv.id

    _login_user(client, app)
    resp = client.post(f"/interviews/{iv_id}/delete", data={}, follow_redirects=False)
    assert resp.status_code == 302
    assert db.session.get(Interview, iv_id) is None


# --------------------------------------------------------------------------- #
# /contacts/<id>/delete -- login_required only.
# --------------------------------------------------------------------------- #

def test_contact_delete_requires_login(app_context, client):
    c = Contact(name="Jane Recruiter", contact_type="Recruiter")
    db.session.add(c)
    db.session.commit()
    resp = client.post(f"/contacts/{c.id}/delete", data={}, follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]
    assert db.session.get(Contact, c.id) is not None


def test_contact_delete_happy_path(client, app, app_context):
    c = Contact(name="Jane Recruiter", contact_type="Recruiter")
    db.session.add(c)
    db.session.commit()
    contact_id = c.id

    _login_user(client, app)
    resp = client.post(f"/contacts/{contact_id}/delete", data={}, follow_redirects=False)
    assert resp.status_code == 302
    assert db.session.get(Contact, contact_id) is None


# --------------------------------------------------------------------------- #
# /submissions/<id>/delete -- login_required only.
# --------------------------------------------------------------------------- #

def test_submission_delete_requires_login(app_context, client):
    c = Contact(name="Jane Recruiter", contact_type="Recruiter")
    db.session.add(c)
    db.session.commit()
    sub = Submission(contact_id=c.id, company="Acme", role_title="Engineer")
    db.session.add(sub)
    db.session.commit()
    resp = client.post(f"/submissions/{sub.id}/delete", data={}, follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]
    assert db.session.get(Submission, sub.id) is not None


def test_submission_delete_happy_path(client, app, app_context):
    c = Contact(name="Jane Recruiter", contact_type="Recruiter")
    db.session.add(c)
    db.session.commit()
    sub = Submission(contact_id=c.id, company="Acme", role_title="Engineer")
    db.session.add(sub)
    db.session.commit()
    sub_id = sub.id

    _login_user(client, app)
    resp = client.post(f"/submissions/{sub_id}/delete", data={}, follow_redirects=False)
    assert resp.status_code == 302
    assert db.session.get(Submission, sub_id) is None


# --------------------------------------------------------------------------- #
# /attachments/<id>/delete -- login_required only.
# --------------------------------------------------------------------------- #

def _make_attachment(app, job_id):
    att = Attachment(job_id=job_id, kind="Resume", original_name="resume.pdf",
                     stored_name="attach-test.pdf")
    db.session.add(att)
    db.session.commit()
    upload_dir = app.config["UPLOAD_DIR"]
    os.makedirs(upload_dir, exist_ok=True)
    with open(os.path.join(upload_dir, att.stored_name), "w") as f:
        f.write("fake pdf bytes")
    return att


def test_attachment_delete_requires_login(client, app, app_context, job):
    att = _make_attachment(app, job.id)
    resp = client.post(f"/attachments/{att.id}/delete", data={}, follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]
    assert db.session.get(Attachment, att.id) is not None


def test_attachment_delete_happy_path_removes_row_and_file(client, app, app_context, job):
    att = _make_attachment(app, job.id)
    att_id = att.id
    path_on_disk = os.path.join(app.config["UPLOAD_DIR"], att.stored_name)
    assert os.path.exists(path_on_disk)

    _login_user(client, app)
    resp = client.post(f"/attachments/{att_id}/delete", data={}, follow_redirects=False)
    assert resp.status_code == 302
    assert db.session.get(Attachment, att_id) is None
    assert not os.path.exists(path_on_disk)


# --------------------------------------------------------------------------- #
# /settings/mcp-revoke-token and /settings/mcp-revoke-all -- admin_required.
# --------------------------------------------------------------------------- #

def _oauth_token_path(app):
    return os.path.join(app.config["DATA_DIR"], "oauth_tokens.json")


def _seed_tokens(app, tokens):
    """tokens: dict of raw_token -> meta. Writes the encrypted store directly,
    mirroring how app/mcp_server.py's OAuth flow persists issued tokens."""
    with app.app_context():
        secret = app.config["SECRET_KEY"]
        dump_encrypted_json(_oauth_token_path(app), secret, tokens)


def test_mcp_revoke_token_requires_login(client, app):
    resp = client.post("/settings/mcp-revoke-token", data={"token_id": "whatever"},
                       follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_mcp_revoke_token_forbids_non_admin(client, app):
    _login_user(client, app)
    resp = client.post("/settings/mcp-revoke-token", data={"token_id": "whatever"},
                       follow_redirects=False)
    assert resp.status_code == 403


def test_mcp_revoke_token_happy_path(client, app):
    now = time.time()
    raw = "raw-token-abc"
    token_id = hashlib.sha256(raw.encode()).hexdigest()
    _seed_tokens(app, {raw: {"client_name": "Claude", "issued_at": now, "exp": now + 3600}})

    _login_admin(client, app)
    resp = client.post("/settings/mcp-revoke-token", data={"token_id": token_id},
                       follow_redirects=True)
    assert resp.status_code == 200

    with app.app_context():
        remaining = load_encrypted_json(_oauth_token_path(app), app.config["SECRET_KEY"], default={})
    assert raw not in remaining


def test_mcp_revoke_all_requires_login(client, app):
    resp = client.post("/settings/mcp-revoke-all", data={}, follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_mcp_revoke_all_forbids_non_admin(client, app):
    _login_user(client, app)
    resp = client.post("/settings/mcp-revoke-all", data={}, follow_redirects=False)
    assert resp.status_code == 403


def test_mcp_revoke_all_happy_path_clears_every_token(client, app):
    now = time.time()
    _seed_tokens(app, {
        "raw-1": {"client_name": "A", "issued_at": now, "exp": now + 3600},
        "raw-2": {"client_name": "B", "issued_at": now, "exp": now + 3600},
    })

    _login_admin(client, app)
    resp = client.post("/settings/mcp-revoke-all", data={}, follow_redirects=True)
    assert resp.status_code == 200
    assert b"Revoked 2 token" in resp.data

    with app.app_context():
        remaining = load_encrypted_json(_oauth_token_path(app), app.config["SECRET_KEY"], default={})
    assert remaining == {}


# --------------------------------------------------------------------------- #
# /settings/ai/providers/<id>/delete -- admin_required.
# --------------------------------------------------------------------------- #

def test_ai_provider_delete_requires_login(app_context, client):
    p = AIProviderConfig(rank=1, provider="anthropic", label="Claude")
    db.session.add(p)
    db.session.commit()
    pid = p.id
    resp = client.post(f"/settings/ai/providers/{pid}/delete", data={}, follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]
    assert db.session.get(AIProviderConfig, pid) is not None
    # Blocked by the gate, so never reached the route -- clean up manually so
    # this leftover row doesn't skew the rank-resequencing assertion in
    # test_ai_provider_delete_happy_path_nulls_fks_and_resequences_ranks.
    db.session.delete(db.session.get(AIProviderConfig, pid))
    db.session.commit()


def test_ai_provider_delete_forbids_non_admin(app_context, client, app):
    p = AIProviderConfig(rank=1, provider="anthropic", label="Claude")
    db.session.add(p)
    db.session.commit()
    pid = p.id
    _login_user(client, app)
    resp = client.post(f"/settings/ai/providers/{pid}/delete", data={}, follow_redirects=False)
    assert resp.status_code == 403
    assert db.session.get(AIProviderConfig, pid) is not None
    db.session.delete(db.session.get(AIProviderConfig, pid))
    db.session.commit()


def test_ai_provider_delete_happy_path_nulls_fks_and_resequences_ranks(client, app, app_context):
    # Other test modules (e.g. test_ai_context_capacity.py) leave
    # AIProviderConfig rows behind in this shared session-scoped DB; reset
    # to a known, isolated state before checking exact rank resequencing.
    AIProviderConfig.query.delete()
    db.session.commit()
    p1 = AIProviderConfig(rank=1, provider="anthropic", label="Primary")
    p2 = AIProviderConfig(rank=2, provider="openai", label="Backup")
    db.session.add_all([p1, p2])
    db.session.commit()
    # app/__init__.py seeds one AITaskConfig row per AI_TASK_NAMES on app
    # creation -- reuse the "triage" row rather than inserting a duplicate
    # (task_name is unique).
    tc = AITaskConfig.query.filter_by(task_name="triage").first()
    assert tc is not None, "expected app init to have seeded a 'triage' AITaskConfig row"
    tc.provider_id = p1.id
    tc.backup_provider_id = p1.id
    db.session.commit()
    p1_id, p2_id = p1.id, p2.id

    _login_admin(client, app)
    resp = client.post(f"/settings/ai/providers/{p1_id}/delete", data={}, follow_redirects=False)
    assert resp.status_code == 302

    assert db.session.get(AIProviderConfig, p1_id) is None
    remaining = db.session.get(AIProviderConfig, p2_id)
    assert remaining is not None
    assert remaining.rank == 1  # re-sequenced after the gap

    db.session.refresh(tc)
    assert tc.provider_id is None
    assert tc.backup_provider_id is None
    # tc is a shared seeded singleton row (task_name="triage") -- leave it in
    # place (already reset to None/None by the route) rather than deleting it,
    # so later tests still find a "triage" row if they look for one.
    db.session.delete(remaining)
    db.session.commit()


# --------------------------------------------------------------------------- #
# /assets/<id>/delete -- admin_required. Happy path: test_onboarding.py.
# --------------------------------------------------------------------------- #

def _make_asset(app_context):
    asset = CandidateAsset(kind="Certification", original_name="cert.pdf",
                           stored_name="cert-test.pdf")
    db.session.add(asset)
    db.session.commit()
    return asset


def test_asset_delete_requires_login(app_context, client):
    asset = _make_asset(app_context)
    resp = client.post(f"/assets/{asset.id}/delete", data={}, follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]
    assert db.session.get(CandidateAsset, asset.id) is not None


def test_asset_delete_forbids_non_admin(app_context, client, app):
    asset = _make_asset(app_context)
    _login_user(client, app)
    resp = client.post(f"/assets/{asset.id}/delete", data={}, follow_redirects=False)
    assert resp.status_code == 403
    assert db.session.get(CandidateAsset, asset.id) is not None


# --------------------------------------------------------------------------- #
# /api/ingest -- X-API-Key auth, no session/CSRF (csrf-exempt).
# --------------------------------------------------------------------------- #

INGEST_URL = "/api/ingest"


def test_ingest_rejects_missing_key(client, monkeypatch):
    monkeypatch.setenv("INGEST_API_KEY", "correct-key")
    resp = client.post(INGEST_URL, json={"jobs": []})
    assert resp.status_code == 401
    assert resp.get_json()["error"] == "unauthorized"


def test_ingest_rejects_wrong_key(client, monkeypatch):
    monkeypatch.setenv("INGEST_API_KEY", "correct-key")
    resp = client.post(INGEST_URL, json={"jobs": []}, headers={"X-API-Key": "wrong-key"})
    assert resp.status_code == 401


def test_ingest_rejects_everything_when_key_unconfigured(client, monkeypatch):
    """An empty/unset INGEST_API_KEY must not act as an "anything goes" wildcard."""
    monkeypatch.setenv("INGEST_API_KEY", "")
    resp = client.post(INGEST_URL, json={"jobs": []}, headers={"X-API-Key": ""})
    assert resp.status_code == 401


def test_ingest_rejects_non_json_body(client, monkeypatch):
    monkeypatch.setenv("INGEST_API_KEY", "correct-key")
    resp = client.post(INGEST_URL, data="not json", headers={"X-API-Key": "correct-key"})
    assert resp.status_code == 400


def test_ingest_happy_path_creates_job(client, app, app_context, monkeypatch):
    monkeypatch.setenv("INGEST_API_KEY", "correct-key")
    payload = {
        "jobs": [
            {"title": "Data Engineer", "company": "NewCo", "location": "Remote",
             "source": "jobicy", "external_id": "abc123", "url": "https://example.com/j/1"},
        ],
        "created_by": "test-harness",
    }
    resp = client.post(INGEST_URL, json=payload, headers={"X-API-Key": "correct-key"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["created"] == 1
    assert body["skipped"] == 0
    assert len(body["ids"]) == 1

    row = db.session.get(Job, body["ids"][0])
    assert row is not None
    assert row.company == "NewCo"
    assert row.title == "Data Engineer"
    assert row.created_by == "test-harness"


def test_ingest_skips_duplicate_external_id(client, app, app_context, monkeypatch):
    monkeypatch.setenv("INGEST_API_KEY", "correct-key")
    existing = Job(company="NewCo", title="Data Engineer", source="jobicy",
                   external_id="dup-1", status="Saved")
    db.session.add(existing)
    db.session.commit()

    payload = {"jobs": [
        {"title": "Data Engineer", "company": "NewCo", "source": "jobicy", "external_id": "dup-1"},
    ]}
    resp = client.post(INGEST_URL, json=payload, headers={"X-API-Key": "correct-key"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["created"] == 0
    assert body["skipped"] == 1
