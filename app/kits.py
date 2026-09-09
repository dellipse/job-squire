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
"""Application-kit blueprint: the manual/API kit builder and the kit-batch
tool.

Split out of app/main.py (QUAL-01, Long-term audit item 17). _load_profile
and _singleton are imported from app.main rather than duplicated -- they're
shared with the not-yet-split settings routes (settings() reads the same
profile text/prompt; settings_profile*() write it).
"""
import logging
import threading
import uuid

from flask import Blueprint, Response, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from werkzeug.utils import secure_filename

from . import ai
from .db_utils import commit
from .extensions import db
from .forms import ConfirmForm, KitForm
from .main import _load_profile, _singleton
from .models import AIConfig, Job, KitConfig, SearchConfig
from .task_status import _StatusLogHandler, _TaskStatus

log = logging.getLogger(__name__)

kits_bp = Blueprint("kits", __name__)

KIT_PROMPT = """\
You are helping the candidate apply to one specific job. Work through the six steps
below in order. Do not invent employers, dates, metrics, or skills not in the profile.

────────────────────────────────────────────────────────────────
STEP 0 — FIT ASSESSMENT
────────────────────────────────────────────────────────────────

Before doing any other work, assess whether this role is a good match for the candidate.

1. VERDICT
   Compare the live job posting requirements against the CANDIDATE PROFILE below and
   output a one-line verdict in this exact format:
     "Strong Fit", "Partial Fit", or "Stretch"
   followed by a confidence note, e.g.:
     Strong Fit — matches 8 of 9 listed requirements

2. FLAGS (bullet form — omit any category where there is no concern)
   - SALARY: If the posting states a compensation range and the top of that range falls
     below $%%FIT_SALARY_FLOOR%%/year, flag it clearly.
   - HARD REQUIREMENTS NOT MET: Required degree, specific certifications,
     or years of experience that exceed the candidate's documented background.
   - OVERQUALIFICATION: If the role is significantly below the candidate's current level,
     note it.
   - LOCATION / WORK MODE: Any conflict between the posting and the candidate's stated
     preferences or constraints.

3. RECOMMENDATION
   End with exactly one of:
     "Proceed" | "Proceed with caveats" | "Consider skipping"
   followed by one sentence of reasoning.

IMPORTANT: If the recommendation is "Consider skipping", STOP HERE. Do not proceed to
Step 1 or any further work. Ask the user: "This role looks like a poor fit. Want me to
continue building the application kit anyway?" Wait for a yes before continuing.

The complete fit assessment (verdict, flags, recommendation) must be included at the
top of both saved artifacts and in the kit data pushed to Job Squire.

────────────────────────────────────────────────────────────────
STEP 1 — GATHER REFERENCE DOCUMENTS
────────────────────────────────────────────────────────────────

The CANDIDATE PROFILE section below is your primary source. Supplement it with uploaded
reference documents if they are accessible:

A. MCP connector (preferred when the Job Squire connector is active):
   Call get_candidate_assets() to retrieve all uploaded resumes and reference files.
   Use the content returned for any "Base Resume" or "Cover Letter Template" assets.

B. Mounted folder (if running in Cowork with a documents folder connected):
   Read any uploaded .docx or .md resume files as READ-ONLY reference.
   Use the docx skill to read .docx files; read .md files directly.

If neither source is accessible, proceed using the CANDIDATE PROFILE section only.

────────────────────────────────────────────────────────────────
STEP 2 — FETCH THE LIVE JOB POSTING AND RESEARCH THE COMPANY
────────────────────────────────────────────────────────────────

Do this BEFORE writing any documents. The captured description in this kit may be
incomplete or out of date — always check the source.

A. Fetch the live job posting:
   If a posting URL is in the kit header, fetch it now (web_fetch or browser tool).
   Extract the full, current job description, required qualifications, preferred skills,
   and any details about the team or department. If the URL is unavailable or returns
   a login wall, rely on the captured description below and note the limitation.

B. Research the company:
   Run a web search for "[Company Name] [current year]" to find:
   - What the company actually does and its current market position
   - Recent news (expansions, acquisitions, layoffs, culture awards, etc.)
   - The specific division or team this role falls under, if discoverable
   - Any public employee reviews or signals about culture and management style
   Keep the research factual and current. Note any findings that are directly useful
   for personalizing the cover letter or interview questions.

C. Research salary benchmarks:
   - If the posting lists a salary range, note it explicitly.
   - Run a web search for the typical salary range for this exact job title in
     %%CANDIDATE_LOCATION%% (e.g., "[Job Title] salary %%CANDIDATE_LOCATION%%
     site:glassdoor.com OR site:levels.fyi OR site:bls.gov OR site:salary.com").
   - Pull at least two data points from different sources if available.
   - Compare the market range and any posted salary to the candidate's minimum target
     of $%%FIT_SALARY_FLOOR%%/year.
   - If the role appears to pay below the candidate's minimum target, output a prominent
     warning:
       *** SALARY WARNING: This role may fall below the candidate's salary minimum. ***
       [sources and figures]
     Place this warning at the very top of the output, before any other sections,
     so it is impossible to miss.
   - If salary data is unavailable or ambiguous, note that and suggest the candidate
     verify before applying.

Summarize your research findings (company research AND salary benchmarks) in a brief
"RESEARCH NOTES" block at the top of your output so the candidate can see what you found.
Include the salary findings in this block. Then use those findings throughout Steps 3-5.

────────────────────────────────────────────────────────────────
STEP 3 — BUILD THE APPLICATION PACKAGE
────────────────────────────────────────────────────────────────

Using the candidate profile, the live job posting, and your research from Step 2,
first run the ATS keyword analysis below, then produce the six sections that follow.

ATS KEYWORD ANALYSIS
Before writing any documents, extract the 10 to 15 most important keywords and
phrases from the live job posting. Focus on: required skills, tools and systems
named, job-function verbs, industry terms, and any phrase that appears more than
once in the posting.

For each keyword, assess whether it appears in the candidate's current profile or resume
and assign one of three statuses:
  Present  — keyword or a clear equivalent is already in the profile
  Absent   — keyword does not appear at all
  Partial  — concept is implied but the exact term or phrasing is missing

Then for every Absent or Partial keyword:
  - If it is truthfully supported by the candidate's background, note briefly how it will
    be incorporated into the tailored resume (e.g., "added to summary", "woven
    into bullet for XYZ role").
  - If it cannot be incorporated without fabricating experience, label it GAP.

Output this analysis as a compact two-column Markdown table with the heading
"ATS KEYWORD ANALYSIS" placed immediately before the TAILORED RESUME section.
Format:

| Keyword / Phrase | Status + Action |
|------------------|-----------------|
| <term>           | Present         |
| <term>           | Absent — incorporated: added to summary |
| <term>           | Partial — incorporated: reworded bullet for Operations Manager role |
| <term>           | GAP — not supportable by profile |

This table must appear in the saved .md file, in the .docx artifact, and in the
markdown passed to save_kit().

1. TAILORED RESUME
   A complete, ATS-friendly resume for this exact role. Mirror the live posting's wording
   and keywords where they are truthfully supported by the profile. Lead the summary and
   top bullets with what this posting cares about most. Plain text, no tables or columns.

2. COVER LETTER
   Under 300 words, addressed to the hiring team. Reference one specific, current detail
   about the company (from your Step 2 research) to show genuine interest. Tie the body to
   two or three concrete accomplishments from the profile.

3. APPLICATION EMAIL
   A short email (under 150 words) to send a recruiter or hiring manager with the resume
   attached. Include a subject line.

4. FOLLOW-UP EMAILS
   a) A follow-up to send 5 to 7 business days after applying with no response.
   b) A thank-you to send within 24 hours after an interview.
   c) A polite check-in to send about a week after the interview if there is no update.
   Give each a subject line and keep each under 150 words.

5. ANTICIPATED INTERVIEW QUESTIONS
   Five questions the candidate is likely to be asked, each with a brief answer framework.

   Draw the questions from the live job posting and company research, not from a generic
   bank. At least two must be behavioral ("Tell me about a time...") and at least one
   must be role-specific (about a tool, process, or scenario from the actual posting).

   For each question, write a 3-5 sentence answer framework using the candidate's real
   background from the profile. Anchor every framework in a specific achievement, number,
   or situation from the profile.

   Use STAR format loosely (Situation, Task, Action, Result) but write it as natural
   talking points, not a rigid formula.

   No fabricated experiences. If a question requires knowledge the candidate does not
   have, say so and suggest how to frame an honest, positive answer anyway.

6. QUESTIONS FOR THE INTERVIEWER
   Three sharp questions the candidate can ask the interviewer, drawn from the live
   posting and your company research — not generic questions that could apply to any role.

7. LINKEDIN OUTREACH MESSAGE
   Two versions of a message to send to the hiring manager or relevant recruiter at the
   company. Use your company research from Step 2 to identify a realistic, named target
   where possible.

   Version A — Connection request (under 300 characters total):
   Short enough to fit LinkedIn's connection request limit. Name the specific role the
   candidate is applying to and reference one concrete, relevant detail from their
   background (a real number or achievement) that ties directly to what this company or
   role needs. Do NOT open with "I came across your profile." Be direct.

   Version B — Direct message (under 150 words, for use if already connected):
   Same requirements as Version A but with more room. Expand on the one concrete detail,
   briefly explain why this specific company interests the candidate (using something from
   your Step 2 research), and close with a clear, low-friction ask (a quick call, a
   question, or simply expressing interest in the role).

   Rules for both versions:
   - No em-dashes. No AI cliches. Sound like a real person.
   - Do not use "I came across your profile", "I wanted to reach out", "leverage",
     "passionate", "I am thrilled", or similar filler openers.
   - Use only real numbers and achievements from the profile.

STYLE RULES (apply to everything you write):
- Write like a real person. Warm, direct, professional.
- Do NOT use em-dashes anywhere. Use commas, periods, or rewrite the sentence.
- Avoid AI-tell phrasing and cliches: no "I am thrilled", "leverage", "passionate about",
  "in today's fast-paced world", "delve", "tapestry", "testament to".
- Use the candidate's real numbers and achievements only. Never fabricate.
- Keep contact details exactly as they appear in the profile.

────────────────────────────────────────────────────────────────
STEP 4 — SAVE ARTIFACTS (two formats)
────────────────────────────────────────────────────────────────

After generating the complete package, save it in two formats.

Derive a safe filename slug from the company and job title in the kit header, e.g.
"Acme-Logistics-Coordinator" (replace spaces with hyphens, strip special characters).
Save both files to the Application Kits folder (%%KIT_OUTPUT_DIR%%).

a) Markdown file (.md):
   Filename: kit-output-{slug}.md
   Content: the FIT ASSESSMENT block from Step 0, then the RESEARCH NOTES block,
   then all seven sections from Step 3.

b) Word document (.docx):
   Use the docx skill to create a formatted Word document with the same content.
   Filename: kit-output-{slug}.docx
   Apply section headings (Heading 1 style) and normal paragraph formatting.
   Present both files to the user when done.

────────────────────────────────────────────────────────────────
STEP 5 — PUSH TO JOB SQUIRE
────────────────────────────────────────────────────────────────

If the Job Squire MCP connector is active AND the kit header above shows a Job ID:
- Call save_kit(job_id=<that ID>, kit_markdown=<the full markdown from Step 4a>)
  The markdown passed to save_kit must include the fit assessment at the top, exactly
  as it appears in the saved .md file.
- Report whether the save succeeded.
- Also call set_follow_up(job_id=<that ID>, days_out=6) to set a follow-up reminder
  6 calendar days from today. Report the follow-up date that was set.

If no Job ID is present (free-form kit), skip this step and note it for the user.

────────────────────────────────────────────────────────────────
"""


def _build_kit(job_title, company, location, url, description, profile, job_id=None,
               fit_salary_floor=60000, candidate_location="", kit_output_dir=""):
    kit_prompt = (
        KIT_PROMPT
        .replace("%%FIT_SALARY_FLOOR%%", f"{fit_salary_floor:,}")
        .replace("%%CANDIDATE_LOCATION%%", candidate_location or "the candidate's city")
        .replace("%%KIT_OUTPUT_DIR%%", kit_output_dir or "your working folder")
    )
    parts = [
        "# Application Kit",
        f"For: {job_title} at {company}" + (f" ({location})" if location else ""),
        (f"Job ID: {job_id}" if job_id else None),
        (f"Posting: {url}" if url else ""),
        "",
        "## INSTRUCTIONS FOR CLAUDE",
        "Paste this entire file into Claude as your first message, or use the "
        "\"Build kit in Claude\" button (MCP mode) for automatic connector access.",
        "",
        kit_prompt,
        "",
        "## CANDIDATE PROFILE",
        profile.strip(),
        "",
        "## JOB POSTING",
        f"Title: {job_title}",
        f"Company: {company}",
        (f"Location: {location}" if location else None),
        (f"URL: {url}" if url else None),
        "",
        "Full description / details:",
        (description or "(No description captured. Paste the full posting text here before sending to Claude.)").strip(),
        "",
    ]
    return "\n".join(p for p in parts if p is not None)


def _kit_response(markdown, company, title):
    safe = secure_filename(f"{company}-{title}")[:60] or "application-kit"
    return Response(
        markdown,
        mimetype="text/markdown",
        headers={"Content-Disposition": f"attachment; filename=kit-{safe}.md"},
    )


def _job_from_kit_form(form):
    """Create, persist, and flash-confirm a new Job from a KitForm's fields.

    Shared by kit_hub and kit_run's ad-hoc ("save as a job too" = yes, no
    tracked_job_id) paths -- both used to carry a verbatim copy of this
    construction (QUAL-02)."""
    job = Job(
        company=form.company.data.strip(),
        title=form.job_title.data.strip(),
        location=(form.location.data or "").strip(),
        url=(form.url.data or "").strip(),
        status="Saved",
        notes=form.job_description.data or "",
        created_by=current_user.display_name or current_user.username,
    )
    db.session.add(job)
    commit()
    flash(f'Saved "{job.title}" to Job Squire as a job.', "success")
    return job


@kits_bp.route("/jobs/<int:job_id>/kit")
@login_required
def job_kit(job_id):
    job = db.get_or_404(Job, job_id)
    description = job.notes or ""
    kit_cfg = _singleton(KitConfig)
    search_cfg = _singleton(SearchConfig)
    md = _build_kit(job.title, job.company, job.location, job.url, description,
                    _load_profile(), job_id=job.id,
                    fit_salary_floor=kit_cfg.fit_salary_floor or 60000,
                    candidate_location=search_cfg.location or "")
    return _kit_response(md, job.company, job.title)


@kits_bp.route("/kit", methods=["GET", "POST"])
@login_required
def kit_hub():
    form = KitForm()
    if form.validate_on_submit():
        # If the form was pre-filled from an existing tracked job, that job wins —
        # never create a second, duplicate job for it even if "save as a job too"
        # was left set to "yes" (it's meaningless once the job is already tracked).
        existing_id = request.form.get("tracked_job_id", type=int)
        if existing_id:
            kit_job_id = existing_id
        elif form.save_job.data == "yes":
            kit_job_id = _job_from_kit_form(form).id
        else:
            kit_job_id = None
        kit_cfg = _singleton(KitConfig)
        search_cfg = _singleton(SearchConfig)
        md = _build_kit(
            form.job_title.data.strip(), form.company.data.strip(),
            (form.location.data or "").strip(), (form.url.data or "").strip(),
            form.job_description.data or "", _load_profile(),
            job_id=kit_job_id,
            fit_salary_floor=kit_cfg.fit_salary_floor or 60000,
            candidate_location=search_cfg.location or "",
        )
        return _kit_response(md, form.company.data, form.job_title.data)

    # GET: optionally pre-fill the form from a tracked job (?job_id=N).
    selected = None
    job_id = request.args.get("job_id", type=int)
    if job_id:
        selected = db.session.get(Job, job_id)
        if selected:
            form.job_title.data = selected.title
            form.company.data = selected.company
            form.location.data = selected.location
            form.url.data = selected.url
            form.job_description.data = selected.notes or ""
    job_options = Job.query.order_by(Job.company.asc(), Job.title.asc()).all()
    return render_template("kit_hub.html", form=form, job_options=job_options,
                           selected=selected)


@kits_bp.route("/kit/run", methods=["POST"])
@login_required
def kit_run():
    """Build an application kit via the configured AI provider chain.

    Handles both entry points: job detail's "Build kit" button (posts just
    job_id) and the Kit Hub form (posts the full KitForm, optionally with
    tracked_job_id). Works with any AI API key configured in Settings,
    including free-tier providers (Ollama, Groq, Gemini, OpenRouter, GitHub
    Models, Cerebras, Mistral), not just Claude/MCP.

    Runs in a background thread with a live status page (same pattern as
    ai_run_task() for triage/follow-up/weekly review) instead of running
    synchronously — a slow or free-tier provider can easily take longer than
    Gunicorn's worker timeout, which kills the worker with SIGABRT mid-request
    with no way for a try/except here to catch it.
    """
    ai_cfg = _singleton(AIConfig)
    if not ai_cfg.api_enabled:
        flash("Building a kit via API requires Automatic features to be enabled in Settings > AI.", "warning")
        return redirect(url_for("kits.kit_hub"))

    job = None
    location = description = url_val = ""

    job_id = request.form.get("job_id", type=int)
    if job_id:
        # Entry point: job detail page's "Build kit" button — always a tracked job.
        job = db.get_or_404(Job, job_id)
        title, company = job.title, job.company
    else:
        # Entry point: Kit Hub form — may reference a tracked job or be ad-hoc.
        form = KitForm()
        if not form.validate_on_submit():
            job_options = Job.query.order_by(Job.company.asc(), Job.title.asc()).all()
            return render_template("kit_hub.html", form=form, job_options=job_options, selected=None)

        # An existing tracked job always wins — never create a second, duplicate
        # job for it even if "save as a job too" was left set to "yes" (it's
        # meaningless once the job is already tracked).
        existing_id = request.form.get("tracked_job_id", type=int)
        if existing_id:
            job = db.session.get(Job, existing_id)
        elif form.save_job.data == "yes":
            job = _job_from_kit_form(form)

        title = form.job_title.data.strip()
        company = form.company.data.strip()
        location = (form.location.data or "").strip()
        description = form.job_description.data or ""
        url_val = (form.url.data or "").strip()

    job_id_for_thread = job.id if job is not None else None

    run_id = uuid.uuid4().hex
    data_dir = current_app.config["DATA_DIR"]
    status = _TaskStatus(run_id, "build_kit", data_dir)
    _app = current_app._get_current_object()
    ai_log = logging.getLogger("app.ai")

    def _run():
        handler = _StatusLogHandler(status)
        prior_level = ai_log.level
        ai_log.addHandler(handler)
        ai_log.setLevel(logging.INFO)
        with _app.app_context():
            try:
                status.log(f"INFO Building application kit for {title} at {company}…")
                if job_id_for_thread is not None:
                    j = db.session.get(Job, job_id_for_thread)
                    if j is None:
                        raise RuntimeError(f"Job {job_id_for_thread} no longer exists")
                    ai.run_build_kit_api(j)
                    status.done({"job_id": job_id_for_thread, "title": title, "company": company})
                else:
                    kit_md = ai.build_kit_api_adhoc(
                        title, company, location=location, description=description,
                        url=url_val, job=None,
                    )
                    status.done({"job_id": None, "title": title, "company": company,
                                 "kit_markdown": kit_md})
            except Exception as exc:  # noqa: BLE001
                db.session.rollback()
                log.exception("kit_run failed (job_id=%s)", job_id_for_thread)
                status.fail(exc)
            finally:
                ai_log.removeHandler(handler)
                ai_log.setLevel(prior_level)

    threading.Thread(target=_run, daemon=True).start()
    return redirect(url_for("task_status.ai_task_status", run_id=run_id, task="build_kit"))


@kits_bp.route("/kit/download-docx", methods=["POST"])
@login_required
def kit_download_docx():
    """Render an ad-hoc (untracked) API-built kit to .docx for direct download.

    No file is stored on the server — the markdown is posted back from the
    result page and converted on the fly. Kits built for a tracked job already
    get a persistent .docx attachment automatically (see ai.build_kit_api_adhoc);
    this route only covers the "didn't save to Job Squire" case, where there's no
    job record to attach a file to.
    """
    from .docgen import markdown_to_docx_bytes

    markdown_text = request.form.get("kit_markdown", "")
    company = (request.form.get("company") or "kit").strip()
    title = (request.form.get("title") or "application").strip()
    docx_bytes = markdown_to_docx_bytes(markdown_text)
    safe = secure_filename(f"{company}-{title}-kit")[:80] or "application-kit"
    return Response(
        docx_bytes,
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f"attachment; filename={safe}.docx"},
    )


@kits_bp.route("/tools/kit-batch", methods=["GET", "POST"])
@login_required
def kit_batch():
    """Manual kit-build page — builds application kits for all Applied jobs missing one.

    POST: launches the batch in a background thread using _TaskStatus + _StatusLogHandler,
    then redirects to GET ?run_id=... so the page can poll for live output and render results.
    """
    if request.method == "POST":
        if not ConfirmForm().validate_on_submit():
            abort(400)
        ai_cfg = _singleton(AIConfig)
        if not ai_cfg.api_enabled:
            flash("Build kits requires Automatic features to be enabled in Settings.", "warning")
            return redirect(url_for("main.dashboard"))

        job_ids = [
            row.id for row in (
                Job.query.filter(Job.status == "Applied")
                .filter(db.or_(Job.kit_output == None, Job.kit_output == ""))  # noqa: E711
                .with_entities(Job.id)
                .all()
            )
        ]
        if not job_ids:
            flash("No Applied jobs are missing kits.", "info")
            return redirect(url_for("main.dashboard"))

        run_id = uuid.uuid4().hex
        data_dir = current_app.config["DATA_DIR"]
        status = _TaskStatus(run_id, "kit_batch", data_dir)
        _app = current_app._get_current_object()

        def _run():
            from .ai import run_build_kit_api as _build_kit
            with _app.app_context():
                built, failed, results = 0, 0, []
                status.log(f"INFO Building kits for {len(job_ids)} job(s)…")
                for jid in job_ids:
                    job = db.session.get(Job, jid)
                    if job is None:
                        continue
                    status.log(f"INFO  · {job.title} @ {job.company}")
                    try:
                        _build_kit(job)
                        built += 1
                        results.append({"id": job.id, "title": job.title,
                                        "company": job.company, "ok": True})
                        status.log("INFO   ✓ done")
                    except Exception as exc:  # noqa: BLE001
                        db.session.rollback()
                        failed += 1
                        results.append({"id": job.id, "title": job.title,
                                        "company": job.company, "ok": False,
                                        "error": str(exc)})
                        log.warning("kit_batch: job %d failed: %s", jid, exc)
                        status.log(f"WARNING   ✗ failed: {exc}")
                status.done({"built": built, "failed": failed, "results": results})

        threading.Thread(target=_run, daemon=True).start()
        return redirect(url_for("kits.kit_batch", run_id=run_id))

    # GET — show the page (with or without an active run_id)
    run_id = request.args.get("run_id", "")
    total_remaining = (
        Job.query.filter(Job.status == "Applied")
        .filter(db.or_(Job.kit_output == None, Job.kit_output == ""))  # noqa: E711
        .count()
    )

    return render_template(
        "kit_batch.html",
        run_id=run_id,
        poll_url=url_for("task_status.ai_task_poll", run_id=run_id) if run_id else "",
        total_remaining=total_remaining,
        confirm_form=ConfirmForm(),
    )
