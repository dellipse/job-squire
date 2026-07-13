# Using Claude Pro

Claude Pro subscribers can connect Job Squire directly through the Model Context Protocol (MCP). No Anthropic API key is required — you use your existing Claude Pro subscription.

The MCP connector and Automated Features are independent. You can enable one, the other, or both at the same time.

---

## What this gives you

The MCP connector gives you live, interactive access to your job pipeline from Claude's chat interface. Claude can read your full pipeline, contacts, and candidate profile, and write results back — scores, kit output, follow-up drafts, interview prep notes — without any exporting or copy-pasting.

Compare this to Automated Features, which run in the background on a schedule whether or not you have Claude open:

| | MCP connector (Claude Pro) | Automated Features (API providers) |
|---|---|---|
| Auto-triage new jobs | Yes, Routine 2 | Yes, after every search run |
| Follow-up drafts | Yes, Routine 4 | Yes, daily at 6 AM |
| Weekly strategy review | Yes, Routine 5 | Yes, every Monday |
| Morning briefing | Yes, Routine 1 | No |
| Application kit queue | Yes, Routine 3 | No |
| Requires Anthropic API key | No | No (works with any provider) |
| Runs without opening Claude | Yes (scheduled routines) | Yes |
| Per-job action buttons | Yes | No |

---

## Prerequisites

- A Claude Pro subscription
- Job Squire running with `PUBLIC_MCP_URL` set and the MCP container up (confirm with your admin)
- The MCP connector enabled in **Settings → AI → MCP Connector**

---

## Connecting Claude to Job Squire

Claude connects via OAuth. Authentication is handled automatically.

1. In Job Squire, open **Settings → AI → MCP Connector** and note the connector URL shown there. Also note the **Connector name** field — you will need it to match exactly in Claude.
2. Open [claude.ai](https://claude.ai) and go to your profile → **Settings** → **Connectors**.
3. Click **Add custom connector**.
4. Paste the connector URL from Step 1.
5. Give it the exact name shown in the Connector name field in Job Squire (default: `job-squire`).
6. Click **Connect**, sign in with your Job Squire credentials, then **Authorize**.

The connector name in Claude and the Connector name field in Settings must match exactly. The routine prompts use this name to locate your connector. If you rename it in either place, update the other.

**Token expiry:** Access tokens last 30 days. To renew, remove the connector from Claude's Settings → Connectors and re-add it. The OAuth flow takes about a minute.

---

## The five routines

Routines are scheduled prompts that run automatically in Claude on your behalf. Claude connects to your Job Squire instance through the MCP connector, does focused work, and writes results back. Your computer does not need to be on — routines run on Anthropic's infrastructure.

The prompt for each routine is available in **Settings → AI → Claude Pro Routines**. Copy the prompt and paste it when creating a new scheduled task in Claude.

To set up a routine:

1. Open [claude.ai](https://claude.ai) and sign in.
2. In the left sidebar, click the **Scheduled** or clock icon.
3. Click **New scheduled task**.
4. Copy the prompt from **Settings → AI → Claude Pro Routines** and paste it.
5. Set the schedule and give the routine a name.
6. Save.

To run a routine immediately without waiting for the schedule, click **Open in Claude** next to it on the Settings page.

---

### Routine 1 — Morning Briefing

**Suggested schedule:** 7:00 AM daily

Claude reads your full pipeline and tells you what needs attention today: overdue follow-ups, interview reminders, stalled applications, and one specific priority to focus on. The briefing is written back to your AI analysis history as a dated entry.

---

### Routine 2 — New Job Triage

**Suggested schedule:** 9:00 AM daily

Scores every unreviewed Saved job for fit against your profile on a 1-to-10 scale and saves the score and reasoning to each job record. After it runs, open your Jobs list and sort by fit score to see which leads are worth pursuing first.

Job discovery happens automatically through **Settings → Sources** (The Muse, ZipRecruiter, Google Jobs, and others run on the scheduler). Indeed is also available as a Claude connector — if you have it connected, Claude can search Indeed and push results in via `add_jobs` during a supplemental search session. This routine focuses on scoring the jobs that have already arrived.

---

### Routine 3 — Application Kit Queue

**Suggested schedule:** Monday, Wednesday, Friday at 8:00 AM

Checks all Applied jobs for a missing application kit. For each job without one (up to 3 per run), Claude builds a complete tailored kit and saves it back to the job record. If all Applied jobs have kits, Claude checks Phone Screen and Interview stage jobs to confirm their kits are still current.

---

### Routine 4 — Follow-Up Drafts

**Suggested schedule:** 8:30 AM daily

Finds every active job and recruiter submission where a follow-up date has passed and no draft exists. For each one, Claude writes a ready-to-send email appropriate to the current stage and saves it to the job record. You review and adjust each draft before sending.

---

### Routine 5 — Weekly Strategy Review

**Suggested schedule:** Monday 8:00 AM

Claude reads the full pipeline and the past week of activity, then writes a structured review covering: what progressed, what stalled, funnel conversion by source, and one concrete strategic change for the week ahead. The review is saved to your AI analysis history.

If you also have Automated Features enabled with a weekly review scheduled, only one is needed — use whichever matches how you prefer to work.

---

## Per-job actions

When the MCP connector is active, each job detail page shows action buttons that open a pre-loaded Claude chat for that specific job:

**Score this job** — Claude reads the posting and your profile, returns a 1-to-10 fit score with explanation, and saves it to the job record.

**Build kit in Claude** — builds a complete tailored application kit (cover letter, tailored resume bullets, talking points) and saves it back. Automatically runs an ATS keyword gap analysis after the kit is saved.

**Prep for interview** — builds an interview prep guide: likely questions, STAR story mappings from your experience, smart questions to ask, and things to watch out for. Saved to the interview record.

**Draft follow-up email** — writes a ready-to-send follow-up appropriate to the current stage and saves it to the job record. Review before sending.

---

## Troubleshooting

**Connector name mismatch** — if routines say they can't find your Job Squire, check that the connector name in Claude's Settings → Connectors matches the Connector name field in Job Squire Settings exactly, including capitalization and hyphens.

**OAuth loop / can't authorize** — confirm the MCP container is running (`docker ps` should show `job-squire-mcp`) and that `PUBLIC_MCP_URL` is set and reachable from your browser.

**Token expired** — access tokens last 30 days. Remove the connector from Claude and re-add it to go through OAuth again.

**Per-job buttons not appearing** — confirm the MCP connector is enabled in Settings → AI → MCP Connector, and that the Claude.ai buttons option is enabled in Settings → AI → AI Features.

**Routine ran but wrote nothing back** — check the Claude chat transcript for that routine run. If the connector was unreachable, Claude will say so in the response. Common cause: the MCP container restarted and the token needs renewal.
