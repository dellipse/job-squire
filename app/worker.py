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
"""Scheduler process. Runs the search on a cron cadence.

Run as a separate service from the web app so it fires exactly once per slot
(the web app may run multiple gunicorn workers). Started with:

    python -m app.worker

Cadence is set by env vars (defaults match: 8am, 1pm, 5pm Mon-Fri plus a
weekend morning). Times use the job-search location's timezone. Schedule
changes require a worker restart; title/location changes are read live.

Also runs a heartbeat job (DATA_DIR/.worker_heartbeat, default every 5
minutes) so a dead or wedged process is detectable via the Docker healthcheck
and the in-app Settings/Dashboard staleness warning, without needing an HTTP
endpoint on this container.
"""
import logging
import os
import random
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from . import create_app
from .ai import (
    run_auto_triage, run_followup_drafts, run_rejection_analysis, run_weekly_review,
    _has_ranked_providers,
)
from .search import run_search
from .timezones import timezone_for_location

logging.basicConfig(level=logging.INFO, format="%(asctime)s [worker] %(message)s")
log = logging.getLogger("worker")


def _heartbeat_path():
    return os.path.join(os.environ.get("DATA_DIR", "/data"), ".worker_heartbeat")


def _touch_heartbeat():
    """Write the current time to the heartbeat file.

    This is how the Docker healthcheck (and the in-app Settings/Dashboard
    staleness warning, see ``app.main._worker_heartbeat_status``) tells whether
    this process is alive. It is written on startup and on its own
    HEARTBEAT_INTERVAL_MINUTES schedule, deliberately independent of the search
    cron jobs: a stopped heartbeat means the worker process/scheduler itself
    died or wedged, not merely that automated search is disabled or between
    runs. Failures here are logged but never allowed to crash the scheduler.
    """
    path = _heartbeat_path()
    try:
        with open(path, "w") as f:
            f.write(str(int(time.time())))
    except OSError:
        log.exception("could not write heartbeat file at %s", path)


def _resolve_timezone():
    """Pick the scheduler timezone.

    The server clock may be GMT/UTC; the schedule must follow the *job-search
    location's* local time. Order: explicit SCHEDULE_TZ override, else derived
    from the SearchConfig location, else UTC as a safe default.
    The raw TZ/OS timezone is deliberately ignored so a GMT host can't hijack it.
    """
    override = os.environ.get("SCHEDULE_TZ", "").strip()
    if override:
        try:
            ZoneInfo(override)
            return override, "SCHEDULE_TZ"
        except (ZoneInfoNotFoundError, ValueError):
            log.warning("SCHEDULE_TZ=%r is not a valid IANA zone; ignoring.", override)

    location = ""
    try:
        from .models import SearchConfig
        app = create_app()
        with app.app_context():
            from .extensions import db
            cfg = db.session.get(SearchConfig, 1)
            location = cfg.location if cfg else ""
    except Exception:  # noqa: BLE001 - never let tz lookup crash startup
        log.exception("could not read search location for timezone")

    tz = timezone_for_location(location)
    return tz, (f"location {location!r}" if location else "default")


def _run():
    # Jitter the actual start time so all workers on the same schedule don't
    # hammer provider APIs simultaneously. Range is 1–SCHEDULE_OFFSET_MAX_MINUTES.
    offset_max = max(1, int(os.environ.get("SCHEDULE_OFFSET_MAX_MINUTES", "20")))
    wait = random.uniform(60, offset_max * 60)
    log.info("start offset: %.0fs (max configured: %dm)", wait, offset_max)
    time.sleep(wait)

    app = create_app()
    with app.app_context():
        run = None
        try:
            run = run_search(trigger="scheduled")
            if run is None:
                log.info("skipped (disabled or no titles)")
            else:
                log.info("run: %s new, %s skipped, %s seen, status=%s",
                         run.created, run.skipped, run.found, run.status)
        except Exception:  # noqa: BLE001
            log.exception("scheduled run failed")

        # Feature 1: Auto-triage — score new Saved jobs after each search run.
        _run_auto_triage_if_enabled(app, run)

        # Feature 5: Rejection alert — check threshold after every run.
        _check_rejection_threshold(app)


def _run_auto_triage_if_enabled(app, search_run):
    """Run auto-triage if Automatic features are enabled and the task is enabled.

    Called after every scheduled search run (even if run is None, in case
    there are unscored jobs from a previous run that never got triaged).
    """
    with app.app_context():
        try:
            from .db_utils import commit
            from .extensions import db
            from .models import AIConfig, AITaskConfig, SearchRun
            from datetime import datetime, timezone

            cfg = db.session.get(AIConfig, 1)
            if not cfg or not cfg.api_enabled:
                return

            task_cfg = AITaskConfig.query.filter_by(task_name="triage").first()
            if task_cfg is not None and not task_cfg.enabled:
                return
            if task_cfg is None and not cfg.auto_triage_enabled:
                return  # legacy fallback

            if not _has_ranked_providers():
                log.warning("auto-triage: enabled but no providers configured — skipping")
                return

            log.info("auto-triage: starting")
            result = run_auto_triage()
            log.info("auto-triage: scored=%d failed=%d", result["scored"], result["failed"])

            # Stamp the search run record if we have one.
            if search_run is not None:
                run_row = db.session.get(SearchRun, search_run.id)
                if run_row:
                    run_row.last_triage_at = datetime.now(timezone.utc)
                    commit()

        except Exception:  # noqa: BLE001
            log.exception("auto-triage failed")


def _smtp_dict(smtp_cfg, secret):
    """Convert a SmtpConfig row to the dict expected by notify.send_email."""
    from .crypto import decrypt as _dec
    return {
        "host": smtp_cfg.host,
        "port": smtp_cfg.port,
        "use_tls": smtp_cfg.use_tls,
        "username": smtp_cfg.username,
        "password": _dec(secret, smtp_cfg.password_enc or ""),
        "from_addr": smtp_cfg.from_addr,
        "to_addr": smtp_cfg.to_addr,
    }


def _run_followup_drafts_job():
    """Feature 2: Generate follow-up drafts for overdue jobs. Runs daily at 6 AM."""
    app = create_app()
    with app.app_context():
        try:
            from .extensions import db
            from .models import AIConfig, AITaskConfig, SmtpConfig
            from .notify import build_followup_digest, send_email

            cfg = db.session.get(AIConfig, 1)
            if not cfg or not cfg.api_enabled:
                return

            task_cfg = AITaskConfig.query.filter_by(task_name="followup").first()
            if task_cfg is not None and not task_cfg.enabled:
                return
            if task_cfg is None and not cfg.auto_followup_enabled:
                return  # legacy fallback

            if not _has_ranked_providers():
                log.warning("auto-followup: no providers configured — skipping")
                return

            secret = os.environ.get("SECRET_KEY", "")
            log.info("auto-followup: starting")
            result = run_followup_drafts()
            log.info("auto-followup: drafted=%d failed=%d", result["drafted"], result["failed"])

            if result["drafted"] > 0:
                smtp = db.session.get(SmtpConfig, 1)
                base_url = os.environ.get("PUBLIC_URL", "")
                if smtp and smtp.enabled and smtp.host:
                    subj, txt, htm = build_followup_digest(
                        result["jobs"], base_url=base_url or None
                    )
                    try:
                        send_email(_smtp_dict(smtp, secret), subj, txt, htm)
                        log.info("auto-followup: digest email sent")
                    except Exception as exc:  # noqa: BLE001
                        log.warning("auto-followup: email failed: %s", exc)
        except Exception:  # noqa: BLE001
            log.exception("auto-followup job failed")


def _run_weekly_review_job():
    """Feature 3: Generate the weekly strategy review. Runs Mondays at 6 AM."""
    app = create_app()
    with app.app_context():
        try:
            from .extensions import db
            from .models import AIConfig, AITaskConfig, SmtpConfig
            from .notify import build_weekly_review_email, send_email

            cfg = db.session.get(AIConfig, 1)
            if not cfg or not cfg.api_enabled:
                return

            task_cfg = AITaskConfig.query.filter_by(task_name="weekly_review").first()
            if task_cfg is not None and not task_cfg.enabled:
                return
            if task_cfg is None and not cfg.auto_weekly_review_enabled:
                return  # legacy fallback

            if not _has_ranked_providers():
                log.warning("weekly-review: no providers configured — skipping")
                return

            secret = os.environ.get("SECRET_KEY", "")
            log.info("weekly-review: starting")
            result = run_weekly_review()

            smtp = db.session.get(SmtpConfig, 1)
            base_url = os.environ.get("PUBLIC_URL", "")
            if smtp and smtp.enabled and smtp.host:
                subj, txt, htm = build_weekly_review_email(
                    result.get("overall_summary", ""),
                    result.get("recommendations", []),
                    base_url=base_url or None,
                )
                try:
                    send_email(_smtp_dict(smtp, secret), subj, txt, htm)
                    log.info("weekly-review: email sent")
                except Exception as exc:  # noqa: BLE001
                    log.warning("weekly-review: email failed: %s", exc)
            log.info("weekly-review: complete")
        except Exception:  # noqa: BLE001
            log.exception("weekly-review job failed")


def _check_rejection_threshold(app):
    """Feature 5: Trigger rejection analysis if rejection count exceeds threshold."""
    with app.app_context():
        try:
            from datetime import datetime, timedelta, timezone
            from .db_utils import commit
            from .extensions import db
            from .models import AIConfig, AITaskConfig, Job, SmtpConfig
            from .notify import build_rejection_alert_email, send_email

            cfg = db.session.get(AIConfig, 1)
            if not cfg or not cfg.api_enabled:
                return

            task_cfg = AITaskConfig.query.filter_by(task_name="rejection_alert").first()
            if task_cfg is not None and not task_cfg.enabled:
                return

            if not _has_ranked_providers():
                return

            secret = os.environ.get("SECRET_KEY", "")
            threshold = cfg.rejection_alert_threshold or 5
            window_start = datetime.now(timezone.utc) - timedelta(days=14)

            # Count rejections in the past 14 days.
            recent_rejections = (
                Job.query
                .filter(Job.status.in_(["Rejected", "Ghosted"]))
                .filter(Job.updated_at >= window_start)
                .count()
            )
            if recent_rejections < threshold:
                return

            # Skip if we already ran an analysis recently (within 7 days).
            if cfg.last_rejection_analysis_at:
                last = cfg.last_rejection_analysis_at
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                if (datetime.now(timezone.utc) - last).days < 7:
                    return

            log.info("rejection-alert: %d rejections in 14 days (threshold=%d) — running analysis",
                     recent_rejections, threshold)

            result = run_rejection_analysis()

            # Stamp the config so we don't re-alert this week.
            cfg.last_rejection_analysis_at = datetime.now(timezone.utc)
            commit()

            smtp = db.session.get(SmtpConfig, 1)
            base_url = os.environ.get("PUBLIC_URL", "")
            if smtp and smtp.enabled and smtp.host:
                subj, txt, htm = build_rejection_alert_email(
                    result.get("overall_summary", ""),
                    result.get("recommendations", []),
                    rejection_count=recent_rejections,
                    base_url=base_url or None,
                )
                try:
                    send_email(_smtp_dict(smtp, secret), subj, txt, htm)
                    log.info("rejection-alert: email sent")
                except Exception as exc:  # noqa: BLE001
                    log.warning("rejection-alert: email failed: %s", exc)
        except Exception:  # noqa: BLE001
            log.exception("rejection threshold check failed")


def main():
    weekday_hours = os.environ.get("SCHEDULE_WEEKDAY_HOURS", "8,13,17")
    weekend_hours = os.environ.get("SCHEDULE_WEEKEND_HOURS", "9")
    minute = os.environ.get("SCHEDULE_MINUTE", "0")
    # Feature 2: Follow-up drafts — daily at this hour (default 6 AM local).
    followup_hour = os.environ.get("FOLLOWUP_DRAFT_HOUR", "6")
    # Feature 3: Weekly review — Mondays at this hour (default 6 AM local).
    weekly_review_hour = os.environ.get("WEEKLY_REVIEW_HOUR", "6")
    tz, tz_source = _resolve_timezone()

    sched = BlockingScheduler(timezone=tz)
    if weekday_hours.strip():
        sched.add_job(_run, CronTrigger(day_of_week="mon-fri", hour=weekday_hours,
                                        minute=minute, timezone=tz),
                      id="weekday", max_instances=1, coalesce=True)
    if weekend_hours.strip():
        sched.add_job(_run, CronTrigger(day_of_week="sat,sun", hour=weekend_hours,
                                        minute=minute, timezone=tz),
                      id="weekend", max_instances=1, coalesce=True)

    # Feature 2: Auto follow-up drafts — daily.
    if followup_hour.strip():
        sched.add_job(_run_followup_drafts_job,
                      CronTrigger(hour=followup_hour, minute="0", timezone=tz),
                      id="followup_drafts", max_instances=1, coalesce=True)

    # Feature 3: Weekly strategy review — Mondays only.
    if weekly_review_hour.strip():
        sched.add_job(_run_weekly_review_job,
                      CronTrigger(day_of_week="mon", hour=weekly_review_hour,
                                  minute="0", timezone=tz),
                      id="weekly_review", max_instances=1, coalesce=True)

    # Heartbeat — proves the scheduler process itself is alive, independent of
    # whether search/AI features are enabled or due to run. Backs the
    # container's aggregated healthcheck and the in-app staleness warning.
    heartbeat_minutes = max(1, int(os.environ.get("HEARTBEAT_INTERVAL_MINUTES", "5")))
    sched.add_job(_touch_heartbeat, "interval", minutes=heartbeat_minutes,
                  id="heartbeat", max_instances=1, coalesce=True)

    log.info("scheduler up (tz=%s via %s, weekdays=%s, weekends=%s, followup=%s:00, review=Mon %s:00, "
             "heartbeat=%sm). Jobs: %s",
             tz, tz_source, weekday_hours, weekend_hours, followup_hour, weekly_review_hour,
             heartbeat_minutes, [str(j.trigger) for j in sched.get_jobs()])

    # Write one heartbeat immediately so the healthcheck passes during
    # start_period without waiting for the first interval tick.
    _touch_heartbeat()

    if os.environ.get("RUN_ON_START") == "1":
        _run()

    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()
