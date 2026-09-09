# Copyright (C) 2026 D. Brandmeyer
# Licensed under the GNU Affero General Public License v3 or later.
"""Full-route-map smoke test (QUAL-01, 2026-09-08 audit, Long-term item 17).

Blueprint endpoint names (e.g. ``main.dashboard``) only get resolved by
``url_for()`` at template-render time, not at import time — a template still
imports and the app still boots even if a template's ``url_for('main.x')``
now points at a blueprint that was renamed or split. That failure mode is
silent until a human happens to click the specific broken link, which is
exactly the risk QUAL-01's ``main.py`` blueprint split creates. This test
hits every GET-able route in the live ``url_map`` once, as a logged-in admin,
and asserts none of them 500 — so a missed ``url_for`` rename anywhere in
``app/templates/`` fails CI instead of production.

Deliberately excluded:
  - ``static`` (not an application route).
  - ``auth.logout`` (would end the shared client's session mid-sweep; it's
    in ``app/auth.py``, untouched by the blueprint split this test guards).
"""
import uuid

import pytest

from app.extensions import db
from app.models import (
    AIProviderConfig, Attachment, CandidateAsset, Contact, Interview, Job, Submission,
)
from tests.conftest import login_admin as _login_admin

EXCLUDED_ENDPOINTS = {"static", "auth.logout"}


@pytest.fixture
def seeded_ids(app_context, app):
    job = Job(company="Acme", title="Engineer", status="Applied")
    contact = Contact(name="Jane Recruiter", contact_type="Recruiter")
    db.session.add_all([job, contact])
    db.session.commit()

    interview = Interview(job_id=job.id, round_type="Phone screen")
    submission = Submission(job_id=job.id, contact_id=contact.id, company="Acme", role_title="Engineer")
    db.session.add_all([interview, submission])
    db.session.commit()

    attachment = Attachment(job_id=job.id, kind="Resume", original_name="resume.pdf",
                             stored_name="resume.pdf")
    provider = AIProviderConfig(rank=99, provider="anthropic", label="Smoke-test provider")
    asset = CandidateAsset(kind="Certification", original_name="cert.pdf", stored_name="cert.pdf")
    db.session.add_all([attachment, provider, asset])
    db.session.commit()

    return {
        "job_id": job.id,
        "contact_id": contact.id,
        "iv_id": interview.id,
        "sub_id": submission.id,
        "att_id": attachment.id,
        "asset_id": asset.id,  # no file on disk written; asset_download 404s cleanly
        "pid": provider.id,
        "run_id": uuid.uuid4().hex,
        "task": "triage",
        "provider": "anthropic",
        "page": "index",
        "step": "accounts",
    }


def test_every_get_route_renders_without_a_server_error(client, app, seeded_ids):
    _login_admin(client)

    adapter = app.url_map.bind("localhost")
    failures = []
    checked = 0

    for rule in app.url_map.iter_rules():
        if rule.endpoint in EXCLUDED_ENDPOINTS or "GET" not in rule.methods:
            continue
        try:
            url = adapter.build(rule.endpoint, values=seeded_ids, method="GET")
        except Exception as exc:  # noqa: BLE001 - report, don't crash the sweep
            failures.append(f"{rule.endpoint} ({rule.rule}): could not build URL: {exc!r}")
            continue

        checked += 1
        resp = client.get(url)
        if resp.status_code >= 500:
            failures.append(f"{rule.endpoint} ({url}): HTTP {resp.status_code}")

    assert checked > 30, f"expected to sweep >30 GET routes, only checked {checked} -- url_map may be empty"
    assert not failures, "route(s) returned a 5xx or failed to build:\n" + "\n".join(failures)
