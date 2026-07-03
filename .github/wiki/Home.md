# JobSquire Wiki

A self-hosted, two-user job-search companion built with Flask + SQLite, packaged as a Docker image run as three containers. It finds new jobs automatically on a schedule, tracks applications through a full hiring funnel, and integrates with Claude three ways: manual copy/paste, a direct Anthropic API call, or a live MCP connector.

---

## Quick Links

| Page | What it covers |
|---|---|
| [Setup Guide](Setup-Guide) | First-time install, Docker, SWAG proxy, in-app configuration |
| [API Reference](API-Reference) | MCP connector tools (22) and the ingest API endpoint |
| [FAQ](FAQ) | Troubleshooting: containers, email, search providers, AI modes |

The developer docs in `docs/` cover architecture, the full code reference, configuration, the deployment runbook, and more. Start there when you need to change behavior.

---

## How It Works

The project builds one Docker image and runs it as three containers:

| Container | Role | Port |
|---|---|---|
| `job-squire` | Web app (UI + ingest API) | 8000 (published 8080) |
| `job-squire-worker` | Automated job search on a cron schedule | -- |
| `job-squire-mcp` | Remote MCP server -- Claude's custom connector | 9000 |

All three containers share one SQLite database via a host bind-mount. SWAG (or any nginx-based proxy) terminates TLS and routes traffic.

---

## Key Concepts

**Status funnel.** Every job moves through a defined set of statuses:

`Saved > Applied > Phone Screen > Interview > Final Interview > Offer > Hired`

plus terminal states: `Rejected`, `Withdrawn`, `Ghosted`, `Pass`.

Jobs found by the automated search arrive as `Saved`. Everything else is manually advanced.

**Two accounts.** The app seeds exactly two accounts from env vars: `admin` (the operator) and `user` (the job seeker). There is no registration page.

**Secrets are encrypted at rest.** Job-board API keys, the SMTP password, and any Anthropic API key are encrypted with Fernet before being stored. The encryption key is derived from `SECRET_KEY`. Changing `SECRET_KEY` invalidates all saved secrets -- you will need to re-enter them.

**AI mode.** Set on the Settings page (AI tab). Three options:
- **Manual** -- export/import JSON. No API key required.
- **API** -- the app calls your configured AI providers (Gemini, Groq, OpenRouter, Ollama, Anthropic, and others) directly. Free tiers available; no Anthropic key required. Enables automated routines (auto-triage, follow-up drafts, weekly review).
- **MCP** -- Job Squire is exposed as a custom Claude connector via OAuth 2.0/PKCE.

**Ranked AI providers.** In API mode, you can configure multiple providers in a priority list. The app tries each in order, falling back to the next on a rate-limit or server error. Add providers under Settings → AI → AI Providers.


---

## Job Source Providers

| Provider | Coverage | Free keys |
|---|---|---|
| Adzuna | US + international | [developer.adzuna.com](https://developer.adzuna.com/) |
| Jooble | US + international | [jooble.org/api/about](https://jooble.org/api/about) |
| USAJOBS | US federal government | [developer.usajobs.gov](https://developer.usajobs.gov/APIRequest/) |
| The Muse | Tech / culture-focused | [themuse.com/developers](https://www.themuse.com/developers/api/v2) |

LinkedIn and Monster block automated access and are intentionally not available.

---

## Security Notes

- Login is required for all UI routes and the ingest API.
- CSRF protection is on every form (Flask-WTF).
- Session cookies are HttpOnly, SameSite=Lax, and Secure (configure `SESSION_COOKIE_SECURE`).
- A strict Content-Security-Policy blocks all inline JavaScript. All client JS lives in `app/static/app.js`.
- The app runs as a non-root user in the container (UID/GID set via `PUID`/`PGID` build args).
- The ingest API (`POST /api/ingest`) is disabled unless `INGEST_API_KEY` is set.
- The MCP server uses OAuth 2.0 Authorization Code with PKCE. Tokens are stored in `DATA_DIR/oauth_tokens.json` and are valid for 30 days.
