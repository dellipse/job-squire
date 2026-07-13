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
"""Sample locations shown as placeholder/example text in the UI.

These are illustrative only — never used for search logic, just to give a
concrete example of the expected "City, ST" format on empty fields and
validation messages, without leaning on one hardcoded city.

Scoped to the US for now, matching the app's current strict "City, ST"
location validation (see settings_search() in main.py). When international
search targets are supported, add a second pool here (e.g. SAMPLE_CITIES_INTL)
and have random_sample_city() choose from it based on the configured country
instead of always drawing from the US list.
"""
import random

# Top 50 US cities by population, "City, ST" format. Order doesn't matter —
# a city is picked at random per call.
TOP_US_CITIES = [
    "New York, NY", "Los Angeles, CA", "Chicago, IL", "Houston, TX",
    "Phoenix, AZ", "Philadelphia, PA", "San Antonio, TX", "San Diego, CA",
    "Dallas, TX", "Jacksonville, FL", "Austin, TX", "Fort Worth, TX",
    "San Jose, CA", "Charlotte, NC", "Columbus, OH", "Indianapolis, IN",
    "San Francisco, CA", "Seattle, WA", "Denver, CO", "Oklahoma City, OK",
    "Nashville, TN", "El Paso, TX", "Washington, DC", "Las Vegas, NV",
    "Boston, MA", "Portland, OR", "Detroit, MI", "Louisville, KY",
    "Memphis, TN", "Baltimore, MD", "Milwaukee, WI", "Albuquerque, NM",
    "Tucson, AZ", "Fresno, CA", "Sacramento, CA", "Mesa, AZ",
    "Kansas City, MO", "Atlanta, GA", "Omaha, NE", "Colorado Springs, CO",
    "Raleigh, NC", "Long Beach, CA", "Virginia Beach, VA", "Miami, FL",
    "Oakland, CA", "Minneapolis, MN", "Tulsa, OK", "Bakersfield, CA",
    "Wichita, KS", "Arlington, TX",
]


def random_sample_city() -> str:
    """Return a random "City, ST" example for placeholder/hint text."""
    return random.choice(TOP_US_CITIES)
