# Setting Up AI

Job Squire works with any AI provider that exposes an OpenAI-compatible API endpoint, plus Anthropic natively. This page covers three concrete starting strategies, how to add and configure providers in Settings, and how to assign different providers to different tasks.

---

## What data Job Squire sends to AI

Before choosing a provider, it helps to know exactly what gets sent. Job Squire sends AI your:

- **Candidate profile** — your resume, work history, skills, career goals, salary expectations
- **Job descriptions** — the full text of postings you're tracking
- **Application pipeline** — which companies you've applied to, what stage each is at, any notes
- **Follow-up context** — who you've heard from, who you haven't, what you've written

This is sensitive material. A job search exposes things you may not share publicly: the fact that you're looking, what you're worth, which companies you're considering, and what you've said in interviews. The right choice depends on how comfortable you are with that data leaving your network and under what terms.

> **Privacy first?** If keeping your data entirely on your own hardware matters, use a local provider (Strategy 3). All other strategies send data to external servers.

---

## Strategy 1: Google AI Studio (free, no credit card)

Google AI Studio offers a generous free tier with a large context window — well-suited for Job Squire's longer analysis tasks like the weekly strategy review.

**Setup:**

1. Go to [aistudio.google.com](https://aistudio.google.com/app/apikey) and sign in with a Google account.
2. Click **Get API key** and create a new key.
3. Open **Settings → AI → AI Providers** in Job Squire and click **+ Add provider**.
4. Select **Google AI Studio (Gemini)** as the provider type.
5. Paste your API key. Leave the base URL blank.
6. Set a model name in the Analysis model field.

**About model names:** Google updates Gemini model names frequently. The model names shown in Job Squire's interface are examples — check [aistudio.google.com/models](https://aistudio.google.com/models) for the current list when you set this up. Gemini Flash models are a good starting point for both analysis and triage tasks.

**Rate limits:** The free tier allows 15 requests per minute and 1 million tokens per day. For a typical active job search, you will not hit these limits.

**Privacy:** Google does not use API data for model training by default. Your data is processed on Google's servers. See [Google AI Studio terms](https://ai.google.dev/gemini-api/terms) for current policy.

---

## Strategy 2: OpenRouter (free models, higher limits with a $10 deposit)

OpenRouter is a single API endpoint that routes to dozens of models, many of them free. One API key gives you access to the full catalog.

**Setup:**

1. Create an account at [openrouter.ai](https://openrouter.ai/keys) and generate an API key.
2. Open **Settings → AI → AI Providers** and click **+ Add provider**.
3. Select **OpenRouter** as the provider type.
4. Paste your API key. Leave the base URL blank.
5. Set model names for analysis and triage tasks (see below).

**Free models:** OpenRouter's free models work without a balance. Adding a $10 deposit unlocked 1,000 requests per day on free models as of July 2026 — check [openrouter.ai/docs](https://openrouter.ai/docs) for current policy, as rate limit terms can change.

**Choosing models:** Model availability changes over time. At the time of this writing, large Llama 3 70B-class models work well for analysis tasks, and smaller 8B-class models are sufficient for triage and follow-up drafts. Browse [openrouter.ai/models](https://openrouter.ai/models) and filter by "Free" to see what's currently available. The model names you enter must match OpenRouter's exact model identifiers.

**Privacy:** OpenRouter routes to third-party model providers. The privacy terms of the underlying provider also apply. Read [openrouter.ai/terms](https://openrouter.ai/terms) before use.

---

## Strategy 3: Local LLMs with Ollama (maximum privacy)

Any local LLM that exposes an OpenAI-compatible endpoint works with Job Squire. Ollama is the most common option.

Your data never leaves your network. This is the only strategy with that guarantee.

**Setup:**

1. Install Ollama from [ollama.com](https://ollama.com).
2. Pull a model: `ollama pull llama3.2` (or any model you prefer).
3. Start the Ollama server: `ollama serve` (runs automatically on macOS after install).
4. Open **Settings → AI → AI Providers** and click **+ Add provider**.
5. Select **Ollama** as the provider type.
6. No API key is needed. Set the base URL:
   - If Job Squire is running directly on your machine: `http://localhost:11434/v1`
   - If Job Squire is running in Docker: `http://host.docker.internal:11434/v1`
7. Enter the model name exactly as it appears in `ollama list`.

**Choosing models:** Small models (around 8B parameters) run on most modern hardware and work well for triage and follow-up tasks. Analysis tasks like the weekly strategy review benefit from a larger model and a bigger context window. Model suggestions are illustrative — browse [ollama.com/library](https://ollama.com/library) for what's available and current.

**Hardware:** Larger models need more RAM. A 70B-parameter model needs roughly 40 GB of system memory or GPU VRAM to run at usable speed. If you don't have that hardware, use a smaller local model for triage and one of the free cloud strategies for analysis.

**Other local options:** LiteLLM (`http://localhost:4000/v1`) works as a local proxy if you want to route through multiple backends from one endpoint. Any other local server that implements the OpenAI chat completions spec works the same way.

---

## Mixing strategies

You are not locked into one provider for everything. Job Squire lets you assign different providers to different tasks — for example, a fast free provider for triage (which runs after every search) and a more capable provider for the weekly strategy review.

Set this up in **Settings → AI → Automatic Feature Settings** after adding your providers. See [Per-task provider assignment](#per-task-provider-assignment) below.

---

## Adding a provider in Settings

Open **Settings → AI** and click **+ Add provider** in the AI Providers card.

| Field | What to enter |
|---|---|
| Provider type | Select from the dropdown. Sets the built-in base URL and test defaults. |
| Label | Optional display name, e.g. "OpenRouter Free Tier". |
| API key | Required for all cloud providers. Not needed for Ollama or LiteLLM. |
| Base URL | Leave blank for cloud providers. Required for Ollama, LiteLLM, or custom deployments. |
| Analysis model | Model used for weekly review, rejection alerts, and the manual Analyze button. |
| Triage model | Optional override for job scoring and follow-up drafts. Leave blank to use the analysis model. |
| Use for analysis tasks | Uncheck for providers with small context windows (e.g. Cerebras free tier). |
| Use for triage tasks | Uncheck if you want to reserve this provider for analysis only. |
| Thinking mode | Anthropic only. Disabled, low, medium, or high. Higher levels improve quality and add cost. |

---

## Analysis vs. triage tasks

Each provider can have two models configured, one for each task type.

**Triage model** — used for fast, high-volume tasks: scoring each new job for fit, drafting short follow-up emails. These run frequently, so speed and cost matter more than depth. A smaller, faster model is usually the better choice here.

**Analysis model** — used for deeper tasks: weekly strategy review, rejection pattern analysis, the manual Analyze button. These run less often but need stronger reasoning and a larger context window (at least 16K tokens; 32K or more recommended for full pipeline analysis).

If no triage model is set on a provider, the analysis model is used for both task types.

**Context window note:** Job Squire's full weekly review can reach 30,000 to 50,000 tokens, depending on how many jobs are in your pipeline. Providers with context windows smaller than 16K tokens (such as the Cerebras free tier) should have "Use for analysis tasks" unchecked.

---

## Per-task provider assignment

Each automatic task (triage, follow-up drafts, weekly review, rejection alert) can have its own provider chain.

Open **Settings → AI → Automatic Feature Settings** and configure each task:

- **Primary provider** — try this first. "Ranked chain" uses all enabled providers in rank order.
- **Backup provider** — try this if the primary fails (rate limit, timeout, server error).
- **Chain fallback** — if both fail, continue down the full ranked chain.

Triage-only providers (those with "Use for analysis" unchecked) only appear in task dropdowns for triage and follow-up tasks.

---

## Ranking and fallback

Use the arrow buttons on each provider row to set the order. The ranked chain is used when a task has no specific primary provider assigned, or when per-task providers are exhausted and chain fallback is enabled.

Click the **Enabled** toggle to temporarily disable a provider without removing it. Disabled providers are skipped.

---

## Testing a provider

Each provider row has a **Test** button. It sends a one-token prompt and reports:

- **Success:** provider name, model used, round-trip latency, first 80 characters of the reply.
- **Failure:** error class and message — usually enough to identify a bad key, wrong model name, or unreachable endpoint.

If no model is set, the test uses a known-cheap default for that provider type.

---

## Troubleshooting

**Provider returns errors immediately** — click Test to get the raw error. Verify the API key and check your provider dashboard for quota usage.

**Rate limit errors** — free tiers have per-minute or per-day caps. Add a second provider as a fallback so work overflows to the next in the chain.

**Ollama not reachable** — confirm Ollama is running (`ollama serve`). In Docker, use `http://host.docker.internal:11434/v1` instead of `localhost`.

**Cerebras context limit** — the Cerebras free tier caps context at around 8K tokens. Sufficient for scoring individual jobs, but not for full pipeline analysis. Uncheck "Use for analysis tasks" on this provider.

**Custom provider not working** — the endpoint must implement the OpenAI chat completions spec (`POST /chat/completions` with a `messages` array). Do not include `/chat/completions` in the base URL; Job Squire appends it.

---

## Further reading

- [AI Pipeline Analysis](11-ai-analysis.md) — running analysis on your pipeline
- [Automated AI Features](09-automated-ai.md) — the four background tasks and their schedules
- [Using Claude Pro](13-claude-pro.md) — connect via MCP with no API key
- Anthropic API privacy: https://www.anthropic.com/legal/privacy
- Google AI Studio terms: https://ai.google.dev/gemini-api/terms
- OpenRouter terms: https://openrouter.ai/terms
