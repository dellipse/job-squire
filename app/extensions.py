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
"""Shared Flask extension instances."""
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_wtf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# expire_on_commit=False: by default SQLAlchemy expires every ORM attribute
# on commit, so a plain `obj.attr` read immediately after a plain
# `db.session.commit()` is actually a fresh SELECT, not a memory read. On
# this app's bind-mounted SQLite setup that SELECT can hit the same
# transient `disk I/O error` the commit itself is prone to (see
# app/db_utils.py) -- and when that read happens inside an `except` block
# handling a different error, it replaces a graceful failure with a second,
# unhandled crash. That's not hypothetical: it's exactly what took down
# /settings/assets/upload (2026-07-17). Turning off auto-expire kills this
# half of the bug class everywhere at once, for free. Safe here because
# Flask-SQLAlchemy tears down the whole session at the end of every request
# (scoped session + teardown_appcontext), so there's no cross-request
# staleness window; any code that deliberately wants a fresh post-commit
# read (e.g. main.py:settings_claude_pro's sanity-check read) already calls
# db.session.expire(obj) explicitly rather than relying on the default, so
# it keeps working unchanged.
db = SQLAlchemy(session_options={"expire_on_commit": False})
login_manager = LoginManager()
csrf = CSRFProtect()
# In-memory storage is intentional for this single-server deployment.
# Replace with storage_uri="redis://redis:6379/0" for strict per-worker enforcement.
limiter = Limiter(key_func=get_remote_address, default_limits=[], storage_uri="memory://")

login_manager.login_view = "auth.login"
login_manager.login_message = "Please sign in to continue."
login_manager.login_message_category = "warning"
