# JobSquire — Documentation

This folder is the full documentation set for the self-hosted JobSquire. It is written so a
future session (or another person) can understand, operate, and extend the project without
re-reading all the source first.

## Document map

| File | What it covers |
|---|---|
| [architecture.md](architecture.md) | System design: the three containers, request flow, data model, security model, how the pieces fit together. |
| [API-Reference.md](API-Reference.md) | The REST ingest endpoint (`POST /api/ingest`) and the full MCP tool reference: every tool's signature, parameters, and example calls. |
| [code-reference.md](code-reference.md) | Module-by-module guide to the Python source: every file in `app/`, the models, the routes, and the key functions. Start here to change behavior. |
| [configuration.md](configuration.md) | Every environment variable and every in-app setting (search providers, SMTP, AI settings, automated features, MCP). |
| [deployment.md](deployment.md) | Deployment runbook: first-time setup, pulling from ghcr.io, updating, and rollback steps. |
| [backup-restore.md](backup-restore.md) | WAL-safe backup and restore runbook: `scripts/backup.sh`/`scripts/restore.sh`, why a plain `tar` of the data folder isn't safe, and a post-restore verification checklist. |
| [multi-instance.md](multi-instance.md) | Running more than one independent instance on the same host (e.g. one per job seeker): directory layout, port configuration, SWAG setup, and per-instance MCP connectors. |
| [mcp-connector.md](mcp-connector.md) | The MCP server: its 23 tools (17 core + 6 routine-support), the OAuth auth flow, and how to connect it in Claude or another MCP-capable agent. |
| [mcp-setup-guide.md](mcp-setup-guide.md) | Developer-focused MCP setup guide: all three connection methods (Claude Pro OAuth, Hermes Agent, OpenClaw), full tool listing, and ready-to-use config blocks. |
| [Setup-Guide.md](Setup-Guide.md) | Step-by-step in-app configuration: search targets, job sources, SMTP, candidate profile, AI, and MCP connector. |
| [troubleshooting.md](troubleshooting.md) | Every real issue hit during build and deploy, with the cause and the fix. Check here first when something breaks. |

The top-level [`../README.md`](../README.md) is the quick-start and feature overview. The end-user
guide for the job seeker is [`Job_Squire_User_Guide.md`](Job_Squire_User_Guide.md), which serves
as the index for the full wiki in [`wiki/`](wiki/).

**This `docs/` folder (plus [`wiki/`](wiki/)) is the single canonical documentation set.** There is
no separate GitHub Wiki in use — everything lives in this repo so it stays versioned with the code.

## One-paragraph summary

JobSquire is a Flask + SQLite web app, packaged as a Docker image and run as three
containers (web, scheduler worker, MCP server) behind a SWAG reverse proxy. It tracks job
applications and interview debriefs for two users, automatically searches eight job boards on a
schedule and emails new matches, generates tailored application documents, and integrates with
AI through two independent paths: Automatic Features (ranked provider chain for one-click
analysis and background automation) and an MCP Connector (remote server for live read/write by
Claude Pro, Hermes Agent, or OpenClaw). All persistent data lives in one SQLite file on a
Docker volume.

## Tech stack

- **Python 3.14**, **Flask 3** (app factory pattern), **Flask-SQLAlchemy**, **Flask-Login**,
  **Flask-WTF** (CSRF), **Flask-Limiter** (login throttle).
- **SQLite** with WAL mode (one file, shared by all three containers).
- **gunicorn** (web), **APScheduler** (worker), **uvicorn + MCP SDK / FastMCP** (MCP server).
- **cryptography** (Fernet) for encrypting stored secrets.
- **requests** for outbound job-board and AI provider API calls.
- Front end is server-rendered Jinja2 + one static CSS file + one static JS file (no framework).

## Where things live (on the host)

- Source / build context + Job Squire compose: `job-squire/` (incl.
  `docker-compose.yml` and `examples/`)
- Persistent data: a **host bind mount** at `./job-squire/data` (`DATA_HOST_DIR`),
  mounted into each container at `/data` (the SQLite DB, uploads, `candidate_profile.md`, init lock, OAuth tokens).
- nginx/SWAG proxy configs: `job-squire/examples/nginx/` — copy these to your proxy's conf directory.
