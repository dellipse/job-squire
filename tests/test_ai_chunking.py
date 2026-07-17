# Copyright (C) 2026 D. Brandmeyer
# Licensed under the GNU Affero General Public License v3 or later.
"""Tests for prompt chunking / map-reduce when a prompt doesn't fit the
configured provider's context window (docs/PLAN-ollama-assist.md).

Design recap:
  - The full single-shot prompt is always tried FIRST. Chunking only kicks in
    on `ai.ContextCapacityError` — i.e. only when the configured provider
    genuinely can't fit the prompt, never as a default. This preserves
    single-pass analysis quality whenever the provider has room for it.
  - Triage / follow-up drafts batch independent jobs, so a capacity failure
    just means "shrink the batch and retry" — no reassembly needed beyond
    what the batch loop already does (`_call_batched_with_capacity_shrink`).
  - Weekly review / rejection analysis are single aggregate prompts over the
    whole pipeline, so a capacity failure means real map-reduce: split into
    chunks, analyze each chunk, then synthesize the partials into one result
    (`_run_chunked_or_single` + `_reduce_partial_analyses`). Whenever this
    path is taken, it must be visible — not just in logs (unattended worker
    runs) but in the returned overall_summary itself, since a chunked
    analysis is a real quality tradeoff the reader should know about.
"""
import json
import re

import pytest

from app import ai
from app.extensions import db
from app.models import Job


# ---------------------------------------------------------------------------
# _call_batched_with_capacity_shrink — pure, no DB needed
# ---------------------------------------------------------------------------

def test_batch_shrink_no_shrink_needed_when_it_fits():
    calls = []

    def build(items):
        calls.append(list(items))
        return f"content for {items}"

    def call(content):
        return "ok:" + content

    def parse(raw):
        return [raw]

    results, unresolved = ai._call_batched_with_capacity_shrink([1, 2, 3], build, call, parse)
    assert results == ["ok:content for [1, 2, 3]"]
    assert unresolved == []
    assert len(calls) == 1  # never split


def test_batch_shrink_splits_and_succeeds_at_smaller_size():
    attempts = []

    def build(items):
        return items

    def call(items):
        attempts.append(list(items))
        if len(items) > 2:
            raise ai.ContextCapacityError("too big")
        return items

    def parse(items):
        return list(items)

    results, unresolved = ai._call_batched_with_capacity_shrink([1, 2, 3, 4, 5], build, call, parse)
    assert sorted(results) == [1, 2, 3, 4, 5]
    assert unresolved == []
    # First attempt was the full batch (and failed), so more than one call happened,
    # and every attempt that actually succeeded was at or under the size that fits.
    assert len(attempts) > 1
    assert attempts[0] == [1, 2, 3, 4, 5]
    successful_sizes = [len(a) for a in attempts if len(a) <= 2]
    assert successful_sizes  # at least one successful sub-batch occurred


def test_batch_shrink_gives_up_at_min_chunk_and_reports_unresolved():
    def build(items):
        return items

    def call(items):
        raise ai.ContextCapacityError("never fits")

    def parse(items):
        return list(items)

    results, unresolved = ai._call_batched_with_capacity_shrink(
        [1, 2, 3], build, call, parse, min_chunk=1)
    assert results == []
    assert sorted(unresolved) == [1, 2, 3]


def test_batch_shrink_non_capacity_exception_propagates_without_shrinking():
    def build(items):
        return items

    def call(items):
        raise ValueError("network blew up")

    def parse(items):
        return list(items)

    with pytest.raises(ValueError, match="network blew up"):
        ai._call_batched_with_capacity_shrink([1, 2, 3], build, call, parse)


# ---------------------------------------------------------------------------
# _run_chunked_or_single — pure, no DB needed
# ---------------------------------------------------------------------------

def test_chunked_or_single_no_chunking_when_it_fits():
    def build(items):
        return json.dumps({"overall_summary": f"n={len(items)}", "recommendations": []})

    def call(content):
        return content

    partials = ai._run_chunked_or_single([1, 2, 3], build, call)
    assert len(partials) == 1
    assert partials[0]["overall_summary"] == "n=3"


def test_chunked_or_single_splits_on_capacity_error():
    attempts = []

    def build(items):
        return items

    def call(items):
        attempts.append(list(items))
        if len(items) > 2:
            raise ai.ContextCapacityError("too big")
        return json.dumps({"overall_summary": f"chunk:{items}", "recommendations": [f"r{items}"]})

    partials = ai._run_chunked_or_single([1, 2, 3, 4, 5], build, call)
    assert len(partials) > 1
    # Every original item shows up in exactly one partial's chunk label.
    all_items_seen = []
    for p in partials:
        nums = [int(x) for x in re.findall(r"\d+", p["overall_summary"])]
        all_items_seen.extend(nums)
    assert sorted(all_items_seen) == [1, 2, 3, 4, 5]


def test_chunked_or_single_reraises_when_even_min_chunk_fails():
    def build(items):
        return items

    def call(items):
        raise ai.ContextCapacityError("never fits, not even solo")

    with pytest.raises(ai.ContextCapacityError):
        ai._run_chunked_or_single([1, 2], build, call, min_chunk=1)


# ---------------------------------------------------------------------------
# _reduce_partial_analyses — monkeypatches ai._call_with_thinking directly
# ---------------------------------------------------------------------------

def test_reduce_partial_analyses_synthesizes_and_calls_through(monkeypatch):
    seen = {}

    def fake_call_with_thinking(system, content, max_tokens, task_name=None):
        seen["content"] = content
        seen["task_name"] = task_name
        return json.dumps({"overall_summary": "combined", "recommendations": ["merged rec"]})

    monkeypatch.setattr(ai, "_call_with_thinking", fake_call_with_thinking)

    partials = [
        {"overall_summary": "chunk one notes", "recommendations": ["a"]},
        {"overall_summary": "chunk two notes", "recommendations": ["b"]},
    ]
    result = ai._reduce_partial_analyses(partials, "weekly strategy review", "weekly_review", "sys")
    assert result == {"overall_summary": "combined", "recommendations": ["merged rec"]}
    assert seen["task_name"] == "weekly_review"
    assert "chunk one notes" in seen["content"]
    assert "chunk two notes" in seen["content"]
    assert "2 separate passes" in seen["content"]


# ---------------------------------------------------------------------------
# Integration: run_auto_triage batch-shrinks on capacity error, no data lost
# ---------------------------------------------------------------------------

def _fake_call_no_thinking_with_cap(max_ok: int):
    """A fake ai._call_no_thinking that fails whenever the prompt's JOBS TO
    SCORE section has more than `max_ok` job ids, and otherwise returns a
    plausible triage response for every id present."""
    calls = []

    def fake(system, content, max_tokens, api_key="", model="",
              use_triage_model=False, task_name=None):
        calls.append(content)
        ids = [int(m) for m in re.findall(r'"id":\s*(\d+)', content)]
        if len(ids) > max_ok:
            raise ai.ContextCapacityError(f"{len(ids)} jobs too many for this window")
        return json.dumps([{"id": i, "score": 7, "reason": "fits fine"} for i in ids])

    fake.calls = calls
    return fake


def test_run_auto_triage_shrinks_batch_on_capacity_error(app_context, monkeypatch):
    jobs = [
        Job(title=f"Role {i}", company=f"Co {i}", status="Saved", created_by="test")
        for i in range(7)
    ]
    db.session.add_all(jobs)
    db.session.commit()

    fake = _fake_call_no_thinking_with_cap(max_ok=3)
    monkeypatch.setattr(ai, "_call_no_thinking", fake)

    result = ai.run_auto_triage()

    assert result["scored"] == 7
    assert result["failed"] == 0
    assert len(fake.calls) > 1  # proves it had to split at least once

    for j in jobs:
        db.session.refresh(j)
        assert j.ai_fit_score == 7

    for j in jobs:
        db.session.delete(j)
    db.session.commit()


# ---------------------------------------------------------------------------
# Integration: run_weekly_review / run_rejection_analysis map-reduce path
# ---------------------------------------------------------------------------

def _fake_call_with_thinking_chunked(chunk_marker: str, reduce_marker: str,
                                      chunk_summary="partial", reduce_summary="combined summary",
                                      reduce_recs=("final rec",)):
    calls = []

    def fake(system, content, max_tokens, task_name=None):
        calls.append(content)
        if reduce_marker in content:
            return json.dumps({"overall_summary": reduce_summary,
                                "recommendations": list(reduce_recs)})
        if chunk_marker in content:
            return json.dumps({"overall_summary": chunk_summary, "recommendations": ["partial rec"]})
        # Full single-shot attempt (no marker) — always too big in this fake,
        # forcing the chunked path so we can exercise it deterministically.
        raise ai.ContextCapacityError("full pipeline too large for this window")

    fake.calls = calls
    return fake


def test_run_weekly_review_falls_back_to_chunked_analysis_and_warns(app_context, monkeypatch):
    jobs = [
        Job(title=f"Role {i}", company=f"Co {i}", status="Applied", created_by="test")
        for i in range(4)
    ]
    db.session.add_all(jobs)
    db.session.commit()

    fake = _fake_call_with_thinking_chunked(
        chunk_marker="one chunk of a larger pipeline",
        reduce_marker="separate passes",
    )
    monkeypatch.setattr(ai, "_call_with_thinking", fake)

    result = ai.run_weekly_review()

    assert result["overall_summary"].startswith("[Note:")
    assert "combined summary" in result["overall_summary"]
    assert result["recommendations"] == ["final rec"]
    assert len(fake.calls) > 2  # full attempt + at least 2 chunks + 1 reduce

    for j in jobs:
        db.session.delete(j)
    db.session.commit()


def test_run_weekly_review_uses_single_pass_when_it_fits(app_context, monkeypatch):
    """Regression guard: when the configured provider CAN fit the full
    pipeline, the higher-quality single-pass analysis is used as-is — no
    chunking note, no map-reduce overhead."""
    jobs = [Job(title="Role", company="Co", status="Applied", created_by="test")]
    db.session.add_all(jobs)
    db.session.commit()

    def fake(system, content, max_tokens, task_name=None):
        return json.dumps({"overall_summary": "clean single-pass review", "recommendations": ["r1"]})

    monkeypatch.setattr(ai, "_call_with_thinking", fake)

    result = ai.run_weekly_review()
    assert result["overall_summary"] == "clean single-pass review"
    assert not result["overall_summary"].startswith("[Note:")

    for j in jobs:
        db.session.delete(j)
    db.session.commit()


def test_run_rejection_analysis_falls_back_to_chunked_analysis_and_warns(app_context, monkeypatch):
    rejected = [
        Job(title=f"Rejected {i}", company=f"Co {i}", status="Rejected", created_by="test")
        for i in range(4)
    ]
    active = [Job(title="Active", company="Co", status="Applied", created_by="test")]
    db.session.add_all(rejected + active)
    db.session.commit()

    fake = _fake_call_with_thinking_chunked(
        chunk_marker="one chunk of a larger set",
        reduce_marker="separate passes",
    )
    monkeypatch.setattr(ai, "_call_with_thinking", fake)

    result = ai.run_rejection_analysis()

    assert result["overall_summary"].startswith("[Note:")
    assert "combined summary" in result["overall_summary"]
    assert len(fake.calls) > 2

    for j in rejected + active:
        db.session.delete(j)
    db.session.commit()
