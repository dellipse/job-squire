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
"""Job-search provider adapters.

Each adapter turns a provider's API response into a list of normalized dicts:

    {
      "external_id": str,      # provider's stable id (used for dedup)
      "source": str,           # provider key, e.g. "adzuna"
      "title": str,
      "company": str,
      "location": str,
      "url": str,
      "salary": str,           # display string, may be ""
      "description": str,      # short snippet, may be ""
      "date_posted": str|None, # YYYY-MM-DD if known
    }

Adding a provider: add an entry to PROVIDERS and a `search_*` function, then map it
in `search_provider`.
"""
import html
import os
import random
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime as _rfc2822_parse

import requests

TIMEOUT = 20
# 503 is intentionally excluded: Adzuna's 503 is a CDN-level outage that lasts
# minutes, not milliseconds. Fast-failing lets search.py put the provider in
# cooldown so the next scheduled run skips it instead of blocking here.
RETRY_CODES = {429, 502, 504}
RETRIES = 3  # total attempts (was effectively 1 retry; now retries twice)
BACKOFF = 2  # seconds, doubled each attempt (stays well under a minute)
# Pause between consecutive API calls to stay under providers' per-minute limits.
# A small random jitter is added on top. Configurable via env.
THROTTLE_SECONDS = float(os.environ.get("SEARCH_THROTTLE_SECONDS", "60"))


def _request(method, url, **kwargs):
    """HTTP call with retry/backoff (plus jitter) on transient errors.

    503 is not retried here — it signals a provider outage lasting minutes, not
    milliseconds. search.py catches it and puts the provider in cooldown instead.
    """
    last = None
    for attempt in range(RETRIES):
        try:
            r = requests.request(method, url, timeout=TIMEOUT, **kwargs)
        except requests.RequestException:
            # Network-level transient failure (connection reset, timeout): back
            # off and retry, or re-raise on the final attempt.
            if attempt < RETRIES - 1:
                time.sleep(BACKOFF * (2 ** attempt) + random.uniform(0, 1))
                continue
            raise
        if r.status_code not in RETRY_CODES:
            r.raise_for_status()
            return r
        last = r
        if attempt < RETRIES - 1:
            time.sleep(BACKOFF * (2 ** attempt) + random.uniform(0, 1))
    last.raise_for_status()  # exhausted retries; raise the last transient error
    return last


# UI metadata: what each provider needs and where to sign up.
PROVIDERS = {
    "dice": {
        "label": "Dice",
        "signup_url": "https://www.dice.com/",
        "note": "No API key required. Uses Dice's public RSS feed. "
                "Tech-focused board — strong for IT, software, and engineering roles.",
        "fields": [],
    },
    "ziprecruiter": {
        "label": "ZipRecruiter",
        "signup_url": "https://www.ziprecruiter.com/partner",
        "note": "Free partner API key required. Apply at ziprecruiter.com/partner. "
                "Strong in employer-direct US postings.",
        "fields": [
            {"name": "api_key", "label": "API Key", "secret": True, "required": True},
        ],
    },
    "googlejobs": {
        "label": "Google Jobs (SerpApi)",
        "signup_url": "https://serpapi.com/users/sign_up",
        "note": "Free tier: 250 searches/month. Register at serpapi.com. "
                "Aggregates listings from Indeed, LinkedIn, ZipRecruiter, Workday, and hundreds "
                "of other boards — broadest cross-board coverage available. "
                "Each page of 10 results costs 1 credit (~3 credits per title per run). "
                "Use the limits below to stay within your monthly quota.",
        "fields": [
            {"name": "api_key", "label": "API Key", "secret": True, "required": True},
            {"name": "max_runs_per_day", "label": "Max runs/day",
             "secret": False, "required": False, "input_type": "number", "placeholder": "1"},
            {"name": "max_titles_per_run", "label": "Max titles/run",
             "secret": False, "required": False, "input_type": "number", "placeholder": ""},
        ],
    },
    "adzuna": {
        "label": "Adzuna",
        "signup_url": "https://developer.adzuna.com/",
        "note": "Free API. Broad US aggregator with wide national coverage. Keyword + radius search.",
        "fields": [
            {"name": "app_id", "label": "App ID", "secret": False, "required": True},
            {"name": "app_key", "label": "App Key", "secret": True, "required": True},
        ],
    },
    "jooble": {
        "label": "Jooble",
        "signup_url": "https://jooble.org/api/about",
        "note": "Free API key by email. Aggregates many boards. Keyword + location.",
        "fields": [
            {"name": "key", "label": "API Key", "secret": True, "required": True},
        ],
    },
    "themuse": {
        "label": "The Muse",
        "signup_url": "https://www.themuse.com/developers/api/v2",
        "note": "Free, API key optional. Filters by location; titles matched on this end.",
        "fields": [
            {"name": "api_key", "label": "API Key (optional)", "secret": True, "required": False},
        ],
    },
    "usajobs": {
        "label": "USAJOBS (federal)",
        "signup_url": "https://developer.usajobs.gov/APIRequest/",
        "note": "Free. Federal jobs only. Useful for government-adjacent roles in the Vegas area.",
        "fields": [
            {"name": "email", "label": "Registered email", "secret": False, "required": True},
            {"name": "api_key", "label": "Authorization Key", "secret": True, "required": True},
        ],
    },
    "jobicy": {
        "label": "Jobicy",
        "signup_url": "https://jobicy.com/",
        "note": "No API key required. Free public JSON API. "
                "Limitation: remote jobs only — location and radius settings are ignored. "
                "Best as a supplement for candidates open to remote work.",
        "fields": [],
    },
}


def _clean(text, limit=5000):
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _fmt_money(lo, hi):
    def f(v):
        try:
            return f"${int(float(v)):,}"
        except (TypeError, ValueError):
            return None
    a, b = f(lo), f(hi)
    if a and b and a != b:
        return f"{a} - {b}"
    return a or b or ""


def _iso_date(value):
    """Parse any ISO-8601-ish date string and return 'YYYY-MM-DD', or None."""
    if not value:
        return None
    value = str(value).strip()
    # Fast path: already just a date.
    if len(value) == 10 and value[4:5] == "-" and value[7:8] == "-":
        return value
    # Try common datetime formats in descending specificity.
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(value[:len(fmt)], fmt).strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            continue
    # Last resort: first 10 chars if they look date-shaped.
    if len(value) >= 10 and value[4:5] == "-" and value[7:8] == "-":
        return value[:10]
    return None


# --------------------------------------------------------------------------
def search_adzuna(creds, title, cfg):
    params = {
        "app_id": creds.get("app_id", ""),
        "app_key": creds.get("app_key", ""),
        "what": title,
        "where": cfg["location"],
        "distance": cfg["radius_miles"],
        "results_per_page": cfg["results_per_query"],
        "max_days_old": cfg["max_age_days"],
        "content-type": "application/json",
    }
    if cfg.get("min_salary"):
        params["salary_min"] = cfg["min_salary"]
    r = _request("GET", "https://api.adzuna.com/v1/api/jobs/us/search/1",
                 params=params)
    out = []
    for j in r.json().get("results", []):
        out.append({
            "external_id": str(j.get("id", "")),
            "source": "adzuna",
            "title": j.get("title", ""),
            "company": (j.get("company") or {}).get("display_name", ""),
            "location": (j.get("location") or {}).get("display_name", ""),
            "url": j.get("redirect_url", ""),
            "salary": _fmt_money(j.get("salary_min"), j.get("salary_max")),
            "description": _clean(j.get("description")),
            "date_posted": _iso_date(j.get("created")),
        })
    return out


def search_jooble(creds, title, cfg):
    url = f"https://jooble.org/api/{creds.get('key', '')}"
    payload = {"keywords": title, "location": cfg["location"],
               "radius": str(cfg["radius_miles"])}
    r = _request("POST", url, json=payload)
    out = []
    for j in r.json().get("jobs", []):
        out.append({
            "external_id": str(j.get("id", "")) or j.get("link", ""),
            "source": "jooble",
            "title": j.get("title", ""),
            "company": j.get("company", ""),
            "location": j.get("location", ""),
            "url": j.get("link", ""),
            "salary": j.get("salary", "") or "",
            "description": _clean(j.get("snippet")),
            "date_posted": _iso_date(j.get("updated")),
        })
    return out


def search_usajobs(creds, title, cfg):
    headers = {
        "Host": "data.usajobs.gov",
        # USAJOBS requires the User-Agent to be the email registered with the key.
        "User-Agent": creds.get("email", ""),
        "Authorization-Key": creds.get("api_key", ""),
    }
    params = {
        "Keyword": title,
        "LocationName": cfg["location"],
        "Radius": cfg["radius_miles"],
        "ResultsPerPage": cfg["results_per_query"],
    }
    r = _request("GET", "https://data.usajobs.gov/api/search",
                 headers=headers, params=params)
    items = r.json().get("SearchResult", {}).get("SearchResultItems", [])
    out = []
    for it in items:
        d = it.get("MatchedObjectDescriptor", {})
        rem = (d.get("PositionRemuneration") or [{}])[0]
        out.append({
            "external_id": str(d.get("PositionID", "")),
            "source": "usajobs",
            "title": d.get("PositionTitle", ""),
            "company": d.get("OrganizationName", ""),
            "location": d.get("PositionLocationDisplay", ""),
            "url": d.get("PositionURI", ""),
            "salary": _fmt_money(rem.get("MinimumRange"), rem.get("MaximumRange")),
            "description": _clean(d.get("QualificationSummary") or d.get("UserArea", {})
                                  .get("Details", {}).get("JobSummary")),
            "date_posted": _iso_date(d.get("PublicationStartDate")),
        })
    return out


def search_themuse(creds, titles, cfg):
    """The Muse has no keyword param, so fetch by location and filter titles here."""
    out = []
    needles = [t.lower() for t in titles]
    for page in range(1, 3):  # two pages is plenty for one metro
        params = {"location": cfg["location"], "page": page}
        if creds.get("api_key"):
            params["api_key"] = creds["api_key"]
        r = _request("GET", "https://www.themuse.com/api/public/jobs",
                     params=params)
        body = r.json()
        for j in body.get("results", []):
            name = (j.get("name") or "")
            if needles and not any(n in name.lower() for n in needles):
                continue
            locs = ", ".join(loc.get("name", "") for loc in j.get("locations", []))
            out.append({
                "external_id": str(j.get("id", "")),
                "source": "themuse",
                "title": name,
                "company": (j.get("company") or {}).get("name", ""),
                "location": locs,
                "url": (j.get("refs") or {}).get("landing_page", ""),
                "salary": "",
                "description": _clean(j.get("contents")),
                "date_posted": _iso_date(j.get("publication_date")),
            })
        if page >= body.get("page_count", 1):
            break
    return out


_RSS_UA = "Mozilla/5.0 (compatible; JobSquire/1.0; +https://github.com/dellipse/job-squire)"


def _parse_rfc2822(date_str):
    """Parse an RFC 2822 pubDate string to 'YYYY-MM-DD', or None."""
    if not date_str:
        return None
    try:
        return _rfc2822_parse(date_str.strip()).strftime("%Y-%m-%d")
    except Exception:
        return None


def _rss_items(xml_text):
    """Parse RSS XML text and return a list of <item> Elements."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    return root.findall(".//item")


def _parse_relative_date(posted_at):
    """Convert SerpApi's relative 'posted_at' string to 'YYYY-MM-DD', or None.

    Google Jobs returns strings like '3 days ago', '22 hours ago', 'yesterday',
    'Today', '2 weeks ago' rather than structured dates.
    """
    if not posted_at:
        return None
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    now = _dt.now(_tz.utc)
    s = posted_at.lower().strip()
    if any(w in s for w in ("today", "just now", "hour", "minute")):
        return now.strftime("%Y-%m-%d")
    if "yesterday" in s:
        return (now - _td(days=1)).strftime("%Y-%m-%d")
    m = re.search(r"(\d+)\s+day", s)
    if m:
        return (now - _td(days=int(m.group(1)))).strftime("%Y-%m-%d")
    m = re.search(r"(\d+)\s+week", s)
    if m:
        return (now - _td(weeks=int(m.group(1)))).strftime("%Y-%m-%d")
    m = re.search(r"(\d+)\s+month", s)
    if m:
        return (now - _td(days=int(m.group(1)) * 30)).strftime("%Y-%m-%d")
    return None


def search_googlejobs(creds, title, cfg):
    """Search Google Jobs via SerpApi (API key required).

    SerpApi returns 10 results per page; we paginate with next_page_token until
    we have enough results or run out of pages.  Each page costs 1 SerpApi credit,
    so we cap at ceil(results_per_query / 10) pages.

    Date filtering: Google Jobs does not expose a structured date parameter.  We
    append a natural-language date phrase to the query based on max_age_days, which
    Google's Jobs index honours as a recency filter.
    """
    api_key = creds.get("api_key", "")
    location = cfg["location"]
    # SerpApi's lrad is in kilometres; convert from miles (hint only, not strict).
    radius_km = round(cfg.get("radius_miles", 25) * 1.60934)
    max_age = cfg.get("max_age_days", 14)
    limit = min(cfg.get("results_per_query", 25), 100)
    max_pages = max(1, -(-limit // 10))  # ceil division

    # Append a recency phrase so Google filters results server-side.
    if max_age <= 1:
        date_phrase = " since yesterday"
    elif max_age <= 3:
        date_phrase = " in the last 3 days"
    elif max_age <= 7:
        date_phrase = " in the last week"
    else:
        date_phrase = ""

    params = {
        "engine": "google_jobs",
        "q": f"{title}{date_phrase}",
        "location": location,
        "gl": "us",
        "hl": "en",
        "lrad": radius_km,
        "api_key": api_key,
    }

    out = []
    for page in range(max_pages):
        r = _request("GET", "https://serpapi.com/search", params=params)
        data = r.json()
        for job in data.get("jobs_results", []):
            ext = job.get("detected_extensions") or {}
            apply_opts = job.get("apply_options") or []
            # Prefer the first direct apply link; fall back to the Google share URL.
            url = apply_opts[0].get("link", "") if apply_opts else job.get("share_link", "")
            out.append({
                "external_id": job.get("job_id", ""),
                "source": "googlejobs",
                "title": job.get("title", ""),
                "company": job.get("company_name", "") or "(see posting)",
                "location": job.get("location", location),
                "url": url,
                "salary": "",
                "description": _clean(job.get("description", "")),
                "date_posted": _parse_relative_date(ext.get("posted_at", "")),
            })
            if len(out) >= limit:
                break
        if len(out) >= limit:
            break
        token = (data.get("serpapi_pagination") or {}).get("next_page_token")
        if not token or not data.get("jobs_results"):
            break
        params["next_page_token"] = token
    return out


def search_ziprecruiter(creds, title, cfg):
    """Search ZipRecruiter via the free partner API (API key required)."""
    params = {
        "search": title,
        "location": cfg["location"],
        "radius_miles": cfg["radius_miles"],
        "days_ago": min(cfg.get("max_age_days", 14), 30),
        "jobs_per_page": min(cfg.get("results_per_query", 25), 100),
        "api_key": creds.get("api_key", ""),
    }
    r = _request("GET", "https://api.ziprecruiter.com/jobs/v1", params=params)
    out = []
    for j in r.json().get("jobs", []):
        hiring = j.get("hiring_company") or {}
        company = hiring.get("name", "") if isinstance(hiring, dict) else ""
        out.append({
            "external_id": str(j.get("id", "")),
            "source": "ziprecruiter",
            "title": j.get("name", ""),
            "company": company or "(see posting)",
            "location": j.get("location", ""),
            "url": j.get("url", ""),
            "salary": _fmt_money(j.get("salary_min"), j.get("salary_max")),
            "description": _clean(j.get("snippet", "")),
            "date_posted": _iso_date(j.get("posted_time", "")),
        })
    return out


def search_dice(creds, title, cfg):
    """Search Dice via the public RSS feed (no API key required)."""
    params = {
        "q": title,
        "countryCode": "US",
        "location": cfg["location"],
        "radius": cfg["radius_miles"],
        "radiusUnit": "miles",
        "pageSize": min(cfg.get("results_per_query", 25), 50),
        "datePosted": str(cfg.get("max_age_days", 14)),
    }
    r = _request("GET", "https://www.dice.com/jobs/rss", params=params,
                 headers={"User-Agent": _RSS_UA})
    DC = "{http://purl.org/dc/elements/1.1/}"
    out = []
    for item in _rss_items(r.text):
        link = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or link).strip()
        raw = (item.findtext("title") or "").strip()
        # Dice uses dc:creator for the company name.
        company = (item.findtext(f"{DC}creator") or "").strip()
        if not company:
            parts = raw.split(" - ", 1)
            if len(parts) == 2:
                raw, company = parts[0].strip(), parts[1].strip()
        out.append({
            "external_id": guid or link,
            "source": "dice",
            "title": raw,
            "company": company or "(see posting)",
            "location": cfg["location"],
            "url": link,
            "salary": "",
            "description": _clean(item.findtext("description") or ""),
            "date_posted": _parse_rfc2822(item.findtext("pubDate")),
        })
    return out


def search_jobicy(creds, title, cfg):
    """Search Jobicy via the free public JSON API (no API key required).

    Jobicy is remote-only: location and radius from cfg are not used.
    Results are capped at 50 per call; the API enforces a 6-hour delay on new
    postings (intentional — Jobicy wants to remain the original source).
    """
    params = {
        "count": min(cfg.get("results_per_query", 50), 50),
        "geo": "usa",
        "tag": title,
    }
    r = _request("GET", "https://jobicy.com/api/v2/remote-jobs", params=params)
    out = []
    for j in r.json().get("jobs", []):
        out.append({
            "external_id": str(j.get("id", "")),
            "source": "jobicy",
            "title": j.get("jobTitle", ""),
            "company": j.get("companyName", "") or "(see posting)",
            "location": "Remote",
            "url": j.get("url", ""),
            "salary": j.get("annualSalaryMin") and _fmt_money(
                j.get("annualSalaryMin"), j.get("annualSalaryMax")
            ) or "",
            "description": _clean(j.get("jobExcerpt", "")),
            "date_posted": _iso_date(j.get("pubDate", "")),
        })
    return out


def _missing_required(provider, creds):
    """Return a list of required credential field labels that are blank.

    A common silent failure: SECRET_KEY changed since the keys were saved, so
    decrypt() returns "" and every field is blank. That surfaces downstream as a
    confusing 401/403/503. Catching it here gives an actionable message instead.
    """
    missing = []
    for f in PROVIDERS.get(provider, {}).get("fields", []):
        if f.get("required") and not (creds.get(f["name"]) or "").strip():
            missing.append(f["label"])
    return missing


def search_provider(provider, creds, titles, cfg):
    """Run one provider across all titles. Returns (results, error_or_None)."""
    try:
        missing = _missing_required(provider, creds)
        if missing:
            return [], (f"{provider}: missing credential(s): {', '.join(missing)}. "
                        "Re-enter on the Connections page (if they were saved before, "
                        "SECRET_KEY may have changed, which clears saved keys).")
        if provider == "themuse":
            return search_themuse(creds, titles, cfg), None
        fn = {
            "adzuna": search_adzuna,
            "jooble": search_jooble,
            "usajobs": search_usajobs,
            "ziprecruiter": search_ziprecruiter,
            "googlejobs": search_googlejobs,
            "dice": search_dice,
            "jobicy": search_jobicy,
        }.get(provider)
        if not fn:
            return [], f"unknown provider {provider}"
        results = []
        for i, title in enumerate(titles):
            if i and THROTTLE_SECONDS:
                time.sleep(THROTTLE_SECONDS + random.uniform(0, THROTTLE_SECONDS))
            results.extend(fn(creds, title, cfg))
        return results, None
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        body = ""
        if e.response is not None:
            body = re.sub(r"\s+", " ", (e.response.text or ""))[:200].strip()
        hint = ""
        if code == 401:
            hint = " (auth rejected: check the API key and, for USAJOBS, that the registered email is set)"
        elif code in (403,):
            keyless = not PROVIDERS.get(provider, {}).get("fields")
            if keyless:
                hint = " (forbidden: provider is blocking automated requests — bot protection or rate limit)"
            else:
                hint = " (forbidden: check that the API key and any other credentials are correct)"
        elif code == 429:
            hint = " (rate limited: too many calls, will retry next run)"
        elif code == 503:
            hint = " (service unavailable after retries: provider outage or rate limit)"
        return [], f"HTTP {code} from {provider}{hint}{': ' + body if body else ''}"
    except requests.RequestException as e:
        return [], f"{provider} request failed: {e.__class__.__name__}"
    except Exception as e:  # noqa: BLE001 - never let one provider kill the run
        return [], f"{provider} error: {e.__class__.__name__}"
