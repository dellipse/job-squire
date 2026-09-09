# Copyright (C) 2026 D. Brandmeyer
# Licensed under the GNU Affero General Public License v3 or later.
"""REL-04: ``_call_anthropic_sdk`` must use the short cloud HTTP timeout like
every other provider in the ranked fallback chain (``_http_timeout_for`` /
``call_openai_compat``), so a stalled rank-1 Anthropic provider fails fast
and falls through instead of blocking for a hardcoded 300s -- EXCEPT when
``thinking_mode`` is enabled, where extended thinking can legitimately run
long and the timeout must stay generous.

Mocking follows the house style in ``test_triage_batch_retry.py``: monkeypatch
``ai.requests.post`` with a stub that just records the ``timeout`` kwarg it
was called with.
"""
from app import ai


def _patch_post(monkeypatch):
    """Capture the timeout kwarg passed to requests.post; return an Anthropic-
    shaped 200 stub (a single text content block)."""
    seen = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"content": [{"type": "text", "text": "ok"}]}

    def fake_post(url, headers=None, json=None, timeout=None):  # noqa: A002
        seen["timeout"] = timeout
        seen["body"] = json
        return _Resp()

    monkeypatch.setattr(ai.requests, "post", fake_post)
    return seen


def test_anthropic_call_uses_cloud_timeout_when_not_thinking(monkeypatch):
    seen = _patch_post(monkeypatch)
    text = ai._call_anthropic_sdk("key", "claude-sonnet-4-6", "disabled", "sys", "hi")
    assert text == "ok"
    assert seen["timeout"] == ai._CLOUD_HTTP_TIMEOUT
    assert seen["timeout"] == ai._http_timeout_for("anthropic")


def test_anthropic_call_uses_cloud_timeout_when_thinking_mode_none(monkeypatch):
    """thinking_mode=None (never explicitly set) behaves like 'disabled'."""
    seen = _patch_post(monkeypatch)
    ai._call_anthropic_sdk("key", "claude-sonnet-4-6", None, "sys", "hi")
    assert seen["timeout"] == ai._CLOUD_HTTP_TIMEOUT


def test_anthropic_call_uses_long_timeout_when_thinking_enabled(monkeypatch):
    seen = _patch_post(monkeypatch)
    ai._call_anthropic_sdk("key", "claude-sonnet-4-6", "medium", "sys", "hi")
    assert seen["timeout"] == 300
    # thinking really was applied to the request body, not just the timeout.
    assert seen["body"]["thinking"] == {"type": "enabled", "budget_tokens": 5000}


def test_anthropic_call_uses_long_timeout_for_adaptive_effort_models(monkeypatch):
    """Adaptive-effort models (_ADAPTIVE_MODELS) use 'effort' instead of a
    'thinking' block, but still get the long timeout -- effort-based thinking
    is the same "can legitimately run long" case."""
    seen = _patch_post(monkeypatch)
    adaptive_model = next(iter(ai._ADAPTIVE_MODELS))
    ai._call_anthropic_sdk("key", adaptive_model, "high", "sys", "hi")
    assert seen["timeout"] == 300
    assert seen["body"]["effort"] == "high"


def test_cloud_timeout_is_materially_shorter_than_thinking_timeout():
    """Sanity check the fix is actually a fix: the short path must be short."""
    assert ai._CLOUD_HTTP_TIMEOUT < 300
