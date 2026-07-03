# Copyright (C) 2026 D. Brandmeyer
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""Prompt generator for all Claude Pro routine slots and per-job actions.

Every function returns a complete, copy-ready string that a job seeker can paste
directly into Claude Pro (with the job-squire MCP connector active) and get
useful, structured work done — with results written back to Job Squire.

Design rules:
- Prompts are self-contained: Claude should not need to ask clarifying questions.
- Each prompt names every MCP tool to call and in what order.
- Write-back tools are always specified so results land in Job Squire.
- Language is plain and friendly — written for a non-technical user reading over
  Claude's shoulder, not for an engineer.
- No em-dashes, no AI-tell phrases.
"""
import os


def _connector(cfg_row) -> str:
    """Return the connector name from AIConfig, with a safe default."""
    return (cfg_row.connector_name if cfg_row else None) or "job-squire"


# ---------------------------------------------------------------------------
# Routine 1 — Morning Briefing
# ---------------------------------------------------------------------------

def morning_briefing_prompt(connector: str) -> str:
    return f"""\
You are my job-search assistant. Use my "{connector}" connector to run my morning job-search briefing.

Steps:
1. Call list_jobs() with no filter to get the full pipeline.
2. Call list_contacts() to check for overdue recruiter follow-ups.
3. Identify everything that needs attention TODAY:
   - Any job where follow_up_date is today or in the past and status is still active (Saved, Applied, Phone Screen, Interview, Final Interview, or Offer).
   - Any contact where follow_up_date is today or past.
   - Any job with status "Interview" or "Final Interview" that has no interview debrief (call get_job for those to check).
   - Any job with status "Offer" that has no notes about next steps.
4. Count how many jobs are in each active status stage.
5. Write back a single plain-text morning briefing using save_analysis. The overall_summary should be the briefing text (no JSON schema needed for the jobs list — just set jobs to an empty array). Format the summary like this:

Good morning! Here is your job-search briefing for [today's date].

PIPELINE SNAPSHOT
[one line per active status with count, e.g. "Applied: 4 | Phone Screen: 1 | Interview: 2"]

NEEDS ATTENTION TODAY
[bullet list of specific actions — job title, company, what to do]

RECRUITER FOLLOW-UPS
[any overdue contacts with name, agency, what they last submitted]

ONE THING TO FOCUS ON
[pick the single highest-leverage action based on the pipeline and write one specific recommendation]

Keep the tone direct and practical. Write it as if you are a coach who knows this candidate well.
"""


# ---------------------------------------------------------------------------
# Routine 2 — New Job Triage (with connector search + fit scoring)
# ---------------------------------------------------------------------------

def new_job_triage_prompt(connector: str, candidate_name: str = "the candidate") -> str:
    return f"""\
You are my job-search assistant. Run the New Job Triage routine for {candidate_name}.

PART 1 — SEARCH FOR NEW JOBS (run this first)

Check whether you have access to any of these job-board connectors: Indeed, ZipRecruiter, Dice.
For each one you have access to:
1. Call get_search_targets() on my "{connector}" connector to get the target job titles, location, and search criteria.
2. Search that job board for each title + location combination.
3. For every posting found, call add_jobs() on my "{connector}" connector with the results. The add_jobs tool deduplicates automatically, so pass everything — it will skip anything already saved.

If none of those connectors are available, skip Part 1 and go straight to Part 2.

PART 2 — SCORE UNANALYZED JOBS

1. Call list_unanalyzed_jobs() on my "{connector}" connector to get all "Saved" jobs that have not been scored yet. Process up to 20 at a time.
2. Call get_candidate_profile() to load the candidate's background and target criteria.
3. For each unanalyzed job:
   a. Call get_job(job_id) to get the full posting details.
   b. Score the fit on a scale of 1 to 10 based on:
      - How well the role matches the candidate's experience and target titles (most important)
      - Whether salary (if listed) meets the minimum target
      - Work mode preference
      - Signs of a low-quality posting (vague description, no company name, commission-only, MLM language, or "unlimited earning potential" — score these 1 or 2)
   c. Call set_job_fit(job_id, score, reason) where reason is 1-2 sentences explaining the score.
4. After scoring all jobs, call save_analysis() with:
   - overall_summary: a short paragraph saying how many jobs were found and added, how many were scored, and how many are high fit (7+) vs. low fit (4 or below).
   - recommendations: a list of the top 3 high-fit job titles and companies worth applying to first.
   - jobs: empty array (individual fit scores were already saved per job).

Be honest about fit scores. A score of 5 or 6 means "worth a look but not ideal." A 3 or below means the candidate should probably skip it.
"""


# ---------------------------------------------------------------------------
# Routine 3 — Application Kit Queue
# ---------------------------------------------------------------------------

def kit_queue_prompt(connector: str) -> str:
    return f"""\
You are my job-search assistant. Run the Application Kit Queue routine using my "{connector}" connector.

1. Call list_jobs(status="Applied") to find all jobs I have already applied to.
2. For each job, call get_job(job_id) and check if kit_output is empty or blank.
3. For jobs with no kit yet (up to 3 per run to keep this manageable), build a complete application kit:
   a. Call get_kit_instructions() to load the full kit-building workflow.
   b. Call get_candidate_profile() to load the candidate background.
   c. Follow the kit instructions exactly, using the job details and candidate profile.
   d. Call save_kit(job_id, kit_markdown) to save the completed kit back to the job record.
4. After finishing, briefly summarize: how many jobs were checked, how many kits were built, and the titles/companies of the jobs that got kits.

If all Applied jobs already have kits, say so clearly and suggest the candidate review the kits for any jobs in "Phone Screen" or "Interview" status to make sure they are still accurate.

Important: build real, tailored kits — not generic templates. Use specific details from the job description and match them to specific accomplishments in the candidate profile.
"""


# ---------------------------------------------------------------------------
# Routine 4 — Follow-Up Drafts
# ---------------------------------------------------------------------------

def followup_drafts_prompt(connector: str) -> str:
    return f"""\
You are my job-search assistant. Run the Follow-Up Drafts routine using my "{connector}" connector.

1. Call list_overdue_followups() to get all jobs and submissions where a follow-up is overdue and no draft exists yet.
2. Call get_candidate_profile() to load the candidate's name and background.
3. For each overdue item, draft an appropriate follow-up email:

   FOR JOBS (status = Applied, 7+ days ago with no response):
   - Subject: "Following Up — [Job Title] Application"
   - Tone: brief, professional, genuinely interested — not desperate.
   - 3 sentences max: express continued interest, mention one specific thing about the role or company, ask about next steps.
   - No em-dashes. No "I hope this email finds you well." No cliches.

   FOR JOBS (status = Phone Screen or Interview, 5+ business days since last activity):
   - Subject: "Following Up — [Job Title] at [Company]"
   - Reference the specific round that happened, note continued strong interest, ask for a timeline update.

   FOR RECRUITER SUBMISSIONS (status = Submitted or Screening):
   - Address the recruiter by name (from the contact record).
   - Ask for a status update on the submission to [company].
   - Keep it to 2-3 sentences.

4. For each draft, call save_followup_draft(job_id, email_text) to save it to the job record.
   For submission follow-ups without a linked job_id, include the draft text in the overall summary instead.

5. End with a summary: how many drafts were written, organized by type. Remind the candidate to review each draft before sending — these are starting points, not final emails.

Write every email as if you know this candidate personally. Use their actual name (from the profile), reference real details from the jobs, and sound like a human wrote it.
"""


# ---------------------------------------------------------------------------
# Routine 5 — Weekly Strategy Review
# ---------------------------------------------------------------------------

def weekly_review_prompt(connector: str) -> str:
    return f"""\
You are my job-search coach. Run the Weekly Strategy Review using my "{connector}" connector.

1. Call get_pipeline() to get the full pipeline with all job history and interview debriefs.
2. Call list_contacts() to see the full recruiter and networking contact list.
3. Analyze the past 7 days of activity and the overall pipeline health:

   WHAT WORKED THIS WEEK
   - Which applications progressed to a new stage?
   - Any interviews completed? What were the self-ratings?
   - Any new connections or recruiter submissions?

   WHAT STALLED
   - Jobs that moved to "Rejected" or "Ghosted" this week — any patterns?
   - Jobs where no activity happened despite being in an active stage for 14+ days.
   - Applications where no kit was built (which may indicate less effort went into them).

   FUNNEL ANALYSIS
   - What is the current conversion rate from Applied to Phone Screen?
   - Where is the biggest drop-off in the funnel?
   - Which job sources (Indeed, LinkedIn, recruiter, direct) are producing the most activity?

   STRATEGY FOR NEXT WEEK
   - One specific change to improve results (be concrete — not "apply to more jobs" but something actionable like "target operations manager roles at distribution centers" or "follow up with the three recruiters who have been quiet for 10+ days").
   - Which 3 active applications deserve the most attention next week and why?

4. Call save_analysis() with:
   - overall_summary: the full weekly review in plain prose (3-5 paragraphs).
   - recommendations: a list of 3-5 specific, actionable items for the coming week.
   - jobs: array of any jobs where you have specific observations worth saving (id + analysis).

Write this like a coach who has been following this search closely. Be honest about what is and is not working. Do not sugarcoat a weak week — identify it and explain what to do differently.
"""


# ---------------------------------------------------------------------------
# Per-job action prompts
# ---------------------------------------------------------------------------

def interview_prep_prompt(connector: str, job_id: int, job_title: str,
                          company: str, round_type: str = "") -> str:
    round_str = f" ({round_type})" if round_type else ""
    return f"""\
You are my job-search coach. Prepare me for my upcoming interview{round_str} for the {job_title} role at {company}.

Use my "{connector}" connector:
1. Call get_job({job_id}) to load the full job details, including any notes I have saved.
2. Call get_candidate_profile() to load my background, skills, and accomplishments.
3. Build a practical interview prep guide with these sections:

   LIKELY QUESTIONS FOR THIS ROLE
   List 8-10 questions that are highly probable for this specific job title, industry, and company type. Mix behavioral, situational, and technical questions appropriate to the role.

   MY TALKING POINTS
   For each likely question, map it to a specific accomplishment from my profile. Use the STAR format (Situation, Task, Action, Result) where appropriate. Pull real numbers and specifics from my experience.

   QUESTIONS TO ASK THEM
   5 thoughtful questions I can ask the interviewer that show genuine interest in the role and the company — not generic questions.

   THINGS TO WATCH OUT FOR
   Any red flags I should probe (gaps in the job description, unclear scope, etc.) and any weaknesses in my background relevant to this specific role that I should be ready to address.

4. Call save_interview_prep({job_id}, prep_guide_text) to save the guide so I can access it from the job record.

Make this specific to this actual job and my actual background — not a generic interview guide.
"""


def fit_score_prompt(connector: str, job_id: int) -> str:
    return f"""\
Use my "{connector}" connector to score job #{job_id} for fit:
1. Call get_job({job_id}) for the full posting details.
2. Call get_candidate_profile() for my background.
3. Score the fit from 1 to 10. Consider: title match, experience match, salary (if listed), work mode, and whether the posting looks legitimate.
4. Call set_job_fit({job_id}, score, reason) where reason is 2-3 sentences explaining the score honestly.
5. Report the score and reasoning.
"""


def followup_email_prompt(connector: str, job_id: int, job_title: str,
                          company: str, status: str) -> str:
    context = {
        "Applied": "I applied and have not heard back yet.",
        "Phone Screen": "I completed a phone screen and am waiting to hear about next steps.",
        "Interview": "I completed an interview and am following up on the timeline.",
        "Final Interview": "I completed a final interview and am waiting for a decision.",
    }.get(status, f"The current status is {status}.")

    return f"""\
Use my "{connector}" connector to draft a follow-up email for job #{job_id} ({job_title} at {company}).

Context: {context}

1. Call get_job({job_id}) for the full details including notes and any contact information.
2. Call get_candidate_profile() for my name and background.
3. Write a follow-up email:
   - Subject line included.
   - 3-4 sentences maximum.
   - Professional but warm. No cliches. No em-dashes.
   - Reference one specific detail about the role or company to show genuine interest.
   - End with a clear, low-pressure ask for an update.
4. Call save_followup_draft({job_id}, full_email_text) to save the draft.
5. Show me the draft so I can review it before sending.
"""


def rejection_analysis_prompt(connector: str) -> str:
    return f"""\
Use my "{connector}" connector to analyze my rejections and identify patterns.

1. Call list_jobs(status="Rejected") and list_jobs(status="Ghosted") to get all unsuccessful applications.
2. Call get_pipeline() for the full history including interview debriefs.
3. Look for patterns across the rejections:
   - At what stage do most rejections happen? (Applied with no response, after phone screen, after interview?)
   - What do the rejected roles have in common? (Title, industry, company size, work mode, salary range, source?)
   - What do the roles that progressed have in common?
   - If there are interview debriefs with low self-ratings, are those connected to rejections?
4. Call save_analysis() with:
   - overall_summary: a direct, honest assessment of the patterns you found and what they mean.
   - recommendations: 3-5 specific changes to the search strategy based on these patterns.
   - jobs: any individual job notes worth adding.

Be specific. "You tend to be rejected after phone screens when applying to roles requiring SAP SD experience you don't have" is useful. "Apply better" is not.
"""


# ---------------------------------------------------------------------------
# Setup instruction helpers (for the /setup page)
# ---------------------------------------------------------------------------

ROUTINE_DESCRIPTIONS = [
    {
        "slot": 1,
        "title": "Morning Briefing",
        "icon": "☀",
        "description": (
            "Every morning, Claude reads your full pipeline and tells you exactly what needs "
            "attention today: overdue follow-ups, interview reminders, stalled applications, "
            "and one priority to focus on. Replaces manually checking Job Squire every morning."
        ),
        "suggested_time": "7:00 AM daily",
        "prompt_fn": "morning_briefing",
    },
    {
        "slot": 2,
        "title": "New Job Triage",
        "icon": "🔍",
        "description": (
            "Searches Indeed, ZipRecruiter, and Dice for new postings (if those connectors are "
            "set up), adds any new jobs to your Job Squire instance, then scores every unreviewed 'Saved' "
            "job for fit against your profile on a 1-10 scale. You open Job Squire and "
            "immediately know which leads are worth pursuing."
        ),
        "suggested_time": "9:00 AM daily",
        "prompt_fn": "new_job_triage",
    },
    {
        "slot": 3,
        "title": "Application Kit Queue",
        "icon": "📄",
        "description": (
            "Automatically builds tailored application kits (resume bullet suggestions, cover "
            "letter, and email drafts) for any jobs you have applied to that do not have a kit "
            "yet. Runs up to 3 kits per session to keep things manageable."
        ),
        "suggested_time": "Mon, Wed, Fri — 8:00 AM",
        "prompt_fn": "kit_queue",
    },
    {
        "slot": 4,
        "title": "Follow-Up Drafts",
        "icon": "✉",
        "description": (
            "Finds every application and recruiter submission where a follow-up is overdue and "
            "writes a ready-to-send email draft for each one. You just review, personalize if "
            "needed, and send. Never let a hot lead go cold because you forgot to follow up."
        ),
        "suggested_time": "8:30 AM daily",
        "prompt_fn": "followup_drafts",
    },
    {
        "slot": 5,
        "title": "Weekly Strategy Review",
        "icon": "📊",
        "description": (
            "Every Monday morning, Claude reviews the past week: what progressed, what stalled, "
            "where the funnel breaks down, and one specific strategic change for the week ahead. "
            "Saves the full review to your AI analysis history."
        ),
        "suggested_time": "Monday 8:00 AM",
        "prompt_fn": "weekly_review",
    },
]

SETUP_STEPS_HOW_TO_CREATE_ROUTINE = """\
How to set up a Claude Pro scheduled routine:

1. Open Claude at claude.ai and sign in to your Pro account.
2. In the left sidebar, look for the clock or calendar icon labeled "Scheduled" or "Routines."
3. Click "New scheduled task" (or the + button).
4. Paste the prompt below into the message box.
5. Set the schedule (daily, specific days, or a specific time).
6. Give it a name (e.g., "Job Search Morning Briefing").
7. Save. Claude will run it automatically on your schedule.

Make sure your JobSquire connector is active before the first run.
You can test any routine right now by clicking "Open in Claude" — it will run immediately.
"""
