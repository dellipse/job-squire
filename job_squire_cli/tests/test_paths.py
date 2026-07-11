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
"""Per-instance directory layout (Prompt C5)."""
from pathlib import Path

from job_squire_cli.ops import paths


def test_default_data_root_is_home_job_squire(monkeypatch):
    monkeypatch.delenv(paths.DATA_ROOT_ENV_VAR, raising=False)
    assert paths.default_data_root() == Path.home() / "job-squire"


def test_default_data_root_honors_override_env_var(monkeypatch, tmp_path):
    monkeypatch.setenv(paths.DATA_ROOT_ENV_VAR, str(tmp_path))
    assert paths.default_data_root() == tmp_path


def test_instance_root_joins_name_onto_data_root(tmp_path):
    assert paths.instance_root("castelo", tmp_path) == tmp_path / "castelo"


def test_instance_root_uses_default_root_when_omitted(monkeypatch, tmp_path):
    monkeypatch.setenv(paths.DATA_ROOT_ENV_VAR, str(tmp_path))
    assert paths.instance_root("castelo") == tmp_path / "castelo"


def test_derived_paths_layout(tmp_path):
    root = tmp_path / "castelo"
    assert paths.compose_path(root) == root / "docker-compose.single.yml"
    assert paths.compose_env_path(root) == root / ".env"
    assert paths.data_dir(root) == root / "data"
    assert paths.data_env_path(root) == root / "data" / ".env"
    assert paths.sqlite_db_path(root) == root / "data" / "job-squire.db"


def test_sqlite_db_filename_matches_app_default():
    """app/__init__.py's DATABASE_URL default is sqlite:///<DATA_DIR>/job-squire.db --
    this constant must stay in lockstep or copy_db_settings would look in
    the wrong place."""
    assert paths.DB_FILENAME == "job-squire.db"
