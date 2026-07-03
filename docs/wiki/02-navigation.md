# How the Job Squire Is Organized

The navigation bar across the top gives you access to everything.

## Pages

**Dashboard** — your home screen. Shows counts by stage, conversion rates (how many applications turned into interviews and offers), follow-ups due, open recruiter submissions, and recent activity. The dashboard also flags stale leads and shows a status widget for three recurring routines: unscored jobs, applied jobs without a kit, and overdue follow-ups without a draft.

**Jobs** — the full list of everything you are tracking. Click any row to open it. Sort by any column, filter by status, and search by company or title.

**Timeline** — a visual history: a bar chart of applications per week for the last 12 weeks, plus a chronological activity feed showing every application, status change, and interview.

**Recruiters** — track staffing agency contacts and the submissions they make on your behalf. See [Recruiters and Staffing Agencies](05-recruiters.md).

**Application kit** — build a tailored application package for any role. See [Application Kits](06-application-kits.md).

**AI analysis** — analyze your whole pipeline for patterns and get per-job advice. See [AI Pipeline Analysis](11-ai-analysis.md).

**Settings** — organized into seven tabs:
- **Search** — what to look for and where (titles, location, radius, age filter).
- **Sources** — API keys for each job board.
- **Email** — SMTP notification settings.
- **AI** — AI mode, Anthropic API key, model choice, thinking mode, connector setup, automated feature toggles, and Claude Pro routine prompts.
- **Candidate Profile** — your master candidate profile, document library, and profile-generation tools.
- **Application Kit** — the salary floor used in kit fit assessments and the kit output folder path.
- **History** — log of every search run.

**Guide** — this documentation, served inside the app.

## Job statuses

Every job moves through these stages, which you set yourself:

```
Saved → Applied → Phone Screen → Interview → Final Interview → Offer → Hired
```

Plus `Rejected`, `Withdrawn`, `Ghosted`, and `Pass` for ones that end early or that you decide not to pursue.

Jobs the automatic search finds always arrive as **Saved**. Think of Saved as your "to review" pile. When you actually submit an application, change the status to **Applied** so your numbers stay accurate.

**Pass** is for roles you choose not to apply to — a posting that is not a fit, pays below your floor, or is just not worth pursuing. Setting a job to Pass removes it from the default Jobs view so it does not clutter your list, but the record is kept. Use the **All** filter on the Jobs page if you want to see Pass jobs.

## Stale job indicators

The Job Squire automatically flags jobs that have gone quiet:

- **Saved for 14+ days** — the posting may have expired or been filled.
- **Applied/active for 21+ days with no update** — may be ghosted.

These show on the dashboard and in the jobs list so you can decide whether to follow up or close them out.
