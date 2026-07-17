# JobSquire — Documentation

This folder is the full documentation set for the self-hosted JobSquire. It is written so a
future session (or another person) can understand, operate, and extend the project without
re-reading all the source first.

## Document map

| File | What it covers |
|---|---|
| [architecture.md](architecture.md) | System design: the single container's three internal processes, request flow, data model, security model, how the pieces fit together. |
| [API-Reference.md](API-Reference.md) | The REST ingest endpoint (`POST /api/ingest`) and the full MCP tool reference: every tool's signature, parameters, and example calls. |
| [code-reference.md](code-reference.md) | Module-by-module guide to the Python source: every file in `app/`, the models, the routes, and the key functions. Start here to change behavior. |
| [configuration.md](configuration.md) | Every environment variable and every in-app setting (search providers, SMTP, AI settings, automated features, MCP). |
| [job-squire-cli.md](job-squire-cli.md) | The `job-squire` CLI itself: package layout, full command grammar, versioning. The reference for exactly what each command does. |
| [deployment.md](deployment.md) | Operator's runbook for the CLI: instance lifecycle, updating/rollback, reverse-proxy and DNS/TLS provisioning for network mode, password reset, `SECRET_KEY` rotation. |
| [backup-restore.md](backup-restore.md) | The CLI's passphrase-encrypted backup/restore archive: what's inside, how it's encrypted, the restore procedure and verification checklist. |
| [multi-instance.md](multi-instance.md) | Running more than one independent instance on the same host (e.g. one per job seeker): the instance model, the cross-platform registry, per-instance isolation. |
| [mcp-connector.md](mcp-connector.md) | The MCP server: its 23 tools (17 core + 6 routine-support), the OAuth auth flow, and how to connect it in Claude or another MCP-capable agent. |
| [mcp-setup-guide.md](mcp-setup-guide.md) | Developer-focused MCP setup guide: all three connection methods (Claude Pro OAuth, Hermes Agent, OpenClaw), full tool listing, and ready-to-use config blocks. |
| [Setup-Guide.md](Setup-Guide.md) | The narrative, non-technical walkthrough: the one-line bootstrap, creating an instance, in-app configuration, and the three deployment modes. Start here if you're setting Job Squire up for the first time. |
| [troubleshooting.md](troubleshooting.md) | Every real issue hit during build and deploy, with the cause and the fix. Check here first when something breaks. |

The top-level [`../README.md`](../README.md) is the quick-start and feature overview. The end-user
guide for the job seeker is [`Job_Squire_User_Guide.md`](Job_Squire_User_Guide.md), which serves
as the index for the full wiki in [`wiki/`](wiki/).

**This `docs/` folder (plus [`wiki/`](wiki/)) is the single canonical documentation set.** There is
no separate GitHub Wiki in use — everything lives in this repo so it stays versioned with the code.

## One-paragraph summary

JobSquire is a Flask + SQLite web app, packaged as a single multi-architecture Docker image and
deployed, updated, and backed up through the `job-squire` CLI — one command bootstraps the CLI,
which then creates, starts, and manages one or more independent instances, each running the web
app, the scheduler, and the MCP server as three s6-supervised processes inside one container. It
tracks job applications and interview debriefs for two users, automatically searches eight job
boards on a schedule and emails new matches, generates tailored application documents, and
integrates with AI through two independent paths: Automatic Features (ranked provider chain for
one-click analysis and background automation) and an MCP Connector (remote server for live
read/write by Claude Pro, Hermes Agent, or OpenClaw). All persistent data lives in one SQLite file
under the instance's own data directory.

## Tech stack

- **Python 3.12** (the LinuxServer Alpine base image's stock version), **Flask 3** (app factory
  pattern), **Flask-SQLAlchemy**, **Flask-Login**, **Flask-WTF** (CSRF), **Flask-Limiter** (login
  throttle).
- **SQLite** with WAL mode (one file per instance, shared by all three internal processes).
- **gunicorn** (web), **APScheduler** (worker), **uvicorn + MCP SDK / FastMCP** (MCP server), all
  three supervised by **s6-overlay** as PID 1.
- **cryptography** (Fernet) for encrypting stored secrets; also backs the CLI's own Argon2id +
  AES-256-GCM backup encryption.
- **requests** for outbound job-board and AI provider API calls.
- **Click** for the `job-squire` CLI (`job_squire_cli/`, a separate installable package in this
  repo — see [job-squire-cli.md](job-squire-cli.md)).
- Front end is server-rendered Jinja2 + one static CSS file + one static JS file (no framework).

## Where things live

- App source: `app/`. CLI source: `job_squire_cli/`.
- Each instance the CLI creates is a self-contained directory, `~/job-squire/<instance-name>/` by
  default: a generated `docker-compose.yml`, a compose-level `.env`, and `data/` (the SQLite
  DB, uploads, `candidate_profile.md`, the OAuth token store, and the instance's own `.env` with
  its `SECRET_KEY`). See [multi-instance.md](multi-instance.md).
- The CLI's own per-user state (the instance registry, MCP endpoint/token config) lives at the
  conventional per-OS config location — see [job-squire-cli.md](job-squire-cli.md).
- `examples/nginx/` — sample reverse-proxy configs, for anyone wiring up a proxy by hand instead of
  via `job-squire proxy`.
