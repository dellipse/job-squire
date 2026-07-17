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
"""Authentication blueprint."""
from flask import Blueprint, flash, make_response, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user
from urllib.parse import urlparse

from .db_utils import commit
from .extensions import limiter
from .forms import ChangePasswordForm, LoginForm
from .models import User

auth_bp = Blueprint("auth", __name__)


def _is_safe_next(target):
    """Only allow relative redirects back into this app."""
    if not target:
        return False
    parsed = urlparse(target)
    return parsed.scheme == "" and parsed.netloc == "" and target.startswith("/")


@auth_bp.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute; 60 per hour", methods=["POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))

    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(username=form.username.data.strip().lower()).first()
        if user and user.check_password(form.password.data):
            login_user(user, remember=True)
            nxt = request.args.get("next")
            return redirect(nxt if _is_safe_next(nxt) else url_for("main.dashboard"))
        flash("Invalid username or password.", "danger")
    # Never cache the login page — a cached copy retains a stale CSRF token
    # that will be invalid against a new session (e.g. after logout), causing
    # spurious "CSRF session token is missing" 400 errors on form submit.
    resp = make_response(render_template("login.html", form=form))
    resp.headers["Cache-Control"] = "no-store"
    return resp


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been signed out.", "success")
    return redirect(url_for("auth.login"))


@auth_bp.route("/account", methods=["GET", "POST"])
@login_required
@limiter.limit("10 per minute; 60 per hour", methods=["POST"])
def account():
    """Self-service password change for the signed-in account (admin or user).

    Requires the current password so a hijacked/left-open session can't be used
    to silently lock out the real owner. This is intentionally the only thing
    an account can change about itself here — username/role changes stay an
    operator (env var + restart) concern.
    """
    form = ChangePasswordForm()
    if form.validate_on_submit():
        if not current_user.check_password(form.current_password.data):
            flash("Current password is incorrect.", "danger")
        elif form.new_password.data != form.confirm_password.data:
            flash("New password and confirmation do not match.", "danger")
        elif form.new_password.data == form.current_password.data:
            flash("New password must be different from the current password.", "danger")
        else:
            current_user.set_password(form.new_password.data)
            commit()
            flash("Password changed.", "success")
            return redirect(url_for("auth.account"))
    return render_template("account.html", form=form)
