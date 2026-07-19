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

import json
import logging
import os
import re
import uuid

from flask import (Blueprint, current_app, flash, redirect, render_template,
                   request, url_for)
from flask_login import current_user, login_required

from .db_utils import commit, with_db_retry
from .extensions import db
from .models import (AIConfig, AIProviderConfig, CandidateAsset,
                     OnboardingState, ProviderCredential, SearchConfig,
                     SearchRun, SmtpConfig, User, ASSET_KINDS)
from .providers import PROVIDERS, REMOTE_ONLY_PROVIDERS

log = logging.getLogger(__name__)

onboarding_bp = Blueprint("onboarding", __name__)

# Ordered walkthrough steps. AI setup deliberately precedes the profile step:
# the Phase 2 resume interview needs an AI provider (or the manual-mode
# prompt), so users decide their AI posture first.
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
    {"key": "notifications", "title": "Email notifications",
     "blurb": "Digests, follow-up reminders, and weekly reviews in your inbox."},
]
STEP_KEYS = [s["key"] for s in STEPS]


def get_state() -> OnboardingState:
    # Called at the top of every Getting Started route, so this is the one
    # spot that most needs to absorb a transient SQLite hiccup rather than
    # 500 the whole page (see app/db_utils.py).
    state = with_db_retry(lambda: db.session.get(OnboardingState, 1))
    if state is None:
        state = OnboardingState(id=1)
        db.session.add(state)
        commit()
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


def _saved_profile_links() -> list[str]:
    """Bullet lines currently saved under any '## Online profiles' heading
    in candidate_profile.md, for read-back display on the onboarding form.

    save_profile_links() below always appends a fresh "## Online profiles"
    section rather than merging into an existing one, so a user who saves
    twice ends up with two such headings in the file — this collects links
    from all of them, in order, without duplicates, so the form can show
    what's actually saved regardless of how many sections exist.
    """
    try:
        with open(_profile_path(), encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return []
    links: list[str] = []
    seen = set()
    in_section = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            in_section = stripped.lower() == "## online profiles"
            continue
        if in_section and stripped.startswith("- "):
            link = stripped[2:].strip()
            if link and link not in seen:
                seen.add(link)
                links.append(link)
    return links


def _append_profile_section(heading: str, body: str) -> None:
    """Append a "## {heading}" section to candidate_profile.md (create if missing)."""
    path = _profile_path()
    existing = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            existing = f.read().rstrip()
    section = f"\n\n## {heading}\n{body}\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write((existing + section).lstrip("\n"))


def save_resume_draft(resume_markdown: str, profile_facts: str = "",
                      created_by: str = "", label: str = "") -> dict:
    """Persist a completed resume from the onboarding resume interview (or a
    manual paste-back) as a new resume variant.

    All three interview transports (manual paste-back, the interactive API
    chat, and the MCP tool) funnel through this one function — see
    docs/PLAN-onboarding.md Phase 2. kind="Resume" is no longer a singleton:
    each call inserts a *new* CandidateAsset variant rather than overwriting
    the previous one, and marks it is_base=True, unsetting that flag on every
    other kind="Resume" row -- the newest save becomes "the" resume used for
    tailoring, while older drafts stick around for reference and can be
    manually promoted back via app/main.py:asset_set_base. Optionally merges
    extracted profile facts (background, skills, etc.) into
    candidate_profile.md.

    Returns {"ok": True, "asset_id": ...} on success or {"error": ...}.
    """
    markdown = (resume_markdown or "").strip()
    if not markdown:
        return {"error": "No resume text to save."}

    upload_dir = current_app.config["UPLOAD_DIR"]
    stored_name = f"{uuid.uuid4().hex}.md"
    path = os.path.join(upload_dir, stored_name)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(markdown + "\n")
    except OSError as exc:
        log.warning("save_resume_draft: could not write %s: %s", path, exc)
        return {"error": f"Could not write resume file: {exc}"}

    size = os.path.getsize(path)

    # Demote whatever was previously the base before adding the new one, so
    # exactly one kind="Resume" row is ever is_base=True at a time.
    (CandidateAsset.query.filter_by(kind="Resume", is_base=True)
     .update({"is_base": False}))

    variant_num = CandidateAsset.query.filter_by(kind="Resume").count() + 1
    asset = CandidateAsset(
        kind="Resume",
        label=(label or "").strip() or f"AI-generated resume #{variant_num}",
        original_name="resume.md",
        stored_name=stored_name,
        content_type="text/markdown",
        size=size,
        notes="Generated by the Getting Started resume interview.",
        uploaded_by=created_by or "",
        is_base=True,
    )
    db.session.add(asset)
    commit()

    if profile_facts and profile_facts.strip():
        _append_profile_section("From resume interview", profile_facts.strip())

    return {
        "ok": True,
        "asset_id": asset.id,
        "message": "Resume saved. It's now your base resume for tailored application kits.",
    }


def _read_resume_asset_markdown(asset) -> str:
    """The saved text of a kind="Resume" markdown variant, for read-back into
    the paste-back textarea on the Getting Started profile step -- whether it
    came from the resume interview, a manual paste, or auto-converting an
    uploaded document (see app/resume_convert.py and
    app/main.py:settings_asset_upload). Every kind="Resume" row is guaranteed
    to be markdown text (never raw binary) by the writers of this slot, so a
    decode failure here means something wrote to it incorrectly -- treat it
    as empty rather than raising through the Getting Started page."""
    if not asset:
        return ""
    path = os.path.join(current_app.config["UPLOAD_DIR"], asset.stored_name)
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError) as exc:
        log.warning("_read_resume_asset_markdown: could not read asset %s (%s): %s",
                    asset.id, path, exc)
        return ""


def _step_data_satisfied(key: str, state: OnboardingState) -> bool:
    """Whether the step's underlying data/answer condition is met, ignoring
    whether its page has ever been visited.

    Split out from _step_done() so the one-time migration backfill (see
    __init__.py._run_migrations) can ask "would this step already look done
    under the old, visit-blind logic?" without duplicating the per-key
    rules — that's how it decides which steps to retroactively mark visited
    for installs that finished onboarding before visit-tracking existed.
    """
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
    if key == "notifications":
        if answered in ("answered", "no_email"):
            return True
        cfg = db.session.get(SmtpConfig, 1)
        return bool(cfg and cfg.enabled and cfg.host.strip() and cfg.to_addr.strip())
    return False


def _step_done(key: str, state: OnboardingState) -> bool:
    """Derived completion — the checklist mirrors actual app state.

    Requires BOTH conditions: the step's own page must have been visited at
    least once (state.visited), AND its underlying data/answer must satisfy
    the step's condition (_step_data_satisfied). Data alone isn't enough —
    several steps ship with non-empty defaults (e.g. "themuse" job board is
    enabled out of the box) that would otherwise mark a step "done" before
    the user ever saw the page. Visited alone isn't enough either — merely
    opening the page shouldn't complete a step whose settings were never
    actually filled in.
    """
    if key not in state.visited:
        return False
    return _step_data_satisfied(key, state)


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


def get_onboarding_redirect():
    """URL to land an admin on instead of the dashboard, or None to show the
    dashboard normally.

    Fresh install: persona is the very first thing shown, since nothing else
    in the walkthrough makes sense before the app knows who it's for. Once
    persona is done (or explicitly skipped — "Skip for now" shouldn't just
    bounce the admin right back to the same page), the checklist overview
    becomes the landing page until every step is "done". Respects the
    "dismissed" flag, same as the dashboard card, so dismissing onboarding
    actually stops the nagging.
    """
    state = get_state()
    if state.dismissed:
        return None
    checklist = build_checklist(state)
    persona_status = next(i["status"] for i in checklist if i["key"] == "persona")
    if persona_status == "todo":
        return url_for("onboarding.step", step="persona")
    if all(item["status"] == "done" for item in checklist):
        return None
    return url_for("onboarding.overview")


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
    commit()
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
    commit()
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
    state.mark_visited(step)
    commit()
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
        ctx["saved_profile_links"] = _saved_profile_links()
        from .forms import CandidateAssetForm
        ctx["asset_form"] = CandidateAssetForm()
        # Resume-interview options (Phase 2): a "Resume"-kind asset is the
        # markdown draft -- either AI-generated via the interview, or
        # produced by auto-converting an uploaded "Base Resume" document (see
        # app/resume_convert.py) -- tracked separately from uploaded
        # documents so either path can replace it without touching anything
        # else the user uploaded.
        ctx["resume_asset"] = CandidateAsset.query.filter_by(
            kind="Resume", is_base=True).first()
        ctx["resume_variants"] = (CandidateAsset.query.filter_by(kind="Resume")
                                   .order_by(CandidateAsset.uploaded_at.desc()).all())
        ctx["uploaded_assets"] = [a for a in ctx["assets"] if a.kind != "Resume"]
        ctx["resume_markdown_draft"] = _read_resume_asset_markdown(ctx["resume_asset"])
        from .ai import _has_ranked_providers
        ai_cfg_for_resume = db.session.get(AIConfig, 1)
        ctx["ai_cfg"] = ai_cfg_for_resume
        ctx["has_ai_provider"] = bool(
            _has_ranked_providers() or (ai_cfg_for_resume and ai_cfg_for_resume.api_key_enc))
        cname = (ai_cfg_for_resume.connector_name if ai_cfg_for_resume else "") or "job-squire"
        from .prompts import resume_interview_manual_prompt, resume_builder_mcp_prompt
        ctx["resume_manual_prompt"] = resume_interview_manual_prompt()
        ctx["resume_mcp_prompt"] = resume_builder_mcp_prompt(cname)
    elif step == "search":
        from .main import _singleton
        from .sample_locations import random_sample_city
        ctx["search_cfg"] = _singleton(SearchConfig)
        ctx["sample_city"] = random_sample_city()
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
    elif step == "notifications":
        from .main import _singleton
        ctx["smtp"] = _singleton(SmtpConfig)
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
    commit()
    return redirect(url_for("onboarding.step", step="accounts"))


@onboarding_bp.route("/getting-started/accounts", methods=["POST"])
@login_required
@_admin_required
def save_accounts():
    state = get_state()
    action = request.form.get("action", "")
    if action == "just_me":
        state.set_step("accounts", "answered")
        commit()
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
    commit()
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
        commit()
        flash("Continuing without AI. Job search, tracking, and follow-up "
              "reminders all work normally; automatic job scoring, tailored "
              "resumes/cover letters, and weekly reviews stay off. Manual "
              "mode (copy/paste into any AI chat) is always available, and "
              "you can add a provider under Settings → AI at any time.", "info")
    else:
        state.set_step("ai", "answered")
        commit()
    return redirect(url_for("onboarding.step", step="profile"))


@onboarding_bp.route("/getting-started/notifications", methods=["POST"])
@login_required
@_admin_required
def save_notifications():
    """Explicit "no email" bypass for the last step, mirroring save_ai's
    "no_ai" — a deliberate opt-out marks the step done and stops the
    checklist nagging, unlike the generic Skip button which leaves it
    flagged as still needing attention. The actual SMTP fields themselves
    are saved by main.settings_smtp (posted to directly from the step
    template with next= this page), not here."""
    state = get_state()
    action = request.form.get("action", "")
    if action == "no_email":
        state.set_step("notifications", "no_email")
        commit()
        flash("Continuing without email notifications. Everything else works "
              "normally; add email later under Settings → Email whenever "
              "you're ready.", "info")
    else:
        state.set_step("notifications", "answered")
        commit()
    return redirect(url_for("onboarding.overview"))


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


# ---------------------------------------------------------------------------
# Resume interview (Phase 2, docs/PLAN-onboarding.md)
# ---------------------------------------------------------------------------

@onboarding_bp.route("/getting-started/profile/interview", methods=["GET", "POST"])
@login_required
@_admin_required
def resume_interview():
    """Interactive, API-mode resume interview — one question per round trip.

    State lives entirely in a hidden `history_json` form field (a list of
    {role, content} turns) rather than a server-side session, matching this
    app's stateless-request style. See ai.run_resume_interview_turn().
    """
    from .ai import run_resume_interview_turn, _has_ranked_providers

    ai_cfg = db.session.get(AIConfig, 1)
    has_provider = _has_ranked_providers() or bool(ai_cfg and ai_cfg.api_key_enc)
    if not has_provider:
        flash("Add an AI provider under Settings → AI first, or use the "
              "copy-paste or Claude connector options instead.", "warning")
        return redirect(url_for("onboarding.step", step="profile"))

    candidate = User.query.filter_by(role="user").first()
    candidate_name = ((candidate.display_name or candidate.username)
                      if candidate else "the candidate")

    history = []
    if request.method == "POST":
        try:
            history = json.loads(request.form.get("history_json") or "[]")
        except ValueError:
            history = []
        answer = (request.form.get("answer") or "").strip()
        if answer:
            history.append({"role": "user", "content": answer})

    back_url = url_for("onboarding.step", step="profile")
    try:
        result = run_resume_interview_turn(history, candidate_name)
    except Exception as exc:  # noqa: BLE001
        log.warning("resume interview turn failed: %s", exc)
        flash(f"The AI interview hit an error ({exc}). Try again, or use the "
              "copy-paste or Claude connector option instead.", "danger")
        return render_template("resume_interview.html", history=history, done=False,
                               history_json=json.dumps(history), question=None,
                               back_url=back_url)

    if result.get("done"):
        return render_template(
            "resume_interview.html", history=history, done=True,
            resume_markdown=result.get("resume_markdown", ""),
            profile_facts=result.get("profile_facts", ""),
            back_url=back_url,
        )

    history.append({"role": "assistant", "content": result.get("message", "")})
    return render_template(
        "resume_interview.html", history=history, done=False,
        history_json=json.dumps(history), question=result.get("message", ""),
        back_url=back_url,
    )


@onboarding_bp.route("/getting-started/profile/resume-draft", methods=["POST"])
@login_required
@_admin_required
def save_resume():
    """Save a finished resume, whichever transport produced it: pasted from a
    manual-mode AI chat, confirmed from the interactive API interview, or (for
    the MCP path) already saved directly by the save_resume_draft MCP tool."""
    markdown = request.form.get("resume_markdown", "")
    facts = request.form.get("profile_facts", "")
    result = save_resume_draft(
        markdown, facts,
        created_by=current_user.display_name or current_user.username,
    )
    if result.get("error"):
        flash(result["error"], "danger")
    else:
        flash("Resume saved as your base resume for tailored application kits.", "success")
    return redirect(url_for("onboarding.step", step="profile"))
