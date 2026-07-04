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
"""Remote MCP server exposing Job Squire to the user's Claude as a custom connector.

Transport: Streamable HTTP (claude.ai custom connectors).
Auth:      Two methods, both use Authorization: Bearer <token>:
           1. OAuth 2.0 Authorization Code + PKCE — required by Claude's connector
              handshake.  The user signs in on the /authorize page; Claude stores
              the resulting Bearer token and sends it on every MCP call.
           2. Static API key — set MCP_API_KEY env var and pass it as a Bearer
              token.  Intended for scripts and non-Claude tools.

Run:  python -m app.mcp_server      (listens on 0.0.0.0:9000)
"""

import base64
import hashlib
import json
import os
import secrets
import time
from urllib.parse import parse_qs, urlencode

import logging
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from werkzeug.security import check_password_hash

import time as _time
_login_failures: dict = {}   # ip -> list of failure timestamps
_LOGIN_MAX_FAILURES = 5
_LOGIN_FAILURE_WINDOW = 600  # seconds


def _login_rate_ok(ip: str) -> bool:
    now = _time.time()
    recent = [t for t in _login_failures.get(ip, []) if now - t < _LOGIN_FAILURE_WINDOW]
    _login_failures[ip] = recent
    return len(recent) < _LOGIN_MAX_FAILURES


def _record_login_failure(ip: str) -> None:
    _login_failures.setdefault(ip, []).append(_time.time())


_log = logging.getLogger(__name__)

from . import create_app
from .ai import apply_analysis, build_export_dict
from .extensions import db
from .models import AIConfig, AIInsight, CandidateAsset, Contact, Interview, Job, JobNote, Submission

flask_app = create_app()

# FastMCP's DNS-rebinding protection validates the Host header against an
# explicit allowlist.  We run behind SWAG, so Claude's requests arrive with the
# public hostname.  List all hosts that may appear: the public subdomain, plus
# localhost variants for local testing.  Protection stays ON — we just tell it
# which hosts are legitimate.
_MCP_PORT = int(os.environ.get("MCP_PORT", "9000"))
_PUBLIC_HOST = os.environ.get("PUBLIC_MCP_HOST", "")
if not _PUBLIC_HOST:
    _PUBLIC_HOST = f"localhost:{_MCP_PORT}"
    _log.warning("PUBLIC_MCP_HOST is not set — falling back to localhost:%d. Set this in production.", _MCP_PORT)
_transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=[
        _PUBLIC_HOST,
        f"{_PUBLIC_HOST}:443",
        "localhost",
        f"localhost:{_MCP_PORT}",
        "127.0.0.1",
        f"127.0.0.1:{_MCP_PORT}",
    ],
    allowed_origins=[
        f"https://{_PUBLIC_HOST}",
        "https://claude.ai",
    ],
)
mcp = FastMCP("JobSquire", stateless_http=True, transport_security=_transport_security)

# ---------------------------------------------------------------------------
# OAuth stores
# ---------------------------------------------------------------------------
# _clients and _codes are in-memory only: clients re-register each session
# and codes expire in 10 minutes, so persistence adds no value.
_clients: dict = {}     # client_id -> {redirect_uris}
_codes: dict = {}       # code -> {client_id, redirect_uri, code_challenge, exp}

# Access tokens ARE persisted to DATA_DIR/oauth_tokens.json so they survive
# container restarts (30-day TTL means a restart would otherwise force re-auth).
#
# SECURITY NOTE: this token store is UNENCRYPTED on disk. Anyone able to read
# DATA_DIR/oauth_tokens.json can reuse any entry whose "exp" is in the future as
# a Bearer token and gain full MCP read/write access until it expires. This is an
# accepted risk for this self-hosted, single-tenant deployment; the mitigations
# relied upon are cryptographically random 48-byte tokens, a finite TTL, and
# restrictive permissions on DATA_DIR (non-root owner, chmod 700). Do not relax
# those assumptions without encrypting this file. See issue #5.
_TOKEN_TTL = 3600 * 24 * 30   # 30 days


def _token_store_path() -> str:
    return os.path.join(flask_app.config.get("DATA_DIR", "/data"), "oauth_tokens.json")


def _load_tokens() -> dict:
    path = _token_store_path()
    try:
        with open(path, "r") as fh:
            data = json.load(fh)
        # Prune expired tokens on load.
        now = time.time()
        return {k: v for k, v in data.items() if v.get("exp", 0) > now}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_tokens(tokens: dict) -> None:
    path = _token_store_path()
    now = time.time()
    live = {k: v for k, v in tokens.items() if v.get("exp", 0) > now}
    try:
        with open(path, "w") as fh:
            json.dump(live, fh)
    except OSError as exc:
        _log.warning("Could not save OAuth tokens to %s: %s", path, exc)


with flask_app.app_context():
    _tokens: dict = _load_tokens()


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------

@mcp.tool()
def get_pipeline() -> dict:
    """Return the user's full job-search pipeline and all interview debriefs, for analysis."""
    with flask_app.app_context():
        return build_export_dict()


@mcp.tool()
def list_jobs(status: str = "") -> list:
    """List jobs, optionally filtered by status (e.g. 'Applied', 'Interview', 'Saved')."""
    with flask_app.app_context():
        q = Job.query
        if status:
            q = q.filter(Job.status == status)
        return [
            {"id": j.id, "company": j.company, "title": j.title, "status": j.status,
             "location": j.location, "url": j.url,
             "date_applied": str(j.date_applied) if j.date_applied else None,
             "ai_fit_score": j.ai_fit_score,
             "created_at": str(j.created_at)[:10] if j.created_at else None}
            for j in q.order_by(Job.updated_at.desc()).all()
        ]


@mcp.tool()
def get_job(job_id: int) -> dict:
    """Get full detail for one job, including notes, AI fit score, and interview debriefs."""
    with flask_app.app_context():
        j = db.session.get(Job, job_id)
        if not j:
            return {"error": f"no job with id {job_id}"}
        return {
            "id": j.id, "company": j.company, "title": j.title, "location": j.location,
            "work_mode": j.work_mode, "status": j.status, "source": j.source, "url": j.url,
            "salary": j.salary, "ai_fit_score": j.ai_fit_score,
            "date_applied": str(j.date_applied) if j.date_applied else None,
            "follow_up_date": str(j.follow_up_date) if j.follow_up_date else None,
            "contact_name": j.contact_name, "contact_email": j.contact_email,
            "notes": j.notes or "", "ai_analysis": j.ai_analysis or "",
            "interviews": [
                {"date": str(iv.interview_date) if iv.interview_date else None,
                 "round": iv.round_type, "format": iv.interview_format,
                 "self_rating": iv.self_rating, "questions_asked": iv.questions_asked,
                 "went_well": iv.went_well, "to_improve": iv.to_improve, "notes": iv.notes}
                for iv in j.interviews
            ],
        }


@mcp.tool()
def get_candidate_profile() -> str:
    """Return the user's master profile (background, skills, experience) for tailoring documents."""
    with flask_app.app_context():
        path = os.path.join(flask_app.config["DATA_DIR"], "candidate_profile.md")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return "(candidate profile not available)"


@mcp.tool()
def save_candidate_profile(profile_markdown: str) -> dict:
    """Save an updated candidate profile back to Job Squire.

    Call this after generating a new or revised profile from uploaded documents.
    profile_markdown: the complete updated profile in Markdown format.
    Returns a confirmation dict on success, or an error dict on failure.
    """
    with flask_app.app_context():
        path = os.path.join(flask_app.config["DATA_DIR"], "candidate_profile.md")
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write((profile_markdown or "").rstrip())
            return {
                "ok": True,
                "message": "Candidate profile saved. It will be used for all future application kits.",
            }
        except OSError as exc:
            return {"error": f"Could not write profile: {exc}"}


@mcp.tool()
def get_candidate_assets(kind: str = "") -> list:
    """List the candidate's master documents (resume, recommendation letters, certs, etc.).

    Optionally filter by kind: 'Base Resume', 'Recommendation Letter',
    'Cover Letter Template', 'Certification', 'Portfolio', or 'Other'.

    Returns metadata for each asset plus a download URL. Use these when building
    application kits, tailoring documents, or advising on the user's credentials.
    Text-based files (.txt, .md) include their full content in the 'content' field;
    binary files (PDF, DOCX) return an empty 'content' — fetch via the download URL
    or reference the file description in 'notes'.
    """
    with flask_app.app_context():
        q = CandidateAsset.query
        if kind:
            q = q.filter(CandidateAsset.kind == kind)
        assets = q.order_by(CandidateAsset.kind.asc(), CandidateAsset.uploaded_at.desc()).all()
        upload_dir = flask_app.config["UPLOAD_DIR"]
        result = []
        for a in assets:
            content = ""
            if a.original_name.rsplit(".", 1)[-1].lower() in ("txt", "md") if "." in a.original_name else False:
                try:
                    path = os.path.join(upload_dir, a.stored_name)
                    with open(path, "r", encoding="utf-8", errors="replace") as fh:
                        content = fh.read()
                except OSError:
                    content = ""
            result.append({
                "id": a.id,
                "kind": a.kind,
                "label": a.label,
                "display_name": a.display_name,
                "original_name": a.original_name,
                "content_type": a.content_type,
                "size_kb": a.size_kb,
                "notes": a.notes,
                "uploaded_at": str(a.uploaded_at.date()),
                "content": content,
            })
        return result


@mcp.tool()
def add_jobs(jobs: list) -> dict:
    """Add job postings Claude found (via its own Indeed/ZipRecruiter/Dice connectors) to the
    Job Squire as 'Saved', skipping duplicates.

    Each item should be an object with as many of these fields as available:
    {"title", "company", "location", "url", "salary", "source", "external_id", "description"}.
    'source' is the board name (e.g. 'indeed'); 'external_id' is that board's job id if known.
    Returns how many were created versus skipped as duplicates.
    """
    from .search import ingest_jobs
    with flask_app.app_context():
        created, skipped = ingest_jobs(jobs or [], created_by="Claude (MCP)")
        return {"created": len(created), "skipped": skipped, "ids": [j.id for j in created]}


@mcp.tool()
def get_search_targets() -> dict:
    """Return the user's target job titles and location, so Claude knows what to search for."""
    from .models import SearchConfig
    with flask_app.app_context():
        cfg = db.session.get(SearchConfig, 1)
        if not cfg:
            return {"titles": [], "location": ""}
        return {"titles": cfg.title_list, "location": cfg.location,
                "radius_miles": cfg.radius_miles, "min_salary": cfg.min_salary}


@mcp.tool()
def save_analysis(overall_summary: str, recommendations: list, jobs: list) -> dict:
    """Save analysis back to Job Squire.

    overall_summary: a short summary string.
    recommendations: a list of concrete recommendation strings.
    jobs: a list of {"id": <job id>, "analysis": <text>} objects.
    Returns how many jobs were updated.
    """
    with flask_app.app_context():
        parsed = {"overall_summary": overall_summary,
                  "recommendations": recommendations or [],
                  "jobs": jobs or []}
        updated, missing = apply_analysis(parsed, created_by="Claude (MCP)")
        return {"updated": updated, "skipped": missing}


@mcp.tool()
def get_kit_instructions() -> str:
    """Return the full step-by-step instructions for building an application kit.

    Call this first when the user asks you to build an application kit for a job.
    Follow every step in the returned instructions exactly, in order.
    """
    from .main import KIT_PROMPT
    return KIT_PROMPT


@mcp.tool()
def update_job_notes(job_id: int, notes: str) -> dict:
    """Replace the notes/description on a job record.

    Use this to save a full job description fetched from the posting URL,
    replacing a short imported snippet.  Pass the plain-text body of the
    job posting — responsibilities, qualifications, and about-the-company.
    Strip navigation, ads, and other page chrome before saving.

    job_id: the integer job ID.
    notes:  the full description text to store.
    Returns the job company and title on success, or an error dict.
    """
    with flask_app.app_context():
        j = db.session.get(Job, job_id)
        if not j:
            return {"error": f"no job with id {job_id}"}
        j.notes = (notes or "").strip()
        db.session.commit()
        return {"ok": True, "job_id": job_id, "company": j.company, "title": j.title,
                "message": f"Notes updated for {j.company} — {j.title}."}


@mcp.tool()
def save_kit(job_id: int, kit_markdown: str) -> dict:
    """Save a completed application kit (tailored resume, cover letter, emails, etc.) back to a
    specific job record.

    job_id:       the integer ID embedded in the kit file header.
    kit_markdown: the full markdown output Claude produced — all five sections.

    Returns the job company and title on success, or an error dict.
    """
    from datetime import datetime as _dt, timezone
    with flask_app.app_context():
        j = db.session.get(Job, job_id)
        if not j:
            return {"error": f"no job with id {job_id}"}
        j.kit_output = kit_markdown
        j.kit_generated_at = _dt.now(timezone.utc)
        db.session.commit()
        result = {"ok": True, "job_id": job_id, "company": j.company, "title": j.title,
                  "message": f"Kit saved to job record for {j.company} — {j.title}."}

        # Feature 4: Auto-run ATS gap analysis if API mode is enabled.
        _run_ats_after_kit(j)

        return result


def _run_ats_after_kit(job):
    """Feature 4: Run ATS keyword gap analysis after a kit is saved.

    Called from save_kit(). Silently skips if API mode is not configured.
    Runs inside the caller's existing app context.
    """
    try:
        from .models import AIConfig
        cfg = db.session.get(AIConfig, 1)
        if not cfg or cfg.mode != "api":
            return
        secret = flask_app.config.get("SECRET_KEY", "")
        from .crypto import decrypt as _dec
        api_key = _dec(secret, cfg.api_key_enc or "").strip()
        if not api_key:
            return
        model = (cfg.triage_model or "claude-haiku-4-5").strip()
        from .ai import run_ats_analysis, _load_candidate_profile
        profile = _load_candidate_profile()
        run_ats_analysis(job, profile, api_key, model)
        _log.info("ATS gap analysis auto-run for job %d", job.id)
    except Exception:  # noqa: BLE001
        _log.exception("ATS auto-analysis failed for job %d", job.id)


@mcp.tool()
def set_follow_up(job_id: int, days_out: int = 6) -> dict:
    """Set a follow-up reminder on a job record.

    Calculates follow_up_date as today + days_out calendar days and saves it.
    Returns the job_id and the date that was set, or an error dict if not found.
    """
    from datetime import datetime as _dt, timedelta, timezone
    with flask_app.app_context():
        j = db.session.get(Job, job_id)
        if not j:
            return {"error": f"no job with id {job_id}"}
        date = _dt.now(timezone.utc).date() + timedelta(days=days_out)
        j.follow_up_date = date
        db.session.commit()
        return {"ok": True, "job_id": job_id, "follow_up_date": str(date)}


@mcp.tool()
def list_contacts(contact_type: str = "") -> list:
    """List the user's recruiter / staffing-agency / networking contacts.

    Optionally filter by type: 'Recruiter', 'Staffing Agency', 'Hiring Manager',
    'Networking', or 'Reference'. Returns each contact with their open-submission count.
    """
    with flask_app.app_context():
        q = Contact.query
        if contact_type:
            q = q.filter(Contact.contact_type == contact_type)
        return [
            {"id": c.id, "name": c.name, "agency": c.agency, "type": c.contact_type,
             "title": c.title, "email": c.email, "phone": c.phone,
             "linkedin_url": c.linkedin_url,
             "last_contacted": str(c.last_contacted) if c.last_contacted else None,
             "follow_up_date": str(c.follow_up_date) if c.follow_up_date else None,
             "open_submissions": len(c.open_submissions)}
            for c in q.order_by(Contact.name.asc()).all()
        ]


@mcp.tool()
def get_contact(contact_id: int) -> dict:
    """Get one contact with their full submission history (who submitted the user where)."""
    with flask_app.app_context():
        c = db.session.get(Contact, contact_id)
        if not c:
            return {"error": f"no contact with id {contact_id}"}
        return {
            "id": c.id, "name": c.name, "agency": c.agency, "type": c.contact_type,
            "title": c.title, "email": c.email, "phone": c.phone,
            "linkedin_url": c.linkedin_url, "notes": c.notes or "",
            "last_contacted": str(c.last_contacted) if c.last_contacted else None,
            "follow_up_date": str(c.follow_up_date) if c.follow_up_date else None,
            "submissions": [
                {"id": s.id, "company": s.company, "role_title": s.role_title,
                 "status": s.status, "job_id": s.job_id,
                 "submitted_date": str(s.submitted_date) if s.submitted_date else None,
                 "follow_up_date": str(s.follow_up_date) if s.follow_up_date else None,
                 "notes": s.notes or ""}
                for s in c.submissions
            ],
        }


@mcp.tool()
def add_contact(name: str, agency: str = "", contact_type: str = "Recruiter",
                title: str = "", email: str = "", phone: str = "",
                linkedin_url: str = "", notes: str = "") -> dict:
    """Add a recruiter / staffing-agency / networking contact to Job Squire.

    name is required. contact_type is one of 'Recruiter', 'Staffing Agency',
    'Hiring Manager', 'Networking', 'Reference' (defaults to 'Recruiter').
    Returns the new contact id.
    """
    with flask_app.app_context():
        c = Contact(
            name=(name or "").strip(),
            agency=(agency or "").strip(),
            contact_type=contact_type or "Recruiter",
            title=(title or "").strip(),
            email=(email or "").strip(),
            phone=(phone or "").strip(),
            linkedin_url=(linkedin_url or "").strip(),
            notes=notes or "",
            created_by="Claude (MCP)",
        )
        if not c.name:
            return {"error": "name is required"}
        db.session.add(c)
        db.session.commit()
        return {"id": c.id, "name": c.name}


@mcp.tool()
def log_submission(contact_id: int = 0, company: str = "", role_title: str = "",
                   job_id: int = 0, status: str = "Submitted",
                   submitted_date: str = "", notes: str = "") -> dict:
    """Log that a recruiter/agency submitted the user for a specific role.

    contact_id is the recruiter who submitted him (0 if unknown). Provide company and
    role_title, or a job_id to link an existing tracked job. status is one of
    'Submitted', 'Screening', 'Interviewing', 'Offer', 'Placed', 'Rejected',
    'Withdrawn', 'No Response'. submitted_date is 'YYYY-MM-DD' (optional).
    """
    from datetime import date as _date
    with flask_app.app_context():
        sd = None
        if submitted_date:
            try:
                sd = _date.fromisoformat(submitted_date.strip())
            except ValueError:
                return {"error": "submitted_date must be YYYY-MM-DD"}
        company = (company or "").strip()
        role_title = (role_title or "").strip()
        linked_job = db.session.get(Job, job_id) if job_id else None
        if linked_job:
            company = company or linked_job.company
            role_title = role_title or linked_job.title
        s = Submission(
            contact_id=contact_id or None,
            job_id=job_id or None,
            company=company,
            role_title=role_title,
            status=status or "Submitted",
            submitted_date=sd,
            notes=notes or "",
            created_by="Claude (MCP)",
        )
        db.session.add(s)
        db.session.commit()
        return {"id": s.id, "contact_id": s.contact_id, "company": s.company,
                "role_title": s.role_title, "status": s.status}


# ---------------------------------------------------------------------------
# Phase 2: Pro routine support tools
# ---------------------------------------------------------------------------

@mcp.tool()
def list_unanalyzed_jobs(limit: int = 20) -> list:
    """Return Saved jobs that have no AI fit score yet — used by the New Job Triage routine.

    Returns up to `limit` jobs (default 20). Each item includes id, title, company,
    location, salary, source, url, and the notes/description so the triage routine
    can score fit without a separate get_job call for each one.
    """
    with flask_app.app_context():
        q = (Job.query
             .filter(Job.status == "Saved")
             .filter((Job.ai_fit_score == None) | (Job.ai_fit_score == 0))  # noqa: E711
             .order_by(Job.created_at.desc())
             .limit(max(1, min(limit, 50))))
        return [
            {"id": j.id, "title": j.title, "company": j.company,
             "location": j.location, "salary": j.salary, "source": j.source,
             "url": j.url, "notes": (j.notes or "")[:600]}
            for j in q.all()
        ]


@mcp.tool()
def set_job_fit(job_id: int, score: int, reason: str) -> dict:
    """Save an AI fit score and brief reasoning to a job record.

    score: integer 1-10 (1=very poor fit, 10=perfect match).
    reason: 1-3 sentence explanation of the score.
    Returns the job id and saved score, or an error dict.
    """
    with flask_app.app_context():
        j = db.session.get(Job, job_id)
        if not j:
            return {"error": f"no job with id {job_id}"}
        j.ai_fit_score = max(1, min(10, int(score)))
        j.ai_fit_reason = (reason or "").strip()
        db.session.commit()
        return {"ok": True, "job_id": job_id, "score": j.ai_fit_score}


@mcp.tool()
def list_overdue_followups() -> dict:
    """Return all jobs and recruiter submissions where a follow-up is overdue
    and no follow-up draft has been written yet.

    Returns two lists:
      jobs: active jobs where follow_up_date <= today and followup_draft is blank.
      submissions: active submissions where follow_up_date <= today.
    """
    from datetime import date as _date
    with flask_app.app_context():
        from .models import ACTIVE_STATUSES, ACTIVE_SUBMISSION_STATUSES, Submission
        today = _date.today()

        overdue_jobs = (
            Job.query
            .filter(Job.status.in_(list(ACTIVE_STATUSES)))
            .filter(Job.follow_up_date != None)  # noqa: E711
            .filter(Job.follow_up_date <= today)
            .filter((Job.followup_draft == None) | (Job.followup_draft == ""))  # noqa: E711
            .order_by(Job.follow_up_date.asc())
            .all()
        )

        overdue_subs = (
            Submission.query
            .filter(Submission.status.in_(list(ACTIVE_SUBMISSION_STATUSES)))
            .filter(Submission.follow_up_date != None)  # noqa: E711
            .filter(Submission.follow_up_date <= today)
            .order_by(Submission.follow_up_date.asc())
            .all()
        )

        return {
            "jobs": [
                {"id": j.id, "title": j.title, "company": j.company,
                 "status": j.status, "follow_up_date": str(j.follow_up_date),
                 "contact_name": j.contact_name, "contact_email": j.contact_email,
                 "date_applied": str(j.date_applied) if j.date_applied else None}
                for j in overdue_jobs
            ],
            "submissions": [
                {"id": s.id, "company": s.company, "role_title": s.role_title,
                 "status": s.status, "follow_up_date": str(s.follow_up_date),
                 "job_id": s.job_id,
                 "contact": ({"name": s.contact.name, "agency": s.contact.agency,
                               "email": s.contact.email} if s.contact else None)}
                for s in overdue_subs
            ],
        }


@mcp.tool()
def save_followup_draft(job_id: int, email_text: str) -> dict:
    """Save an AI-drafted follow-up email to a job record.

    job_id: the job this follow-up is for.
    email_text: the full draft email including subject line.
    Returns confirmation or an error dict.
    """
    with flask_app.app_context():
        j = db.session.get(Job, job_id)
        if not j:
            return {"error": f"no job with id {job_id}"}
        j.followup_draft = (email_text or "").strip()
        db.session.commit()
        return {"ok": True, "job_id": job_id, "company": j.company, "title": j.title}


@mcp.tool()
def save_interview_prep(job_id: int, prep_notes: str) -> dict:
    """Save an AI-generated interview prep guide to the most recent interview record for a job.

    If no interview record exists yet for this job, saves the notes to the job's
    general notes field instead and returns a warning.

    job_id: the job being prepped for.
    prep_notes: the full prep guide text.
    """
    with flask_app.app_context():
        j = db.session.get(Job, job_id)
        if not j:
            return {"error": f"no job with id {job_id}"}
        if j.interviews:
            # Save to the most recent interview record.
            iv = sorted(j.interviews, key=lambda x: x.created_at, reverse=True)[0]
            iv.prep_notes = (prep_notes or "").strip()
            db.session.commit()
            return {"ok": True, "job_id": job_id, "interview_id": iv.id,
                    "message": f"Prep guide saved to interview record for {j.company}."}
        else:
            # No interview record yet — append to job notes.
            existing = (j.notes or "").rstrip()
            j.notes = (existing + "\n\n--- INTERVIEW PREP ---\n" + (prep_notes or "").strip()).lstrip()
            db.session.commit()
            return {"ok": True, "job_id": job_id, "interview_id": None,
                    "warning": "No interview record found — prep notes saved to job notes instead. "
                               "Add an interview debrief record when ready."}


@mcp.tool()
def get_weekly_summary() -> dict:
    """Return a summary of pipeline activity over the past 7 days for the Weekly Strategy Review.

    Includes: jobs added, status changes logged in job_notes, interviews completed,
    and any AIInsights created. Gives Claude the data it needs to write a coherent
    weekly review without having to parse the full pipeline manually.
    """
    from datetime import datetime as _dt, timedelta, timezone
    with flask_app.app_context():
        week_ago = _dt.now(timezone.utc).replace(tzinfo=None) - timedelta(days=7)

        new_jobs = Job.query.filter(Job.created_at >= week_ago).all()
        status_notes = (
            JobNote.query
            .filter(JobNote.note_type == "status_change")
            .filter(JobNote.created_at >= week_ago)
            .order_by(JobNote.created_at.desc())
            .all()
        )
        new_interviews = (
            db.session.query(Interview)
            .filter(Interview.created_at >= week_ago)
            .all()
        )
        recent_insights = (
            AIInsight.query
            .filter(AIInsight.created_at >= week_ago)
            .order_by(AIInsight.created_at.desc())
            .all()
        )

        return {
            "period_days": 7,
            "new_jobs_added": len(new_jobs),
            "new_jobs": [{"id": j.id, "title": j.title, "company": j.company,
                          "status": j.status, "source": j.source} for j in new_jobs],
            "status_changes": [
                {"job_id": n.job_id, "content": n.content,
                 "when": n.created_at.strftime("%Y-%m-%d")}
                for n in status_notes
            ],
            "interviews_completed": len(new_interviews),
            "interviews": [
                {"job_id": iv.job_id, "round": iv.round_type,
                 "self_rating": iv.self_rating,
                 "went_well": (iv.went_well or "")[:200],
                 "to_improve": (iv.to_improve or "")[:200]}
                for iv in new_interviews
            ],
            "insights_this_week": len(recent_insights),
        }


# ---------------------------------------------------------------------------
# OAuth 2.0 helpers
# ---------------------------------------------------------------------------

def _base_url() -> str:
    url = os.environ.get("PUBLIC_MCP_URL", "").rstrip("/")
    if not url:
        url = f"http://localhost:{_MCP_PORT}"
        _log.warning("PUBLIC_MCP_URL is not set — falling back to http://localhost:%d. Set this in production.", _MCP_PORT)
    return url


def _oauth_metadata() -> dict:
    base = _base_url()
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "revocation_endpoint": f"{base}/oauth/revoke",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
        "revocation_endpoint_auth_methods_supported": ["none"],
    }


_AUTH_PAGE = """\
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>JobSquire - Authorize Claude</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 400px; margin: 80px auto;
             padding: 0 1.25rem; color: #1a1a1a; }}
    h2 {{ margin-bottom: .25rem; }}
    p  {{ color: #555; margin-top: 0; }}
    label {{ display: block; margin: 1rem 0 .3rem; font-weight: 600; }}
    input[type=text], input[type=password] {{
      width: 100%; padding: .55rem .7rem; font-size: 1rem;
      border: 1px solid #ccc; border-radius: 6px; box-sizing: border-box;
    }}
    button {{
      margin-top: 1.4rem; width: 100%; padding: .7rem; font-size: 1rem; font-weight: 600;
      background: #6c4beb; color: #fff; border: none; border-radius: 6px; cursor: pointer;
    }}
    button:hover {{ background: #5a3dcc; }}
    .err {{ color: #c0392b; margin-top: .75rem; font-size: .95rem; }}
  </style>
</head>
<body>
  <h2>Connect Claude to JobSquire</h2>
  <p>Sign in with your JobSquire account to let Claude read and update your pipeline.</p>
  <form method="post">
    <input type="hidden" name="client_id"             value="{client_id}">
    <input type="hidden" name="redirect_uri"          value="{redirect_uri}">
    <input type="hidden" name="state"                 value="{state}">
    <input type="hidden" name="code_challenge"        value="{code_challenge}">
    <input type="hidden" name="code_challenge_method" value="{code_challenge_method}">
    <label for="u">Username</label>
    <input id="u" type="text" name="username" autocomplete="username" autofocus>
    <label for="p">Password</label>
    <input id="p" type="password" name="password" autocomplete="current-password">
    {error_html}
    <button type="submit">Authorize</button>
  </form>
</body>
</html>
"""


async def _read_body(receive) -> bytes:
    body = b""
    while True:
        ev = await receive()
        body += ev.get("body", b"")
        if not ev.get("more_body"):
            break
    return body


async def _respond(send, status: int, body, content_type: str = "application/json"):
    if isinstance(body, (dict, list)):
        body = json.dumps(body).encode()
    elif isinstance(body, str):
        body = body.encode()
    await send({
        "type": "http.response.start", "status": status,
        "headers": [
            (b"content-type", content_type.encode()),
            (b"content-length", str(len(body)).encode()),
            (b"cache-control", b"no-store"),
        ],
    })
    await send({"type": "http.response.body", "body": body})


def _parse_qs(scope) -> dict:
    raw = scope.get("query_string", b"").decode()
    return {k: v[0] for k, v in parse_qs(raw).items()}


async def _handle_oauth_metadata(scope, receive, send):
    await _respond(send, 200, _oauth_metadata())


async def _handle_revoke(scope, receive, send):
    """RFC 7009 token revocation endpoint.

    Accepts token= in the POST body. Always returns 200 per the spec — the
    caller shouldn't be able to probe whether a given token existed.
    """
    raw = await _read_body(receive)
    params = {k: v[0] for k, v in parse_qs(raw.decode()).items()} if raw.strip() else {}
    token = params.get("token", "").strip()
    if token and token in _tokens:
        del _tokens[token]
        _save_tokens(_tokens)
    await _respond(send, 200, {})


async def _handle_register(scope, receive, send):
    raw = await _read_body(receive)
    try:
        data = json.loads(raw)
    except Exception:
        await _respond(send, 400, {"error": "invalid_request"})
        return
    client_id = secrets.token_urlsafe(16)
    # Capture client_name so it can be stored with issued tokens for the management UI.
    client_name = (data.get("client_name") or "").strip() or "Unknown client"
    _clients[client_id] = {
        "redirect_uris": data.get("redirect_uris", []),
        "client_name": client_name,
    }
    await _respond(send, 201, {
        "client_id": client_id,
        "client_name": client_name,
        "redirect_uris": data.get("redirect_uris", []),
    })


async def _handle_authorize_get(scope, receive, send):
    qs = _parse_qs(scope)
    client_id = qs.get("client_id", "")
    redirect_uri = qs.get("redirect_uri", "")
    registered = _clients.get(client_id, {}).get("redirect_uris", [])
    if not registered or redirect_uri not in registered:
        await _respond(send, 400,
                       "invalid_request: client_id unknown or redirect_uri not registered",
                       "text/plain")
        return
    code_challenge = qs.get("code_challenge", "")
    code_challenge_method = qs.get("code_challenge_method", "")
    if not code_challenge or code_challenge_method != "S256":
        await _respond(send, 400, "invalid_request: PKCE with S256 is required", "text/plain")
        return
    html = _AUTH_PAGE.format(
        client_id=client_id,
        redirect_uri=redirect_uri,
        state=qs.get("state", ""),
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
        error_html="",
    )
    await _respond(send, 200, html, "text/html; charset=utf-8")


def _get_client_ip(scope: dict) -> str:
    """Return the real client IP, reading forwarded headers when the direct
    connection comes from a private/loopback address (i.e. a reverse proxy).

    Trusts X-Real-IP set by nginx (proxy_set_header X-Real-IP $remote_addr) and
    falls back to the leftmost entry of X-Forwarded-For, which represents the
    original client before any proxies touched the chain.  Only acts on headers
    when the TCP peer is in a private range — public-IP direct connections are
    used as-is so a malicious client cannot spoof its address by injecting headers.
    """
    direct_ip = scope.get("client", ("unknown", 0))[0]
    _PRIVATE_PREFIXES = ("10.", "172.", "192.168.", "127.", "::1", "fc", "fd")
    if any(direct_ip.startswith(p) for p in _PRIVATE_PREFIXES):
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        real_ip = headers.get(b"x-real-ip", b"").decode().strip()
        if real_ip:
            return real_ip
        forwarded_for = headers.get(b"x-forwarded-for", b"").decode().strip()
        if forwarded_for:
            return forwarded_for.split(",")[0].strip()
    return direct_ip


async def _handle_authorize_post(scope, receive, send):
    raw = await _read_body(receive)
    params = {k: v[0] for k, v in parse_qs(raw.decode()).items()}

    username = params.get("username", "").strip()
    password = params.get("password", "")
    client_id = params.get("client_id", "")
    redirect_uri = params.get("redirect_uri", "")
    state = params.get("state", "")
    code_challenge = params.get("code_challenge", "")
    code_challenge_method = params.get("code_challenge_method", "")

    ip = _get_client_ip(scope)
    if not _login_rate_ok(ip):
        await _respond(send, 429, "Too many failed login attempts. Try again later.", "text/plain")
        return

    registered = _clients.get(client_id, {}).get("redirect_uris", [])
    if not registered or redirect_uri not in registered:
        await _respond(send, 400,
                       "invalid_request: client_id unknown or redirect_uri not registered",
                       "text/plain")
        return
    if not code_challenge or code_challenge_method != "S256":
        await _respond(send, 400, "invalid_request: PKCE with S256 is required", "text/plain")
        return

    # Validate against Job Squire DB. Any valid account (admin or user) may
    # authorize an MCP client — this is intentional for this two-user app.
    authed = False
    with flask_app.app_context():
        from .models import User
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password_hash, password):
            authed = True

    if not authed:
        _record_login_failure(ip)
        html = _AUTH_PAGE.format(
            client_id=client_id, redirect_uri=redirect_uri, state=state,
            code_challenge=code_challenge, code_challenge_method=code_challenge_method,
            error_html='<p class="err">Incorrect username or password.</p>',
        )
        await _respond(send, 200, html, "text/html; charset=utf-8")
        return

    _login_failures.pop(ip, None)
    code = secrets.token_urlsafe(32)
    _codes[code] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "code_challenge_method": code_challenge_method,
        "exp": time.time() + 600,
    }
    sep = "&" if "?" in redirect_uri else "?"
    location = f"{redirect_uri}{sep}code={code}&state={state}"
    await send({
        "type": "http.response.start", "status": 302,
        "headers": [
            (b"location", location.encode()),
            (b"cache-control", b"no-store"),
        ],
    })
    await send({"type": "http.response.body", "body": b""})


async def _handle_token(scope, receive, send):
    # Claude.ai may send token params in the query string rather than the body.
    # Merge both; body takes precedence so form submissions still win.
    qs_params = _parse_qs(scope)
    raw = await _read_body(receive)
    body_params = {k: v[0] for k, v in parse_qs(raw.decode()).items()} if raw.strip() else {}
    params = {**qs_params, **body_params}

    code = params.get("code", "")
    code_verifier = params.get("code_verifier", "")

    rec = _codes.pop(code, None)
    if not rec or rec["exp"] < time.time():
        await _respond(send, 400, {"error": "invalid_grant",
                                   "error_description": "code expired or not found"})
        return

    # PKCE verification (S256) — always required. /authorize refuses to issue a
    # code without an S256 challenge, so a record lacking one is rejected here too.
    expected_challenge = rec.get("code_challenge")
    digest = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).rstrip(b"=").decode()
    if not expected_challenge or digest != expected_challenge:
        await _respond(send, 400, {"error": "invalid_grant",
                                   "error_description": "PKCE verification failed"})
        return

    client_id = rec["client_id"]
    client_name = _clients.get(client_id, {}).get("client_name", "Unknown client")
    now = time.time()

    # Revoke any prior tokens for this client so re-auth doesn't accumulate stale entries.
    stale = [k for k, v in _tokens.items() if v.get("client_id") == client_id]
    for k in stale:
        del _tokens[k]

    token = secrets.token_urlsafe(48)
    _tokens[token] = {
        "client_id": client_id,
        "client_name": client_name,
        "issued_at": now,
        "exp": now + _TOKEN_TTL,
    }
    _save_tokens(_tokens)
    await _respond(send, 200, {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": _TOKEN_TTL,
    })


def _extract_bearer(scope) -> str:
    for name, val in scope.get("headers", []):
        if name.lower() == b"authorization":
            v = val.decode()
            if v.lower().startswith("bearer "):
                return v[7:].strip()
    return ""


# ---------------------------------------------------------------------------
# Main ASGI app
# ---------------------------------------------------------------------------

_inner = mcp.streamable_http_app()


async def _send_json(send, status, body):
    data = json.dumps(body).encode()
    await send({
        "type": "http.response.start", "status": status,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(data)).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": data})


def _scope_for_inner(scope: dict, path: str) -> dict:
    """Rewrite the request path before handing off to FastMCP's _inner app."""
    return {**scope, "path": path, "raw_path": path.encode()}


async def asgi_app(scope, receive, send):
    if scope["type"] != "http":
        await _inner(scope, receive, send)
        return

    path = scope.get("path", "")
    method = scope.get("method", "GET").upper()

    # Health check — always open
    if path == "/health":
        await _send_json(send, 200, {"ok": True})
        return

    # RFC 9396 Protected Resource Metadata — Claude probes this before falling
    # back to oauth-authorization-server.  Return enough to point it at our AS.
    if path in ("/.well-known/oauth-protected-resource",) or \
            path.startswith("/.well-known/oauth-protected-resource/"):
        base = _base_url()
        await _respond(send, 200, {"resource": base, "authorization_servers": [base]})
        return

    # OAuth Authorization Server Metadata (RFC 8414)
    if path == "/.well-known/oauth-authorization-server":
        await _handle_oauth_metadata(scope, receive, send)
        return

    # OAuth Dynamic Client Registration
    # Claude.ai strips the path prefix from registration_endpoint, so we handle both.
    if path in ("/oauth/register", "/register") and method == "POST":
        await _handle_register(scope, receive, send)
        return

    # OAuth Authorization endpoint
    # Claude.ai ignores authorization_endpoint from metadata and appends /authorize to the
    # server base URL, so we handle both /oauth/authorize and /authorize.
    if path in ("/oauth/authorize", "/authorize"):
        if method == "GET":
            await _handle_authorize_get(scope, receive, send)
        else:
            await _handle_authorize_post(scope, receive, send)
        return

    # OAuth Token endpoint
    # Same issue — Claude.ai appends /token to base URL instead of using token_endpoint.
    if path in ("/oauth/token", "/token") and method == "POST":
        await _handle_token(scope, receive, send)
        return

    # OAuth Revocation endpoint (RFC 7009)
    if path in ("/oauth/revoke", "/revoke") and method == "POST":
        await _handle_revoke(scope, receive, send)
        return

    # MCP endpoint — accept OAuth Bearer token OR static API key
    bearer = _extract_bearer(scope)

    # Static API key auth: key stored encrypted in AIConfig.mcp_api_key_enc.
    # Intended for scripts and non-Claude tools that can't do OAuth.
    _api_key = ""
    with flask_app.app_context():
        from .crypto import decrypt as _dec
        cfg = db.session.get(AIConfig, 1)
        if cfg and cfg.mcp_api_key_enc:
            try:
                _api_key = _dec(flask_app.config["SECRET_KEY"], cfg.mcp_api_key_enc)
            except Exception:
                pass
    if bearer and _api_key and secrets.compare_digest(bearer, _api_key):
        await _inner(_scope_for_inner(scope, "/mcp"), receive, send)
        return

    # OAuth Bearer token (issued by /oauth/token above).
    # Reload from disk on every check so that revocations made by the main-app
    # web process (which edits oauth_tokens.json directly) take effect immediately
    # without requiring an MCP server restart.
    _tokens.clear()
    _tokens.update(_load_tokens())
    if bearer and bearer in _tokens and _tokens[bearer]["exp"] > time.time():
        await _inner(_scope_for_inner(scope, "/mcp"), receive, send)
        return

    await _send_json(send, 401, {"error": "unauthorized"})


def main():
    port = int(os.environ.get("MCP_PORT", "9000"))
    uvicorn.run(asgi_app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
