# Copyright (C) 2026 D. Brandmeyer
# Licensed under the GNU Affero General Public License v3 or later.
"""Tests for two fixes to the manual Triage Batch tool against slow local
providers (Ollama/LiteLLM).

Background: the manual backlog tool (`run_triage_batch`, /tools/triage-batch)
builds ONE prompt containing the whole batch and fires a single AI call. Two
problems surfaced with a local Ollama provider:

  1. `call_openai_compat` used a fixed 55s read timeout. That's fine for a
     hosted API, but a local model *generates* the whole multi-job response on
     the user's own hardware, which routinely takes minutes -- so a perfectly
     healthy run was aborted mid-generation with a read timeout. Fixed by
     giving local providers a much longer timeout (`_http_timeout_for`).

  2. When that initial call raised, `run_triage_batch` marked all jobs failed
     and returned immediately -- it never reached the sub-batch-of-5 -> solo
     retry ladder that already existed for jobs missing from a *successful*
     response. Fixed so an initial-call failure falls through to the ladder,
     which feeds the provider smaller prompts it can actually finish in time.
"""
import json
import re

import pytest
import requests

from app import ai
from app.extensions import db
from app.models import AIProviderConfig, Job


# ---------------------------------------------------------------------------
# _http_timeout_for + call_openai_compat timeout wiring -- no DB needed
# ---------------------------------------------------------------------------

def test_http_timeout_for_local_vs_cloud():
    assert ai._http_timeout_for("ollama") == ai._LOCAL_HTTP_TIMEOUT
    assert ai._http_timeout_for("litellm") == ai._LOCAL_HTTP_TIMEOUT
    assert ai._http_timeout_for("groq") == ai._CLOUD_HTTP_TIMEOUT
    assert ai._http_timeout_for("") == ai._CLOUD_HTTP_TIMEOUT
    # Local timeout must be materially larger, or the fix is pointless.
    assert ai._LOCAL_HTTP_TIMEOUT > ai._CLOUD_HTTP_TIMEOUT


def _patch_post(monkeypatch):
    """Capture the timeout kwarg passed to requests.post; return a 200 stub."""
    seen = {}

    class _Resp:
        ok = True
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

    def fake_post(url, headers=None, json=None, timeout=None):  # noqa: A002
        seen["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(ai.requests, "post", fake_post)
    return seen


def test_call_openai_compat_uses_local_timeout_for_ollama(monkeypatch):
    seen = _patch_post(monkeypatch)
    ai.call_openai_compat("http://localhost:11434/v1", "", "gemma4:12b",
                          "sys", "user", 1024, "ollama")
    assert seen["timeout"] == ai._LOCAL_HTTP_TIMEOUT


def test_call_openai_compat_uses_cloud_timeout_for_hosted(monkeypatch):
    seen = _patch_post(monkeypatch)
    ai.call_openai_compat("https://api.groq.example/v1", "k", "llama-3.1-8b-instant",
                          "sys", "user", 1024, "groq")
    assert seen["timeout"] == ai._CLOUD_HTTP_TIMEOUT


def test_call_openai_compat_explicit_timeout_wins(monkeypatch):
    seen = _patch_post(monkeypatch)
    ai.call_openai_compat("http://localhost:11434/v1", "", "gemma4:12b",
                          "sys", "user", 1024, "ollama", timeout=7)
    assert seen["timeout"] == 7


# ---------------------------------------------------------------------------
# run_triage_batch: an initial-call failure must fall through to the retry
# ladder rather than failing the whole batch.
# ---------------------------------------------------------------------------

@pytest.fixture
def ollama_provider(app_context):
    # The test DB persists across the session (no per-test reset), so start from
    # an empty Job table -- run_triage_batch scans ALL unscored Saved jobs, and
    # leftovers from other tests would otherwise be swept into this batch.
    Job.query.delete()
    db.session.commit()
    row = AIProviderConfig(
        provider="ollama", model="gemma4:12b", rank=1, enabled=True,
        base_url="http://localhost:11434/v1",
    )
    db.session.add(row)
    db.session.commit()
    pid = row.id
    yield pid
    Job.query.delete()
    AIProviderConfig.query.delete()
    db.session.commit()


def _make_saved_jobs(n):
    jobs = [
        Job(company=f"Co{i}", title=f"Role {i}", status="Saved",
            notes="A job description.", ai_fit_score=None)
        for i in range(n)
    ]
    db.session.add_all(jobs)
    db.session.commit()
    return [j.id for j in jobs]


def test_initial_timeout_recovers_via_retry_ladder(ollama_provider, monkeypatch):
    """Initial 8-job call times out; the sub-batch-of-5 retries succeed, so
    every job still gets scored and none are reported failed."""
    pid = ollama_provider
    _make_saved_jobs(8)

    calls = {"big": 0, "small": 0}

    def fake_call(*args, **kwargs):
        content = args[4] if len(args) > 4 else kwargs.get("user_content", "")
        ids = [int(m) for m in re.findall(r'"id":\s*(\d+)', content)]
        # The single full-batch prompt (>5 jobs) is what the slow local model
        # can't finish in time -- simulate the read timeout it produced.
        if len(ids) > 5:
            calls["big"] += 1
            raise requests.Timeout("HTTPConnectionPool: Read timed out. (read timeout=300)")
        calls["small"] += 1
        return json.dumps([{"id": i, "score": 7, "reason": "good fit"} for i in ids])

    monkeypatch.setattr(ai, "call_openai_compat", fake_call)

    result = ai.run_triage_batch(offset=0, limit=20, provider_id=pid)

    assert calls["big"] == 1        # the initial batch was attempted once
    assert calls["small"] >= 1      # and the retry ladder took over
    assert result["scored"] == 8
    assert result["failed"] == 0
    assert all(r["ok"] for r in result["results"])

    for jid in [r["id"] for r in result["results"]]:
        assert db.session.get(Job, jid).ai_fit_score == 7


def test_persistent_failure_still_reports_failed(ollama_provider, monkeypatch):
    """If every attempt (batch and retries) fails, jobs are reported failed
    rather than silently vanishing -- the ladder terminates."""
    pid = ollama_provider
    _make_saved_jobs(3)

    def always_timeout(*args, **kwargs):
        raise requests.Timeout("read timeout=300")

    monkeypatch.setattr(ai, "call_openai_compat", always_timeout)

    result = ai.run_triage_batch(offset=0, limit=20, provider_id=pid)

    assert result["scored"] == 0
    assert result["failed"] == 3
    assert all(not r["ok"] for r in result["results"])
