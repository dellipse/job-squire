# JobSquire: User Guide

> **For operators:** This guide is written for the job seeker using Job Squire.
> The full wiki lives in `docs/wiki/` — each topic is a separate page.
> This file is rendered in the app's **Guide** tab.

JobSquire is your private job-search command center. It runs on your own server, so your resume, pipeline, and salary expectations stay under your control.

The Job Squire works on two fronts at once. On the discovery side, it searches multiple job boards on a schedule, deduplicates what it finds, and emails you a digest of new postings — so opportunities come to you instead of requiring daily manual searches. On the tracking side, it follows every application through the full hiring funnel, surfaces overdue follow-ups, logs interview debriefs, and keeps a timestamped record of everything that happens.

AI is woven into both sides. Without any setup, you can export your pipeline as JSON and paste it into Claude for analysis. Add a free API key from Google, OpenRouter, or another provider and Job Squire scores new jobs automatically, drafts follow-up emails daily, and sends you a strategic review every Monday morning. Connect a Claude Pro subscription via MCP and Claude can read and write your pipeline live, building tailored application kits and coaching you through interviews without any copying or pasting.

The goal is to run a search that is thorough, consistent, and hard to ghost — and to show up to every interview better prepared than the competition.

---

## Wiki — Topics

### Getting started

- [Quick Start: 15-Minute Setup](wiki/01-quick-start.md) — connect sources, set your search targets, run your first search.
- [How the Job Squire Is Organized](wiki/02-navigation.md) — every page, every status, what the dashboard shows.

### Finding and tracking jobs

- [Connecting Job Sources](wiki/03-job-sources.md) — The Muse and Jobicy (no key required), ZipRecruiter, Google Jobs via SerpApi (free tier, broadest cross-board coverage), plus Adzuna, Jooble, and USAJOBS; metro-specific coverage notes; how automatic searches run.
- [Daily Workflow](wiki/04-daily-workflow.md) — applying, updating status, follow-up reminders, interview debriefs, attachments, CSV export.
- [Recruiters and Staffing Agencies](wiki/05-recruiters.md) — tracking contacts, logging submissions, dashboard surfacing.

### AI and document generation

- [Application Kits](wiki/06-application-kits.md) — what Claude produces (fit check, research, ATS analysis, resume, cover letter, emails, interview prep, LinkedIn outreach).
- [Candidate Profile and Documents](wiki/07-candidate-profile.md) — keeping your master profile current, the document library, generating a profile from uploads.
- [Quick-Apply Bookmarklet](wiki/08-bookmarklet.md) — capturing any posting in one click.
- [Automated AI Features](wiki/09-automated-ai.md) — auto-triage, auto-follow-up drafts, weekly strategy review, ATS gap analysis, rejection pattern alert.
- [Setting Up AI](wiki/10-ai-setup.md) — choosing a provider strategy (Google AI Studio, OpenRouter, local Ollama); adding providers in Settings; per-task assignment and fallback chains.
- [AI Pipeline Analysis](wiki/11-ai-analysis.md) — manual, one-click, and MCP modes; thinking mode; when to run it.

### AI agents

- [Using Claude Pro](wiki/13-claude-pro.md) — connect via MCP with no API key; five scheduled routines; per-job action buttons.
- [Using Hermes Agent](wiki/14-hermes-agent.md) — open-source agent harness; static API key setup; chat loop against your pipeline.
- [Using OpenClaw](wiki/15-openclaw.md) — self-hosted gateway; interact with your pipeline from Telegram, WhatsApp, or any connected chat app.

### Reference

- [Troubleshooting](wiki/12-troubleshooting.md) — common problems and fixes.
- [License](wiki/99-license.md) — AGPLv3 license and source file copyright notice.

---

## Quick reference

**Job statuses (in order):**
`Saved` → `Applied` → `Phone Screen` → `Interview` → `Final Interview` → `Offer` → `Hired`

Plus terminal states: `Rejected`, `Withdrawn`, `Ghosted`, and `Pass`.

**Automatic search schedule:** 8 AM, 1 PM, 5 PM weekdays + one weekend morning (your search location's local time). New matches are emailed to you.

**AI settings** (Settings → AI) are two independent toggles:
- **Automatic Features** — one-click analysis and background automation. Add any AI provider (Gemini, Groq, OpenRouter, Ollama, and others — free tiers available) or an Anthropic API key. Enables auto-triage, auto-follow-up drafts, weekly strategy review, and rejection pattern alert.
- **MCP Connector** — live read/write connector for Claude Pro, Hermes Agent, and OpenClaw. "Open in Claude" buttons appear throughout the app. Five scheduled routine prompts are available in Settings → AI.

Both toggles can be active at the same time. Manual copy/paste analysis is always available without any setup.

**Automatic Features required** for: auto-triage, auto-follow-up drafts, weekly strategy review, rejection pattern alert.

**MCP Connector required** for: Build kit in Claude, Score this job, Prep for interview, Draft follow-up email buttons on job pages, and the five Claude Pro scheduled routines.
