# MCP Connector

The Job Squire can run as a **remote MCP server** so Claude (or any MCP-capable agent) can read and write
the pipeline live. The MCP connector is enabled independently of Automatic Features — both can be
active at the same time. Custom connectors via remote MCP are supported on Claude
Free/Pro/Max/Team/Enterprise (launched March 2026).

## How it works

- `app/mcp_server.py` runs a `FastMCP` server over **Streamable HTTP** (uvicorn, port 9000), in
  its own container `job-squire-mcp`.
- SWAG proxies `mcp-squire.yourdomain.com` → `job-squire-mcp:9000`, over **HTTP/1.1**
  (`http2 off`, because MCP streaming/SSE breaks nginx HTTP/2).
- **Auth is OAuth 2.0 Authorization Code with PKCE**, which is what Claude's connector handshake
  requires. The user adds the **base URL** (`https://mcp-squire.<domain>`, no path, no token).
  Claude discovers the OAuth endpoints, registers a client, and opens a login page the MCP server
  serves; the user signs in with their **Job Squire** credentials, Claude exchanges the code for a
  **30-day Bearer token**, and sends it on every call. The `asgi_app` wrapper handles the
  `/.well-known/...` discovery, `/oauth/register|authorize|token`, and gates `/mcp` on a valid
  token. `/health` is open and returns `{"ok": true}`.
- OAuth clients/codes/tokens are kept **in memory**, so a container restart invalidates them —
  re-authorizing takes ~10 seconds. Only the **user** account (role `user`) may authorize.
- A **legacy token-in-path** route (`/mcp/<token>`, token from `AIConfig.mcp_token_enc`) is still
  accepted as a fallback for any older API-mode callers, but new connectors use OAuth.
- DNS-rebinding protection is on; the public MCP host (`PUBLIC_MCP_HOST`) is allowlisted.

## Tools exposed

22 tools total across reads and writes.

### Core tools

| Tool | Signature | Does |
|---|---|---|
| `get_pipeline` | `() -> dict` | The full pipeline + interview debriefs (for analysis). |
| `list_jobs` | `(status="") -> list` | Jobs, optionally filtered by status. |
| `get_job` | `(job_id) -> dict` | Full detail for one job incl. debriefs. |
| `get_candidate_profile` | `() -> str` | User's master profile (from `candidate_profile.md`). |
| `save_candidate_profile` | `(profile_markdown) -> dict` | Save an updated master profile back to Job Squire. |
| `get_candidate_assets` | `(kind="") -> list` | List master documents (resume, rec letters, certs); inlines text/markdown content, returns metadata for binaries. |
| `get_search_targets` | `() -> dict` | Target titles + location, so Claude knows what to search. |
| `add_jobs` | `(jobs) -> dict` | Push found jobs into Job Squire as `Saved` (deduped via `ingest_jobs`). |
| `save_analysis` | `(overall_summary, recommendations, jobs) -> dict` | Write analysis back (global insight + per-job notes). |
| `get_kit_instructions` | `() -> str` | Return the full step-by-step application-kit prompt (`KIT_PROMPT`). |
| `save_kit` | `(job_id, kit_markdown) -> dict` | Save a completed application kit back onto a job record. Also auto-triggers ATS gap analysis if API mode is active. |
| `set_follow_up` | `(job_id, days_out=6) -> dict` | Set a follow-up reminder N calendar days out. |
| `list_contacts` | `(contact_type="") -> list` | Recruiter/contact list, optionally filtered by type. |
| `get_contact` | `(contact_id) -> dict` | One contact with their full submission history. |
| `add_contact` | `(name, agency="", contact_type="Recruiter", ...) -> dict` | Add a recruiter/contact. |
| `log_submission` | `(contact_id=0, company="", role_title="", job_id=0, status="Submitted", submitted_date="", notes="") -> dict` | Record that a recruiter submitted User for a role. |

### Phase 2: Routine support tools

These tools support the automated and semi-automated routines (triage, follow-up drafts, interview prep, weekly review).

| Tool | Signature | Does |
|---|---|---|
| `list_unanalyzed_jobs` | `(limit=20) -> list` | Return `Saved` jobs with no AI fit score yet (up to 50). Includes description snippet so triage can score without a separate `get_job` call per job. |
| `set_job_fit` | `(job_id, score, reason) -> dict` | Save an AI fit score (1-10) and brief reasoning to a job record. |
| `list_overdue_followups` | `() -> dict` | Return all active jobs and recruiter submissions where a follow-up date has passed and no draft exists yet. Returns two lists: `jobs` and `submissions`. |
| `save_followup_draft` | `(job_id, email_text) -> dict` | Save an AI-drafted follow-up email (including subject line) to a job record. |
| `save_interview_prep` | `(job_id, prep_notes) -> dict` | Save an AI-generated interview prep guide to the most recent interview record for a job. Falls back to job notes if no interview record exists yet. |
| `get_weekly_summary` | `() -> dict` | Return a summary of pipeline activity over the past 7 days: new jobs added, status changes, interviews completed, and recent AI insights. Used by the Weekly Strategy Review routine. |

Reads: `get_*`, `list_*`, `get_weekly_summary`. Writes: `save_candidate_profile`, `add_jobs`, `save_analysis`, `save_kit`, `set_follow_up`, `add_contact`, `log_submission`, `set_job_fit`, `save_followup_draft`, `save_interview_prep`.

All tools run inside a Flask app context against the shared SQLite DB. The application-kit flow chains: `get_kit_instructions` → build documents → `save_kit` + `set_follow_up`.

## Setup in Claude

### Admin steps (one-time)

1. Confirm the MCP service is running:
   ```
   curl https://mcp-squire.<domain>/health
   # expect: {"ok": true}
   ```
2. Confirm `PUBLIC_MCP_URL` is set in `data/.env` (e.g. `PUBLIC_MCP_URL=https://mcp-squire.yourdomain.com`),
   and `PUBLIC_MCP_HOST` matches the public hostname if it differs from the default.
3. In Job Squire: **Settings → AI tab → MCP Connector** → enable the connector and save. (No token
   to generate — auth is OAuth.)
4. Give the user the **base connector URL**: `https://mcp-squire.<domain>` (no path, no token).

### User steps

1. Sign in to [claude.ai](https://claude.ai) → profile icon → **Settings → Connectors**.
2. Click **Add custom connector**, paste the base URL, give it a name ("JobSquire").
3. Claude opens an **Authorize** page served by the MCP server. Enter the JobSquire
   username and password and click **Authorize**.
4. Claude completes the OAuth handshake and shows the connector as active.

**Auth mechanism:** OAuth 2.0 Authorization Code flow with PKCE (required by the MCP 2025 spec /
Claude's connector handshake). The Job Squire login page IS the OAuth authorization step. Tokens are
in-memory and valid for 30 days; the container must be running for them to persist. After a
restart or expiry, the user removes and re-adds the connector (~60 seconds).

**Confirmed working** with claude.ai as of June 2026. Verified via `docker logs job-squire-mcp`:
full OAuth flow (discovery → register → authorize → token) followed by live `ListToolsRequest`,
`ListResourcesRequest`, and `ListPromptsRequest` all returning 200.

## The "Open in Claude" buttons

In MCP mode the UI shows buttons that deep-link a pre-filled chat (`https://claude.ai/new?q=...`)
so Claude acts through the connector:

- **AI tab → "Open in Claude to analyze"** → Claude reads the pipeline and calls `save_analysis`.
- **Job page → "Build kit in Claude"** and **Kit generator → "Build kit in Claude"** → Claude
  pulls the profile + job and drafts resume/cover letter/emails. (On the kit page the button reads
  the current form fields via `app.js`.)
- **Jobs list → "Search jobs in Claude"** → Claude runs a supplemental search using your saved
  targets and calls `add_jobs` to push finds back. Indeed has a published Claude connector, so
  Claude can search it directly in this session and push results in via `add_jobs`. Dice,
  ZipRecruiter, Google Jobs/SerpApi, and others also run automatically on the scheduler via
  Settings → Sources.

The prompts enforce the writing style rules (no em-dashes, no AI-tell phrasing, only real facts).

## Security notes

- Access requires an OAuth sign-in with the **user** account's Job Squire credentials; Claude holds a
  30-day Bearer token. Revoke access by removing the connector in Claude (tokens also drop on a
  container restart).
- Served only over HTTPS via SWAG. The server itself listens on plain HTTP on the internal
  network, with DNS-rebinding protection allowlisting `PUBLIC_MCP_HOST`.
- Reads expose User's full pipeline, profile, and documents; writes can add jobs, analysis, kits,
  contacts, and submissions. There is no per-tool permissioning, so the connector should only be
  added in the user's own Claude.
- The legacy `/mcp/<token>` path remains as a fallback; treat that token like a password if used.

## Testing without Claude

- Health: `curl https://mcp-squire.<domain>/health`.
- The tools can be exercised in Python against the FastMCP instance
  (`await mcp.call_tool("get_search_targets", {})`), which is how they were unit-tested. The
  claude.ai connector handshake itself can only be verified by adding the connector and watching
  `docker logs -f job-squire-mcp` for incoming tool calls.
