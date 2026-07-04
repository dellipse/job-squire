# Multi-Provider AI — Design Notes

This document describes the architecture for the multi-provider AI system implemented in Job Squire. It is a reference for contributors, not a user guide — see [Setting Up AI](wiki/10-ai-setup.md) and [Automated AI Features](wiki/09-automated-ai.md) for user-facing documentation.

---

## Goals

- Support any OpenAI-compatible provider (Anthropic remains optional, not required)
- Per-task provider assignment: each automatic feature can have its own primary provider, backup, and chain fallback toggle
- Hybrid mode: Automatic Features (API) and MCP Connector are independent toggles, not a mutually exclusive mode
- Capability flags: distinguish triage-capable vs. analysis-capable providers (context window size matters)
- Clean UI: clear explanation of what each feature does, what model it uses, and what it costs

---

## Data model

### AIConfig (updated)

| Column | Purpose |
|---|---|
| `api_enabled` | New: Automatic Features on/off (replaces `mode == "api"`) |
| `mcp_enabled` | New: MCP Connector on/off (replaces `mode == "mcp"`) |
| `mode` | Legacy: kept for backward compatibility; data migration populates `api_enabled`/`mcp_enabled` |
| `fallback_to_anthropic` | If True, Anthropic is tried after all ranked providers fail |
| `model` | Anthropic analysis model |
| `triage_model` | Anthropic triage model |
| `api_key_enc` | Encrypted Anthropic API key |
| `thinking_mode` | Anthropic extended thinking level (disabled/low/medium/high) |
| `rejection_alert_threshold` | Number of rejections in 14 days to trigger alert |

### AIProviderConfig (updated)

| Column | Purpose |
|---|---|
| `use_for_triage` | New: this provider can handle triage tasks |
| `use_for_analysis` | New: this provider can handle analysis tasks (needs larger context) |
| `rank` | Order in the fallback chain |
| `provider` | Type key: openrouter, gemini, cerebras, github_models, nous_portal, litellm, ollama, mistral, groq, openai, custom |
| `model` | Analysis model |
| `triage_model` | Triage model (blank = use analysis model) |
| `api_key_enc` | Encrypted API key |
| `base_url` | Override URL (blank = use built-in default) |
| `enabled` | Toggle without deleting |

### AITaskConfig (new)

One row per automatic task: triage, followup, weekly_review, rejection_alert.

| Column | Purpose |
|---|---|
| `task_name` | Task identifier |
| `enabled` | Task on/off |
| `provider_id` | FK to primary AIProviderConfig (NULL = use ranked chain) |
| `backup_provider_id` | FK to backup AIProviderConfig (NULL = none) |
| `use_ranked_chain_fallback` | After primary+backup fail, try remaining ranked chain |

---

## Provider dispatch (ai.py)

`call_with_fallback(system, user_content, max_tokens, use_triage_model, task_name)` implements:

1. **Per-task primary** — if AITaskConfig.provider_id is set, try that provider using the appropriate model
2. **Per-task backup** — if primary fails, try AITaskConfig.backup_provider_id
3. **Ranked chain** — if use_ranked_chain_fallback is True, iterate remaining enabled providers filtered by capability (use_for_triage or use_for_analysis based on task type)
4. **Anthropic fallback** — if AIConfig.fallback_to_anthropic is True and an API key is set

Retryable errors: HTTP 429, 503, 529, and `requests.Timeout`. Other errors (400, 401, 404) are not retried.

### Provider base URLs

```python
_PROVIDER_URLS = {
    "openrouter":    "https://openrouter.ai/api/v1",
    "gemini":        "https://generativelanguage.googleapis.com/v1beta/openai",
    "cerebras":      "https://api.cerebras.ai/v1",
    "github_models": "https://models.github.ai/inference",
    "nous_portal":   "https://inference-api.nousresearch.com/v1",
    "ollama":        "http://localhost:11434/v1",
    "litellm":       "http://localhost:4000/v1",
    "mistral":       "https://api.mistral.ai/v1",
    "groq":          "https://api.groq.com/openai/v1",
    "openai":        "https://api.openai.com/v1",
}
```

---

## Task types

| Task | Type | Why |
|---|---|---|
| triage | triage | Short prompt per job; runs frequently; needs fast cheap model |
| followup | triage | Short per-job draft; same pattern as triage |
| weekly_review | analysis | Full pipeline data; needs large context and reasoning |
| rejection_alert | analysis | Full pipeline + history; needs large context and reasoning |

Triage tasks filter to providers where `use_for_triage=True`. Analysis tasks filter to `use_for_analysis=True`.

---

## Migrations

All schema changes are additive ALTER TABLE statements in `_run_migrations()` in `__init__.py`. No Flask-Migrate. Idempotent — safe to run on every startup.

New columns added:
- `ai_config.api_enabled`
- `ai_config.mcp_enabled`
- `ai_provider_configs.use_for_triage`
- `ai_provider_configs.use_for_analysis`
- New table: `ai_task_configs`

Data migration: existing installs with `mode='api'` or `mode='mcp'` get `api_enabled` or `mcp_enabled` set to True on first boot after upgrade.

Seeding: `_seed_task_configs()` creates AITaskConfig rows for all four tasks, migrating enabled state from the legacy `auto_*_enabled` flags on AIConfig.

---

## SQLite FK note

SQLite does not enforce `ondelete="SET NULL"` on foreign keys by default. The `ai_provider_delete` route in `main.py` manually nulls out `AITaskConfig.provider_id` and `backup_provider_id` before deleting the provider row.

---

## Recommended provider order (Settings UI)

1. OpenRouter — easiest setup, many free models, single key
2. Google AI Studio — reliable free tier, large context on Gemini Flash
3. Nous Portal — Hermes model family
4. Cerebras, GitHub Models, Groq — triage-appropriate free options
5. OpenAI, Mistral — paid
6. Ollama, LiteLLM — local/private
7. Custom — any OpenAI-compatible endpoint
