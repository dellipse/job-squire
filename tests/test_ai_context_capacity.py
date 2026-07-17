# Copyright (C) 2026 D. Brandmeyer
# Licensed under the GNU Affero General Public License v3 or later.
"""Tests for the context-window capacity guard in ``app/ai.py``
(docs/PLAN-ollama-assist.md) and the adjacent ``triage_model`` fix.

Background: Ollama's OpenAI-compatible endpoint has no per-request way to
set context size, so an over-long prompt against an under-provisioned local
model isn't an error at all -- Ollama silently truncates the input and
answers anyway. ``fits_in_context`` estimates whether a prompt will fit
*before* the call, so ``call_with_fallback`` can skip that provider (same
as an unmet ``use_for_triage``/``use_for_analysis`` flag already does)
instead of accepting a plausible-looking answer generated from a chopped
prompt with no signal anything went wrong -- the risk called out
explicitly for unattended automation (auto-triage, weekly review, etc.).

Also covered here: while wiring the capacity check into ``_try_one``,
``AIProviderConfig.triage_model`` turned out to be read from nowhere in the
call path -- every task used ``model`` regardless of ``is_triage``. Fixed
in the same change; guarded here so it doesn't regress silently again.
"""
import pytest

from app import ai
from app.extensions import db
from app.models import AIConfig, AIProviderConfig

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "admin-test-pw"  # mirrors conftest's seeded credentials


def _login(client):
    return client.post("/login", data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD})


# ---------------------------------------------------------------------------
# estimate_tokens / fits_in_context — pure functions, no DB needed
# ---------------------------------------------------------------------------

def test_estimate_tokens_uses_chars_per_token_heuristic():
    assert ai.estimate_tokens("a" * 400) == 100
    assert ai.estimate_tokens("") == 1  # never zero -- an empty prompt still "costs" something
    assert ai.estimate_tokens(None) == 1


def test_fits_in_context_none_num_ctx_always_fits():
    """No num_ctx configured (cloud provider, or not yet run through
    `job-squire ollama setup`) means the guard stays out of the way entirely."""
    fits, estimated, available = ai.fits_in_context(None, "system", "user" * 10000, max_tokens=4096)
    assert fits is True
    assert estimated == 0
    assert available == 0


def test_fits_in_context_zero_num_ctx_always_fits():
    fits, _, _ = ai.fits_in_context(0, "system", "user", max_tokens=100)
    assert fits is True


def test_fits_in_context_short_prompt_fits_small_window():
    fits, estimated, available = ai.fits_in_context(2048, "You are a coach.", "Score this job.", max_tokens=512)
    assert fits is True
    assert estimated < available


def test_fits_in_context_long_prompt_exceeds_small_window():
    long_user = "x" * 20000  # ~5000 estimated tokens
    fits, estimated, available = ai.fits_in_context(2048, "system", long_user, max_tokens=512)
    assert fits is False
    assert estimated > available


def test_fits_in_context_reserves_max_tokens_and_safety_margin():
    """A prompt that would exactly fill num_ctx with no room left for the
    response or template overhead must not be reported as fitting."""
    # num_ctx=1000, max_tokens=500 -> only ~244 tokens (1000-500-256) available for input.
    borderline_user = "x" * (245 * 4)  # just over the available budget
    fits, estimated, available = ai.fits_in_context(1000, "", borderline_user, max_tokens=500)
    assert available == 244
    assert fits is False
    assert estimated > available


# ---------------------------------------------------------------------------
# call_with_fallback: capacity-based skip, using the same fake-call
# convention as tests/test_privacy.py's TestChokePoint.
# ---------------------------------------------------------------------------

@pytest.fixture
def clean_providers(app_context):
    """Every provider test starts from an empty chain and cleans up after
    itself, mirroring test_privacy.py's `seeded` fixture."""
    cfg = db.session.get(AIConfig, 1)
    if cfg is None:
        cfg = AIConfig(id=1)
        db.session.add(cfg)
    cfg.fallback_to_anthropic = False  # isolate ranked-chain behavior from the legacy Anthropic fallback
    db.session.commit()
    yield app_context
    AIProviderConfig.query.delete()
    db.session.commit()


def test_undersized_local_provider_is_skipped_not_silently_truncated(clean_providers, monkeypatch):
    row = AIProviderConfig(
        provider="ollama", model="qwen3:4b", rank=1, enabled=True,
        base_url="http://localhost:11434/v1", num_ctx=2048,
    )
    db.session.add(row)
    db.session.commit()

    def fail_if_called(*a, **k):
        raise AssertionError("call_openai_compat must not be called for an over-budget provider")

    monkeypatch.setattr(ai, "call_openai_compat", fail_if_called)

    long_prompt = "job description " * 3000  # far beyond a 2048-token window
    with pytest.raises(RuntimeError, match="context window"):
        ai.call_with_fallback("system", long_prompt)


def test_undersized_local_provider_falls_back_to_next_in_chain(clean_providers, monkeypatch):
    small = AIProviderConfig(
        provider="ollama", model="qwen3:4b", rank=1, enabled=True,
        base_url="http://localhost:11434/v1", num_ctx=2048,
    )
    cloud = AIProviderConfig(
        provider="groq", model="llama-3.1-8b-instant", rank=2, enabled=True,
        base_url="https://api.groq.example/v1", num_ctx=None,
    )
    db.session.add_all([small, cloud])
    db.session.commit()

    calls = []

    def fake_call(base_url, api_key, model, system, user_content, max_tokens, provider):
        calls.append(provider)
        return "ok from " + provider

    monkeypatch.setattr(ai, "call_openai_compat", fake_call)

    long_prompt = "job description " * 3000
    text, provider = ai.call_with_fallback("system", long_prompt)

    assert provider == "groq"
    assert calls == ["groq"]  # ollama was skipped before any HTTP call, never appears here


def test_provider_without_num_ctx_is_never_capacity_checked(clean_providers, monkeypatch):
    """A provider row with num_ctx left blank (cloud, or pre-dates `ollama setup`)
    must behave exactly as before this change -- no regression."""
    row = AIProviderConfig(
        provider="groq", model="llama-3.1-8b-instant", rank=1, enabled=True,
        base_url="https://api.groq.example/v1", num_ctx=None,
    )
    db.session.add(row)
    db.session.commit()

    monkeypatch.setattr(ai, "call_openai_compat", lambda *a, **k: "fine")
    long_prompt = "x" * 100000
    text, provider = ai.call_with_fallback("system", long_prompt)
    assert text == "fine"
    assert provider == "groq"


def test_adequately_sized_local_provider_is_used_normally(clean_providers, monkeypatch):
    row = AIProviderConfig(
        provider="ollama", model="gemma4:12b", rank=1, enabled=True,
        base_url="http://localhost:11434/v1", num_ctx=16384,
    )
    db.session.add(row)
    db.session.commit()

    monkeypatch.setattr(ai, "call_openai_compat", lambda *a, **k: "ollama reply")
    text, provider = ai.call_with_fallback("system", "a short prompt")
    assert text == "ollama reply"
    assert provider == "ollama"


# ---------------------------------------------------------------------------
# triage_model fix
# ---------------------------------------------------------------------------

def test_triage_task_uses_triage_model(clean_providers, monkeypatch):
    row = AIProviderConfig(
        provider="ollama", model="gemma4:12b", triage_model="qwen3:4b",
        rank=1, enabled=True, base_url="http://localhost:11434/v1",
    )
    db.session.add(row)
    db.session.commit()

    seen = {}

    def fake_call(base_url, api_key, model, system, user_content, max_tokens, provider):
        seen["model"] = model
        return "ok"

    monkeypatch.setattr(ai, "call_openai_compat", fake_call)
    ai.call_with_fallback("system", "score this job", task_name="triage")
    assert seen["model"] == "qwen3:4b"


def test_analysis_task_uses_model_not_triage_model(clean_providers, monkeypatch):
    row = AIProviderConfig(
        provider="ollama", model="gemma4:12b", triage_model="qwen3:4b",
        rank=1, enabled=True, base_url="http://localhost:11434/v1",
    )
    db.session.add(row)
    db.session.commit()

    seen = {}

    def fake_call(base_url, api_key, model, system, user_content, max_tokens, provider):
        seen["model"] = model
        return "ok"

    monkeypatch.setattr(ai, "call_openai_compat", fake_call)
    ai.call_with_fallback("system", "write the weekly review", task_name="weekly_review")
    assert seen["model"] == "gemma4:12b"


def test_triage_task_falls_back_to_model_when_triage_model_blank(clean_providers, monkeypatch):
    """A row with no triage_model set (every row created through the web UI
    today -- see app/main.py's ai_provider_add/edit) keeps working exactly
    as it did before this change."""
    row = AIProviderConfig(
        provider="ollama", model="gemma4:12b", triage_model="",
        rank=1, enabled=True, base_url="http://localhost:11434/v1",
    )
    db.session.add(row)
    db.session.commit()

    seen = {}
    monkeypatch.setattr(
        ai, "call_openai_compat",
        lambda base_url, api_key, model, system, user_content, max_tokens, provider: seen.setdefault("model", model) or "ok",
    )
    ai.call_with_fallback("system", "score this job", task_name="triage")
    assert seen["model"] == "gemma4:12b"


# ---------------------------------------------------------------------------
# Settings routes: triage_model and num_ctx are now settable through the web
# UI (previously triage_model existed on the model but had no form field at
# all -- see app/main.py's ai_provider_add/ai_provider_edit).
# ---------------------------------------------------------------------------

def test_add_provider_form_persists_triage_model_and_num_ctx(client, app):
    _login(client)
    resp = client.post("/settings/ai/providers/add", data={
        "provider": "ollama", "label": "Home Mac mini", "base_url": "http://localhost:11434/v1",
        "model": "gemma4:12b", "triage_model": "qwen3:8b", "num_ctx": "16384",
    }, follow_redirects=False)
    assert resp.status_code == 302

    with app.app_context():
        row = AIProviderConfig.query.filter_by(provider="ollama").first()
        assert row is not None
        assert row.triage_model == "qwen3:8b"
        assert row.num_ctx == 16384
        db.session.delete(row)
        db.session.commit()


def test_add_provider_form_blank_num_ctx_stays_none(client, app):
    _login(client)
    resp = client.post("/settings/ai/providers/add", data={
        "provider": "groq", "label": "Groq free tier", "model": "llama-3.1-8b-instant",
    }, follow_redirects=False)
    assert resp.status_code == 302

    with app.app_context():
        row = AIProviderConfig.query.filter_by(provider="groq").first()
        assert row is not None
        assert row.num_ctx is None
        assert row.triage_model == ""
        db.session.delete(row)
        db.session.commit()


def test_edit_provider_form_updates_triage_model_and_num_ctx(client, app):
    with app.app_context():
        row = AIProviderConfig(provider="ollama", model="gemma4:12b", rank=99, enabled=True)
        db.session.add(row)
        db.session.commit()
        pid = row.id

    _login(client)
    resp = client.post(f"/settings/ai/providers/{pid}/edit", data={
        "label": "Home Mac mini", "model": "gemma4:12b", "triage_model": "qwen3:8b", "num_ctx": "16384",
    }, follow_redirects=False)
    assert resp.status_code == 302

    with app.app_context():
        updated = db.session.get(AIProviderConfig, pid)
        assert updated.triage_model == "qwen3:8b"
        assert updated.num_ctx == 16384

    # Clearing the field (blank string, not "0") clears num_ctx back to None.
    resp = client.post(f"/settings/ai/providers/{pid}/edit", data={
        "label": "Home Mac mini", "model": "gemma4:12b", "triage_model": "qwen3:8b", "num_ctx": "",
    }, follow_redirects=False)
    assert resp.status_code == 302
    with app.app_context():
        updated = db.session.get(AIProviderConfig, pid)
        assert updated.num_ctx is None
        db.session.delete(updated)
        db.session.commit()
