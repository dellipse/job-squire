# Copyright (C) 2026 D. Brandmeyer
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""AI analysis: shared payload, prompt, parsing, and the three run modes.

- manual : user copies the prompt + JSON into their own Claude, pastes result back
- api    : the app calls the Anthropic Messages API and applies the result itself
- mcp    : Claude reads/writes Job Squire live via the MCP connector (see mcp_server.py)
"""
import json
import logging
import os
from datetime import datetime, timezone

import requests

from . import privacy
from .db_utils import with_db_retry
from .extensions import db
from .models import AIInsight, Job, User

log = logging.getLogger(__name__)

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

DEFAULT_MODEL = os.environ.get("CLAUDE_DEFAULT_MODEL", "claude-sonnet-4-6")

# Models that use the top-level `effort` param (adaptive thinking) rather than
# the `thinking` block (extended thinking).
_ADAPTIVE_MODELS = set(os.environ.get("CLAUDE_ADAPTIVE_MODELS", "claude-opus-4-8").split(","))

# budget_tokens per thinking level for extended-thinking models.
_THINKING_BUDGETS = {"low": 1024, "medium": 5000, "high": 10000}

# ---------------------------------------------------------------------------
# Multi-provider support: OpenAI-compatible dispatch with ranked fallback
# ---------------------------------------------------------------------------

# Default base URLs per provider. Users can override in Settings.
_PROVIDER_URLS = {
    "openrouter":    "https://openrouter.ai/api/v1",
    "gemini":        "https://generativelanguage.googleapis.com/v1beta/openai",
    "cerebras":      "https://api.cerebras.ai/v1",
    "github_models": "https://models.github.ai/inference",
    "nous_portal":   "https://inference-api.nousresearch.com/v1",
    "ollama":        "http://localhost:11434/v1",
    "litellm":       "http://localhost:4000/v1",   # default LiteLLM port; user may override
    "mistral":       "https://api.mistral.ai/v1",
    "groq":          "https://api.groq.com/openai/v1",
    "openai":        "https://api.openai.com/v1",
}

# Extra headers required or recommended by specific providers.
_PROVIDER_EXTRA_HEADERS = {
    "openrouter": {
        "HTTP-Referer": "https://github.com/dellipse/job-squire",
        "X-Title": "JobSquire",
    },
}

# HTTP status codes that mean "transient capacity problem — try the next provider".
_FALLBACK_STATUS_CODES = {429, 503, 529}

# ---------------------------------------------------------------------------
# Context-window capacity check (docs/PLAN-ollama-assist.md)
#
# Ollama's OpenAI-compatible endpoint has no per-request way to set context
# size — the only supported method is baking it into the model itself via a
# Modelfile (see job_squire_cli/ops/ollama_assist.py's setup flow). When a
# prompt exceeds whatever context size the configured model actually has,
# Ollama does not error: it silently drops the earliest part of the input and
# answers anyway, so a naive caller gets a normal-looking 200 response
# generated from a truncated prompt with no signal anything went wrong. The
# check below estimates whether a prompt will fit before the call is made, so
# an under-provisioned local provider gets skipped in call_with_fallback's
# existing ranked-chain-with-fallback loop — same as an unmet use_for_triage/
# use_for_analysis flag already skips a provider — instead of silently
# returning a bad answer. `AIProviderConfig.num_ctx` (app/models.py) is what a
# provider is known to be configured for; it's left blank for cloud providers
# (already generous, fixed windows) and for any provider not yet run through
# `job-squire ollama setup`, in which case this check is a no-op.
# ---------------------------------------------------------------------------

# ~4 characters per token for English text. A heuristic, not real
# tokenization (deliberately avoiding a tiktoken/tokenizers dependency for
# this one guard) — leaning conservative via _CONTEXT_SAFETY_MARGIN_TOKENS
# below, since undercounting the prompt is exactly what would let a
# truncation slip through undetected.
_CHARS_PER_TOKEN_ESTIMATE = 4

# Reserved off the top of num_ctx for chat-template/role overhead (message
# framing, special tokens) beyond the raw system+user text — on top of the
# response's own max_tokens, which the caller already budgets for separately.
_CONTEXT_SAFETY_MARGIN_TOKENS = 256


def estimate_tokens(text: str) -> int:
    """Rough token count for the capacity pre-check below — not real
    tokenization, just len(text) // 4."""
    return max(1, len(text or "") // _CHARS_PER_TOKEN_ESTIMATE)


def fits_in_context(
    num_ctx: int | None, system: str, user_content: str, max_tokens: int,
) -> tuple[bool, int, int]:
    """Whether (system, user_content) plus a max_tokens response is likely to
    fit inside num_ctx. Returns (fits, estimated_input_tokens, available_budget).

    `fits` is always True when num_ctx is falsy — nothing is configured to
    check against (a cloud provider, or a provider that hasn't been through
    `job-squire ollama setup` yet), so this guard stays out of the way
    entirely rather than guessing.
    """
    if not num_ctx:
        return True, 0, 0
    estimated = estimate_tokens(system) + estimate_tokens(user_content)
    available = max(0, num_ctx - max_tokens - _CONTEXT_SAFETY_MARGIN_TOKENS)
    return estimated <= available, estimated, available


class ContextCapacityError(RuntimeError):
    """Raised by call_with_fallback() when every eligible provider was skipped
    specifically because the prompt doesn't fit any of their configured context
    windows (num_ctx) — as opposed to no providers being configured at all, or
    a genuine API/network failure. Subclasses RuntimeError so existing
    `except RuntimeError` / `except Exception` callers are unaffected.

    Callers that can shrink or split their prompt catch this specifically to
    trigger chunking rather than giving up outright: run_auto_triage() and
    run_followup_drafts() shrink their batch size (_call_batched_with_capacity_shrink);
    run_weekly_review() and run_rejection_analysis() fall back to a map-reduce
    pass (_run_chunked_or_single() + _reduce_partial_analyses()) — full-pipeline
    single-shot analysis is always tried first and is preferred when it fits;
    chunking only kicks in when it doesn't. See docs/PLAN-ollama-assist.md.
    """

# Display names for the AIInsight source field.
_PROVIDER_LABELS = {
    "openrouter":    "OpenRouter",
    "gemini":        "Gemini",
    "cerebras":      "Cerebras",
    "github_models": "GitHub Models",
    "nous_portal":   "Nous Portal",
    "ollama":        "Ollama",
    "litellm":       "LiteLLM",
    "mistral":       "Mistral",
    "groq":          "Groq",
    "openai":        "OpenAI",
    "anthropic":     "Claude",
}

# Sentinel object returned by call_with_fallback when all ranked providers fail
# sentinel retained for internal use only — no longer returned to callers.
_ANTHROPIC_FALLBACK = object()


def _call_anthropic_sdk(api_key: str, model: str, thinking_mode: str | None,
                         system: str, content: str, max_tokens: int = 4096) -> str:
    """Call the Anthropic Messages API directly. Handles thinking mode.

    Used by:
      - _try_one() in call_with_fallback() when provider == "anthropic"
      - Legacy _call_no_thinking() / _call_with_thinking() Anthropic fallback paths
    """
    model_str = (model or DEFAULT_MODEL).strip()
    body: dict = {
        "model": model_str,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": content}],
    }
    if thinking_mode and thinking_mode != "disabled":
        _apply_thinking(body, model_str, thinking_mode)
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    r = requests.post(ANTHROPIC_URL, headers=headers, json=body, timeout=300)
    r.raise_for_status()
    parts = r.json().get("content", [])
    return "".join(p.get("text", "") for p in parts if p.get("type") == "text")


def call_openai_compat(base_url: str, api_key: str, model: str,
                        system: str, user_content: str,
                        max_tokens: int = 4096,
                        provider: str = "") -> str:
    """Call any OpenAI-compatible chat/completions endpoint. Returns reply text."""
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    headers.update(_PROVIDER_EXTRA_HEADERS.get(provider, {}))
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": user_content},
        ],
    }
    r = requests.post(url, headers=headers, json=body, timeout=55)
    if not r.ok:
        # Include the provider's response body in the exception so the UI can show
        # an actionable message (e.g. "model not found" from OpenRouter on a 400).
        try:
            detail = r.json()
        except Exception:  # noqa: BLE001
            detail = r.text[:500]
        hint = ""
        if provider == "openrouter" and r.status_code == 400:
            hint = (
                " — OpenRouter model names must include a provider prefix, "
                "e.g. 'anthropic/claude-sonnet-4-6' or 'openai/gpt-4o-mini'."
            )
        raise requests.HTTPError(
            f"{r.status_code} {r.reason} from {provider or url}: {detail}{hint}",
            response=r,
        )
    data = r.json()
    choices = data.get("choices") or []
    if not choices:
        # Some providers (e.g. OpenRouter free tier) return 200 with choices: null
        # when the model is overloaded.  Surface a clear error instead of a cryptic
        # TypeError so the UI can show an actionable message.
        err = data.get("error") or data
        raise requests.HTTPError(
            f"Provider returned no choices (model may be overloaded or unavailable): {err}",
            response=r,
        )
    return choices[0]["message"]["content"]


def call_with_fallback(system: str, user_content: str,
                        max_tokens: int = 4096,
                        use_triage_model: bool = False,
                        task_name: str | None = None):
    """Route an AI call through the per-task provider chain then the ranked fallback.

    Lookup order:
      1. Per-task primary provider (from AITaskConfig, if task_name given)
      2. Per-task backup provider  (from AITaskConfig, if task_name given)
      3. Ranked chain              (if task has use_ranked_chain_fallback=True, or no task config)
      4. Anthropic                 (if fallback_to_anthropic=True and key is saved)

    Each step skips providers that lack the required capability:
      - Triage tasks (triage, followup): provider must have use_for_triage=True
      - Analysis tasks (weekly_review, rejection_alert): must have use_for_analysis=True

    Returns:
      (text, provider_name)  on success
      (_ANTHROPIC_FALLBACK, "anthropic")  when all providers fail and Anthropic fallback is configured
    Raises:
      The last transient exception if all options are exhausted.
    """
    from flask import current_app
    from . import privacy
    from .crypto import decrypt
    from .models import AIConfig, AIProviderConfig, AITaskConfig, AI_TRIAGE_TASKS

    secret = current_app.config["SECRET_KEY"]
    is_triage = use_triage_model or (task_name in AI_TRIAGE_TASKS if task_name else False)

    # PII/SPI redaction choke point (docs/PLAN-ai-privacy.md). Redacted variants
    # are computed once, lazily — local providers may receive the original text
    # (redact_local off), cloud providers always get the redacted version.
    _redacted: dict = {}

    def _outbound_for(provider_row):
        """Return (system, user_content, was_redacted) for one provider attempt."""
        if not privacy.should_redact_for(provider_row):
            return system, user_content, False
        if not _redacted:
            _redacted["system"] = privacy.redact(system).text
            _redacted["user"] = privacy.redact(user_content).text
        return _redacted["system"], _redacted["user"], True

    # Load per-task config if a task name was given.
    task_cfg = None
    if task_name:
        task_cfg = AITaskConfig.query.filter_by(task_name=task_name).first()

    last_exc = None
    tried_ids: set[int] = set()
    capacity_skips: list[str] = []

    def _try_one(p) -> str | None:
        """Attempt p. Returns text on success, None on skip/transient failure, raises on hard error."""
        nonlocal last_exc

        # Capability check.
        if is_triage and not getattr(p, "use_for_triage", True):
            log.warning("call_with_fallback: provider %s (id=%s) skipped — use_for_triage=%s",
                        getattr(p, "provider", "?"), getattr(p, "id", "?"),
                        getattr(p, "use_for_triage", "MISSING"))
            return None
        if not is_triage and not getattr(p, "use_for_analysis", True):
            log.warning("call_with_fallback: provider %s (id=%s) skipped — use_for_analysis=%s",
                        getattr(p, "provider", "?"), getattr(p, "id", "?"),
                        getattr(p, "use_for_analysis", "MISSING"))
            return None

        # Triage gets the row's triage_model when one is set (fast/cheap model for the
        # high-frequency triage/follow-up tasks); every other task, and any row that
        # hasn't set triage_model, uses the row's model (the analysis model).
        model = ((p.triage_model or "").strip() if is_triage else "") or (p.model or "").strip()
        api_key = decrypt(secret, p.api_key_enc).strip() if p.api_key_enc else ""

        if not model:
            log.warning("provider %s (rank %s) skipped — no model set",
                        p.provider, getattr(p, "rank", "?"))
            return None

        out_system, out_user, was_redacted = _outbound_for(p)

        # Context-capacity check — see this module's "Context-window capacity check"
        # section above for why this exists. A skip here is deliberately silent-to-the-
        # user in the same way a use_for_triage/use_for_analysis mismatch is: it just
        # moves to the next provider in the chain. If every provider gets skipped for
        # this reason, the final RuntimeError below says so explicitly.
        fits, estimated, available = fits_in_context(getattr(p, "num_ctx", None), out_system, out_user, max_tokens)
        if not fits:
            log.warning(
                "provider %s (rank %s) skipped — estimated prompt ~%d tokens exceeds its "
                "configured context budget of %d (num_ctx=%s, %d reserved for response/overhead)",
                p.provider, getattr(p, "rank", "?"), estimated, available, p.num_ctx,
                max_tokens + _CONTEXT_SAFETY_MARGIN_TOKENS,
            )
            capacity_skips.append(f"{p.display_name} (~{estimated} tokens needed, {available} available)")
            return None

        tried_ids.add(p.id)
        try:
            # Anthropic uses its own SDK path; all others use OpenAI-compat HTTP.
            if p.provider == "anthropic":
                thinking = getattr(p, "thinking_mode", None) or None
                result = _call_anthropic_sdk(api_key, model, thinking,
                                              out_system, out_user, max_tokens)
            else:
                base_url = (p.base_url or _PROVIDER_URLS.get(p.provider, "")).strip()
                if not base_url:
                    log.warning("provider %s (rank %s) skipped — no base URL",
                                p.provider, getattr(p, "rank", "?"))
                    tried_ids.discard(p.id)
                    return None
                result = call_openai_compat(base_url, api_key, model,
                                             out_system, out_user, max_tokens, p.provider)
            if was_redacted:
                result, _unresolved = privacy.rehydrate(result)
            if last_exc is not None:
                log.info("task %s: succeeded with %s after earlier failure(s)",
                         task_name or "?", p.provider)
            return result
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code in _FALLBACK_STATUS_CODES:
                log.warning("provider %s HTTP %d — trying next",
                            p.provider, exc.response.status_code)
                last_exc = exc
                return None
            raise
        except requests.Timeout as exc:
            log.warning("provider %s timed out — trying next", p.provider)
            last_exc = exc
            return None

    # Step 1 — per-task primary.
    if task_cfg and task_cfg.provider_id and task_cfg.provider and task_cfg.provider.enabled:
        text = _try_one(task_cfg.provider)
        if text is not None:
            return text, task_cfg.provider.provider

    # Step 2 — per-task backup.
    if task_cfg and task_cfg.backup_provider_id and task_cfg.backup_provider and task_cfg.backup_provider.enabled:
        text = _try_one(task_cfg.backup_provider)
        if text is not None:
            if tried_ids:
                log.info("task %s: fell back to backup provider %s",
                         task_name or "?", task_cfg.backup_provider.provider)
            return text, task_cfg.backup_provider.provider

    # Step 3 — ranked chain (skipped if task explicitly disables it).
    # Treat NULL as True (the intended default) so stale rows without this column
    # don't silently block all providers.
    use_chain = task_cfg is None or (task_cfg.use_ranked_chain_fallback is not False)
    if use_chain:
        # Discard the scoped session entirely before querying — background threads may
        # inherit a session with a stale or dropped connection from a prior request.
        # remove() is stronger than expire_all(): it closes the connection and opens a
        # fresh one, guaranteeing we see the latest committed data.
        db.session.remove()
        providers = (
            AIProviderConfig.query
            .filter_by(enabled=True)
            .order_by(AIProviderConfig.rank)
            .all()
        )
        log.warning("call_with_fallback: step3 task=%s found %d provider(s)", task_name, len(providers))
        for p in providers:
            if p.id in tried_ids:
                continue
            text = _try_one(p)
            if text is not None:
                if tried_ids:
                    log.info("task %s: fell back to ranked chain provider %s",
                             task_name or "?", p.provider)
                return text, p.provider

    # Step 4 — Anthropic legacy fallback (api_key stored in AIConfig, not as a provider row).
    # Uses cfg.model (user-configured analysis model) — no hardcoded strings.
    ai_cfg = db.session.get(AIConfig, 1)
    if ai_cfg and getattr(ai_cfg, "fallback_to_anthropic", True) and ai_cfg.api_key_enc:
        fallback_key = decrypt(secret, ai_cfg.api_key_enc).strip()
        fallback_model = (ai_cfg.model or DEFAULT_MODEL).strip()
        if fallback_key and fallback_model:
            tried_names = [db.session.get(AIProviderConfig, i).provider for i in tried_ids
                           if db.session.get(AIProviderConfig, i)]
            log.warning("task %s: all providers failed (%s) — falling back to legacy Anthropic key (model: %s)",
                        task_name or "?", tried_names, fallback_model)
            try:
                # Legacy Anthropic is always a cloud call — redact whenever enabled.
                if privacy.redaction_enabled():
                    if not _redacted:
                        _redacted["system"] = privacy.redact(system).text
                        _redacted["user"] = privacy.redact(user_content).text
                    result = _call_anthropic_sdk(fallback_key, fallback_model, None,
                                                  _redacted["system"], _redacted["user"],
                                                  max_tokens)
                    result, _unresolved = privacy.rehydrate(result)
                else:
                    result = _call_anthropic_sdk(fallback_key, fallback_model, None,
                                                  system, user_content, max_tokens)
                return result, "anthropic-legacy"
            except Exception as exc:  # noqa: BLE001
                log.warning("legacy Anthropic fallback failed: %s", exc)
                last_exc = exc

    if last_exc:
        raise last_exc
    log.warning("call_with_fallback: exhausted all options — task=%s use_chain=%s tried=%s capacity_skips=%s",
                task_name, use_chain, tried_ids, capacity_skips)
    if capacity_skips:
        # Every candidate that would otherwise have run this got skipped for context-
        # capacity reasons specifically (fits_in_context() above) — distinct from "no
        # providers configured at all" so whoever reads this (a log line during an
        # unattended worker run, or a flashed error from a manual Analyze click) knows
        # immediately this is a context-window sizing problem, not a missing API key.
        raise ContextCapacityError(
            f"No AI providers available{' for task ' + task_name if task_name else ''} — every "
            f"eligible provider was skipped because this prompt is too large for its configured "
            f"context window: {'; '.join(capacity_skips)}. Raise num_ctx (docs/PLAN-ollama-assist.md) "
            f"or add/enable a provider with a larger context window as a fallback."
        )
    raise RuntimeError(
        f"No AI providers available{' for task ' + task_name if task_name else ''}. "
        "Add a provider in Settings → AI → AI Providers."
    )


def _has_ranked_providers() -> bool:
    """Return True if at least one AIProviderConfig row is enabled."""
    from .models import AIProviderConfig
    return AIProviderConfig.query.filter_by(enabled=True).count() > 0


def _has_any_provider(task_name: str | None = None) -> bool:
    """Return True if there is anything to try (task-specific or ranked)."""
    from .models import AIProviderConfig, AITaskConfig
    if task_name:
        tc = AITaskConfig.query.filter_by(task_name=task_name).first()
        if tc and (tc.provider_id or tc.backup_provider_id):
            return True
    return AIProviderConfig.query.filter_by(enabled=True).count() > 0


def _call_no_thinking(system: str, content: str, max_tokens: int,
                       api_key: str = "",
                       model: str = "",
                       use_triage_model: bool = False,
                       task_name: str | None = None) -> str:
    """Route through the provider chain. No thinking-mode support.

    Used by batch features (triage, follow-up drafts, ATS analysis) where
    thinking mode adds cost without meaningful benefit.

    Model selection comes entirely from provider configuration — there are no
    hardcoded model strings. The legacy api_key/model params are kept for
    backward compatibility but are no longer used; all fallback logic (including
    the Anthropic legacy key in AIConfig) is handled inside call_with_fallback().
    """
    raw, _ = call_with_fallback(system, content, max_tokens,
                                 use_triage_model=use_triage_model,
                                 task_name=task_name)
    return raw


def _call_with_thinking(system: str, content: str, max_tokens: int,
                         api_key: str = "",
                         model: str = "",
                         thinking_mode: str = "disabled",
                         task_name: str | None = None) -> str:
    """Route through the provider chain with optional thinking mode.

    Used by weekly review and rejection analysis where deeper reasoning helps.
    When Anthropic is in the ranked provider chain (provider=="anthropic"), thinking_mode
    is read from that provider's row. Non-Anthropic providers receive a standard chat
    request (thinking params are ignored).

    Model selection comes from provider configuration. The legacy api_key/model params
    are kept for backward compatibility but are no longer used; all fallback logic
    is handled inside call_with_fallback().
    """
    raw, _ = call_with_fallback(system, content, max_tokens, task_name=task_name)
    return raw


# ---------------------------------------------------------------------------
# The instruction block shared by every mode. Kept here so the manual prompt,
# the API call, and the MCP tool all describe the task identically.
ANALYSIS_INSTRUCTIONS = (
    "You are an expert job-search coach reviewing the candidate's application pipeline "
    "and interview debriefs. Analyze the data and identify patterns in what is and is "
    "not working: where applications stall in the funnel, which roles or sources convert, "
    "and concrete, specific improvements. Return ONLY valid JSON matching this schema:\n"
    '{"overall_summary": string, "recommendations": [string, ...], '
    '"jobs": [{"id": number, "analysis": string}, ...]}\n'
    "Each jobs[].id MUST match the job id given in the data so the results import cleanly. "
    "Keep recommendations concrete and actionable. Do not invent facts not present in the data."
)


def build_export_dict():
    """The full pipeline as a JSON-able dict, including the analysis instructions."""
    jobs = Job.query.order_by(Job.date_applied.desc()).all()
    candidate_user = User.query.filter_by(role="user").first()
    if candidate_user:
        candidate_name = candidate_user.display_name or candidate_user.username or "Candidate"
    else:
        candidate_name = "Candidate"
    return {
        "exported_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "candidate": candidate_name,
        "instructions_for_ai": ANALYSIS_INSTRUCTIONS,
        "jobs": [
            {
                "id": j.id,
                "company": j.company,
                "title": j.title,
                "location": j.location,
                "work_mode": j.work_mode,
                "status": j.status,
                "source": j.source,
                "salary": j.salary,
                "date_applied": str(j.date_applied) if j.date_applied else None,
                "notes": j.notes or "",
                "interviews": [
                    {
                        "date": str(iv.interview_date) if iv.interview_date else None,
                        "round": iv.round_type,
                        "format": iv.interview_format,
                        "self_rating": iv.self_rating,
                        "questions_asked": iv.questions_asked,
                        "went_well": iv.went_well,
                        "to_improve": iv.to_improve,
                        "notes": iv.notes,
                    }
                    for iv in j.interviews
                ],
            }
            for j in jobs
        ],
    }


def manual_prompt():
    """A human-readable prompt the user pastes into any AI assistant, above the JSON file."""
    return (
        "Attached (or pasted below) is the JSON export of my Job Squire pipeline, "
        "including every application and interview debrief.\n\n"
        + ANALYSIS_INSTRUCTIONS
        + "\n\nReturn only the JSON object so I can paste it straight back into my Job Squire instance."
    )


def extract_json(raw):
    """Parse JSON, tolerating ```json fences or surrounding prose.

    Uses balanced-brace scanning so nested objects followed by trailing prose
    don't produce a truncated (and invalid) JSON string.
    """
    raw = (raw or "").strip()
    # Strip markdown code fences.
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # Walk forward from the first '{', tracking brace depth, to find the
    # matching closing '}'. This is correct for nested objects and avoids
    # rfind() grabbing a closing brace inside trailing prose.
    start = raw.find("{")
    if start == -1:
        raise ValueError("no JSON object found")
    depth = 0
    in_str = False
    escape = False
    for i, ch in enumerate(raw[start:], start):
        if escape:
            escape = False
            continue
        if ch == "\\" and in_str:
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(raw[start:i + 1])
                except json.JSONDecodeError:
                    break
    raise ValueError("no JSON object found")


def apply_analysis(parsed, created_by="ai", provider="anthropic"):
    """Apply a parsed analysis dict: global insight + per-job analysis. Returns (updated, missing)."""
    updated = missing = 0
    summary = (parsed.get("overall_summary") or "").strip()
    recs = parsed.get("recommendations") or []
    if isinstance(recs, str):
        recs = [recs]
    source_label = _PROVIDER_LABELS.get(provider, (provider or "AI").title())
    if summary or recs:
        db.session.add(AIInsight(
            summary=summary,
            recommendations="\n".join(str(r) for r in recs),
            source=source_label,
            created_by=created_by,
        ))
    for item in parsed.get("jobs", []) or []:
        try:
            jid = int(item.get("id"))
        except (TypeError, ValueError):
            missing += 1
            continue
        job = db.session.get(Job, jid)
        if not job:
            missing += 1
            continue
        analysis = (item.get("analysis") or "").strip()
        if analysis:
            job.ai_analysis = analysis
            job.ai_analysis_at = datetime.now(timezone.utc)
            updated += 1
    db.session.commit()
    return updated, missing


def run_api_analysis(api_key="", model="", thinking_mode="disabled"):
    """Call configured AI providers and return (parsed_dict, provider_name).

    Routes through the provider chain via call_with_fallback(), which includes
    the legacy Anthropic fallback if configured. The api_key, model, and
    thinking_mode params are kept for backward compatibility but are only used
    if the legacy Anthropic fallback path is triggered inside call_with_fallback().

    Raises requests.HTTPError on API errors, ValueError if the reply is not JSON.
    """
    system = "You are an expert job-search coach. Respond with ONLY a valid JSON object."
    data = build_export_dict()
    content = ANALYSIS_INSTRUCTIONS + "\n\nHere is the pipeline data:\n" + json.dumps(
        {"candidate": data["candidate"], "jobs": data["jobs"]}
    )
    raw, provider = call_with_fallback(system, content, max_tokens=4096)
    return extract_json(raw), provider


# ---------------------------------------------------------------------------
# Feature 1: Scheduled Triage
# ---------------------------------------------------------------------------

_TRIAGE_BATCH_SIZE = 10

_TRIAGE_SYSTEM = (
    "You are a job-search assistant scoring job postings for candidate fit. "
    "Respond with ONLY a valid JSON array — no prose, no markdown fences."
)

_TRIAGE_INSTRUCTIONS = """\
Score each job for fit against the candidate profile below on a scale of 1-10.

Scoring guide:
- 8-10: Strong match — title, experience level, and work mode all align; salary (if listed) meets or exceeds the candidate's target.
- 5-7:  Partial match — worth considering but has notable gaps (wrong work mode, stretch title, low salary, or vague posting).
- 3-4:  Weak match — significant mismatch in title, experience level, or requirements.
- 1-2:  Poor fit or low-quality posting — commission-only, MLM language, \"unlimited earning potential\", no real company name, or completely outside the candidate's field.

Return a JSON array with one object per job, in this exact schema:
[{"id": <job_id>, "score": <1-10>, "reason": "<1-2 sentence explanation>"}]

Every job in the input must appear in the output. Do not add prose before or after the array.
"""


def _call_batched_with_capacity_shrink(
    items: list,
    build_content,
    call,
    parse,
    min_chunk: int = 1,
) -> tuple[list, list]:
    """Call `call(build_content(items))` as one chunk; on ContextCapacityError,
    split `items` in half and retry each half recursively, down to `min_chunk`.

    Returns (parsed_results_concatenated, items_that_still_didn't_fit_at_min_chunk).
    Any exception other than ContextCapacityError (parse error, network failure,
    a genuine provider error, ...) propagates to the caller unchanged — only a
    capacity failure triggers a shrink, so this never masks a real error as a
    silent partial-failure.

    Used by run_auto_triage()/run_followup_drafts(): these batch several
    independent jobs into one prompt, so shrinking the batch size is free and
    lossless — every job still gets scored/drafted individually once the batch
    is small enough, no reassembly beyond what the caller already does.
    """
    try:
        raw = call(build_content(items))
        return parse(raw), []
    except ContextCapacityError as exc:
        if len(items) <= min_chunk:
            log.warning(
                "capacity shrink: %d item(s) still exceed the configured provider's context "
                "window even at the minimum chunk size — giving up on these: %s",
                len(items), exc,
            )
            return [], list(items)
        mid = len(items) // 2
        log.warning(
            "capacity shrink: a batch of %d item(s) exceeded the configured provider's "
            "context window — splitting into %d + %d and retrying (expected on small local "
            "models; raise num_ctx or use a larger model to avoid the extra calls — see "
            "docs/PLAN-ollama-assist.md)",
            len(items), mid, len(items) - mid,
        )
        left_results, left_failed = _call_batched_with_capacity_shrink(
            items[:mid], build_content, call, parse, min_chunk)
        right_results, right_failed = _call_batched_with_capacity_shrink(
            items[mid:], build_content, call, parse, min_chunk)
        return left_results + right_results, left_failed + right_failed


def run_auto_triage(api_key: str = "", model: str = "") -> dict:
    """Score all unanalyzed 'Saved' jobs via the configured provider(s).

    Model selection comes from provider configuration via call_with_fallback().
    The api_key and model params are kept for backward compatibility but ignored.

    Returns {"scored": int, "failed": int}.
    """

    # Load candidate profile once.
    profile_text = _load_candidate_profile()

    # Fetch all unanalyzed Saved jobs (limit 50 to cap cost per run).
    jobs = (
        Job.query
        .filter(Job.status == "Saved")
        .filter((Job.ai_fit_score == None) | (Job.ai_fit_score == 0))  # noqa: E711
        .order_by(Job.created_at.desc())
        .limit(50)
        .all()
    )

    if not jobs:
        log.info("auto-triage: no unanalyzed jobs found")
        return {"scored": 0, "failed": 0}

    scored = 0
    failed = 0
    total_batches = (len(jobs) + _TRIAGE_BATCH_SIZE - 1) // _TRIAGE_BATCH_SIZE

    # Process in batches.
    for i in range(0, len(jobs), _TRIAGE_BATCH_SIZE):
        batch = jobs[i: i + _TRIAGE_BATCH_SIZE]
        batch_num = i // _TRIAGE_BATCH_SIZE + 1
        log.info("auto-triage: batch %d/%d — scoring %d job(s), calling AI provider...",
                  batch_num, total_batches, len(batch))

        def _build(batch_jobs):
            job_list = [
                {
                    "id": j.id,
                    "title": j.title,
                    "company": j.company,
                    "location": j.location or "",
                    "work_mode": j.work_mode or "",
                    "salary": j.salary or "",
                    "source": j.source or "",
                    "description": (j.notes or "")[:800],
                }
                for j in batch_jobs
            ]
            return (
                _TRIAGE_INSTRUCTIONS
                + "\n\nCANDIDATE PROFILE:\n"
                + profile_text
                + "\n\nJOBS TO SCORE:\n"
                + json.dumps(job_list)
            )

        def _invoke(call_content):
            return _call_no_thinking(_TRIAGE_SYSTEM, call_content, 1024,
                                      use_triage_model=True, task_name="triage")

        try:
            results, unresolved = _call_batched_with_capacity_shrink(
                batch, _build, _invoke, _parse_triage_response)
        except Exception as exc:  # noqa: BLE001
            log.warning("auto-triage batch %d/%d failed: %s", batch_num, total_batches, exc)
            failed += len(batch)
            continue
        failed += len(unresolved)
        log.info("auto-triage: batch %d/%d — response received, applying scores...",
                  batch_num, total_batches)

        # Apply scores.
        for item in results:
            try:
                jid = int(item.get("id"))
                score = max(1, min(10, int(item.get("score", 5))))
                reason = (item.get("reason") or "").strip()
            except (TypeError, ValueError):
                failed += 1
                continue
            job = db.session.get(Job, jid)
            if not job:
                failed += 1
                continue
            job.ai_fit_score = score
            job.ai_fit_reason = reason
            scored += 1

        # Commit after each batch so partial progress is preserved.
        try:
            db.session.commit()
        except Exception as exc:  # noqa: BLE001
            db.session.rollback()
            log.warning("auto-triage commit failed: %s", exc)

    log.info("auto-triage complete: %d scored, %d failed", scored, failed)
    return {"scored": scored, "failed": failed}


def run_triage_batch(offset: int, limit: int = 20,
                     provider_id: int | None = None) -> dict:
    """Run triage on a specific page of unscored Saved jobs.

    Used by the manual backlog tool at /tools/triage-batch.

    Args:
        offset:      Number of jobs to skip (pagination).
        limit:       Batch size (default 20).
        provider_id: If set, bypass the chain and use this AIProviderConfig directly.

    Returns:
        {
            "results": list of {id, title, company, score, reason, ok},
            "scored": int,
            "failed": int,
            "total_remaining": int,
            "next_offset": int,
        }
    """
    from flask import current_app
    from .crypto import decrypt
    from .models import AIProviderConfig

    profile_text = _load_candidate_profile()

    unscored_q = (
        Job.query
        .filter(Job.status == "Saved")
        .filter((Job.ai_fit_score == None) | (Job.ai_fit_score == 0))  # noqa: E711
    )

    jobs = with_db_retry(
        lambda: (
            unscored_q
            .order_by(Job.created_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )
    )

    if not jobs:
        return {
            "results": [],
            "scored": 0,
            "failed": 0,
            "total_remaining": with_db_retry(unscored_q.count),
            "next_offset": offset,
        }

    job_list = [
        {
            "id": j.id,
            "title": j.title,
            "company": j.company,
            "location": j.location or "",
            "work_mode": j.work_mode or "",
            "salary": j.salary or "",
            "source": j.source or "",
            "description": (j.notes or "")[:800],
        }
        for j in jobs
    ]

    content = (
        _TRIAGE_INSTRUCTIONS
        + "\n\nCANDIDATE PROFILE:\n"
        + profile_text
        + "\n\nJOBS TO SCORE:\n"
        + json.dumps(job_list)
    )

    results = []
    scored = 0
    failed = 0

    # Resolve direct-provider details up front so retry logic can reuse them.
    secret = current_app.config["SECRET_KEY"]
    _p = _api_key = _model = _base_url = None
    if provider_id is not None:
        _p = db.session.get(AIProviderConfig, provider_id)
        if _p is None:
            for j in jobs:
                results.append({"id": j.id, "title": j.title, "company": j.company,
                                "score": None, "reason": f"Provider ID {provider_id} not found.", "ok": False})
                failed += 1
            return {"results": results, "scored": 0, "failed": failed,
                    "total_remaining": with_db_retry(unscored_q.count), "next_offset": offset + len(jobs)}
        _model = (_p.model or "").strip()
        if not _model:
            for j in jobs:
                results.append({"id": j.id, "title": j.title, "company": j.company,
                                "score": None, "reason": f"Provider '{_p.display_name}' has no model configured.", "ok": False})
                failed += 1
            return {"results": results, "scored": 0, "failed": failed,
                    "total_remaining": with_db_retry(unscored_q.count), "next_offset": offset + len(jobs)}
        _api_key = decrypt(secret, _p.api_key_enc).strip() if _p.api_key_enc else ""
        if _p.provider != "anthropic":
            _base_url = (_p.base_url or _PROVIDER_URLS.get(_p.provider, "")).strip()
            if not _base_url:
                for j in jobs:
                    results.append({"id": j.id, "title": j.title, "company": j.company,
                                    "score": None, "reason": f"Provider '{_p.display_name}' has no base URL.", "ok": False})
                    failed += 1
                return {"results": results, "scored": 0, "failed": failed,
                        "total_remaining": with_db_retry(unscored_q.count), "next_offset": offset + len(jobs)}

    def _invoke(call_content: str) -> str:
        """Make a single triage AI call using the configured provider or chain."""
        if _p is not None:
            # Direct-provider path bypasses call_with_fallback, so it applies
            # the PII redaction choke point itself (docs/PLAN-ai-privacy.md).
            out_system, out_content = _TRIAGE_SYSTEM, call_content
            was_redacted = privacy.should_redact_for(_p)
            if was_redacted:
                out_system = privacy.redact(out_system).text
                out_content = privacy.redact(out_content).text
            if _p.provider == "anthropic":
                raw = _call_anthropic_sdk(_api_key, _model, None,
                                          out_system, out_content, 1024)
            else:
                raw = call_openai_compat(_base_url, _api_key, _model,
                                         out_system, out_content, 1024, _p.provider)
            if was_redacted:
                raw, _ = privacy.rehydrate(raw)
            return raw
        return _call_no_thinking(_TRIAGE_SYSTEM, call_content, 1024,
                                  use_triage_model=True, task_name="triage")

    def _build_content(job_dicts: list) -> str:
        return (
            _TRIAGE_INSTRUCTIONS
            + "\n\nCANDIDATE PROFILE:\n"
            + profile_text
            + "\n\nJOBS TO SCORE:\n"
            + json.dumps(job_dicts)
        )

    try:
        parsed = _parse_triage_response(_invoke(content))
    except Exception as exc:  # noqa: BLE001
        log.warning("triage-batch batch failed: %s", exc)
        for j in jobs:
            results.append({"id": j.id, "title": j.title, "company": j.company,
                            "score": None, "reason": str(exc), "ok": False})
            failed += 1
        return {
            "results": results,
            "scored": 0,
            "failed": failed,
            "total_remaining": with_db_retry(unscored_q.count),
            "next_offset": offset + len(jobs),
        }

    job_map = {j.id: j for j in jobs}
    applied_ids: set[int] = set()

    def _apply(item: dict) -> bool:
        """Apply one parsed result. Returns True if successfully applied."""
        try:
            jid = int(item.get("id"))
            score = max(1, min(10, int(item.get("score", 5))))
            reason = (item.get("reason") or "").strip()
        except (TypeError, ValueError):
            return False
        if jid in applied_ids:
            return False
        job = job_map.get(jid) or db.session.get(Job, jid)
        if not job:
            return False
        job.ai_fit_score = score
        job.ai_fit_reason = reason
        results.append({"id": jid, "title": job.title, "company": job.company,
                        "score": score, "reason": reason, "ok": True})
        applied_ids.add(jid)
        return True

    for item in parsed:
        if _apply(item):
            scored += 1
        # Don't count unparseable items from the main pass as failures yet —
        # they'll be retried below.

    # Retry any jobs missing from the initial response in sub-batches of 5 with
    # shorter descriptions (400 chars). Free-tier models often have small context
    # windows and silently drop entries when the prompt is too long.
    missing = [j for j in jobs if j.id not in applied_ids]
    if missing:
        log.info("triage-batch: %d job(s) not in initial response; retrying in sub-batches of 5",
                 len(missing))
        _RETRY_CHUNK = 5
        for chunk_start in range(0, len(missing), _RETRY_CHUNK):
            chunk = missing[chunk_start:chunk_start + _RETRY_CHUNK]
            chunk_dicts = [
                {
                    "id": j.id,
                    "title": j.title,
                    "company": j.company,
                    "location": j.location or "",
                    "work_mode": j.work_mode or "",
                    "salary": j.salary or "",
                    "source": j.source or "",
                    "description": (j.notes or "")[:400],
                }
                for j in chunk
            ]
            try:
                retry_parsed = _parse_triage_response(_invoke(_build_content(chunk_dicts)))
            except Exception as exc:  # noqa: BLE001
                log.warning("triage-batch retry chunk failed: %s", exc)
                continue
            for item in retry_parsed:
                if _apply(item):
                    scored += 1

    # Second retry: send each still-missing job individually (one per call).
    # Free-tier models may drop entries even from a 5-job sub-batch when
    # descriptions push the prompt over their context limit.
    still_missing = [j for j in jobs if j.id not in applied_ids]
    if still_missing:
        log.info("triage-batch: %d job(s) still missing; retrying one-by-one", len(still_missing))
        for j in still_missing:
            solo_dict = [{
                "id": j.id,
                "title": j.title,
                "company": j.company,
                "location": j.location or "",
                "work_mode": j.work_mode or "",
                "salary": j.salary or "",
                "source": j.source or "",
                "description": (j.notes or "")[:400],
            }]
            try:
                solo_parsed = _parse_triage_response(_invoke(_build_content(solo_dict)))
            except Exception as exc:  # noqa: BLE001
                log.warning("triage-batch solo retry failed for job %d: %s", j.id, exc)
                continue
            for item in solo_parsed:
                if _apply(item):
                    scored += 1

    # Flag any jobs that still didn't come back after all retry passes.
    for j in jobs:
        if j.id not in applied_ids:
            results.append({"id": j.id, "title": j.title, "company": j.company,
                            "score": None, "reason": "not returned by AI", "ok": False})
            failed += 1

    try:
        with_db_retry(db.session.commit)
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        log.warning("triage-batch commit failed: %s", exc)

    total_remaining = with_db_retry(
        lambda: (
            Job.query
            .filter(Job.status == "Saved")
            .filter((Job.ai_fit_score == None) | (Job.ai_fit_score == 0))  # noqa: E711
            .count()
        )
    )

    log.info("triage-batch batch: scored=%d failed=%d remaining=%d",
             scored, failed, total_remaining)
    return {
        "results": results,
        "scored": scored,
        "failed": failed,
        "total_remaining": total_remaining,
        "next_offset": offset + len(jobs),
    }


def _load_candidate_profile() -> str:
    """Read candidate_profile.md from DATA_DIR. Returns a placeholder if missing."""
    try:
        from flask import current_app
        path = os.path.join(current_app.config["DATA_DIR"], "candidate_profile.md")
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except Exception:  # noqa: BLE001
        return "(candidate profile not available — upload one in Settings > Candidate Profile)"


# ---------------------------------------------------------------------------
# Feature 2: Automatic Follow-Up Draft Generation
# ---------------------------------------------------------------------------

_FOLLOWUP_SYSTEM = (
    "You are a job-search assistant drafting professional follow-up emails. "
    "Respond with ONLY a valid JSON array — no prose, no markdown fences."
)

_FOLLOWUP_INSTRUCTIONS = """\
Draft a follow-up email for each job listed below. Use the candidate profile to personalize each email.

Rules:
- Subject line required, included in the email_text field.
- 3-4 sentences maximum per email.
- Professional and warm. No cliches. No em-dashes. Do not open with "I hope this email finds you well."
- Reference one specific detail about the role or company.
- Close with a low-pressure ask for a status update.
- Use the candidate's real name from the profile.

Tone guide by status:
- Applied (7+ days, no response): polite check-in, express continued interest.
- Phone Screen: reference the conversation, ask about next steps.
- Interview: reference the specific round, note strong continued interest.

Return a JSON array with one object per job:
[{"job_id": <id>, "email_text": "<subject line>\\n\\n<email body>"}]

Every job in the input must appear in the output. Do not add prose before or after the array.
"""


def run_followup_drafts(api_key: str = "", model: str = "") -> dict:
    """Draft follow-up emails for all overdue jobs with no existing draft.

    Queries jobs where follow_up_date <= today, status is active, and
    followup_draft is empty. Model selection comes from provider configuration.
    The api_key and model params are kept for backward compatibility but ignored.
    Returns {"drafted": int, "failed": int, "jobs": [{"id", "title", "company"}]}.
    """
    from datetime import date
    from .models import ACTIVE_STATUSES
    profile_text = _load_candidate_profile()
    today = date.today()

    overdue = (
        Job.query
        .filter(Job.status.in_(list(ACTIVE_STATUSES)))
        .filter(Job.follow_up_date != None)       # noqa: E711
        .filter(Job.follow_up_date <= today)
        .filter((Job.followup_draft == None) | (Job.followup_draft == ""))  # noqa: E711
        .order_by(Job.follow_up_date.asc())
        .all()
    )

    if not overdue:
        log.info("auto-followup: no overdue jobs without drafts")
        return {"drafted": 0, "failed": 0, "jobs": []}

    drafted = 0
    failed = 0
    drafted_jobs = []
    total_batches = (len(overdue) + _TRIAGE_BATCH_SIZE - 1) // _TRIAGE_BATCH_SIZE

    for i in range(0, len(overdue), _TRIAGE_BATCH_SIZE):
        batch = overdue[i: i + _TRIAGE_BATCH_SIZE]
        batch_num = i // _TRIAGE_BATCH_SIZE + 1
        log.info("auto-followup: batch %d/%d — drafting %d email(s), calling AI provider...",
                  batch_num, total_batches, len(batch))

        def _build(batch_jobs):
            job_list = [
                {
                    "id": j.id,
                    "title": j.title,
                    "company": j.company,
                    "status": j.status,
                    "follow_up_date": str(j.follow_up_date),
                    "contact_name": j.contact_name or "",
                    "contact_email": j.contact_email or "",
                    "date_applied": str(j.date_applied) if j.date_applied else None,
                    "notes": (j.notes or "")[:400],
                }
                for j in batch_jobs
            ]
            return (
                _FOLLOWUP_INSTRUCTIONS
                + "\n\nCANDIDATE PROFILE:\n"
                + profile_text
                + "\n\nJOBS NEEDING FOLLOW-UP:\n"
                + json.dumps(job_list)
            )

        def _invoke(call_content):
            return _call_no_thinking(_FOLLOWUP_SYSTEM, call_content, 2048,
                                      use_triage_model=True, task_name="followup")

        try:
            results, unresolved = _call_batched_with_capacity_shrink(
                batch, _build, _invoke, _parse_triage_response)  # reuse array parser
        except Exception as exc:  # noqa: BLE001
            log.warning("auto-followup batch %d/%d failed: %s", batch_num, total_batches, exc)
            failed += len(batch)
            continue
        failed += len(unresolved)
        log.info("auto-followup: batch %d/%d — response received, saving drafts...",
                  batch_num, total_batches)

        for item in results:
            try:
                jid = int(item.get("job_id") or item.get("id"))
                email_text = (item.get("email_text") or "").strip()
            except (TypeError, ValueError):
                failed += 1
                continue
            if not email_text:
                failed += 1
                continue
            job = db.session.get(Job, jid)
            if not job:
                failed += 1
                continue
            job.followup_draft = email_text
            drafted_jobs.append({"id": job.id, "title": job.title, "company": job.company})
            drafted += 1

        try:
            db.session.commit()
        except Exception as exc:  # noqa: BLE001
            db.session.rollback()
            log.warning("auto-followup commit failed: %s", exc)

    log.info("auto-followup complete: %d drafted, %d failed", drafted, failed)
    return {"drafted": drafted, "failed": failed, "jobs": drafted_jobs}


# ---------------------------------------------------------------------------
# Feature 3: Automatic Weekly Strategy Review
# ---------------------------------------------------------------------------

_WEEKLY_REVIEW_SYSTEM = (
    "You are an expert job-search coach writing a weekly strategy review. "
    "Respond with ONLY a valid JSON object — no prose, no markdown fences."
)

_WEEKLY_REVIEW_INSTRUCTIONS = """\
Write a detailed weekly strategy review based on the job-search pipeline data below.

Structure your analysis to cover:
1. WHAT WORKED THIS WEEK — applications that progressed, interviews completed, new connections
2. WHAT STALLED — rejections, ghosting, applications dormant for 14+ days
3. FUNNEL ANALYSIS — conversion rates, biggest drop-off stage, which sources produce activity
4. STRATEGY FOR NEXT WEEK — one concrete change (not generic), three applications deserving most attention

Be honest. If the week was weak, say so and explain what to do differently.
Write like a coach who knows this candidate well. Use specific job titles and companies from the data.

Return ONLY a JSON object matching this schema:
{"overall_summary": "<full review in 3-5 paragraphs>", "recommendations": ["<specific action>", ...]}

No jobs array is needed. Do not add prose before or after the object.
"""


def _run_chunked_or_single(items: list, build_content, call, min_chunk: int = 1) -> list:
    """Try `call(build_content(items))` as one shot; on ContextCapacityError, split
    `items` in half and recurse, returning the list of parsed partial results.

    `build_content` must return prompt text asking for the SAME
    {"overall_summary","recommendations"} schema regardless of chunk size — the
    only thing that changes per call is which subset of items is included (see
    run_weekly_review()/run_rejection_analysis()'s closures, which also add a
    "this is a partial chunk" note once `items` is smaller than the full set).

    Returns a list of length 1 (the normal, unchunked case) when the full set
    fits; a list of length >1 when it had to split. Re-raises ContextCapacityError
    if even `min_chunk` items don't fit — that's a genuinely too-small provider,
    not something more splitting can fix.
    """
    try:
        raw = call(build_content(items))
        return [extract_json(raw)]
    except ContextCapacityError:
        if len(items) <= min_chunk:
            raise
        mid = len(items) // 2
        return (
            _run_chunked_or_single(items[:mid], build_content, call, min_chunk)
            + _run_chunked_or_single(items[mid:], build_content, call, min_chunk)
        )


def _reduce_partial_analyses(partials: list, label: str, task_name: str, system: str) -> dict:
    """Synthesize N partial {"overall_summary","recommendations"} results — each
    produced by analyzing one chunk of a pipeline too large for a single pass —
    into one coherent result.

    This call only ever sees the already-condensed partial summaries, never the
    raw job data, so it stays small regardless of pipeline size and should fit
    even a small context window without needing capacity handling of its own.
    """
    joined = "\n\n".join(
        f"--- Chunk {i + 1} of {len(partials)} ---\n"
        f"Summary: {(p.get('overall_summary') or '').strip()}\n"
        f"Recommendations: {'; '.join(str(r) for r in (p.get('recommendations') or []))}"
        for i, p in enumerate(partials)
    )
    content = (
        f"This {label} was produced in {len(partials)} separate passes because the "
        "candidate's full pipeline didn't fit in the configured AI model's context window "
        "in one call. Synthesize the per-chunk notes below into ONE coherent result: merge "
        "overlapping points, resolve contradictions using the more specific/concrete version, "
        "and deduplicate recommendations rather than just concatenating them.\n\n"
        + joined
        + '\n\nReturn ONLY a JSON object: {"overall_summary": string, "recommendations": [string, ...]}'
    )
    text = _call_with_thinking(system, content, 4096, task_name=task_name)
    return extract_json(text)


def run_weekly_review(api_key: str = "", model: str = "",
                      thinking_mode: str = "medium") -> dict:
    """Generate the weekly strategy review via the API.

    Sends pipeline + weekly summary data to Claude, saves the result as an
    AIInsight, and returns the parsed dict (overall_summary, recommendations).
    Model selection comes from provider configuration. The api_key and model
    params are kept for backward compatibility but ignored.
    Raises requests.HTTPError or ValueError on failure.
    """
    from datetime import timedelta, datetime as dt

    # Build the data payload.
    pipeline = build_export_dict()
    week_ago = dt.now(timezone.utc) - timedelta(days=7)

    # Lightweight weekly summary (mirrors get_weekly_summary MCP tool).
    from .models import Interview as _Interview, JobNote as _JobNote
    new_jobs = Job.query.filter(Job.created_at >= week_ago).all()
    status_notes = (
        _JobNote.query
        .filter(_JobNote.note_type == "status_change")
        .filter(_JobNote.created_at >= week_ago)
        .all()
    )
    new_interviews = (
        db.session.query(_Interview)
        .filter(_Interview.created_at >= week_ago)
        .all()
    )
    weekly_summary = {
        "period": "last 7 days",
        "new_jobs_added": len(new_jobs),
        "new_jobs": [{"id": j.id, "title": j.title, "company": j.company,
                      "status": j.status, "source": j.source} for j in new_jobs],
        "status_changes": [{"job_id": n.job_id, "content": n.content,
                            "when": n.created_at.strftime("%Y-%m-%d")} for n in status_notes],
        "interviews_completed": len(new_interviews),
        "interviews": [
            {"job_id": iv.job_id, "round": iv.round_type,
             "self_rating": iv.self_rating,
             "went_well": (iv.went_well or "")[:200],
             "to_improve": (iv.to_improve or "")[:200]}
            for iv in new_interviews
        ],
    }

    full_jobs = pipeline["jobs"]

    def _content(job_subset):
        c = (
            _WEEKLY_REVIEW_INSTRUCTIONS
            + "\n\nCANDIDATE: " + pipeline["candidate"]
            + "\n\nWEEKLY ACTIVITY:\n" + json.dumps(weekly_summary)
        )
        if len(job_subset) < len(full_jobs):
            c += (
                "\n\n(NOTE: this is one chunk of a larger pipeline, being analyzed in parts "
                "because the configured AI model's context window is limited. Analyze only "
                "the jobs given below; a separate pass will combine all chunks.)"
            )
        c += "\n\nFULL PIPELINE:\n" + json.dumps({"jobs": job_subset})
        return c

    def _call(content):
        return _call_with_thinking(_WEEKLY_REVIEW_SYSTEM, content, 4096, task_name="weekly_review")

    log.info("weekly review: pipeline data assembled (%d jobs), calling AI provider...",
              len(full_jobs))
    try:
        partials = _run_chunked_or_single(full_jobs, _content, _call)
    except ContextCapacityError:
        log.error(
            "weekly review: even a single job exceeds the configured provider's context "
            "window — raise num_ctx (docs/PLAN-ollama-assist.md) or configure a provider "
            "with more headroom"
        )
        raise

    if len(partials) == 1:
        log.info("weekly review: response received, parsing...")
        parsed = partials[0]
    else:
        log.warning(
            "weekly review: pipeline (%d jobs) didn't fit the configured provider's context "
            "window in one pass — split into %d chunk(s) and synthesized; cross-job patterns "
            "spanning chunk boundaries may be less precise than a single-pass review",
            len(full_jobs), len(partials),
        )
        parsed = _reduce_partial_analyses(
            partials, "weekly strategy review", "weekly_review", _WEEKLY_REVIEW_SYSTEM)
        summary_note = (parsed.get("overall_summary") or "").strip()
        if summary_note:
            parsed["overall_summary"] = (
                "[Note: the configured AI model's context window couldn't fit the full "
                "pipeline in one pass, so this review was assembled from a chunked analysis "
                "and may miss some cross-job patterns a single-pass review would catch.] "
                + summary_note
            )

    # Save to AIInsight.
    summary = (parsed.get("overall_summary") or "").strip()
    recs = parsed.get("recommendations") or []
    if isinstance(recs, str):
        recs = [recs]
    if summary:
        db.session.add(AIInsight(
            summary=summary,
            recommendations="\n".join(str(r) for r in recs),
            source="AI (Weekly Review)",
            created_by="api",
        ))
        db.session.commit()

    log.info("weekly review generated: %d chars, %d recommendations", len(summary), len(recs))
    return parsed


# ---------------------------------------------------------------------------
# Feature 4: ATS Keyword Gap Analysis
# ---------------------------------------------------------------------------

_ATS_SYSTEM = (
    "You are an ATS (Applicant Tracking System) and resume expert analyzing keyword gaps. "
    "Respond with ONLY a valid JSON object — no prose, no markdown fences."
)

_ATS_INSTRUCTIONS = """\
Analyze the job description below and identify keywords and phrases that are absent or weakly
represented in the candidate's resume/profile. Focus on terms an ATS would scan for.

Look for:
- Required and preferred skills listed in the job description
- Industry-specific terminology and acronyms
- Action verbs and outcome phrases used in the job description
- Software, tools, certifications, and methodologies mentioned
- Any keywords that appear multiple times (higher weight)

For each gap, suggest a specific substitution or addition the candidate can make to their resume.

Return ONLY a JSON object:
{
  "missing_keywords": [{"keyword": "<term>", "context": "<where it appears in JD>", "suggestion": "<how to work it in>"}],
  "weak_keywords": [{"keyword": "<term>", "current_usage": "<how candidate uses it now>", "suggestion": "<stronger phrasing>"}],
  "overall_match_estimate": "<percentage range, e.g. 60-70%>",
  "top_priority_additions": ["<keyword 1>", "<keyword 2>", "<keyword 3>"]
}
"""


def run_ats_analysis(job, profile_text: str, api_key: str = "", model: str = "") -> dict:
    """Run ATS keyword gap analysis for a job against the candidate profile.

    job: a Job model instance (needs .title, .company, .notes).
    Model selection comes from provider configuration. The api_key and model
    params are kept for backward compatibility but ignored.
    Returns the parsed gap analysis dict.
    Raises requests.HTTPError or ValueError on failure.
    """
    description = (job.notes or "").strip()
    if not description:
        return {
            "missing_keywords": [],
            "weak_keywords": [],
            "overall_match_estimate": "N/A — no job description captured",
            "top_priority_additions": [],
        }

    content = (
        _ATS_INSTRUCTIONS
        + "\n\nJOB: " + job.title + " at " + job.company
        + "\n\nJOB DESCRIPTION:\n" + description[:3000]
        + "\n\nCANDIDATE PROFILE:\n" + profile_text[:3000]
    )
    text = _call_no_thinking(_ATS_SYSTEM, content, 2048)
    parsed = extract_json(text)

    # Persist to the job record.
    gap_md = _format_ats_gap_as_markdown(parsed, job)
    job.kit_ats_gap = gap_md
    db.session.commit()

    log.info("ATS analysis complete for job %d (%s @ %s)", job.id, job.title, job.company)
    return parsed


def _format_ats_gap_as_markdown(parsed: dict, job) -> str:
    """Convert the structured ATS gap dict to readable markdown for storage."""
    lines = [
        f"## ATS Gap Analysis — {job.title} at {job.company}",
        "",
        f"**Estimated ATS match:** {parsed.get('overall_match_estimate', 'N/A')}",
        "",
    ]

    top = parsed.get("top_priority_additions") or []
    if top:
        lines += ["**Top priority additions:** " + ", ".join(f"`{k}`" for k in top), ""]

    missing = parsed.get("missing_keywords") or []
    if missing:
        lines.append("### Missing keywords")
        for item in missing:
            lines.append(f"- **{item.get('keyword', '')}** — {item.get('context', '')}")
            if item.get("suggestion"):
                lines.append(f"  *Suggestion:* {item['suggestion']}")
        lines.append("")

    weak = parsed.get("weak_keywords") or []
    if weak:
        lines.append("### Keywords to strengthen")
        for item in weak:
            lines.append(f"- **{item.get('keyword', '')}** (currently: {item.get('current_usage', 'present')})")
            if item.get("suggestion"):
                lines.append(f"  *Suggestion:* {item['suggestion']}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Onboarding — resume interview, API mode (docs/PLAN-onboarding.md, Phase 2)
#
# Multi-turn conversation over a stateless single-shot call: each turn resends
# the whole transcript as plain text in user_content (call_with_fallback / the
# OpenAI-compat path only ever sends one system + one user message — there is
# no chat-history param), and the model is instructed to ask one question at a
# time until it has enough to write the resume, at which point it emits a
# sentinel block this function parses out.
# ---------------------------------------------------------------------------

_RESUME_INTERVIEW_SENTINEL = "===RESUME_READY==="
_RESUME_FACTS_SENTINEL = "===PROFILE_FACTS==="

_RESUME_INTERVIEW_SYSTEM = (
    "You are a resume-writing coach interviewing a candidate who has no resume yet. "
    "Ask exactly ONE question per turn — never a list of questions. Build on the "
    "transcript so far; do not repeat a question already answered. Keep questions "
    "short and concrete, and push for specific numbers (how many, how much, how "
    "often) when an answer is vague."
)


def _format_interview_transcript(history: list) -> str:
    lines = []
    for turn in history or []:
        role = "Interviewer" if turn.get("role") == "assistant" else "Candidate"
        content = (turn.get("content") or "").strip()
        if content:
            lines.append(f"{role}: {content}")
    return "\n\n".join(lines)


def run_resume_interview_turn(history: list, candidate_name: str = "the candidate") -> dict:
    """Advance the onboarding resume interview by one turn.

    history: list of {"role": "assistant"|"user", "content": str} — "assistant"
    entries are questions already asked, "user" entries are the candidate's
    answers, in order. Pass [] to get the opening question.

    Returns:
      {"done": False, "message": "<next question>"}
      {"done": True, "message": "<closing note>", "resume_markdown": "...", "profile_facts": "..."}
    Raises whatever call_with_fallback raises (caller shows an error and lets
    the candidate retry or fall back to manual/MCP mode).
    """
    from .prompts import RESUME_BEST_PRACTICES

    transcript = _format_interview_transcript(history)
    cover_areas = (
        "target job titles/field, work history (employer, title, dates, specific "
        "quantified accomplishments per job), education, certifications or "
        "licenses, skills, and other notable achievements"
    )
    content = (
        f"Candidate name: {candidate_name}\n\n"
        f"Areas to cover before you have enough to write the resume: {cover_areas}.\n\n"
        "Resume writing rules the FINAL resume must follow:\n"
        f"{RESUME_BEST_PRACTICES}\n"
        "If you do NOT yet have enough to write a complete, specific resume, respond "
        "with ONLY your next single question — no preamble, no markdown, no sentinel.\n\n"
        "Once you DO have enough, respond with EXACTLY this format and nothing else:\n"
        f"{_RESUME_INTERVIEW_SENTINEL}\n"
        "<the complete resume as clean markdown>\n"
        f"{_RESUME_FACTS_SENTINEL}\n"
        "<a short plain-text summary of the candidate's background worth adding to "
        "their profile: target roles, years of experience, top skills. Leave this "
        "section blank if there is nothing beyond what's in the resume.>\n\n"
        + ("This is the start of the interview — there is no transcript yet. Ask your "
           "first question." if not transcript else
           f"Transcript so far:\n\n{transcript}")
    )

    raw, _provider = call_with_fallback(_RESUME_INTERVIEW_SYSTEM, content, max_tokens=3000)
    raw = (raw or "").strip()

    if _RESUME_INTERVIEW_SENTINEL in raw:
        after = raw.split(_RESUME_INTERVIEW_SENTINEL, 1)[1].strip()
        if _RESUME_FACTS_SENTINEL in after:
            resume_md, facts = after.split(_RESUME_FACTS_SENTINEL, 1)
        else:
            resume_md, facts = after, ""
        return {
            "done": True,
            "message": "Resume drafted — review it below before saving.",
            "resume_markdown": resume_md.strip(),
            "profile_facts": facts.strip(),
        }

    return {"done": False, "message": raw}


# ---------------------------------------------------------------------------
# Feature 5: Rejection Pattern Deep-Dive
# ---------------------------------------------------------------------------

_REJECTION_SYSTEM = (
    "You are an expert job-search coach analyzing rejection patterns. "
    "Respond with ONLY a valid JSON object — no prose, no markdown fences."
)

_REJECTION_INSTRUCTIONS = """\
Analyze the rejection and ghosting history below and identify patterns.

Look for:
- At what stage do most rejections occur? (Applied with no response, after phone screen, after interview?)
- What do rejected roles have in common? (title, industry, company size, work mode, salary range, source)
- What do roles that progressed have in common?
- Are there interview self-ratings that correlate with rejection?
- Are there posting quality signals (source, salary) that correlate with outcomes?

Be specific and data-driven. "You tend to be rejected after phone screens when applying to roles
requiring SAP SD experience you don't have" is useful. "Apply better" is not.

Return ONLY a JSON object:
{"overall_summary": "<honest, specific analysis in 2-3 paragraphs>", "recommendations": ["<specific action>", ...]}
"""


def run_rejection_analysis(api_key: str = "", model: str = "",
                           thinking_mode: str = "medium") -> dict:
    """Analyze rejection patterns via the API.

    Queries all rejected/ghosted jobs plus their interview history. Saves result
    to AIInsight. Returns the parsed dict. Model selection comes from provider
    configuration. The api_key and model params are kept for backward compatibility
    but ignored. Raises on API failure.
    """

    rejected = Job.query.filter(Job.status.in_(["Rejected", "Ghosted"])).all()
    active = Job.query.filter(Job.status.in_(["Applied", "Phone Screen",
                                               "Interview", "Final Interview",
                                               "Offer", "Hired"])).all()

    active_light = [
        {"id": j.id, "title": j.title, "company": j.company,
         "status": j.status, "source": j.source,
         "work_mode": j.work_mode, "salary": j.salary}
        for j in active
    ]

    def _rejected_dicts(subset):
        return [
            {
                "id": j.id, "title": j.title, "company": j.company,
                "status": j.status, "source": j.source,
                "location": j.location, "work_mode": j.work_mode,
                "salary": j.salary,
                "date_applied": str(j.date_applied) if j.date_applied else None,
                "interviews": [
                    {"round": iv.round_type, "self_rating": iv.self_rating,
                     "went_well": (iv.went_well or "")[:150],
                     "to_improve": (iv.to_improve or "")[:150]}
                    for iv in j.interviews
                ],
            }
            for j in subset
        ]

    def _content(rejected_subset):
        pipeline_data = {
            "rejected_or_ghosted": _rejected_dicts(rejected_subset),
            "active_or_succeeded": active_light,
            "total_rejected": len(rejected),
            "total_active": len(active),
        }
        c = _REJECTION_INSTRUCTIONS + "\n\nPIPELINE DATA:\n" + json.dumps(pipeline_data)
        if len(rejected_subset) < len(rejected):
            c += (
                "\n\n(NOTE: 'rejected_or_ghosted' above is one chunk of a larger set, being "
                "analyzed in parts because the configured AI model's context window is "
                "limited — total_rejected reflects the true full count across all chunks.)"
            )
        return c

    def _call(content):
        return _call_with_thinking(_REJECTION_SYSTEM, content, 4096, task_name="rejection_alert")

    try:
        partials = _run_chunked_or_single(rejected, _content, _call)
    except ContextCapacityError:
        log.error(
            "rejection analysis: even a single job exceeds the configured provider's context "
            "window — raise num_ctx (docs/PLAN-ollama-assist.md) or configure a provider "
            "with more headroom"
        )
        raise

    if len(partials) == 1:
        parsed = partials[0]
    else:
        log.warning(
            "rejection analysis: %d rejected/ghosted job(s) didn't fit the configured "
            "provider's context window in one pass — split into %d chunk(s) and synthesized; "
            "cross-job patterns spanning chunk boundaries may be less precise than a "
            "single-pass analysis",
            len(rejected), len(partials),
        )
        parsed = _reduce_partial_analyses(
            partials, "rejection pattern analysis", "rejection_alert", _REJECTION_SYSTEM)
        summary_note = (parsed.get("overall_summary") or "").strip()
        if summary_note:
            parsed["overall_summary"] = (
                "[Note: the configured AI model's context window couldn't fit all "
                "rejected/ghosted jobs in one pass, so this analysis was assembled from a "
                "chunked pass and may miss some cross-job patterns a single-pass analysis "
                "would catch.] " + summary_note
            )

    # Save to AIInsight.
    summary = (parsed.get("overall_summary") or "").strip()
    recs = parsed.get("recommendations") or []
    if isinstance(recs, str):
        recs = [recs]
    if summary:
        db.session.add(AIInsight(
            summary=summary,
            recommendations="\n".join(str(r) for r in recs),
            source="AI (Rejection Analysis)",
            created_by="api",
        ))
        db.session.commit()

    log.info("rejection analysis complete: %d rejected jobs analyzed", len(rejected))
    return parsed


# ---------------------------------------------------------------------------
# Feature 6: Thinking mode helper (shared by F3 and F5)
# ---------------------------------------------------------------------------

def _apply_thinking(body: dict, model_str: str, thinking_mode: str):
    """Mutate body in-place to add thinking params if thinking_mode != 'disabled'."""
    thinking = (thinking_mode or "disabled").strip()
    if thinking == "disabled":
        return
    if model_str in _ADAPTIVE_MODELS:
        body["effort"] = thinking
    else:
        budget = _THINKING_BUDGETS.get(thinking, 5000)
        body["thinking"] = {"type": "enabled", "budget_tokens": budget}
        body["max_tokens"] = max(body.get("max_tokens", 4096), budget + 2048)


# ---------------------------------------------------------------------------
# Single-job helpers: Score fit, Draft follow-up, Build kit (API mode buttons)
# ---------------------------------------------------------------------------

_SINGLE_SCORE_SYSTEM = (
    "You are a job-search assistant scoring a job posting for candidate fit. "
    "Respond with ONLY a valid JSON object — no prose, no markdown fences."
)

_SINGLE_SCORE_INSTRUCTIONS = """\
Score this specific job for fit against the candidate profile below on a scale of 1-10.

Scoring guide:
- 8-10: Strong match — title, experience level, and work mode all align; salary (if listed) meets or exceeds the candidate's target.
- 5-7:  Partial match — worth considering but has notable gaps (wrong work mode, stretch title, low salary, or vague posting).
- 3-4:  Weak match — significant mismatch in title, experience level, or requirements.
- 1-2:  Poor fit — commission-only, MLM language, outside the candidate's field, or completely mismatched.

Return ONLY a JSON object in this exact schema:
{"score": <1-10>, "reason": "<2-3 sentence explanation>"}

No other text before or after the object.
"""


def run_score_fit_single(job) -> dict:
    """Score a single job's fit against the candidate profile via the API.

    Returns {"score": int, "reason": str}.
    Updates job.ai_fit_score and job.ai_fit_reason in the DB.
    Raises on API error.
    """
    profile_text = _load_candidate_profile()
    job_data = {
        "id": job.id,
        "title": job.title,
        "company": job.company,
        "location": job.location or "",
        "work_mode": job.work_mode or "",
        "salary": job.salary or "",
        "source": job.source or "",
        "description": (job.notes or "")[:1200],
    }
    content = (
        _SINGLE_SCORE_INSTRUCTIONS
        + "\n\nCANDIDATE PROFILE:\n"
        + profile_text
        + "\n\nJOB:\n"
        + json.dumps(job_data)
    )
    raw = _call_no_thinking(_SINGLE_SCORE_SYSTEM, content, 512,
                             use_triage_model=True, task_name="score_fit")
    parsed = extract_json(raw)
    score = max(1, min(10, int(parsed.get("score", 5))))
    reason = (parsed.get("reason") or "").strip()
    job.ai_fit_score = score
    job.ai_fit_reason = reason
    db.session.commit()
    log.info("score_fit_single: job %d scored %d", job.id, score)
    return {"score": score, "reason": reason}


_SINGLE_FOLLOWUP_SYSTEM = (
    "You are a professional job-search assistant writing follow-up emails. "
    "Respond with ONLY a valid JSON object — no prose, no markdown fences."
)

_SINGLE_FOLLOWUP_INSTRUCTIONS = """\
Draft a brief, professional follow-up email for the job described below.

Requirements:
- 3-4 sentences maximum, not counting the subject line
- Include a subject line as the first line, then a blank line, then the body
- No em-dashes anywhere
- No AI cliches ("I wanted to reach out", "I hope this finds you well", "leverage", "passionate", etc.)
- Address to the hiring team or the recruiter if a contact name is provided
- Reference the specific role and company
- Keep it warm, direct, and human

Return ONLY a JSON object in this schema:
{"email_text": "<Subject: ...>\\n\\n<body text>"}

No other text before or after the object.
"""


def run_draft_followup_single(job) -> dict:
    """Draft a follow-up email for a single job via the API.

    Returns {"email_text": str}.
    Saves the draft to job.followup_draft in the DB.
    Raises on API error.
    """
    profile_text = _load_candidate_profile()
    job_data = {
        "id": job.id,
        "title": job.title,
        "company": job.company,
        "status": job.status,
        "contact_name": job.contact_name or "",
        "contact_email": job.contact_email or "",
        "date_applied": str(job.date_applied) if job.date_applied else None,
        "notes": (job.notes or "")[:400],
    }
    content = (
        _SINGLE_FOLLOWUP_INSTRUCTIONS
        + "\n\nCANDIDATE PROFILE:\n"
        + profile_text
        + "\n\nJOB:\n"
        + json.dumps(job_data)
    )
    raw = _call_no_thinking(_SINGLE_FOLLOWUP_SYSTEM, content, 512,
                             use_triage_model=True, task_name="followup_single")
    parsed = extract_json(raw)
    email_text = (parsed.get("email_text") or "").strip()
    if not email_text:
        raise ValueError("AI returned empty follow-up draft")
    job.followup_draft = email_text
    db.session.commit()
    log.info("draft_followup_single: follow-up drafted for job %d", job.id)
    return {"email_text": email_text}


_KIT_API_SYSTEM = (
    "You are an expert career coach building a tailored job application package. "
    "Respond with ONLY Markdown — no JSON wrapper, no preamble, no fences."
)

_KIT_API_INSTRUCTIONS = """\
Build a COMPLETE tailored application package for the job below using the candidate profile
provided. This is a full package with six required sections — do not stop early, do not
summarize instead of writing the actual documents, and do not skip any section for length.
Every section below is mandatory and must appear with its exact Markdown heading, in order:

## Fit Assessment
One-line verdict (Strong Fit / Partial Fit / Stretch), any flags (salary, hard requirements not met,
work mode conflicts), and exactly one of: Proceed | Proceed with caveats | Consider skipping
followed by one sentence of reasoning. Keep this section short — a few lines only. The bulk of
your output should go to the sections below, not this one.

## Tailored Resume
A complete, ATS-friendly resume with keywords from the posting woven in truthfully. Full contact
header, summary, skills, and complete work history with real bullet points, not a condensed
outline. Plain text, no tables or columns, no fabricated employers, dates, metrics, or skills not
in the profile.

## Cover Letter
A complete cover letter, up to 300 words, addressed to the hiring team. Tie the body to two or
three concrete accomplishments from the profile. Write the full letter, not a summary of what it
should contain.

## Application Email
A complete, ready-to-send email up to 150 words with a subject line. Short and direct.

## Follow-Up Email
A complete, ready-to-send follow-up to send 5-7 business days after applying with no response.
Subject line + 3-4 sentences.

## Interview Questions
Five likely questions, each with its own complete 3-5 sentence answer framework, drawn from the
job posting and the candidate's real background. Use STAR format loosely. No fabricated
experiences. Write out all five questions with full answers, not an abbreviated list.

{research_section}
STYLE RULES (apply everywhere):
- No em-dashes. No AI cliches. Sound like a real person.
- Avoid: "I am thrilled", "leverage", "passionate about", "in today's fast-paced world",
  "delve", "tapestry", "testament to", "I wanted to reach out", "I hope this finds you well".
- Real numbers and achievements only. Never fabricate.
- Write every section in full. A one-line placeholder or a summary sentence in place of the
  actual resume/letter/email text is an incomplete answer and not acceptable.
"""

# Markdown "## Heading" strings the model is expected to produce. Used to check
# completeness of the response so an incomplete kit (a common failure mode with
# smaller/free-tier models — they sometimes stop after the Fit Assessment and
# never write the actual documents) is visible in the live status log instead
# of silently shipping a half-finished kit.
_KIT_EXPECTED_SECTIONS = [
    "Fit Assessment", "Tailored Resume", "Cover Letter",
    "Application Email", "Follow-Up Email", "Interview Questions",
]


def _build_kit_markdown(title: str, company: str, location: str = "",
                         salary: str = "", work_mode: str = "",
                         description: str = "", profile_text: str = "",
                         research_notes: str = "") -> tuple[str, str]:
    """Core kit builder shared by tracked-job and ad-hoc kit generation.

    Calls the configured AI provider chain (call_with_fallback — Anthropic or any
    OpenAI-compatible provider, including free-tier ones like Ollama, Groq,
    Gemini, OpenRouter, GitHub Models, Cerebras, Mistral).

    research_notes, if given (see websearch.py), is a best-effort web research
    summary (company background + salary benchmarks) folded into the prompt so
    the kit isn't built blind, even though the API path can't browse the live
    posting the way the Claude/MCP flow does.

    Returns (kit_markdown_without_header, provider_name). Raises on API error.
    """
    job_section = (
        f"Title: {title}\n"
        f"Company: {company}\n"
        + (f"Location: {location}\n" if location else "")
        + (f"Work mode: {work_mode}\n" if work_mode else "")
        + (f"Salary: {salary}\n" if salary else "")
        + "\nJob Description:\n"
        + (description or "(No description captured.)").strip()[:3000]
    )
    if research_notes:
        research_block = (
            "## Research Notes\n"
            "Summarize the web research findings below in 2-4 sentences at the very top of your "
            "output, before the Fit Assessment, under a 'Research Notes' heading. Use them to "
            "personalize the Cover Letter and to flag any salary concerns in the Fit Assessment. "
            "Treat these as unverified search snippets, not confirmed facts.\n"
        )
    else:
        research_block = ""
    instructions = _KIT_API_INSTRUCTIONS.format(research_section=research_block)
    content = (
        instructions
        + "\n\nCANDIDATE PROFILE:\n"
        + profile_text
        + "\n\nJOB:\n"
        + job_section
        + (f"\n\nWEB RESEARCH FINDINGS (unverified, best-effort search results):\n{research_notes}"
           if research_notes else "")
    )
    raw, provider = call_with_fallback(_KIT_API_SYSTEM, content, max_tokens=8192)
    raw = raw.strip()

    missing = [s for s in _KIT_EXPECTED_SECTIONS if s not in raw]
    if missing:
        log.warning("kit response is missing section(s), likely truncated or the model "
                    "didn't follow instructions: %s", ", ".join(missing))
    return raw, provider


_KIT_ATTACHMENT_KIND = "Application Kit"


def _save_kit_docx_attachment(job, kit_md: str, provider: str) -> None:
    """Render the kit markdown to .docx and attach it to the job record.

    This is what makes the API-built kit downloadable from the job detail page
    like any other uploaded file, instead of living only as text in kit_output.
    Replaces any previous API-generated kit attachment on the job so re-running
    the build doesn't pile up stale copies. Failures here are logged and
    swallowed — a docx-generation problem should never lose the kit text itself,
    which is already saved to job.kit_output by the caller.
    """
    import uuid as _uuid
    from flask import current_app
    from werkzeug.utils import secure_filename

    from .docgen import markdown_to_docx_bytes
    from .models import Attachment

    try:
        docx_bytes = markdown_to_docx_bytes(kit_md)
    except Exception as exc:  # noqa: BLE001
        log.warning("kit docx generation failed for job %d: %s", job.id, exc)
        return

    upload_dir = current_app.config["UPLOAD_DIR"]

    # Remove any prior API-generated kit attachment (and its file) before adding
    # the fresh one, so kits built more than once don't accumulate duplicates.
    for old in list(job.attachments):
        if old.kind == _KIT_ATTACHMENT_KIND:
            old_path = os.path.join(upload_dir, old.stored_name)
            try:
                if os.path.exists(old_path):
                    os.remove(old_path)
            except OSError:
                log.warning("could not remove old kit attachment file %s", old_path)
            db.session.delete(old)

    stored_name = f"{_uuid.uuid4().hex}.docx"
    dest = os.path.join(upload_dir, stored_name)
    with open(dest, "wb") as fh:
        fh.write(docx_bytes)

    label = _PROVIDER_LABELS.get(provider, (provider or "AI").title())
    safe_stem = secure_filename(f"{job.company}-{job.title}-kit")[:80] or "application-kit"

    db.session.add(Attachment(
        job_id=job.id,
        kind=_KIT_ATTACHMENT_KIND,
        original_name=f"{safe_stem}.docx",
        stored_name=stored_name,
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size=len(docx_bytes),
        uploaded_by=f"AI ({label})",
    ))
    db.session.commit()
    log.info("kit docx attached to job %d (%s)", job.id, safe_stem)


def _safe_research(company: str, title: str, location: str = "") -> str:
    """Best-effort DuckDuckGo research, logged so it streams into the live status page.

    Never raises — a research failure should never block kit generation.
    Returns "" if research found nothing or the lookup itself failed.
    """
    try:
        log.info("Researching %s and salary benchmarks for %s (web search, best-effort)…",
                  company, title)
        from .websearch import research_company_and_salary
        notes = research_company_and_salary(company, title, location)
        if notes:
            log.info("Research found %d character(s) of company/salary context.", len(notes))
        else:
            log.info("Research found nothing usable — continuing without it.")
        return notes
    except Exception as exc:  # noqa: BLE001
        log.warning("Research step failed (%s) — continuing without it.", exc)
        return ""


def run_build_kit_api(job) -> str:
    """Build a tailored application kit for a tracked job via the API.

    Generates a resume, cover letter, application/follow-up emails, and interview
    prep using the candidate profile and job description on file, after a
    best-effort web research pass (see _safe_research / websearch.py). Saves the
    result to job.kit_output and attaches a .docx copy to the job (downloadable
    from the job detail page, like any other uploaded file). Returns the kit
    markdown string. Raises on API error.
    """
    job_id = job.id
    profile_text = _load_candidate_profile()
    research_notes = _safe_research(job.company, job.title, job.location or "")
    raw, provider = _build_kit_markdown(
        job.title, job.company, job.location or "", job.salary or "",
        job.work_mode or "", job.notes or "", profile_text,
        research_notes=research_notes,
    )
    kit_md = _kit_header(job.title, job.company, provider) + raw

    # call_with_fallback() may call db.session.remove() internally (ranked-chain
    # provider lookup), which detaches any ORM objects loaded before the call —
    # including `job`. Re-fetch it from the (possibly new) scoped session before
    # touching it again, or job.kit_output silently fails to save and any
    # attribute/relationship access below raises DetachedInstanceError.
    job = db.session.get(Job, job_id)
    if job is None:
        log.warning("run_build_kit_api: job %d vanished mid-request", job_id)
        return kit_md
    job.kit_output = kit_md
    db.session.commit()
    _save_kit_docx_attachment(job, kit_md, provider)
    log.info("build_kit_api: kit generated for job %d (%s @ %s) via %s",
              job.id, job.title, job.company, provider)
    return kit_md


def build_kit_api_adhoc(title: str, company: str, location: str = "",
                         description: str = "", url: str = "",
                         salary: str = "", job=None) -> str:
    """Build an application kit via the API for a job that may or may not be tracked.

    Used by the Kit Hub page's "Build kit via API" button, so kit generation works
    for anyone using an AI API key or a free-tier provider — not just Claude/MCP.

    If `job` (a Job model instance) is given, the result is also saved to
    job.kit_output and attached as a downloadable .docx on that job. Returns the
    kit markdown string. Raises on API error (e.g. RuntimeError if no AI
    provider is configured).
    """
    job_id = job.id if job is not None else None
    profile_text = _load_candidate_profile()
    research_notes = _safe_research(company, title, location)
    raw, provider = _build_kit_markdown(
        title, company, location, salary, "", description, profile_text,
        research_notes=research_notes,
    )
    kit_md = _kit_header(title, company, provider, url=url) + raw
    if job_id is not None:
        # Re-fetch: call_with_fallback() may have called db.session.remove()
        # internally (ranked-chain provider lookup), which detaches any ORM
        # objects loaded before the call, including `job`. See the matching
        # comment in run_build_kit_api() for the full explanation.
        job = db.session.get(Job, job_id)
        if job is None:
            log.warning("build_kit_api_adhoc: job %d vanished mid-request", job_id)
            return kit_md
        job.kit_output = kit_md
        db.session.commit()
        _save_kit_docx_attachment(job, kit_md, provider)
        log.info("build_kit_api_adhoc: kit generated for job %d (%s @ %s) via %s",
                  job.id, title, company, provider)
    else:
        log.info("build_kit_api_adhoc: kit generated for untracked posting (%s @ %s) via %s",
                  title, company, provider)
    return kit_md


def _kit_header(title: str, company: str, provider: str, url: str = "") -> str:
    label = _PROVIDER_LABELS.get(provider, (provider or "AI").title())
    return (
        f"# Application Kit — {title} at {company}\n"
        f"*Generated via API ({label}). Web research is best-effort (DuckDuckGo) and unverified — review before use.*\n"
        + (f"*Posting: {url}*\n" if url else "")
        + "\n"
    )


_INTERVIEW_PREP_SYSTEM = (
    "You are an expert career coach building an interview preparation guide. "
    "Respond with ONLY Markdown — no JSON wrapper, no preamble, no fences."
)

_INTERVIEW_PREP_INSTRUCTIONS = """\
Build a thorough interview prep guide for the candidate preparing for the job below.

Produce these sections in order, each with a Markdown heading:

## Role and Company Context
One short paragraph summarizing what to know going in: what the company does, what the role
requires, and what the interviewer likely cares most about. Draw only from the job description
and candidate profile provided — no invented facts.

## Likely Interview Questions
8-10 questions likely to be asked for this specific role. At least two must be behavioral
("Tell me about a time...") and at least one must be role-specific (a tool, process, or
scenario from the job description).

For each question, write a 3-5 sentence answer framework using the candidate's real background.
Anchor every framework in a specific achievement, number, or situation from the profile.
Use STAR format loosely (Situation, Task, Action, Result) but write it as natural talking points.
No fabricated experiences. If a question requires knowledge the candidate does not have,
say so and suggest how to frame an honest, positive answer.

## STAR Stories from Your Profile
3-4 strong stories from the candidate's background that can flex across multiple question types.
Format each as: Story title → Situation → Task → Action → Result (one sentence each).

## Weaknesses to Address
1-2 likely gaps or concerns an interviewer might raise based on this specific role and the
candidate's profile. For each, suggest an honest, positive framing.

## Questions to Ask Them
5 sharp questions the candidate can ask the interviewer, drawn from this specific posting —
not generic questions that could apply to any role.

STYLE RULES: No em-dashes. No AI cliches. Sound like a real person.
"""


def run_interview_prep_single(job) -> str:
    """Generate an interview prep guide for a single job via the API.

    Saves the result to the most recent interview record's prep_notes field,
    or appends to job.notes if no interview record exists yet.
    Returns the prep guide markdown string.
    Raises on API error.
    """
    profile_text = _load_candidate_profile()
    job_section = (
        f"Title: {job.title}\n"
        f"Company: {job.company}\n"
        + (f"Location: {job.location}\n" if job.location else "")
        + (f"Work mode: {job.work_mode}\n" if job.work_mode else "")
        + "\nJob Description:\n"
        + (job.notes or "(No description captured.)").strip()[:2000]
    )
    content = (
        _INTERVIEW_PREP_INSTRUCTIONS
        + "\n\nCANDIDATE PROFILE:\n"
        + profile_text
        + "\n\nJOB:\n"
        + job_section
    )
    raw, _ = call_with_fallback(_INTERVIEW_PREP_SYSTEM, content, max_tokens=2048)
    prep_md = raw.strip()

    # Save to the most recent interview record, or fall back to job notes.
    if job.interviews:
        iv = sorted(job.interviews, key=lambda x: x.created_at, reverse=True)[0]
        iv.prep_notes = prep_md
    else:
        existing = (job.notes or "").rstrip()
        job.notes = (existing + "\n\n--- INTERVIEW PREP ---\n" + prep_md).lstrip()
    db.session.commit()
    log.info("interview_prep_single: prep guide generated for job %d (%s @ %s)",
             job.id, job.title, job.company)
    return prep_md


def _parse_triage_response(raw: str) -> list:
    """Parse a JSON array from the triage API response, tolerating minor formatting."""
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()
    # Try direct array parse first.
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return parsed
        # Wrapped in an object key.
        for v in parsed.values():
            if isinstance(v, list):
                return v
    except json.JSONDecodeError:
        pass
    # Fall back to extract_json which handles prose-wrapped objects.
    try:
        obj = extract_json(raw)
        if isinstance(obj, list):
            return obj
        for v in obj.values():
            if isinstance(v, list):
                return v
    except (ValueError, AttributeError):
        pass
    return []
