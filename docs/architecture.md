# Architecture

## Containers

The project builds **one Docker image** (`Dockerfile`, `ghcr.io/linuxserver/baseimage-alpine`
base) that runs as **one container** (`docker-compose.single.yml`), generated per instance by the
`job-squire` CLI's `create` command. The same application code runs three logical processes inside
that one container, sharing the same `/data` bind mount and therefore the same SQLite database.

| Process | Command | Port | Role |
|---|---|---|---|
| `web` | `gunicorn ... wsgi:app` (2 workers) | 8000 | The web app (UI + JSON ingest API). Owns first-boot DB init, migrations, and seeding. |
| `worker` | `python -m app.worker` | none | APScheduler process that runs the automated job search on a cron cadence. Exactly one process so each scheduled slot fires once. |
| `mcp` | `python -m app.mcp_server` | 9000 | Remote MCP server (Streamable HTTP) that Claude connects to as a custom connector. |

### Single-container topology

All three processes run inside **one container**, supervised by **s6-overlay as PID 1** on the
LinuxServer Alpine base — not a shell script backgrounding three processes, because that wouldn't
propagate `SIGTERM` correctly and would risk SQLite WAL corruption on every stop/update. `worker`
and `mcp` are s6 longrun services that depend on `web` via s6's `notification-fd` mechanism
(`s6-notifyoncheck` polls `web`'s `/health` in the background while `web` itself stays the
directly-supervised process, so it receives `SIGTERM` straight from s6). A single aggregated
`HEALTHCHECK` (`/etc/s6-overlay/scripts/healthcheck`) passes only when all three internal probes
pass: `web`'s `/health` on 8000, `mcp`'s `/health` on `MCP_PORT`, and the worker's
`.worker_heartbeat` freshness check (the worker has no HTTP endpoint of its own) — this is what
`job-squire status` reads.

The container runs as a non-root user (`abc`) via the LinuxServer `PUID`/`PGID`/`UMASK`
convention — the base image itself must start as root so s6's init can apply that mapping and
drop each service to `abc` itself, which is why `docker-compose.single.yml` deliberately does not
set a compose `user:` directive.

In network mode, an external reverse proxy (SWAG or nginx, provisioned by `job-squire proxy` — see
[`deployment.md`](deployment.md#network-mode-the-reverse-proxy)) terminates TLS and reaches the
container over a shared Docker network by container name:

```
                         ┌─────────────── SWAG (nginx, TLS) ───────────────┐
   Browser  ──HTTPS──►   │ castelo.*      → job-squire-castelo:8000       │
   Claude   ──HTTPS──►   │ mcp-castelo.*  → job-squire-castelo:9000       │
                         └─────────────────────┬───────────────────────────┘
                                               │  (shared Docker network)
                              ┌────────────────┴────────────────┐
                              │   job-squire-castelo (1 container) │
                              │   s6-overlay (PID 1, root)        │
                              │   ├─ web    (gunicorn, non-root)  │
                              │   ├─ worker (APScheduler, ↳ web)  │
                              │   └─ mcp    (uvicorn, ↳ web)      │
                              └────────────────┬────────────────┘
                                               │  bind mount
                    ~/job-squire/castelo/data/          → /data
                                        /data/job-squire.db (SQLite, WAL)
                                        /data/uploads/  /data/candidate_profile.md
```

In local mode there is no proxy at all — the container publishes its two ports straight to
`127.0.0.1` and the browser talks to it directly (see
[`PLAN-deployment-modes.md`](PLAN-deployment-modes.md) Section 5 for why loopback is a safe,
warning-free trust boundary on its own).

A prior three-container topology (`docker-compose.yml`, one process per container) existed during
the migration to this single-container image and has been removed now that the single-container
image is proven in practice (`PLAN-deployment-modes.md` Section 8). Anyone still running that
topology can move onto this one with `job-squire adopt` — see
[`adopt-single-container.md`](adopt-single-container.md).

## Request lifecycle (web)

1. `wsgi.py` calls `create_app()` (the factory in `app/__init__.py`).
2. The factory resolves `DEPLOY_MODE` into the granular `trust_proxy`/`secure_cookie` flags
   (`app/deploy.py`), runs the startup safety guard against them, loads the rest of config from
   env, conditionally wraps the app in `ProxyFix` (trusts one hop of `X-Forwarded-*` — only when
   `trust_proxy` resolved true, never unconditionally), initializes extensions, registers the
   `auth` and `main` blueprints, and adds security headers (including a strict CSP) via an
   `after_request` hook. See [`configuration.md`](configuration.md#web--security) for the guard.
3. On startup it runs `_init_database()` under a cross-process file lock: enables SQLite WAL,
   `create_all()`, applies additive `_run_migrations()` (columns `create_all()` can't add to
   existing tables), then seeds the two users and a blank singleton `SearchConfig`. It also
   registers the `local_dt` Jinja filter (renders naive-UTC timestamps in the search location's
   local time) and `_seed_data_files()` copies the bundled `candidate_profile.md` into `/data`
   on first boot so it can be edited without a rebuild.
4. Per request: Flask-Login enforces auth, Flask-WTF validates CSRF on POSTs, routes in
   `app/main.py` handle the work, Jinja templates render, and `app/static/app.js` wires up
   client behavior (no inline JS, to satisfy the CSP).

## Data model (SQLite tables)

| Model (`app/models.py`) | Table | Purpose |
|---|---|---|
| `User` | `users` | The two accounts (admin + user), hashed passwords. |
| `Job` | `jobs` | One application/opportunity. Has `status`, `external_id` (for dedup), notes, `ai_analysis`, and `kit_output` (the tailored application kit saved back from Claude). |
| `JobNote` | `job_notes` | A timestamped activity-log entry on a job: manual notes plus auto-logged status changes and follow-up changes (`note_type`). |
| `Interview` | `interviews` | A debrief per interview round (questions asked, rating, notes), FK to `jobs`. |
| `Attachment` | `attachments` | Uploaded resume/cover-letter files, FK to `jobs`. Stored on disk under `/data/uploads`. |
| `CandidateAsset` | `candidate_assets` | Master documents not tied to a job (base resume, recommendation letters, certs, portfolio). Exposed to Claude via the MCP `get_candidate_assets` tool and used when building kits. |
| `AIInsight` | `ai_insights` | A global analysis result (summary + recommendations) written by an AI run. |
| `ProviderCredential` | `provider_credentials` | Per job-board provider: enabled flag + encrypted JSON of API keys. |
| `SearchConfig` | `search_config` | Singleton (id=1): titles, location, radius, salary, freshness, results-per-query, enabled. |
| `KitConfig` | `kit_config` | Singleton (id=1): application-kit settings (the `fit_salary_floor` used to flag low-paying roles). |
| `SmtpConfig` | `smtp_config` | Singleton (id=1): SMTP host/port/user/encrypted password/from/to, plus a separate `admin_email` for error alerts. |
| `AIConfig` | `ai_config` | Singleton (id=1): `api_enabled` (Automatic Features on/off), `mcp_enabled` (MCP Connector on/off), `claude_buttons_enabled` (show "Open in Claude" buttons), encrypted Anthropic key, model, thinking mode, the connector name the user gave it. Legacy `mode` column kept for backward compatibility. |
| `AIProviderConfig` | `ai_provider_configs` | One row per AI provider in the ranked fallback chain: provider type, API key (encrypted), base URL, analysis model, triage model, rank, capability flags (`use_for_triage`, `use_for_analysis`), enabled toggle. |
| `AITaskConfig` | `ai_task_configs` | One row per automatic task (triage, followup, weekly_review, rejection_alert): enabled flag, primary provider FK, backup provider FK, and whether to fall through to the ranked chain after primary + backup fail. |
| `SearchRun` | `search_runs` | Audit record of each search execution (found/created/skipped/emailed/status). |
| `Contact` | `contacts` | A recruiter / staffing-agency rep / hiring manager / networking contact, with type, contact details, last-contacted and follow-up dates. |
| `Submission` | `submissions` | "Who submitted User where, and when" — optional FK to `contacts` (the submitter) and an optional FK to `jobs`, plus company/role text and a submission status. |

The master candidate profile (`candidate_profile.md`) and the kit/profile prompt overrides live as
files in `/data`, not in the database, so they survive image rebuilds and can be edited in the UI
(Candidate Profile tab) or via the MCP `save_candidate_profile` tool.

Status funnel (`STATUSES` in `models.py`): `Saved → Applied → Phone Screen → Interview →
Final Interview → Offer → Hired`, plus terminal states `Rejected`, `Withdrawn`, `Ghosted`, `Pass`. `ACTIVE_STATUSES`
marks the live opportunities used in dashboard metrics. Auto-found jobs always land as `Saved`.

Submission funnel (`SUBMISSION_STATUSES`): `Submitted → Screening → Interviewing → Offer →
Placed`, plus `Rejected`, `Withdrawn`, `No Response`. `ACTIVE_SUBMISSION_STATUSES` marks the live
submissions that drive the dashboard "open submissions" count and follow-up nudges. Deleting a
job unlinks its submissions (sets `job_id` to null) rather than deleting them, so the recruiter
relationship history is preserved.

## The two search architectures

The Job Squire can acquire jobs two ways, both deduplicated through the same `ingest_jobs()` path:

- **In-app search (Model B):** the worker calls job-board APIs (The Muse, Jobicy, ZipRecruiter,
  Google Jobs via SerpApi, Adzuna, Jooble, USAJOBS) using keys stored on the Settings page
  (Sources tab), on a schedule. The Muse and Jobicy require no key. Calls are throttled between titles,
  retried with backoff on transient errors, and a provider that returns a 503 outage is put in a
  temporary cooldown (`DATA_DIR/provider_cooldowns.json`). Providers with a `max_runs_per_day`
  credential field (currently Google Jobs/SerpApi) also have daily run counts tracked in
  `DATA_DIR/provider_daily_runs.json`, reset at UTC midnight. See `app/providers.py` and
  `app/search.py`.
- **External push (Model A):** `POST /api/ingest` accepts a batch of jobs with a token
  (`INGEST_API_KEY`). The MCP `add_jobs` tool also funnels into `ingest_jobs()`, so Claude can
  push jobs it found with its own connectors — including Indeed, which publishes an official Claude
  connector and is not available in the automated scheduler.

## AI integration

AI is controlled through two independent toggles on the Settings page, AI tab. Both can be active simultaneously.

**Manual** analysis is always available — the user exports a JSON payload, pastes it into any Claude, and imports the structured reply. No setup required.

**Automatic Features** (`AIConfig.api_enabled`) enables server-side AI calls: one-click pipeline analysis, auto-triage after each search run, daily follow-up drafts, and a weekly strategy review. The app dispatches through `app/ai.py:call_with_fallback()`, which tries providers in the `AIProviderConfig` ranked chain. Per-task assignment is available via `AITaskConfig` (each task can have a primary provider, a backup, and a chain-fallback flag). Anthropic can also be added as a last-resort fallback (`AIConfig.fallback_to_anthropic`). Providers are any OpenAI-compatible endpoint; Anthropic is optional. An optional thinking mode (disabled/low/medium/high) is honored when an Anthropic model is in the chain — maps to the `effort` param on Opus 4.8 (`_ADAPTIVE_MODELS`) or `budget_tokens` on Sonnet/Haiku (`_THINKING_BUDGETS`).

**MCP Connector** (`AIConfig.mcp_enabled`) runs the `app/mcp_server.py` FastMCP server as a custom connector. Any MCP-capable agent (Claude Pro via OAuth, Hermes Agent or OpenClaw via static Bearer key) can read and write Job Squire live. A separate flag, `AIConfig.claude_buttons_enabled`, controls whether "Open in Claude" deep-link buttons appear in the UI — this is set automatically when the MCP connector is enabled and a connector name is saved, but can be toggled independently. Five scheduled routine prompts (morning briefing, job triage, kit queue, follow-up drafts, weekly review) are generated by `app/prompts.py` and shown in the AI tab for use with Claude Pro's scheduled tasks feature.

The legacy `AIConfig.mode` column (`manual`/`api`/`mcp`) is retained for backward compatibility; a startup migration populates `api_enabled`/`mcp_enabled` from it on first boot after upgrade.

## Security model

- Login required for all UI; passwords hashed (Werkzeug).
- CSRF on every form (Flask-WTF); the `/api/ingest` endpoint is CSRF-exempt but token-gated.
- Session cookies: HttpOnly, SameSite=Lax, Secure (mode-aware default, see
  [`configuration.md`](configuration.md#web--security) — always overridable explicitly).
- Login rate limiting (Flask-Limiter, in-memory).
- Strict security headers incl. `Content-Security-Policy: ... script-src 'self'` — this is why
  **all** JavaScript is in `app/static/app.js` and there are no inline handlers.
- Stored secrets (provider API keys, SMTP password, Anthropic key, static MCP token) are
  **encrypted at rest** with Fernet, keyed off a hash of `SECRET_KEY` (`app/crypto.py`). Rotating
  `SECRET_KEY` invalidates stored secrets.
- MCP auth: **OAuth 2.0 Authorization Code flow with PKCE** is the default everywhere (required
  by Claude's connector handshake). The user signs in with their Job Squire credentials on a
  login page the MCP server serves; Claude stores the resulting 30-day Bearer token and sends it
  on every call. Served only over HTTPS, with DNS-rebinding protection (host allowlist) on. A
  static Bearer token (`app/mcp_auth.py`, prefixed `jsq_mcp_`, constant-time compared, optional
  TTL) is the sanctioned escape hatch for headless/non-browser clients that can't complete OAuth's
  browser redirect (scripts, agent harnesses) — generated on the Settings page, loopback-only by
  default, and rejected on a network-reachable (`DEPLOY_MODE=network`) instance unless the
  operator explicitly opts in.
- Runs non-root in the container.

Not designed for public multi-tenant signup: there are exactly two seeded accounts and no
registration route.
