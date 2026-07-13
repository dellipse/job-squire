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
"""Application factory for the Job Squire."""
import logging
import os
from datetime import timedelta, timezone as _utc

from flask import Flask, current_app
from sqlalchemy import text

from .deploy import apply_proxy_trust, enforce_startup_guard, resolve_deploy_flags
from .extensions import csrf, db, limiter, login_manager

log = logging.getLogger(__name__)


def _bool_env(name, default=False):
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def create_app():
    app = Flask(__name__)

    deploy_flags = resolve_deploy_flags()
    # Fatal misconfigurations (network mode without HTTPS/TRUST_PROXY) exit
    # the process here, before anything else runs. Non-fatal ones (local
    # mode with a non-loopback PUBLIC_URL) are returned for the in-app
    # banner set up below, once app.config exists.
    deploy_warnings = enforce_startup_guard(deploy_flags)

    # --- Core config -------------------------------------------------------
    secret = os.environ.get("SECRET_KEY")
    if not secret:
        if _bool_env("ALLOW_INSECURE", False):
            log.warning("=" * 70)
            log.warning("ALLOW_INSECURE is ON: using a STATIC, publicly-known dev "
                        "SECRET_KEY. This key also derives the encryption key for "
                        "all stored secrets. NEVER enable ALLOW_INSECURE outside "
                        "local development.")
            log.warning("=" * 70)
            secret = "dev-insecure-secret-change-me"
        else:
            raise RuntimeError(
                "SECRET_KEY is required. Generate one with "
                "`python -c \"import secrets; print(secrets.token_hex(32))\"` "
                "and set it in the environment (or set ALLOW_INSECURE=1 for local dev)."
            )

    data_dir = os.environ.get("DATA_DIR", "/data")
    os.makedirs(data_dir, exist_ok=True)
    upload_dir = os.path.join(data_dir, "uploads")
    os.makedirs(upload_dir, exist_ok=True)

    # CSRF token lifetime (seconds). Default 4 hours; set CSRF_TIME_LIMIT=0 (or
    # empty) to disable expiry entirely for very long-lived forms.
    _csrf_raw = os.environ.get("CSRF_TIME_LIMIT", "14400").strip()
    csrf_time_limit = None if _csrf_raw in ("", "0", "none", "None") else int(_csrf_raw)

    app.config.update(
        SECRET_KEY=secret,
        SQLALCHEMY_DATABASE_URI=os.environ.get(
            "DATABASE_URL", f"sqlite:///{os.path.join(data_dir, 'job-squire.db')}"
        ),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SQLALCHEMY_ENGINE_OPTIONS={"connect_args": {"timeout": 30}},
        DATA_DIR=data_dir,
        UPLOAD_DIR=upload_dir,
        MAX_CONTENT_LENGTH=int(os.environ.get("MAX_UPLOAD_MB", "10")) * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        # Each instance must have a unique cookie name so that multiple
        # instances on the same domain (or same-site subdomains) cannot
        # clobber each other's sessions and CSRF tokens.  Derive a safe
        # default from INSTANCE_NAME (already required per-instance in the
        # multi-instance compose setup) so that no explicit configuration is
        # needed.  Override with SESSION_COOKIE_NAME for full control.
        SESSION_COOKIE_NAME=os.environ.get(
            "SESSION_COOKIE_NAME",
            "{}_session".format(
                os.environ.get("INSTANCE_NAME", "jt").strip()
                .lower().replace("-", "_").replace(" ", "_") or "jt"
            ),
        ),
        # Behind a TLS-terminating reverse proxy, keep secure cookies on.
        # Set SESSION_COOKIE_SECURE=false only for plain-HTTP local dev.
        # IMPORTANT: if this is true but the browser receives the login page
        # over plain HTTP (e.g. TLS not yet configured in the proxy), the
        # browser will silently drop the Secure cookie, leaving the session
        # empty and causing "CSRF session token is missing" on form submit.
        # Defaults to DEPLOY_MODE's preset (see app/deploy.py) when unset;
        # an explicit SESSION_COOKIE_SECURE always overrides the preset.
        SESSION_COOKIE_SECURE=deploy_flags["secure_cookie"],
        DEPLOY_MODE=deploy_flags["mode"],
        TRUST_PROXY=deploy_flags["trust_proxy"],
        # Startup safety guard warnings (see app/deploy.py) -- rendered as a
        # persistent banner by _inject_deploy_warnings() below. Fixed set at
        # boot time since it's derived from env vars, which don't change
        # without a restart; it "clears itself" in the sense that the next
        # boot after the operator fixes the underlying var won't repopulate it.
        DEPLOY_WARNINGS=deploy_warnings,
        PERMANENT_SESSION_LIFETIME=timedelta(days=int(os.environ.get("SESSION_DAYS", "7"))),
        WTF_CSRF_TIME_LIMIT=csrf_time_limit,
    )

    log.info("Session cookie name: %s", app.config["SESSION_COOKIE_NAME"])
    log.info(
        "Deploy mode: %s (trust_proxy=%s, secure_cookie=%s)",
        deploy_flags["mode"], deploy_flags["trust_proxy"], deploy_flags["secure_cookie"],
    )

    # Trust one hop of X-Forwarded-* headers from the reverse proxy -- but
    # only when the resolved deploy flags say a proxy is actually there.
    # See app/deploy.py: applying this unconditionally on an untrusted
    # network would let forwarded headers be spoofed.
    apply_proxy_trust(app, deploy_flags["trust_proxy"])

    # --- Extensions --------------------------------------------------------
    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)
    limiter.init_app(app)

    from .models import User

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    # --- Blueprints --------------------------------------------------------
    from .auth import auth_bp
    from .main import main_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)

    # --- Template filter: convert naive UTC datetime to local time ---------
    # Timezone resolution order: SCHEDULE_TZ env var → search location in DB
    # (US only — timezones.py has no non-US timezone table) → DEFAULT_TZ (UTC).
    # Mirrors the scheduler's logic. Non-US operators should set SCHEDULE_TZ
    # explicitly; otherwise times display in UTC.
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    def _display_tz():
        """Return the IANA timezone name to use for display."""
        override = os.environ.get("SCHEDULE_TZ", "").strip()
        if override:
            try:
                ZoneInfo(override)
                return override
            except (ZoneInfoNotFoundError, KeyError):
                log.warning("SCHEDULE_TZ=%r is not a valid IANA zone; ignoring.", override)
        try:
            from .models import SearchConfig
            cfg = db.session.get(SearchConfig, 1)
            if cfg and cfg.location:
                from .timezones import timezone_for_location
                return timezone_for_location(cfg.location)
        except Exception:  # noqa: BLE001 - DB may not be ready
            pass
        from .timezones import DEFAULT_TZ
        return DEFAULT_TZ

    def _to_local(dt):
        """Convert a naive UTC datetime (or Unix timestamp int/float) to the configured local timezone."""
        import flask
        from datetime import datetime as _dt
        if not hasattr(flask.g, "_cached_tz"):
            flask.g._cached_tz = ZoneInfo(_display_tz())
        if isinstance(dt, (int, float)):
            dt = _dt.fromtimestamp(dt, tz=_utc.utc)
            return dt.astimezone(flask.g._cached_tz)
        return dt.replace(tzinfo=_utc.utc).astimezone(flask.g._cached_tz)

    @app.template_filter("local_dt")
    def local_dt_filter(dt):
        """Format a naive UTC datetime as local date+time (e.g. 6/9/2026 2:34 PM)."""
        if dt is None:
            return "—"
        local = _to_local(dt)
        try:
            return local.strftime("%-m/%-d/%Y %-I:%M %p")
        except ValueError:
            return local.strftime("%m/%d/%Y %I:%M %p")

    @app.template_filter("local_date")
    def local_date_filter(dt):
        """Format a naive UTC datetime as a local date only (e.g. 6/9/2026)."""
        if dt is None:
            return "—"
        local = _to_local(dt)
        try:
            return local.strftime("%-m/%-d/%Y")
        except ValueError:
            return local.strftime("%m/%d/%Y")

    # --- Startup safety guard banner ----------------------------------------
    # Extends the existing warning-banner pattern (worker heartbeat staleness
    # on Dashboard/Settings) to a site-wide, persistent banner rather than a
    # new mechanism -- injected into every authenticated page's template
    # context so a misconfiguration is visible no matter where the operator
    # lands, not just on one settings tab.
    @app.context_processor
    def _inject_deploy_warnings():
        return {"deploy_warnings": app.config.get("DEPLOY_WARNINGS") or []}

    # --- Security headers --------------------------------------------------
    @app.after_request
    def set_security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self'; "
            "form-action 'self'; base-uri 'self'; frame-ancestors 'none'",
        )
        return resp

    # --- DB init + seed (guarded against concurrent workers) --------------
    with app.app_context():
        _init_database(app, data_dir)

    # --- Seed data-dir files (profile MD lives outside the container) ------
    _seed_data_files(data_dir)

    return app


def _seed_data_files(data_dir: str) -> None:
    """Copy bundled text files to the data dir on first start.

    candidate_profile.md is the source of truth for application kits.  It lives
    in the data dir (outside the container) so it can be edited without a rebuild.
    On first boot, copy the bundled version from the app package as a starting point.
    """
    import shutil
    app_dir = os.path.dirname(__file__)
    files_to_seed = ["candidate_profile.md"]
    for fname in files_to_seed:
        target = os.path.join(data_dir, fname)
        if not os.path.exists(target):
            bundled = os.path.join(app_dir, fname)
            if os.path.exists(bundled):
                shutil.copy2(bundled, target)
                log.info("Seeded %s to data dir", fname)


def _init_database(app, data_dir):
    """Create tables and seed data under a cross-process lock.

    Multiple gunicorn workers (and the scheduler container) share one SQLite
    file. Without serialization they race on CREATE TABLE / first insert. An
    exclusive flock on a file in the shared data dir lets exactly one process
    initialize while the others wait, then find everything already present.
    """
    import fcntl

    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    lock_path = os.path.join(data_dir, ".init.lock")
    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            if app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite"):
                db.session.execute(text("PRAGMA journal_mode=WAL"))
                db.session.commit()
            try:
                db.create_all()
            except OperationalError as e:
                # Belt-and-suspenders if two processes still collide.
                if "already exists" not in str(e).lower():
                    raise
                db.session.rollback()
            _run_migrations()
            _seed_users(app)
            _seed_search_defaults()
            _seed_task_configs()
            _seed_anthropic_provider(app)
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def _run_migrations():
    """Apply additive schema changes that create_all() won't handle on existing DBs."""
    migrations = [
        "ALTER TABLE smtp_config ADD COLUMN admin_email VARCHAR(160) DEFAULT ''",
        "ALTER TABLE ai_config ADD COLUMN connector_name VARCHAR(120) DEFAULT 'job-squire'",
        "ALTER TABLE ai_config ADD COLUMN thinking_mode VARCHAR(20) DEFAULT 'disabled'",
        "ALTER TABLE jobs ADD COLUMN kit_output TEXT DEFAULT ''",
        "ALTER TABLE jobs ADD COLUMN kit_generated_at DATETIME",
        # Phase 2: Pro routine support
        "ALTER TABLE jobs ADD COLUMN ai_fit_score INTEGER",
        "ALTER TABLE jobs ADD COLUMN ai_fit_reason TEXT DEFAULT ''",
        "ALTER TABLE jobs ADD COLUMN followup_draft TEXT DEFAULT ''",
        "ALTER TABLE interviews ADD COLUMN prep_notes TEXT DEFAULT ''",
        # Feature 1: Scheduled Triage
        "ALTER TABLE ai_config ADD COLUMN auto_triage_enabled BOOLEAN DEFAULT 0",
        "ALTER TABLE ai_config ADD COLUMN triage_model VARCHAR(80) DEFAULT 'claude-haiku-4-5'",
        "ALTER TABLE search_runs ADD COLUMN last_triage_at DATETIME",
        # Feature 2: Auto Follow-Up Drafts
        "ALTER TABLE ai_config ADD COLUMN auto_followup_enabled BOOLEAN DEFAULT 0",
        # Feature 3: Weekly Strategy Review
        "ALTER TABLE ai_config ADD COLUMN auto_weekly_review_enabled BOOLEAN DEFAULT 0",
        # Feature 4: ATS Keyword Gap Analysis
        "ALTER TABLE jobs ADD COLUMN kit_ats_gap TEXT DEFAULT ''",
        # Feature 5: Rejection Pattern Alert
        "ALTER TABLE ai_config ADD COLUMN rejection_alert_threshold INTEGER DEFAULT 5",
        "ALTER TABLE ai_config ADD COLUMN last_rejection_analysis_at DATETIME",
        # Static API key for non-Claude MCP clients
        "ALTER TABLE ai_config ADD COLUMN mcp_api_key_enc TEXT DEFAULT ''",
        # Multi-provider: fall back to Anthropic after all ranked providers fail
        "ALTER TABLE ai_config ADD COLUMN fallback_to_anthropic BOOLEAN DEFAULT 1",
        # Hybrid mode: independent toggles replacing the single 'mode' column
        "ALTER TABLE ai_config ADD COLUMN api_enabled BOOLEAN DEFAULT 0",
        "ALTER TABLE ai_config ADD COLUMN mcp_enabled BOOLEAN DEFAULT 0",
        # Provider capability flags
        "ALTER TABLE ai_provider_configs ADD COLUMN use_for_triage BOOLEAN DEFAULT 1",
        "ALTER TABLE ai_provider_configs ADD COLUMN use_for_analysis BOOLEAN DEFAULT 1",
        # Anthropic as first-class provider: per-provider thinking_mode
        "ALTER TABLE ai_provider_configs ADD COLUMN thinking_mode VARCHAR(20)",
        # Claude.ai integration: separate toggle for "Open in Claude" buttons
        "ALTER TABLE ai_config ADD COLUMN claude_buttons_enabled BOOLEAN DEFAULT 0",
        # User saved default view for the jobs list
        "ALTER TABLE users ADD COLUMN jobs_default_sort VARCHAR(200)",
        "ALTER TABLE users ADD COLUMN jobs_default_status VARCHAR(40)",
        "ALTER TABLE users ADD COLUMN jobs_default_per_page INTEGER",
        # Backfill use_ranked_chain_fallback for task config rows seeded before column existed
        "UPDATE ai_task_configs SET use_ranked_chain_fallback = 1 WHERE use_ranked_chain_fallback IS NULL",
        # International location support: country code driving location validation
        # strictness and provider country params (Adzuna, Google Jobs). Existing
        # installs keep the original US behavior by defaulting to 'US'.
        "ALTER TABLE search_config ADD COLUMN country VARCHAR(2) DEFAULT 'US'",
        "UPDATE search_config SET country = 'US' WHERE country IS NULL",
        # Static MCP token lifecycle metadata (see app/mcp_auth.py).
        "ALTER TABLE ai_config ADD COLUMN mcp_api_key_created_at DATETIME",
        "ALTER TABLE ai_config ADD COLUMN mcp_api_key_last_used_at DATETIME",
        "ALTER TABLE ai_config ADD COLUMN mcp_api_key_expires_at DATETIME",
        "ALTER TABLE ai_config ADD COLUMN mcp_api_key_allow_network BOOLEAN DEFAULT 0",
        # AI privacy: PII/SPI redaction toggles (docs/PLAN-ai-privacy.md).
        # redaction_enabled defaults ON — existing installs gain redaction on upgrade.
        "ALTER TABLE ai_config ADD COLUMN redaction_enabled BOOLEAN DEFAULT 1",
        "ALTER TABLE ai_config ADD COLUMN redact_strict BOOLEAN DEFAULT 0",
        "ALTER TABLE ai_config ADD COLUMN redact_local BOOLEAN DEFAULT 0",
    ]
    for stmt in migrations:
        try:
            db.session.execute(text(stmt))
            db.session.commit()
            log.info("migration applied: %s", stmt[:60])
        except Exception as e:  # noqa: BLE001
            db.session.rollback()
            msg = str(e).lower()
            if "duplicate column" in msg or "already exists" in msg:
                pass  # idempotent — column is already there
            else:
                log.warning("migration skipped (%s): %s", type(e).__name__, e)

    # One-time data migration: populate api_enabled/mcp_enabled from legacy mode column.
    # Guard: only fires when the new boolean cols are still at their default (0), meaning
    # this particular migration has not run yet on this database.
    try:
        db.session.execute(text(
            "UPDATE ai_config SET api_enabled=1 WHERE mode='api' AND api_enabled=0"
        ))
        # claude_buttons_enabled: only bootstrap for old MCP deployments that have NOT
        # yet had mcp_enabled set (mcp_enabled=0 is the sentinel that this is the first
        # boot after the migration added this column).  Once mcp_enabled=1, the user owns
        # claude_buttons_enabled independently — never reset it on restart.
        # IMPORTANT: this must run BEFORE the mcp_enabled flip below, which would
        # otherwise clear the mcp_enabled=0 sentinel in the same transaction and
        # leave this bootstrap permanently dead.
        db.session.execute(text(
            "UPDATE ai_config SET claude_buttons_enabled=1"
            " WHERE mode='mcp' AND mcp_enabled=0 AND claude_buttons_enabled=0"
        ))
        db.session.execute(text(
            "UPDATE ai_config SET mcp_enabled=1 WHERE mode='mcp' AND mcp_enabled=0"
        ))
        db.session.commit()
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        log.warning("mode migration skipped: %s", e)

    # Integrity check: disable any provider that is marked enabled but is
    # missing one or more required credentials.  Runs on every startup; safe
    # to repeat because it only touches rows that are actually misconfigured.
    try:
        from .providers import PROVIDERS as _PROVIDERS
        from .models import ProviderCredential as _PC
        from .crypto import decrypt as _decrypt
        import json as _json
        _secret = current_app.config["SECRET_KEY"]
        for _pc in _PC.query.filter_by(enabled=True).all():
            _meta = _PROVIDERS.get(_pc.provider)
            if not _meta:
                continue
            _required = [f["name"] for f in _meta["fields"] if f.get("required")]
            if not _required:
                continue  # keyless provider (Indeed, Dice, Jobicy) — always OK
            _creds = {}
            if _pc.secret_blob:
                try:
                    _creds = _json.loads(_decrypt(_secret, _pc.secret_blob)) or {}
                except Exception:  # noqa: BLE001
                    _creds = {}
            if any(not _creds.get(f, "").strip() for f in _required):
                _pc.enabled = False
                log.warning(
                    "startup: disabled provider '%s' — required credentials not set",
                    _pc.provider,
                )
        db.session.commit()
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        log.warning("provider credential check skipped: %s", e)


def _seed_task_configs():
    """Create default AITaskConfig rows for all four automatic features.

    Migrates enabled state from the legacy AIConfig boolean flags so existing
    installs don't lose their auto-feature settings on upgrade.
    """
    from .models import AIConfig, AITaskConfig, AI_TASK_NAMES

    ai_cfg = db.session.get(AIConfig, 1)

    for task_name in AI_TASK_NAMES:
        if AITaskConfig.query.filter_by(task_name=task_name).first():
            continue  # already seeded

        # Migrate legacy enabled flags.
        enabled = True
        if ai_cfg:
            if task_name == "triage":
                enabled = bool(ai_cfg.auto_triage_enabled)
            elif task_name == "followup":
                enabled = bool(ai_cfg.auto_followup_enabled)
            elif task_name == "weekly_review":
                enabled = bool(ai_cfg.auto_weekly_review_enabled)
            # rejection_alert has no legacy toggle — defaults to True

        row = AITaskConfig(
            task_name=task_name,
            provider_id=None,
            backup_provider_id=None,
            use_ranked_chain_fallback=True,
            enabled=enabled,
        )
        db.session.add(row)

    db.session.commit()


def _seed_anthropic_provider(app):
    """If AIConfig has a saved Anthropic key but no Anthropic AIProviderConfig row exists,
    create one automatically so existing installs get a seamless migration to the
    unified provider list.

    Idempotent — does nothing if an Anthropic provider row already exists.
    """
    from .models import AIConfig, AIProviderConfig

    ai_cfg = db.session.get(AIConfig, 1)
    if not ai_cfg or not ai_cfg.api_key_enc:
        return  # nothing to migrate

    if AIProviderConfig.query.filter_by(provider="anthropic").first():
        return  # already migrated

    max_rank = db.session.query(db.func.max(AIProviderConfig.rank)).scalar() or 0

    p = AIProviderConfig(
        rank=max_rank + 1,
        provider="anthropic",
        label="",
        api_key_enc=ai_cfg.api_key_enc,  # reuse the encrypted key as-is
        base_url="",
        model=(ai_cfg.model or "claude-sonnet-4-6"),
        triage_model=(ai_cfg.triage_model or "claude-haiku-4-5"),
        thinking_mode=(ai_cfg.thinking_mode if ai_cfg.thinking_mode not in (None, "disabled") else None),
        use_for_triage=True,
        use_for_analysis=True,
        enabled=True,
    )
    db.session.add(p)
    db.session.commit()
    log.info("Migrated Anthropic API key from AIConfig to AIProviderConfig (rank %d)", p.rank)


def _seed_search_defaults():
    """Create a blank singleton SearchConfig on first start."""
    from .models import SearchConfig

    if db.session.get(SearchConfig, 1):
        return
    cfg = SearchConfig(id=1, titles="", location="",
                       radius_miles=40, max_age_days=14, results_per_query=25, enabled=False)
    db.session.add(cfg)
    db.session.commit()


def _seed_users(app):
    """Create accounts from env vars if missing.

    The admin account is required.  The user account is optional — it is only
    created when USER_PASSWORD is explicitly set (or ALLOW_INSECURE=1 is set
    and a user username is provided).  This lets the app run with a single
    admin login when a separate job-seeker account is not needed.
    """
    from .models import User

    admin_seed = (
        os.environ.get("ADMIN_USERNAME", "admin"),
        os.environ.get("ADMIN_PASSWORD"),
        os.environ.get("ADMIN_NAME", "Admin"),
        "admin",
    )

    # User account is opt-in: only seed it when a password is provided (or
    # ALLOW_INSECURE is on and a username is explicitly configured).
    user_password = os.environ.get("USER_PASSWORD")
    user_username = os.environ.get("USER_USERNAME", "").strip().lower()
    include_user = bool(user_password) or (
        _bool_env("ALLOW_INSECURE", False) and user_username
    )

    seeds = [admin_seed]
    if include_user:
        seeds.append((
            user_username or "user",
            user_password,
            os.environ.get("USER_NAME", "User"),
            "user",
        ))

    reset = _bool_env("RESET_UIDS_AND_PWDS_ON_START", False)
    for username, password, display_name, role in seeds:
        username = (username or "").strip().lower()
        # When resetting, look up by role so username changes take effect.
        existing = (User.query.filter_by(role=role).first() if reset
                    else User.query.filter_by(username=username).first())
        if existing:
            if reset:
                existing.username = username
                existing.display_name = display_name
                if password:
                    existing.set_password(password)
                db.session.commit()
            continue
        if not password:
            if _bool_env("ALLOW_INSECURE", False):
                app.logger.warning(
                    "ALLOW_INSECURE is ON: seeding %r account with the default "
                    "password 'changeme'. Change it immediately; never use this "
                    "outside local development.", role)
                password = "changeme"
            else:
                raise RuntimeError(
                    "ADMIN_PASSWORD is not set. Generate a password and set it "
                    "in the environment before first start."
                )
        u = User(username=username, display_name=display_name, role=role)
        u.set_password(password)
        db.session.add(u)
        app.logger.info("Seeded %s account: %s", role, username)
    db.session.commit()
