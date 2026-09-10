# Code Reference

Module-by-module guide to the source. Paths are relative to the project root. Start here when you
need to change behavior, then read the specific file.

## Layout

```
job-squire/
  wsgi.py                  # gunicorn entrypoint: app = create_app()
  requirements.txt
  Dockerfile               # LinuxServer baseimage-alpine + s6-overlay, non-root UID/GID (PUID/PGID)
  docker-compose.yml # web+worker+mcp in one container; `job-squire create` generates a per-instance copy of this
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
    auth.py                # auth blueprint: login / logout / self-service password change
    main.py                # main blueprint: dashboard, health check, timeline, guide/wiki
    jobs.py                # jobs blueprint: jobs/interviews/attachments (split from main.py in v0.8.0)
    contacts.py            # contacts blueprint: contacts/submissions (split from main.py in v0.8.0)
    kits.py                # kits blueprint: application-kit generator (split from main.py in v0.8.0)
    settings.py            # settings blueprint: search/sources/email/AI/profile/assets/ingest API
    ai_tasks.py            # AI tab + analyze/run-task/triage-batch routes (split from main.py in v0.8.0)
    task_status.py         # shared background-thread/poll routes for long-running AI calls
    onboarding.py          # Getting Started walkthrough blueprint
    providers.py           # job-board adapters (The Muse, Jobicy, ZipRecruiter, Google Jobs, Adzuna,
                            # Jooble, USAJOBS) + retry/backoff
    search.py              # run_search(), ingest_jobs() dedup, cooldowns, email triggers
    notify.py              # SMTP send + digest + error-report builders
    worker.py              # APScheduler process (python -m app.worker)
    ai.py                  # AI payload, prompt, JSON parsing, multi-provider API calls, apply
    privacy.py             # PII/SPI redaction + rehydration for all AI paths
    prompts.py             # Claude Pro routine prompt templates
    mcp_server.py          # remote MCP server with OAuth (python -m app.mcp_server)
    mcp_auth.py            # MCP OAuth 2.0/PKCE endpoints and static-key verification
    backup.py              # in-app backup archive builder
    backup_cli.py          # container-side backup entrypoint (job-squire-cli)
    mcp_token_cli.py       # container-side MCP token entrypoint (job-squire-cli)
    ollama_provider_cli.py # container-side Ollama provider entrypoint (job-squire-cli)
    secrets_copy_cli.py    # container-side settings-copy entrypoint (job-squire-cli)
    db_utils.py            # transient SQLite retry helper
    deploy.py              # DEPLOY_MODE preset resolution + startup guard
    docgen.py              # Markdown -> .docx renderer
    kit_export.py          # ATS cleaning + PDF export for saved kits
    resume_convert.py      # deterministic resume upload -> Markdown
    sample_locations.py    # placeholder "City, ST" text for empty-field copy
    websearch.py           # best-effort DuckDuckGo research for kit generation
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
- `GET/POST /account` — rate-limited self-service password change.
- `_is_safe_next(target)` — only allows relative redirects back into the app.

## `app/main.py` — `main` blueprint

As of the v0.8.0 blueprint split (see CHANGELOG), `main.py` only holds the dashboard and a handful
of app-wide routes — jobs, contacts, kits, settings, and AI routes now live in their own
blueprints (below). Every route path is unchanged from pre-split; only the Flask endpoint name
(and which file it lives in) moved.

Helpers: `_inject_globals()` (context processor injecting `ai_mode`/`build_version` into every
template), `admin_required` (decorator gating admin-only routes), `_singleton(model)` (get-or-create
the id=1 row for a config model), `_worker_heartbeat_status()`/`_stale_cutoffs()` (worker-heartbeat
staleness check backing the dashboard warning and `/health`), `_bookmarklet_js()` (generates the
quick-apply bookmarklet's JS), `_user_guide_path()`/`_render_user_guide()` (render bundled
Markdown docs for `/guide` and `/wiki/<page>`), `_load_profile()`/`_save_profile()`/
`_load_profile_prompt()`/`_save_profile_prompt()` (read/write the candidate profile + its
generation prompt in `/data`).

| Method & path | Function | Notes |
|---|---|---|
| `GET /health` | `health` | Aggregated healthcheck; also checks worker heartbeat staleness. |
| `GET /` | `dashboard` | Metrics, pipeline, follow-ups due (jobs + recruiters), open submissions, recent activity, latest AI summary. |
| `GET /guide` | `user_guide` | Renders the bundled `Job_Squire_User_Guide.md` as an in-app page. |
| `GET /wiki/<page>` | `wiki_page` | Renders a bundled `docs/wiki/*.md` page. |
| `GET /timeline` | `timeline` | Cross-job activity timeline. |
| `GET /setup` | `setup_redirect` | First-boot redirect into the Getting Started walkthrough. |
| `GET /api/mcp-ping` | `mcp_ping` | Lightweight liveness check used by the MCP setup flow. |

## `app/jobs.py` — `jobs` blueprint

Helpers: `_claude_search_prompt()` (the "Search jobs in Claude" prompt), `_business_days_from(start,
n)` (date n business days out; default follow-up = 3 business days), `_add_job_note(job_id, content,
note_type)` (append an activity-log entry), `_apply_job_form`/`_apply_interview_form` (copy form
fields onto a model), `_parse_sort`/`_apply_sort` (multi-column sort, NULLs last, with per-page and
sort preferences persisted in the session).

| Method & path | Function | Notes |
|---|---|---|
| `GET /jobs` | `jobs_list` | Filter by status/search; multi-column sort + pagination. Passes `search_prompt`. |
| `POST /jobs/save-default-view` | `save_default_view` | Persist the current filter/sort as the default. |
| `POST /jobs/clear-default-view` | `clear_default_view` | Clear the saved default view. |
| `GET/POST /jobs/new` | `job_new` | Create a job. |
| `GET /jobs/<id>` | `job_detail` | Detail + attachments + debriefs + activity log. Passes `ai_mode`, connector name. |
| `GET/POST /jobs/<id>/edit` | `job_edit` | Auto-logs status and follow-up changes to the activity log. |
| `POST /jobs/<id>/ats-gap` | `job_ats_gap` | Run ATS keyword-gap analysis (Feature 4). |
| `POST /jobs/<id>/score-fit` | `job_score_fit` | One-off AI fit score for a single job. |
| `POST /jobs/<id>/draft-followup` | `job_draft_followup` | One-off AI follow-up draft for a single job. |
| `POST /jobs/build-kits-api` | `jobs_build_kits_api` | Kick off API-mode kit generation for one or more jobs. |
| `POST /jobs/<id>/prep-interview` | `job_prep_interview` | Runs the resume-interview-style interview prep routine for a job. |
| `POST /jobs/<id>/delete` | `job_delete` | **admin only**. Deletes files too; unlinks submissions. |
| `POST /jobs/bulk-update` | `jobs_bulk_update` | Bulk status/follow-up update across selected jobs. |
| `POST /jobs/<id>/notes` | `job_add_note` | Add a manual activity-log note. |
| `POST /jobs/<id>/set-followup` | `job_set_followup` | Set/clear the follow-up date (defaults to +3 business days). |
| `GET/POST /jobs/<id>/interviews/new` | `interview_new` | Add a debrief. |
| `GET/POST /interviews/<id>/edit` | `interview_edit` | |
| `POST /interviews/<id>/delete` | `interview_delete` | |
| `POST /jobs/<id>/upload` | `attachment_upload` | Validated doc upload to `/data/uploads`. |
| `GET /attachments/<id>/download` | `attachment_download` | Auth-gated file serving. |
| `POST /attachments/<id>/delete` | `attachment_delete` | |
| `GET /export/csv` | `export_csv` | Whole Job Squire as CSV. |

## `app/contacts.py` — `contacts` blueprint

Helpers: `_apply_contact_form`, `_populate_submission_choices(form)` (fills the recruiter/job
dropdowns on `SubmissionForm`), `_apply_submission_form` (parses string `contact_id`/`job_id`
selects to ints or None, back-fills company/role from a linked job when left blank).

| Method & path | Function | Notes |
|---|---|---|
| `GET /contacts` | `contacts_list` | Recruiters/Contacts list. Filter by type/search. |
| `GET/POST /contacts/new` | `contact_new` | Create a contact. |
| `GET /contacts/<id>` | `contact_detail` | Contact detail + their submission history. |
| `GET/POST /contacts/<id>/edit` | `contact_edit` | |
| `POST /contacts/<id>/delete` | `contact_delete` | Deletes the contact and (cascade) its submissions. |
| `GET /export/contacts.csv` | `export_contacts_csv` | All contacts as CSV. |
| `GET/POST /submissions/new` | `submission_new` | Log a submission. GET `?contact_id=N`/`?job_id=N` pre-fills. |
| `GET/POST /submissions/<id>/edit` | `submission_edit` | |
| `POST /submissions/<id>/delete` | `submission_delete` | |

## `app/kits.py` — `kits` blueprint

Helpers: `_build_kit(...)`, `KIT_PROMPT` — assemble the application-kit markdown. `KIT_PROMPT` is
the full multi-step kit instruction set (fit assessment, company + salary research, ATS keyword
analysis, the tailored documents, save to disk, push back via MCP); `_build_kit` substitutes the
candidate location and `fit_salary_floor` into it.

| Method & path | Function | Notes |
|---|---|---|
| `GET /jobs/<id>/kit` | `job_kit` | Download the application-kit markdown for a job. |
| `GET/POST /kit` | `kit_hub` | Kit generator. GET `?job_id=N` pre-fills from a tracked job. |
| `POST /kit/run` | `kit_run` | API-mode kit generation (background thread, polled via `task_status`). |
| `POST /kit/download-docx` | `kit_download_docx` | Convert a saved kit to `.docx` on demand. |
| `GET/POST /tools/kit-batch` | `kit_batch` | Batch kit generation across multiple applied jobs. |

## `app/settings.py` — `settings` blueprint

Helper: `_int(...)` — tolerant int parsing for settings forms. Also owns the JSON ingest API.

| Method & path | Function | Notes |
|---|---|---|
| `GET /settings/backup/download` | `settings_backup_download` | Download a full data-snapshot `.tgz`. |
| `POST /settings/ai-mode` | `settings_ai_mode` | Toggle Manual/API/MCP mode flags. |
| `POST /settings/claude-pro` | `settings_claude_pro` | Save the Claude Pro connector name. |
| `POST /settings/ai` | `settings_ai` | Save AI model/key/thinking mode. |
| `POST /settings/mcp-api-key` | `settings_mcp_api_key` | Generate/rotate the static MCP bearer key. |
| `POST /settings/mcp-revoke-token` | `settings_mcp_revoke_token` | Revoke a single OAuth token. |
| `POST /settings/mcp-revoke-all` | `settings_mcp_revoke_all` | Revoke all OAuth tokens. |
| `POST /settings/ai/tasks` | `settings_ai_tasks` | Save per-task (`AITaskConfig`) provider assignments. |
| `POST /settings/ai/providers/add` | `settings_ai_provider_add` | Add a row to the ranked provider chain. |
| `POST /settings/ai/providers/<id>/edit` | `settings_ai_provider_edit` | |
| `POST /settings/ai/providers/<id>/delete` | `settings_ai_provider_delete` | |
| `POST /settings/ai/providers/<id>/toggle` | `settings_ai_provider_toggle` | |
| `POST /settings/ai/providers/<id>/move-up` | `settings_ai_provider_move_up` | Re-rank. |
| `POST /settings/ai/providers/<id>/move-down` | `settings_ai_provider_move_down` | Re-rank. |
| `POST /settings/ai/providers/<id>/test` | `settings_ai_provider_test` | Ping one provider with its saved key. |
| `POST /settings/ai/privacy` | `settings_ai_privacy` | Save redaction/privacy toggles and custom patterns. |
| `POST /settings/ai/providers/fallback` | `settings_ai_providers_fallback` | Save "fall back to Anthropic" toggle. |
| `POST /api/ingest` | `api_ingest` | **CSRF-exempt**, `X-API-Key` = `INGEST_API_KEY`. Batch job push. |
| `GET /settings` | `settings` | Settings page (Search, Sources, Email, AI, Candidate Profile, Application Kit, History, Backup tabs). |
| `POST /settings/search` | `settings_search` | Save search targets (validates `"City, ST"`). |
| `POST /settings/kit` | `settings_kit` | Save the application-kit `fit_salary_floor`. |
| `POST /settings/providers/save-keyless` | `settings_providers_save_keyless` | Enable/disable keyless providers. |
| `POST /settings/provider/<provider>` | `settings_provider` | Save+encrypt a provider's keys. |
| `POST /settings/provider/<provider>/test` | `settings_provider_test` | Ping one provider with its saved key. |
| `POST /settings/provider/<provider>/pull` | `settings_provider_pull` | Run a full search for one provider now (background thread, polled), clear its cooldown. |
| `POST /settings/smtp` | `settings_smtp` | Save+encrypt SMTP config (incl. admin alert address). |
| `POST /settings/test-email` | `settings_test_email` | Send a one-off test email. |
| `POST /settings/run` | `settings_run` | Run the search now (background thread, polled). |
| `POST /settings/assets/upload` | `settings_asset_upload` | Upload a master candidate document. |
| `GET /assets/<id>/download` | `asset_download` | Download a candidate asset. |
| `GET /assets/<id>/download-source` | `asset_download_source` | Download the original uploaded file, unconverted. |
| `POST /assets/<id>/set-base` | `asset_set_base` | Mark an asset as the Base Resume. |
| `POST /assets/<id>/edit` | `asset_edit` | Edit a candidate asset's kind/label/notes. |
| `POST /assets/<id>/delete` | `asset_delete` | Delete a candidate asset (and its file). |
| `POST /settings/profile` | `settings_profile` | Save the candidate profile markdown. |
| `POST /settings/profile-prompt` | `settings_profile_prompt` | Save the profile-generation prompt. |
| `POST /settings/profile/upload` | `settings_profile_upload` | Replace the profile from an uploaded `.md`. |

## `app/ai_tasks.py` — `ai_tasks` blueprint

| Method & path | Function | Notes |
|---|---|---|
| `GET /export/ai` | `export_ai` | Download the pipeline JSON for manual AI analysis. |
| `GET/POST /ai` | `ai_hub` | AI tab; POST is the manual import. |
| `POST /ai/analyze` | `ai_analyze` | API mode: calls the ranked provider chain, applies result. |
| `POST /ai/run/<task>` | `ai_run_task` | Run one automated task (triage/followup/weekly_review/rejection_alert) on demand. |
| `GET/POST /tools/triage-batch` | `triage_batch` | Batch auto-triage across unscored Saved jobs. |

AI logic itself (provider calls, prompt building, JSON parsing, apply) lives in `app/ai.py`, not
duplicated in `ai_tasks.py`.

## `app/task_status.py` — background-task polling

Shared infrastructure backing the "runs in a background thread, poll for progress" pattern used by
kit generation, triage, search pulls, and AI analysis (adopted after a class of gunicorn-timeout
bugs from running slow AI/search calls on the request thread — see CHANGELOG REL-01 and the
`settings_provider_pull` fix in [0.8.1]).

| Method & path | Function | Notes |
|---|---|---|
| `GET /ai/task/<run_id>/status` | `task_status_view` | Render the task-status page for a run. |
| `GET /ai/task/<run_id>/poll` | `task_poll` | JSON poll endpoint the status page calls. |

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

## `app/db_utils.py` — transient SQLite retry helper

`with_db_retry(fn, attempts=3, base_delay=0.15)` and `commit(attempts=3, base_delay=0.15)` retry a
DB operation a couple of times with backoff on a transient `disk I/O error` / `database is
locked` `OperationalError` (`_is_transient()`), which can surface under WAL-mode concurrent access
on some bind-mount filesystem bridges (e.g. OrbStack/Docker Desktop on macOS) with nothing
actually wrong with the data. `commit()` is the drop-in replacement for `db.session.commit()` used
throughout the app.

## `app/deploy.py` — `DEPLOY_MODE` preset resolution

`DEPLOY_MODE` is a convenience preset over the granular flags `create_app()` actually reads
(`trust_proxy`, `secure_cookie`) — the app never branches on the mode string itself.
`resolve_deploy_mode()` / `resolve_deploy_flags()` compute those flags (explicit env vars always
win). `apply_proxy_trust(app, trust_proxy)` wires `ProxyFix`. `evaluate_startup_guard()` /
`enforce_startup_guard()` refuse to boot in an unsafe combination (e.g. secure cookies without
HTTPS in front); `format_issue()` renders one guard failure for the startup log.

## `app/docgen.py` — Markdown → .docx

`markdown_to_docx_bytes(markdown_text)` renders just the Markdown subset the kit prompts actually
produce (`#`/`##`/`###` headings, `-`/`*`/numbered lists, `**bold**`) into a `.docx` via
python-docx; anything else degrades to a plain paragraph rather than failing.

## `app/kit_export.py` — ATS cleaning and PDF export for saved kits

Runs whenever a kit is saved (MCP `save_kit` or an API build): `ats_clean(text)` replaces
Unicode punctuation that ATS scanners choke on (smart quotes, em/en dashes, fancy bullets) with
plain ASCII; `extract_sections(kit_markdown)` splits a kit into its Tailored Resume / Cover Letter
sections; `render_pdf(title, body)` renders a dependency-free plain-text PDF (own minimal
`_assemble_pdf`/`_content_stream`, no reportlab). `sync_kit_attachments(job)` runs both and attaches
the PDF to the job record alongside the existing `.docx` kit attachment (`app/ai.py`).

## `app/resume_convert.py` — deterministic resume upload → Markdown

`convert_to_markdown(data, ext)` powers the "Base Resume" upload path
(`app/main.py:settings_asset_upload`) without requiring any AI provider: `.docx` gets real
structure (headings, bold/italic, lists, simple tables) via python-docx; `.pdf` gets plain
extracted text only; `.txt`/`.md` pass through unchanged. Best-effort by design — meant to be
reviewed and hand-edited on the Getting Started profile step, not a pixel-perfect round trip.

## `app/onboarding.py` — Getting Started walkthrough

A persistent, re-entrant checklist (docs/PLAN-onboarding.md, Phase 1) rather than a one-shot
wizard: `build_checklist()` / `checklist_for_dashboard()` derive completion from real data (a
resume exists, targets are set, a search has run) instead of a stored flag, so the checklist can't
drift from reality. `get_onboarding_redirect()` sends a fresh admin to the next incomplete step.
Step views mostly post to existing Settings routes (via their `next` field) so there's one save
path per setting. `save_resume_draft(resume_markdown, profile_facts, created_by)` upserts the
single "Resume" candidate asset from the resume-interview routine (also exposed as the MCP tool of
the same name) and optionally folds `profile_facts` into the candidate profile.

## `app/privacy.py` — PII/SPI redaction and rehydration

Every AI transmission path routes through here. `redact(text, strict=None, strip_spi=True)`
replaces identifiers (names, emails, phones, addresses, SSNs, work-authorization statements) with
deterministic placeholders like `{{PII:EMAIL_3f2a1c8b}}` (`make_placeholder`) before anything
reaches a provider; `rehydrate(text, mapping)` swaps them back in the results. `scan_spi(text)` /
`_strip_spi(text)` remove SPI/PHI (health info, age, marital status) outright rather than
tokenizing it, and surface it as coaching flags instead. Optional strict mode
(`collect_known_values()`, `_strict_values()`) additionally pseudonymizes employer/org names and
locations. `redact_obj`/`rehydrate_obj` recurse through dicts/lists for structured payloads.
`should_redact_for(provider_row)` / `is_local_provider()` decide whether a given AI call needs
redaction at all (local/Ollama providers can skip it per `redact_local()`).

## `app/prompts.py` — Claude Pro routine prompts

Generates the copy-ready prompt text for every routine slot and per-job action (morning briefing,
triage, kit queue, follow-up drafts, weekly review, interview prep, rejection analysis, the resume
interview/builder) for a user to paste into Claude Pro with the MCP connector active. Each prompt
names every MCP tool to call, in order, and always specifies the write-back tool so results land
back in Job Squire — no clarifying questions, no em-dashes or AI-tell phrasing.

## `app/sample_locations.py` — placeholder location text

`random_sample_city()` returns an illustrative "City, ST" example for empty-field/validation
copy — never used for search logic. US-only for now, matching the app's strict location
validation (`settings_search()` in `main.py`).

## `app/websearch.py` — best-effort DuckDuckGo research

`ddg_search(query, max_results=4, timeout=10)` scrapes `html.duckduckgo.com/html/` (the only free,
keyless option — there's no official DuckDuckGo search API), and `research_company_and_salary(...)`
builds on it for kit generation. Both are defensive: any failure (network error, layout change,
rate limit, empty results) returns an empty result instead of raising, so a research hiccup never
blocks kit generation — the kit just builds without that extra context.

## `app/backup.py` — in-app backup download

`build_backup_archive(data_dir, upload_dir, include_env=True)` builds the same WAL-safe `.tgz`
(DB snapshot + `uploads/` + `candidate_profile.md` + `oauth_tokens.json` + optionally `.env`) that
`scripts/backup.sh` produces, so a browser-downloaded archive restores with the existing
`scripts/restore.sh` with no format differences. Restore is deliberately **not** an in-app HTTP
action — a safe restore requires stopping the container before its data is replaced, which the
container can't do to itself; that's `scripts/restore.sh`'s job (see docs/backup-restore.md).

## `app/backup_cli.py` — container-side backup entrypoint

`main()`, run as `python -m app.backup_cli` inside the running container (via `docker exec`/
`podman exec`), for job_squire_cli's `job-squire backup` when `/data` is a named volume rather
than a host bind mount and the CLI can't read a WAL-safe DB snapshot straight off the host
filesystem. Not a Flask route — no request context or auth needed since the CLI already controls
access via the exec itself. See `job_squire_cli/ops/backup.py`'s docstring for the full picture.

## `app/mcp_auth.py` — static MCP bearer token

The sanctioned escape hatch for MCP clients that can't complete OAuth's browser redirect (scripts,
`jobsquire-cli`, `mcp-remote` bridges): `generate_token()` produces 256 bits of URL-safe base64,
prefixed `jsq_mcp_`; `verify_static_token(bearer, stored_encrypted, secret_key, deploy_mode, ...)`
constant-time compares it. Stored Fernet-encrypted in `AIConfig.mcp_api_key_enc`, never plaintext.
`is_network_reachable(deploy_mode)` / `is_static_token_allowed(deploy_mode, allow_network)` enforce
the loopback-only-by-default rule.

## `app/mcp_token_cli.py` — container-side MCP token entrypoint

`main()`, run as `python -m app.mcp_token_cli` inside the running container, fed a JSON request on
stdin, for `job-squire configure NAME --mcp-token ...` (`job_squire_cli`'s `ops/mcp_token.py`) —
same "named volume, not a host bind mount" reasoning as `app/backup_cli.py`. Hand-maintains its own
copy of `AIConfig`'s column defaults (`_FRESH_ROW_DEFAULTS`) since this module has no SQLAlchemy
model to introspect, kept in lockstep with `ops/mcp_token.py`'s own tests.

## `app/ollama_provider_cli.py` — container-side Ollama provider entrypoint

`main()`, run as `python -m app.ollama_provider_cli` inside the running container, for
`job-squire ollama setup` (`job_squire_cli`'s `ops/ollama_assist.py`) — same reasoning as
`app/backup_cli.py`. `write_provider_row(db_path, payload)` performs the actual
`ai_provider_configs` write.

## `app/secrets_copy_cli.py` — container-side settings-copy entrypoint

`main()`, run as `python -m app.secrets_copy_cli` inside the running container, for
`job-squire create --import-from` (`job_squire_cli`'s `ops/secrets_copy.py`). `handle_request()`
dispatches `dump` (read-only SELECT, run against the *source* instance's container) and `apply`
(run against the *destination*'s), letting `copy_db_settings` move settings between instances
without either side reading the other's `/data` off the host.

## `app/mcp_server.py` — remote MCP server

See [mcp-connector.md](mcp-connector.md) for the full picture. In brief: a `FastMCP` server
(Streamable HTTP) wrapped by `asgi_app`, which handles the **OAuth 2.0** endpoints
(`/.well-known/...`, `/oauth/register|authorize|token`), serves a login page as the authorization
step, issues 30-day Bearer tokens (in-memory, PKCE-verified), and gates the `/mcp` endpoint on a
valid token. A static API key (`Authorization: Bearer`, `app/mcp_auth.py`) is also accepted for
non-browser clients, loopback-only by default -- there is no token-in-path route. `/health` is
open. `main()` runs uvicorn on `MCP_PORT` (9000). Reuses the Flask app context for DB access;
DNS-rebinding protection allowlists `PUBLIC_MCP_HOST`.

The 24 tools span reads and writes. Core tools: `get_pipeline`, `list_jobs`, `get_job`,
`get_candidate_profile`, `save_candidate_profile`, `save_resume_draft`, `get_candidate_assets`,
`add_jobs`, `get_search_targets`, `save_analysis`, `get_kit_instructions`, `update_job_notes`,
`save_kit`, `set_follow_up`, `list_contacts`, `get_contact`, `add_contact`, and `log_submission`.
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
