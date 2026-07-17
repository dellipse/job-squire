# API Reference

The Job Squire exposes two programmatic interfaces: a **REST ingest endpoint** for pushing jobs in from external sources, and a **remote MCP server** that Claude connects to as a custom connector.

---

## Ingest API

### `POST /api/ingest`

Push a batch of job postings into Job Squire. Jobs are deduplicated by `external_id` and stored with status `Saved`.

**Authentication.** Include the `INGEST_API_KEY` as a Bearer token:

```
Authorization: Bearer <INGEST_API_KEY>
```

The endpoint is disabled (returns 404) unless `INGEST_API_KEY` is set in `data/.env`. The comparison is constant-time to prevent timing attacks.

**Request body.** JSON array of job objects:

```json
[
  {
    "title":       "Supply Chain Analyst",
    "company":     "Acme Corp",
    "location":    "Columbus, OH",
    "url":         "https://example.com/jobs/42",
    "description": "Full job description text...",
    "source":      "external-claude",
    "external_id": "acme-42",
    "salary":      "$70,000 - $90,000",
    "work_mode":   "Hybrid",
    "posted_date": "2026-06-20"
  }
]
```

All fields except `title` are optional. `external_id` is used for deduplication -- jobs with a matching `external_id` are skipped.

**Response:**

```json
{
  "created": 3,
  "skipped": 1,
  "total":   4
}
```

**Example:**

```bash
curl -s -X POST https://squire.yourdomain.com/api/ingest \
  -H "Authorization: Bearer <INGEST_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '[{"title":"Analyst","company":"Acme","url":"https://example.com/jobs/1","external_id":"acme-1"}]'
```

---

## MCP Connector

The MCP server (`app/mcp_server.py`) runs as one of three s6-supervised processes inside Job Squire's single container (alongside web and worker) and is reached at `https://mcp-squire.<domain>`. It exposes 23 tools over Streamable HTTP (uvicorn + FastMCP).

**Authentication.** OAuth 2.0 Authorization Code flow with PKCE. Claude handles the handshake automatically when the connector is added. The user signs in with their Job Squire credentials on the OAuth page; Claude stores a 30-day Bearer token. Tokens are persisted to `DATA_DIR/oauth_tokens.json`.

**Health check.** `GET /health` returns `{"ok": true}` with no authentication required.

---

### Read Tools

#### `get_pipeline`

Returns the full job pipeline including interview debriefs. Used for analysis routines.

```
get_pipeline() -> dict
```

Response contains all jobs with their statuses, AI scores, notes, and any interview records.

---

#### `list_jobs`

Returns jobs filtered by status. Omit `status` to return all jobs.

```
list_jobs(status: str = "") -> list
```

Valid status values: `Saved`, `Applied`, `Phone Screen`, `Interview`, `Final Interview`, `Offer`, `Hired`, `Rejected`, `Withdrawn`, `Ghosted`, `Pass`.

---

#### `get_job`

Returns full detail for one job including interview debriefs and notes.

```
get_job(job_id: int) -> dict
```

---

#### `get_candidate_profile`

Returns the candidate's master profile from `candidate_profile.md`.

```
get_candidate_profile() -> str
```

---

#### `get_candidate_assets`

Returns the document library (base resume, rec letters, certs, portfolio). Text/Markdown content is inlined; binary files return metadata only.

```
get_candidate_assets(kind: str = "") -> list
```

Valid `kind` values: `Resume`, `Cover Letter`, `Recommendation Letter`, `Certificate`, `Portfolio`, `Other`.

---

#### `get_search_targets`

Returns the configured job titles and search location. Used by Claude when searching for new jobs with its own connectors.

```
get_search_targets() -> dict
```

---

#### `get_kit_instructions`

Returns the full application-kit prompt template. Used by Claude to know what to produce when building a kit.

```
get_kit_instructions() -> str
```

---

#### `list_contacts`

Returns the recruiter/contact list, optionally filtered by type.

```
list_contacts(contact_type: str = "") -> list
```

Valid `contact_type` values: `Recruiter`, `Hiring Manager`, `Networking`, `Staffing Agency`, `Other`.

---

#### `get_contact`

Returns one contact with their full submission history.

```
get_contact(contact_id: int) -> dict
```

---

#### `list_unanalyzed_jobs`

Returns `Saved` jobs that have no AI fit score yet (up to 50). Includes a description snippet so triage can score without a separate `get_job` call per job.

```
list_unanalyzed_jobs(limit: int = 20) -> list
```

---

#### `list_overdue_followups`

Returns active jobs and recruiter submissions where a follow-up date has passed and no draft exists yet. Returns two keys: `jobs` and `submissions`.

```
list_overdue_followups() -> dict
```

---

#### `get_weekly_summary`

Returns a summary of pipeline activity over the past 7 days: new jobs added, status changes, interviews completed, and recent AI insights. Used by the Weekly Strategy Review routine.

```
get_weekly_summary() -> dict
```

---

### Write Tools

#### `save_candidate_profile`

Saves an updated master profile back to Job Squire. Overwrites `candidate_profile.md` in `DATA_DIR`.

```
save_candidate_profile(profile_markdown: str) -> dict
```

---

#### `add_jobs`

Pushes one or more found jobs into Job Squire as `Saved`. Deduplicated via `external_id` (same as the ingest API). Used by Claude when searching with its own connectors.

```
add_jobs(jobs: list) -> dict
```

Each job object in the list follows the same schema as the ingest API request body.

Response: `{"created": N, "skipped": N, "total": N}`.

---

#### `save_analysis`

Writes pipeline analysis back to Job Squire: a global insight and optional per-job notes.

```
save_analysis(
  overall_summary:   str,
  recommendations:   str,
  jobs:              list   # [{job_id, notes, fit_score?}]
) -> dict
```

---

#### `update_job_notes`

Replaces the notes/description on a job record — typically used to save the full posting text fetched from its URL in place of a short imported snippet. Strip navigation, ads, and other page chrome before saving.

```
update_job_notes(job_id: int, notes: str) -> dict
```

---

#### `save_kit`

Saves a completed application kit onto a job record. Also auto-triggers ATS keyword gap analysis if API mode is active.

```
save_kit(job_id: int, kit_markdown: str) -> dict
```

---

#### `set_follow_up`

Sets a follow-up reminder N calendar days from today.

```
set_follow_up(job_id: int, days_out: int = 6) -> dict
```

---

#### `add_contact`

Adds a recruiter, hiring manager, or networking contact.

```
add_contact(
  name:         str,
  agency:       str = "",
  contact_type: str = "Recruiter",
  email:        str = "",
  phone:        str = "",
  linkedin_url: str = "",
  notes:        str = ""
) -> dict
```

---

#### `log_submission`

Records that a recruiter submitted the candidate for a role. Optionally links to an existing contact and/or job record.

```
log_submission(
  contact_id:     int    = 0,
  company:        str    = "",
  role_title:     str    = "",
  job_id:         int    = 0,
  status:         str    = "Submitted",
  submitted_date: str    = "",
  notes:          str    = ""
) -> dict
```

Valid `status` values: `Submitted`, `Screening`, `Interviewing`, `Offer`, `Placed`, `Rejected`, `Withdrawn`, `No Response`.

---

#### `set_job_fit`

Saves an AI fit score (1-10) and brief reasoning to a job record. Used by the auto-triage routine.

```
set_job_fit(job_id: int, score: int, reason: str) -> dict
```

---

#### `save_followup_draft`

Saves an AI-drafted follow-up email (including subject line) to a job record.

```
save_followup_draft(job_id: int, email_text: str) -> dict
```

---

#### `save_interview_prep`

Saves an AI-generated interview prep guide to the most recent interview record for a job. Falls back to job notes if no interview record exists yet.

```
save_interview_prep(job_id: int, prep_notes: str) -> dict
```

---

## Typical Tool Chains

**Application kit (per job):**

```
get_kit_instructions
get_candidate_profile
get_job(job_id)
get_candidate_assets
  -> build resume, cover letter, emails
save_kit(job_id, kit_markdown)
set_follow_up(job_id, days_out=6)
```

**Pipeline analysis:**

```
get_pipeline
  -> analyze fit, patterns, next actions
save_analysis(overall_summary, recommendations, jobs)
```

**New job triage:**

```
list_unanalyzed_jobs
  -> score each job 1-10
set_job_fit(job_id, score, reason)  [repeat per job]
```

**Job search (supplemental via Claude):**

The Muse, ZipRecruiter, Google Jobs (SerpApi), and other sources are available as direct sources in
Settings → Sources and run automatically on the scheduler — no MCP call needed for standard
discovery. The `add_jobs` tool remains available for Claude to push additional results when used
through the "Search jobs in Claude" button.

```
get_search_targets
  -> Claude searches supplemental boards or custom queries
add_jobs(found_jobs)
```

**Overdue follow-ups:**

```
list_overdue_followups
  -> draft follow-up emails
save_followup_draft(job_id, email_text)  [repeat per job]
```
