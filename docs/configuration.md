# Configuration

Two layers: **environment variables** (set in `data/.env`, read at container start) and **in-app
settings** (entered on the Settings page, stored encrypted in the database).

## Environment variables (`data/.env`)

For an instance created by the `job-squire` CLI, this lives at
`~/job-squire/<instance-name>/data/.env` (the per-instance directory `job-squire create` generates
— see [`multi-instance.md`](multi-instance.md)) and is read at container start. Editing it directly
is fine; restart the instance afterward with `job-squire restart <name>` (or
`docker compose`/`podman compose` directly from that directory) to pick up the change.

> **Gotcha:** Docker Compose interpolates `$` in `.env` values. A literal `$` must be escaped as
> `$$`, or avoided. A `$` in a password silently truncated it during deployment once.

### Required

| Variable | Purpose |
|---|---|
| `SECRET_KEY` | Signs sessions **and** derives the encryption key for all stored secrets. Generate with `python -c "import secrets; print(secrets.token_hex(32))"`. Changing it invalidates saved provider/SMTP/Anthropic secrets. |
| `ADMIN_PASSWORD` | Password for the admin account. |

### Optional user account

If you only need one login, you can run the app with just the admin account. The separate user account is created only when `USER_PASSWORD` is set.

| Variable | Purpose |
|---|---|
| `USER_PASSWORD` | Password for the optional user account. Omit entirely to run admin-only. |

### Accounts (optional, have defaults)

| Variable | Default | Purpose |
|---|---|---|
| `ADMIN_USERNAME` | `admin` | Admin login (lowercased). |
| `ADMIN_NAME` | `Admin` | Display name. |
| `USER_USERNAME` | `user` | User login. |
| `USER_NAME` | `User` | Display name. |
| `RESET_UIDS_AND_PWDS_ON_START` | `false` | Set `true` for **one** boot to reset both usernames, display names, and passwords to the `data/.env` values, then remove it. |

### Web / security

| Variable | Default | Purpose |
|---|---|---|
| `DEPLOY_MODE` | `local` | `local` or `network`. A convenience preset over `TRUST_PROXY` and `SESSION_COOKIE_SECURE` below — the running app never branches on this string itself, only on the two flags it fills in when they're not set explicitly. `local` assumes this instance is reachable only via loopback with no reverse proxy in front; `network` assumes an external TLS-terminating reverse proxy sits in front of it. |
| `TRUST_PROXY` | mode-dependent (`0` for `local`, `1` for `network`) | Trust one hop of `X-Forwarded-For/Proto/Host` from a reverse proxy (`werkzeug.middleware.proxy_fix.ProxyFix`), for correct client IPs (rate limiting) and request scheme. Leave unset to take `DEPLOY_MODE`'s default; an explicit value here always wins. Must stay `0` if nothing actually sits in front of the app on this network path — otherwise those headers can be spoofed by anything that reaches the app directly. |
| `SESSION_COOKIE_SECURE` | mode-dependent (`false` for `local`, `true` for `network`) | Keep `true` behind HTTPS/SWAG. `false` only for plain-HTTP local testing. Leave unset to take `DEPLOY_MODE`'s default; an explicit value here always wins over the preset, so an existing install that already sets this keeps its current behavior regardless of `DEPLOY_MODE`. |
| `SESSION_COOKIE_NAME` | derived from `INSTANCE_NAME` (e.g. `castelo_session`) | Not mode-dependent — the derivation is the same in either mode. Prevents instances that share a registrable domain from clobbering each other's sessions and CSRF tokens. Override for full manual control. |
| `SESSION_DAYS` | `7` | Session lifetime. |
| `CSRF_TIME_LIMIT` | `14400` | CSRF token lifetime in seconds (4h). Set `0` to disable expiry for very long-lived forms. |
| `PUBLIC_URL` | — | Public base URL, used in notification emails and consulted by the startup safety guard below. |
| `DATA_DIR` | `/data` | Where the SQLite DB + uploads live (the volume mount). |
| `MAX_UPLOAD_MB` | `10` | Attachment size limit. |
| `DATABASE_URL` | sqlite in `DATA_DIR` | Override the DB URI if ever moving off SQLite. |
| `ALLOW_INSECURE` | `false` | Dev only: allows a default secret/passwords. Never set in production. |

#### Startup safety guard

At boot, the app validates `DEPLOY_MODE`, `PUBLIC_URL`, and `TRUST_PROXY` together and turns two
known-unsafe combinations into an early, explicit signal rather than a silent misconfiguration:

- **Fatal — refuses to start, exits non-zero:** `DEPLOY_MODE=network` but `PUBLIC_URL` isn't an
  `https://` URL, or `TRUST_PROXY` resolves off. Written to the log and printed as a
  `FATAL:`-prefixed line on stderr — a stable shape the `job-squire` CLI catches and reprints
  verbatim on `create`/`start`/`restart`, rather than a generic "container exited" message.
- **Warning — starts, shows a persistent in-app banner:** `DEPLOY_MODE=local` but `PUBLIC_URL` is
  set to a non-loopback host. This is a self-consistency check between two declared values, not a
  live network probe — the app has no way to observe its own container's actual host-level
  network exposure (Docker always binds `0.0.0.0` internally regardless of how the host publishes
  that port). The banner clears on the next boot once the underlying variable is fixed.

Every message names the offending variable, its value, why it's unsafe, and the fix. See
`app/deploy.py` and [`PLAN-deployment-modes.md`](PLAN-deployment-modes.md) Section 3 for the full
precedence rules and guard logic, and
[`adopt-single-container.md`](adopt-single-container.md) if you're moving an existing install onto
the single-container image and want to know exactly which of these to set.

### Scheduler (worker)

The schedule fires in the **job-search location's** local time (derived from the location set in
the app, e.g. `Columbus, OH` → Eastern), **not** the server clock. The server's own `TZ` is
ignored for scheduling.

| Variable | Default | Purpose |
|---|---|---|
| `SCHEDULE_TZ` | — | Force a specific IANA zone (e.g. `America/New_York`). Blank = auto-derive from the search location. |
| `SCHEDULE_WEEKDAY_HOURS` | `8,13,17` | Cron hours Mon–Fri (8am, 1pm, 5pm local). |
| `SCHEDULE_WEEKEND_HOURS` | `9` | Cron hours Sat–Sun. |
| `SCHEDULE_MINUTE` | `0` | Minute of the hour to fire. |
| `SCHEDULE_OFFSET_MAX_MINUTES` | `20` | Random delay (1 min–this) added after a trigger fires before the search starts, to spread load. |
| `SEARCH_THROTTLE_SECONDS` | `60` | Pause (+ jitter) between consecutive per-title API calls, to stay under provider rate limits. |
| `PROVIDER_COOLDOWN_HOURS` | `4` | Hours to skip a provider after it returns a 503 outage; resumes automatically afterward. |
| `RUN_ON_START` | `0` | `1` runs one search immediately when the worker boots (handy to test). |

> Changing the schedule requires `job-squire restart <name>` (the worker is one of the three
> processes inside the instance's single container, so a full instance restart picks it up).

### Automated AI features (scheduler)

These control the server-side AI routines run by the worker. The worker requires AI mode set to **API** and at least one AI provider or Anthropic API key configured in the app.

| Variable | Default | Purpose |
|---|---|---|
| `FOLLOWUP_DRAFT_HOUR` | `6` | Hour (0–23, local time) the auto-follow-up draft job runs daily. |
| `WEEKLY_REVIEW_HOUR` | `6` | Hour the weekly strategy review runs every Monday. |
| `CLAUDE_DEFAULT_MODEL` | `claude-sonnet-4-6` | Default Claude model for API analysis calls when using Anthropic. Overrides the in-app model setting if set. |
| `CLAUDE_ADAPTIVE_MODELS` | `claude-opus-4-8` | Comma-separated list of model names that use the `effort` param for thinking (adaptive thinking) instead of `budget_tokens` (extended thinking). |

### Integrations

| Variable | Default | Purpose |
|---|---|---|
| `INGEST_API_KEY` | — | Enables `POST /api/ingest`. Leave blank to disable. Generate with `secrets.token_urlsafe(32)`. |
| `PUBLIC_MCP_URL` | — | Public HTTPS base of the MCP service (e.g. `https://mcp-squire.yourdomain.com`). Enables MCP mode; this **base URL** is what the user adds in Claude (OAuth handles auth, no token in the path). |
| `PUBLIC_MCP_HOST` | `mcp-squire.yourdomain.com` | Hostname the MCP server allowlists for DNS-rebinding protection. Set to the public MCP host if it differs from the default. |
| `MCP_PORT` | `9000` | Port the MCP container listens on internally. |

### Build args (Dockerfile)

| Arg | Default | Purpose |
|---|---|---|
| `PUID` | `1000` | Runtime user id. Run `id -u` on your host to find the right value. |
| `PGID` | `1000` | Runtime group id. Run `id -g` on your host. |
| `BUILD_VERSION` | `dev` | Stamped into the image at build time and shown in the page footer. The GitHub Actions workflow sets it to the git SHA automatically. |

Set in the compose `build.args`. Changing `PUID`/`PGID` requires a rebuild **and** wiping the
`Job Squire-data` volume so it re-initializes with the new ownership.

## In-app settings (Settings page, encrypted in DB)

The Settings page is tabbed: **Search**, **Sources**, **Email**, **AI**, **Candidate Profile**,
**Application Kit**, **History**, and **Backup**.

### Search targets — `SearchConfig`
Job titles (one per line), country, location, radius (miles), minimum salary (blank = no
filter), max posting age (days), results per query, and an enabled toggle. Starts blank on a
fresh install.

**Country** is an ISO 3166-1 alpha-2 code, default `US`. It controls how strictly location is
validated and which country parameter Adzuna/Google Jobs are called with:

- **`US`** (default): location must be `"City, ST"` with a valid US state code (e.g. `Provo,
  UT`) — ZIP codes and street addresses are rejected, because the job APIs need a parseable
  city/state and the scheduler derives its timezone from it.
- **Anything else**: location just needs to be non-empty free text (e.g. `Manchester`,
  `Manchester, UK`). Adzuna is called against the configured country's endpoint if it's one of
  the countries Adzuna's API supports (AT, AU, BR, CA, DE, FR, GB, IN, IT, MX, NL, PL, RU, SG,
  US, ZA) — otherwise that source is skipped with an explanation in Settings → History rather
  than failing every run. Google Jobs (SerpApi) accepts a broader set of countries via the same
  field. USAJOBS is US federal jobs only and ignores this setting entirely.
  `timezones.py`'s location → timezone lookup only covers US states, so set `SCHEDULE_TZ`
  explicitly for a non-US install — without it, scheduled search times are computed in UTC.

### Application kit — `KitConfig` (Application Kit tab)
`fit_salary_floor` (default `$60,000`): the kit's fit-assessment step flags any posting whose top
salary falls below this so the candidate doesn't waste effort on low-paying roles.

### Candidate documents (Candidate Profile tab)
The master **candidate profile** (`candidate_profile.md`, stored in `/data`) is edited here, or
replaced by uploading a `.md` file, and is the source of truth for every application kit. The
**document library** (`CandidateAsset`) holds the base resume, recommendation letters, certs, and
portfolio items; each can carry a note for Claude. Two prompt helpers are available in this tab:
a **profile-generation prompt** (Claude reads uploaded assets and writes a new profile back via
`save_candidate_profile`) and an **evaluate documents prompt** (Claude reviews every uploaded file
and returns a structured assessment of strengths, gaps, and recommendations).

### Job sources — `ProviderCredential` (one row per provider)
Each provider has its own free API key(s), entered here and **encrypted at rest**. Tick "Use this
source" to enable. Providers and their fields:

| Provider | Fields | Sign up |
|---|---|---|
| The Muse | API Key (optional) | https://www.themuse.com/developers/api/v2 |
| Jobicy | No key required | https://jobicy.com/ |
| ZipRecruiter | API Key | https://www.ziprecruiter.com/partner |
| Google Jobs (SerpApi) | API Key; Max runs/day; Max titles/run | https://serpapi.com/users/sign_up |
| Adzuna | App ID, App Key | https://developer.adzuna.com/ |
| Jooble | API Key | https://jooble.org/api/about |
| USAJOBS (federal) | Registered email, Authorization Key | https://developer.usajobs.gov/APIRequest/ |

Google Jobs (SerpApi) aggregates Indeed, LinkedIn, ZipRecruiter, Workday, Greenhouse, and hundreds of other boards. The free SerpApi tier includes 250 searches/month; the **Max runs/day** and **Max titles/run** fields let you stay within that quota. A live monthly query estimate is shown in the Settings form. Per-provider daily run counts are persisted in `DATA_DIR/provider_daily_runs.json` and reset at UTC midnight.

> LinkedIn, Monster, and Indeed block direct automated access and are not available as standalone
> sources. Google Jobs (SerpApi) provides indirect coverage of those boards via Google's aggregated
> job index.

### Email notifications — `SmtpConfig`
Host, port, username, password (encrypted), from address, send-to, STARTTLS toggle, enabled
toggle. **Credential gotcha:** the **Username** is the provider's SMTP login, which is not always
your account email. For **Brevo**, use the dedicated SMTP login on its SMTP & API page (not the
Brevo account email); the **Password** is the SMTP key, not your account password. Use the
**Send test email** button to verify.

### AI analysis — `AIConfig` (AI tab)

Mode (`manual` / `api` / `mcp`); Anthropic model (API mode, default `claude-sonnet-4-6`);
Anthropic API key (encrypted, optional in API mode when ranked providers are configured); a
thinking mode (`disabled` / `low` / `medium` / `high`, Anthropic only); and the **connector
name** you gave the connector in Claude (used to phrase the "Open in Claude" prompts and Claude
Pro routine prompts). In MCP mode the page shows the **base connector URL** to add in Claude —
auth is OAuth (you sign in on the connector's own login page), so there is no token to generate
or paste.

In MCP mode the AI tab also shows a **Daily routines** section with five copy-ready prompts
(Morning Briefing, New Job Triage, Application Kit Queue, Follow-Up Drafts, Weekly Strategy
Review) generated by `app/prompts.py`. Each prompt can be pasted directly into Claude Pro's
scheduled task feature or run immediately via the "Open in Claude" button.

**Automated AI feature toggles** (API mode only, each stored in `AIConfig`):

| Setting | Default | Purpose |
|---|---|---|
| `auto_triage_enabled` | off | Score new Saved jobs for fit (1-10) after each scheduled search run. |
| `triage_model` | `claude-haiku-4-5` | Fallback Anthropic model for auto-triage and auto-follow-up drafts. Ranked providers use the triage model set on each provider row. |
| `auto_followup_enabled` | off | Draft follow-up emails daily for overdue jobs with no existing draft. |
| `auto_weekly_review_enabled` | off | Generate a written weekly strategy review every Monday. Uses the main model and thinking mode. |
| `rejection_alert_threshold` | `5` | Trigger an automatic rejection pattern analysis when this many jobs move to Rejected or Ghosted within 14 days. Runs at most once per 7 days. |
| `fallback_to_anthropic` | on | After all ranked providers fail, attempt the Anthropic API as a last resort (requires an Anthropic API key). |

### AI providers — `AIProviderConfig` (AI tab → AI Providers)

Zero or more rows; tried in `rank` order. When a provider returns a rate-limit (429), server
error (503/529), or times out, Job Squire moves to the next row.

| Column | Purpose |
|---|---|
| `rank` | Try order (1 = first). Reorder with the ▲/▼ buttons in the UI. |
| `provider` | Type slug: `gemini`, `groq`, `openrouter`, `ollama`, `mistral`, `openai`, `custom`. Determines the built-in base URL. |
| `label` | Optional display name (e.g. "Gemini Free Tier"). |
| `api_key_enc` | API key, Fernet-encrypted at rest. Not required for Ollama. |
| `base_url` | Override URL. Required for Ollama and custom endpoints; leave blank for cloud providers. |
| `model` | Model string for analysis / weekly review / rejection alert calls. Leave blank for provider default. |
| `triage_model` | Model string for auto-triage and follow-up draft calls (prefer a fast, cheap model). Falls back to `model` if blank. |
| `enabled` | Toggle without deleting. Disabled rows are skipped. |

See [Setting Up AI](wiki/10-ai-setup.md) for model recommendations and per-provider setup instructions.
