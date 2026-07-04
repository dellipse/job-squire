# Code Reference

Module-by-module guide to the source. Paths are relative to the project root. Start here when you
need to change behavior, then read the specific file.

## Layout

```
job-squire/
  wsgi.py                  # gunicorn entrypoint: app = create_app()
  requirements.txt
  Dockerfile               # python:3.14-slim, non-root UID/GID (build args PUID/PGID, default 1000)
  docker-compose.yml       # the job-squire services (web, worker, mcp); Option A (ports) or Option B (shared network)
  examples/
    .env.example                     # template for data/.env
    nginx/
      job-squire.subdomain.conf     # sample nginx/SWAG proxy-conf for the web app
      mcp-squire.subdomain.conf     # sample nginx/SWAG proxy-conf for the MCP server (http2 off)
  app/
    __init__.py            # app factory, config, security headers, DB init + migrations + seeding
    extensions.py          # db, login_manager, csrf, limiter singletons
    models.py              # all SQLAlchemy models + status constants
    forms.py               # WTForms (also provide CSRF)
    crypto.py              # Fernet encrypt/decrypt for stored secrets
    timezones.py           # map a "City, ST" location to an IANA timezone (for the scheduler)
    auth.py                # auth blueprint: login / logout
    main.py                # main blueprint: everything else (UI, API, settings)
    providers.py           # job-board adapters (Adzuna, Jooble, USAJOBS, The Muse) + retry/backoff
    search.py              # run_search(), ingest_jobs() dedup, cooldowns, email triggers
    notify.py              # SMTP send + digest + error-report builders
    worker.py              # APScheduler process (python -m app.worker)
    ai.py                  # AI payload, prompt, JSON parsing, Anthropic API call, apply
    mcp_server.py          # remote MCP server with OAuth (python -m app.mcp_server)
    candidate_profile.md   # bundled master profile, copied to /data on first boot (then edited there)
    templates/             # Jinja2 templates
    static/style.css       # all styling
    static/app.js          # all client JS (CSP-safe; no inline handlers)
  docs/                    # this documentation (bundled into the image; the user guide is served at /guide)
    Job_Squire_User_Guide.md
```

## `app/__init__.py` — application factory

- `create_app()` — builds and returns the Flask app. Reads config from env (see
  [configuration.md](configuration.md)). Requires `SECRET_KEY` (raises unless `ALLOW_INSECURE`).
  Sets `MAX_CONTENT_LENGTH`, secure cookie flags, `SQLALCHEMY_ENGINE_OPTIONS` (SQLite timeout).
  Wraps in `ProxyFix`. Registers blueprints. Adds `set_security_headers()` (`after_request`) with
  the CSP, `X-Frame-Options`, etc. Finally runs `_init_database()`.
- `_init_database(app, data_dir)` — **important**: serializes DB setup with an exclusive
  `fcntl.flock` on `/data/.init.lock` so the multiple gunicorn workers and the other containers
  don't race on `create_all()` (the original "table jobs already exists" bug). Enables WAL,
  `create_all()`, runs `_run_migrations()`, then seeds.
- `_run_migrations()` — applies **additive** `ALTER TABLE ... ADD COLUMN` statements that
  `create_all()` won't apply to an existing table (e.g. `smtp_config.admin_email`,
  `ai_config.connector_name` / `thinking_mode`, `jobs.kit_output` / `kit_generated_at`).
  Idempotent: a "duplicate column" error is swallowed.
- `_seed_data_files(data_dir)` — on first boot, copies the bundled `candidate_profile.md` from the
  app package into `/data` so it can be edited (in the UI or via MCP) without rebuilding the image.
- `_display_tz()` + the `local_dt` Jinja filter — render naive-UTC datetimes in the search
  location's local time (12-hour clock). Resolution order: `SCHEDULE_TZ` → search location →
  `America/Los_Angeles`.
- `_seed_users(app)` — creates the admin and user accounts from env passwords (lowercased
  usernames). Honors `RESET_UIDS_AND_PWDS_ON_START` (resets username, display name, and password for each account).
- `_seed_search_defaults()` — creates a **blank** singleton `SearchConfig` (id=1), disabled, on
  first start (titles/location are entered in the UI).
- `_bool_env(name, default)` — parse a boolean env var.

## `app/extensions.py`

Holds the shared extension instances so modules avoid circular imports: `db` (SQLAlchemy),
`login_manager`, `csrf` (CSRFProtect), `limiter` (Flask-Limiter, keyed by remote address).
Sets `login_manager.login_view = "auth.login"`.

## `app/models.py`

All models (see the table in [architecture.md](architecture.md)) plus constants:
`STATUSES`, `ACTIVE_STATUSES`, `WORK_MODES`, `ATTACHMENT_KINDS`, `ASSET_KINDS`, `CONTACT_TYPES`,
`SUBMISSION_STATUSES`, `ACTIVE_SUBMISSION_STATUSES`. `User` has
`set_password`/`check_password`/`is_admin`. `Job` has `is_active`, `follow_up_due` properties and
a `notes_log` relationship (its `JobNote` activity entries). `JobNote` records manual notes plus
auto-logged status/follow-up changes. `CandidateAsset` (master documents) has `display_name` and
`size_kb` helpers. `KitConfig` holds `fit_salary_floor`.
`Contact` has `follow_up_due` and `open_submissions` properties and a cascade relationship to its
`submissions`. `Submission` has `is_active`/`follow_up_due` and an optional `job` relationship
(`passive_deletes`, so deleting a job unlinks rather than deletes the submission).
`SearchConfig.title_list` splits the titles textarea into a list. Singletons (`SearchConfig`,
`KitConfig`, `SmtpConfig`, `AIConfig`) are always row **id=1**, created on demand by `_singleton()`
in `main.py`. `AIConfig` also stores `connector_name` (the name the user gave the connector in
Claude, used to build the "Open in Claude" prompts) and `thinking_mode`.

> Adding a column/table: edit the model, then redeploy. `create_all()` creates **new tables**
> automatically, but it does **not** alter existing tables. Adding a column to an existing table
> needs a manual migration (or, in dev with no data, wipe the `Job Squire-data` volume).

## `app/forms.py`

WTForms classes (each also enforces CSRF): `LoginForm`, `JobForm`, `InterviewForm`,
`AttachmentForm` (file type/size validation), `AIImportForm`, `KitForm` (the kit generator),
`ContactForm` (recruiter/contact), `SubmissionForm` (a logged submission; its `contact_id` and
`job_id` select choices are populated per-request in `main.py` via `_populate_submission_choices`),
`CandidateAssetForm` (upload a master document — accepts docs and images) and
`CandidateAssetEditForm` (edit a stored asset's kind/label/notes without re-uploading), and
`ConfirmForm` (a bare CSRF-only form used to protect delete/confirm POST buttons). Note `JobForm`
and `KitForm` use WTForms `URL()` validation, which requires a TLD (e.g. `https://x/job` is
rejected — real postings are fine); `ContactForm.linkedin_url` is plain text (lenient) so a pasted
profile URL without a scheme is accepted.

## `app/timezones.py`

Maps a job-search location string to an IANA timezone so the scheduler fires in the location's
local time, not the server clock (often GMT/UTC). `parse_state(location)` extracts the two-letter
US state code from `"City, ST"`, `"City, ST 89011"`, or a spelled-out state name (returns `None`
if it can't — the Settings form rejects locations it can't parse). `timezone_for_location(...)`
returns the state's predominant IANA zone, falling back to `DEFAULT_TZ` (`America/Los_Angeles`).

## `app/crypto.py`

`encrypt(secret_key, plaintext)` / `decrypt(secret_key, stored)`. Fernet key is
`base64(sha256(SECRET_KEY))`. Encrypted values are prefixed `enc:`. Plaintext (legacy) is
tolerated on decrypt. **Rotating `SECRET_KEY` makes all stored secrets undecryptable** (re-enter
provider keys, SMTP password, Anthropic key; regenerate the MCP token).

## `app/auth.py` — `auth` blueprint

- `GET/POST /login` — rate limited (`10/min; 60/hour` on POST). Looks up the user (username
  lowercased), checks password, logs in, redirects to a safe `next` or the dashboard.
- `GET /logout`.
- `_is_safe_next(target)` — only allows relative redirects back into the app.

## `app/main.py` — `main` blueprint (the bulk of the app)

Helpers:
- `_inject_globals()` — a `app_context_processor` that injects `ai_mode` (drives the "Open in
  Claude" buttons) and `build_version` (the `BUILD_VERSION` build arg, shown in the page footer)
  into **every** template.
- `_claude_search_prompt()` — builds the "Search jobs in Claude" prompt from `SearchConfig` and the
  configured connector name.
- `admin_required` — decorator gating admin-only routes (job delete).
- `_singleton(model)` — get-or-create the id=1 row for a config model.
- `_business_days_from(start, n)` — date `n` business days out (default follow-up = 3 business days).
- `_add_job_note(job_id, content, note_type)` — append an activity-log entry; called by edits to
  auto-log status and follow-up changes.
- `_apply_job_form` / `_apply_interview_form` / `_apply_contact_form` / `_apply_submission_form` —
  copy form fields onto a model. `_apply_submission_form` parses the string `contact_id`/`job_id`
  selects to ints (or None) and back-fills company/role from a linked job when left blank.
- `_populate_submission_choices(form)` — fills the recruiter and job dropdowns on `SubmissionForm`.
- `_build_kit(...)`, `_load_profile()` / `_save_profile()`, `_load_profile_prompt()` /
  `_save_profile_prompt()`, `KIT_PROMPT` — assemble the application-kit markdown and read/write the
  profile + profile-generation prompt files in `/data`. `KIT_PROMPT` is the full multi-step kit
  instruction set (fit assessment, company + salary research, ATS keyword analysis, the tailored
  documents, save to disk, push back via MCP); `_build_kit` substitutes the candidate location and
  `fit_salary_floor` into it.
- Jobs-list sort/pagination helpers: `_parse_sort` / `_apply_sort` (multi-column sort, NULLs last)
  with per-page and sort preferences persisted in the session.
- `_user_guide_path()` / `_render_user_guide()` — locate and render the bundled user-guide
  Markdown to HTML (via the `markdown` library) for the `/guide` page. The guide ships in
  `docs/` and is copied into the image at `docs/` by the Dockerfile.
- `_int(...)` — tolerant int parsing for settings forms.

### Route table

| Method & path | Function | Notes |
|---|---|---|
| `GET /` | `dashboard` | Metrics, pipeline, follow-ups due (jobs + recruiters), open submissions, recent activity, latest AI summary. |
| `GET /jobs` | `jobs_list` | Filter by status/search; multi-column sort + pagination (per-page & sort persisted in session). Passes `search_prompt`. |
| `GET/POST /jobs/new` | `job_new` | Create a job. |
| `GET /jobs/<id>` | `job_detail` | Detail + attachments + debriefs + activity log. Passes `ai_mode`, connector name. |
| `GET/POST /jobs/<id>/edit` | `job_edit` | Auto-logs status and follow-up changes to the activity log. |
| `POST /jobs/<id>/delete` | `job_delete` | **admin only**. Deletes files too; unlinks submissions. |
| `POST /jobs/<id>/notes` | `job_add_note` | Add a manual activity-log note. |
| `POST /jobs/<id>/set-followup` | `job_set_followup` | Set/clear the follow-up date (defaults to +3 business days). |
| `GET/POST /jobs/<id>/interviews/new` | `interview_new` | Add a debrief. |
| `GET/POST /interviews/<id>/edit` | `interview_edit` | |
| `POST /interviews/<id>/delete` | `interview_delete` | |
| `GET /contacts` | `contacts_list` | Recruiter/contact list. Filter by type/search. |
| `GET/POST /contacts/new` | `contact_new` | Create a contact. |
| `GET /contacts/<id>` | `contact_detail` | Contact detail + their submission history. |
| `GET/POST /contacts/<id>/edit` | `contact_edit` | |
| `POST /contacts/<id>/delete` | `contact_delete` | Deletes the contact and (cascade) its submissions. |
| `GET /export/contacts.csv` | `export_contacts_csv` | All contacts as CSV. |
| `GET/POST /submissions/new` | `submission_new` | Log a submission. GET `?contact_id=N`/`?job_id=N` pre-fills. |
| `GET/POST /submissions/<id>/edit` | `submission_edit` | |
| `POST /submissions/<id>/delete` | `submission_delete` | |
| `POST /jobs/<id>/upload` | `attachment_upload` | Validated doc upload to `/data/uploads`. |
| `GET /attachments/<id>/download` | `attachment_download` | Auth-gated file serving. |
| `POST /attachments/<id>/delete` | `attachment_delete` | |
| `GET /export/csv` | `export_csv` | Whole Job Squire as CSV. |
| `GET /guide` | `user_guide` | Renders the bundled `Job_Squire_User_Guide.md` as an in-app page. |
| `GET /jobs/<id>/kit` | `job_kit` | Download the application-kit markdown for a job. |
| `GET/POST /kit` | `kit_hub` | Kit generator. GET `?job_id=N` pre-fills from a tracked job. |
| `GET /export/ai` | `export_ai` | Download the pipeline JSON for manual AI analysis. |
| `GET/POST /ai` | `ai_hub` | AI tab; POST is the manual import. Renders per `AIConfig.mode`. |
| `POST /ai/analyze` | `ai_analyze` | API mode: calls Anthropic (with thinking mode), applies result. |
| `POST /settings/ai` | `settings_ai` | Save AI mode/model/key/connector name/thinking mode. |
| `POST /api/ingest` | `api_ingest` | **CSRF-exempt**, `X-API-Key` = `INGEST_API_KEY`. Batch job push. |
| `GET /settings` | `settings` | Settings page (Search, Sources, Email, AI, Candidate Profile, Application Kit, History tabs). |
| `POST /settings/search` | `settings_search` | Save search targets (validates `"City, ST"`). |
| `POST /settings/kit` | `settings_kit` | Save the application-kit `fit_salary_floor`. |
| `POST /settings/provider/<provider>` | `settings_provider` | Save+encrypt a provider's keys. |
| `POST /settings/provider/<provider>/test` | `settings_provider_test` | Ping one provider with its saved key. |
| `POST /settings/provider/<provider>/pull` | `settings_provider_pull` | Run a full search for one provider now, clear its cooldown. |
| `POST /settings/smtp` | `settings_smtp` | Save+encrypt SMTP config (incl. admin alert address). |
| `POST /settings/test-email` | `settings_test_email` | Send a one-off test email. |
| `POST /settings/run` | `settings_run` | Run the search now (synchronous). |
| `POST /settings/assets/upload` | `settings_asset_upload` | Upload a master candidate document. |
| `GET /assets/<id>/download` | `asset_download` | Download a candidate asset. |
| `POST /assets/<id>/edit` | `asset_edit` | Edit a candidate asset's kind/label/notes. |
| `POST /assets/<id>/delete` | `asset_delete` | Delete a candidate asset (and its file). |
| `POST /settings/profile` | `settings_profile` | Save the candidate profile markdown. |
| `POST /settings/profile/upload` | `settings_profile_upload` | Replace the profile from an uploaded `.md`. |
| `POST /settings/profile-prompt` | `settings_profile_prompt` | Save the profile-generation prompt. |

AI helpers used by routes live in `app/ai.py` (not duplicated in `main.py`).

## `app/providers.py` — job-board adapters

- `PROVIDERS` — dict of metadata for the UI (label, signup URL, note, fields). To add a provider,
  add an entry here + a `search_*` function + a branch in `search_provider`.
- `search_adzuna/jooble/usajobs/themuse(creds, title/titles, cfg)` — each returns a list of
  **normalized job dicts**: `external_id, source, title, company, location, url, salary,
  description, date_posted`.
- `search_provider(provider, creds, titles, cfg)` — runs one provider across all titles and
  returns `(results, error_or_None)`. Never raises (one bad provider can't kill a run). Pauses
  `SEARCH_THROTTLE_SECONDS` (+ jitter) between titles, and first calls `_missing_required` to fail
  fast with an actionable message if required creds are blank (a common sign `SECRET_KEY` changed
  and cleared saved keys). HTTPError messages include the status code, response body, and a
  plain-English hint per code (401/403/429/503).
- `_request(method, url, ...)` — HTTP wrapper with retry/backoff (+ jitter) on transient codes
  (429/502/504). 503 is **not** retried here — it signals a multi-minute outage, so `search.py`
  puts the provider in cooldown instead.
- Helpers: `_clean` (strip HTML), `_fmt_money`, `_iso_date`, `_missing_required`.

## `app/search.py` — orchestration + dedup

- `ingest_jobs(items, created_by, default_status="Saved")` — the **single dedup + insert path**
  used by the worker, `/api/ingest`, and the MCP `add_jobs` tool. Dedup key: `(source,
  external_id)` if present, else case-insensitive `(company, title)`; also dedups within the
  batch. Returns `(created_jobs, skipped)`.
- `run_search(trigger)` — loads enabled providers (decrypts creds) and `SearchConfig`, skips any
  provider currently in cooldown, queries the rest, ingests, records a `SearchRun`, emails a digest
  on new finds, and emails an error report if any provider failed. Must run in app context.
- Cooldown helpers `_load_cooldowns` / `_save_cooldowns` / `_in_cooldown` / `_set_cooldown` — a
  provider that returns 503 is parked in `/data/provider_cooldowns.json` for
  `PROVIDER_COOLDOWN_HOURS` so later runs skip it until the outage clears. (The Settings "Pull now"
  button clears a provider's cooldown.)
- `_maybe_email(secret_key, created_jobs)` — builds and sends the new-jobs digest if SMTP is on.
- `_maybe_error_email(secret_key, errors, trigger)` — sends the error report to the admin address
  (CC the job-seeker if different).

## `app/notify.py`

- `send_email(smtp_dict, subject, text, html=None, extra_to=None)` — smtplib send; handles port 465
  (SSL) vs 587 (STARTTLS); `extra_to` adds CC recipients.
- `build_digest(jobs, base_url)` — returns `(subject, text, html)` for new-jobs emails.
- `build_error_report(errors, trigger, base_url)` — returns `(subject, text, html)` for a run that
  hit provider errors.

## `app/worker.py` — scheduler

`python -m app.worker`. Builds a `BlockingScheduler` with two cron triggers from env
(`SCHEDULE_WEEKDAY_HOURS` default `8,13,17` Mon–Fri, `SCHEDULE_WEEKEND_HOURS` default `9`). The
scheduler timezone follows the **job-search location** (via `timezones.py`), not the server clock:
`_resolve_timezone()` uses `SCHEDULE_TZ` if set, else derives it from the location, else Pacific.
`_run()` waits a random 1–`SCHEDULE_OFFSET_MAX_MINUTES` minutes (so parallel workers don't hit
provider APIs at the same instant), then calls `run_search("scheduled")`. `RUN_ON_START=1` runs
once at boot. **Schedule changes require restarting this container**; title/location changes are
read live each run.

## `app/ai.py` — AI analysis (shared by all three modes)

- `ANALYSIS_INSTRUCTIONS` — the task/schema text used everywhere so modes stay consistent.
- `build_export_dict()` — the full pipeline + debriefs as a JSON-able dict.
- `manual_prompt()` — the human prompt for manual mode.
- `extract_json(raw)` — tolerant JSON parse (handles ```json fences / surrounding prose).
- `apply_analysis(parsed, created_by)` — writes the global `AIInsight` + per-job `ai_analysis`.
  Returns `(updated, missing)`.
- `run_api_analysis(api_key, model, thinking_mode="disabled")` — POSTs to
  `https://api.anthropic.com/v1/messages`, parses the JSON reply (collecting only text blocks, so
  thinking blocks are ignored). Default model `claude-sonnet-4-6` (configurable on the Settings
  page). `thinking_mode` maps to the `effort` param on Opus 4.8 (`_ADAPTIVE_MODELS`) or
  `thinking.budget_tokens` on Sonnet/Haiku (`_THINKING_BUDGETS`).

## `app/mcp_server.py` — remote MCP server

See [mcp-connector.md](mcp-connector.md) for the full picture. In brief: a `FastMCP` server
(Streamable HTTP) wrapped by `asgi_app`, which handles the **OAuth 2.0** endpoints
(`/.well-known/...`, `/oauth/register|authorize|token`), serves a login page as the authorization
step, issues 30-day Bearer tokens (in-memory, PKCE-verified), and gates the `/mcp` endpoint on a
valid token. The legacy token-in-path (`/mcp/<token>`, via `_legacy_token()`) still works as a
fallback. `/health` is open. `main()` runs uvicorn on `MCP_PORT` (9000). Reuses the Flask app
context for DB access; DNS-rebinding protection allowlists `PUBLIC_MCP_HOST`.

The 23 tools span reads and writes. Core tools: `get_pipeline`, `list_jobs`, `get_job`,
`get_candidate_profile`, `save_candidate_profile`, `get_candidate_assets`, `add_jobs`,
`get_search_targets`, `save_analysis`, `get_kit_instructions`, `update_job_notes`, `save_kit`,
`set_follow_up`, `list_contacts`, `get_contact`, `add_contact`, and `log_submission`.
Routine-support tools: `list_unanalyzed_jobs`, `set_job_fit`, `list_overdue_followups`,
`save_followup_draft`, `save_interview_prep`, and `get_weekly_summary`.

## `app/static/app.js` — client behavior (CSP-safe)

Because the CSP is `script-src 'self'`, there are **no inline handlers**. This file wires up,
via `data-*` attributes / classes: clickable rows (`data-href`), navigate-on-change selects
(`data-navigate`), auto-submit selects (`data-autosubmit`), click-to-select inputs
(`.select-on-click`), confirm-before-submit forms (`data-confirm`), and the "Build kit in Claude"
button (`#kit-claude-btn`) which reads the kit form fields and opens a pre-filled `claude.ai/new`
chat. **If you add client interactivity, add it here, not inline.**

## Templates (`app/templates/`)

`base.html` (layout + nav + flashes + build-version footer + the `app.js` include), `login.html`,
`dashboard.html`, `jobs.html` (sortable headers + pagination), `job_form.html`, `job_detail.html`
(attachments, debriefs, activity log), `interview_form.html`, `kit_hub.html`, `ai_hub.html`,
`settings.html` (the tabbed Search / Sources / Email / AI / Documents / History page),
`contacts.html`, `contact_form.html`, `contact_detail.html`, `submission_form.html`, and
`guide.html` (renders the bundled user guide). The nav and the "Open in Claude" buttons key off the
injected `claude_buttons_enabled` flag.
