<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="app/static/JobSquire-Logo-DarkTheme.png">
    <source media="(prefers-color-scheme: light)" srcset="app/static/JobSquire-Logo-LightTheme.png">
    <img src="app/static/JobSquire-Logo-DarkTheme.png" alt="JobSquire" width="500">
  </picture>
</p>

# JobSquire

[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL%203.0-blue.svg)](LICENSE.md)
[![Python 3.14](https://img.shields.io/badge/python-3.14-blue.svg)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/docker-ghcr.io%2Fdellipse%2Fjob--squire-blue)](https://github.com/dellipse/job-squire/pkgs/container/job-squire)

A self-hosted, two-user job-search assistant built with Flask + SQLite, packaged as a single multi-architecture Docker image and deployed through the `job-squire` CLI. It both **finds** new jobs automatically and **tracks** applications from first contact to offer. Claude integrates three ways: manual copy/paste, a direct Anthropic API call, or a live MCP connector.

> **Two-user design.** The app is built for exactly two trusted accounts: one admin (the operator) and one user (the job seeker). There is no public registration and it is not hardened for multi-tenant use. Keep it behind TLS and a reverse proxy.

**Full documentation is in [`docs/`](docs/README.md):** architecture, code reference, configuration, deployment runbook, MCP connector, and troubleshooting.

---

## Features

**Automated job search** runs on a configurable schedule (default: 8am, 1pm, 5pm weekdays in the search location's local time) across multiple job boards. Direct sources available in Settings → Sources — no separate setup beyond an API key: The Muse (no key), Jobicy (no key), ZipRecruiter, Google Jobs via SerpApi (free tier, 250 searches/month), Adzuna, Jooble, and USAJOBS. Google Jobs aggregates Indeed, LinkedIn, and hundreds of other boards in a single call. New postings are deduplicated and dropped into Job Squire as `Saved`; a digest email goes to the job seeker when anything new is found.

**Application tracking** follows a full hiring funnel:

`Saved > Applied > Phone Screen > Interview > Final Interview > Offer > Hired`

plus terminal states: `Rejected`, `Withdrawn`, `Ghosted`, `Pass`.

Other tracking features: interview debriefs (questions asked, self-rating, notes), per-job follow-up reminders, recruiter/contact log with submission tracking, file attachments per job, a timestamped activity log, and CSV export.

**AI integration** has three independent paths, all configurable in Settings → AI:

- **Manual** -- export JSON, analyze in any Claude, paste the result back. No setup required.
- **Automatic Features** -- configure one or more AI providers (Google Gemini, Groq, OpenRouter, Ollama, Mistral, OpenAI, Anthropic, and others); the app calls them directly on a schedule. Enables auto-triage after every search, daily follow-up drafts, and a weekly strategy review. Free tiers from Gemini, Groq, and OpenRouter are sufficient for typical use.
- **MCP connector** -- expose Job Squire as a custom connector. Any MCP-capable agent can read and write live: Claude Pro (via OAuth, no API key needed), Hermes Agent (static key, local agent loop), or OpenClaw (self-hosted gateway for chat-app access via Telegram, WhatsApp, etc.).

Automatic Features and the MCP connector are independent toggles — both can be active at the same time.

---

## Installation

One command installs the `job-squire` CLI, which then drives everything else — creating an
instance, starting it, and bringing it up in your browser:

```bash
# macOS or Linux
curl -fsSL https://raw.githubusercontent.com/dellipse/job-squire/main/bootstrap.sh | sh
```

```powershell
# Windows (PowerShell)
irm https://raw.githubusercontent.com/dellipse/job-squire/main/bootstrap.ps1 | iex
```

The CLI detects an existing container runtime (Docker, Podman, OrbStack, Colima) and reuses it; if
none is found, it offers to install Podman (free for any use, including commercial) with your
consent. There's no separate installer beyond this — `job-squire create`, `update`, `remove`, and
the rest are all subcommands of the same tool.

For the full guided walkthrough — written for a non-technical, first-time setup — see
[`docs/Setup-Guide.md`](docs/Setup-Guide.md). For the complete command reference and network-mode
runbook, see [`docs/deployment.md`](docs/deployment.md).

---

## Prerequisites

- Nothing you need to install yourself first — the bootstrap script above lands the CLI, and the
  CLI handles the container runtime.
- Free API keys for one or more job sources (Adzuna + Jooble recommended as a starting pair)
- Optional: a free AI provider API key (Gemini, Groq, OpenRouter, etc.) or an Anthropic API key for API mode; Claude Pro for MCP mode
- Optional, for network mode only: a domain name (a free DuckDNS subdomain works) — see [`docs/deployment.md`](docs/deployment.md#network-mode-the-reverse-proxy)

---

## Quick Start

```bash
# 1. Install the CLI
curl -fsSL https://raw.githubusercontent.com/dellipse/job-squire/main/bootstrap.sh | sh

# 2. Create an instance (interactive: name, mode, admin password, ...)
job-squire create

# 3. Check it's up
job-squire status <name>
```

`create` prints the address to open once it's done — for local mode (the default, and what almost
everyone wants), something like `http://localhost:8080`. Sign in with `admin` and the password you
set (or the one it generated for you).

---

## Configuration

Configuration is split into two layers.

**Environment variables** live in the instance's `data/.env` and are loaded by all three internal processes at container startup. The only required variables are `SECRET_KEY` and `ADMIN_PASSWORD` — `job-squire create` sets both for you. See [`docs/configuration.md`](docs/configuration.md) for the full reference.

Key variables:

| Variable | Purpose |
|---|---|
| `SECRET_KEY` | Signs sessions and derives the Fernet encryption key for all stored secrets. Changing it invalidates saved API keys and passwords. |
| `ADMIN_PASSWORD` | Password for the admin account. Avoid `$` characters (or escape as `$$`). |
| `USER_PASSWORD` | Optional. Creates a second job-seeker account. Omit to run with admin only. |
| `SESSION_COOKIE_SECURE` | `true` behind HTTPS/SWAG; `false` for plain-HTTP local dev. |
| `PUBLIC_URL` | Base URL used in notification emails (e.g. `https://squire.yourdomain.com`). |
| `PUBLIC_MCP_URL` | Base URL for the MCP connector (enables MCP mode). |
| `SCHEDULE_WEEKDAY_HOURS` | Hours to run the search on weekdays (default `8,13,17`). |
| `INGEST_API_KEY` | Enables the `POST /api/ingest` endpoint. Leave blank to disable. |

**In-app settings** are entered on the Settings page after first login and stored encrypted in the database: job-source API keys, SMTP credentials, the Anthropic API key, search targets (job titles, location, radius), and the candidate profile.

---

## Network Mode (reachable over the internet)

Local mode (the default) is loopback-only — nothing beyond the one machine can reach it. To put an
instance on a server behind a domain name and HTTPS instead:

```bash
job-squire create --mode network --hostname squire.yourdomain.com
job-squire proxy squire                # detects an existing proxy, or installs SWAG
job-squire dns duckdns squire --subdomain squire --token <your-duckdns-token>
```

SWAG (bundled nginx + certbot + fail2ban) terminates TLS and issues Let's Encrypt certificates
automatically. See [`docs/deployment.md`](docs/deployment.md#network-mode-the-reverse-proxy) for
the complete runbook, including using a domain you already have on Cloudflare instead of DuckDNS.

To run more than one instance on the same host (e.g. one per job seeker), see [`docs/multi-instance.md`](docs/multi-instance.md).

---

## Local Dev (no Docker, for contributors working on the source)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export SECRET_KEY=dev \
       ADMIN_PASSWORD=devpass \
       USER_PASSWORD=devpass \
       DATA_DIR=./data \
       SESSION_COOKIE_SECURE=false

python wsgi.py   # http://localhost:8000
```

---

## Turning on the Automated Search

1. Sign in and open **Settings > Sources**. Add API keys for at least one provider (Adzuna + Jooble is the recommended starting pair). Tick "Use this source" and save.
2. On the **Search** tab, set your target job titles and location. Location must be `City, ST` (e.g. `Austin, TX`).
3. On the **Email** tab, configure SMTP so the job seeker receives digest emails.
4. Click **Run search now** to test. New roles appear under the `Saved` status.

---

## AI Workflows

### Application Kit

Open any job and click **Application kit**. Download the generated Markdown file (it contains the candidate profile, the job posting, and a full step-by-step prompt) and paste it into Claude. Claude runs a fit assessment, researches the company and salary, does an ATS keyword pass, then returns a tailored resume, cover letter, application and follow-up emails, interview questions, and LinkedIn outreach.

In MCP mode, Claude can save the finished kit back to the job and set a follow-up reminder automatically.

### Pipeline Analysis

The **AI analysis** tab shows up to three options depending on what's configured — all can be active at once:

- **Manual** -- always available. Copy the provided prompt, attach the JSON export, paste the structured JSON result back.
- **Analyze now** -- shown when Automatic Features is enabled. Calls your configured AI providers in rank order and applies the result in one step. Thinking mode available for Anthropic.
- **Open in Claude** -- shown when the MCP connector is active. Claude reads the live pipeline and writes analysis back. The AI tab also displays five routine prompts (morning briefing, job triage, kit queue, follow-up drafts, weekly review) for use with Claude's scheduled tasks feature.

### MCP Connector Setup

1. Ensure `PUBLIC_MCP_URL` is set in the instance's `data/.env` (local mode's default already points at the loopback MCP port; network mode's is set when you run `job-squire create --mode network`).
2. On **Settings → AI → MCP Connector**, enable the connector and note the connector URL and name.

**Claude Pro (OAuth):** In Claude, go to Settings → Connectors → Add custom connector. Paste the connector URL, give it the exact name shown in Settings, and authorize. **Open in Claude** buttons appear throughout the UI. No Anthropic API key required.

**Hermes Agent or other tools (static key):** Click **Generate static API key** in Settings → AI → MCP Connector. Configure the tool to send `Authorization: Bearer <key>` to your `PUBLIC_MCP_URL`. The Automatic Features toggle and the MCP connector are independent — both can be enabled at the same time.

The MCP server exposes 23 tools for reads and writes. See [`docs/mcp-connector.md`](docs/mcp-connector.md) for the full list.

---

## Backups

```bash
job-squire backup <name>
```

Writes a single passphrase-encrypted archive of the instance to your home folder. See
[`docs/backup-restore.md`](docs/backup-restore.md) for what's inside and the restore procedure.

---

## Resetting a Password

Set the new value in the instance's `data/.env`, add `RESET_UIDS_AND_PWDS_ON_START=true`, run
`job-squire restart <name>`, confirm login, then remove the flag and restart again.

---

## Repository Layout

```
job-squire/
  app/
    __init__.py       App factory, DB init, cross-process locking, migrations, seeding
    models.py         All SQLAlchemy models
    main.py           All UI + API routes
    auth.py           Login / logout blueprint
    ai.py             AI logic: payload, API calls, auto-triage, follow-up drafts, weekly review
    mcp_server.py     Remote MCP server (OAuth 2.0/PKCE, 23 tools)
    worker.py         APScheduler process
    providers.py      Job-board adapters: The Muse, Jobicy, ZipRecruiter, Google Jobs (SerpApi), Adzuna, Jooble, USAJOBS
    search.py         Search orchestration: dedup, ingest, cooldowns, email trigger
    notify.py         SMTP email: search digests, follow-up digests, weekly review
    prompts.py        Claude prompt templates for all five routines
    forms.py          WTForms definitions
    crypto.py         Fernet encryption for stored secrets
    timezones.py      "City, ST" to IANA timezone lookup for the scheduler
    extensions.py     Shared Flask extension singletons
    templates/        Jinja2 templates
    static/           CSS and JS (one file each; no inline JS -- CSP enforced)
  job_squire_cli/            The job-squire deployment CLI (separate installable package)
    job_squire_cli/
      cli.py                  Top-level command group
      ops/                    Lifecycle, registry, runtime, proxy, DNS, backup/restore, ...
      query/                  MCP query commands (health, list, pipeline, contacts, ...)
  wsgi.py
  Dockerfile
  docker-compose.single.yml   Generated per instance by `job-squire create`; also usable directly
  bootstrap.sh / bootstrap.ps1  The one-line CLI installer
  requirements.txt
  examples/
    .env.example              Template for an instance's data/.env
    nginx/                    Sample SWAG/nginx proxy-conf files
  docs/                       Full documentation
```

---

## Acknowledgements

Special thanks to C. Andrews, whose job search was the inspiration and proving ground for JobSquire. The features, workflows, and AI routines in this application were shaped by real-world use — and by the patience of someone willing to be the first guinea pig. This one's for you.

---

## License

[AGPL-3.0](LICENSE.md)
