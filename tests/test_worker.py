# Copyright (C) 2026 D. Brandmeyer
# Licensed under the GNU Affero General Public License v3 or later.
"""Tests for the scheduler process (app/worker.py), previously untested at
13% coverage per the 2026-09-08 pre-prod audit (TEST-01: "no test_worker.py").

Scope, per the audit's own recommendation for this item: assert job
registration (the scheduler wires up the expected cron/interval jobs) and the
heartbeat file (written on startup, and read by
app.main._worker_heartbeat_status to back the Dashboard/Settings staleness
banner -- see tests/test_ops.py for the reader side). This deliberately does
not re-run the job bodies themselves (_run, _run_followup_drafts_job, etc.)
end to end -- those call into app.search / app.ai, which already have their
own network-boundary mocks in test_search_run_resilience.py and
test_triage_batch_retry.py; duplicating that here would just be a second,
weaker copy of the same coverage.

House style borrowed from those two files: mock exactly the boundary that
would otherwise reach outside the process (here, BlockingScheduler.start(),
which runs the real event loop forever) and assert real end-state -- the
jobs actually registered on the real scheduler object, the heartbeat file
actually written to disk -- rather than mocking so much the test proves
nothing.
"""
import os
import time

from apscheduler.schedulers.blocking import BlockingScheduler

from app import worker


class _NonBlockingScheduler(BlockingScheduler):
    """A real BlockingScheduler in every respect except start(): job
    registration (add_job) already happened by the time worker.main() calls
    start(), so overriding it to return immediately lets the test inspect
    get_jobs() without spinning up a thread or hanging the test on the real
    (forever) event loop.
    """
    def start(self, *args, **kwargs):
        return None


def _recording_scheduler_class(captured):
    """A _NonBlockingScheduler subclass that stashes the instance it creates
    into `captured["sched"]` so the test can inspect it after worker.main()
    (which only keeps the scheduler as a local variable) returns."""
    class RecordingScheduler(_NonBlockingScheduler):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            captured["sched"] = self
    return RecordingScheduler


def _heartbeat_path(data_dir):
    return os.path.join(data_dir, ".worker_heartbeat")


# --------------------------------------------------------------------------- #
# Job registration
# --------------------------------------------------------------------------- #

def test_main_registers_expected_jobs(app, monkeypatch):
    """worker.main() wires up one job per automated feature: the search
    schedule (weekday/weekend), auto-triage's own short interval, the daily
    follow-up drafts job, the Monday weekly review, and the heartbeat.
    SCHEDULE_TZ is already set by conftest (America/Los_Angeles), so
    _resolve_timezone() takes the override branch and never touches the DB.
    """
    monkeypatch.setenv("DATA_DIR", app.config["DATA_DIR"])
    captured = {}
    monkeypatch.setattr(worker, "BlockingScheduler", _recording_scheduler_class(captured))

    worker.main()

    job_ids = {j.id for j in captured["sched"].get_jobs()}
    assert job_ids == {
        "weekday", "weekend", "followup_drafts", "weekly_review",
        "auto_triage_interval", "heartbeat",
    }


def test_main_respects_disabled_schedules(app, monkeypatch):
    """A blank cadence env var (or 0 for the triage interval) is the
    documented way to fully disable a feature -- confirm that actually skips
    registering the job, rather than registering it with a broken trigger.
    """
    monkeypatch.setenv("DATA_DIR", app.config["DATA_DIR"])
    monkeypatch.setenv("SCHEDULE_WEEKEND_HOURS", "")
    monkeypatch.setenv("TRIAGE_INTERVAL_MINUTES", "0")
    captured = {}
    monkeypatch.setattr(worker, "BlockingScheduler", _recording_scheduler_class(captured))

    worker.main()

    job_ids = {j.id for j in captured["sched"].get_jobs()}
    assert "weekend" not in job_ids
    assert "auto_triage_interval" not in job_ids
    # Everything else, still on its non-blank default, is unaffected.
    assert {"weekday", "followup_drafts", "weekly_review", "heartbeat"} <= job_ids


def test_heartbeat_job_uses_the_configured_interval(app, monkeypatch):
    monkeypatch.setenv("DATA_DIR", app.config["DATA_DIR"])
    monkeypatch.setenv("HEARTBEAT_INTERVAL_MINUTES", "7")
    captured = {}
    monkeypatch.setattr(worker, "BlockingScheduler", _recording_scheduler_class(captured))

    worker.main()

    hb_job = captured["sched"].get_job("heartbeat")
    assert hb_job is not None
    assert hb_job.trigger.interval.total_seconds() == 7 * 60


# --------------------------------------------------------------------------- #
# Heartbeat file
# --------------------------------------------------------------------------- #

def test_main_writes_heartbeat_immediately_on_start(app, monkeypatch):
    """main() writes one heartbeat before sched.start() (per the module
    docstring: "so the healthcheck passes during start_period without waiting
    for the first interval tick") -- independent of whether the scheduler
    ever actually ticks.
    """
    data_dir = app.config["DATA_DIR"]
    monkeypatch.setenv("DATA_DIR", data_dir)
    path = _heartbeat_path(data_dir)
    if os.path.exists(path):
        os.remove(path)

    monkeypatch.setattr(worker, "BlockingScheduler", _NonBlockingScheduler)

    worker.main()

    assert os.path.exists(path)
    with open(path) as f:
        stamp = int(f.read().strip())
    assert abs(time.time() - stamp) < 10


def test_touch_heartbeat_writes_current_timestamp(app_context, monkeypatch):
    data_dir = app_context.config["DATA_DIR"]
    monkeypatch.setenv("DATA_DIR", data_dir)
    path = _heartbeat_path(data_dir)
    if os.path.exists(path):
        os.remove(path)

    worker._touch_heartbeat()

    assert os.path.exists(path)
    with open(path) as f:
        stamp = int(f.read().strip())
    assert abs(time.time() - stamp) < 10


def test_touch_heartbeat_updates_an_existing_stale_file(app_context, monkeypatch):
    data_dir = app_context.config["DATA_DIR"]
    monkeypatch.setenv("DATA_DIR", data_dir)
    path = _heartbeat_path(data_dir)
    with open(path, "w") as f:
        f.write("0")
    old = time.time() - 3600
    os.utime(path, (old, old))

    worker._touch_heartbeat()

    with open(path) as f:
        stamp = int(f.read().strip())
    assert abs(time.time() - stamp) < 10


def test_touch_heartbeat_swallows_write_failure(app_context, monkeypatch):
    """A write failure (e.g. DATA_DIR missing/read-only) must be logged, never
    raised -- the module docstring is explicit that heartbeat failures can
    never be allowed to crash the scheduler process.
    """
    monkeypatch.setattr(worker, "_heartbeat_path",
                        lambda: "/nonexistent-dir-for-test-01/.worker_heartbeat")
    worker._touch_heartbeat()  # must not raise
