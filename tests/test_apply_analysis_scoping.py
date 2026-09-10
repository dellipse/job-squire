# Copyright (C) 2026 D. Brandmeyer
# Licensed under the GNU Affero General Public License v3 or later.
"""Regression tests for SEC-10 (2026-09-08 audit, Long-term item 20).

apply_analysis() writes a model-returned {id, analysis} to any job id in
the DB. Job descriptions arrive from untrusted sources (search ingest,
/api/ingest, MCP add_jobs) and are replayed verbatim into the analysis
prompt -- an injected instruction in one job's description could get the
model to name a *different* job's id and have fabricated analysis written
there, with no human review in the automated (API-mode "Analyze now")
path. run_triage_batch._apply already correctly scopes writeback ids to
the batch it actually sent to the model; apply_analysis did not.

The fix: an optional allowed_job_ids parameter, mirroring
run_triage_batch._apply's job_map pattern. run_api_analysis() -- the
automated, no-human-review call path -- now passes the exact set of ids
it exported to the model in that call. The manual-import path (ai_hub(),
where an admin pastes an AI response back by hand) and the MCP
save_analysis tool intentionally keep allowed_job_ids=None (unrestricted,
unchanged behavior) since neither has a well-defined per-call export set
to scope against -- see app/ai.py's apply_analysis docstring.
"""
from app.ai import apply_analysis
from app.extensions import db
from app.models import AIInsight, Job


def _make_jobs(app_context, n=3):
    jobs = [Job(company=f"Co{i}", title=f"Title{i}", status="Saved") for i in range(n)]
    db.session.add_all(jobs)
    db.session.commit()
    return jobs


def test_apply_analysis_refuses_an_id_outside_the_allowed_set(app_context):
    """The core regression: a parsed response naming a job id that was
    never part of this call's exported set must not be applied, even
    though that job genuinely exists in the DB -- exactly the shape of an
    injection in one job's description naming an unrelated job's id."""
    victim, attacker_adjacent, untouched = _make_jobs(app_context)

    # Only `victim` was actually exported/sent to the model in this call.
    allowed_ids = {victim.id}

    # The model's response (as if steered by an injected instruction)
    # targets a DIFFERENT job -- one that exists, but was never part of
    # what this call exported.
    parsed = {
        "overall_summary": "",
        "recommendations": [],
        "jobs": [
            {"id": attacker_adjacent.id, "analysis": "INJECTED: this company is a scam, withdraw immediately"},
        ],
    }

    updated, missing = apply_analysis(parsed, created_by="test", allowed_job_ids=allowed_ids)

    assert updated == 0
    assert missing == 1
    db.session.refresh(attacker_adjacent)
    assert not attacker_adjacent.ai_analysis, "analysis must not be written to an out-of-scope job"


def test_apply_analysis_accepts_an_id_inside_the_allowed_set(app_context):
    (job,) = _make_jobs(app_context, n=1)
    parsed = {"jobs": [{"id": job.id, "analysis": "Strong fit, apply soon."}]}

    updated, missing = apply_analysis(parsed, created_by="test", allowed_job_ids={job.id})

    assert updated == 1
    assert missing == 0
    db.session.refresh(job)
    assert job.ai_analysis == "Strong fit, apply soon."


def test_apply_analysis_still_rejects_a_nonexistent_id_when_scoped(app_context):
    (job,) = _make_jobs(app_context, n=1)
    parsed = {"jobs": [{"id": 999999, "analysis": "should never land"}]}

    updated, missing = apply_analysis(parsed, created_by="test", allowed_job_ids={job.id})

    assert updated == 0
    assert missing == 1


def test_apply_analysis_unscoped_call_keeps_prior_unrestricted_behavior(app_context):
    """The manual-import path (ai_hub()) and the MCP save_analysis tool
    call apply_analysis without allowed_job_ids -- omitting it must still
    behave exactly as it always has (any existing job id is accepted),
    so this fix doesn't silently break either of those call patterns."""
    job, other = _make_jobs(app_context, n=2)
    parsed = {"jobs": [
        {"id": job.id, "analysis": "A"},
        {"id": other.id, "analysis": "B"},
    ]}

    updated, missing = apply_analysis(parsed, created_by="test")

    assert updated == 2
    assert missing == 0
    db.session.refresh(job)
    db.session.refresh(other)
    assert job.ai_analysis == "A"
    assert other.ai_analysis == "B"


def test_apply_analysis_still_writes_the_global_insight_when_scoped(app_context):
    """Scoping ids must only gate per-job writeback -- the overall_summary/
    recommendations AIInsight row (not tied to any single job id) is
    unaffected."""
    (job,) = _make_jobs(app_context, n=1)
    other_job_id = job.id + 1  # not in allowed set, and may not even exist
    parsed = {
        "overall_summary": "Pipeline looks healthy.",
        "recommendations": ["Follow up on stale leads."],
        "jobs": [{"id": other_job_id, "analysis": "should be rejected"}],
    }

    updated, missing = apply_analysis(parsed, created_by="test", allowed_job_ids={job.id})

    assert updated == 0
    assert missing == 1
    insight = AIInsight.query.order_by(AIInsight.created_at.desc()).first()
    assert insight is not None
    assert insight.summary == "Pipeline looks healthy."
