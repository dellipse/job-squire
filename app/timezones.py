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
"""Map a job-search location string to an IANA timezone.

Used by the scheduler so the cron cadence follows the search location's local
time rather than the server clock (which may be GMT/UTC). The lookup is a simple
US state -> predominant timezone table, which is all this app needs: searches are
per-metro. A few states span two zones; the predominant zone is used. Override
with the SCHEDULE_TZ env var for anything this doesn't cover.
"""
import re

DEFAULT_TZ = "UTC"  # fallback when state can't be determined; override with SCHEDULE_TZ

# Two-letter state/territory -> predominant IANA zone.
_STATE_TZ = {
    "AL": "America/Chicago", "AK": "America/Anchorage", "AZ": "America/Phoenix",
    "AR": "America/Chicago", "CA": "America/Los_Angeles", "CO": "America/Denver",
    "CT": "America/New_York", "DE": "America/New_York", "DC": "America/New_York",
    "FL": "America/New_York", "GA": "America/New_York", "HI": "Pacific/Honolulu",
    "ID": "America/Boise", "IL": "America/Chicago", "IN": "America/Indiana/Indianapolis",
    "IA": "America/Chicago", "KS": "America/Chicago", "KY": "America/New_York",
    "LA": "America/Chicago", "ME": "America/New_York", "MD": "America/New_York",
    "MA": "America/New_York", "MI": "America/Detroit", "MN": "America/Chicago",
    "MS": "America/Chicago", "MO": "America/Chicago", "MT": "America/Denver",
    "NE": "America/Chicago", "NV": "America/Los_Angeles", "NH": "America/New_York",
    "NJ": "America/New_York", "NM": "America/Denver", "NY": "America/New_York",
    "NC": "America/New_York", "ND": "America/Chicago", "OH": "America/New_York",
    "OK": "America/Chicago", "OR": "America/Los_Angeles", "PA": "America/New_York",
    "RI": "America/New_York", "SC": "America/New_York", "SD": "America/Chicago",
    "TN": "America/Chicago", "TX": "America/Chicago", "UT": "America/Denver",
    "VT": "America/New_York", "VA": "America/New_York", "WA": "America/Los_Angeles",
    "WV": "America/New_York", "WI": "America/Chicago", "WY": "America/Denver",
    "PR": "America/Puerto_Rico",
}

# Full state names -> abbreviation, for locations written out in long form.
_NAME_ABBR = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "district of columbia": "DC", "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "puerto rico": "PR",
}


KNOWN_STATES = frozenset(_STATE_TZ)  # valid two-letter US state/territory codes


def parse_state(location):
    """Return the uppercase two-letter US state code from a location, or None.

    Accepts 'City, ST', 'City, ST 89011', or a full state name ('City, ST').
    """
    if not location:
        return None
    text = location.strip()
    m = re.search(r",\s*([A-Za-z]{2})\b", text)
    if m and m.group(1).upper() in KNOWN_STATES:
        return m.group(1).upper()
    low = text.lower()
    for name, abbr in _NAME_ABBR.items():
        if re.search(r"\b" + re.escape(name) + r"\b", low):
            return abbr
    return None


def timezone_for_location(location):
    """Return an IANA timezone string for a location like 'Tulsa, OK'.

    Falls back to DEFAULT_TZ when the state can't be determined.
    """
    if not location:
        return DEFAULT_TZ
    text = location.strip()

    # 1) trailing two-letter state code, e.g. "..., NV" or "..., NV 89011".
    m = re.search(r",\s*([A-Za-z]{2})\b", text)
    if m:
        abbr = m.group(1).upper()
        if abbr in _STATE_TZ:
            return _STATE_TZ[abbr]

    # 2) full state name appearing anywhere in the string.
    low = text.lower()
    for name, abbr in _NAME_ABBR.items():
        if re.search(r"\b" + re.escape(name) + r"\b", low):
            return _STATE_TZ[abbr]

    return DEFAULT_TZ
