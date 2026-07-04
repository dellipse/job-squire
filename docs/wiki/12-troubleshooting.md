# Troubleshooting

## No new jobs after a search run

Check the **History** tab in Settings. If a source shows an error, the most common cause is a mistyped or expired API key — re-paste it and try again using the **Test connection** button.

A run that finds zero new jobs is normal once your sources have already pulled in what is currently posted. The Job Squire deduplicates, so it will not re-add jobs it has already seen.

## No email digests

Open Settings → **Email** and confirm the SMTP settings are filled in. Use the **Send test email** button to verify. If you are not comfortable with the SMTP setup, ask your admin.

Common SMTP gotcha: the **Username** is the provider's SMTP login, which is not always your account email. For Brevo, use the dedicated SMTP login shown on its SMTP and API page (not your Brevo account email), and the **Password** is the SMTP key, not your account password.

## Forgot your password or need it changed

Your admin can reset it by updating the password in the environment file and running a one-time password reset.

## A job appears twice

The Job Squire deduplicates automatically. If you spot a duplicate, delete the extra from the job page (admin action only) or ask your admin to remove it.

## The MCP connector shows an error

The most common cause is token expiration (tokens last 30 days) or a container restart. To fix it: go to **Settings → Connectors** in your Claude account, remove the JobSquire connector, and add it again following the steps in [Using Claude Pro](13-claude-pro.md). It takes about a minute.

If the error appears immediately after you click Authorize, double-check that you are using your JobSquire username and password, not your Claude account password.

## Routine prompts reference the wrong connector name

Go to **Settings → AI** in Job Squire and make sure the **Connector name** field matches the name you gave the connector in your Claude settings. They must match exactly, including capitalization.

## Auto-triage or auto-followup not running

Check that:
1. **Automatic Features** is enabled in Settings → AI → AI Features.
2. At least one AI provider is configured under **Settings → AI → AI Providers**, or an Anthropic API key is saved.
3. The relevant toggle is enabled (Auto-triage new jobs / Auto-draft follow-ups).
4. The scheduler worker container is running (`docker compose logs job-squire-worker`).

If a provider is configured but the feature still skips, check the worker logs for a message like `auto-triage: enabled but no API key or ranked providers set`. This means the provider row exists but may be disabled — check the Enabled toggle on the provider card.

## An AI provider returns errors or is skipped

When a provider fails with a rate-limit (429), server error (503/529), or timeout, Job Squire automatically tries the next provider in the ranked list. If all fail, the request fails.

Use the **Test** button on the provider row (Settings → AI → AI Providers) to send a one-token prompt and see the raw error or latency. Then check:
1. The API key is correct and the account has quota remaining (check the provider's dashboard).
2. The base URL is correct — for Ollama and custom endpoints only; leave blank for cloud providers.
3. The provider is set to **Enabled** on the Settings → AI providers list.

If you're hitting free-tier per-minute limits, add a second provider as a fallback so overflow requests go there instead of failing.

## ATS gap analysis button not visible

The ATS Gap Analysis button on job pages only appears when **Automatic Features** is enabled and either an AI provider or an Anthropic API key is configured under Settings → AI.

## MCP buttons not visible on job pages

The **Build kit in Claude**, **Score this job**, **Prep for interview**, and **Draft follow-up email** buttons appear when **Claude.ai buttons** is enabled in Settings → AI → AI Features. This is set automatically when you enable the MCP Connector and save a connector name — confirm both are filled in under Settings → AI → MCP Connector.
