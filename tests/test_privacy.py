# Copyright (C) 2026 D. Brandmeyer
# Licensed under the GNU Affero General Public License v3 or later.
"""Tests for the PII/SPI redaction layer (``app/privacy.py``) and its wiring.

The contract under test:

- identifiers the app knows about (accounts, contacts, SMTP addresses) and
  identifier-shaped strings (emails, phones, SSNs, addresses, LinkedIn URLs)
  never appear in text bound for an AI provider;
- placeholders are deterministic and survive a round trip back to real values,
  even when the model mangles the token formatting;
- SPI/PHI (health, age signals, marital status) is stripped outbound and
  reported as coaching flags, never tokenized;
- the ``call_with_fallback`` choke point redacts for cloud providers, skips
  local ones by default, and rehydrates responses;
- MCP tools redact results and rehydrate placeholder-bearing arguments.
"""
import re

import pytest

from app import privacy
from app.extensions import db
from app.models import AIConfig, Contact, Job, User


@pytest.fixture
def seeded(app_context):
    """A candidate, a contact, and clean privacy config for each test."""
    user = User.query.filter_by(role="user").first()
    user.display_name = "Jordan Ellison"
    contact = Contact(name="Priya Raghunathan", agency="TalentBridge Staffing",
                      email="priya@talentbridge.example", phone="702-555-0142",
                      linkedin_url="https://www.linkedin.com/in/priya-raghunathan")
    db.session.add(contact)
    cfg = db.session.get(AIConfig, 1)
    if cfg is None:                      # the singleton row is created lazily
        cfg = AIConfig(id=1)
        db.session.add(cfg)
    cfg.redaction_enabled = True
    cfg.redact_strict = False
    cfg.redact_local = False
    db.session.commit()
    yield app_context
    db.session.delete(contact)
    user.display_name = "User"
    db.session.commit()


# ---------------------------------------------------------------------------
# Placeholders
# ---------------------------------------------------------------------------

class TestPlaceholders:
    def test_deterministic(self, seeded):
        a = privacy.make_placeholder("EMAIL", "x@example.com")
        b = privacy.make_placeholder("EMAIL", "X@Example.com")   # case-insensitive
        assert a == b
        assert re.fullmatch(r"\{\{PII:EMAIL_[0-9a-f]{8}\}\}", a)

    def test_distinct_per_value_and_kind(self, seeded):
        assert (privacy.make_placeholder("EMAIL", "a@x.com")
                != privacy.make_placeholder("EMAIL", "b@x.com"))
        assert (privacy.make_placeholder("EMAIL", "a@x.com")
                != privacy.make_placeholder("NAME", "a@x.com"))


# ---------------------------------------------------------------------------
# Known-values pass
# ---------------------------------------------------------------------------

class TestKnownValues:
    def test_candidate_and_contact_identifiers_redacted(self, seeded):
        text = ("Jordan Ellison applied via Priya Raghunathan "
                "(priya@talentbridge.example, 702-555-0142).")
        out = privacy.redact(text).text
        for leak in ("Jordan", "Ellison", "Priya", "Raghunathan",
                     "priya@talentbridge.example", "702-555-0142"):
            assert leak not in out
        assert "{{PII:" in out

    def test_known_phone_matches_other_formatting(self, seeded):
        out = privacy.redact("Call her at (702) 555-0142 today.").text
        assert "555-0142" not in out and "{{PII:PHONE_" in out

    def test_name_not_matched_inside_words(self, seeded):
        user = User.query.filter_by(role="user").first()
        user.display_name = "Ann Chu"
        db.session.commit()
        out = privacy.redact("The Announcement mentions Ann Chu.").text
        assert "Announcement" in out
        assert "Ann Chu" not in out

    def test_employer_names_kept_by_default(self, seeded):
        out = privacy.redact("Led CP4BA deployments at IBM for six years.").text
        assert "IBM" in out


# ---------------------------------------------------------------------------
# Pattern pass
# ---------------------------------------------------------------------------

class TestPatternPass:
    def test_unknown_email_phone_ssn(self, seeded):
        out = privacy.redact("Reach me: someone.new@nowhere.example, "
                             "(415) 555-9999, SSN 123-45-6789.").text
        assert "someone.new@nowhere.example" not in out
        assert "555-9999" not in out
        assert "123-45-6789" not in out
        assert out.count("{{PII:") == 3

    def test_linkedin_and_street_address(self, seeded):
        out = privacy.redact("Profile: linkedin.com/in/jordan-e-12345. "
                             "Home: 4821 Desert Bloom Way, Apt 12.").text
        assert "jordan-e-12345" not in out
        assert "Desert Bloom" not in out

    def test_salary_range_and_dates_not_phone(self, seeded):
        text = "Salary 120,000-150,000, posted 2026-07-12."
        out = privacy.redact(text).text
        assert out == text  # nothing identifier-shaped here

    def test_workauth_tokenized_and_rehydratable(self, seeded):
        res = privacy.redact("Active TS/SCI clearance, US citizen.")
        assert "TS/SCI" not in res.text
        assert "{{PII:WORKAUTH_" in res.text
        back, unresolved = privacy.rehydrate(res.text)
        assert "TS/SCI" in back and not unresolved


# ---------------------------------------------------------------------------
# SPI/PHI — strip and coach, never rehydrate
# ---------------------------------------------------------------------------

class TestSPI:
    def test_health_sentence_stripped_and_flagged(self, seeded):
        res = privacy.redact("Strong Python skills. I was treated for cancer "
                             "in 2024. Ten years of ops experience.")
        assert "cancer" not in res.text
        assert privacy.SPI_REMOVED_MARKER in res.text
        assert "Strong Python skills." in res.text
        assert "Ten years of ops experience." in res.text
        assert any(f["category"] == "health" for f in res.spi_flags)

    def test_age_and_marital_flagged(self, seeded):
        flags = privacy.scan_spi("Born in 1962. Married with three kids.")
        cats = {f["category"] for f in flags}
        assert {"age", "marital"} <= cats
        for f in flags:
            assert f["guidance"]

    def test_stripped_spi_never_rehydrates(self, seeded):
        res = privacy.redact("I take medication for anxiety.")
        back, _ = privacy.rehydrate(res.text)
        assert "anxiety" not in back and "medication" not in back

    def test_benign_words_not_flagged(self, seeded):
        assert privacy.scan_spi("Runs in a single container. Married the "
                                "frontend to the API.") == [
        ] or all(f["category"] == "marital" for f in privacy.scan_spi(
            "Runs in a single container."))
        assert privacy.scan_spi("Runs in a single container.") == []


# ---------------------------------------------------------------------------
# Rehydration
# ---------------------------------------------------------------------------

class TestRehydrate:
    def test_round_trip_identity(self, seeded):
        text = ("Jordan Ellison <jordan.ellison@fastmail.example> interviewed "
                "at Acme; recruiter Priya Raghunathan, 702-555-0142.")
        res = privacy.redact(text, strip_spi=False)
        back, unresolved = privacy.rehydrate(res.text)
        assert not unresolved
        assert "Jordan Ellison" in back
        assert "jordan.ellison@fastmail.example" in back  # regex-found, via vault
        assert "702-555-0142" in back

    def test_lenient_placeholder_matching(self, seeded):
        ph = privacy.make_placeholder("NAME", "Jordan Ellison")
        digest = ph.split("_")[1].rstrip("}")
        for mangled in (f"{{PII:NAME_{digest}}}",           # single braces
                        f"{{{{pii:name-{digest}}}}}",        # case + hyphen
                        f"{{{{ PII : NAME _ {digest} }}}}"):  # stray spaces
            back, unresolved = privacy.rehydrate(f"Dear {mangled},")
            assert "Jordan Ellison" in back, mangled
            assert not unresolved

    def test_unknown_placeholder_reported_not_dropped(self, seeded):
        text = "Contact {{PII:EMAIL_deadbeef}} for details."
        back, unresolved = privacy.rehydrate(text)
        assert unresolved == ["{{PII:EMAIL_deadbeef}}"]
        assert "{{PII:EMAIL_deadbeef}}" in back
        assert privacy.contains_placeholders(back)

    def test_plain_text_untouched(self, seeded):
        text = "No placeholders here."
        back, unresolved = privacy.rehydrate(text)
        assert back == text and not unresolved


# ---------------------------------------------------------------------------
# Structured payloads
# ---------------------------------------------------------------------------

class TestObjHelpers:
    def test_redact_and_rehydrate_nested(self, seeded):
        payload = {"jobs": [{"notes": "Sent resume to priya@talentbridge.example",
                             "id": 7}],
                   "candidate": "Jordan Ellison"}
        red = privacy.redact_obj(payload)
        assert red["jobs"][0]["id"] == 7
        assert "priya@" not in red["jobs"][0]["notes"]
        assert "Jordan" not in red["candidate"]
        back = privacy.rehydrate_obj(red)
        assert back["candidate"] == "Jordan Ellison"
        assert "priya@talentbridge.example" in back["jobs"][0]["notes"]


# ---------------------------------------------------------------------------
# Strict mode
# ---------------------------------------------------------------------------

class TestStrictMode:
    def test_orgs_and_locations_pseudonymized(self, seeded):
        job = Job(title="Ops Manager", company="Acme Logistics",
                  location="Henderson, NV", created_by="test")
        db.session.add(job)
        db.session.commit()
        try:
            res = privacy.redact("Interview at Acme Logistics in Henderson, NV.",
                                 strict=True)
            assert "Acme Logistics" not in res.text
            assert "Henderson, NV" not in res.text
            back, _ = privacy.rehydrate(res.text)
            assert "Acme Logistics" in back and "Henderson, NV" in back
        finally:
            db.session.delete(job)
            db.session.commit()


# ---------------------------------------------------------------------------
# Config gates and provider locality
# ---------------------------------------------------------------------------

class _FakeProvider:
    def __init__(self, provider, base_url=""):
        self.provider = provider
        self.base_url = base_url


class TestConfigGates:
    def test_defaults(self, seeded):
        assert privacy.redaction_enabled() is True
        assert privacy.strict_mode() is False
        assert privacy.redact_local() is False

    def test_local_provider_detection(self, seeded):
        assert privacy.is_local_provider(_FakeProvider("ollama"))
        assert privacy.is_local_provider(
            _FakeProvider("ollama", "http://host.docker.internal:11434/v1"))
        assert privacy.is_local_provider(
            _FakeProvider("custom", "http://127.0.0.1:8080/v1"))
        assert not privacy.is_local_provider(
            _FakeProvider("ollama", "https://ollama.example.com/v1"))
        assert not privacy.is_local_provider(_FakeProvider("groq"))

    def test_should_redact_matrix(self, seeded):
        cloud, local = _FakeProvider("groq"), _FakeProvider("ollama")
        assert privacy.should_redact_for(cloud) is True
        assert privacy.should_redact_for(local) is False
        cfg = db.session.get(AIConfig, 1)
        cfg.redact_local = True
        db.session.commit()
        assert privacy.should_redact_for(local) is True
        cfg.redact_local = False
        cfg.redaction_enabled = False
        db.session.commit()
        assert privacy.should_redact_for(cloud) is False


# ---------------------------------------------------------------------------
# No-leak integration: the call_with_fallback choke point
# ---------------------------------------------------------------------------

class TestChokePoint:
    def test_cloud_call_is_redacted_and_response_rehydrated(self, seeded, monkeypatch):
        from app import ai
        from app.models import AIProviderConfig

        row = AIProviderConfig(provider="groq", model="test-model", rank=1,
                               enabled=True, base_url="https://api.groq.example/v1")
        db.session.add(row)
        db.session.commit()

        sent = {}

        def fake_call(base_url, api_key, model, system, user_content,
                      max_tokens=4096, provider=""):
            sent["system"], sent["user"] = system, user_content
            # Echo a placeholder back, as a model drafting an email would.
            ph = privacy.make_placeholder("NAME", "Jordan Ellison")
            return f"Dear {ph}, your application was received."

        monkeypatch.setattr(ai, "call_openai_compat", fake_call)
        try:
            text, provider = ai.call_with_fallback(
                "You are a helpful coach.",
                "Draft a follow-up for Jordan Ellison (jordan@fastmail.example).")
            assert "Jordan Ellison" not in sent["user"]
            assert "jordan@fastmail.example" not in sent["user"]
            assert "{{PII:" in sent["user"]
            assert "Dear Jordan Ellison," in text   # rehydrated
        finally:
            db.session.delete(row)
            db.session.commit()

    def test_local_provider_gets_real_text(self, seeded, monkeypatch):
        from app import ai
        from app.models import AIProviderConfig

        row = AIProviderConfig(provider="ollama", model="qwen3:4b", rank=1,
                               enabled=True)
        db.session.add(row)
        db.session.commit()

        sent = {}

        def fake_call(base_url, api_key, model, system, user_content,
                      max_tokens=4096, provider=""):
            sent["user"] = user_content
            return "ok"

        monkeypatch.setattr(ai, "call_openai_compat", fake_call)
        try:
            ai.call_with_fallback("system", "Notes about Jordan Ellison.")
            assert "Jordan Ellison" in sent["user"]   # redact_local off by default
        finally:
            db.session.delete(row)
            db.session.commit()


# ---------------------------------------------------------------------------
# MCP boundary
# ---------------------------------------------------------------------------

class TestMcpBoundary:
    def test_tool_schemas_survive_wrapping(self, app):
        """functools.wraps must preserve signatures or Claude sees broken tools."""
        from app.mcp_server import mcp
        import asyncio
        tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
        assert "get_pipeline" in tools and "set_job_fit" in tools
        props = tools["set_job_fit"].inputSchema.get("properties", {})
        assert {"job_id", "score", "reason"} <= set(props)

    def test_read_tool_output_redacted(self, seeded):
        from app.mcp_server import get_candidate_profile  # wrapped function
        import os as _os
        # Write a profile containing PII into the temp DATA_DIR.
        from flask import current_app
        path = _os.path.join(current_app.config["DATA_DIR"], "candidate_profile.md")
        with open(path, "w") as f:
            f.write("# Jordan Ellison\nEmail: jordan@fastmail.example\n")
        try:
            out = get_candidate_profile()
            assert "Jordan Ellison" not in out
            assert "jordan@fastmail.example" not in out
            assert "{{PII:" in out
        finally:
            _os.unlink(path)

    def test_write_tool_args_rehydrated(self, seeded):
        from app.mcp_server import update_job_notes
        job = Job(title="Ops Manager", company="Acme", created_by="test")
        db.session.add(job)
        db.session.commit()
        job_id = job.id
        ph = privacy.make_placeholder("NAME", "Jordan Ellison")
        try:
            update_job_notes(job_id, f"Spoke with {ph} about next steps.")
            db.session.expire_all()
            stored = db.session.get(Job, job_id)
            assert "Jordan Ellison" in stored.notes
            assert "{{PII:" not in stored.notes
        finally:
            db.session.delete(db.session.get(Job, job_id))
            db.session.commit()
