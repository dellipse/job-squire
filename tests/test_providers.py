# Copyright (C) 2026 D. Brandmeyer
# Licensed under the GNU Affero General Public License v3 or later.
"""Tests for the job-board provider adapters (``app/providers.py``) and the
search orchestration helpers (``app/search.py``).

All HTTP is mocked — no test makes a live network call. Adapters are exercised
by monkeypatching ``providers._request`` (the single choke point every
``search_*`` function calls) with a fake response, then asserting the adapter
yields normalized job dicts with the expected fields.

Search-orchestration coverage focuses on the pieces with real logic: in-batch
and DB-level dedup in ``ingest_jobs``, the per-provider cooldown after a 503,
and the daily run-count bookkeeping. The cooldown/daily-run files are redirected
to a temp path so nothing touches a shared DATA_DIR.
"""
from datetime import datetime, timedelta, timezone

import pytest
import requests

import app.providers as providers


# A complete search config as the orchestration would build it.
CFG = {
    "location": "Las Vegas, NV",
    "radius_miles": 25,
    "results_per_query": 25,
    "max_age_days": 14,
    "min_salary": None,
}


class FakeResp:
    """Minimal stand-in for a requests.Response."""

    def __init__(self, json_data=None, text=""):
        self._json = json_data
        self.text = text

    def json(self):
        return self._json

    def raise_for_status(self):
        return None


def _patch_request(monkeypatch, resp):
    """Make every provider HTTP call return ``resp`` regardless of args."""
    monkeypatch.setattr(providers, "_request", lambda *a, **k: resp)


# ---------------------------------------------------------------------------
# 1. Per-provider response parsing
# ---------------------------------------------------------------------------

DICE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel>
    <item>
      <title>Senior Java Developer</title>
      <link>https://dice.example/job/1</link>
      <guid>dice-guid-1</guid>
      <dc:creator>Dice Co</dc:creator>
      <description>Great &lt;b&gt;Java&lt;/b&gt; role</description>
      <pubDate>Mon, 01 Jun 2026 00:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>"""


PROVIDER_CASES = {
    "adzuna": (
        {"app_id": "a", "app_key": "b"},
        FakeResp(json_data={"results": [{
            "id": 111, "title": "Platform Engineer",
            "company": {"display_name": "Acme"},
            "location": {"display_name": "Las Vegas, NV"},
            "redirect_url": "https://adzuna.example/1",
            "salary_min": 100000, "salary_max": 120000,
            "description": "<p>Build platforms</p>",
            "created": "2026-06-01T00:00:00Z",
        }]}),
        {"external_id": "111", "source": "adzuna", "title": "Platform Engineer",
         "company": "Acme", "url": "https://adzuna.example/1",
         "salary": "$100,000 - $120,000", "description": "Build platforms",
         "date_posted": "2026-06-01"},
    ),
    "jooble": (
        {"key": "k"},
        FakeResp(json_data={"jobs": [{
            "id": 222, "title": "Backend Developer", "company": "Beta LLC",
            "location": "Las Vegas, NV", "link": "https://jooble.example/2",
            "salary": "$90k", "snippet": "Do backend work",
            "updated": "2026-06-02",
        }]}),
        {"external_id": "222", "source": "jooble", "title": "Backend Developer",
         "company": "Beta LLC", "url": "https://jooble.example/2",
         "salary": "$90k", "date_posted": "2026-06-02"},
    ),
    "usajobs": (
        {"email": "seeker@example.com", "api_key": "k"},
        FakeResp(json_data={"SearchResult": {"SearchResultItems": [{
            "MatchedObjectDescriptor": {
                "PositionID": "PD-9", "PositionTitle": "IT Specialist",
                "OrganizationName": "Dept of Example",
                "PositionLocationDisplay": "Las Vegas, NV",
                "PositionURI": "https://usajobs.example/9",
                "PositionRemuneration": [
                    {"MinimumRange": "80000", "MaximumRange": "100000"}],
                "QualificationSummary": "Federal IT role",
                "PublicationStartDate": "2026-05-15",
            }}]}}),
        {"external_id": "PD-9", "source": "usajobs", "title": "IT Specialist",
         "company": "Dept of Example", "url": "https://usajobs.example/9",
         "salary": "$80,000 - $100,000", "date_posted": "2026-05-15"},
    ),
    "ziprecruiter": (
        {"api_key": "k"},
        FakeResp(json_data={"jobs": [{
            "id": "zip-3", "name": "Site Reliability Engineer",
            "hiring_company": {"name": "Zip Co"}, "location": "Las Vegas, NV",
            "url": "https://zip.example/3", "salary_min": 130000,
            "salary_max": 160000, "snippet": "Keep things up",
            "posted_time": "2026-06-03",
        }]}),
        {"external_id": "zip-3", "source": "ziprecruiter",
         "title": "Site Reliability Engineer", "company": "Zip Co",
         "url": "https://zip.example/3", "salary": "$130,000 - $160,000",
         "date_posted": "2026-06-03"},
    ),
    "googlejobs": (
        {"api_key": "k"},
        FakeResp(json_data={"jobs_results": [{
            "job_id": "g-42", "title": "DevOps Engineer",
            "company_name": "Goog Aggregate", "location": "Las Vegas, NV",
            "apply_options": [{"link": "https://apply.example/42"}],
            "description": "Cloud work",
            "detected_extensions": {"posted_at": "2 days ago"},
        }], "serpapi_pagination": {}}),
        {"external_id": "g-42", "source": "googlejobs", "title": "DevOps Engineer",
         "company": "Goog Aggregate", "url": "https://apply.example/42"},
    ),
    "jobicy": (
        {},
        FakeResp(json_data={"jobs": [{
            "id": "jb-7", "jobTitle": "Remote Python Engineer",
            "companyName": "Jobicy Co", "url": "https://jobicy.example/7",
            "annualSalaryMin": 95000, "annualSalaryMax": 115000,
            "jobExcerpt": "Remote python", "pubDate": "2026-06-04",
        }]}),
        {"external_id": "jb-7", "source": "jobicy",
         "title": "Remote Python Engineer", "company": "Jobicy Co",
         "url": "https://jobicy.example/7", "location": "Remote",
         "salary": "$95,000 - $115,000", "date_posted": "2026-06-04"},
    ),
    "dice": (
        {},
        FakeResp(text=DICE_RSS),
        {"external_id": "dice-guid-1", "source": "dice",
         "title": "Senior Java Developer", "company": "Dice Co",
         "url": "https://dice.example/job/1", "date_posted": "2026-06-01"},
    ),
}


@pytest.mark.parametrize("provider", list(PROVIDER_CASES))
def test_provider_parses_response(provider, monkeypatch):
    creds, resp, expected = PROVIDER_CASES[provider]
    _patch_request(monkeypatch, resp)

    results, err = providers.search_provider(provider, creds, ["engineer"], CFG)

    assert err is None
    assert len(results) == 1
    job = results[0]
    for key, value in expected.items():
        assert job[key] == value, f"{provider}: {key} = {job[key]!r}, expected {value!r}"
    # Every adapter must emit the full normalized shape.
    for field in ("external_id", "source", "title", "company", "location",
                  "url", "salary", "description", "date_posted"):
        assert field in job


def test_themuse_filters_by_title(monkeypatch):
    """The Muse has no keyword param, so titles are filtered client-side."""
    resp = FakeResp(json_data={"results": [
        {"id": 1, "name": "Senior Platform Engineer",
         "company": {"name": "Muse Co"}, "locations": [{"name": "Las Vegas, NV"}],
         "refs": {"landing_page": "https://muse.example/1"},
         "contents": "Engineer role", "publication_date": "2026-06-05"},
        {"id": 2, "name": "Marketing Manager",
         "company": {"name": "Muse Co"}, "locations": [{"name": "Las Vegas, NV"}],
         "refs": {"landing_page": "https://muse.example/2"},
         "contents": "Marketing role", "publication_date": "2026-06-05"},
    ], "page_count": 1})
    _patch_request(monkeypatch, resp)

    results, err = providers.search_provider("themuse", {}, ["engineer"], CFG)

    assert err is None
    # Only the title containing "engineer" survives the client-side filter.
    assert [j["title"] for j in results] == ["Senior Platform Engineer"]
    assert results[0]["external_id"] == "1"
    assert results[0]["source"] == "themuse"


# ---------------------------------------------------------------------------
# 2. Missing-credential handling
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("provider,creds", [
    ("adzuna", {}),
    ("adzuna", {"app_id": "only-id"}),          # app_key still missing
    ("ziprecruiter", {}),
    ("jooble", {}),
    ("usajobs", {"email": "e@x.com"}),          # api_key missing
    ("googlejobs", {}),
])
def test_missing_credentials_skips_provider(provider, creds, monkeypatch):
    # If a network call were made this would raise, proving none happens.
    def explode(*a, **k):
        raise AssertionError("no HTTP call should be made when creds are missing")
    monkeypatch.setattr(providers, "_request", explode)

    results, err = providers.search_provider(provider, creds, ["engineer"], CFG)

    assert results == []
    assert err is not None
    assert "missing credential" in err


def test_keyless_providers_need_no_credentials(monkeypatch):
    """dice/jobicy/themuse declare no required fields, so empty creds are fine."""
    _patch_request(monkeypatch, FakeResp(json_data={"jobs": []}))
    for provider in ("jobicy",):
        results, err = providers.search_provider(provider, {}, ["engineer"], CFG)
        assert err is None
        assert results == []


def test_unknown_provider_returns_error(monkeypatch):
    results, err = providers.search_provider("notaprovider", {}, ["x"], CFG)
    assert results == []
    assert "unknown provider" in err


# ---------------------------------------------------------------------------
# 3. HTTP errors are surfaced (503 is the cooldown trigger in search.py)
# ---------------------------------------------------------------------------

def _http_error(status_code):
    resp = requests.models.Response()
    resp.status_code = status_code
    resp._content = b""
    return requests.HTTPError(response=resp)


def test_503_surfaces_as_error(monkeypatch):
    def boom(*a, **k):
        raise _http_error(503)
    monkeypatch.setattr(providers, "_request", boom)

    results, err = providers.search_provider(
        "adzuna", {"app_id": "a", "app_key": "b"}, ["engineer"], CFG)

    assert results == []
    # search.py keys cooldown off the substring "503" (or "service unavailable").
    assert "503" in err


def test_401_surfaces_as_error(monkeypatch):
    def boom(*a, **k):
        raise _http_error(401)
    monkeypatch.setattr(providers, "_request", boom)

    results, err = providers.search_provider(
        "ziprecruiter", {"api_key": "bad"}, ["engineer"], CFG)

    assert results == []
    assert "401" in err


def test_one_provider_error_does_not_raise(monkeypatch):
    """A provider raising an unexpected error is caught, not propagated."""
    def boom(*a, **k):
        raise ValueError("kaboom")
    monkeypatch.setattr(providers, "_request", boom)

    results, err = providers.search_provider(
        "adzuna", {"app_id": "a", "app_key": "b"}, ["engineer"], CFG)

    assert results == []
    assert err is not None


# ---------------------------------------------------------------------------
# 4. Dedup in ingest_jobs (needs an app context for the DB)
# ---------------------------------------------------------------------------

def test_ingest_dedup_within_batch(app_context):
    from app.search import ingest_jobs
    items = [
        {"title": "Dev", "company": "AcmeBatch", "source": "adzuna", "external_id": "1"},
        {"title": "Dev", "company": "AcmeBatch", "source": "adzuna", "external_id": "1"},
    ]
    created, skipped = ingest_jobs(items)
    assert len(created) == 1
    assert skipped == 1


def test_ingest_dedup_across_runs(app_context):
    from app.search import ingest_jobs
    items = [{"title": "Eng", "company": "BetaRuns", "source": "jooble",
              "external_id": "9"}]
    created_first, skipped_first = ingest_jobs(items)
    assert len(created_first) == 1
    assert skipped_first == 0

    created_second, skipped_second = ingest_jobs(items)
    assert len(created_second) == 0
    assert skipped_second == 1


def test_ingest_dedup_company_title_without_external_id(app_context):
    from app.search import ingest_jobs
    items = [
        {"title": "QA Analyst", "company": "GammaCT", "source": "dice"},
        # Same company/title, different casing, no external_id -> normalized dup.
        {"title": "qa analyst", "company": "gammact", "source": "dice"},
    ]
    created, skipped = ingest_jobs(items)
    assert len(created) == 1
    assert skipped == 1


def test_ingest_skips_incomplete_rows(app_context):
    from app.search import ingest_jobs
    items = [
        {"title": "", "company": "NoTitle", "source": "adzuna"},
        {"title": "NoCompany", "company": "", "source": "adzuna"},
    ]
    created, skipped = ingest_jobs(items)
    assert created == []
    assert skipped == 2


# ---------------------------------------------------------------------------
# 5. Cooldown + daily-run bookkeeping in search.py
# ---------------------------------------------------------------------------

@pytest.fixture
def search_mod(tmp_path, monkeypatch):
    """Isolate the cooldown / daily-run JSON files to a temp path."""
    from app import search
    monkeypatch.setattr(search, "_COOLDOWN_FILE", tmp_path / "cooldowns.json")
    monkeypatch.setattr(search, "_DAILY_RUNS_FILE", tmp_path / "daily_runs.json")
    return search


def test_cooldown_set_active_and_persisted(search_mod):
    cooldowns = search_mod._load_cooldowns()
    assert cooldowns == {}

    search_mod._set_cooldown(cooldowns, "adzuna")
    assert search_mod._in_cooldown(cooldowns, "adzuna") is True

    # Survives a save/load round trip.
    search_mod._save_cooldowns(cooldowns)
    reloaded = search_mod._load_cooldowns()
    assert search_mod._in_cooldown(reloaded, "adzuna") is True


def test_cooldown_lapses(search_mod):
    past = (datetime.now(timezone.utc).replace(tzinfo=None)
            - timedelta(hours=1)).isoformat(timespec="seconds")
    cooldowns = {"jooble": past}
    # Past timestamp -> no longer in cooldown; unknown provider -> never in cooldown.
    assert search_mod._in_cooldown(cooldowns, "jooble") is False
    assert search_mod._in_cooldown(cooldowns, "usajobs") is False


def test_daily_run_counts(search_mod):
    counts = {}
    assert search_mod._provider_runs_today(counts, "googlejobs") == 0

    search_mod._increment_provider_runs(counts, "googlejobs")
    assert search_mod._provider_runs_today(counts, "googlejobs") == 1

    search_mod._increment_provider_runs(counts, "googlejobs")
    assert search_mod._provider_runs_today(counts, "googlejobs") == 2

    # A stale entry from a previous day reads as zero for today.
    counts["adzuna"] = {"date": "2000-01-01", "count": 5}
    assert search_mod._provider_runs_today(counts, "adzuna") == 0
