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
"""Line-preserving .env read/append/set helpers (Prompt C7)."""
from job_squire_cli.ops import dotenv


def test_parse_missing_file_returns_empty_dict(tmp_path):
    assert dotenv.parse(tmp_path / "nope.env") == {}


def test_parse_skips_blank_lines_and_comments(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# a comment\n\nKEY=value\n  # indented comment\nOTHER=1\n")
    assert dotenv.parse(path) == {"KEY": "value", "OTHER": "1"}


def test_get_returns_default_when_absent(tmp_path):
    path = tmp_path / ".env"
    path.write_text("KEY=value\n")
    assert dotenv.get(path, "MISSING", "fallback") == "fallback"
    assert dotenv.get(path, "KEY") == "value"


def test_set_line_appends_when_absent(tmp_path):
    path = tmp_path / ".env"
    path.write_text("A=1\n")
    dotenv.set_line(path, "B", "2")
    assert dotenv.parse(path) == {"A": "1", "B": "2"}


def test_set_line_replaces_in_place_preserving_other_lines(tmp_path):
    path = tmp_path / ".env"
    path.write_text("A=1\nB=old\nC=3\n")
    dotenv.set_line(path, "B", "new")
    lines = path.read_text().splitlines()
    assert lines == ["A=1", "B=new", "C=3"]


def test_set_line_creates_file_if_missing(tmp_path):
    path = tmp_path / ".env"
    dotenv.set_line(path, "A", "1")
    assert dotenv.parse(path) == {"A": "1"}


def test_append_if_absent_appends_and_reports_true(tmp_path):
    path = tmp_path / ".env"
    path.write_text("SECRET_KEY=abc\n")
    appended = dotenv.append_if_absent(path, "TRUST_PROXY", "1", comment="# why")
    assert appended is True
    text = path.read_text()
    assert "TRUST_PROXY=1" in text
    assert "# why" in text
    assert "SECRET_KEY=abc" in text  # untouched


def test_append_if_absent_noop_when_already_set(tmp_path):
    path = tmp_path / ".env"
    path.write_text("TRUST_PROXY=0\n")
    appended = dotenv.append_if_absent(path, "TRUST_PROXY", "1")
    assert appended is False
    assert dotenv.parse(path) == {"TRUST_PROXY": "0"}  # never overwritten


def test_append_if_absent_on_missing_file_creates_it(tmp_path):
    path = tmp_path / "data" / ".env"
    path.parent.mkdir()
    appended = dotenv.append_if_absent(path, "A", "1")
    assert appended is True
    assert dotenv.parse(path) == {"A": "1"}
