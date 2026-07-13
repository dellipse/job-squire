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
"""Getting Started walkthrough (docs/PLAN-onboarding.md, Phase 1).

A persistent, re-entrant checklist rather than a one-shot wizard: each step is
a focused page, steps can be skipped and revisited any time, and completion is
*derived from real data* wherever possible (a resume exists, search targets
are set, a search has run) so the checklist reflects reality instead of a
stored flag that can drift. Step pages mostly post to the existing Settings
routes (via their `next` field) so there is exactly one code path for saving
anything.
"""

import logging
import os
import re

from flask import (Blueprint, current_app, flash, redirect, render_template,
                   request, url_for)
from flask_login import login_required

from .extensions import db
from .models import (AIConfig, AIProviderConfig, CandidateAsset,
                     OnboardingState, ProviderCredential, SearchConfig,
                     SearchRun, User, ASSET_KINDS)
from .providers import PROVIDERS, REMOTE_ONLY_PROVIDERS

log = logging.getLogger(__name__)

onboarding_bp = Blueprint("onboarding", __name__)

# Ordered walkthrough steps. AI setup deliberately precedes the profile step:
# the Phase 2 resume interview needs an AI provider (or the manual-mode
# prompt), so users decide their AI posture first. See the plan doc.
STEPS = [
    {"key": "persona",      "title": "Welcome",
     "blurb": "Tell Job Squire who this install is for."},
    {"key": "accounts",     "title": "Accounts",
     "blurb": "Optionally add a second sign-in."},
    {"key": "ai",           "title": "AI setup",
     "blurb": "Local, cloud, Claude — or no AI at all."},
    {"key": "profile",      "title": "Resume & documents",
     "blurb": "Upload your resume, certifications, and letters."},
    {"key": "search",       "title": "Search targets",
     "blurb": "What jobs, where, and your salary floor."},
    {"key": "providers",    "title": "Job boards",
     "blurb": "Connect the boards that feed your searches."},
    {"key": "first_search", "title": "First search",
     "blurb": "Run it and see real results."},
]
STEP_KEYS = [s["key"] for s in STEPS]


def get_state() -> OnboardingState:
    state = db.session.get(OnboardingState, 1)
    if state is None:
        state = OnboardingState(id=1)
        db.session.add(state)
        db.session.commit()
    return state


def _profile_path() -> str:
    return os.path.join(current_app.config["DATA_DIR"], "candidate_profile.md")


def _bundled_profile_template() -> str:
    """The placeholder candidate_profile.md shipped with the app and copied
    into DATA_DIR on first boot (see _seed_data_files in __init__.py)."""
    try:
        path = os.path.join(os.path.dirname(__file__), "candidate_profile.md")
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


# Bracket placeholders from the shipped template (e.g. "[Write a 2-4 sentence
# professional summary here.]"). Several of these still present means the user
# hasn't actually written their profile yet, even if the file is non-empty.
_PROFILE_PLACEHOLDER_MARKERS = (
    "[Write a", "[List key skills", "[Job Title]", "[Degree]",
    "[Certification name]", "[Accomplishment", "[Name], [Title]",
)


def _profile_seems_filled_out(text: str) -> bool:
    """True once the profile looks like more than the untouched seed template.

    A plain non-empty check marks this step "done" the instant the bundled
    template is copied to DATA_DIR on first boot — before the user has
    written a word of their own profile. This requires the text to actually
    differ from the shipped placeholder, have most of its bracket
    placeholders replaced, and be long enough to hold real content.
    """
    text = (text or "").strip()
    if not text:
        return False
    if text == _bundled_profile_template():
        return False
    remaining = sum(1 for m in _PROFILE_PLACEHOLDER_MARKERS if m in text)
    if remaining >= 3:
        return False
    return len(text) > 300


def _profile_exists() -> bool:
    try:
        with open(_profile_path(), encoding="utf-8") as f:
            return _profile_seems_filled_out(f.read())
    except OSError:
        return False


def _step_done(key: str, state: OnboardingState) -> bool:
    """Derived completion — the checklist mirrors actual app state."""
    answered = state.steps.get(key, "")
    if key == "persona":
        return state.persona in ("self", "helper")
    if key == "accounts":
        return answered == "answered" or User.query.count() > 1
    if key == "ai":
        if answered in ("answered", "no_ai"):
            return True
        cfg = db.session.get(AIConfig, 1)
        has_provider = AIProviderConfig.query.filter_by(enabled=True).count() > 0
        return has_provider or bool(cfg and (cfg.api_enabled or cfg.mcp_enabled))
    if key == "profile":
        has_resume = CandidateAsset.query.filter_by(kind="Resume").count() > 0
        return has_resume or _profile_exists()
    if key == "search":
        cfg = db.session.get(SearchConfig, 1)
        return bool(cfg and cfg.titles.strip() and cfg.location.strip())
    if key == "providers":
        return ProviderCredential.query.filter_by(enabled=True).count() > 0
    if key == "first_search":
        return SearchRun.query.count() > 0
    return False


def build_checklist(state: OnboardingState | None = None) -> list:
    """[{key, title, blurb, status}] — status is done | skipped | todo."""
    state = state or get_state()
    stored = state.steps
    out = []
    for step in STEPS:
        if _step_done(step["key"], state):
            status = "done"
        elif stored.get(step["key"]) == "skipped":
            status = "skipped"
        else:
            status = "todo"
        out.append({**step, "status": status})
    return out


def checklist_for_dashboard():
    """Checklist for the dashboard card, or None when it shouldn't show."""
    state = get_state()
    if state.dismissed:
        return None
    checklist = build_checklist(state)
    if all(item["status"] == "done" for item in checklist):
        return None
    return checklist


def _next_todo(checklist, after: str | None = None) -> str | None:
    """Key of the next actionable step, optionally after a given one."""
    keys = STEP_KEYS
    start = keys.index(after) + 1 if after in keys else 0
    for key in keys[start:] + keys[:start]:
        item = next(i for i in checklist if i["key"] == key)
        if item["status"] == "todo":
            return key
    return None


def _admin_required(f):
    # Local import to avoid pulling app.main at module import time.
    from .main import admin_required
    return admin_required(f)


# ---------------------------------------------------------------------------
# Checklist overview
# ---------------------------------------------------------------------------

@onboarding_bp.route("/getting-started")
@login_required
@_admin_required
def overview():
    state = get_state()
    checklist = build_checklist(state)
    return render_template("getting_started.html",
                           checklist=checklist, state=state,
                           next_step=_next_todo(checklist))


@onboarding_bp.route("/getting-started/dismiss", methods=["POST"])
@login_required
@_admin_required
def dismiss():
    state = get_state()
    state.dismissed = True
    db.session.commit()
    flash("Getting Started hidden from the dashboard. It stays available "
          "from the navigation menu whenever you need it.", "info")
    return redirect(url_for("main.dashboard"))


@onboarding_bp.route("/getting-started/<step>/skip", methods=["POST"])
@login_required
@_admin_required
def skip(step):
    if step not in STEP_KEYS:
        return redirect(url_for("onboarding.overview"))
    state = get_state()
    state.set_step(step, "skipped")
    db.session.commit()
    nxt = _next_todo(build_checklist(state), after=step)
    if nxt:
        return redirect(url_for("onboarding.step", step=nxt))
    return redirect(url_for("onboarding.overview"))


# ---------------------------------------------------------------------------
# Step pages
# ---------------------------------------------------------------------------

@onboarding_bp.route("/getting-started/<step>")
@login_required
@_admin_required
def step(step):
    if step not in STEP_KEYS:
        return redirect(url_for("onboarding.overview"))
    state = get_state()
    checklist = build_checklist(state)
    item = next(i for i in checklist if i["key"] == step)
    ctx = {
        "checklist": checklist,
        "state": state,
        "item": item,
        "step_key": step,
        "next_step": _next_todo(checklist, after=step),
        "back_url": url_for("onboarding.step", step=step),
    }
    if step == "accounts":
        ctx["second_account"] = User.query.filter(User.role != "admin").first()
        ctx["user_count"] = User.query.count()
    elif step == "ai":
        cfg = db.session.get(AIConfig, 1)
        ctx["ai_cfg"] = cfg
        ctx["enabled_ai_providers"] = (AIProviderConfig.query
                                       .filter_by(enabled=True)
                                       .order_by(AIProviderConfig.rank).all())
    elif step == "profile":
        ctx["assets"] = CandidateAsset.query.order_by(CandidateAsset.uploaded_at.desc()).all()
        ctx["asset_kinds"] = ASSET_KINDS
        ctx["profile_exists"] = _profile_exists()
        from .forms import CandidateAssetForm
        ctx["asset_form"] = CandidateAssetForm()
    elif step == "search":
        from .main import _singleton
        ctx["search_cfg"] = _singleton(SearchConfig)
    elif step == "providers":
        creds = {pc.provider: pc for pc in ProviderCredential.query.all()}
        keyless, keyed = [], []
        for name, meta in PROVIDERS.items():
            entry = {"name": name, "meta": meta,
                     "enabled": bool(creds.get(name) and creds[name].enabled),
                     "remote_only": name in REMOTE_ONLY_PROVIDERS,
                     "needs_key": any(f.get("required") for f in meta["fields"])}
            (keyed if entry["needs_key"] else keyless).append(entry)
        ctx["keyless_providers"] = keyless
        ctx["keyed_providers"] = keyed
    elif step == "first_search":
        cfg = db.session.get(SearchConfig, 1)
        ctx["search_cfg"] = cfg
        ctx["enabled_providers"] = ProviderCredential.query.filter_by(enabled=True).all()
        ctx["last_run"] = SearchRun.query.order_by(SearchRun.id.desc()).first()
        ai_cfg = db.session.get(AIConfig, 1)
        ctx["ai_active"] = bool(
            AIProviderConfig.query.filter_by(enabled=True).count()
            or (ai_cfg and (ai_cfg.api_enabled or ai_cfg.mcp_enabled)))
        ctx["search_ready"] = bool(cfg and cfg.titles.strip() and cfg.location.strip()
                                   and ctx["enabled_providers"])
    return render_template("getting_started_step.html", **ctx)


@onboarding_bp.route("/getting-started/persona", methods=["POST"])
@login_required
@_admin_required
def save_persona():
    choice = request.form.get("persona", "")
    if choice not in ("self", "helper"):
        flash("Pick one of the two options.", "warning")
        return redirect(url_for("onboarding.step", step="persona"))
    state = get_state()
    state.persona = choice
    db.session.commit()
    return redirect(url_for("onboarding.step", step="accounts"))


@onboarding_bp.route("/getting-started/accounts", methods=["POST"])
@login_required
@_admin_required
def save_accounts():
    state = get_state()
    action = request.form.get("action", "")
    if action == "just_me":
        state.set_step("accounts", "answered")
        db.session.commit()
        flash("No problem — you can add a second account later from this page.", "info")
        return redirect(url_for("onboarding.step", step="ai"))

    username = (request.form.get("username") or "").strip().lower()
    display_name = (request.form.get("display_name") or "").strip()
    password = request.form.get("password") or ""
    confirm = request.form.get("confirm") or ""
    if not re.fullmatch(r"[a-z0-9_.-]{3,40}", username):
        flash("Username must be 3-40 characters: lowercase letters, digits, "
              "dots, dashes, underscores.", "danger")
        return redirect(url_for("onboarding.step", step="accounts"))
    if User.query.filter_by(username=username).first():
        flash(f"The username \"{username}\" is already taken.", "danger")
        return redirect(url_for("onboarding.step", step="accounts"))
    if len(password) < 8:
        flash("Password must be at least 8 characters.", "danger")
        return redirect(url_for("onboarding.step", step="accounts"))
    if password != confirm:
        flash("Passwords do not match.", "danger")
        return redirect(url_for("onboarding.step", step="accounts"))

    user = User(username=username, display_name=display_name or username,
                role="user")
    user.set_password(password)
    db.session.add(user)
    state.set_step("accounts", "answered")
    db.session.commit()
    log.info("onboarding: second account %r created", username)
    flash(f"Account \"{username}\" created.", "success")
    return redirect(url_for("onboarding.step", step="ai"))


@onboarding_bp.route("/getting-started/ai", methods=["POST"])
@login_required
@_admin_required
def save_ai():
    state = get_state()
    action = request.form.get("action", "")
    if action == "no_ai":
        state.set_step("ai", "no_ai")
        db.session.commit()
        flash("Continuing without AI. Job search, tracking, and follow-up "
              "reminders all work normally; automatic job scoring, tailored "
              "resumes/cover letters, and weekly reviews stay off. Manual "
              "mode (copy/paste into any AI chat) is always available, and "
              "you can add a provider under Settings → AI at any time.", "info")
    else:
        state.set_step("ai", "answered")
        db.session.commit()
    return redirect(url_for("onboarding.step", step="profile"))


@onboarding_bp.route("/getting-started/profile-links", methods=["POST"])
@login_required
@_admin_required
def save_profile_links():
    """Append online profile links (LinkedIn etc.) to candidate_profile.md."""
    links = (request.form.get("links") or "").strip()
    if not links:
        flash("Enter at least one link first.", "warning")
        return redirect(url_for("onboarding.step", step="profile"))
    lines = [ln.strip() for ln in links.splitlines() if ln.strip()][:20]
    path = _profile_path()
    try:
        existing = ""
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                existing = f.read().rstrip()
        block = "\n".join(f"- {ln}" for ln in lines)
        section = f"\n\n## Online profiles\n{block}\n"
        with open(path, "w", encoding="utf-8") as f:
            f.write((existing + section).lstrip("\n"))
    except OSError as exc:
        log.warning("onboarding: could not write profile links: %s", exc)
        flash("Could not save the links — check the data directory is writable.",
              "danger")
        return redirect(url_for("onboarding.step", step="profile"))
    flash("Online profiles saved to your candidate profile.", "success")
    return redirect(url_for("onboarding.step", step="profile"))
