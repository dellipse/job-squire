# Automated AI Features

When **Automatic Features** is enabled in **Settings → AI**, Job Squire gains four server-side automated tasks that run in the background on a schedule.

You do not need a Claude subscription or Anthropic API key. Free providers — OpenRouter, Google AI Studio, Cerebras, GitHub Models, Groq, or a local Ollama/LiteLLM instance — can power all of these. See [Setting Up AI](10-ai-setup.md) for setup.

> **Also have MCP?** Automatic Features and the MCP Connector are independent toggles — you can enable both at the same time. The MCP connector gives you live interactive access via Claude Pro or other agents; Automatic Features handle the scheduled background work.

---

## Enabling Automatic Features

1. Open **Settings → AI**.
2. Check **Automatic Features**.
3. Add at least one AI provider in the **AI Providers** card, or enter an Anthropic API key in the Anthropic section.
4. In the **Automatic Feature Settings** card, enable each task you want and optionally assign a preferred provider to it.

---

## Per-Task Provider Assignment

Each task has its own provider configuration:

- **Primary provider** — the first provider to try for this task. "Ranked chain" means try all enabled providers in rank order.
- **Backup provider** — if the primary fails (rate limit, timeout, server error), try this one.
- **Chain fallback** — if both fail, continue down the full ranked provider chain.

This lets you use different providers for different tasks. For example: a fast free provider for triage, a more capable model for weekly review.

**Triage tasks** (Feature 1 and 2) use a smaller context window and shorter prompts. Triage-only providers (e.g. Cerebras free tier) are only available for these tasks.

**Analysis tasks** (Feature 3 and 4) send the full pipeline data to the AI and need a larger context window — at least 16K tokens recommended.

---

## Feature 1: Automatic Job Triage

After every scheduled search run, Job Squire scores each unreviewed **Saved** job for fit against your candidate profile — 1-10 with a brief reason saved to the job record.

- **Task type:** Triage (small context, high frequency)
- **Schedule:** After every scheduled search run
- **Model:** Each provider's Triage model if set; otherwise its Analysis model
- **Estimated cost:** ~$0.002/job with Haiku; free with OpenRouter free models
- **Where to see results:** Jobs list (score badge on each Saved job); dashboard routine-status widget

Scoring guide: 8-10 is a strong match; 5-7 is partial; 3-4 is a weak match; 1-2 is a poor fit.

---

## Feature 2: Automatic Follow-Up Drafts

Every day at 6 AM (configurable via `FOLLOWUP_DRAFT_HOUR` env var), Job Squire finds all active jobs where a follow-up date has passed and no draft exists, then writes a ready-to-send email for each one.

- **Task type:** Triage (short prompts, one draft per job)
- **Schedule:** Daily at 6 AM local time
- **Model:** Triage model (same as Feature 1 by default)
- **Estimated cost:** ~$0.003/draft; free with OpenRouter free models
- **Where to see results:** Each job detail page shows the drafted email. Review and send manually.
- **Email notification:** If SMTP is configured, a digest of new drafts is emailed to you.

---

## Feature 3: Weekly Strategy Review

Every Monday at 6 AM (configurable via `WEEKLY_REVIEW_HOUR` env var), Job Squire generates a written review of the past week and saves it to the AI Analysis page.

- **Task type:** Analysis (full pipeline data, larger context needed)
- **Schedule:** Monday at 6 AM local time
- **Model:** Analysis model
- **Estimated cost:** ~$0.05–0.15/review (Sonnet + thinking); free with OpenRouter free models
- **What it covers:** what progressed, what stalled, funnel conversion analysis, one concrete strategy change for the week ahead
- **Email notification:** If SMTP is configured, the review is emailed on Monday morning.

---

## Feature 4: Rejection Pattern Alert

After each scheduled search run, Job Squire counts how many jobs moved to Rejected or Ghosted in the past 14 days. When that count hits your configured threshold (default: 5), it runs a deep-dive analysis.

- **Task type:** Analysis (full pipeline context)
- **Threshold:** Configurable in Settings → AI (default: 5 rejections in 14 days)
- **Cooldown:** Runs at most once per 7-day period
- **Model:** Analysis model; uses Anthropic thinking mode if Anthropic is handling the task
- **Estimated cost:** ~$0.05–0.15/alert; free with OpenRouter free models
- **What it produces:** rejection-stage clustering, patterns across rejected vs. progressed roles, concrete actions
- **Email notification:** If SMTP is configured, the analysis is emailed to you.

---

## Feature 5: ATS Gap Analysis

Available as an on-demand action from any job detail page — not a background feature. Click **ATS Gap Analysis** to run a keyword gap check against your profile, with specific suggestions and an estimated match percentage. Results are saved to the job record.

---

## Analysis model vs. Triage model

**Triage model** — used for Features 1 and 2. Small prompts, high frequency. A fast, inexpensive model (e.g. Haiku, Gemini Flash Lite, Llama 3.1 8B) is ideal.

**Analysis model** — used for Features 3 and 4. Full pipeline data, needs at least 16K context window. A more capable model (e.g. Sonnet, Gemini Flash, Llama 3.3 70B) produces better results.

Each provider in Settings has separate Analysis model and Triage model fields. If the triage model is blank, the analysis model is used for all tasks.

---

## Thinking mode (Anthropic only)

When Anthropic handles analysis tasks, you can enable extended thinking in Settings → AI. Options are disabled, low, medium, and high. Higher thinking levels produce more thorough analysis at greater API cost. This setting has no effect on other providers.

---

## Troubleshooting

**Tasks not running** — verify Automatic Features is checked and the container is healthy (`docker compose ps`). Check the logs (web, worker, and mcp all share one container): `docker compose logs`.

**Provider errors** — use the **Test** button on a provider row in Settings → AI to verify connectivity. Check the provider's dashboard for quota usage.

**Rate limits on free tiers** — add a second provider as backup. The Job Squire will automatically fall through to the next provider in the chain.

See [Setting Up AI](10-ai-setup.md) and [Troubleshooting](12-troubleshooting.md) for more detail.
