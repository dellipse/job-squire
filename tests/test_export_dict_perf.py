# Copyright (C) 2026 D. Brandmeyer
# Licensed under the GNU Affero General Public License v3 or later.
"""PERF-01: ``Job.interviews`` must not N+1 when a full-pipeline export
(``build_export_dict``, used by manual analyze, weekly review, and rejection
analysis) iterates every job's interviews. Before the fix, the relationship's
default ``lazy="select"`` strategy issued one extra query per job -- ~200
jobs meant ~201 round trips with the session open into the AI provider call.

The fix is ``lazy="selectin"`` on ``Job.interviews`` (app/models.py), which
batches the child load into one (or a handful of, above SQLite's variable-
count-per-statement ceiling) ``WHERE job_id IN (...)`` queries regardless of
how many jobs there are.
"""
from datetime import date, timedelta

from sqlalchemy import event

from app import ai
from app.extensions import db
from app.models import Interview, Job


def _count_queries(engine, fn):
    count = {"n": 0}

    def _before_cursor_execute(*args, **kwargs):
        count["n"] += 1

    event.listen(engine, "before_cursor_execute", _before_cursor_execute)
    try:
        result = fn()
    finally:
        event.remove(engine, "before_cursor_execute", _before_cursor_execute)
    return result, count["n"]


def _make_job_with_interviews(company, n_interviews):
    job = Job(company=company, title="Engineer", status="Applied", source="perf-test")
    db.session.add(job)
    db.session.flush()  # need job.id for the FK below
    for i in range(n_interviews):
        db.session.add(Interview(
            job_id=job.id,
            round_type=f"Round {i}",
            interview_date=date(2026, 1, 1) + timedelta(days=i),
        ))
    return job


def test_build_export_dict_query_count_is_bounded_by_job_count(app_context):
    # A handful of jobs, several with multiple interviews -- if job.interviews
    # were still lazy="select", query count would scale with job count (one
    # extra SELECT per job that has interviews accessed).
    for idx in range(12):
        _make_job_with_interviews(f"PerfCo-{idx}", n_interviews=(idx % 3))
    db.session.commit()

    engine = db.session.get_bind()
    export, n_queries = _count_queries(engine, ai.build_export_dict)

    perf_jobs = [j for j in export["jobs"] if j["company"].startswith("PerfCo-")]
    assert len(perf_jobs) == 12

    # Bounded and small regardless of job count: one query for jobs, one (or a
    # couple, if SQLAlchemy needs to batch the IN-list) for interviews, plus the
    # one-off candidate-user lookup -- nowhere near "one per job".
    assert n_queries <= 6, (
        f"build_export_dict() issued {n_queries} queries for 12 jobs -- "
        "job.interviews is no longer batched (selectin), N+1 regression"
    )


def test_build_export_dict_interviews_still_ordered_by_date(app_context):
    """selectin must still honor Interview's order_by="Interview.interview_date"."""
    job = Job(company="OrderCo", title="Engineer", status="Applied", source="perf-test")
    db.session.add(job)
    db.session.flush()
    db.session.add(Interview(job_id=job.id, round_type="Final",
                              interview_date=date(2026, 3, 1)))
    db.session.add(Interview(job_id=job.id, round_type="Phone Screen",
                              interview_date=date(2026, 1, 1)))
    db.session.add(Interview(job_id=job.id, round_type="Onsite",
                              interview_date=date(2026, 2, 1)))
    db.session.commit()

    export = ai.build_export_dict()
    row = next(j for j in export["jobs"] if j["company"] == "OrderCo")
    rounds = [iv["round"] for iv in row["interviews"]]
    assert rounds == ["Phone Screen", "Onsite", "Final"]
