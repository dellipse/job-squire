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
"""Database models for the Job Squire."""
import os
from datetime import datetime, timezone

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db

# Application pipeline statuses, in funnel order.
STATUSES = [
    "Saved",
    "Applied",
    "Phone Screen",
    "Interview",
    "Final Interview",
    "Offer",
    "Hired",
    "Rejected",
    "Withdrawn",
    "Ghosted",
    "Pass",
]

# Statuses that count as an active, live opportunity.
ACTIVE_STATUSES = {"Saved", "Applied", "Phone Screen", "Interview", "Final Interview", "Offer"}

WORK_MODES = ["On-site", "Hybrid", "Remote", "Unknown"]

ATTACHMENT_KINDS = ["Resume", "Cover Letter", "Job Description", "Other"]

# Kinds for master candidate assets (not tied to a specific job).
# "Resume" (shown to users as "Custom Resume") is the markdown-draft slot --
# populated by the onboarding resume interview, a manual paste, or by
# auto-converting an uploaded docx/pdf/txt -- and can hold multiple variants
# (see CandidateAsset.is_base). It's distinct from "Base Resume" (a plain
# archival upload, no conversion guarantee) so onboarding can tell them apart.
# See docs/PLAN-onboarding.md Phase 2.
ASSET_KINDS = [
    "Base Resume",
    "Resume",
    "Recommendation Letter",
    "Cover Letter Template",
    "Certification",
    "Portfolio",
    "Other",
]

# Display-only relabeling for ASSET_KINDS -- the stored value stays "Resume"
# (existing DB rows, filter_by(kind="Resume") call sites throughout
# onboarding.py/main.py depend on it) but the dropdown shows a clearer name.
ASSET_KIND_LABELS = {"Resume": "Custom Resume"}


def asset_kind_label(kind: str) -> str:
    return ASSET_KIND_LABELS.get(kind, kind)

# Networking / recruiter log.
CONTACT_TYPES = ["Recruiter", "Staffing Agency", "Hiring Manager", "Networking", "Reference"]

# Lifecycle of a candidate submission made by a recruiter/agency.
SUBMISSION_STATUSES = [
    "Submitted",
    "Screening",
    "Interviewing",
    "Offer",
    "Placed",
    "Rejected",
    "Withdrawn",
    "No Response",
]

# Submission statuses that are still live (drive follow-up nudges and "open" counts).
ACTIVE_SUBMISSION_STATUSES = {"Submitted", "Screening", "Interviewing", "Offer"}


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    display_name = db.Column(db.String(120), nullable=False, default="")
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="user")  # "admin" or "user"
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    # Saved default view for the jobs list page
    jobs_default_sort = db.Column(db.String(200), nullable=True)
    jobs_default_status = db.Column(db.String(40), nullable=True)
    jobs_default_per_page = db.Column(db.Integer, nullable=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_admin(self):
        return self.role == "admin"


class Job(db.Model):
    __tablename__ = "jobs"

    id = db.Column(db.Integer, primary_key=True)
    company = db.Column(db.String(160), nullable=False)
    title = db.Column(db.String(160), nullable=False)
    location = db.Column(db.String(160), default="")
    work_mode = db.Column(db.String(20), default="Unknown")
    source = db.Column(db.String(80), default="")          # Indeed, LinkedIn, referral, etc.
    url = db.Column(db.String(500), default="")
    salary = db.Column(db.String(80), default="")
    status = db.Column(db.String(40), default="Applied", index=True)
    external_id = db.Column(db.String(255), default="", index=True)  # provider's job id, for dedup
    date_applied = db.Column(db.Date, nullable=True)
    follow_up_date = db.Column(db.Date, nullable=True, index=True)
    contact_name = db.Column(db.String(120), default="")
    contact_email = db.Column(db.String(160), default="")
    notes = db.Column(db.Text, default="")
    ai_analysis = db.Column(db.Text, default="")           # populated by Claude round-trip
    ai_analysis_at = db.Column(db.DateTime, nullable=True)
    ai_fit_score = db.Column(db.Integer, nullable=True)    # 1-10 fit score from triage routine
    ai_fit_reason = db.Column(db.Text, default="")         # brief reasoning for the score
    followup_draft = db.Column(db.Text, default="")        # AI-drafted follow-up email text
    kit_output = db.Column(db.Text, default="")            # application kit saved back via MCP
    kit_generated_at = db.Column(db.DateTime, nullable=True)
    kit_ats_gap = db.Column(db.Text, default="")           # Feature 4: ATS keyword gap analysis

    created_by = db.Column(db.String(80), default="")
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    interviews = db.relationship(
        "Interview", backref="job", cascade="all, delete-orphan", order_by="Interview.interview_date"
    )
    attachments = db.relationship(
        "Attachment", backref="job", cascade="all, delete-orphan", order_by="Attachment.uploaded_at"
    )
    notes_log = db.relationship(
        "JobNote", backref="job", cascade="all, delete-orphan",
        order_by="JobNote.created_at.desc()",
    )

    @property
    def is_active(self):
        return self.status in ACTIVE_STATUSES

    @property
    def follow_up_due(self):
        if not self.follow_up_date:
            return False
        from datetime import date
        return self.is_active and self.follow_up_date <= date.today()


class JobNote(db.Model):
    """A timestamped activity-log entry for a job.

    note_type values:
      note           – manually entered by a user
      status_change  – auto-logged when job status changes
      follow_up      – auto-logged when follow-up date is set/changed
      system         – other automated events
    """
    __tablename__ = "job_notes"

    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, db.ForeignKey("jobs.id"), nullable=False, index=True)
    note_type = db.Column(db.String(40), default="note")
    content = db.Column(db.Text, nullable=False)
    created_by = db.Column(db.String(80), default="")
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class Interview(db.Model):
    """A debrief record for one interview round."""
    __tablename__ = "interviews"

    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, db.ForeignKey("jobs.id"), nullable=False)
    interview_date = db.Column(db.Date, nullable=True)
    round_type = db.Column(db.String(80), default="")      # Phone screen, Technical, Panel, Final, etc.
    interview_format = db.Column(db.String(40), default="")  # Phone, Video, On-site
    interviewer = db.Column(db.String(160), default="")
    questions_asked = db.Column(db.Text, default="")       # post-interview questions captured
    self_rating = db.Column(db.Integer, nullable=True)     # 1-5, how it felt
    went_well = db.Column(db.Text, default="")
    to_improve = db.Column(db.Text, default="")
    notes = db.Column(db.Text, default="")
    prep_notes = db.Column(db.Text, default="")            # AI-generated interview prep guide
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class Attachment(db.Model):
    __tablename__ = "attachments"

    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, db.ForeignKey("jobs.id"), nullable=False)
    kind = db.Column(db.String(40), default="Other")
    original_name = db.Column(db.String(255), nullable=False)
    stored_name = db.Column(db.String(255), nullable=False)  # unique name on disk
    content_type = db.Column(db.String(120), default="")
    size = db.Column(db.Integer, default=0)
    uploaded_by = db.Column(db.String(80), default="")
    uploaded_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class AIInsight(db.Model):
    """A global analysis result imported back from Claude."""
    __tablename__ = "ai_insights"

    id = db.Column(db.Integer, primary_key=True)
    summary = db.Column(db.Text, default="")
    recommendations = db.Column(db.Text, default="")  # newline-joined list
    source = db.Column(db.String(80), default="Claude")
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    created_by = db.Column(db.String(80), default="")


class ProviderCredential(db.Model):
    """Per-provider enable flag and encrypted credentials (JSON blob)."""
    __tablename__ = "provider_credentials"

    id = db.Column(db.Integer, primary_key=True)
    provider = db.Column(db.String(40), unique=True, nullable=False, index=True)
    enabled = db.Column(db.Boolean, default=False)
    secret_blob = db.Column(db.Text, default="")   # encrypted JSON of provider fields
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class CandidateAsset(db.Model):
    """Master documents and credentials for the candidate (not tied to a specific job).

    Examples: base resume, recommendation letters, certifications, portfolio samples.
    These are stored once and referenced when building application kits or passed to
    Claude via the MCP connector's get_candidate_assets tool.
    """
    __tablename__ = "candidate_assets"

    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(60), default="Other", index=True)     # ASSET_KINDS
    label = db.Column(db.String(255), default="")                    # human-readable name
    original_name = db.Column(db.String(255), nullable=False)        # original filename
    stored_name = db.Column(db.String(255), nullable=False)          # uuid-based name on disk
    content_type = db.Column(db.String(120), default="")
    size = db.Column(db.Integer, default=0)
    notes = db.Column(db.Text, default="")                           # admin notes / context for Claude
    uploaded_by = db.Column(db.String(80), default="")
    uploaded_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    # kind="Resume" can have multiple rows (variants) -- see app/onboarding.py
    # save_resume_draft and app/main.py settings_asset_upload. is_base marks
    # which one is "the" resume used for tailoring / shown in the Getting
    # Started paste-back box; exactly one kind="Resume" row should have
    # is_base=True at a time (enforced in application code, not the DB).
    is_base = db.Column(db.Boolean, default=False, index=True)
    # When a Resume-kind row was produced by auto-converting an uploaded
    # docx/pdf/txt (see app/resume_convert.py), these hold the originally
    # uploaded file alongside the converted markdown in `stored_name` --
    # null when the row came from the AI interview or a manual paste, since
    # there's no original document in that case.
    source_stored_name = db.Column(db.String(255))
    source_original_name = db.Column(db.String(255))
    source_content_type = db.Column(db.String(120))

    @property
    def display_name(self):
        return self.label or self.original_name

    @property
    def size_kb(self):
        return round(self.size / 1024, 1) if self.size else 0


class SearchConfig(db.Model):
    """Singleton row (id=1) holding what/where to search."""
    __tablename__ = "search_config"

    id = db.Column(db.Integer, primary_key=True)
    titles = db.Column(db.Text, default="")        # one query per line
    location = db.Column(db.String(160), default="")
    # ISO 3166-1 alpha-2 country code. "US" keeps the original strict "City, ST"
    # validation and US-state timezone lookup; any other value only requires a
    # non-empty location string. See app/timezones.py and app/providers.py
    # (ADZUNA_COUNTRIES) for where this actually changes provider behavior.
    country = db.Column(db.String(2), default="US")
    # Include remote-only job boards (Jobicy) and remote listings in searches.
    include_remote = db.Column(db.Boolean, default=True)
    radius_miles = db.Column(db.Integer, default=40)
    min_salary = db.Column(db.Integer, nullable=True)
    max_age_days = db.Column(db.Integer, default=14)
    results_per_query = db.Column(db.Integer, default=25)
    enabled = db.Column(db.Boolean, default=True)
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    @property
    def title_list(self):
        return [t.strip() for t in (self.titles or "").splitlines() if t.strip()]


class OnboardingState(db.Model):
    """Singleton row (id=1) tracking the Getting Started walkthrough.

    Most step completion is *derived* from real data (a resume exists, search
    targets are set, a search has run) so the checklist reflects reality and
    can never drift. This row only stores what can't be derived: the persona
    answer, explicit skips/answers, and whether the dashboard card was
    dismissed. See docs/PLAN-onboarding.md.
    """
    __tablename__ = "onboarding_state"

    id = db.Column(db.Integer, primary_key=True)
    persona = db.Column(db.String(10), default="")   # "" | "self" | "helper"
    steps_json = db.Column(db.Text, default="{}")     # step key -> "skipped" | "answered" | "no_ai"
    visited_json = db.Column(db.Text, default="[]")   # step keys whose page has been loaded at least once
    dismissed = db.Column(db.Boolean, default=False)  # hide the dashboard card
    completed_at = db.Column(db.DateTime, nullable=True)
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    @property
    def steps(self) -> dict:
        import json as _json
        try:
            return _json.loads(self.steps_json or "{}")
        except ValueError:
            return {}

    def set_step(self, key: str, value: str) -> None:
        import json as _json
        data = self.steps
        data[key] = value
        self.steps_json = _json.dumps(data)

    @property
    def visited(self) -> set:
        import json as _json
        try:
            return set(_json.loads(self.visited_json or "[]"))
        except ValueError:
            return set()

    def mark_visited(self, key: str) -> None:
        """Record that the step's own page has been loaded at least once.

        Some steps derive "done" from data that can already exist on a fresh
        install (e.g. a default job board is enabled out of the box), which
        would otherwise mark the step complete before the user ever saw it.
        _step_done() in onboarding.py requires both this flag AND the
        underlying data before calling a step done.
        """
        import json as _json
        seen = self.visited
        if key in seen:
            return
        seen.add(key)
        self.visited_json = _json.dumps(sorted(seen))


class KitConfig(db.Model):
    """Singleton row (id=1) holding application-kit generation settings."""
    __tablename__ = "kit_config"

    id = db.Column(db.Integer, primary_key=True)
    fit_salary_floor = db.Column(db.Integer, default=60000)   # flag if posting tops out below this
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class SmtpConfig(db.Model):
    """Singleton row (id=1) for outbound email notifications."""
    __tablename__ = "smtp_config"

    id = db.Column(db.Integer, primary_key=True)
    enabled = db.Column(db.Boolean, default=False)
    host = db.Column(db.String(160), default="")
    port = db.Column(db.Integer, default=587)
    use_tls = db.Column(db.Boolean, default=True)
    username = db.Column(db.String(160), default="")
    password_enc = db.Column(db.Text, default="")  # encrypted
    from_addr = db.Column(db.String(160), default="")
    to_addr = db.Column(db.String(160), default="")        # job-seeker (User)
    admin_email = db.Column(db.String(160), default="")    # admin error alerts (Admin)
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class AIConfig(db.Model):
    """Singleton row (id=1) controlling how AI analysis is run."""
    __tablename__ = "ai_config"

    id = db.Column(db.Integer, primary_key=True)
    mode = db.Column(db.String(20), default="manual")  # legacy — kept for migration; use api_enabled/mcp_enabled
    # Independent feature toggles — all can be on simultaneously
    api_enabled = db.Column(db.Boolean, default=False)   # Automatic: app calls AI providers on a schedule
    mcp_enabled = db.Column(db.Boolean, default=False)   # MCP Connector: live read/write endpoint
    claude_buttons_enabled = db.Column(db.Boolean, default=False)  # Show "Open in Claude" buttons (Claude Pro only)
    api_key_enc = db.Column(db.Text, default="")         # encrypted Anthropic API key (Anthropic path only)
    model = db.Column(db.String(80), default=lambda: os.environ.get("CLAUDE_DEFAULT_MODEL", "claude-sonnet-4-6"))
    mcp_token_enc = db.Column(db.Text, default="")       # legacy, unused
    mcp_api_key_enc = db.Column(db.Text, default="")     # encrypted static API key for non-Claude MCP tools
    # Lifecycle metadata for the static key above -- see app/mcp_auth.py.
    # created_at/last_used_at are informational (shown in Settings);
    # expires_at is the optional TTL enforced on every auth check;
    # allow_network is the explicit opt-in required to use the key at all
    # on a network-reachable (DEPLOY_MODE=network) instance.
    mcp_api_key_created_at = db.Column(db.DateTime, nullable=True)
    mcp_api_key_last_used_at = db.Column(db.DateTime, nullable=True)
    mcp_api_key_expires_at = db.Column(db.DateTime, nullable=True)
    mcp_api_key_allow_network = db.Column(db.Boolean, default=False)
    connector_name = db.Column(db.String(120), default="job-squire")
    thinking_mode = db.Column(db.String(20), default="disabled")  # disabled | low | medium | high (Anthropic only)
    # Legacy per-feature toggles (superseded by AITaskConfig.enabled; kept for migration)
    auto_triage_enabled = db.Column(db.Boolean, default=False)
    triage_model = db.Column(db.String(80), default="claude-haiku-4-5")
    auto_followup_enabled = db.Column(db.Boolean, default=False)
    auto_weekly_review_enabled = db.Column(db.Boolean, default=False)
    # Feature 5: Rejection Pattern Alert
    rejection_alert_threshold = db.Column(db.Integer, default=5)
    last_rejection_analysis_at = db.Column(db.DateTime, nullable=True)
    # Multi-provider: fall back to Anthropic after all ranked providers fail
    fallback_to_anthropic = db.Column(db.Boolean, default=True)
    # AI privacy — PII/SPI redaction before transmission (docs/PLAN-ai-privacy.md)
    redaction_enabled = db.Column(db.Boolean, default=True)   # tokenize identifiers, strip SPI
    redact_strict = db.Column(db.Boolean, default=False)      # also pseudonymize orgs/locations
    redact_local = db.Column(db.Boolean, default=False)       # apply redaction to local providers too
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


# Canonical display names for each provider type.
PROVIDER_DISPLAY_NAMES = {
    "anthropic":    "Anthropic (Claude)",
    "openrouter":   "OpenRouter",
    "gemini":       "Google Gemini",
    "cerebras":     "Cerebras",
    "github_models": "GitHub Models",
    "nous_portal":  "Nous Portal",
    "ollama":       "Ollama (local)",
    "litellm":      "LiteLLM (local proxy)",
    "mistral":      "Mistral",
    "groq":         "Groq",
    "openai":       "OpenAI",
    "custom":       "Custom",
}

# Task metadata used by the scheduler, AI dispatcher, and UI.
AI_TASK_NAMES = ("triage", "followup", "weekly_review", "rejection_alert")
AI_TRIAGE_TASKS = frozenset({"triage", "followup"})      # use triage model; small prompts
AI_ANALYSIS_TASKS = frozenset({"weekly_review", "rejection_alert"})  # use analysis model; large prompts

AI_TASK_LABELS = {
    "triage":           "Auto-triage",
    "followup":         "Follow-up drafts",
    "weekly_review":    "Weekly review",
    "rejection_alert":  "Rejection alert",
}

AI_TASK_DESCRIPTIONS = {
    "triage": (
        "Scores each new job for fit right after every search run. "
        "Uses the <strong>triage model</strong> — short prompt, one job at a time, fast and cheap. "
        "Providers marked 'triage only' (e.g. Cerebras free tier) are eligible here."
    ),
    "followup": (
        "Drafts a follow-up email for every active job whose follow-up date has passed and "
        "has no draft yet. Runs each morning at 6 AM. "
        "Uses the <strong>triage model</strong> — short prompt per job."
    ),
    "weekly_review": (
        "Generates a full strategy review every Monday at 6 AM and emails it to you. "
        "Uses the <strong>analysis model</strong> — the entire pipeline is sent in one large prompt. "
        "Providers marked 'triage only' are not eligible."
    ),
    "rejection_alert": (
        "Analyzes rejection patterns when the configured threshold is reached within 14 days. "
        "Uses the <strong>analysis model</strong> — full rejection history in one prompt. "
        "Providers marked 'triage only' are not eligible."
    ),
}


class AIProviderConfig(db.Model):
    """One row per configured AI provider, tried in rank order.

    Providers are tried lowest-rank-first (rank 1 = primary). On HTTP 429/503/529
    or a timeout, the dispatcher moves to the next row automatically.
    Anthropic (Claude) is a first-class provider type — add it here like any other.
    """
    __tablename__ = "ai_provider_configs"

    id           = db.Column(db.Integer, primary_key=True)
    rank         = db.Column(db.Integer, default=1, nullable=False)
    # Provider type key — see PROVIDER_DISPLAY_NAMES for valid values.
    provider     = db.Column(db.String(40), nullable=False)
    # Optional human-readable label (e.g. "OpenRouter — free tier")
    label        = db.Column(db.String(80), default="")
    # Encrypted API key. Leave blank for Ollama/LiteLLM if no auth is needed.
    api_key_enc  = db.Column(db.Text, default="")
    # Base URL override. Required for Ollama, LiteLLM, Custom; optional for cloud providers.
    base_url     = db.Column(db.String(255), default="")
    # Model for full analysis runs (weekly review, rejection analysis, manual analysis).
    # These tasks send large prompts — requires a model with adequate context window.
    model        = db.Column(db.String(120), default="")
    # Model for triage and follow-up drafts. Falls back to `model` if blank.
    # Prefer a fast, cheap model here — this runs on every job after each search.
    triage_model = db.Column(db.String(120), default="")
    # Context window this provider is configured for, in tokens. Local providers only
    # (Ollama/LiteLLM/custom) — cloud providers already have generous, fixed windows and
    # leave this blank/None. Ollama's OpenAI-compatible endpoint has no per-request way to
    # set context size (confirmed against docs.ollama.com/api/openai-compatibility — the
    # only supported method is a Modelfile's `PARAMETER num_ctx`, applied when the model
    # was created — see job_squire_cli/ops/ollama_assist.py's setup flow), so this column
    # is metadata describing what the *configured model* was built with, not a value sent
    # per request. call_with_fallback() (app/ai.py) uses it to estimate whether a given
    # prompt will fit before attempting the call, skipping to the next provider in the
    # chain rather than silently sending a request Ollama would truncate without error.
    num_ctx = db.Column(db.Integer, nullable=True, default=None)
    # Capability flags — control which task types this provider appears for.
    # Set use_for_analysis=False for providers with small context windows (e.g. Cerebras free tier).
    use_for_triage   = db.Column(db.Boolean, default=True)
    use_for_analysis = db.Column(db.Boolean, default=True)
    # Thinking mode — Anthropic (Claude) only. Controls extended/adaptive thinking depth.
    # Values: None (disabled) | "low" | "medium" | "high"
    # Ignored for all non-Anthropic providers.
    thinking_mode = db.Column(db.String(20), nullable=True, default=None)
    # Quick toggle without deleting the row.
    enabled      = db.Column(db.Boolean, default=True)
    created_at   = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    @property
    def display_name(self):
        return self.label or PROVIDER_DISPLAY_NAMES.get(self.provider, self.provider.title())

    @property
    def capability_label(self):
        """Short text describing what this provider can be used for."""
        if self.use_for_triage and self.use_for_analysis:
            return "Triage + Analysis"
        if self.use_for_triage:
            return "Triage only"
        if self.use_for_analysis:
            return "Analysis only"
        return "Disabled"


class AITaskConfig(db.Model):
    """Per-task AI provider assignment for the four automatic features.

    Each row pins a specific primary and/or backup provider to one task.
    If neither is set, the task uses the ranked fallback chain.
    Set enabled=False to disable a task without removing its provider assignment.
    """
    __tablename__ = "ai_task_configs"

    id         = db.Column(db.Integer, primary_key=True)
    task_name  = db.Column(db.String(40), unique=True, nullable=False, index=True)
    # triage | followup | weekly_review | rejection_alert

    # Primary provider. NULL = use ranked chain directly.
    provider_id = db.Column(
        db.Integer,
        db.ForeignKey("ai_provider_configs.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Backup provider — tried if primary fails or is unavailable.
    backup_provider_id = db.Column(
        db.Integer,
        db.ForeignKey("ai_provider_configs.id", ondelete="SET NULL"),
        nullable=True,
    )
    # If True, try the ranked chain after primary+backup both fail.
    use_ranked_chain_fallback = db.Column(db.Boolean, default=True)
    # False = this task never runs automatically (manual only).
    enabled    = db.Column(db.Boolean, default=True)
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    provider = db.relationship(
        "AIProviderConfig", foreign_keys=[provider_id], lazy="joined"
    )
    backup_provider = db.relationship(
        "AIProviderConfig", foreign_keys=[backup_provider_id], lazy="joined"
    )

    @property
    def is_triage_task(self):
        return self.task_name in AI_TRIAGE_TASKS

    @property
    def task_label(self):
        return AI_TASK_LABELS.get(self.task_name, self.task_name.replace("_", " ").title())

    @property
    def task_description(self):
        return AI_TASK_DESCRIPTIONS.get(self.task_name, "")


class SearchRun(db.Model):
    """A record of one search execution (manual or scheduled)."""
    __tablename__ = "search_runs"

    id = db.Column(db.Integer, primary_key=True)
    started_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    finished_at = db.Column(db.DateTime, nullable=True)
    trigger = db.Column(db.String(20), default="manual")  # manual | scheduled
    status = db.Column(db.String(20), default="running")  # running | ok | error
    providers = db.Column(db.String(255), default="")
    found = db.Column(db.Integer, default=0)
    created = db.Column(db.Integer, default=0)
    skipped = db.Column(db.Integer, default=0)
    emailed = db.Column(db.Boolean, default=False)
    detail = db.Column(db.Text, default="")
    last_triage_at = db.Column(db.DateTime, nullable=True)  # Feature 1: when auto-triage last ran


class Contact(db.Model):
    """A recruiter, staffing-agency rep, hiring manager, or networking contact."""
    __tablename__ = "contacts"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False)
    title = db.Column(db.String(160), default="")            # their job title
    agency = db.Column(db.String(160), default="", index=True)  # company / staffing agency
    contact_type = db.Column(db.String(40), default="Recruiter", index=True)
    email = db.Column(db.String(160), default="")
    phone = db.Column(db.String(60), default="")
    linkedin_url = db.Column(db.String(500), default="")
    notes = db.Column(db.Text, default="")
    last_contacted = db.Column(db.Date, nullable=True)
    follow_up_date = db.Column(db.Date, nullable=True, index=True)

    created_by = db.Column(db.String(80), default="")
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    submissions = db.relationship(
        "Submission", backref="contact", cascade="all, delete-orphan",
        order_by="Submission.submitted_date.desc()",
    )

    @property
    def follow_up_due(self):
        if not self.follow_up_date:
            return False
        from datetime import date
        return self.follow_up_date <= date.today()

    @property
    def open_submissions(self):
        return [s for s in self.submissions if s.status in ACTIVE_SUBMISSION_STATUSES]


class Submission(db.Model):
    """A record of a recruiter/agency submitting User to a specific role.

    Captures "who submitted me where, and when". Optionally links to a tracked Job;
    always stores the company/role text so a submission stands on its own.
    """
    __tablename__ = "submissions"

    id = db.Column(db.Integer, primary_key=True)
    contact_id = db.Column(db.Integer, db.ForeignKey("contacts.id"), nullable=True, index=True)
    job_id = db.Column(db.Integer, db.ForeignKey("jobs.id"), nullable=True, index=True)
    company = db.Column(db.String(160), default="")      # where User was submitted
    role_title = db.Column(db.String(160), default="")
    submitted_date = db.Column(db.Date, nullable=True)
    status = db.Column(db.String(40), default="Submitted", index=True)
    follow_up_date = db.Column(db.Date, nullable=True, index=True)
    notes = db.Column(db.Text, default="")

    created_by = db.Column(db.String(80), default="")
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    job = db.relationship("Job", backref=db.backref("submissions", passive_deletes=True))

    @property
    def is_active(self):
        return self.status in ACTIVE_SUBMISSION_STATUSES

    @property
    def follow_up_due(self):
        if not self.follow_up_date:
            return False
        from datetime import date
        return self.is_active and self.follow_up_date <= date.today()
