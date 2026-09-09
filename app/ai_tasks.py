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
"""AI analysis blueprint: manual export/import, one-click API analysis, the
triage/followup/weekly-review hub actions, and the manual triage-backlog
tool.

Split out of app/main.py (QUAL-01, Long-term audit item 17).
"""
import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone

from flask import Blueprint, Response, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from . import ai, privacy
from .crypto import decrypt
from .db_utils import commit
from .extensions import db
from .forms import AIImportForm, ConfirmForm
from .main import _singleton
from .models import AIConfig, AIInsight, AIProviderConfig, Job
from .task_status import _StatusLogHandler, _TaskStatus

log = logging.getLogger(__name__)

ai_tasks_bp = Blueprint("ai_tasks", __name__)


@ai_tasks_bp.route("/export/ai")
@login_required
def export_ai():
    # Manual-mode export goes to whatever AI chat the user pastes it into —
    # redact it like any other transmission.
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


@ai_tasks_bp.route("/ai", methods=["GET", "POST"])
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
            return redirect(url_for("ai_tasks.ai_hub"))
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
            return redirect(url_for("ai_tasks.ai_hub"))
        updated, missing = ai.apply_analysis(
            parsed, created_by=current_user.display_name or current_user.username)
        msg = f"Imported analysis. Updated {updated} job(s)."
        if missing:
            msg += f" {missing} job id(s) did not match and were skipped."
        flash(msg, "success")
        return redirect(url_for("ai_tasks.ai_hub"))

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


@ai_tasks_bp.route("/ai/analyze", methods=["POST"])
@login_required
def ai_analyze():
    """Full-pipeline AI analysis (api_mode "Analyze now" button).

    REL-01: was previously a synchronous in-request call — the same class of
    gunicorn-timeout SIGKILL as the job 1162 ats-gap incident. Routed through
    the shared background-thread + poll pattern used by triage/followup/
    weekly_review (see `ai_run_task` just above).
    """
    if not ConfirmForm().validate_on_submit():
        abort(400)
    cfg = _singleton(AIConfig)
    secret = current_app.config["SECRET_KEY"]
    api_key = decrypt(secret, cfg.api_key_enc) if cfg.api_key_enc else ""
    has_providers = ai._has_ranked_providers()
    if not api_key and not has_providers:
        flash("Add an AI provider or Anthropic API key under Settings first.", "warning")
        return redirect(url_for("ai_tasks.ai_hub"))

    run_id = uuid.uuid4().hex
    data_dir = current_app.config["DATA_DIR"]
    status = _TaskStatus(run_id, "analyze", data_dir)
    _app = current_app._get_current_object()
    ai_log = logging.getLogger("app.ai")
    created_by = current_user.display_name or current_user.username

    def _run():
        handler = _StatusLogHandler(status)
        prior_level = ai_log.level
        ai_log.addHandler(handler)
        ai_log.setLevel(logging.INFO)
        with _app.app_context():
            try:
                status.log("INFO Analyzing the full pipeline…")
                parsed, provider = ai.run_api_analysis(api_key, cfg.model, cfg.thinking_mode or "disabled")
                updated, missing = ai.apply_analysis(parsed, created_by=created_by, provider=provider)
                status.done({"updated": updated, "skipped": missing,
                            "overall_summary": parsed.get("overall_summary", "")})
            except Exception as exc:  # noqa: BLE001
                db.session.rollback()
                log.exception("ai_analyze failed")
                status.fail(exc)
            finally:
                ai_log.removeHandler(handler)
                ai_log.setLevel(prior_level)

    threading.Thread(target=_run, daemon=True).start()
    return redirect(url_for("task_status.ai_task_status", run_id=run_id, task="analyze"))


@ai_tasks_bp.route("/ai/run/<task>", methods=["POST"])
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
        return redirect(url_for("settings.settings", _anchor="ai-auto-settings-card"))
    if not ai._has_ranked_providers():
        secret = current_app.config["SECRET_KEY"]
        api_key = decrypt(secret, cfg.api_key_enc) if cfg.api_key_enc else ""
        if not api_key:
            flash("Add an AI provider or Anthropic API key under Settings → Claude first.", "warning")
            anchor = "tab-documents" if task == "rescore" else f"feature-{task}"
            return redirect(url_for("settings.settings", _anchor=anchor))

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
    return redirect(url_for("task_status.ai_task_status", run_id=run_id, task=task))


# ---------------------------------------------------------------------------
# Triage backlog tool (hidden URL, login required)
# ---------------------------------------------------------------------------

@ai_tasks_bp.route("/tools/triage-batch", methods=["GET", "POST"])
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
        return redirect(url_for("ai_tasks.triage_batch", run_id=run_id))

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
        poll_url=url_for("task_status.ai_task_poll", run_id=run_id) if run_id else "",
        total_remaining=total_remaining,
        confirm_form=ConfirmForm(),
    )
