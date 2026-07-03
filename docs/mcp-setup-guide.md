# MCP Setup Guide

JobSquire exposes a **remote MCP server** that any MCP-capable agent can connect to as a custom connector. Once connected, the agent can read and write your job-search pipeline live: pull job listings, score opportunities, build application kits, log contacts, set follow-up reminders, and more.

This guide covers the three supported connection methods:

1. **Claude Pro** — OAuth 2.0 PKCE, browser-based sign-in, no API key required
2. **Hermes Agent** — static Bearer key, local agent loop
3. **OpenClaw** — static Bearer key, self-hosted gateway for chat apps (Telegram, WhatsApp, iMessage, Discord)

---

## Prerequisites

Before connecting any agent, confirm:

1. The `job-squire-mcp` container is running.
2. `PUBLIC_MCP_URL` is set in `data/.env` to the publicly reachable HTTPS base URL of the MCP service, e.g.:
   ```
   PUBLIC_MCP_URL=https://mcp-squire.yourdomain.com
   ```
3. The MCP connector is enabled in **Settings → AI → MCP Connector**.
4. A health check returns OK:
   ```bash
   curl https://mcp-squire.yourdomain.com/health
   # expect: {"ok": true}
   ```

The MCP server listens on port 9000 (internal). SWAG proxies it over HTTP/1.1 (`http2 off` — required because MCP streaming breaks nginx HTTP/2 framing).

---

## Method 1: Claude Pro (OAuth 2.0)

Claude's connector handshake uses **OAuth 2.0 Authorization Code flow with PKCE**. No API key or token generation needed — you sign in with your JobSquire credentials.

### Steps

1. In Claude, go to **Settings → Connectors → Add custom connector**.
2. Paste the base URL: `https://mcp-squire.yourdomain.com` (no path, no token).
3. Give the connector a name (e.g. `JobSquire`). Copy this name exactly — you will need it in Settings.
4. Claude opens an authorization page served by the MCP server. Enter the JobSquire **user** account credentials (not the admin account, not your Claude password).
5. Claude completes the handshake. The connector shows as active.

Back in JobSquire: open **Settings → AI → MCP Connector** and paste the connector name into the **Connector name** field. This name is used to phrase the "Open in Claude" deep-link prompts and the five scheduled routine prompts.

### What happens under the hood

- Claude queries `/.well-known/oauth-authorization-server` for endpoint discovery.
- Claude registers a client at `/oauth/register` (dynamic client registration).
- Claude redirects the browser to `/oauth/authorize`, which serves a login page.
- After sign-in, Claude exchanges the code at `/oauth/token` for a 30-day Bearer token.
- Every tool call sends `Authorization: Bearer <token>` to `/mcp`.

Tokens are persisted to `DATA_DIR/oauth_tokens.json`. A container restart does not invalidate them. If the token file is lost or corrupted, remove and re-add the connector in Claude (~60 seconds).

### Session watch

To verify the connection while adding the connector:

```bash
docker logs -f job-squire-mcp
```

You should see: discovery → register → authorize → token → `ListToolsRequest 200`.

---

## Method 2: Hermes Agent (static Bearer key)

[Hermes Agent](https://github.com/NousResearch/hermes-agent) is an open-source local agent loop. It connects via a static API key instead of OAuth.

### Generate a static key

In **Settings → AI → MCP Connector**, click **Generate static API key**. Copy the key shown — it is stored encrypted and not displayed again.

### Install Hermes Agent

```bash
pip install hermes-agent
```

### Configure

Create or edit `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  jobsquire:
    url: "https://mcp-squire.yourdomain.com/mcp"
    headers:
      Authorization: "Bearer ${JOB_SQUIRE_KEY}"
    timeout: 30
    connect_timeout: 10
    enabled: true
```

Create `~/.hermes/.env` and add your key:

```
JOB_SQUIRE_KEY=your-static-key-here
```

Hermes substitutes `${VAR}` references from this file at startup, keeping the key out of the config file.

### Start

```bash
hermes chat
```

Once running, type `/reload-mcp` if you changed the config after starting. Example interactions:

```
You: What jobs are currently in my pipeline?
You: Score the unanalyzed jobs in my Saved list.
You: Draft follow-up emails for overdue applications.
You: Add these three jobs I found: [paste JSON]
```

### Limitations

- No "Open in Claude" buttons (those are UI elements in Job Squire, not agent features).
- Hermes does not run on a scheduler — interactions are on-demand in the chat loop.
- The static key grants full read/write access; treat it like a password.

---

## Method 3: OpenClaw (static Bearer key)

[OpenClaw](https://openclaw.ai/) is a self-hosted gateway that connects your AI stack to messaging apps — Telegram, WhatsApp, iMessage, Discord, and others. Use this if you want to interact with your pipeline from your phone via a chat app.

### Install

```bash
npm install -g openclaw@latest
```

### Onboard

```bash
openclaw onboard
```

The onboarding wizard walks you through connecting your first chat app and generating your config.

### Configure the MCP connection

Add the JobSquire server to `~/.openclaw/openclaw.json`:

```json
{
  "mcp": {
    "servers": {
      "jobsquire": {
        "url": "https://mcp-squire.yourdomain.com/mcp",
        "headers": {
          "Authorization": "Bearer your-static-key-here"
        }
      }
    }
  }
}
```

Use the same static key generated in **Settings → AI → MCP Connector → Generate static API key**.

### Use via Telegram (example)

In your connected Telegram channel, send natural-language commands:

```
What's in my pipeline this week?
Are there any jobs overdue for a follow-up?
Build an application kit for job ID 42.
```

You can restrict which Telegram users can trigger OpenClaw commands by adding `allowFrom` entries to the channel config — see the OpenClaw docs for access control.

### Limitations

- Static key grants full read/write access; treat it like a password.
- No OAuth flow — the key does not expire automatically (revoke it by regenerating in Settings).
- OpenClaw does not run the five Claude Pro scheduled routines; those are Claude-specific.

---

## Available tools (22)

All tools are available to every connection method.

### Read tools

| Tool | What it returns |
|---|---|
| `get_pipeline` | Full pipeline with interview debriefs — use this for analysis. |
| `list_jobs` | All jobs, optionally filtered by status. |
| `get_job` | Full detail for one job including debriefs. |
| `get_candidate_profile` | The master candidate profile (Markdown). |
| `get_candidate_assets` | Master documents (resume, letters, certs); inlines text content. |
| `get_search_targets` | Target job titles and location — tells the agent what to search. |
| `list_contacts` | Recruiter/contact list, optionally filtered by type. |
| `get_contact` | One contact with full submission history. |
| `list_unanalyzed_jobs` | Saved jobs with no AI fit score yet (up to 50); includes description snippet. |
| `list_overdue_followups` | Active jobs and submissions where follow-up date has passed and no draft exists. |
| `get_weekly_summary` | Pipeline activity over the past 7 days: new jobs, status changes, interviews, AI insights. |

### Write tools

| Tool | What it does |
|---|---|
| `save_candidate_profile` | Save an updated master profile back to Job Squire. |
| `add_jobs` | Push found jobs into Job Squire as `Saved` (deduplicated). |
| `save_analysis` | Write analysis back: global insight + per-job notes. |
| `save_kit` | Save a completed application kit to a job record. |
| `set_follow_up` | Set a follow-up reminder N calendar days out (default 6). |
| `add_contact` | Add a recruiter or networking contact. |
| `log_submission` | Record that a recruiter submitted for a role. |
| `set_job_fit` | Save an AI fit score (1-10) and reasoning to a job record. |
| `save_followup_draft` | Save an AI-drafted follow-up email (subject + body) to a job. |
| `save_interview_prep` | Save an AI-generated interview prep guide to the most recent interview record. |

All tools run against the shared SQLite database. Writes are committed immediately and visible in Job Squire UI on the next page load.

---

## Security notes

- The OAuth token (Claude Pro) and static key (Hermes, OpenClaw) both grant full read/write access to the pipeline. There is no per-tool permissioning.
- Keep the MCP subdomain behind TLS. The MCP server itself listens on plain HTTP internally; SWAG terminates TLS.
- DNS-rebinding protection is enabled: the server allowlists `PUBLIC_MCP_HOST` and rejects requests from other hostnames.
- Revoke OAuth access by removing the connector in Claude. Revoke static key access by generating a new key in Settings (the old key is immediately invalidated).
- Only the **user** account (not admin) can authorize through OAuth. The static key path bypasses this restriction — use it only in trusted, private environments.

---

## Troubleshooting

**Health check fails (`curl .../health` returns nothing or an error):**
Confirm the container is running (`docker compose ps`) and `PUBLIC_MCP_URL` is set. Check `docker logs job-squire-mcp` for startup errors.

**Claude shows "connector unavailable" or can't complete OAuth:**
The URL must be HTTPS with a valid cert. Try the health check first. Watch `docker logs -f job-squire-mcp` while re-adding the connector to see where the handshake fails.

**Static key returns 401:**
The key may have been regenerated. Copy the current key from **Settings → AI → MCP Connector** and update your agent config.

**Tools return empty results or stale data:**
All tools query the live database. If results look stale, the container may be running against a different data volume — check `DATA_DIR` in `data/.env`.

**Hermes `connect_timeout` errors:**
The MCP server may be cold-starting. Increase `connect_timeout` in `config.yaml` or confirm the container is already running before starting Hermes.
