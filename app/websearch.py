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
"""Best-effort, keyless web research via DuckDuckGo's plain HTML results page.

There is no official, free DuckDuckGo search API. This scrapes
html.duckduckgo.com/html/, which is unofficial, unsupported, and can change
layout or start rate-limiting at any time without notice. Every function here
is defensive: a failure (network error, layout change, timeout, empty
results) returns an empty result rather than raising, so a research hiccup
never blocks or breaks kit generation — the kit is simply built without that
context, same as before this feature existed.
"""
import logging
from datetime import datetime

import requests

log = logging.getLogger(__name__)

_DDG_URL = "https://html.duckduckgo.com/html/"
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; JobSquireBot/1.0; +https://github.com/dellipse/job-squire)"}


def ddg_search(query: str, max_results: int = 4, timeout: int = 10) -> list[dict]:
    """Return up to max_results {"title", "url", "snippet"} dicts. [] on any failure."""
    try:
        r = requests.post(_DDG_URL, data={"q": query}, headers=_HEADERS, timeout=timeout)
        r.raise_for_status()
    except requests.RequestException as exc:
        log.warning("ddg_search: request failed for %r: %s", query, exc)
        return []

    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(r.text, "html.parser")
        results = []
        for block in soup.select(".result")[:max_results]:
            link = block.select_one(".result__a")
            snippet = block.select_one(".result__snippet")
            if not link:
                continue
            title = link.get_text(strip=True)
            if not title:
                continue
            results.append({
                "title": title,
                "url": link.get("href", ""),
                "snippet": snippet.get_text(strip=True) if snippet else "",
            })
        return results
    except Exception as exc:  # noqa: BLE001
        # Covers missing bs4, unexpected markup, or anything else — research is
        # a nice-to-have, never worth failing the kit build over.
        log.warning("ddg_search: parse failed for %r: %s", query, exc)
        return []


def research_company_and_salary(company: str, title: str, location: str = "") -> str:
    """Best-effort research notes for an application kit.

    Runs two DuckDuckGo searches (company background, salary benchmarks) and
    formats the combined snippets as plain text for inclusion in an AI prompt.
    Returns "" if both searches come up empty — callers should treat that as
    "no research available" and proceed without it, exactly like before this
    feature existed.
    """
    year = datetime.now().year
    sections = []

    company_results = ddg_search(f"{company} company overview {year}")
    if company_results:
        lines = [f"COMPANY RESEARCH ({company}):"]
        for r in company_results:
            lines.append(f"- {r['title']}: {r['snippet']}" if r["snippet"] else f"- {r['title']}")
        sections.append("\n".join(lines))

    loc_part = f" {location}" if location else ""
    salary_results = ddg_search(f"{title} salary{loc_part} glassdoor levels.fyi bls.gov")
    if salary_results:
        lines = [f"SALARY BENCHMARK ({title}{loc_part}):"]
        for r in salary_results:
            lines.append(f"- {r['title']}: {r['snippet']}" if r["snippet"] else f"- {r['title']}")
        sections.append("\n".join(lines))

    return "\n\n".join(sections).strip()
