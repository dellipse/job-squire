# Copyright (C) 2026 D. Brandmeyer
# Licensed under the GNU Affero General Public License v3 or later.
"""Tests for POST /settings/search — specifically the international location
support added on top of the original US-only "City, ST" validation.

Behavior under test:
  * country defaults to "US" and preserves the original strict "City, ST"
    validation (regression guard — must not loosen for existing installs).
  * a non-US country only requires a non-empty location string.
  * a malformed country code (not exactly 2 letters) is rejected before saving.
"""
from app.extensions import db
from app.models import SearchConfig
from tests.conftest import login_admin as _login_admin

SEARCH_URL = "/settings/search"


def _post_search_settings(client, **fields):
    data = {
        "titles": "Engineer",
        "location": "",
        "country": "US",
        "radius_miles": "40",
        "max_age_days": "14",
        "results_per_query": "25",
    }
    data.update(fields)
    return client.post(SEARCH_URL, data=data, follow_redirects=True)


def _current_cfg(app):
    with app.app_context():
        row = db.session.get(SearchConfig, 1)
        return row.location, row.country


def test_us_location_still_requires_city_state(client, app):
    _login_admin(client)
    resp = _post_search_settings(client, location="not-a-valid-location", country="US")
    assert b"must be" in resp.data.lower() or b"city, st" in resp.data.lower()

    # Rejected input must not have been saved.
    location, country = _current_cfg(app)
    assert location != "not-a-valid-location"


def test_us_location_with_valid_city_state_saves(client, app):
    _login_admin(client)
    resp = _post_search_settings(client, location="Boise, ID", country="US")
    assert resp.status_code == 200

    location, country = _current_cfg(app)
    assert location == "Boise, ID"
    assert country == "US"


def test_non_us_country_accepts_free_text_location(client, app):
    _login_admin(client)
    resp = _post_search_settings(client, location="Manchester", country="GB")
    assert resp.status_code == 200
    assert b"danger" not in resp.data or b"Search settings saved" in resp.data

    location, country = _current_cfg(app)
    assert location == "Manchester"
    assert country == "GB"


def test_non_us_country_still_rejects_empty_location(client, app):
    _login_admin(client)
    # First save a known-good baseline so we can confirm the empty attempt didn't clobber it.
    _post_search_settings(client, location="Berlin", country="DE")

    resp = _post_search_settings(client, location="", country="DE")
    assert b"Location is required" in resp.data

    location, country = _current_cfg(app)
    assert location == "Berlin"  # unchanged — the empty submission was rejected


def test_malformed_country_code_rejected(client, app):
    _login_admin(client)
    _post_search_settings(client, location="Boise, ID", country="US")  # known-good baseline

    resp = _post_search_settings(client, location="Toronto, ON", country="USA")
    assert b"2-letter code" in resp.data

    location, country = _current_cfg(app)
    assert country == "US"  # unchanged
    assert location == "Boise, ID"


def test_blank_country_defaults_to_us(client, app):
    _login_admin(client)
    resp = _post_search_settings(client, location="Boise, ID", country="")
    assert resp.status_code == 200

    location, country = _current_cfg(app)
    assert country == "US"
