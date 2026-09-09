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
"""Jobs blueprint: the job list/detail/edit views, single-job AI actions,
bulk updates, notes, interview debriefs, attachments, and CSV export.

Split out of app/main.py (QUAL-01, Long-term audit item 17). _csv_safe,
_singleton, and admin_required are imported from app.main rather than
duplicated -- all three are shared with other blueprints (contacts.py
already imports _csv_safe the same way).
"""
import csv
import io
import logging
import os
import threading
import uuid
from datetime import date, datetime, timedelta, timezone

from flask import (
    Blueprint, Response, abort, current_app, flash, redirect, render_template,
    request, send_file, session, url_for,
)
from flask_login import current_user, login_required
from werkzeug.utils import secure_filename

from .db_utils import commit
from .extensions import db
from .forms import AttachmentForm, ConfirmForm, InterviewForm, JobForm
from .main import _csv_safe, _singleton, _stale_cutoffs, admin_required
from .models import ACTIVE_STATUSES, STATUSES, AIConfig, Attachment, Interview, Job, JobNote
from .task_status import _StatusLogHandler, _TaskStatus

log = logging.getLogger(__name__)

jobs_bp = Blueprint("jobs", __name__)

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
# Jobs
# --------------------------------------------------------------------------
@jobs_bp.route("/jobs")
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
    _stale_filter_saved_cutoff, _stale_filter_active_cutoff, _stale_filter_active_statuses = _stale_cutoffs()
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
    _saved_cutoff, _active_cutoff, _stale_active_list = _stale_cutoffs()
    _stale_active = set(_stale_active_list)
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


@jobs_bp.route("/jobs/save-default-view", methods=["POST"])
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
    return redirect(url_for("jobs.jobs_list", sort=sort_str, status=status, per_page=per_page))


@jobs_bp.route("/jobs/clear-default-view", methods=["POST"])
@login_required
def jobs_clear_default_view():
    """Clear the user's saved default view (revert to system defaults)."""
    current_user.jobs_default_sort = None
    current_user.jobs_default_status = None
    current_user.jobs_default_per_page = None
    commit()
    flash("Default view cleared.", "success")
    return redirect(url_for("jobs.jobs_list"))


@jobs_bp.route("/jobs/new", methods=["GET", "POST"])
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
        return redirect(url_for("jobs.job_detail", job_id=job.id))
    return render_template("job_form.html", form=form, mode="new")


@jobs_bp.route("/jobs/<int:job_id>")
@login_required
def job_detail(job_id):
    job = db.get_or_404(Job, job_id)
    ai_cfg = _singleton(AIConfig)
    gcal_urls = {iv.id: _gcal_interview_url(iv, job) for iv in job.interviews}
    return render_template("job_detail.html", job=job, today=date.today(),
                           confirm_form=ConfirmForm(), ai_mode=ai_cfg.mode,
                           connector_name=ai_cfg.connector_name or "job-squire",
                           gcal_urls=gcal_urls)


@jobs_bp.route("/jobs/<int:job_id>/edit", methods=["GET", "POST"])
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
        return redirect(url_for("jobs.job_detail", job_id=job.id))
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
        return redirect(url_for("jobs.job_detail", job_id=job_id))

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
    return redirect(url_for("task_status.ai_task_status", run_id=run_id, task=task_name))


@jobs_bp.route("/jobs/<int:job_id>/ats-gap", methods=["POST"])
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


@jobs_bp.route("/jobs/<int:job_id>/score-fit", methods=["POST"])
@login_required
def job_score_fit(job_id):
    """Score a single job's fit via the API (api_mode button on job detail)."""
    def _work(job):
        from .ai import run_score_fit_single
        return run_score_fit_single(job)
    return _run_single_job_ai_task(job_id, "score_fit", "Score fit", _work)


@jobs_bp.route("/jobs/<int:job_id>/draft-followup", methods=["POST"])
@login_required
def job_draft_followup(job_id):
    """Draft a follow-up email for a single job via the API (api_mode button on job detail)."""
    def _work(job):
        from .ai import run_draft_followup_single
        return run_draft_followup_single(job)
    return _run_single_job_ai_task(job_id, "draft_followup", "Draft follow-up", _work)


@jobs_bp.route("/jobs/build-kits-api", methods=["POST"])
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


@jobs_bp.route("/jobs/<int:job_id>/prep-interview", methods=["POST"])
@login_required
def job_prep_interview(job_id):
    """Generate an interview prep guide for a single job via the API (api_mode button).

    REL-01: was previously a synchronous in-request call — the same class of
    gunicorn-timeout SIGKILL as the job 1162 ats-gap incident. Routed through
    the shared background-thread + poll pattern like the other single-job AI
    actions below.
    """
    def _work(job):
        from .ai import run_interview_prep_single
        run_interview_prep_single(job)
        return {}
    return _run_single_job_ai_task(job_id, "prep_interview", "Interview prep", _work)


@jobs_bp.route("/jobs/<int:job_id>/delete", methods=["POST"])
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
    return redirect(url_for("jobs.jobs_list"))


# --------------------------------------------------------------------------
# Bulk status update
# --------------------------------------------------------------------------
@jobs_bp.route("/jobs/bulk-update", methods=["POST"])
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
        return redirect(url_for("jobs.jobs_list"))

    action = request.form.get("action", "").strip()
    if action == "set_status":
        new_status = request.form.get("status", "").strip()
        if new_status not in STATUSES:
            flash("Invalid status.", "error")
            return redirect(url_for("jobs.jobs_list"))
    elif action == "withdrawn":
        new_status = "Withdrawn"
    elif action == "ghosted":
        new_status = "Ghosted"
    elif action == "pass":
        new_status = "Pass"
    else:
        flash("Unknown action.", "error")
        return redirect(url_for("jobs.jobs_list"))

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
    return redirect(url_for("jobs.jobs_list"))


# --------------------------------------------------------------------------
# Job notes / activity log
# --------------------------------------------------------------------------
@jobs_bp.route("/jobs/<int:job_id>/notes", methods=["POST"])
@login_required
def job_add_note(job_id):
    job = db.get_or_404(Job, job_id)
    form = ConfirmForm()
    if not form.validate_on_submit():
        abort(400)
    content = (request.form.get("content") or "").strip()
    if not content:
        flash("Note cannot be empty.", "error")
        return redirect(url_for("jobs.job_detail", job_id=job_id))
    _add_job_note(job.id, content, note_type="note")
    commit()
    flash("Note added.", "success")
    return redirect(url_for("jobs.job_detail", job_id=job_id))


@jobs_bp.route("/jobs/<int:job_id>/set-followup", methods=["POST"])
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
            return redirect(url_for("jobs.job_detail", job_id=job_id))
    else:
        new_date = _business_days_from(date.today(), 3)
    old_followup = job.follow_up_date
    job.follow_up_date = new_date
    if new_date != old_followup:
        _add_job_note(job.id, f"Follow-up date set to {new_date}.", note_type="follow_up")
    commit()
    flash(f"Follow-up date set to {new_date}.", "success")
    return redirect(url_for("jobs.job_detail", job_id=job_id))


# --------------------------------------------------------------------------
# Interview debriefs
# --------------------------------------------------------------------------
@jobs_bp.route("/jobs/<int:job_id>/interviews/new", methods=["GET", "POST"])
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
        return redirect(url_for("jobs.job_detail", job_id=job.id))
    return render_template("interview_form.html", form=form, job=job, mode="new",
                           gcal_url=None)


@jobs_bp.route("/interviews/<int:iv_id>/edit", methods=["GET", "POST"])
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
        return redirect(url_for("jobs.job_detail", job_id=iv.job_id))
    gcal_url = _gcal_interview_url(iv, iv.job) if iv.interview_date else None
    return render_template("interview_form.html", form=form, job=iv.job, mode="edit",
                           gcal_url=gcal_url)


@jobs_bp.route("/interviews/<int:iv_id>/delete", methods=["POST"])
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
    return redirect(url_for("jobs.job_detail", job_id=job_id))


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
# Attachments
# --------------------------------------------------------------------------
def _delete_attachment_file(att):
    path = os.path.join(current_app.config["UPLOAD_DIR"], att.stored_name)
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        current_app.logger.warning("Could not delete file %s", path)


@jobs_bp.route("/jobs/<int:job_id>/upload", methods=["POST"])
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
    return redirect(url_for("jobs.job_detail", job_id=job_id))


@jobs_bp.route("/attachments/<int:att_id>/download")
@login_required
def attachment_download(att_id):
    att = db.get_or_404(Attachment, att_id)
    path = os.path.join(current_app.config["UPLOAD_DIR"], att.stored_name)
    if not os.path.exists(path):
        abort(404)
    return send_file(path, as_attachment=True, download_name=att.original_name)


@jobs_bp.route("/attachments/<int:att_id>/delete", methods=["POST"])
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
    return redirect(url_for("jobs.job_detail", job_id=job_id))


# --------------------------------------------------------------------------
# CSV export
# --------------------------------------------------------------------------
@jobs_bp.route("/export/csv")
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
