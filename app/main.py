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
import json
import logging
import os
import time
from datetime import date, datetime, timedelta, timezone
from functools import wraps

import markdown as markdown_lib

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    redirect,
    render_template,
    url_for,
)
from flask_login import current_user, login_required
from sqlalchemy import func
from werkzeug.utils import secure_filename

from .db_utils import commit

log = logging.getLogger(__name__)
from .extensions import db
from .models import (
    ACTIVE_STATUSES,
    ACTIVE_SUBMISSION_STATUSES,
    STATUSES,
    AIConfig,
    AIInsight,
    Contact,
    Interview,
    Job,
    JobNote,
    Submission,
)

main_bp = Blueprint("main", __name__)


def _csv_safe(value):
    """Prevent CSV formula injection by prefixing dangerous leading characters."""
    if value and isinstance(value, str) and value[0] in ('=', '+', '-', '@', '\t', '\r'):
        return "'" + value
    return value or ""


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


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(403)
        return f(*args, **kwargs)

    return wrapper


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
                "url": url_for("jobs.job_detail", job_id=j.id),
                "status": j.status,
            })
        if j.kit_generated_at:
            events.append({
                "date": j.kit_generated_at.date(),
                "type": "kit",
                "label": "Kit built",
                "detail": f"{j.title} at {j.company}",
                "url": url_for("jobs.job_detail", job_id=j.id),
                "status": j.status,
            })

    for n in JobNote.query.filter(JobNote.note_type == "status_change").all():
        if n.created_at:
            events.append({
                "date": n.created_at.date(),
                "type": "status",
                "label": "Status change",
                "detail": n.content,
                "url": url_for("jobs.job_detail", job_id=n.job_id),
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
                "url": url_for("jobs.job_detail", job_id=iv.job_id),
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
    return redirect(url_for("settings.settings"))


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


def _singleton(model):
    row = db.session.get(model, 1)
    if not row:
        row = model(id=1)
        db.session.add(row)
        commit()
    return row


