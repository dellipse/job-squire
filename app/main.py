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
"""Main application blueprint: jobs, dashboard, debriefs, uploads, export/import."""
import csv
import hmac
import io
import json
import logging
import os
import re
import time
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from functools import wraps

import markdown as markdown_lib

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from flask_login import current_user, login_required
from sqlalchemy import func, text
from werkzeug.utils import secure_filename

from . import ai, privacy
from .backup import build_backup_archive
from .crypto import decrypt, dump_encrypted_json, encrypt, load_encrypted_json
from .db_utils import commit
from .mcp_auth import expires_at_from_ttl_hours, generate_token, is_network_reachable

log = logging.getLogger(__name__)
from .extensions import csrf, db
from .forms import (
    AIImportForm,
    AttachmentForm,
    CandidateAssetEditForm,
    CandidateAssetForm,
    ConfirmForm,
    ContactForm,
    InterviewForm,
    JobForm,
    KitForm,
    SubmissionForm,
)
from .models import (
    ACTIVE_STATUSES,
    ACTIVE_SUBMISSION_STATUSES,
    ASSET_KINDS,
    CONTACT_TYPES,
    STATUSES,
    AIConfig,
    AIInsight,
    AIProviderConfig,
    Attachment,
    CandidateAsset,
    Contact,
    Interview,
    Job,
    JobNote,
    KitConfig,
    ProviderCredential,
    SearchConfig,
    SearchRun,
    SmtpConfig,
    Submission,
)
from .notify import send_email
from .providers import PROVIDERS, search_provider
from .timezones import parse_state
from .search import ingest_jobs, run_search

main_bp = Blueprint("main", __name__)


def _csv_safe(value):
    """Prevent CSV formula injection by prefixing dangerous leading characters."""
    if value and isinstance(value, str) and value[0] in ('=', '+', '-', '@', '\t', '\r'):
        return "'" + value
    return value or ""

# --------------------------------------------------------------------------
# Jobs list: sort / pagination helpers
# --------------------------------------------------------------------------
_SORT_COLUMNS = {
    "company": lambda: Job.company,
    "title": lambda: Job.title,
    "location": lambda: Job.location,
    "status": lambda: Job.status,
    "date_applied": lambda: Job.date_applied,
    "follow_up_date": lambda: Job.follow_up_date,
    "created_at": lambda: Job.created_at,
    "ai_fit_score": lambda: Job.ai_fit_score,
}
_DEFAULT_SORT = "created_at:desc"


def _parse_sort(sort_str):
    """'company:asc,title:desc' → [('company','asc'), ('title','desc')]"""
    result, seen = [], set()
    for part in (sort_str or "").split(","):
        part = part.strip()
        if not part:
            continue
        col, _, direction = part.partition(":")
        col = col.strip().lower()
        direction = (direction.strip().lower() or "asc")
        if col in _SORT_COLUMNS and direction in ("asc", "desc") and col not in seen:
            result.append((col, direction))
            seen.add(col)
    return result


def _apply_sort(query, sort_cols):
    """Apply multi-column sort; NULLs always sort last."""
    if not sort_cols:
        return query.order_by(
            Job.date_applied.is_(None),
            Job.date_applied.desc(),
            Job.updated_at.desc(),
        )
    clauses = []
    for col_name, direction in sort_cols:
        col = _SORT_COLUMNS[col_name]()
        clauses.append(col.is_(None))   # False(0) = non-null first
        clauses.append(col.asc() if direction == "asc" else col.desc())
    return query.order_by(*clauses)


@main_bp.app_context_processor
def _inject_globals():
    """Inject globals available to every template."""
    try:
        cfg = _singleton(AIConfig)
        mode = cfg.mode
        claude_buttons_enabled = bool(getattr(cfg, "claude_buttons_enabled", False))
        ai_api_enabled = bool(getattr(cfg, "api_enabled", False))
    except Exception as _e:  # noqa: BLE001 - DB may not be ready on very first request
        log.warning("_inject_globals: exception reading AIConfig: %s", _e)
        mode = "manual"
        claude_buttons_enabled = False
        ai_api_enabled = False
    return {
        "ai_mode": mode,
        "claude_buttons_enabled": claude_buttons_enabled,
        "ai_api_enabled": ai_api_enabled,
        "build_version": os.environ.get("BUILD_VERSION", "dev"),
        "build_year": datetime.now(timezone.utc).year,
    }


def _claude_search_prompt():
    """Prompt for the 'Search jobs in Claude' button (uses the saved search targets)."""
    ai_cfg = db.session.get(AIConfig, 1)
    cname = (ai_cfg.connector_name if ai_cfg else None) or "job-squire"
    return (
        f'Use my job-search connectors (Indeed, ZipRecruiter) to find current postings. '
        f'First call the "{cname}" connector\'s get_search_targets tool to get my exact titles, '
        f'location, and criteria. Then search those connectors and collect matching jobs. '
        f'For each new posting, call the "{cname}" connector\'s add_jobs tool with an array of '
        f'objects in this format: {{"title": "...", "company": "...", "location": "...", '
        f'"url": "...", "salary": "...", "source": "<board name, e.g. indeed>", '
        f'"external_id": "<board\'s job id>", "description": "..."}}. '
        f'The tool returns how many were added vs. skipped as duplicates.'
    )


def _bookmarklet_js(app_origin: str) -> str:
    """Return a clean single-line bookmarklet JavaScript string.

    The bookmarklet opens /jobs/new on Job Squire with title, company,
    location, and URL pre-filled from the current job-board page.
    Generated server-side so the origin is baked in and there are no
    Jinja whitespace / CSP rendering issues.
    """
    new_url = app_origin.rstrip("/") + "/jobs/new"
    code = (
        "javascript:(function(){"
        "var t='',c='',l='';"
        # Indeed
        "var jt=document.querySelector('[data-testid=\"jobsearch-JobInfoHeader-title\"],.jobsearch-JobInfoHeader-title,h1.jobTitle');"
        "if(jt)t=jt.innerText.trim();"
        "var co=document.querySelector('[data-testid=\"inlineHeader-companyName\"] a,[data-testid=\"inlineHeader-companyName\"]');"
        "if(co)c=co.innerText.split('\\n')[0].trim();"
        # LinkedIn
        "if(!t){var e=document.querySelector('.job-details-jobs-unified-top-card__job-title h1,.jobs-unified-top-card__job-title h2');if(e)t=e.innerText.trim();}"
        "if(!c){var e=document.querySelector('.job-details-jobs-unified-top-card__company-name a,.jobs-unified-top-card__company-name a');if(e)c=e.innerText.trim();}"
        # ZipRecruiter
        "if(!t){var e=document.querySelector('h1.job_title,h1[class*=\"title\"]');if(e)t=e.innerText.trim();}"
        "if(!c){var e=document.querySelector('a[class*=\"hiring_company_text\"],span[class*=\"company\"]');if(e)c=e.innerText.trim();}"
        # Generic page-title fallback
        "if(!t&&!c){var s=document.title.replace(/\\s*[|·\\-–]\\s*/g,'|').split('|');"
        "if(s.length>=2){t=s[0].trim();c=s[1].trim();}else{t=document.title.trim();}}"
        # Location
        "var le=document.querySelector('[data-testid=\"job-location\"],.jobsearch-JobInfoHeader-subtitle span,.jobs-unified-top-card__bullet');"
        "if(le)l=le.innerText.trim();"
        "var p=new URLSearchParams({title:t,company:c,location:l,url:window.location.href,source:'bookmarklet'});"
        f"window.open('{new_url}?'+p.toString(),'_blank');"
        "})();"
    )
    return code


def _gcal_interview_url(iv, job):
    """Build a Google Calendar event-creation URL for an interview.

    Uses all-day event format (YYYYMMDD/YYYYMMDD+1) so no time zone is needed.
    """
    from urllib.parse import urlencode
    if not iv.interview_date:
        return None
    start = iv.interview_date
    end = start + timedelta(days=1)
    round_label = f" ({iv.round_type})" if iv.round_type else ""
    title = f"Interview: {job.title} at {job.company}{round_label}"
    details_parts = []
    if iv.interviewer:
        details_parts.append(f"Interviewer: {iv.interviewer}")
    if iv.interview_format:
        details_parts.append(f"Format: {iv.interview_format}")
    details_parts.append(f"Job Job Squire: /jobs/{job.id}")
    details = "\n".join(details_parts)
    params = {
        "text": title,
        "dates": f"{start.strftime('%Y%m%d')}/{end.strftime('%Y%m%d')}",
        "details": details,
    }
    return "https://calendar.google.com/calendar/r/eventedit?" + urlencode(params)


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(403)
        return f(*args, **kwargs)

    return wrapper


def _business_days_from(start, n):
    """Return the date n business days after start (skips Sat/Sun)."""
    from datetime import timedelta
    d = start
    added = 0
    while added < n:
        d += timedelta(days=1)
        if d.weekday() < 5:   # 0=Mon … 4=Fri
            added += 1
    return d


def _add_job_note(job_id, content, note_type="note"):
    """Append a timestamped log entry to a job. Must be followed by commit()."""
    author = ""
    try:
        from flask_login import current_user as _cu
        if _cu.is_authenticated:
            author = _cu.display_name or _cu.username
    except Exception:  # noqa: BLE001
        pass
    note = JobNote(job_id=job_id, note_type=note_type, content=content, created_by=author)
    db.session.add(note)


def _apply_job_form(job, form):
    job.company = form.company.data.strip()
    job.title = form.title.data.strip()
    job.location = (form.location.data or "").strip()
    job.work_mode = form.work_mode.data
    job.status = form.status.data
    job.source = (form.source.data or "").strip()
    job.url = (form.url.data or "").strip()
    job.salary = (form.salary.data or "").strip()
    job.date_applied = form.date_applied.data
    job.follow_up_date = form.follow_up_date.data
    job.contact_name = (form.contact_name.data or "").strip()
    job.contact_email = (form.contact_email.data or "").strip()
    job.notes = form.notes.data or ""


# --------------------------------------------------------------------------
# Health check (used by docker-compose healthcheck; no auth required)
# --------------------------------------------------------------------------
@main_bp.route("/health")
def health():
    return Response('{"ok": true}', status=200, mimetype="application/json")


def _worker_heartbeat_status(max_age_seconds=900):
    """Read the worker's heartbeat file and report whether it looks alive.

    app/worker.py touches DATA_DIR/.worker_heartbeat on startup and every
    HEARTBEAT_INTERVAL_MINUTES thereafter (default 5), independent of whether
    automated search is enabled or due to run. So "stale" here means the
    worker process/scheduler has died or wedged -- it is not a
    statement about search being disabled or merely idle between runs. This
    backs the same signal the container's own aggregated healthcheck probes
    (see root/etc/s6-overlay/scripts/healthcheck), but surfaced in-app
    (Dashboard + Settings > History) so it doesn't require running
    `docker ps` to notice.

    Returns a dict: {"last_seen": aware datetime | None, "stale": bool}.
    """
    data_dir = current_app.config.get("DATA_DIR") or os.environ.get("DATA_DIR", "/data")
    path = os.path.join(data_dir, ".worker_heartbeat")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {"last_seen": None, "stale": True}
    return {
        "last_seen": datetime.fromtimestamp(mtime, tz=timezone.utc),
        "stale": (time.time() - mtime) > max_age_seconds,
    }


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------
@main_bp.route("/")
@login_required
def dashboard():
    # Getting Started (admins only; hides itself once dismissed/complete).
    # Fresh installs land on the persona step, then the checklist overview,
    # until onboarding is done — this is the app's post-login landing route,
    # so gating it here covers both a fresh /login redirect and a remembered
    # session hitting "/" directly.
    onboarding_checklist = None
    if current_user.is_admin:
        from .onboarding import checklist_for_dashboard, get_onboarding_redirect
        redirect_target = get_onboarding_redirect()
        if redirect_target:
            return redirect(redirect_target)
        onboarding_checklist = checklist_for_dashboard()

    jobs = Job.query.all()
    total = len(jobs)
    rows = db.session.query(Job.status, func.count(Job.id)).group_by(Job.status).all()
    counts = {s: 0 for s in STATUSES}
    for status, cnt in rows:
        if status in counts:
            counts[status] = cnt

    applied = sum(counts.get(s, 0) for s in
                  ["Applied", "Phone Screen", "Interview", "Final Interview", "Offer", "Hired", "Rejected", "Ghosted"])
    reached_interview = sum(counts.get(s, 0) for s in
                            ["Interview", "Final Interview", "Offer", "Hired"])
    offers = counts.get("Offer", 0) + counts.get("Hired", 0)
    active = sum(1 for j in jobs if j.status in ACTIVE_STATUSES)

    def pct(n, d):
        return round(100 * n / d) if d else 0

    metrics = {
        "total": total,
        "active": active,
        "applied": applied,
        "reached_interview": reached_interview,
        "offers": offers,
        "interview_rate": pct(reached_interview, applied),
        "offer_rate": pct(offers, applied),
    }

    follow_ups = (
        Job.query.filter(Job.follow_up_date.isnot(None))
        .filter(Job.status.in_(list(ACTIVE_STATUSES)))
        .order_by(Job.follow_up_date.asc())
        .all()
    )
    follow_ups = [j for j in follow_ups if j.follow_up_date <= date.today()]

    recent = Job.query.order_by(Job.updated_at.desc()).limit(8).all()
    latest_insight = AIInsight.query.order_by(AIInsight.created_at.desc()).first()

    # Networking: recruiter follow-ups due and active submissions.
    contact_follow_ups = (
        Contact.query.filter(Contact.follow_up_date.isnot(None))
        .filter(Contact.follow_up_date <= date.today())
        .order_by(Contact.follow_up_date.asc())
        .all()
    )
    open_submissions = (
        Submission.query.filter(Submission.status.in_(list(ACTIVE_SUBMISSION_STATUSES)))
        .order_by(Submission.submitted_date.is_(None), Submission.submitted_date.desc())
        .all()
    )
    metrics["contacts"] = Contact.query.count()
    metrics["open_submissions"] = len(open_submissions)

    # Stale job detection.
    _stale_cutoff_naive = datetime.utcnow()
    _stale_saved_cutoff = _stale_cutoff_naive - timedelta(days=14)
    _stale_active_cutoff = _stale_cutoff_naive - timedelta(days=21)
    _stale_active_statuses = ["Applied", "Phone Screen", "Interview", "Final Interview"]
    stale_saved_count = Job.query.filter(
        Job.status == "Saved",
        Job.created_at <= _stale_saved_cutoff,
    ).count()
    stale_active_count = Job.query.filter(
        Job.status.in_(_stale_active_statuses),
        Job.updated_at <= _stale_active_cutoff,
    ).count()

    # Routine status widget counts.
    unscored_count = (
        Job.query.filter(Job.status == "Saved")
        .filter((Job.ai_fit_score == None) | (Job.ai_fit_score == 0))  # noqa: E711
        .count()
    )
    unkitted_count = (
        Job.query.filter(Job.status == "Applied")
        .filter((Job.kit_output == None) | (Job.kit_output == ""))  # noqa: E711
        .count()
    )
    overdue_followup_count = len([j for j in follow_ups if not (j.followup_draft or "").strip()])

    worker_health = _worker_heartbeat_status()

    return render_template(
        "dashboard.html",
        onboarding_checklist=onboarding_checklist,
        metrics=metrics,
        counts=counts,
        statuses=STATUSES,
        follow_ups=follow_ups,
        recent=recent,
        today=date.today(),
        latest_insight=latest_insight,
        contact_follow_ups=contact_follow_ups,
        open_submissions=open_submissions,
        unscored_count=unscored_count,
        unkitted_count=unkitted_count,
        overdue_followup_count=overdue_followup_count,
        stale_saved_count=stale_saved_count,
        stale_active_count=stale_active_count,
        worker_stale=worker_health["stale"],
        worker_last_seen=worker_health["last_seen"],
    )


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------
@main_bp.route("/jobs")
@login_required
def jobs_list():
    # Load the current user's saved default view (may be None if never set).
    _user_default_sort = getattr(current_user, "jobs_default_sort", None) or _DEFAULT_SORT
    _user_default_status = getattr(current_user, "jobs_default_status", None) or ""
    _user_default_per_page = getattr(current_user, "jobs_default_per_page", None) or 25

    # If the user arrives at /jobs with no query params at all (fresh navigation),
    # apply their saved default view rather than bare system defaults.
    _fresh_load = not request.args

    status = request.args.get("status", "").strip()
    if _fresh_load:
        status = _user_default_status

    q = request.args.get("q", "").strip()

    # --- Per-page (session-persisted; user default as fallback) ---
    if "per_page" in request.args:
        try:
            per_page = int(request.args["per_page"])
        except (ValueError, TypeError):
            per_page = 25
        if per_page not in (10, 25, 50, 100, 0):
            per_page = 25
        session["jobs_per_page"] = per_page
    else:
        per_page = session.get("jobs_per_page", _user_default_per_page)

    # --- Page number ---
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (ValueError, TypeError):
        page = 1

    # --- Sort (session-persisted; user default as fallback; explicit empty = reset to user default) ---
    if "sort" in request.args:
        sort_str = request.args["sort"].strip()
        if sort_str:
            session["jobs_sort"] = sort_str
        else:
            session.pop("jobs_sort", None)
            sort_str = _user_default_sort
    else:
        sort_str = session.get("jobs_sort", _user_default_sort)

    sort_cols = _parse_sort(sort_str)

    # --- Build query ---
    query = Job.query
    _stale_filter_now = datetime.utcnow()
    _stale_filter_saved_cutoff = _stale_filter_now - timedelta(days=14)
    _stale_filter_active_cutoff = _stale_filter_now - timedelta(days=21)
    _stale_filter_active_statuses = ["Applied", "Phone Screen", "Interview", "Final Interview"]
    if status == "active":
        query = query.filter(Job.status.in_(list(ACTIVE_STATUSES)))
    elif status == "all":
        pass  # no filter — show everything including Pass
    elif status == "stale":
        query = query.filter(
            db.or_(
                db.and_(Job.status == "Saved", Job.created_at <= _stale_filter_saved_cutoff),
                db.and_(Job.status.in_(_stale_filter_active_statuses), Job.updated_at <= _stale_filter_active_cutoff),
            )
        )
    elif status == "unkitted":
        query = query.filter(
            Job.status == "Applied",
            db.or_(Job.kit_output == None, Job.kit_output == ""),  # noqa: E711
        )
    elif status and status in STATUSES:
        query = query.filter(Job.status == status)
    else:
        # Default: hide Pass jobs so they don't clutter the main view.
        query = query.filter(Job.status != "Pass")
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(Job.company.ilike(like), Job.title.ilike(like),
                                    Job.location.ilike(like)))
    query = _apply_sort(query, sort_cols)

    # --- Paginate ---
    if per_page == 0:
        jobs = query.all()
        pagination = None
        total = len(jobs)
    else:
        pagination = query.paginate(page=page, per_page=per_page, error_out=False)
        jobs = pagination.items
        total = pagination.total

    sort_dir = {col: d for col, d in sort_cols}
    sort_priority = {col: i + 1 for i, (col, _) in enumerate(sort_cols)}

    # Staleness: flag jobs that have gone quiet.
    # Saved >14 days → "stale lead" (posting may have expired).
    # Applied/active >21 days no update → "stale active" (may be ghosted).
    _now = datetime.utcnow()
    _saved_cutoff = _now - timedelta(days=14)
    _active_cutoff = _now - timedelta(days=21)
    _stale_active = {"Applied", "Phone Screen", "Interview", "Final Interview"}
    stale_map: dict[int, str] = {}
    for _j in jobs:
        if _j.status == "Saved" and _j.created_at and _j.created_at <= _saved_cutoff:
            stale_map[_j.id] = "stale-lead"
        elif _j.status in _stale_active and _j.updated_at and _j.updated_at <= _active_cutoff:
            stale_map[_j.id] = "stale-active"

    return render_template(
        "jobs.html",
        jobs=jobs,
        statuses=STATUSES,
        current_status=status,
        q=q,
        today=date.today(),
        confirm_form=ConfirmForm(),
        search_prompt=_claude_search_prompt(),
        sort_str=sort_str,
        sort_dir=sort_dir,
        sort_priority=sort_priority,
        per_page=per_page,
        page=page,
        pagination=pagination,
        total=total,
        stale_map=stale_map,
        has_default_view=bool(getattr(current_user, "jobs_default_sort", None)),
    )


@main_bp.route("/jobs/save-default-view", methods=["POST"])
@login_required
def jobs_save_default_view():
    """Save the current filter/sort/per-page combination as the user's default view."""
    sort_str = request.form.get("sort", "").strip() or _DEFAULT_SORT
    status = request.form.get("status", "").strip()
    try:
        per_page = int(request.form.get("per_page", 25))
    except (ValueError, TypeError):
        per_page = 25
    if per_page not in (10, 25, 50, 100, 0):
        per_page = 25

    # Validate sort string before saving.
    if _parse_sort(sort_str):
        current_user.jobs_default_sort = sort_str
    else:
        current_user.jobs_default_sort = _DEFAULT_SORT

    current_user.jobs_default_status = status
    current_user.jobs_default_per_page = per_page
    commit()
    flash("Default view saved.", "success")
    return redirect(url_for("main.jobs_list", sort=sort_str, status=status, per_page=per_page))


@main_bp.route("/jobs/clear-default-view", methods=["POST"])
@login_required
def jobs_clear_default_view():
    """Clear the user's saved default view (revert to system defaults)."""
    current_user.jobs_default_sort = None
    current_user.jobs_default_status = None
    current_user.jobs_default_per_page = None
    commit()
    flash("Default view cleared.", "success")
    return redirect(url_for("main.jobs_list"))


@main_bp.route("/jobs/new", methods=["GET", "POST"])
@login_required
def job_new():
    form = JobForm()
    if request.method == "GET":
        # Default values.
        form.status.data = "Applied"
        form.date_applied.data = date.today()
        # Pre-fill from GET params (used by the quick-apply bookmarklet).
        # Only safe string fields are accepted; URL is validated by the form.
        _prefill_map = {
            "title": "title", "company": "company", "location": "location",
            "url": "url", "salary": "salary", "source": "source",
            "status": "status", "notes": "notes",
        }
        for param, field_name in _prefill_map.items():
            val = request.args.get(param, "").strip()
            if val:
                getattr(form, field_name).data = val
    if form.validate_on_submit():
        job = Job(created_by=current_user.display_name or current_user.username)
        _apply_job_form(job, form)
        db.session.add(job)
        commit()
        flash("Job added.", "success")
        return redirect(url_for("main.job_detail", job_id=job.id))
    return render_template("job_form.html", form=form, mode="new")


@main_bp.route("/jobs/<int:job_id>")
@login_required
def job_detail(job_id):
    job = db.get_or_404(Job, job_id)
    ai_cfg = _singleton(AIConfig)
    gcal_urls = {iv.id: _gcal_interview_url(iv, job) for iv in job.interviews}
    return render_template("job_detail.html", job=job, today=date.today(),
                           confirm_form=ConfirmForm(), ai_mode=ai_cfg.mode,
                           connector_name=ai_cfg.connector_name or "job-squire",
                           gcal_urls=gcal_urls)


@main_bp.route("/jobs/<int:job_id>/edit", methods=["GET", "POST"])
@login_required
def job_edit(job_id):
    job = db.get_or_404(Job, job_id)
    form = JobForm(obj=job)
    if form.validate_on_submit():
        old_status = job.status
        old_followup = job.follow_up_date
        _apply_job_form(job, form)
        # Auto-log status change
        if job.status != old_status:
            _add_job_note(job.id, f"Status changed: {old_status} → {job.status}.",
                          note_type="status_change")
        # Auto-log follow-up date change
        if job.follow_up_date != old_followup:
            if job.follow_up_date:
                _add_job_note(job.id, f"Follow-up date set to {job.follow_up_date}.",
                              note_type="follow_up")
            else:
                _add_job_note(job.id, "Follow-up date cleared.", note_type="follow_up")
        commit()
        flash("Job updated.", "success")
        return redirect(url_for("main.job_detail", job_id=job.id))
    return render_template("job_form.html", form=form, mode="edit", job=job)


def _run_single_job_ai_task(job_id: int, task_name: str, label: str, work_fn):
    """Launch a single-job AI action (ats-gap, score-fit, draft-followup) in a daemon
    thread and redirect to the live status page, instead of blocking the gunicorn
    worker for the duration of the AI call.

    Slow or stalling providers used to hold the request open past gunicorn's hard
    --timeout, which SIGABRTs the worker mid-call and surfaces as a bare 500 with
    no chance for our own try/except to run (see job 1162 ats-gap incident,
    2026-07-01). Routing through the same background-thread + poll pattern already
    used for triage/followup/weekly_review/build_kit fixes that at the root.

    work_fn(job) must return a dict of extra fields to merge into the status result
    (e.g. {"score": .., "reason": ..}) and is expected to persist its own changes
    (commit()) before returning.
    """
    job = db.get_or_404(Job, job_id)
    ai_cfg = _singleton(AIConfig)
    if not ai_cfg.api_enabled:
        flash(f"{label} requires Automatic features to be enabled in Settings.", "warning")
        return redirect(url_for("main.job_detail", job_id=job_id))

    run_id = uuid.uuid4().hex
    data_dir = current_app.config["DATA_DIR"]
    status = _TaskStatus(run_id, task_name, data_dir)
    _app = current_app._get_current_object()
    ai_log = logging.getLogger("app.ai")
    title, company = job.title, job.company

    def _run():
        handler = _StatusLogHandler(status)
        prior_level = ai_log.level
        ai_log.addHandler(handler)
        ai_log.setLevel(logging.INFO)
        with _app.app_context():
            try:
                status.log(f"INFO Running {label} for {title} at {company}…")
                j = db.session.get(Job, job_id)
                if j is None:
                    raise RuntimeError(f"Job {job_id} no longer exists")
                extra = work_fn(j) or {}
                status.done({"job_id": job_id, "title": title, "company": company, **extra})
            except Exception as exc:  # noqa: BLE001
                db.session.rollback()
                log.exception("%s failed (job_id=%s)", task_name, job_id)
                status.fail(exc)
            finally:
                ai_log.removeHandler(handler)
                ai_log.setLevel(prior_level)

    threading.Thread(target=_run, daemon=True).start()
    return redirect(url_for("main.ai_task_status", run_id=run_id, task=task_name))


@main_bp.route("/jobs/<int:job_id>/ats-gap", methods=["POST"])
@login_required
def job_ats_gap(job_id):
    """Feature 4: Run ATS keyword gap analysis for a job via the API."""
    def _work(job):
        from .ai import run_ats_analysis, _load_candidate_profile
        profile = _load_candidate_profile()
        parsed = run_ats_analysis(job, profile)
        return {
            "overall_match_estimate": parsed.get("overall_match_estimate", ""),
            "missing_count": len(parsed.get("missing_keywords", []) or []),
        }
    return _run_single_job_ai_task(job_id, "ats_gap", "ATS gap analysis", _work)


@main_bp.route("/jobs/<int:job_id>/score-fit", methods=["POST"])
@login_required
def job_score_fit(job_id):
    """Score a single job's fit via the API (api_mode button on job detail)."""
    def _work(job):
        from .ai import run_score_fit_single
        return run_score_fit_single(job)
    return _run_single_job_ai_task(job_id, "score_fit", "Score fit", _work)


@main_bp.route("/jobs/<int:job_id>/draft-followup", methods=["POST"])
@login_required
def job_draft_followup(job_id):
    """Draft a follow-up email for a single job via the API (api_mode button on job detail)."""
    def _work(job):
        from .ai import run_draft_followup_single
        return run_draft_followup_single(job)
    return _run_single_job_ai_task(job_id, "draft_followup", "Draft follow-up", _work)



@main_bp.route("/jobs/build-kits-api", methods=["POST"])
@login_required
def jobs_build_kits_api():
    """Build application kits for all Applied jobs that don't have one yet.

    Runs in a background thread so the request returns immediately — kit
    generation can take well over Gunicorn's worker timeout when there are
    multiple jobs queued.
    """
    ai_cfg = _singleton(AIConfig)
    if not ai_cfg.api_enabled:
        flash("Build kits requires Automatic features to be enabled in Settings.", "warning")
        return redirect(url_for("main.dashboard"))
    job_ids = [
        j.id for j in (
            Job.query.filter(Job.status == "Applied")
            .filter(db.or_(Job.kit_output == None, Job.kit_output == ""))  # noqa: E711
            .with_entities(Job.id)
            .all()
        )
    ]
    if not job_ids:
        flash("No Applied jobs are missing kits.", "info")
        return redirect(url_for("main.dashboard"))

    app = current_app._get_current_object()

    def _build_all(app, job_ids):
        from .ai import run_build_kit_api
        with app.app_context():
            built, failed = 0, 0
            for jid in job_ids:
                job = db.session.get(Job, jid)
                if job is None:
                    continue
                try:
                    run_build_kit_api(job)
                    built += 1
                except Exception as exc:  # noqa: BLE001
                    db.session.rollback()
                    log.warning("jobs_build_kits_api: job %d failed: %s", jid, exc)
                    failed += 1
            log.info("jobs_build_kits_api: complete — built=%d failed=%d", built, failed)

    t = threading.Thread(target=_build_all, args=(app, job_ids), daemon=True)
    t.start()

    n = len(job_ids)
    flash(f"Building kits for {n} job{'s' if n != 1 else ''} in the background — check back in a few minutes.", "info")
    return redirect(url_for("main.dashboard"))


@main_bp.route("/jobs/<int:job_id>/prep-interview", methods=["POST"])
@login_required
def job_prep_interview(job_id):
    """Generate an interview prep guide for a single job via the API (api_mode button)."""
    job = db.get_or_404(Job, job_id)
    ai_cfg = _singleton(AIConfig)
    if not ai_cfg.api_enabled:
        flash("Interview prep requires Automatic features to be enabled in Settings.", "warning")
        return redirect(url_for("main.job_detail", job_id=job_id))
    try:
        from .ai import run_interview_prep_single
        run_interview_prep_single(job)
        flash("Interview prep guide saved.", "success")
    except Exception as exc:  # noqa: BLE001
        flash(f"Prep guide generation failed: {exc}", "danger")
    return redirect(url_for("main.job_detail", job_id=job_id))


@main_bp.route("/jobs/<int:job_id>/delete", methods=["POST"])
@login_required
@admin_required
def job_delete(job_id):
    form = ConfirmForm()
    if not form.validate_on_submit():
        abort(400)
    job = db.get_or_404(Job, job_id)
    # Remove attachment files from disk.
    for att in job.attachments:
        _delete_attachment_file(att)
    # Keep recruiter submissions, but unlink them from the deleted job.
    for sub in list(job.submissions):
        sub.job_id = None
    db.session.delete(job)
    commit()
    flash("Job deleted.", "success")
    return redirect(url_for("main.jobs_list"))


# --------------------------------------------------------------------------
# Bulk status update
# --------------------------------------------------------------------------
@main_bp.route("/jobs/bulk-update", methods=["POST"])
@login_required
def jobs_bulk_update():
    """Apply a status action to multiple jobs at once.

    Expects form fields:
      job_ids   — one or more job id values (checkbox group)
      action    — one of: set_status, withdrawn, ghosted, pass
      status    — target status string (only used when action=set_status)
    """
    form = ConfirmForm()
    if not form.validate_on_submit():
        abort(400)

    raw_ids = request.form.getlist("job_ids")
    try:
        job_ids = [int(i) for i in raw_ids if i]
    except ValueError:
        abort(400)

    if not job_ids:
        flash("No jobs selected.", "warning")
        return redirect(url_for("main.jobs_list"))

    action = request.form.get("action", "").strip()
    if action == "set_status":
        new_status = request.form.get("status", "").strip()
        if new_status not in STATUSES:
            flash("Invalid status.", "error")
            return redirect(url_for("main.jobs_list"))
    elif action == "withdrawn":
        new_status = "Withdrawn"
    elif action == "ghosted":
        new_status = "Ghosted"
    elif action == "pass":
        new_status = "Pass"
    else:
        flash("Unknown action.", "error")
        return redirect(url_for("main.jobs_list"))

    updated = 0
    for job in Job.query.filter(Job.id.in_(job_ids)).all():
        if job.status != new_status:
            old_status = job.status
            job.status = new_status
            _add_job_note(job.id, f"Status changed: {old_status} → {new_status}.",
                          note_type="status_change")
            updated += 1
    commit()
    flash(f"{updated} job{'s' if updated != 1 else ''} updated to {new_status}.", "success")
    return redirect(url_for("main.jobs_list"))


# --------------------------------------------------------------------------
# Job notes / activity log
# --------------------------------------------------------------------------
@main_bp.route("/jobs/<int:job_id>/notes", methods=["POST"])
@login_required
def job_add_note(job_id):
    job = db.get_or_404(Job, job_id)
    form = ConfirmForm()
    if not form.validate_on_submit():
        abort(400)
    content = (request.form.get("content") or "").strip()
    if not content:
        flash("Note cannot be empty.", "error")
        return redirect(url_for("main.job_detail", job_id=job_id))
    _add_job_note(job.id, content, note_type="note")
    commit()
    flash("Note added.", "success")
    return redirect(url_for("main.job_detail", job_id=job_id))


@main_bp.route("/jobs/<int:job_id>/set-followup", methods=["POST"])
@login_required
def job_set_followup(job_id):
    job = db.get_or_404(Job, job_id)
    form = ConfirmForm()
    if not form.validate_on_submit():
        abort(400)
    raw = (request.form.get("follow_up_date") or "").strip()
    if raw:
        try:
            new_date = date.fromisoformat(raw)
        except ValueError:
            flash("Invalid date.", "error")
            return redirect(url_for("main.job_detail", job_id=job_id))
    else:
        new_date = _business_days_from(date.today(), 3)
    old_followup = job.follow_up_date
    job.follow_up_date = new_date
    if new_date != old_followup:
        _add_job_note(job.id, f"Follow-up date set to {new_date}.", note_type="follow_up")
    commit()
    flash(f"Follow-up date set to {new_date}.", "success")
    return redirect(url_for("main.job_detail", job_id=job_id))


# --------------------------------------------------------------------------
# Interview debriefs
# --------------------------------------------------------------------------
@main_bp.route("/jobs/<int:job_id>/interviews/new", methods=["GET", "POST"])
@login_required
def interview_new(job_id):
    job = db.get_or_404(Job, job_id)
    form = InterviewForm()
    if form.validate_on_submit():
        iv = Interview(job_id=job.id)
        _apply_interview_form(iv, form)
        db.session.add(iv)
        commit()
        flash("Interview debrief saved.", "success")
        return redirect(url_for("main.job_detail", job_id=job.id))
    return render_template("interview_form.html", form=form, job=job, mode="new",
                           gcal_url=None)


@main_bp.route("/interviews/<int:iv_id>/edit", methods=["GET", "POST"])
@login_required
def interview_edit(iv_id):
    iv = db.get_or_404(Interview, iv_id)
    form = InterviewForm(obj=iv)
    if request.method == "GET" and iv.self_rating:
        form.self_rating.data = str(iv.self_rating)
    if form.validate_on_submit():
        _apply_interview_form(iv, form)
        commit()
        flash("Interview debrief updated.", "success")
        return redirect(url_for("main.job_detail", job_id=iv.job_id))
    gcal_url = _gcal_interview_url(iv, iv.job) if iv.interview_date else None
    return render_template("interview_form.html", form=form, job=iv.job, mode="edit",
                           gcal_url=gcal_url)


@main_bp.route("/interviews/<int:iv_id>/delete", methods=["POST"])
@login_required
def interview_delete(iv_id):
    form = ConfirmForm()
    if not form.validate_on_submit():
        abort(400)
    iv = db.get_or_404(Interview, iv_id)
    job_id = iv.job_id
    db.session.delete(iv)
    commit()
    flash("Debrief removed.", "success")
    return redirect(url_for("main.job_detail", job_id=job_id))


def _apply_interview_form(iv, form):
    iv.interview_date = form.interview_date.data
    iv.round_type = (form.round_type.data or "").strip()
    iv.interview_format = form.interview_format.data or ""
    iv.interviewer = (form.interviewer.data or "").strip()
    iv.questions_asked = form.questions_asked.data or ""
    iv.self_rating = int(form.self_rating.data) if form.self_rating.data else None
    iv.went_well = form.went_well.data or ""
    iv.to_improve = form.to_improve.data or ""
    iv.notes = form.notes.data or ""


# --------------------------------------------------------------------------
# Networking: recruiter / staffing-agency contacts and submissions
# --------------------------------------------------------------------------
def _apply_contact_form(contact, form):
    contact.name = form.name.data.strip()
    contact.contact_type = form.contact_type.data
    contact.title = (form.title.data or "").strip()
    contact.agency = (form.agency.data or "").strip()
    contact.email = (form.email.data or "").strip()
    contact.phone = (form.phone.data or "").strip()
    contact.linkedin_url = (form.linkedin_url.data or "").strip()
    contact.last_contacted = form.last_contacted.data
    contact.follow_up_date = form.follow_up_date.data
    contact.notes = form.notes.data or ""


def _populate_submission_choices(form):
    contacts = Contact.query.order_by(Contact.name.asc()).all()
    form.contact_id.choices = [("", "— none —")] + [
        (str(c.id), f"{c.name}{(' · ' + c.agency) if c.agency else ''}") for c in contacts
    ]
    jobs = Job.query.order_by(Job.company.asc(), Job.title.asc()).all()
    form.job_id.choices = [("", "— none —")] + [
        (str(j.id), f"{j.company} — {j.title}") for j in jobs
    ]


def _apply_submission_form(sub, form):
    sub.contact_id = int(form.contact_id.data) if form.contact_id.data else None
    sub.job_id = int(form.job_id.data) if form.job_id.data else None
    sub.company = (form.company.data or "").strip()
    sub.role_title = (form.role_title.data or "").strip()
    sub.status = form.status.data
    sub.submitted_date = form.submitted_date.data
    sub.follow_up_date = form.follow_up_date.data
    sub.notes = form.notes.data or ""
    # If linked to a tracked job and company/role left blank, fill from the job.
    if sub.job_id and (not sub.company or not sub.role_title):
        job = db.session.get(Job, sub.job_id)
        if job:
            sub.company = sub.company or job.company
            sub.role_title = sub.role_title or job.title


@main_bp.route("/contacts")
@login_required
def contacts_list():
    ctype = request.args.get("type", "").strip()
    q = request.args.get("q", "").strip()
    query = Contact.query
    if ctype and ctype in CONTACT_TYPES:
        query = query.filter(Contact.contact_type == ctype)
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(Contact.name.ilike(like), Contact.agency.ilike(like),
                                    Contact.email.ilike(like)))
    contacts = query.order_by(Contact.follow_up_date.is_(None), Contact.follow_up_date.asc(),
                              Contact.name.asc()).all()
    return render_template("contacts.html", contacts=contacts, contact_types=CONTACT_TYPES,
                           current_type=ctype, q=q, today=date.today())


@main_bp.route("/contacts/new", methods=["GET", "POST"])
@login_required
def contact_new():
    form = ContactForm()
    if form.validate_on_submit():
        contact = Contact(created_by=current_user.display_name or current_user.username)
        _apply_contact_form(contact, form)
        db.session.add(contact)
        commit()
        flash("Contact added.", "success")
        return redirect(url_for("main.contact_detail", contact_id=contact.id))
    return render_template("contact_form.html", form=form, mode="new")


@main_bp.route("/contacts/<int:contact_id>")
@login_required
def contact_detail(contact_id):
    contact = db.get_or_404(Contact, contact_id)
    return render_template("contact_detail.html", contact=contact, today=date.today(),
                           confirm_form=ConfirmForm())


@main_bp.route("/contacts/<int:contact_id>/edit", methods=["GET", "POST"])
@login_required
def contact_edit(contact_id):
    contact = db.get_or_404(Contact, contact_id)
    form = ContactForm(obj=contact)
    if form.validate_on_submit():
        _apply_contact_form(contact, form)
        commit()
        flash("Contact updated.", "success")
        return redirect(url_for("main.contact_detail", contact_id=contact.id))
    return render_template("contact_form.html", form=form, mode="edit", contact=contact)


@main_bp.route("/contacts/<int:contact_id>/delete", methods=["POST"])
@login_required
def contact_delete(contact_id):
    if not ConfirmForm().validate_on_submit():
        abort(400)
    contact = db.get_or_404(Contact, contact_id)
    db.session.delete(contact)
    commit()
    flash("Contact deleted.", "success")
    return redirect(url_for("main.contacts_list"))


@main_bp.route("/export/contacts.csv")
@login_required
def export_contacts_csv():
    contacts = Contact.query.order_by(Contact.name.asc()).all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "Name", "Type", "Title", "Agency", "Email", "Phone", "LinkedIn",
        "Last contacted", "Follow-up date", "Open submissions", "Notes",
    ])
    for c in contacts:
        w.writerow([
            _csv_safe(c.name), _csv_safe(c.contact_type), _csv_safe(c.title),
            _csv_safe(c.agency), _csv_safe(c.email), _csv_safe(c.phone),
            _csv_safe(c.linkedin_url),
            c.last_contacted or "", c.follow_up_date or "", len(c.open_submissions),
            _csv_safe((c.notes or "").replace("\n", " ")),
        ])
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=contacts-{stamp}.csv"},
    )


@main_bp.route("/submissions/new", methods=["GET", "POST"])
@login_required
def submission_new():
    form = SubmissionForm()
    _populate_submission_choices(form)
    if form.validate_on_submit():
        sub = Submission(created_by=current_user.display_name or current_user.username)
        _apply_submission_form(sub, form)
        db.session.add(sub)
        commit()
        flash("Submission logged.", "success")
        if sub.contact_id:
            return redirect(url_for("main.contact_detail", contact_id=sub.contact_id))
        return redirect(url_for("main.contacts_list"))
    if request.method == "GET":
        form.contact_id.data = request.args.get("contact_id", "")
        form.job_id.data = request.args.get("job_id", "")
        form.submitted_date.data = date.today()
    return render_template("submission_form.html", form=form, mode="new")


@main_bp.route("/submissions/<int:sub_id>/edit", methods=["GET", "POST"])
@login_required
def submission_edit(sub_id):
    sub = db.get_or_404(Submission, sub_id)
    form = SubmissionForm(obj=sub)
    _populate_submission_choices(form)
    if request.method == "GET":
        form.contact_id.data = str(sub.contact_id) if sub.contact_id else ""
        form.job_id.data = str(sub.job_id) if sub.job_id else ""
    if form.validate_on_submit():
        _apply_submission_form(sub, form)
        commit()
        flash("Submission updated.", "success")
        if sub.contact_id:
            return redirect(url_for("main.contact_detail", contact_id=sub.contact_id))
        return redirect(url_for("main.contacts_list"))
    return render_template("submission_form.html", form=form, mode="edit", submission=sub)


@main_bp.route("/submissions/<int:sub_id>/delete", methods=["POST"])
@login_required
def submission_delete(sub_id):
    if not ConfirmForm().validate_on_submit():
        abort(400)
    sub = db.get_or_404(Submission, sub_id)
    contact_id = sub.contact_id
    db.session.delete(sub)
    commit()
    flash("Submission removed.", "success")
    if contact_id:
        return redirect(url_for("main.contact_detail", contact_id=contact_id))
    return redirect(url_for("main.contacts_list"))


# --------------------------------------------------------------------------
# Attachments
# --------------------------------------------------------------------------
def _delete_attachment_file(att):
    path = os.path.join(current_app.config["UPLOAD_DIR"], att.stored_name)
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        current_app.logger.warning("Could not delete file %s", path)


@main_bp.route("/jobs/<int:job_id>/upload", methods=["POST"])
@login_required
def attachment_upload(job_id):
    job = db.get_or_404(Job, job_id)
    form = AttachmentForm()
    if form.validate_on_submit():
        f = form.file.data
        original = secure_filename(f.filename) or "file"
        ext = original.rsplit(".", 1)[-1].lower() if "." in original else ""
        stored = f"{uuid.uuid4().hex}{('.' + ext) if ext else ''}"
        dest = os.path.join(current_app.config["UPLOAD_DIR"], stored)
        f.save(dest)
        att = Attachment(
            job_id=job.id,
            kind=form.kind.data,
            original_name=original,
            stored_name=stored,
            content_type=f.mimetype or "",
            size=os.path.getsize(dest),
            uploaded_by=current_user.display_name or current_user.username,
        )
        db.session.add(att)
        commit()
        flash("File uploaded.", "success")
    else:
        msg = "Upload failed."
        for errs in form.errors.values():
            msg = errs[0]
            break
        flash(msg, "danger")
    return redirect(url_for("main.job_detail", job_id=job_id))


@main_bp.route("/attachments/<int:att_id>/download")
@login_required
def attachment_download(att_id):
    att = db.get_or_404(Attachment, att_id)
    path = os.path.join(current_app.config["UPLOAD_DIR"], att.stored_name)
    if not os.path.exists(path):
        abort(404)
    return send_file(path, as_attachment=True, download_name=att.original_name)


@main_bp.route("/attachments/<int:att_id>/delete", methods=["POST"])
@login_required
def attachment_delete(att_id):
    form = ConfirmForm()
    if not form.validate_on_submit():
        abort(400)
    att = db.get_or_404(Attachment, att_id)
    job_id = att.job_id
    _delete_attachment_file(att)
    db.session.delete(att)
    commit()
    flash("Attachment removed.", "success")
    return redirect(url_for("main.job_detail", job_id=job_id))


# --------------------------------------------------------------------------
# CSV export
# --------------------------------------------------------------------------
@main_bp.route("/export/csv")
@login_required
def export_csv():
    jobs = Job.query.order_by(Job.date_applied.desc()).all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "Company", "Title", "Location", "Work mode", "Status", "Source", "URL",
        "Salary", "Date applied", "Follow-up date", "Contact name", "Contact email",
        "Interviews", "Notes",
    ])
    for j in jobs:
        w.writerow([
            _csv_safe(j.company), _csv_safe(j.title), _csv_safe(j.location),
            _csv_safe(j.work_mode), _csv_safe(j.status), _csv_safe(j.source),
            _csv_safe(j.url), _csv_safe(j.salary),
            j.date_applied or "", j.follow_up_date or "",
            _csv_safe(j.contact_name), _csv_safe(j.contact_email),
            len(j.interviews), _csv_safe((j.notes or "").replace("\n", " ")),
        ])
    out = buf.getvalue()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return Response(
        out,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=job-squire-{stamp}.csv"},
    )


# --------------------------------------------------------------------------
# Full backup download (DB snapshot + attachments). See app/backup.py for why
# restore is a CLI-only operation (scripts/restore.sh), not a route here.
# --------------------------------------------------------------------------
@main_bp.route("/settings/backup/download")
@login_required
@admin_required
def backup_download():
    include_env = request.args.get("include_env", "1") != "0"
    try:
        filename, data = build_backup_archive(
            current_app.config["DATA_DIR"],
            current_app.config["UPLOAD_DIR"],
            include_env=include_env,
        )
    except FileNotFoundError:
        flash("Nothing to back up yet — no database found.", "danger")
        return redirect(url_for("main.settings", _anchor="tab-backup"))
    except Exception:
        log.exception("Backup archive build failed")
        flash("Backup failed — check the server logs for details.", "danger")
        return redirect(url_for("main.settings", _anchor="tab-backup"))

    return Response(
        data,
        mimetype="application/gzip",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# --------------------------------------------------------------------------
# User guide (renders the bundled Markdown guide as an in-app page)
# --------------------------------------------------------------------------
def _user_guide_path():
    # The guide lives in the docs/ folder, one level up from this app package.
    return os.path.join(os.path.dirname(__file__), "..", "docs", "Job_Squire_User_Guide.md")


def _render_user_guide():
    try:
        with open(_user_guide_path(), "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None
    html = markdown_lib.markdown(
        text,
        extensions=["extra", "sane_lists", "toc", "nl2br"],
        output_format="html5",
    )
    return html


@main_bp.route("/guide")
@login_required
def user_guide():
    html = _render_user_guide()
    return render_template("guide.html", guide_html=html)


@main_bp.route("/wiki/<page>")
@login_required
def user_guide_wiki(page):
    """Serve an individual wiki page from docs/wiki/<page>.md."""
    # Accept requests with or without the .md extension.
    safe = secure_filename(page)
    if not safe:
        abort(404)
    if not safe.endswith(".md"):
        safe += ".md"
    path = os.path.join(os.path.dirname(__file__), "..", "docs", "wiki", safe)
    # Prevent directory traversal — the resolved path must stay inside docs/wiki/.
    wiki_dir = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", "docs", "wiki"))
    if not os.path.realpath(path).startswith(wiki_dir + os.sep):
        abort(404)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        abort(404)
    html = markdown_lib.markdown(
        text,
        extensions=["extra", "sane_lists", "toc", "nl2br"],
        output_format="html5",
    )
    return render_template("guide.html", guide_html=html)


# --------------------------------------------------------------------------
# Application timeline view
# --------------------------------------------------------------------------
@main_bp.route("/timeline")
@login_required
def timeline():
    """Week-by-week application activity bar chart + chronological feed."""
    today = date.today()

    # --- Collect events -------------------------------------------------------
    events: list[dict] = []

    for j in Job.query.all():
        if j.date_applied:
            events.append({
                "date": j.date_applied,
                "type": "applied",
                "label": "Applied",
                "detail": f"{j.title} at {j.company}",
                "url": url_for("main.job_detail", job_id=j.id),
                "status": j.status,
            })
        if j.kit_generated_at:
            events.append({
                "date": j.kit_generated_at.date(),
                "type": "kit",
                "label": "Kit built",
                "detail": f"{j.title} at {j.company}",
                "url": url_for("main.job_detail", job_id=j.id),
                "status": j.status,
            })

    for n in JobNote.query.filter(JobNote.note_type == "status_change").all():
        if n.created_at:
            events.append({
                "date": n.created_at.date(),
                "type": "status",
                "label": "Status change",
                "detail": n.content,
                "url": url_for("main.job_detail", job_id=n.job_id),
                "status": None,
            })

    for iv in Interview.query.all():
        iv_date = iv.interview_date or (iv.created_at.date() if iv.created_at else None)
        if iv_date and iv.job:
            events.append({
                "date": iv_date,
                "type": "interview",
                "label": iv.round_type or "Interview",
                "detail": f"{iv.job.title} at {iv.job.company}",
                "url": url_for("main.job_detail", job_id=iv.job_id),
                "status": iv.job.status,
            })

    # Sort newest first.
    events.sort(key=lambda e: e["date"], reverse=True)

    # --- Group feed by date ---------------------------------------------------
    from itertools import groupby
    feed_groups: list[dict] = []
    for day, day_events in groupby(events, key=lambda e: e["date"]):
        feed_groups.append({"date": day, "events": list(day_events)})

    # --- Weekly application chart (last 12 ISO weeks) -------------------------
    # Build Monday-anchored week buckets.
    week_starts = []
    monday = today - timedelta(days=today.weekday())
    for i in range(11, -1, -1):
        week_starts.append(monday - timedelta(weeks=i))

    applied_events = [e for e in events if e["type"] == "applied"]
    chart_weeks: list[dict] = []
    for ws in week_starts:
        we = ws + timedelta(days=6)
        count = sum(1 for e in applied_events if ws <= e["date"] <= we)
        chart_weeks.append({
            "label": ws.strftime("%-m/%-d"),
            "count": count,
            "start": ws,
            "end": we,
        })

    max_count = max((w["count"] for w in chart_weeks), default=1) or 1

    # Total stats for the header.
    total_applied = len(applied_events)
    total_interviews = sum(1 for e in events if e["type"] == "interview")

    return render_template(
        "timeline.html",
        feed_groups=feed_groups,
        chart_weeks=chart_weeks,
        max_count=max_count,
        total_applied=total_applied,
        total_interviews=total_interviews,
        today=today,
    )


# --------------------------------------------------------------------------
# Claude Pro Setup wizard — guided connector + routine configuration
# --------------------------------------------------------------------------
@main_bp.route("/setup")
@login_required
def setup():
    """Redirect: Claude Pro setup has moved into Settings → Claude tab."""
    return redirect(url_for("main.settings"))


@main_bp.route("/api/mcp-ping")
@login_required
def mcp_ping():
    """Lightweight endpoint for the setup page to verify the MCP URL is configured."""
    public_mcp_url = os.environ.get("PUBLIC_MCP_URL", "")
    return Response(
        json.dumps({"configured": bool(public_mcp_url), "url": public_mcp_url}),
        status=200,
        mimetype="application/json",
    )


# --------------------------------------------------------------------------
# Application kits: profile + job + prompt for User's own Claude
# --------------------------------------------------------------------------
def _profile_path():
    """Return the path to candidate_profile.md in the data dir."""
    return os.path.join(current_app.config["DATA_DIR"], "candidate_profile.md")


def _load_profile():
    try:
        with open(_profile_path(), "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return "(candidate_profile.md not found — add User's master profile.)"


def _save_profile(text):
    with open(_profile_path(), "w", encoding="utf-8") as fh:
        fh.write(text)


_DEFAULT_PROFILE_PROMPT = """\
Read every document returned by get_candidate_assets() carefully (resumes, cover letter \
templates, recommendation letters, certifications, etc.).

Based solely on what you find in those documents, write an updated Candidate Profile in \
the same Markdown format as the current profile shown below. Include: contact info, target \
roles and salary, professional summary, core skills, detailed work history with specific \
metrics, education, certifications, and notable achievements.

Do NOT invent or embellish — only include information explicitly found in the documents.

Present the full updated profile text so Admin can review and copy it into the profile editor.\
"""


def _load_profile_prompt():
    path = os.path.join(current_app.config["DATA_DIR"], "profile_prompt.md")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return _DEFAULT_PROFILE_PROMPT


def _save_profile_prompt(text):
    path = os.path.join(current_app.config["DATA_DIR"], "profile_prompt.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


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


@main_bp.route("/jobs/<int:job_id>/kit")
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


@main_bp.route("/kit", methods=["GET", "POST"])
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
            kit_job_id = job.id
            flash(f'Saved "{job.title}" to Job Squire as a job.', "success")
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


@main_bp.route("/kit/run", methods=["POST"])
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
        return redirect(url_for("main.kit_hub"))

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
    return redirect(url_for("main.ai_task_status", run_id=run_id, task="build_kit"))


@main_bp.route("/kit/download-docx", methods=["POST"])
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


# --------------------------------------------------------------------------
# AI analysis: manual (export/import), API (one-click), or MCP (connector)
# --------------------------------------------------------------------------

@main_bp.route("/export/ai")
@login_required
def export_ai():
    # Manual-mode export goes to whatever AI chat the user pastes it into —
    # redact it like any other transmission (docs/PLAN-ai-privacy.md).
    export = ai.build_export_dict()
    if privacy.redaction_enabled():
        export = privacy.redact_obj(export)
    payload = json.dumps(export, indent=2)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return Response(
        payload,
        mimetype="application/json",
        headers={"Content-Disposition": f"attachment; filename=job-squire-for-claude-{stamp}.json"},
    )


@main_bp.route("/ai", methods=["GET", "POST"])
@login_required
def ai_hub():
    cfg = _singleton(AIConfig)
    form = AIImportForm()
    if form.validate_on_submit():  # manual import is available in any mode
        raw = ""
        if form.file.data:
            raw = form.file.data.read().decode("utf-8", errors="replace")
        elif form.payload.data:
            raw = form.payload.data
        if not raw.strip():
            flash("Paste JSON or choose a file first.", "warning")
            return redirect(url_for("main.ai_hub"))
        # The pasted result may contain placeholders from a redacted export —
        # swap the real values back before storing anything.
        raw, unresolved = privacy.rehydrate(raw)
        if unresolved:
            flash(f"{len(unresolved)} privacy placeholder(s) in the AI response could not "
                  "be matched to stored values and were left as-is — review the imported "
                  "analysis for stray {{PII:…}} tokens.", "warning")
        try:
            parsed = ai.extract_json(raw)
        except ValueError as e:
            flash(f"Could not parse JSON: {e}", "danger")
            return redirect(url_for("main.ai_hub"))
        updated, missing = ai.apply_analysis(
            parsed, created_by=current_user.display_name or current_user.username)
        msg = f"Imported analysis. Updated {updated} job(s)."
        if missing:
            msg += f" {missing} job id(s) did not match and were skipped."
        flash(msg, "success")
        return redirect(url_for("main.ai_hub"))

    insights = AIInsight.query.order_by(AIInsight.created_at.desc()).limit(10).all()
    analyzed_jobs = (
        Job.query.filter(Job.ai_analysis.isnot(None))
        .filter(Job.ai_analysis != "")
        .order_by(Job.ai_analysis_at.desc())
        .all()
    )
    ai_providers = AIProviderConfig.query.filter_by(enabled=True).order_by(AIProviderConfig.rank).all()
    return render_template(
        "ai_hub.html", form=form, cfg=cfg, insights=insights, analyzed_jobs=analyzed_jobs,
        manual_prompt=ai.manual_prompt(), confirm_form=ConfirmForm(),
        api_key_set=bool(cfg.api_key_enc),
        mcp_configured=bool(os.environ.get("PUBLIC_MCP_URL")),
        ai_providers=ai_providers,
        has_ranked_providers=bool(ai_providers),
    )


@main_bp.route("/ai/analyze", methods=["POST"])
@login_required
def ai_analyze():
    if not ConfirmForm().validate_on_submit():
        abort(400)
    cfg = _singleton(AIConfig)
    secret = current_app.config["SECRET_KEY"]
    api_key = decrypt(secret, cfg.api_key_enc) if cfg.api_key_enc else ""
    has_providers = ai._has_ranked_providers()
    if not api_key and not has_providers:
        flash("Add an AI provider or Anthropic API key under Settings first.", "warning")
        return redirect(url_for("main.ai_hub"))
    try:
        parsed, provider = ai.run_api_analysis(api_key, cfg.model, cfg.thinking_mode or "disabled")
    except Exception as e:  # noqa: BLE001 - surface API/parse errors to the user
        flash(f"Analysis failed: {e.__class__.__name__}: {str(e)[:200]}", "danger")
        return redirect(url_for("main.ai_hub"))
    updated, missing = ai.apply_analysis(
        parsed, created_by=current_user.display_name or current_user.username,
        provider=provider)
    flash(f"AI analyzed your pipeline. Updated {updated} job(s).", "success")
    return redirect(url_for("main.ai_hub"))


class _TaskStatus:
    """File-backed status object shared between the background thread and the poll endpoint.

    Written to DATA_DIR/task_{run_id}.json so it's visible across gunicorn workers.
    Writes are atomic (write-tmp + os.replace).
    """
    def __init__(self, run_id: str, task: str, data_dir: str):
        self.run_id = run_id
        self.task = task
        self.path = os.path.join(data_dir, f"task_{run_id}.json")
        self._data = {"run_id": run_id, "task": task, "status": "running",
                      "logs": [], "result": None, "error": None}
        self._flush()

    def log(self, text: str) -> None:
        self._data["logs"].append(text)
        self._flush()

    def done(self, result) -> None:
        self._data["status"] = "done"
        self._data["result"] = result
        self._flush()

    def fail(self, error) -> None:
        self._data["status"] = "error"
        self._data["error"] = str(error)
        self._flush()

    def _flush(self) -> None:
        import tempfile
        dir_ = os.path.dirname(self.path)
        try:
            fd, tmp = tempfile.mkstemp(dir=dir_, suffix=".json")
            with os.fdopen(fd, "w") as f:
                json.dump(self._data, f)
            os.replace(tmp, self.path)
        except Exception:  # noqa: BLE001
            pass


class _StatusLogHandler(logging.Handler):
    """Forwards log records from the 'ai' logger into a _TaskStatus object."""
    def __init__(self, status: _TaskStatus):
        super().__init__()
        self.status = status
        self.setFormatter(logging.Formatter("%(levelname)s %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.status.log(self.format(record))
        except Exception:  # noqa: BLE001
            pass


@main_bp.route("/ai/run/<task>", methods=["POST"])
@login_required
def ai_run_task(task):
    """Manually trigger one of the automatic background tasks (triage, followup,
    weekly_review), or a full rescore of already-scored Saved jobs.

    "rescore" exists because run_auto_triage() only scores jobs with no score yet —
    by design, so a normal triage run doesn't burn API calls re-scoring jobs that
    haven't changed. That means fit scores go stale after the candidate profile is
    edited. This clears scores on Saved jobs first, then runs the same triage.

    Launches the AI call in a daemon thread (so the gunicorn worker is freed immediately),
    then redirects to a live status page that opens in a new browser tab.
    """
    if task not in ("triage", "followup", "weekly_review", "rescore"):
        abort(404)
    if not ConfirmForm().validate_on_submit():
        abort(400)
    cfg = _singleton(AIConfig)
    if not cfg.api_enabled:
        flash("Automatic features are not enabled. Turn them on in Settings → Claude.", "warning")
        return redirect(url_for("main.settings", _anchor="ai-auto-settings-card"))
    if not ai._has_ranked_providers():
        secret = current_app.config["SECRET_KEY"]
        api_key = decrypt(secret, cfg.api_key_enc) if cfg.api_key_enc else ""
        if not api_key:
            flash("Add an AI provider or Anthropic API key under Settings → Claude first.", "warning")
            anchor = "tab-documents" if task == "rescore" else f"feature-{task}"
            return redirect(url_for("main.settings", _anchor=anchor))

    run_id = uuid.uuid4().hex
    data_dir = current_app.config["DATA_DIR"]
    status = _TaskStatus(run_id, task, data_dir)
    _app = current_app._get_current_object()
    # app/ai.py logs via logging.getLogger(__name__), which resolves to "app.ai" —
    # must match that exact name or records never propagate to this handler.
    ai_log = logging.getLogger("app.ai")

    def _run_task():
        handler = _StatusLogHandler(status)
        # Gunicorn's root logger runs at WARNING; force INFO on the ai logger
        # so call_with_fallback provider-selection messages reach the handler.
        _prior_level = ai_log.level
        ai_log.addHandler(handler)
        ai_log.setLevel(logging.INFO)
        with _app.app_context():
            try:
                labels = {"triage": "Auto-Triage", "followup": "Follow-Up Drafts",
                          "weekly_review": "Weekly Review", "rescore": "Rescore All Jobs"}
                status.log(f"INFO Starting {labels.get(task, task)}")
                if task == "triage":
                    result = ai.run_auto_triage()
                elif task == "rescore":
                    reset = (
                        Job.query
                        .filter(Job.status == "Saved")
                        .filter(Job.ai_fit_score.isnot(None))
                        .filter(Job.ai_fit_score != 0)
                        .update({Job.ai_fit_score: None, Job.ai_fit_reason: None},
                                synchronize_session=False)
                    )
                    commit()
                    status.log(f"INFO Cleared {reset} existing score(s) — rescoring against the current candidate profile")
                    result = ai.run_auto_triage()
                elif task == "followup":
                    result = ai.run_followup_drafts()
                else:  # weekly_review
                    result = ai.run_weekly_review()
                status.done(result)
            except Exception as exc:  # noqa: BLE001
                log.exception("manual %s task failed", task)
                status.fail(exc)
            finally:
                ai_log.removeHandler(handler)
                ai_log.setLevel(_prior_level)

    threading.Thread(target=_run_task, daemon=True).start()
    return redirect(url_for("main.ai_task_status", run_id=run_id, task=task))


# run_id is always server-generated via uuid.uuid4().hex (see _TaskStatus /
# the callers that create one) -- 32 lowercase hex characters, nothing else.
# The regex check below is inlined into each route right before its
# filesystem use rather than factored into a shared helper, so CodeQL's
# taint-tracking sees the sanitizing guard in the same function as the sink
# it protects (os.path.join/open/os.unlink) instead of losing the flow
# across a function boundary.
_RUN_ID_RE = re.compile(r"^[0-9a-f]{32}$")


@main_bp.route("/ai/task/<run_id>/status")
@login_required
def ai_task_status(run_id: str):
    """Status page for a running background AI task. Opened in a new tab."""
    if not _RUN_ID_RE.fullmatch(run_id or ""):
        return jsonify({"status": "not_found"}), 404
    task = request.args.get("task", "")
    labels = {
        "triage": "Auto-Triage", "followup": "Follow-Up Drafts", "weekly_review": "Weekly Review",
        "ats_gap": "ATS Gap Analysis", "score_fit": "Score Fit", "draft_followup": "Draft Follow-Up",
    }
    label = labels.get(task, task.replace("_", " ").title())
    return render_template("task_status.html", run_id=run_id, task=task, label=label)


@main_bp.route("/ai/task/<run_id>/poll")
@login_required
def ai_task_poll(run_id: str):
    """JSON endpoint polled by the status page every 2 s."""
    if not _RUN_ID_RE.fullmatch(run_id or ""):
        return jsonify({"status": "not_found"}), 404
    data_dir = current_app.config["DATA_DIR"]
    # secure_filename() is this codebase's existing sanitizer for
    # user-influenced filenames elsewhere (see the upload/kit routes above);
    # used here too so the same recognized library call guards this sink,
    # on top of the regex check above which already makes run_id safe.
    path = os.path.join(data_dir, secure_filename(f"task_{run_id}.json"))
    if not os.path.exists(path):
        return jsonify({"status": "not_found"}), 404
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return jsonify({"status": "not_found"}), 404
    # Clean up completed task files after delivering the final state.
    if data.get("status") in ("done", "error"):
        try:
            os.unlink(path)
        except OSError:
            pass
    return jsonify(data)


@main_bp.route("/settings/ai-mode", methods=["POST"])
@login_required
@admin_required
def settings_ai_mode():
    """Save Automatic Features toggle (api_enabled only)."""
    cfg = _singleton(AIConfig)
    cfg.api_enabled = "api_enabled" in request.form
    log.info(
        "settings_ai_mode: saving api_enabled=%s form_keys=%s",
        cfg.api_enabled, list(request.form.keys()),
    )
    # Keep legacy mode field in sync.
    if cfg.api_enabled:
        cfg.mode = "api"
    elif cfg.mcp_enabled:
        cfg.mode = "mcp"
    else:
        cfg.mode = "manual"
    commit()
    flash("AI features saved.", "success")
    return redirect(url_for("main.settings"))


@main_bp.route("/settings/claude-pro", methods=["POST"])
@login_required
@admin_required
def settings_claude_pro():
    """Save MCP Connector and Claude Pro toggles (mcp_enabled, claude_buttons_enabled)."""
    cfg = _singleton(AIConfig)
    cfg.mcp_enabled = "mcp_enabled" in request.form
    cfg.claude_buttons_enabled = "claude_buttons_enabled" in request.form
    log.info(
        "settings_claude_pro: saving mcp_enabled=%s claude_buttons_enabled=%s form_keys=%s",
        cfg.mcp_enabled, cfg.claude_buttons_enabled, list(request.form.keys()),
    )
    # Keep legacy mode field in sync.
    if cfg.api_enabled and cfg.mcp_enabled:
        cfg.mode = "api"
    elif cfg.api_enabled:
        cfg.mode = "api"
    elif cfg.mcp_enabled:
        cfg.mode = "mcp"
    else:
        cfg.mode = "manual"
    commit()
    db.session.expire(cfg)
    log.info(
        "settings_claude_pro: post-commit DB read → mcp_enabled=%s claude_buttons_enabled=%s",
        cfg.mcp_enabled, cfg.claude_buttons_enabled,
    )
    flash("Connector settings saved.", "success")
    return redirect(url_for("main.settings") + "#tab-claude")


@main_bp.route("/settings/ai", methods=["POST"])
@login_required
@admin_required
def settings_ai():
    cfg = _singleton(AIConfig)
    # Connector name (used in MCP prompts and buttons).
    connector_name = request.form.get("connector_name", "").strip()
    if connector_name:
        cfg.connector_name = connector_name
    commit()
    flash("Connector settings saved.", "success")
    return redirect(url_for("main.settings") + "#tab-claude")


# --------------------------------------------------------------------------
# MCP API key management — see app/mcp_auth.py for the token spec (shape,
# storage, comparison, and the loopback-only reachability rule).
# --------------------------------------------------------------------------
@main_bp.route("/settings/mcp-api-key", methods=["POST"])
@login_required
@admin_required
def settings_mcp_api_key():
    from datetime import datetime, timezone
    secret = current_app.config["SECRET_KEY"]
    cfg = _singleton(AIConfig)
    action = request.form.get("action", "generate")

    if action == "revoke":
        cfg.mcp_api_key_enc = ""
        cfg.mcp_api_key_created_at = None
        cfg.mcp_api_key_last_used_at = None
        cfg.mcp_api_key_expires_at = None
        commit()
        flash("MCP API key revoked.", "success")

    elif action == "set_network_override":
        # Explicit, independent opt-in required to let the static token be
        # used at all on a network-reachable instance -- generating or
        # rotating a key never turns this on implicitly.
        cfg.mcp_api_key_allow_network = bool(request.form.get("allow_network"))
        commit()
        flash(
            "Static key allowed on this network-reachable instance."
            if cfg.mcp_api_key_allow_network else
            "Static key restricted to loopback-only use again.",
            "success",
        )

    else:
        key = generate_token()
        now = datetime.now(timezone.utc)
        cfg.mcp_api_key_enc = encrypt(secret, key)
        cfg.mcp_api_key_created_at = now
        cfg.mcp_api_key_last_used_at = None
        cfg.mcp_api_key_expires_at = expires_at_from_ttl_hours(
            request.form.get("ttl_hours"), now=now)
        commit()
        flash(f"New MCP API key generated: {key}", "success")

    return redirect(url_for("main.settings") + "#tab-claude")


# --------------------------------------------------------------------------
# OAuth token management (read/revoke — shared with mcp_server process via
# the DATA_DIR/oauth_tokens.json file)
# --------------------------------------------------------------------------

def _oauth_token_path() -> str:
    return os.path.join(current_app.config.get("DATA_DIR", "/data"), "oauth_tokens.json")


def _read_oauth_tokens() -> list:
    """Return live OAuth tokens as a list of display-safe dicts.

    Each entry has:
      token_id   — SHA-256 of the raw token (safe to expose in HTML)
      client_name — human-readable label captured at DCR
      issued_at  — Unix timestamp (float) or None for legacy tokens
      exp        — Unix timestamp (float)
    Expired tokens are omitted. Raw token values are never returned.
    """
    import hashlib as _hl
    path = _oauth_token_path()
    data = load_encrypted_json(path, current_app.config["SECRET_KEY"], default={})
    now = time.time()
    result = []
    for raw_token, meta in data.items():
        if meta.get("exp", 0) <= now:
            continue
        result.append({
            "token_id": _hl.sha256(raw_token.encode()).hexdigest(),
            "client_name": meta.get("client_name") or "Unknown client",
            "issued_at": meta.get("issued_at"),
            "exp": meta.get("exp"),
        })
    result.sort(key=lambda x: x.get("issued_at") or 0, reverse=True)
    return result


def _revoke_oauth_token_by_id(token_id: str) -> bool:
    """Remove the token whose SHA-256 matches token_id. Returns True if found."""
    import hashlib as _hl
    path = _oauth_token_path()
    secret = current_app.config["SECRET_KEY"]
    data = load_encrypted_json(path, secret, default={})
    match = next((k for k in data if _hl.sha256(k.encode()).hexdigest() == token_id), None)
    if match:
        del data[match]
        dump_encrypted_json(path, secret, data)
        return True
    return False


def _revoke_all_oauth_tokens() -> int:
    """Remove all tokens. Returns count removed."""
    path = _oauth_token_path()
    secret = current_app.config["SECRET_KEY"]
    data = load_encrypted_json(path, secret, default={})
    count = len(data)
    dump_encrypted_json(path, secret, {})
    return count


@main_bp.route("/settings/mcp-revoke-token", methods=["POST"])
@login_required
@admin_required
def settings_mcp_revoke_token():
    token_id = request.form.get("token_id", "").strip()
    if token_id and _revoke_oauth_token_by_id(token_id):
        flash("Token revoked.", "success")
    else:
        flash("Token not found or already expired.", "warning")
    return redirect(url_for("main.settings") + "#tab-claude")


@main_bp.route("/settings/mcp-revoke-all", methods=["POST"])
@login_required
@admin_required
def settings_mcp_revoke_all():
    count = _revoke_all_oauth_tokens()
    flash(f"Revoked {count} token{'s' if count != 1 else ''}.", "success")
    return redirect(url_for("main.settings") + "#tab-claude")


# --------------------------------------------------------------------------
# Per-task AI configuration
# --------------------------------------------------------------------------
@main_bp.route("/settings/ai/tasks", methods=["POST"])
@login_required
@admin_required
def settings_ai_tasks():
    from .models import AITaskConfig, AI_TASK_NAMES
    for task_name in AI_TASK_NAMES:
        tc = AITaskConfig.query.filter_by(task_name=task_name).first()
        if tc is None:
            tc = AITaskConfig(task_name=task_name)
            db.session.add(tc)
        tc.enabled = f"{task_name}_enabled" in request.form
        primary_id = request.form.get(f"{task_name}_provider_id", "").strip()
        backup_id = request.form.get(f"{task_name}_backup_provider_id", "").strip()
        tc.provider_id = int(primary_id) if primary_id.isdigit() else None
        tc.backup_provider_id = int(backup_id) if backup_id.isdigit() else None
        tc.use_ranked_chain_fallback = f"{task_name}_chain_fallback" in request.form
    # Rejection alert threshold is submitted alongside task settings.
    cfg = _singleton(AIConfig)
    try:
        cfg.rejection_alert_threshold = max(1, int(request.form.get("rejection_alert_threshold") or 5))
    except (ValueError, TypeError):
        cfg.rejection_alert_threshold = 5
    commit()
    flash("Task settings saved.", "success")
    return redirect(url_for("main.settings"))


# --------------------------------------------------------------------------
# AI Provider management (ranked fallback providers)
# --------------------------------------------------------------------------
_VALID_PROVIDER_TYPES = {
    "anthropic", "gemini", "groq", "openrouter", "ollama", "mistral", "openai",
    "cerebras", "github_models", "nous_portal", "litellm", "custom",
}


@main_bp.route("/settings/ai/providers/add", methods=["POST"])
@login_required
@admin_required
def ai_provider_add():
    secret = current_app.config["SECRET_KEY"]
    provider = request.form.get("provider", "").strip().lower()
    if provider not in _VALID_PROVIDER_TYPES:
        flash("Unknown provider type.", "danger")
        return redirect(url_for("main.settings") + "#tab-claude")
    label = request.form.get("label", "").strip()
    api_key = request.form.get("api_key", "").strip()
    base_url = request.form.get("base_url", "").strip()
    model = request.form.get("model", "").strip()
    triage_model = request.form.get("triage_model", "").strip()
    num_ctx_raw = request.form.get("num_ctx", "").strip()
    num_ctx = int(num_ctx_raw) if num_ctx_raw.isdigit() else None
    thinking_mode_raw = request.form.get("thinking_mode", "disabled")
    thinking_mode = thinking_mode_raw if thinking_mode_raw in ("disabled", "low", "medium", "high") else "disabled"
    # Assign the next rank
    max_rank = db.session.query(db.func.max(AIProviderConfig.rank)).scalar() or 0
    use_for_triage = bool(request.form.get("use_for_triage", True))
    use_for_analysis = bool(request.form.get("use_for_analysis", True))
    p = AIProviderConfig(
        rank=max_rank + 1,
        provider=provider,
        label=label,
        api_key_enc=encrypt(secret, api_key) if api_key else "",
        base_url=base_url,
        model=model,
        triage_model=triage_model,
        num_ctx=num_ctx,
        thinking_mode=thinking_mode if provider == "anthropic" else None,
        enabled=True,
        use_for_triage=use_for_triage,
        use_for_analysis=use_for_analysis,
    )
    db.session.add(p)
    commit()
    flash(f"Added {p.display_name} (rank {p.rank}).", "success")
    return redirect(url_for("main.settings") + "#tab-claude")


@main_bp.route("/settings/ai/providers/<int:pid>/edit", methods=["POST"])
@login_required
@admin_required
def ai_provider_edit(pid):
    secret = current_app.config["SECRET_KEY"]
    p = db.session.get(AIProviderConfig, pid)
    if not p:
        abort(404)
    p.label = request.form.get("label", "").strip()
    api_key = request.form.get("api_key", "").strip()
    if api_key:
        p.api_key_enc = encrypt(secret, api_key)
    base_url = request.form.get("base_url", "").strip()
    if base_url:
        p.base_url = base_url
    p.model = request.form.get("model", "").strip()
    p.triage_model = request.form.get("triage_model", "").strip()
    num_ctx_raw = request.form.get("num_ctx", "").strip()
    p.num_ctx = int(num_ctx_raw) if num_ctx_raw.isdigit() else None
    # thinking_mode only applies to Anthropic providers
    if p.provider == "anthropic":
        thinking_mode_raw = request.form.get("thinking_mode", "disabled")
        p.thinking_mode = thinking_mode_raw if thinking_mode_raw in ("disabled", "low", "medium", "high") else "disabled"
    # Capability flags — use_for_triage is checked via checkbox presence
    p.use_for_triage = "use_for_triage" in request.form
    p.use_for_analysis = "use_for_analysis" in request.form
    commit()
    flash(f"Updated {p.display_name}.", "success")
    return redirect(url_for("main.settings") + "#tab-claude")


@main_bp.route("/settings/ai/providers/<int:pid>/delete", methods=["POST"])
@login_required
@admin_required
def ai_provider_delete(pid):
    from .models import AITaskConfig
    p = db.session.get(AIProviderConfig, pid)
    if not p:
        abort(404)
    name = p.display_name
    # SQLite doesn't enforce ondelete="SET NULL" — null out FKs manually.
    for tc in AITaskConfig.query.filter(
        (AITaskConfig.provider_id == pid) | (AITaskConfig.backup_provider_id == pid)
    ).all():
        if tc.provider_id == pid:
            tc.provider_id = None
        if tc.backup_provider_id == pid:
            tc.backup_provider_id = None
    db.session.delete(p)
    commit()
    # Re-sequence ranks so there are no gaps
    for i, row in enumerate(
        AIProviderConfig.query.order_by(AIProviderConfig.rank).all(), start=1
    ):
        row.rank = i
    commit()
    flash(f"Removed {name}.", "success")
    return redirect(url_for("main.settings") + "#tab-claude")


@main_bp.route("/settings/ai/providers/<int:pid>/toggle", methods=["POST"])
@login_required
@admin_required
def ai_provider_toggle(pid):
    p = db.session.get(AIProviderConfig, pid)
    if not p:
        abort(404)
    p.enabled = not p.enabled
    commit()
    state = "enabled" if p.enabled else "disabled"
    flash(f"{p.display_name} {state}.", "success")
    return redirect(url_for("main.settings") + "#tab-claude")


@main_bp.route("/settings/ai/providers/<int:pid>/move-up", methods=["POST"])
@login_required
@admin_required
def ai_provider_move_up(pid):
    p = db.session.get(AIProviderConfig, pid)
    if not p:
        abort(404)
    if p.rank > 1:
        prev = AIProviderConfig.query.filter(
            AIProviderConfig.rank == p.rank - 1
        ).first()
        if prev:
            prev.rank, p.rank = p.rank, prev.rank
            commit()
    return redirect(url_for("main.settings") + "#tab-claude")


@main_bp.route("/settings/ai/providers/<int:pid>/move-down", methods=["POST"])
@login_required
@admin_required
def ai_provider_move_down(pid):
    p = db.session.get(AIProviderConfig, pid)
    if not p:
        abort(404)
    nxt = AIProviderConfig.query.filter(
        AIProviderConfig.rank == p.rank + 1
    ).first()
    if nxt:
        nxt.rank, p.rank = p.rank, nxt.rank
        commit()
    return redirect(url_for("main.settings") + "#tab-claude")


@main_bp.route("/settings/ai/providers/<int:pid>/test", methods=["POST"])
@login_required
@admin_required
def ai_provider_test(pid):
    import time
    p = db.session.get(AIProviderConfig, pid)
    if not p:
        abort(404)
    secret = current_app.config["SECRET_KEY"]
    # This route is submitted as the Edit form's "Test connection" button (via
    # formaction), so it sees whatever is currently typed — including unsaved
    # changes. Fields left blank fall back to the last-saved value, matching
    # the "leave blank to keep current" convention used when actually saving.
    form_api_key = (request.form.get("api_key") or "").strip()
    api_key = form_api_key if form_api_key else (decrypt(secret, p.api_key_enc) if p.api_key_enc else "")
    form_base_url = (request.form.get("base_url") or "").strip()
    base_url = form_base_url or (p.base_url.strip() if p.base_url else "") or ai._PROVIDER_URLS.get(p.provider, "")
    if not base_url:
        flash(f"{p.display_name}: no base URL — enter one in the Edit form.", "warning")
        return redirect(url_for("main.settings") + "#tab-claude")
    model = (request.form.get("model") or "").strip() or (p.model or "").strip()
    if not model:
        # Supply a known-cheap default per provider so the test call doesn't fail on a missing model
        _test_defaults = {
            "gemini": "gemini-2.0-flash-lite",
            "groq": "llama-3.1-8b-instant",
            "openrouter": "openrouter/free",
            "mistral": "mistral-small-latest",
            "openai": "gpt-4o-mini",
            "ollama": "llama3.2",
            "cerebras": "llama-3.3-70b",
            "github_models": "gpt-4o-mini",
            "nous_portal": "Hermes-3-Llama-3.1-70B",
            "litellm": "gpt-4o-mini",
        }
        model = _test_defaults.get(p.provider, "")
    if not model:
        flash(f"{p.display_name}: enter a model name before testing.", "warning")
        return redirect(url_for("main.settings") + "#tab-claude")
    try:
        t0 = time.monotonic()
        reply = ai.call_openai_compat(
            base_url=base_url,
            api_key=api_key,
            model=model,
            system="You are a test assistant.",
            user_content="Reply with only the word OK.",
            max_tokens=16,
            provider=p.provider,
        )
        elapsed = round((time.monotonic() - t0) * 1000)
        flash(f"{p.display_name} ({model}): OK — {elapsed} ms. Reply: {reply[:80]!r}", "success")
    except Exception as e:  # noqa: BLE001
        flash(f"{p.display_name} test failed: {e.__class__.__name__}: {str(e)[:200]}", "danger")
    return redirect(url_for("main.settings") + "#tab-claude")


@main_bp.route("/settings/ai/privacy", methods=["POST"])
@login_required
@admin_required
def settings_ai_privacy():
    """Save the AI privacy (redaction) toggles — see docs/PLAN-ai-privacy.md."""
    cfg = _singleton(AIConfig)
    cfg.redaction_enabled = bool(request.form.get("redaction_enabled"))
    cfg.redact_strict = bool(request.form.get("redact_strict"))
    cfg.redact_local = bool(request.form.get("redact_local"))
    commit()
    if not cfg.redaction_enabled:
        flash("Privacy redaction disabled — personal identifiers will be sent "
              "to AI providers as-is.", "warning")
    else:
        bits = ["identifier redaction on"]
        if cfg.redact_strict:
            bits.append("strict mode (employers/locations pseudonymized)")
        if cfg.redact_local:
            bits.append("applied to local providers too")
        flash("Privacy settings saved: " + ", ".join(bits) + ".", "success")
    return redirect(url_for("main.settings") + "#tab-claude")


@main_bp.route("/settings/ai/providers/fallback", methods=["POST"])
@login_required
@admin_required
def ai_provider_fallback_toggle():
    cfg = _singleton(AIConfig)
    cfg.fallback_to_anthropic = bool(request.form.get("fallback_to_anthropic"))
    commit()
    state = "enabled" if cfg.fallback_to_anthropic else "disabled"
    flash(f"Anthropic fallback {state}.", "success")
    return redirect(url_for("main.settings") + "#tab-claude")


# --------------------------------------------------------------------------
# Machine ingest API (Model A): token-authenticated push of found jobs
# --------------------------------------------------------------------------
@main_bp.route("/api/ingest", methods=["POST"])
@csrf.exempt
def api_ingest():
    expected = os.environ.get("INGEST_API_KEY", "")
    provided = request.headers.get("X-API-Key", "")
    if not expected or not hmac.compare_digest(provided, expected):
        return {"error": "unauthorized"}, 401
    data = request.get_json(silent=True) or {}
    items = data.get("jobs")
    if not isinstance(items, list):
        return {"error": "expected JSON body with a 'jobs' array"}, 400
    created, skipped = ingest_jobs(items, created_by=(data.get("created_by") or "api"))
    return {"created": len(created), "skipped": skipped, "ids": [j.id for j in created]}


# --------------------------------------------------------------------------
# Connections / Settings (Model B): in-app search configuration
# --------------------------------------------------------------------------
def _singleton(model):
    row = db.session.get(model, 1)
    if not row:
        row = model(id=1)
        db.session.add(row)
        commit()
    return row


@main_bp.route("/settings")
@login_required
@admin_required
def settings():
    cfg = _singleton(SearchConfig)
    smtp = _singleton(SmtpConfig)
    secret = current_app.config["SECRET_KEY"]

    providers = []
    for key, meta in PROVIDERS.items():
        pc = ProviderCredential.query.filter_by(provider=key).first()
        creds = {}
        if pc and pc.secret_blob:
            try:
                creds = json.loads(decrypt(secret, pc.secret_blob)) or {}
            except json.JSONDecodeError:
                creds = {}
        fields = []
        for f in meta["fields"]:
            val = creds.get(f["name"], "")
            fields.append({
                **f,
                "value": "" if f["secret"] else val,
                "is_set": bool(val),
            })
        providers.append({
            "key": key, "label": meta["label"], "note": meta["note"],
            "signup_url": meta["signup_url"], "enabled": bool(pc and pc.enabled),
            "fields": fields,
        })

    runs = SearchRun.query.order_by(SearchRun.started_at.desc()).limit(10).all()

    ai_cfg = _singleton(AIConfig)
    # Diagnostic: raw SQL read to confirm DB value matches ORM value.
    _raw = db.session.execute(
        text("SELECT claude_buttons_enabled FROM ai_config WHERE id=1")
    ).fetchone()
    log.info(
        "settings(): ORM claude_buttons_enabled=%s  raw_DB=%s",
        ai_cfg.claude_buttons_enabled,
        _raw[0] if _raw else "NO ROW",
    )
    kit_cfg = _singleton(KitConfig)
    mcp_base_url = os.environ.get("PUBLIC_MCP_URL", "").rstrip("/")

    assets = CandidateAsset.query.order_by(CandidateAsset.kind.asc(),
                                            CandidateAsset.uploaded_at.desc()).all()
    asset_form = CandidateAssetForm()

    cname = ai_cfg.connector_name or "job-squire"
    profile_text = _load_profile()
    profile_prompt = _load_profile_prompt()

    # The embedded profile excerpt travels to claude.ai as chat text — redact it
    # like any other AI-bound content (the MCP tools rehydrate on write-back).
    _profile_for_prompt = (privacy.redact(profile_text).text
                          if privacy.redaction_enabled() else profile_text)
    regen_profile_prompt = (
        f'Using my "{cname}" connector, call get_candidate_assets() to retrieve all '
        f'uploaded candidate documents.\n\n'
        + profile_prompt
        + '\n\nOnce you have written the updated profile, call save_candidate_profile() '
        'with the full profile markdown so it saves directly to Job Squire — '
        'do not ask me to copy and paste it. Confirm once saved.\n\n'
        'Current profile (for format reference — do not simply copy this):\n---\n'
        + _profile_for_prompt
    )

    evaluate_docs_prompt = (
        f'Using my "{cname}" connector, call get_candidate_assets() to retrieve all '
        f'uploaded candidate documents.\n\n'
        'For each document returned, provide:\n'
        '- Document type and label\n'
        '- A brief summary of its contents\n'
        '- Key strengths demonstrated (specific skills, accomplishments, quantified metrics)\n'
        '- Any gaps, weaknesses, or areas for improvement\n'
        '- How well it supports the candidate\'s target roles (as stated in the profile)\n\n'
        'After reviewing all documents, provide an overall assessment:\n'
        '- Which documents are strongest and why\n'
        '- What critical items are missing (e.g., certifications, specific metrics, LinkedIn alignment)\n'
        '- Specific, actionable recommendations to strengthen the overall application package'
    )

    # Build routine prompts for the Claude tab.
    from .prompts import (
        ROUTINE_DESCRIPTIONS,
        morning_briefing_prompt,
        new_job_triage_prompt,
        kit_queue_prompt,
        followup_drafts_prompt,
        weekly_review_prompt,
    )
    from .models import User as _SettingsUser
    cuser = _SettingsUser.query.filter_by(role="user").first()
    candidate_name = (cuser.display_name or cuser.username) if cuser else "the candidate"
    if candidate_name != "the candidate" and privacy.redaction_enabled():
        candidate_name = privacy.redact(candidate_name).text
    _routine_prompts = [
        morning_briefing_prompt(cname),
        new_job_triage_prompt(cname, candidate_name),
        kit_queue_prompt(cname),
        followup_drafts_prompt(cname),
        weekly_review_prompt(cname),
    ]
    routines = [{**desc, "prompt": _routine_prompts[i]}
                for i, desc in enumerate(ROUTINE_DESCRIPTIONS)]

    from flask import request as _req
    _origin = _req.host_url.rstrip("/")
    bookmarklet_js = _bookmarklet_js(_origin)

    # All providers for the settings table (disabled ones shown grayed-out);
    # the template filters out disabled ones inside dropdowns.
    ai_providers = AIProviderConfig.query.order_by(AIProviderConfig.rank).all()

    from .models import AITaskConfig, AI_TASK_NAMES
    ai_task_configs = {
        tc.task_name: tc
        for tc in AITaskConfig.query.all()
    }

    oauth_tokens = _read_oauth_tokens()
    worker_health = _worker_heartbeat_status()

    return render_template(
        "settings.html", cfg=cfg, smtp=smtp, providers=providers, runs=runs,
        worker_stale=worker_health["stale"], worker_last_seen=worker_health["last_seen"],
        ingest_enabled=bool(os.environ.get("INGEST_API_KEY")),
        ai_cfg=ai_cfg, ai_key_set=bool(ai_cfg.api_key_enc),
        kit_cfg=kit_cfg,
        mcp_base_url=mcp_base_url,
        mcp_configured=bool(mcp_base_url),
        public_mcp_url=mcp_base_url,
        mcp_api_key_set=bool(ai_cfg.mcp_api_key_enc),
        mcp_api_key_created_at=ai_cfg.mcp_api_key_created_at,
        mcp_api_key_last_used_at=ai_cfg.mcp_api_key_last_used_at,
        mcp_api_key_expires_at=ai_cfg.mcp_api_key_expires_at,
        mcp_api_key_allow_network=bool(ai_cfg.mcp_api_key_allow_network),
        mcp_network_reachable=is_network_reachable(current_app.config.get("DEPLOY_MODE")),
        connector=cname,
        routines=routines,
        bookmarklet_js=bookmarklet_js,
        assets=assets, asset_form=asset_form, asset_kinds=ASSET_KINDS,
        confirm_form=ConfirmForm(),
        candidate_profile=profile_text,
        profile_prompt=profile_prompt,
        regen_profile_prompt=regen_profile_prompt,
        evaluate_docs_prompt=evaluate_docs_prompt,
        ai_providers=ai_providers,
        ai_task_configs=ai_task_configs,
        ai_task_names=AI_TASK_NAMES,
        oauth_tokens=oauth_tokens,
    )


def _safe_next(default_url: str) -> str:
    """Honor a relative `next` form field so onboarding pages can reuse settings
    POST routes and return to the walkthrough. Relative paths only — anything
    absolute (scheme or protocol-relative) is ignored to avoid open redirects."""
    nxt = (request.form.get("next") or "").strip()
    if nxt.startswith("/") and not nxt.startswith("//"):
        return nxt
    return default_url


@main_bp.route("/settings/search", methods=["POST"])
@login_required
@admin_required
def settings_search():
    cfg = _singleton(SearchConfig)
    back = _safe_next(url_for("main.settings"))
    location = request.form.get("location", "").strip()
    country = (request.form.get("country") or "US").strip().upper()
    if len(country) != 2 or not country.isalpha():
        flash("Country must be a 2-letter code (ISO 3166-1 alpha-2), "
              "e.g. \"US\", \"GB\", \"DE\".", "danger")
        return redirect(back)
    if country == "US":
        # Providers expect "City, ST" (a valid US state code). Reject anything else
        # so a ZIP or address doesn't silently return empty results, and so the
        # scheduler can derive the right timezone from it. This strictness is
        # US-only: outside the US, timezones.py has no state table to key off of
        # anyway (see SCHEDULE_TZ), so it's just a plain non-empty location.
        if not parse_state(location):
            from .sample_locations import random_sample_city
            flash("Location must be \"City, ST\" with a valid US state code, "
                  f"e.g. \"{random_sample_city()}\". ZIP codes and street addresses are not "
                  "supported by the job sources; use the radius to widen the area.",
                  "danger")
            return redirect(back)
    elif not location:
        flash("Location is required, e.g. \"Manchester\" or \"Manchester, UK\".", "danger")
        return redirect(back)
    cfg.titles = request.form.get("titles", "").strip()
    cfg.location = location
    cfg.country = country
    cfg.radius_miles = _int(request.form.get("radius_miles"), 40)
    cfg.min_salary = _int(request.form.get("min_salary"), None, allow_none=True)
    cfg.max_age_days = _int(request.form.get("max_age_days"), 14)
    cfg.results_per_query = max(1, min(50, _int(request.form.get("results_per_query"), 25)))
    cfg.enabled = request.form.get("enabled") == "on"
    cfg.include_remote = request.form.get("include_remote") == "on"
    commit()
    flash("Search settings saved.", "success")
    return redirect(back)


@main_bp.route("/settings/kit", methods=["POST"])
@login_required
@admin_required
def settings_kit():
    cfg = _singleton(KitConfig)
    cfg.fit_salary_floor = _int(request.form.get("fit_salary_floor"), 60000)
    commit()
    flash("Application Kit settings saved.", "success")
    return redirect(url_for("main.settings"))


@main_bp.route("/settings/providers/save-keyless", methods=["POST"])
@login_required
@admin_required
def settings_providers_keyless_save():
    """Batch-save the no-key-required job boards shown on the Getting Started
    'providers' step. These used to be separate auto-submitting checkboxes
    (one per board); checking several in a row could race against each
    other's page reload and silently drop a change. One form + one Save
    button submits all of them together instead."""
    checked = set(request.form.getlist("provider"))
    changed = []
    for name, meta in PROVIDERS.items():
        if any(f.get("required") for f in meta["fields"]):
            continue  # needs a key — managed individually on Settings | Sources
        pc = ProviderCredential.query.filter_by(provider=name).first()
        if not pc:
            pc = ProviderCredential(provider=name)
            db.session.add(pc)
        wants = name in checked
        if pc.enabled != wants:
            changed.append(meta["label"])
        pc.enabled = wants
    commit()
    flash(f"Job boards updated: {', '.join(changed)}." if changed else "No changes to job boards.", "success")
    return redirect(_safe_next(url_for("main.settings")))


@main_bp.route("/settings/provider/<provider>", methods=["POST"])
@login_required
@admin_required
def settings_provider(provider):
    if provider not in PROVIDERS:
        abort(404)
    secret = current_app.config["SECRET_KEY"]
    pc = ProviderCredential.query.filter_by(provider=provider).first()
    if not pc:
        pc = ProviderCredential(provider=provider)
        db.session.add(pc)
    existing = {}
    if pc.secret_blob:
        try:
            existing = json.loads(decrypt(secret, pc.secret_blob)) or {}
        except json.JSONDecodeError:
            existing = {}
    creds = dict(existing)
    for f in PROVIDERS[provider]["fields"]:
        submitted = request.form.get(f["name"], "")
        if f["secret"]:
            # Keep the stored secret if the field was left blank.
            if submitted.strip():
                creds[f["name"]] = submitted.strip()
        else:
            creds[f["name"]] = submitted.strip()
    pc.secret_blob = encrypt(secret, json.dumps(creds))
    wants_enabled = request.form.get("enabled") == "on"
    if wants_enabled:
        # Block enabling if any required field is still missing after this save.
        missing_label = next(
            (f["label"] for f in PROVIDERS[provider]["fields"]
             if f.get("required") and not creds.get(f["name"], "").strip()),
            None,
        )
        if missing_label:
            pc.enabled = False
            commit()
            flash(
                f"{PROVIDERS[provider]['label']} saved but not enabled — "
                f"{missing_label} is required.",
                "warning",
            )
            return redirect(_safe_next(url_for("main.settings")))
    pc.enabled = wants_enabled
    commit()
    flash(f"{PROVIDERS[provider]['label']} settings saved.", "success")
    return redirect(_safe_next(url_for("main.settings")))


@main_bp.route("/settings/provider/<provider>/test", methods=["POST"])
@login_required
@admin_required
def settings_provider_test(provider):
    """Ping one provider with the saved key and report the live result."""
    if provider not in PROVIDERS:
        abort(404)
    label = PROVIDERS[provider]["label"]
    secret = current_app.config["SECRET_KEY"]
    pc = ProviderCredential.query.filter_by(provider=provider).first()
    creds = {}
    if pc and pc.secret_blob:
        try:
            creds = json.loads(decrypt(secret, pc.secret_blob)) or {}
        except json.JSONDecodeError:
            creds = {}

    cfg_row = db.session.get(SearchConfig, 1)
    titles = (cfg_row.title_list if cfg_row else None) or []
    cfg = {
        "location": (cfg_row.location if cfg_row else None) or "",
        "radius_miles": (cfg_row.radius_miles if cfg_row else None) or 40,
        "min_salary": cfg_row.min_salary if cfg_row else None,
        "max_age_days": (cfg_row.max_age_days if cfg_row else None) or 14,
        "results_per_query": 5,  # keep the probe light
    }
    # One title is enough to verify auth without burning rate limit.
    results, err = search_provider(provider, creds, titles[:1], cfg)
    if err:
        flash(f"{label} test failed: {err}", "danger")
    else:
        flash(f"{label} OK — connected and returned {len(results)} result(s) for "
              f"\"{titles[0]}\".", "success")
    return redirect(url_for("main.settings"))


@main_bp.route("/settings/provider/<provider>/pull", methods=["POST"])
@login_required
@admin_required
def settings_provider_pull(provider):
    """Run a full search for one provider, ingest results, and clear any cooldown."""
    if provider not in PROVIDERS:
        abort(404)
    label = PROVIDERS[provider]["label"]
    secret = current_app.config["SECRET_KEY"]
    pc = ProviderCredential.query.filter_by(provider=provider).first()
    creds = {}
    if pc and pc.secret_blob:
        try:
            creds = json.loads(decrypt(secret, pc.secret_blob)) or {}
        except json.JSONDecodeError:
            creds = {}

    cfg_row = db.session.get(SearchConfig, 1)
    titles = (cfg_row.title_list if cfg_row else None) or []
    cfg = {
        "location": (cfg_row.location if cfg_row else None) or "",
        "radius_miles": (cfg_row.radius_miles if cfg_row else None) or 40,
        "min_salary": cfg_row.min_salary if cfg_row else None,
        "max_age_days": (cfg_row.max_age_days if cfg_row else None) or 14,
        "results_per_query": (cfg_row.results_per_query if cfg_row else None) or 25,
    }
    # Tracked the same way a full search is, so it shows up in Settings | History
    # instead of silently vanishing after the flash message disappears.
    run = SearchRun(trigger="manual", status="running", providers=provider)
    db.session.add(run)
    commit()

    results, err = search_provider(provider, creds, titles, cfg)
    if err:
        run.finished_at = datetime.now(timezone.utc)
        run.status = "error"
        run.detail = err[:1000]
        commit()
        flash(f"{label} pull failed: {err}", "danger")
        return redirect(url_for("main.settings"))

    from .search import _load_cooldowns, _save_cooldowns
    cooldowns = _load_cooldowns()
    if provider in cooldowns:
        del cooldowns[provider]
        _save_cooldowns(cooldowns)

    created, skipped = ingest_jobs(results, created_by=f"pull:{provider}")
    run.finished_at = datetime.now(timezone.utc)
    run.found = len(results)
    run.created = len(created)
    run.skipped = skipped
    run.status = "ok"
    commit()
    flash(
        f"{label}: fetched {len(results)}, {len(created)} new"
        + (f", {skipped} already in Job Squire" if skipped else "") + ".",
        "success",
    )
    return redirect(url_for("main.settings"))


@main_bp.route("/settings/smtp", methods=["POST"])
@login_required
@admin_required
def settings_smtp():
    secret = current_app.config["SECRET_KEY"]
    smtp = _singleton(SmtpConfig)
    smtp.enabled = request.form.get("enabled") == "on"
    smtp.host = request.form.get("host", "").strip()
    smtp.port = _int(request.form.get("port"), 587)
    smtp.use_tls = request.form.get("use_tls") == "on"
    smtp.username = request.form.get("username", "").strip()
    pw = request.form.get("password", "")
    if pw.strip():  # keep existing password if blank
        smtp.password_enc = encrypt(secret, pw)
    smtp.from_addr = request.form.get("from_addr", "").strip()
    smtp.to_addr = request.form.get("to_addr", "").strip()
    smtp.admin_email = request.form.get("admin_email", "").strip()
    commit()
    flash("Email settings saved.", "success")
    return redirect(url_for("main.settings"))


@main_bp.route("/settings/test-email", methods=["POST"])
@login_required
@admin_required
def settings_test_email():
    secret = current_app.config["SECRET_KEY"]
    smtp_row = db.session.get(SmtpConfig, 1)
    if not smtp_row or not smtp_row.host or not smtp_row.to_addr:
        flash("Save the SMTP host and recipient first, then send a test.", "warning")
        return redirect(url_for("main.settings"))
    smtp = {
        "host": smtp_row.host,
        "port": smtp_row.port,
        "use_tls": smtp_row.use_tls,
        "username": smtp_row.username,
        "password": decrypt(secret, smtp_row.password_enc),
        "from_addr": smtp_row.from_addr,
        "to_addr": smtp_row.to_addr,
    }
    if not smtp["password"] and smtp_row.password_enc:
        log.warning("SMTP password could not be decrypted — SECRET_KEY may have changed; re-enter credentials in Settings.")
        flash("SMTP password could not be decrypted — SECRET_KEY may have changed; re-enter credentials in Settings.", "danger")
        return redirect(url_for("main.settings"))
    try:
        send_email(
            smtp,
            "JobSquire test email",
            "This is a test from your JobSquire. If you received this, email "
            "notifications are configured correctly.",
            "<p>This is a test from your <strong>JobSquire</strong>. If you received "
            "this, email notifications are configured correctly.</p>",
        )
        flash(f"Test email sent to {smtp_row.to_addr}. Check the inbox (and spam the first time).",
              "success")
    except Exception as e:  # noqa: BLE001
        flash(f"Test failed: {e.__class__.__name__}: {e}", "danger")
    return redirect(url_for("main.settings"))


@main_bp.route("/settings/run", methods=["POST"])
@login_required
@admin_required
def settings_run():
    _app = current_app._get_current_object()

    def _bg_search():
        with _app.app_context():
            run_search(trigger="manual")
    t = threading.Thread(target=_bg_search, daemon=True)
    t.start()
    flash("Search started — check Run History in a moment for results.", "info")
    return redirect(_safe_next(url_for("main.settings")))


# --------------------------------------------------------------------------
# Candidate asset library (master documents: resume, rec letters, certs, etc.)
# --------------------------------------------------------------------------
@main_bp.route("/settings/assets/upload", methods=["POST"])
@login_required
@admin_required
def settings_asset_upload():
    form = CandidateAssetForm()
    if form.validate_on_submit():
        f = form.file.data
        original = secure_filename(f.filename) or "file"
        ext = original.rsplit(".", 1)[-1].lower() if "." in original else ""
        kind = form.kind.data
        label = (form.label.data or "").strip()
        notes = form.notes.data or ""
        uploaded_by = current_user.display_name or current_user.username

        # "Custom Resume" (kind="Resume") is the markdown-draft slot read
        # back into the Getting Started paste-back box and shown to Claude
        # as "the" resume (see app/onboarding.py:_read_resume_asset_markdown)
        # -- it can never hold raw binary, so unlike every other kind, a
        # document uploaded here MUST convert successfully or the upload is
        # rejected outright rather than silently storing something broken.
        # The originally uploaded file is kept in source_* on the same row
        # (there's no separate archival copy the way "Base Resume" gets one).
        if kind == "Resume":
            from .onboarding import save_resume_draft
            from .resume_convert import ResumeConversionError, SUPPORTED_EXTENSIONS, convert_to_markdown

            if ext not in SUPPORTED_EXTENSIONS:
                flash(f"Custom Resume needs a file Job Squire can convert to markdown "
                      f"({', '.join(SUPPORTED_EXTENSIONS)}) — .{ext or 'this'} isn't "
                      "supported. Use Base Resume instead to keep the original file "
                      "as-is, or paste the text into the resume interview's markdown box.",
                      "danger")
                return redirect(_safe_next(url_for("main.settings", _anchor="tab-documents")))

            raw = f.read()
            try:
                markdown = convert_to_markdown(raw, ext)
            except ResumeConversionError as exc:
                flash(f"Couldn't convert this file: {exc}", "danger")
                return redirect(_safe_next(url_for("main.settings", _anchor="tab-documents")))
            except Exception:
                log.exception("resume auto-convert failed for a Custom Resume upload")
                flash("Automatic markdown conversion hit an unexpected error. Use Base "
                      "Resume instead, or paste the text into the resume interview's "
                      "markdown box.", "danger")
                return redirect(_safe_next(url_for("main.settings", _anchor="tab-documents")))

            source_stored = f"{uuid.uuid4().hex}{('.' + ext) if ext else ''}"
            source_dest = os.path.join(current_app.config["UPLOAD_DIR"], source_stored)
            with open(source_dest, "wb") as fh:
                fh.write(raw)

            result = save_resume_draft(
                markdown, created_by=uploaded_by,
                label=label or f'Converted from "{original}"')
            if result.get("ok"):
                asset = db.session.get(CandidateAsset, result["asset_id"])
                asset.source_stored_name = source_stored
                asset.source_original_name = original
                asset.source_content_type = f.mimetype or ""
                if notes:
                    asset.notes = notes
                commit()
                flash("Converted it to markdown and saved as a new Custom Resume — "
                      "review it below and edit if anything needs cleanup.", "success")
            else:
                try:
                    os.remove(source_dest)
                except OSError:
                    pass
                flash(f"Couldn't save the converted resume: {result.get('error')}", "danger")
            return redirect(_safe_next(url_for("main.settings", _anchor="tab-documents")))

        stored = f"{uuid.uuid4().hex}{('.' + ext) if ext else ''}"
        dest = os.path.join(current_app.config["UPLOAD_DIR"], stored)
        f.save(dest)
        asset = CandidateAsset(
            kind=kind,
            label=label,
            notes=notes,
            original_name=original,
            stored_name=stored,
            content_type=f.mimetype or "",
            size=os.path.getsize(dest),
            uploaded_by=uploaded_by,
        )
        db.session.add(asset)
        commit()
        flash(f"Uploaded \"{asset.display_name}\".", "success")

        # A "Base Resume" upload is the user's actual resume, uploaded as a
        # document rather than produced through the Getting Started resume
        # interview. Convert it to markdown here (no AI needed) and save it
        # as a new kind="Resume" variant the same way the interview does, so
        # a plain upload satisfies the Getting Started "Resume & documents"
        # step the same way the interview does. The original stays on file
        # as its own "Base Resume" asset regardless of whether conversion
        # succeeds. See app/resume_convert.py and
        # app/onboarding.py:save_resume_draft.
        if kind == "Base Resume":
            from .onboarding import save_resume_draft
            from .resume_convert import ResumeConversionError, SUPPORTED_EXTENSIONS, convert_to_markdown
            if ext in SUPPORTED_EXTENSIONS:
                try:
                    with open(dest, "rb") as fh:
                        raw = fh.read()
                    markdown = convert_to_markdown(raw, ext)
                    result = save_resume_draft(
                        markdown, created_by=uploaded_by,
                        label=f'Converted from "{original}"')
                    if result.get("ok"):
                        flash("Converted it to markdown and saved as a new Custom Resume — "
                              "review it below and edit if anything needs cleanup.", "success")
                    else:
                        flash(f"Uploaded, but couldn't auto-convert it: {result.get('error')}",
                              "warning")
                except ResumeConversionError as exc:
                    flash(f"Uploaded, but couldn't auto-convert it: {exc}", "warning")
                except Exception:
                    log.exception("resume auto-convert failed for asset %s", asset.id)
                    flash("Uploaded, but the automatic markdown conversion hit an "
                          "unexpected error. Use the resume interview below, or paste "
                          "the text into the markdown box yourself.", "warning")
            else:
                flash(f"Uploaded. Automatic markdown conversion isn't available for "
                      f".{ext or 'this'} files yet — use the resume interview below, or "
                      "paste the text into the markdown box yourself.", "warning")
    else:
        msg = "Upload failed."
        for errs in form.errors.values():
            msg = errs[0]
            break
        flash(msg, "danger")
    return redirect(_safe_next(url_for("main.settings", _anchor="tab-documents")))


@main_bp.route("/assets/<int:asset_id>/download")
@login_required
@admin_required
def asset_download(asset_id):
    asset = db.get_or_404(CandidateAsset, asset_id)
    path = os.path.join(current_app.config["UPLOAD_DIR"], asset.stored_name)
    if not os.path.exists(path):
        abort(404)
    return send_file(path, as_attachment=True, download_name=asset.original_name)


@main_bp.route("/assets/<int:asset_id>/download-source")
@login_required
@admin_required
def asset_download_source(asset_id):
    """The originally uploaded docx/pdf/txt behind a converted Custom Resume
    variant (see CandidateAsset.source_stored_name) -- 404s for variants that
    came from the interview or a manual paste, since there's no original
    file in that case."""
    asset = db.get_or_404(CandidateAsset, asset_id)
    if not asset.source_stored_name:
        abort(404)
    path = os.path.join(current_app.config["UPLOAD_DIR"], asset.source_stored_name)
    if not os.path.exists(path):
        abort(404)
    return send_file(path, as_attachment=True,
                      download_name=asset.source_original_name or asset.original_name)


@main_bp.route("/assets/<int:asset_id>/set-base", methods=["POST"])
@login_required
@admin_required
def asset_set_base(asset_id):
    """Promote a kind="Resume" variant to is_base=True, demoting whichever
    one currently holds it. The base variant is the one shown in the Getting
    Started paste-back box and used for tailoring -- see
    app/onboarding.py:save_resume_draft."""
    form = ConfirmForm()
    if not form.validate_on_submit():
        abort(400)
    asset = db.get_or_404(CandidateAsset, asset_id)
    if asset.kind != "Resume":
        flash("Only Custom Resume variants can be marked base.", "danger")
        return redirect(url_for("main.settings", _anchor="tab-documents"))
    (CandidateAsset.query.filter_by(kind="Resume", is_base=True)
     .update({"is_base": False}))
    asset.is_base = True
    commit()
    flash(f"\"{asset.display_name}\" is now your base resume.", "success")
    return redirect(_safe_next(url_for("main.settings", _anchor="tab-documents")))


@main_bp.route("/assets/<int:asset_id>/edit", methods=["POST"])
@login_required
@admin_required
def asset_edit(asset_id):
    asset = db.get_or_404(CandidateAsset, asset_id)
    form = CandidateAssetEditForm()
    if form.validate_on_submit():
        asset.kind = form.kind.data
        asset.label = (form.label.data or "").strip()
        asset.notes = form.notes.data or ""
        commit()
        flash("Document updated.", "success")
    else:
        flash("Could not save changes.", "danger")
    return redirect(url_for("main.settings", _anchor="tab-documents"))


@main_bp.route("/assets/<int:asset_id>/delete", methods=["POST"])
@login_required
@admin_required
def asset_delete(asset_id):
    form = ConfirmForm()
    if not form.validate_on_submit():
        abort(400)
    asset = db.get_or_404(CandidateAsset, asset_id)
    upload_dir = current_app.config["UPLOAD_DIR"]
    for name in (asset.stored_name, asset.source_stored_name):
        if not name:
            continue
        path = os.path.join(upload_dir, name)
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            current_app.logger.warning("Could not delete asset file %s", path)

    was_base = asset.kind == "Resume" and asset.is_base
    db.session.delete(asset)
    commit()

    # Deleting the base variant shouldn't silently leave the Getting Started
    # profile step (and kit-building) without any base resume -- promote the
    # newest remaining variant if one exists.
    if was_base:
        remaining = (CandidateAsset.query.filter_by(kind="Resume")
                     .order_by(CandidateAsset.uploaded_at.desc()).first())
        if remaining:
            remaining.is_base = True
            commit()

    flash("Document removed.", "success")
    return redirect(url_for("main.settings", _anchor="tab-documents"))


@main_bp.route("/settings/profile", methods=["POST"])
@login_required
@admin_required
def settings_profile():
    """Save the candidate profile markdown to disk."""
    text = request.form.get("candidate_profile", "").rstrip()
    _save_profile(text)
    flash("Candidate profile saved.", "success")
    return redirect(url_for("main.settings", _anchor="tab-documents"))


@main_bp.route("/settings/profile-prompt", methods=["POST"])
@login_required
@admin_required
def settings_profile_prompt():
    """Save the profile generation prompt to disk."""
    text = request.form.get("profile_prompt", "").rstrip()
    _save_profile_prompt(text)
    flash("Profile generation prompt saved.", "success")
    return redirect(url_for("main.settings", _anchor="tab-documents"))


@main_bp.route("/settings/profile/upload", methods=["POST"])
@login_required
@admin_required
def settings_profile_upload():
    """Replace the candidate profile by uploading a .md file."""
    file = request.files.get("profile_file")
    if not file or not file.filename:
        flash("No file selected.", "danger")
        return redirect(url_for("main.settings", _anchor="tab-documents"))
    if not file.filename.lower().endswith(".md"):
        flash("Only .md files are accepted for the candidate profile.", "danger")
        return redirect(url_for("main.settings", _anchor="tab-documents"))
    text = file.read().decode("utf-8", errors="replace").rstrip()
    _save_profile(text)
    flash(f"Candidate profile replaced from \"{file.filename}\".", "success")
    return redirect(url_for("main.settings", _anchor="tab-documents"))


# ---------------------------------------------------------------------------
# Triage backlog tool (hidden URL, login required)
# ---------------------------------------------------------------------------

@main_bp.route("/tools/triage-batch", methods=["GET", "POST"])
@login_required
def triage_batch():
    """Manual triage page — runs batches of 20 with a live log display.

    POST: launches run_triage_batch in a background thread, then redirects to
    GET ?run_id=... so the page can poll for live log output and render results.
    """
    from .models import AIProviderConfig as _APC

    providers = (
        _APC.query
        .filter_by(enabled=True)
        .filter_by(use_for_triage=True)
        .order_by(_APC.rank)
        .all()
    )

    if request.method == "POST":
        if not ConfirmForm().validate_on_submit():
            abort(400)
        offset = int(request.form.get("offset", 0) or 0)
        pid_raw = request.form.get("provider_id", "")
        provider_id = int(pid_raw) if pid_raw and pid_raw.isdigit() else None

        run_id = uuid.uuid4().hex
        data_dir = current_app.config["DATA_DIR"]
        status = _TaskStatus(run_id, "triage_batch", data_dir)
        _app = current_app._get_current_object()
        ai_log = logging.getLogger("app.ai")

        def _run():
            handler = _StatusLogHandler(status)
            _prior = ai_log.level
            ai_log.addHandler(handler)
            ai_log.setLevel(logging.INFO)
            with _app.app_context():
                try:
                    status.log("INFO Starting triage batch")
                    result = ai.run_triage_batch(offset, limit=20, provider_id=provider_id)
                    status.done(result)
                except Exception as exc:  # noqa: BLE001
                    log.exception("triage batch failed")
                    status.fail(exc)
                finally:
                    ai_log.removeHandler(handler)
                    ai_log.setLevel(_prior)

        threading.Thread(target=_run, daemon=True).start()
        return redirect(url_for("main.triage_batch", run_id=run_id))

    # GET — show the page (with or without an active run_id)
    run_id = request.args.get("run_id", "")
    total_remaining = (
        Job.query
        .filter(Job.status == "Saved")
        .filter((Job.ai_fit_score == None) | (Job.ai_fit_score == 0))  # noqa: E711
        .count()
    )

    return render_template(
        "triage_batch.html",
        providers=providers,
        run_id=run_id,
        poll_url=url_for("main.ai_task_poll", run_id=run_id) if run_id else "",
        total_remaining=total_remaining,
        confirm_form=ConfirmForm(),
    )


@main_bp.route("/tools/kit-batch", methods=["GET", "POST"])
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
        return redirect(url_for("main.kit_batch", run_id=run_id))

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
        poll_url=url_for("main.ai_task_poll", run_id=run_id) if run_id else "",
        total_remaining=total_remaining,
        confirm_form=ConfirmForm(),
    )


def _int(value, default, allow_none=False):
    if value is None or str(value).strip() == "":
        return None if allow_none else default
    try:
        return int(str(value).strip())
    except ValueError:
        return None if allow_none else default
