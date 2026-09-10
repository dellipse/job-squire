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
"""Settings blueprint: account/AI/search/kit/SMTP configuration, AI provider
management, MCP API key + OAuth token administration, candidate assets
(resumes/certs), the candidate profile, backup download, and the machine
job-ingest API.

Split out of app/main.py (QUAL-01, Long-term audit item 17) -- the last
and largest of five domain blueprint extractions. _singleton,
_worker_heartbeat_status, admin_required, _bookmarklet_js, and the
candidate-profile read/write helpers (_load_profile, _save_profile,
_load_profile_prompt, _save_profile_prompt) are imported from app.main
rather than duplicated -- all are shared with other blueprints
(dashboard() and/or app/kits.py, per each helper's own docstring in
app/main.py).
"""
import hmac
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from flask_login import current_user, login_required
from sqlalchemy import text
from werkzeug.utils import secure_filename

from . import ai, privacy
from .backup import build_backup_archive
from .crypto import decrypt, dump_encrypted_json, encrypt, load_encrypted_json
from .db_utils import commit
from .extensions import csrf, db, limiter
from .forms import CandidateAssetEditForm, CandidateAssetForm, ConfirmForm
from .main import (
    _bookmarklet_js,
    _load_profile,
    _load_profile_prompt,
    _save_profile,
    _save_profile_prompt,
    _singleton,
    _worker_heartbeat_status,
    admin_required,
)
from .mcp_auth import expires_at_from_ttl_hours, generate_token, is_network_reachable
from .models import (
    ASSET_KINDS,
    AIConfig,
    AIProviderConfig,
    CandidateAsset,
    KitConfig,
    ProviderCredential,
    SearchConfig,
    SearchRun,
    SmtpConfig,
)
from .notify import send_email
from .providers import PROVIDERS, search_provider
from .search import ingest_jobs, run_search
from .task_status import _TaskStatus
from .timezones import parse_state

log = logging.getLogger(__name__)

settings_bp = Blueprint("settings", __name__)


# --------------------------------------------------------------------------
# Full backup download (DB snapshot + attachments). See app/backup.py for why
# restore is a CLI-only operation (scripts/restore.sh), not a route here.
# --------------------------------------------------------------------------
@settings_bp.route("/settings/backup/download")
@login_required
@admin_required
def backup_download():
    include_env = request.args.get("include_env", "1") != "0"
    try:
        filename, data = build_backup_archive(
            current_app.config["DATA_DIR"],
            current_app.config["UPLOAD_DIR"],
            include_env=include_env,
        )
    except FileNotFoundError:
        flash("Nothing to back up yet — no database found.", "danger")
        return redirect(url_for("settings.settings", _anchor="tab-backup"))
    except Exception:
        log.exception("Backup archive build failed")
        flash("Backup failed — check the server logs for details.", "danger")
        return redirect(url_for("settings.settings", _anchor="tab-backup"))

    return Response(
        data,
        mimetype="application/gzip",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )



@settings_bp.route("/settings/ai-mode", methods=["POST"])
@login_required
@admin_required
def settings_ai_mode():
    """Save Automatic Features toggle (api_enabled only)."""
    cfg = _singleton(AIConfig)
    cfg.api_enabled = "api_enabled" in request.form
    log.info(
        "settings_ai_mode: saving api_enabled=%s form_keys=%s",
        cfg.api_enabled, list(request.form.keys()),
    )
    # Keep legacy mode field in sync.
    if cfg.api_enabled:
        cfg.mode = "api"
    elif cfg.mcp_enabled:
        cfg.mode = "mcp"
    else:
        cfg.mode = "manual"
    commit()
    flash("AI features saved.", "success")
    return redirect(url_for("settings.settings"))


@settings_bp.route("/settings/claude-pro", methods=["POST"])
@login_required
@admin_required
def settings_claude_pro():
    """Save MCP Connector and Claude Pro toggles (mcp_enabled, claude_buttons_enabled)."""
    cfg = _singleton(AIConfig)
    cfg.mcp_enabled = "mcp_enabled" in request.form
    cfg.claude_buttons_enabled = "claude_buttons_enabled" in request.form
    log.info(
        "settings_claude_pro: saving mcp_enabled=%s claude_buttons_enabled=%s form_keys=%s",
        cfg.mcp_enabled, cfg.claude_buttons_enabled, list(request.form.keys()),
    )
    # Keep legacy mode field in sync.
    if cfg.api_enabled and cfg.mcp_enabled:
        cfg.mode = "api"
    elif cfg.api_enabled:
        cfg.mode = "api"
    elif cfg.mcp_enabled:
        cfg.mode = "mcp"
    else:
        cfg.mode = "manual"
    commit()
    db.session.expire(cfg)
    log.info(
        "settings_claude_pro: post-commit DB read → mcp_enabled=%s claude_buttons_enabled=%s",
        cfg.mcp_enabled, cfg.claude_buttons_enabled,
    )
    flash("Connector settings saved.", "success")
    return redirect(url_for("settings.settings") + "#tab-claude")


@settings_bp.route("/settings/ai", methods=["POST"])
@login_required
@admin_required
def settings_ai():
    cfg = _singleton(AIConfig)
    # Connector name (used in MCP prompts and buttons).
    connector_name = request.form.get("connector_name", "").strip()
    if connector_name:
        cfg.connector_name = connector_name
    commit()
    flash("Connector settings saved.", "success")
    return redirect(url_for("settings.settings") + "#tab-claude")


# --------------------------------------------------------------------------
# MCP API key management — see app/mcp_auth.py for the token spec (shape,
# storage, comparison, and the loopback-only reachability rule).
# --------------------------------------------------------------------------
@settings_bp.route("/settings/mcp-api-key", methods=["POST"])
@login_required
@admin_required
def settings_mcp_api_key():
    from datetime import datetime, timezone
    secret = current_app.config["SECRET_KEY"]
    cfg = _singleton(AIConfig)
    action = request.form.get("action", "generate")

    if action == "revoke":
        cfg.mcp_api_key_enc = ""
        cfg.mcp_api_key_created_at = None
        cfg.mcp_api_key_last_used_at = None
        cfg.mcp_api_key_expires_at = None
        commit()
        flash("MCP API key revoked.", "success")

    elif action == "set_network_override":
        # Explicit, independent opt-in required to let the static token be
        # used at all on a network-reachable instance -- generating or
        # rotating a key never turns this on implicitly.
        cfg.mcp_api_key_allow_network = bool(request.form.get("allow_network"))
        commit()
        flash(
            "Static key allowed on this network-reachable instance."
            if cfg.mcp_api_key_allow_network else
            "Static key restricted to loopback-only use again.",
            "success",
        )

    else:
        key = generate_token()
        now = datetime.now(timezone.utc)
        cfg.mcp_api_key_enc = encrypt(secret, key)
        cfg.mcp_api_key_created_at = now
        cfg.mcp_api_key_last_used_at = None
        cfg.mcp_api_key_expires_at = expires_at_from_ttl_hours(
            request.form.get("ttl_hours"), now=now)
        commit()
        flash(f"New MCP API key generated: {key}", "success")

    return redirect(url_for("settings.settings") + "#tab-claude")


# --------------------------------------------------------------------------
# OAuth token management (read/revoke — shared with mcp_server process via
# the DATA_DIR/oauth_tokens.json file)
# --------------------------------------------------------------------------

def _oauth_token_path() -> str:
    return os.path.join(current_app.config.get("DATA_DIR", "/data"), "oauth_tokens.json")


def _read_oauth_tokens() -> list:
    """Return live OAuth tokens as a list of display-safe dicts.

    Each entry has:
      token_id   — SHA-256 of the raw token (safe to expose in HTML)
      client_name — human-readable label captured at DCR
      issued_at  — Unix timestamp (float) or None for legacy tokens
      exp        — Unix timestamp (float)
    Expired tokens are omitted. Raw token values are never returned.
    """
    import hashlib as _hl
    path = _oauth_token_path()
    data = load_encrypted_json(path, current_app.config["SECRET_KEY"], default={})
    now = time.time()
    result = []
    for raw_token, meta in data.items():
        if meta.get("exp", 0) <= now:
            continue
        result.append({
            "token_id": _hl.sha256(raw_token.encode()).hexdigest(),
            "client_name": meta.get("client_name") or "Unknown client",
            "issued_at": meta.get("issued_at"),
            "exp": meta.get("exp"),
        })
    result.sort(key=lambda x: x.get("issued_at") or 0, reverse=True)
    return result


def _revoke_oauth_token_by_id(token_id: str) -> bool:
    """Remove the token whose SHA-256 matches token_id. Returns True if found."""
    import hashlib as _hl
    path = _oauth_token_path()
    secret = current_app.config["SECRET_KEY"]
    data = load_encrypted_json(path, secret, default={})
    match = next((k for k in data if _hl.sha256(k.encode()).hexdigest() == token_id), None)
    if match:
        del data[match]
        dump_encrypted_json(path, secret, data)
        return True
    return False


def _revoke_all_oauth_tokens() -> int:
    """Remove all tokens. Returns count removed."""
    path = _oauth_token_path()
    secret = current_app.config["SECRET_KEY"]
    data = load_encrypted_json(path, secret, default={})
    count = len(data)
    dump_encrypted_json(path, secret, {})
    return count


@settings_bp.route("/settings/mcp-revoke-token", methods=["POST"])
@login_required
@admin_required
def settings_mcp_revoke_token():
    token_id = request.form.get("token_id", "").strip()
    if token_id and _revoke_oauth_token_by_id(token_id):
        flash("Token revoked.", "success")
    else:
        flash("Token not found or already expired.", "warning")
    return redirect(url_for("settings.settings") + "#tab-claude")


@settings_bp.route("/settings/mcp-revoke-all", methods=["POST"])
@login_required
@admin_required
def settings_mcp_revoke_all():
    count = _revoke_all_oauth_tokens()
    flash(f"Revoked {count} token{'s' if count != 1 else ''}.", "success")
    return redirect(url_for("settings.settings") + "#tab-claude")


# --------------------------------------------------------------------------
# Per-task AI configuration
# --------------------------------------------------------------------------
@settings_bp.route("/settings/ai/tasks", methods=["POST"])
@login_required
@admin_required
def settings_ai_tasks():
    from .models import AITaskConfig, AI_TASK_NAMES
    for task_name in AI_TASK_NAMES:
        tc = AITaskConfig.query.filter_by(task_name=task_name).first()
        if tc is None:
            tc = AITaskConfig(task_name=task_name)
            db.session.add(tc)
        tc.enabled = f"{task_name}_enabled" in request.form
        primary_id = request.form.get(f"{task_name}_provider_id", "").strip()
        backup_id = request.form.get(f"{task_name}_backup_provider_id", "").strip()
        tc.provider_id = int(primary_id) if primary_id.isdigit() else None
        tc.backup_provider_id = int(backup_id) if backup_id.isdigit() else None
        tc.use_ranked_chain_fallback = f"{task_name}_chain_fallback" in request.form
    # Rejection alert threshold is submitted alongside task settings.
    cfg = _singleton(AIConfig)
    try:
        cfg.rejection_alert_threshold = max(1, int(request.form.get("rejection_alert_threshold") or 5))
    except (ValueError, TypeError):
        cfg.rejection_alert_threshold = 5
    commit()
    flash("Task settings saved.", "success")
    return redirect(url_for("settings.settings"))


# --------------------------------------------------------------------------
# AI Provider management (ranked fallback providers)
# --------------------------------------------------------------------------
_VALID_PROVIDER_TYPES = {
    "anthropic", "gemini", "groq", "openrouter", "ollama", "mistral", "openai",
    "cerebras", "github_models", "nous_portal", "litellm", "custom",
}


def _valid_ai_base_url(raw: str) -> bool:
    """Restrict a custom AI provider base URL to http(s) with a host.

    This only narrows the scheme (blocks file://, gopher://, and similar) —
    it deliberately does NOT block private/loopback/LAN hosts, since this
    field exists precisely so an admin can point at a self-hosted
    OpenAI-compatible endpoint (Ollama, LiteLLM, an internal server), often
    on localhost or the LAN. Full SSRF hardening isn't appropriate here: the
    field is admin-only (see @admin_required below) and the intended targets
    are private-network addresses. See security alert #214."""
    parsed = urlsplit(raw)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


@settings_bp.route("/settings/ai/providers/add", methods=["POST"])
@login_required
@admin_required
def ai_provider_add():
    secret = current_app.config["SECRET_KEY"]
    provider = request.form.get("provider", "").strip().lower()
    if provider not in _VALID_PROVIDER_TYPES:
        flash("Unknown provider type.", "danger")
        return redirect(url_for("settings.settings") + "#tab-claude")
    label = request.form.get("label", "").strip()
    api_key = request.form.get("api_key", "").strip()
    base_url = request.form.get("base_url", "").strip()
    if base_url and not _valid_ai_base_url(base_url):
        flash("Base URL must be a valid http:// or https:// address.", "danger")
        return redirect(url_for("settings.settings") + "#tab-claude")
    model = request.form.get("model", "").strip()
    triage_model = request.form.get("triage_model", "").strip()
    num_ctx_raw = request.form.get("num_ctx", "").strip()
    num_ctx = int(num_ctx_raw) if num_ctx_raw.isdigit() else None
    thinking_mode_raw = request.form.get("thinking_mode", "disabled")
    thinking_mode = thinking_mode_raw if thinking_mode_raw in ("disabled", "low", "medium", "high") else "disabled"
    # Assign the next rank
    max_rank = db.session.query(db.func.max(AIProviderConfig.rank)).scalar() or 0
    use_for_triage = bool(request.form.get("use_for_triage", True))
    use_for_analysis = bool(request.form.get("use_for_analysis", True))
    p = AIProviderConfig(
        rank=max_rank + 1,
        provider=provider,
        label=label,
        api_key_enc=encrypt(secret, api_key) if api_key else "",
        base_url=base_url,
        model=model,
        triage_model=triage_model,
        num_ctx=num_ctx,
        thinking_mode=thinking_mode if provider == "anthropic" else None,
        enabled=True,
        use_for_triage=use_for_triage,
        use_for_analysis=use_for_analysis,
    )
    db.session.add(p)
    commit()
    flash(f"Added {p.display_name} (rank {p.rank}).", "success")
    return redirect(url_for("settings.settings") + "#tab-claude")


@settings_bp.route("/settings/ai/providers/<int:pid>/edit", methods=["POST"])
@login_required
@admin_required
def ai_provider_edit(pid):
    secret = current_app.config["SECRET_KEY"]
    p = db.session.get(AIProviderConfig, pid)
    if not p:
        abort(404)
    p.label = request.form.get("label", "").strip()
    api_key = request.form.get("api_key", "").strip()
    if api_key:
        p.api_key_enc = encrypt(secret, api_key)
    base_url = request.form.get("base_url", "").strip()
    if base_url:
        if not _valid_ai_base_url(base_url):
            flash("Base URL must be a valid http:// or https:// address.", "danger")
            return redirect(url_for("settings.settings") + "#tab-claude")
        p.base_url = base_url
    p.model = request.form.get("model", "").strip()
    p.triage_model = request.form.get("triage_model", "").strip()
    num_ctx_raw = request.form.get("num_ctx", "").strip()
    p.num_ctx = int(num_ctx_raw) if num_ctx_raw.isdigit() else None
    # thinking_mode only applies to Anthropic providers
    if p.provider == "anthropic":
        thinking_mode_raw = request.form.get("thinking_mode", "disabled")
        p.thinking_mode = thinking_mode_raw if thinking_mode_raw in ("disabled", "low", "medium", "high") else "disabled"
    # Capability flags — use_for_triage is checked via checkbox presence
    p.use_for_triage = "use_for_triage" in request.form
    p.use_for_analysis = "use_for_analysis" in request.form
    commit()
    flash(f"Updated {p.display_name}.", "success")
    return redirect(url_for("settings.settings") + "#tab-claude")


@settings_bp.route("/settings/ai/providers/<int:pid>/delete", methods=["POST"])
@login_required
@admin_required
def ai_provider_delete(pid):
    from .models import AITaskConfig
    p = db.session.get(AIProviderConfig, pid)
    if not p:
        abort(404)
    name = p.display_name
    # SQLite doesn't enforce ondelete="SET NULL" — null out FKs manually.
    for tc in AITaskConfig.query.filter(
        (AITaskConfig.provider_id == pid) | (AITaskConfig.backup_provider_id == pid)
    ).all():
        if tc.provider_id == pid:
            tc.provider_id = None
        if tc.backup_provider_id == pid:
            tc.backup_provider_id = None
    db.session.delete(p)
    commit()
    # Re-sequence ranks so there are no gaps
    for i, row in enumerate(
        AIProviderConfig.query.order_by(AIProviderConfig.rank).all(), start=1
    ):
        row.rank = i
    commit()
    flash(f"Removed {name}.", "success")
    return redirect(url_for("settings.settings") + "#tab-claude")


@settings_bp.route("/settings/ai/providers/<int:pid>/toggle", methods=["POST"])
@login_required
@admin_required
def ai_provider_toggle(pid):
    p = db.session.get(AIProviderConfig, pid)
    if not p:
        abort(404)
    p.enabled = not p.enabled
    commit()
    state = "enabled" if p.enabled else "disabled"
    flash(f"{p.display_name} {state}.", "success")
    return redirect(url_for("settings.settings") + "#tab-claude")


@settings_bp.route("/settings/ai/providers/<int:pid>/move-up", methods=["POST"])
@login_required
@admin_required
def ai_provider_move_up(pid):
    p = db.session.get(AIProviderConfig, pid)
    if not p:
        abort(404)
    if p.rank > 1:
        prev = AIProviderConfig.query.filter(
            AIProviderConfig.rank == p.rank - 1
        ).first()
        if prev:
            prev.rank, p.rank = p.rank, prev.rank
            commit()
    return redirect(url_for("settings.settings") + "#tab-claude")


@settings_bp.route("/settings/ai/providers/<int:pid>/move-down", methods=["POST"])
@login_required
@admin_required
def ai_provider_move_down(pid):
    p = db.session.get(AIProviderConfig, pid)
    if not p:
        abort(404)
    nxt = AIProviderConfig.query.filter(
        AIProviderConfig.rank == p.rank + 1
    ).first()
    if nxt:
        nxt.rank, p.rank = p.rank, nxt.rank
        commit()
    return redirect(url_for("settings.settings") + "#tab-claude")


@settings_bp.route("/settings/ai/providers/<int:pid>/test", methods=["POST"])
@login_required
@admin_required
def ai_provider_test(pid):
    import time
    p = db.session.get(AIProviderConfig, pid)
    if not p:
        abort(404)
    secret = current_app.config["SECRET_KEY"]
    # This route is submitted as the Edit form's "Test connection" button (via
    # formaction), so it sees whatever is currently typed — including unsaved
    # changes. Fields left blank fall back to the last-saved value, matching
    # the "leave blank to keep current" convention used when actually saving.
    form_api_key = (request.form.get("api_key") or "").strip()
    api_key = form_api_key if form_api_key else (decrypt(secret, p.api_key_enc) if p.api_key_enc else "")
    form_base_url = (request.form.get("base_url") or "").strip()
    base_url = form_base_url or (p.base_url.strip() if p.base_url else "") or ai._PROVIDER_URLS.get(p.provider, "")
    if not base_url:
        flash(f"{p.display_name}: no base URL — enter one in the Edit form.", "warning")
        return redirect(url_for("settings.settings") + "#tab-claude")
    model = (request.form.get("model") or "").strip() or (p.model or "").strip()
    if not model:
        # Supply a known-cheap default per provider so the test call doesn't fail on a missing model
        _test_defaults = {
            "gemini": "gemini-2.0-flash-lite",
            "groq": "llama-3.1-8b-instant",
            "openrouter": "openrouter/free",
            "mistral": "mistral-small-latest",
            "openai": "gpt-4o-mini",
            "ollama": "llama3.2",
            "cerebras": "llama-3.3-70b",
            "github_models": "gpt-4o-mini",
            "nous_portal": "Hermes-3-Llama-3.1-70B",
            "litellm": "gpt-4o-mini",
        }
        model = _test_defaults.get(p.provider, "")
    if not model:
        flash(f"{p.display_name}: enter a model name before testing.", "warning")
        return redirect(url_for("settings.settings") + "#tab-claude")
    try:
        t0 = time.monotonic()
        reply = ai.call_openai_compat(
            base_url=base_url,
            api_key=api_key,
            model=model,
            system="You are a test assistant.",
            user_content="Reply with only the word OK.",
            max_tokens=16,
            provider=p.provider,
        )
        elapsed = round((time.monotonic() - t0) * 1000)
        flash(f"{p.display_name} ({model}): OK — {elapsed} ms. Reply: {reply[:80]!r}", "success")
    except Exception as e:  # noqa: BLE001
        flash(f"{p.display_name} test failed: {e.__class__.__name__}: {str(e)[:200]}", "danger")
    return redirect(url_for("settings.settings") + "#tab-claude")


@settings_bp.route("/settings/ai/privacy", methods=["POST"])
@login_required
@admin_required
def settings_ai_privacy():
    """Save the AI privacy (redaction) toggles."""
    cfg = _singleton(AIConfig)
    cfg.redaction_enabled = bool(request.form.get("redaction_enabled"))
    cfg.redact_strict = bool(request.form.get("redact_strict"))
    cfg.redact_local = bool(request.form.get("redact_local"))
    cfg.redact_heuristic_names = bool(request.form.get("redact_heuristic_names"))
    extra_patterns_raw = (request.form.get("redact_extra_patterns") or "").strip()
    # SEC-13: validate at save time so a typo is caught here, not silently
    # skipped (and only logged) the next time something actually gets
    # redacted -- see privacy.parse_extra_patterns()'s own docstring.
    _valid, pattern_warnings = privacy.parse_extra_patterns(extra_patterns_raw)
    cfg.redact_extra_patterns = extra_patterns_raw
    commit()
    if not cfg.redaction_enabled:
        flash("Privacy redaction disabled — personal identifiers will be sent "
              "to AI providers as-is.", "warning")
    else:
        bits = ["identifier redaction on"]
        if cfg.redact_strict:
            bits.append("strict mode (employers/locations pseudonymized)")
        if cfg.redact_local:
            bits.append("applied to local providers too")
        if cfg.redact_heuristic_names:
            bits.append("heuristic name detection on")
        if _valid:
            bits.append(f"{len(_valid)} extra pattern(s) active")
        flash("Privacy settings saved: " + ", ".join(bits) + ".", "success")
    for w in pattern_warnings:
        flash(f"Extra redaction pattern ignored — {w}", "warning")
    return redirect(url_for("settings.settings") + "#tab-claude")


@settings_bp.route("/settings/ai/providers/fallback", methods=["POST"])
@login_required
@admin_required
def ai_provider_fallback_toggle():
    cfg = _singleton(AIConfig)
    cfg.fallback_to_anthropic = bool(request.form.get("fallback_to_anthropic"))
    commit()
    state = "enabled" if cfg.fallback_to_anthropic else "disabled"
    flash(f"Anthropic fallback {state}.", "success")
    return redirect(url_for("settings.settings") + "#tab-claude")


# --------------------------------------------------------------------------
# Machine ingest API (Model A): token-authenticated push of found jobs
# --------------------------------------------------------------------------
@settings_bp.route("/api/ingest", methods=["POST"])
@csrf.exempt
# SEC-12 (2026-09-08 audit): coarse rate limit against brute-forcing
# INGEST_API_KEY -- no gate existed here at all before. Generous relative
# to /login's since this is a machine API meant for legitimate periodic
# bulk pushes, not a human typing a password.
@limiter.limit("30 per minute; 300 per hour", methods=["POST"])
def api_ingest():
    expected = os.environ.get("INGEST_API_KEY", "")
    provided = request.headers.get("X-API-Key", "")
    if not expected or not hmac.compare_digest(provided, expected):
        return {"error": "unauthorized"}, 401
    data = request.get_json(silent=True) or {}
    items = data.get("jobs")
    if not isinstance(items, list):
        return {"error": "expected JSON body with a 'jobs' array"}, 400
    created, skipped = ingest_jobs(items, created_by=(data.get("created_by") or "api"))
    return {"created": len(created), "skipped": skipped, "ids": [j.id for j in created]}



# --------------------------------------------------------------------------
# Connections / Settings (Model B): in-app search configuration
# --------------------------------------------------------------------------
@settings_bp.route("/settings")
@login_required
@admin_required
def settings():
    cfg = _singleton(SearchConfig)
    smtp = _singleton(SmtpConfig)
    secret = current_app.config["SECRET_KEY"]

    providers = []
    for key, meta in PROVIDERS.items():
        pc = ProviderCredential.query.filter_by(provider=key).first()
        creds = {}
        if pc and pc.secret_blob:
            try:
                creds = json.loads(decrypt(secret, pc.secret_blob)) or {}
            except json.JSONDecodeError:
                creds = {}
        fields = []
        for f in meta["fields"]:
            val = creds.get(f["name"], "")
            fields.append({
                **f,
                "value": "" if f["secret"] else val,
                "is_set": bool(val),
            })
        providers.append({
            "key": key, "label": meta["label"], "note": meta["note"],
            "signup_url": meta["signup_url"], "enabled": bool(pc and pc.enabled),
            "fields": fields,
        })

    runs = SearchRun.query.order_by(SearchRun.started_at.desc()).limit(10).all()

    ai_cfg = _singleton(AIConfig)
    # Diagnostic: raw SQL read to confirm DB value matches ORM value.
    _raw = db.session.execute(
        text("SELECT claude_buttons_enabled FROM ai_config WHERE id=1")
    ).fetchone()
    log.info(
        "settings(): ORM claude_buttons_enabled=%s  raw_DB=%s",
        ai_cfg.claude_buttons_enabled,
        _raw[0] if _raw else "NO ROW",
    )
    kit_cfg = _singleton(KitConfig)
    mcp_base_url = os.environ.get("PUBLIC_MCP_URL", "").rstrip("/")

    assets = CandidateAsset.query.order_by(CandidateAsset.kind.asc(),
                                            CandidateAsset.uploaded_at.desc()).all()
    asset_form = CandidateAssetForm()

    cname = ai_cfg.connector_name or "job-squire"
    profile_text = _load_profile()
    profile_prompt = _load_profile_prompt()

    # The embedded profile excerpt travels to claude.ai as chat text — redact it
    # like any other AI-bound content (the MCP tools rehydrate on write-back).
    _profile_for_prompt = (privacy.redact(profile_text).text
                          if privacy.redaction_enabled() else profile_text)
    regen_profile_prompt = (
        f'Using my "{cname}" connector, call get_candidate_assets() to retrieve all '
        f'uploaded candidate documents.\n\n'
        + profile_prompt
        + '\n\nOnce you have written the updated profile, call save_candidate_profile() '
        'with the full profile markdown so it saves directly to Job Squire — '
        'do not ask me to copy and paste it. Confirm once saved.\n\n'
        'Current profile (for format reference — do not simply copy this):\n---\n'
        + _profile_for_prompt
    )

    evaluate_docs_prompt = (
        f'Using my "{cname}" connector, call get_candidate_assets() to retrieve all '
        f'uploaded candidate documents.\n\n'
        'For each document returned, provide:\n'
        '- Document type and label\n'
        '- A brief summary of its contents\n'
        '- Key strengths demonstrated (specific skills, accomplishments, quantified metrics)\n'
        '- Any gaps, weaknesses, or areas for improvement\n'
        '- How well it supports the candidate\'s target roles (as stated in the profile)\n\n'
        'After reviewing all documents, provide an overall assessment:\n'
        '- Which documents are strongest and why\n'
        '- What critical items are missing (e.g., certifications, specific metrics, LinkedIn alignment)\n'
        '- Specific, actionable recommendations to strengthen the overall application package'
    )

    # Build routine prompts for the Claude tab.
    from .prompts import (
        ROUTINE_DESCRIPTIONS,
        morning_briefing_prompt,
        new_job_triage_prompt,
        kit_queue_prompt,
        followup_drafts_prompt,
        weekly_review_prompt,
    )
    from .models import User as _SettingsUser
    cuser = _SettingsUser.query.filter_by(role="user").first()
    candidate_name = (cuser.display_name or cuser.username) if cuser else "the candidate"
    if candidate_name != "the candidate" and privacy.redaction_enabled():
        candidate_name = privacy.redact(candidate_name).text
    _routine_prompts = [
        morning_briefing_prompt(cname),
        new_job_triage_prompt(cname, candidate_name),
        kit_queue_prompt(cname),
        followup_drafts_prompt(cname),
        weekly_review_prompt(cname),
    ]
    routines = [{**desc, "prompt": _routine_prompts[i]}
                for i, desc in enumerate(ROUTINE_DESCRIPTIONS)]

    from flask import request as _req
    _origin = _req.host_url.rstrip("/")
    bookmarklet_js = _bookmarklet_js(_origin)

    # All providers for the settings table (disabled ones shown grayed-out);
    # the template filters out disabled ones inside dropdowns.
    ai_providers = AIProviderConfig.query.order_by(AIProviderConfig.rank).all()

    from .models import AITaskConfig, AI_TASK_NAMES
    ai_task_configs = {
        tc.task_name: tc
        for tc in AITaskConfig.query.all()
    }

    oauth_tokens = _read_oauth_tokens()
    worker_health = _worker_heartbeat_status()

    return render_template(
        "settings.html", cfg=cfg, smtp=smtp, providers=providers, runs=runs,
        worker_stale=worker_health["stale"], worker_last_seen=worker_health["last_seen"],
        ingest_enabled=bool(os.environ.get("INGEST_API_KEY")),
        ai_cfg=ai_cfg, ai_key_set=bool(ai_cfg.api_key_enc),
        kit_cfg=kit_cfg,
        mcp_base_url=mcp_base_url,
        mcp_configured=bool(mcp_base_url),
        public_mcp_url=mcp_base_url,
        mcp_api_key_set=bool(ai_cfg.mcp_api_key_enc),
        mcp_api_key_created_at=ai_cfg.mcp_api_key_created_at,
        mcp_api_key_last_used_at=ai_cfg.mcp_api_key_last_used_at,
        mcp_api_key_expires_at=ai_cfg.mcp_api_key_expires_at,
        mcp_api_key_allow_network=bool(ai_cfg.mcp_api_key_allow_network),
        mcp_network_reachable=is_network_reachable(current_app.config.get("DEPLOY_MODE")),
        connector=cname,
        routines=routines,
        bookmarklet_js=bookmarklet_js,
        assets=assets, asset_form=asset_form, asset_kinds=ASSET_KINDS,
        confirm_form=ConfirmForm(),
        candidate_profile=profile_text,
        profile_prompt=profile_prompt,
        regen_profile_prompt=regen_profile_prompt,
        evaluate_docs_prompt=evaluate_docs_prompt,
        ai_providers=ai_providers,
        ai_task_configs=ai_task_configs,
        ai_task_names=AI_TASK_NAMES,
        oauth_tokens=oauth_tokens,
    )


def _safe_next(default_url: str) -> str:
    """Honor a relative `next` form field so onboarding pages can reuse settings
    POST routes and return to the walkthrough. Relative paths only — anything
    absolute (scheme or protocol-relative) is ignored to avoid open redirects.

    Uses urlsplit() rather than a raw startswith() check: some browsers
    normalize a leading "/\\" to "//" when resolving a URL, so a naive
    "starts with / but not //" test lets "/\\evil.com" slip through as a
    protocol-relative redirect. Parsing the URL and requiring an empty
    scheme/netloc closes that, and is the pattern CodeQL recognizes as a
    sanitizer for py/url-redirection (see security alerts #182-213)."""
    nxt = (request.form.get("next") or "").strip()
    if not nxt or "\\" in nxt:
        return default_url
    parsed = urlsplit(nxt)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
        return default_url
    return nxt


@settings_bp.route("/settings/search", methods=["POST"])
@login_required
@admin_required
def settings_search():
    cfg = _singleton(SearchConfig)
    back = _safe_next(url_for("settings.settings"))
    location = request.form.get("location", "").strip()
    country = (request.form.get("country") or "US").strip().upper()
    if len(country) != 2 or not country.isalpha():
        flash("Country must be a 2-letter code (ISO 3166-1 alpha-2), "
              "e.g. \"US\", \"GB\", \"DE\".", "danger")
        return redirect(back)
    if country == "US":
        # Providers expect "City, ST" (a valid US state code). Reject anything else
        # so a ZIP or address doesn't silently return empty results, and so the
        # scheduler can derive the right timezone from it. This strictness is
        # US-only: outside the US, timezones.py has no state table to key off of
        # anyway (see SCHEDULE_TZ), so it's just a plain non-empty location.
        if not parse_state(location):
            from .sample_locations import random_sample_city
            flash("Location must be \"City, ST\" with a valid US state code, "
                  f"e.g. \"{random_sample_city()}\". ZIP codes and street addresses are not "
                  "supported by the job sources; use the radius to widen the area.",
                  "danger")
            return redirect(back)
    elif not location:
        flash("Location is required, e.g. \"Manchester\" or \"Manchester, UK\".", "danger")
        return redirect(back)
    cfg.titles = request.form.get("titles", "").strip()
    cfg.location = location
    cfg.country = country
    cfg.radius_miles = _int(request.form.get("radius_miles"), 40)
    cfg.min_salary = _int(request.form.get("min_salary"), None, allow_none=True)
    cfg.max_age_days = _int(request.form.get("max_age_days"), 14)
    cfg.results_per_query = max(1, min(50, _int(request.form.get("results_per_query"), 25)))
    cfg.enabled = request.form.get("enabled") == "on"
    cfg.include_remote = request.form.get("include_remote") == "on"
    commit()
    flash("Search settings saved.", "success")
    return redirect(back)


@settings_bp.route("/settings/kit", methods=["POST"])
@login_required
@admin_required
def settings_kit():
    cfg = _singleton(KitConfig)
    cfg.fit_salary_floor = _int(request.form.get("fit_salary_floor"), 60000)
    commit()
    flash("Application Kit settings saved.", "success")
    return redirect(url_for("settings.settings"))


@settings_bp.route("/settings/providers/save-keyless", methods=["POST"])
@login_required
@admin_required
def settings_providers_keyless_save():
    """Batch-save the no-key-required job boards shown on the Getting Started
    'providers' step. These used to be separate auto-submitting checkboxes
    (one per board); checking several in a row could race against each
    other's page reload and silently drop a change. One form + one Save
    button submits all of them together instead."""
    checked = set(request.form.getlist("provider"))
    changed = []
    for name, meta in PROVIDERS.items():
        if any(f.get("required") for f in meta["fields"]):
            continue  # needs a key — managed individually on Settings | Sources
        pc = ProviderCredential.query.filter_by(provider=name).first()
        if not pc:
            pc = ProviderCredential(provider=name)
            db.session.add(pc)
        wants = name in checked
        if pc.enabled != wants:
            changed.append(meta["label"])
        pc.enabled = wants
    commit()
    flash(f"Job boards updated: {', '.join(changed)}." if changed else "No changes to job boards.", "success")
    return redirect(_safe_next(url_for("settings.settings")))


@settings_bp.route("/settings/provider/<provider>", methods=["POST"])
@login_required
@admin_required
def settings_provider(provider):
    if provider not in PROVIDERS:
        abort(404)
    secret = current_app.config["SECRET_KEY"]
    pc = ProviderCredential.query.filter_by(provider=provider).first()
    if not pc:
        pc = ProviderCredential(provider=provider)
        db.session.add(pc)
    existing = {}
    if pc.secret_blob:
        try:
            existing = json.loads(decrypt(secret, pc.secret_blob)) or {}
        except json.JSONDecodeError:
            existing = {}
    creds = dict(existing)
    for f in PROVIDERS[provider]["fields"]:
        submitted = request.form.get(f["name"], "")
        if f["secret"]:
            # Keep the stored secret if the field was left blank.
            if submitted.strip():
                creds[f["name"]] = submitted.strip()
        else:
            creds[f["name"]] = submitted.strip()
    pc.secret_blob = encrypt(secret, json.dumps(creds))
    wants_enabled = request.form.get("enabled") == "on"
    if wants_enabled:
        # Block enabling if any required field is still missing after this save.
        missing_label = next(
            (f["label"] for f in PROVIDERS[provider]["fields"]
             if f.get("required") and not creds.get(f["name"], "").strip()),
            None,
        )
        if missing_label:
            pc.enabled = False
            commit()
            flash(
                f"{PROVIDERS[provider]['label']} saved but not enabled — "
                f"{missing_label} is required.",
                "warning",
            )
            return redirect(_safe_next(url_for("settings.settings")))
    pc.enabled = wants_enabled
    commit()
    flash(f"{PROVIDERS[provider]['label']} settings saved.", "success")
    return redirect(_safe_next(url_for("settings.settings")))


@settings_bp.route("/settings/provider/<provider>/test", methods=["POST"])
@login_required
@admin_required
def settings_provider_test(provider):
    """Ping one provider with the saved key and report the live result."""
    if provider not in PROVIDERS:
        abort(404)
    label = PROVIDERS[provider]["label"]
    secret = current_app.config["SECRET_KEY"]
    pc = ProviderCredential.query.filter_by(provider=provider).first()
    creds = {}
    if pc and pc.secret_blob:
        try:
            creds = json.loads(decrypt(secret, pc.secret_blob)) or {}
        except json.JSONDecodeError:
            creds = {}

    cfg_row = db.session.get(SearchConfig, 1)
    titles = (cfg_row.title_list if cfg_row else None) or []
    cfg = {
        "location": (cfg_row.location if cfg_row else None) or "",
        "radius_miles": (cfg_row.radius_miles if cfg_row else None) or 40,
        "min_salary": cfg_row.min_salary if cfg_row else None,
        "max_age_days": (cfg_row.max_age_days if cfg_row else None) or 14,
        "results_per_query": 5,  # keep the probe light
    }
    # One title is enough to verify auth without burning rate limit.
    results, err = search_provider(provider, creds, titles[:1], cfg)
    if err:
        flash(f"{label} test failed: {err}", "danger")
    else:
        flash(f"{label} OK — connected and returned {len(results)} result(s) for "
              f"\"{titles[0]}\".", "success")
    return redirect(url_for("settings.settings"))


@settings_bp.route("/settings/provider/<provider>/pull", methods=["POST"])
@login_required
@admin_required
def settings_provider_pull(provider):
    """Run a full search for one provider, ingest results, and clear any cooldown --
    in a background thread, polled via the shared task-status page.

    search_provider() throttles between search titles (60-120s per gap; see
    THROTTLE_SECONDS in app/providers.py) and used to run inline on this request,
    so as few as 4 configured titles (3 throttle gaps) reliably exceeded gunicorn's
    180s --timeout and got the worker SIGABRT'd mid-request -- the same class of
    gunicorn-timeout-kills-a-slow-request bug REL-01 already fixed for
    job_prep_interview/ai_analyze/the resume interview. Routes through that same
    background-thread + poll pattern instead of joining that list of fixes with a
    fourth, parallel one.
    """
    if provider not in PROVIDERS:
        abort(404)
    label = PROVIDERS[provider]["label"]
    secret = current_app.config["SECRET_KEY"]
    pc = ProviderCredential.query.filter_by(provider=provider).first()
    creds = {}
    if pc and pc.secret_blob:
        try:
            creds = json.loads(decrypt(secret, pc.secret_blob)) or {}
        except json.JSONDecodeError:
            creds = {}

    cfg_row = db.session.get(SearchConfig, 1)
    titles = (cfg_row.title_list if cfg_row else None) or []
    cfg = {
        "location": (cfg_row.location if cfg_row else None) or "",
        "radius_miles": (cfg_row.radius_miles if cfg_row else None) or 40,
        "min_salary": cfg_row.min_salary if cfg_row else None,
        "max_age_days": (cfg_row.max_age_days if cfg_row else None) or 14,
        "results_per_query": (cfg_row.results_per_query if cfg_row else None) or 25,
    }
    # Tracked the same way a full search is, so it shows up in Settings | History
    # instead of silently vanishing after the task-status tab is closed.
    run = SearchRun(trigger="manual", status="running", providers=provider)
    db.session.add(run)
    commit()
    run_id_db = run.id

    run_id = uuid.uuid4().hex
    task_name = f"pull_{provider}"
    data_dir = current_app.config["DATA_DIR"]
    status = _TaskStatus(run_id, task_name, data_dir)
    _app = current_app._get_current_object()

    def _run():
        with _app.app_context():
            db_run = db.session.get(SearchRun, run_id_db)
            try:
                status.log(f"INFO Pulling {label}…")
                results, err = search_provider(provider, creds, titles, cfg)
                if err:
                    db_run.finished_at = datetime.now(timezone.utc)
                    db_run.status = "error"
                    db_run.detail = err[:1000]
                    commit()
                    status.fail(err)
                    return

                from .search import _load_cooldowns, _save_cooldowns
                cooldowns = _load_cooldowns()
                if provider in cooldowns:
                    del cooldowns[provider]
                    _save_cooldowns(cooldowns)

                created, skipped = ingest_jobs(results, created_by=f"pull:{provider}")
                db_run.finished_at = datetime.now(timezone.utc)
                db_run.found = len(results)
                db_run.created = len(created)
                db_run.skipped = skipped
                db_run.status = "ok"
                commit()
                status.done({
                    "provider": provider, "label": label,
                    "found": len(results), "created": len(created), "skipped": skipped,
                })
            except Exception as exc:  # noqa: BLE001
                db.session.rollback()
                log.exception("provider pull failed (provider=%s)", provider)
                db_run.finished_at = datetime.now(timezone.utc)
                db_run.status = "error"
                db_run.detail = str(exc)[:1000]
                commit()
                status.fail(exc)

    threading.Thread(target=_run, daemon=True).start()
    return redirect(url_for("task_status.ai_task_status", run_id=run_id, task=task_name))


@settings_bp.route("/settings/smtp", methods=["POST"])
@login_required
@admin_required
def settings_smtp():
    secret = current_app.config["SECRET_KEY"]
    back = _safe_next(url_for("settings.settings"))
    smtp = _singleton(SmtpConfig)
    smtp.enabled = request.form.get("enabled") == "on"
    smtp.host = request.form.get("host", "").strip()
    smtp.port = _int(request.form.get("port"), 587)
    smtp.use_tls = request.form.get("use_tls") == "on"
    smtp.username = request.form.get("username", "").strip()
    pw = request.form.get("password", "")
    if pw.strip():  # keep existing password if blank
        smtp.password_enc = encrypt(secret, pw)
    smtp.from_addr = request.form.get("from_addr", "").strip()
    smtp.to_addr = request.form.get("to_addr", "").strip()
    smtp.admin_email = request.form.get("admin_email", "").strip()
    commit()
    flash("Email settings saved.", "success")
    return redirect(back)


@settings_bp.route("/settings/test-email", methods=["POST"])
@login_required
@admin_required
def settings_test_email():
    secret = current_app.config["SECRET_KEY"]
    back = _safe_next(url_for("settings.settings"))
    smtp_row = db.session.get(SmtpConfig, 1)
    if not smtp_row or not smtp_row.host or not smtp_row.to_addr:
        flash("Save the SMTP host and recipient first, then send a test.", "warning")
        return redirect(back)
    smtp = {
        "host": smtp_row.host,
        "port": smtp_row.port,
        "use_tls": smtp_row.use_tls,
        "username": smtp_row.username,
        "password": decrypt(secret, smtp_row.password_enc),
        "from_addr": smtp_row.from_addr,
        "to_addr": smtp_row.to_addr,
    }
    if not smtp["password"] and smtp_row.password_enc:
        log.warning("SMTP password could not be decrypted — SECRET_KEY may have changed; re-enter credentials in Settings.")
        flash("SMTP password could not be decrypted — SECRET_KEY may have changed; re-enter credentials in Settings.", "danger")
        return redirect(back)
    try:
        send_email(
            smtp,
            "JobSquire test email",
            "This is a test from your JobSquire. If you received this, email "
            "notifications are configured correctly.",
            "<p>This is a test from your <strong>JobSquire</strong>. If you received "
            "this, email notifications are configured correctly.</p>",
        )
        flash(f"Test email sent to {smtp_row.to_addr}. Check the inbox (and spam the first time).",
              "success")
    except Exception as e:  # noqa: BLE001
        flash(f"Test failed: {e.__class__.__name__}: {e}", "danger")
    return redirect(back)


@settings_bp.route("/settings/run", methods=["POST"])
@login_required
@admin_required
def settings_run():
    _app = current_app._get_current_object()

    def _bg_search():
        with _app.app_context():
            run_search(trigger="manual")
    t = threading.Thread(target=_bg_search, daemon=True)
    t.start()
    flash("Search started — check Run History in a moment for results.", "info")
    return redirect(_safe_next(url_for("settings.settings")))


# --------------------------------------------------------------------------
# Candidate asset library (master documents: resume, rec letters, certs, etc.)
# --------------------------------------------------------------------------
def _handle_resume_kind_upload(f, ext, original, label, notes, uploaded_by):
    """Convert an uploaded file straight into a Custom Resume (kind="Resume")
    markdown asset -- always returns the settings redirect, since unlike
    every other kind, this upload MUST convert successfully or be rejected
    outright rather than silently storing something broken. The originally
    uploaded file is kept in source_* on the same row (there's no separate
    archival copy the way "Base Resume" gets one)."""
    from .onboarding import save_resume_draft
    from .resume_convert import ResumeConversionError, SUPPORTED_EXTENSIONS, convert_to_markdown

    if ext not in SUPPORTED_EXTENSIONS:
        flash(f"Custom Resume needs a file Job Squire can convert to markdown "
              f"({', '.join(SUPPORTED_EXTENSIONS)}) — .{ext or 'this'} isn't "
              "supported. Use Base Resume instead to keep the original file "
              "as-is, or paste the text into the resume interview's markdown box.",
              "danger")
        return redirect(_safe_next(url_for("settings.settings", _anchor="tab-documents")))

    raw = f.read()
    try:
        markdown = convert_to_markdown(raw, ext)
    except ResumeConversionError as exc:
        flash(f"Couldn't convert this file: {exc}", "danger")
        return redirect(_safe_next(url_for("settings.settings", _anchor="tab-documents")))
    except Exception:
        log.exception("resume auto-convert failed for a Custom Resume upload")
        flash("Automatic markdown conversion hit an unexpected error. Use Base "
              "Resume instead, or paste the text into the resume interview's "
              "markdown box.", "danger")
        return redirect(_safe_next(url_for("settings.settings", _anchor="tab-documents")))

    source_stored = f"{uuid.uuid4().hex}{('.' + ext) if ext else ''}"
    source_dest = os.path.join(current_app.config["UPLOAD_DIR"], source_stored)
    with open(source_dest, "wb") as fh:
        fh.write(raw)

    result = save_resume_draft(
        markdown, created_by=uploaded_by,
        label=label or f'Converted from "{original}"')
    if result.get("ok"):
        asset = db.session.get(CandidateAsset, result["asset_id"])
        asset.source_stored_name = source_stored
        asset.source_original_name = original
        asset.source_content_type = f.mimetype or ""
        if notes:
            asset.notes = notes
        commit()
        flash("Converted it to markdown and saved as a new Custom Resume — "
              "review it below and edit if anything needs cleanup.", "success")
    else:
        try:
            os.remove(source_dest)
        except OSError:
            pass
        flash(f"Couldn't save the converted resume: {result.get('error')}", "danger")
    return redirect(_safe_next(url_for("settings.settings", _anchor="tab-documents")))


def _attempt_base_resume_autoconvert(dest, ext, original, asset, uploaded_by):
    """After a "Base Resume" upload is already saved, best-effort convert it
    to markdown too and save that as a new kind="Resume" variant -- the
    same outcome the Getting Started resume interview produces, but without
    AI. Only flashes a warning on failure; the Base Resume upload itself has
    already succeeded and is not rolled back. See app/resume_convert.py and
    app/onboarding.py:save_resume_draft."""
    from .onboarding import save_resume_draft
    from .resume_convert import ResumeConversionError, SUPPORTED_EXTENSIONS, convert_to_markdown

    if ext not in SUPPORTED_EXTENSIONS:
        flash(f"Uploaded. Automatic markdown conversion isn't available for "
              f".{ext or 'this'} files yet — use the resume interview below, or "
              "paste the text into the markdown box yourself.", "warning")
        return

    try:
        with open(dest, "rb") as fh:
            raw = fh.read()
        markdown = convert_to_markdown(raw, ext)
        result = save_resume_draft(
            markdown, created_by=uploaded_by,
            label=f'Converted from "{original}"')
        if result.get("ok"):
            flash("Converted it to markdown and saved as a new Custom Resume — "
                  "review it below and edit if anything needs cleanup.", "success")
        else:
            flash(f"Uploaded, but couldn't auto-convert it: {result.get('error')}",
                  "warning")
    except ResumeConversionError as exc:
        flash(f"Uploaded, but couldn't auto-convert it: {exc}", "warning")
    except Exception:
        log.exception("resume auto-convert failed for asset %s", asset.id)
        flash("Uploaded, but the automatic markdown conversion hit an "
              "unexpected error. Use the resume interview below, or paste "
              "the text into the markdown box yourself.", "warning")


@settings_bp.route("/settings/assets/upload", methods=["POST"])
@login_required
@admin_required
def settings_asset_upload():
    form = CandidateAssetForm()
    if form.validate_on_submit():
        f = form.file.data
        original = secure_filename(f.filename) or "file"
        ext = original.rsplit(".", 1)[-1].lower() if "." in original else ""
        kind = form.kind.data
        label = (form.label.data or "").strip()
        notes = form.notes.data or ""
        uploaded_by = current_user.display_name or current_user.username

        # "Custom Resume" (kind="Resume") is the markdown-draft slot read
        # back into the Getting Started paste-back box and shown to Claude
        # as "the" resume (see app/onboarding.py:_read_resume_asset_markdown).
        if kind == "Resume":
            return _handle_resume_kind_upload(f, ext, original, label, notes, uploaded_by)

        stored = f"{uuid.uuid4().hex}{('.' + ext) if ext else ''}"
        dest = os.path.join(current_app.config["UPLOAD_DIR"], stored)
        f.save(dest)
        asset = CandidateAsset(
            kind=kind,
            label=label,
            notes=notes,
            original_name=original,
            stored_name=stored,
            content_type=f.mimetype or "",
            size=os.path.getsize(dest),
            uploaded_by=uploaded_by,
        )
        db.session.add(asset)
        commit()
        flash(f"Uploaded \"{asset.display_name}\".", "success")

        # A "Base Resume" upload is the user's actual resume, uploaded as a
        # document rather than produced through the Getting Started resume
        # interview -- also try to convert it to markdown so a plain upload
        # satisfies the Getting Started "Resume & documents" step the same
        # way the interview does. The original stays on file as its own
        # "Base Resume" asset regardless of whether conversion succeeds.
        if kind == "Base Resume":
            _attempt_base_resume_autoconvert(dest, ext, original, asset, uploaded_by)
    else:
        msg = "Upload failed."
        for errs in form.errors.values():
            msg = errs[0]
            break
        flash(msg, "danger")
    return redirect(_safe_next(url_for("settings.settings", _anchor="tab-documents")))


@settings_bp.route("/assets/<int:asset_id>/download")
@login_required
@admin_required
def asset_download(asset_id):
    asset = db.get_or_404(CandidateAsset, asset_id)
    path = os.path.join(current_app.config["UPLOAD_DIR"], asset.stored_name)
    if not os.path.exists(path):
        abort(404)
    return send_file(path, as_attachment=True, download_name=asset.original_name)


@settings_bp.route("/assets/<int:asset_id>/download-source")
@login_required
@admin_required
def asset_download_source(asset_id):
    """The originally uploaded docx/pdf/txt behind a converted Custom Resume
    variant (see CandidateAsset.source_stored_name) -- 404s for variants that
    came from the interview or a manual paste, since there's no original
    file in that case."""
    asset = db.get_or_404(CandidateAsset, asset_id)
    if not asset.source_stored_name:
        abort(404)
    path = os.path.join(current_app.config["UPLOAD_DIR"], asset.source_stored_name)
    if not os.path.exists(path):
        abort(404)
    return send_file(path, as_attachment=True,
                      download_name=asset.source_original_name or asset.original_name)


@settings_bp.route("/assets/<int:asset_id>/set-base", methods=["POST"])
@login_required
@admin_required
def asset_set_base(asset_id):
    """Promote a kind="Resume" variant to is_base=True, demoting whichever
    one currently holds it. The base variant is the one shown in the Getting
    Started paste-back box and used for tailoring -- see
    app/onboarding.py:save_resume_draft."""
    form = ConfirmForm()
    if not form.validate_on_submit():
        abort(400)
    asset = db.get_or_404(CandidateAsset, asset_id)
    if asset.kind != "Resume":
        flash("Only Custom Resume variants can be marked base.", "danger")
        return redirect(url_for("settings.settings", _anchor="tab-documents"))
    (CandidateAsset.query.filter_by(kind="Resume", is_base=True)
     .update({"is_base": False}))
    asset.is_base = True
    commit()
    flash(f"\"{asset.display_name}\" is now your base resume.", "success")
    return redirect(_safe_next(url_for("settings.settings", _anchor="tab-documents")))


@settings_bp.route("/assets/<int:asset_id>/edit", methods=["POST"])
@login_required
@admin_required
def asset_edit(asset_id):
    asset = db.get_or_404(CandidateAsset, asset_id)
    form = CandidateAssetEditForm()
    if form.validate_on_submit():
        asset.kind = form.kind.data
        asset.label = (form.label.data or "").strip()
        asset.notes = form.notes.data or ""
        commit()
        flash("Document updated.", "success")
    else:
        flash("Could not save changes.", "danger")
    return redirect(url_for("settings.settings", _anchor="tab-documents"))


@settings_bp.route("/assets/<int:asset_id>/delete", methods=["POST"])
@login_required
@admin_required
def asset_delete(asset_id):
    form = ConfirmForm()
    if not form.validate_on_submit():
        abort(400)
    asset = db.get_or_404(CandidateAsset, asset_id)
    upload_dir = current_app.config["UPLOAD_DIR"]
    for name in (asset.stored_name, asset.source_stored_name):
        if not name:
            continue
        path = os.path.join(upload_dir, name)
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            current_app.logger.warning("Could not delete asset file %s", path)

    was_base = asset.kind == "Resume" and asset.is_base
    db.session.delete(asset)
    commit()

    # Deleting the base variant shouldn't silently leave the Getting Started
    # profile step (and kit-building) without any base resume -- promote the
    # newest remaining variant if one exists.
    if was_base:
        remaining = (CandidateAsset.query.filter_by(kind="Resume")
                     .order_by(CandidateAsset.uploaded_at.desc()).first())
        if remaining:
            remaining.is_base = True
            commit()

    flash("Document removed.", "success")
    return redirect(url_for("settings.settings", _anchor="tab-documents"))


@settings_bp.route("/settings/profile", methods=["POST"])
@login_required
@admin_required
def settings_profile():
    """Save the candidate profile markdown to disk."""
    text = request.form.get("candidate_profile", "").rstrip()
    _save_profile(text)
    flash("Candidate profile saved.", "success")
    return redirect(url_for("settings.settings", _anchor="tab-documents"))


@settings_bp.route("/settings/profile-prompt", methods=["POST"])
@login_required
@admin_required
def settings_profile_prompt():
    """Save the profile generation prompt to disk."""
    text = request.form.get("profile_prompt", "").rstrip()
    _save_profile_prompt(text)
    flash("Profile generation prompt saved.", "success")
    return redirect(url_for("settings.settings", _anchor="tab-documents"))


@settings_bp.route("/settings/profile/upload", methods=["POST"])
@login_required
@admin_required
def settings_profile_upload():
    """Replace the candidate profile by uploading a .md file."""
    file = request.files.get("profile_file")
    if not file or not file.filename:
        flash("No file selected.", "danger")
        return redirect(url_for("settings.settings", _anchor="tab-documents"))
    if not file.filename.lower().endswith(".md"):
        flash("Only .md files are accepted for the candidate profile.", "danger")
        return redirect(url_for("settings.settings", _anchor="tab-documents"))
    text = file.read().decode("utf-8", errors="replace").rstrip()
    _save_profile(text)
    flash(f"Candidate profile replaced from \"{file.filename}\".", "success")
    return redirect(url_for("settings.settings", _anchor="tab-documents"))


def _int(value, default, allow_none=False):
    if value is None or str(value).strip() == "":
        return None if allow_none else default
    try:
        return int(str(value).strip())
    except ValueError:
        return None if allow_none else default
