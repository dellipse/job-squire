# AI Pipeline Analysis

The **AI analysis** page lets you review your whole search for patterns. Claude identifies where applications stall, which sources convert, and concrete steps to improve.

The AI analysis page shows up to three options depending on your settings — they are independent and can all be active at once.

## Manual (always available)

Works with any Claude plan — no API key or connector required.

1. On the AI analysis page, click **Download pipeline JSON** to get the export file.
2. Open [claude.ai](https://claude.ai), start a new chat, and paste the provided prompt followed by the JSON file.
3. Claude returns a JSON object. Paste it back into the **Import analysis** section on the same page.
4. The Job Squire applies the result: per-job analysis notes and a global summary are saved.

Use this when you want to run an analysis occasionally without setting up anything.

## One-click analysis (Automatic Features)

A one-click **Analyze now** button sends your pipeline to an AI provider directly. Shown when **Automatic Features** is enabled in Settings → AI → AI Features.

Requires at least one AI provider configured in Settings → AI → AI Providers. Click **Analyze now** and Job Squire tries your configured providers in rank order, applies the analysis, and writes the results back in one step.

**Thinking mode** is available when Anthropic is in your provider chain. Set it on the Anthropic provider row (disabled, low, medium, or high). Higher levels are more thorough but cost more. Other providers ignore this setting.

Billing depends on the provider. Free tiers from Gemini, Groq, and OpenRouter can cover this use case at no cost. See [Setting Up AI](10-ai-setup.md) for setup.

## Open in Claude (Claude.ai buttons)

An **Open in Claude to analyze** button opens a pre-loaded Claude chat. Claude reads your pipeline through the JobSquire connector and writes the analysis back automatically. No downloading or importing needed.

Shown when **Claude.ai buttons** is enabled in Settings → AI → AI Features. Requires the MCP connector to be connected in your Claude account — see [Using Claude Pro](13-claude-pro.md). Does not require a paid Anthropic API key, but does require a Claude Pro subscription.

## What the analysis covers

Every analysis returns:

- A global summary of patterns in your pipeline.
- Concrete, specific recommendations (not generic advice).
- Per-job analysis notes saved to each job record.

The analysis looks at where applications stall in the funnel, which roles or sources lead to interviews, and what interview debriefs reveal about patterns in how you are performing.

## When to run it

Run an analysis every couple of weeks, or any time you feel like your search is stalling and you want an outside read on what to change. The weekly strategy review (if enabled under Automatic Features — see [Automated AI Features](09-automated-ai.md)) automates this on Mondays.
