# Copyright (C) 2026 D. Brandmeyer
# Licensed under the GNU Affero General Public License v3 or later.
"""Tests for the additive schema migrations in app/__init__.py.

Migrations here are append-only ``ALTER TABLE ... ADD COLUMN`` statements that
run on every boot against the real user database. The safety properties that
matter:

  1. Running them on an older database adds the missing columns (upgrade path).
  2. Running them again is a no-op and never raises (idempotency).
  3. They never destroy existing rows.
  4. The one-time data backfill migrates legacy ``mode`` into the new
     api_enabled / mcp_enabled boolean flags.

There are no assertions about *how* a column is added, only that the observable
schema and data end up correct — so these tests stay valid if the migration list
is reordered or extended.
"""
import pytest
from sqlalchemy import text

from app import _run_migrations
from app.extensions import db

# A representative slice of columns that the migrations are responsible for
# adding. Not exhaustive, but spread across every table the migrations touch so
# a wrong table name or typo in any block is caught.
MIGRATED_COLUMNS = {
    "smtp_config": ["admin_email"],
    "ai_config": [
        "connector_name", "thinking_mode", "auto_triage_enabled", "triage_model",
        "auto_followup_enabled", "auto_weekly_review_enabled",
        "rejection_alert_threshold", "mcp_api_key_enc", "fallback_to_anthropic",
        "api_enabled", "mcp_enabled", "claude_buttons_enabled",
    ],
    "jobs": [
        "kit_output", "kit_generated_at", "ai_fit_score", "ai_fit_reason",
        "followup_draft", "kit_ats_gap",
    ],
    "interviews": ["prep_notes"],
    "search_runs": ["last_triage_at"],
    "users": ["jobs_default_sort", "jobs_default_status", "jobs_default_per_page"],
    "ai_provider_configs": ["use_for_triage", "use_for_analysis", "thinking_mode"],
}


def _columns(table):
    rows = db.session.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return [r[1] for r in rows]


@pytest.fixture
def mdb(app_context):
    """Give each migration test a clean, full-schema database in its own context.

    ``create_all()`` builds the current model schema; individual tests then
    simulate an *older* database by dropping specific columns before calling
    ``_run_migrations()``. Teardown restores the full schema so later tests are
    unaffected.
    """
    db.session.rollback()
    db.drop_all()
    db.create_all()
    yield db
    db.session.rollback()
    db.drop_all()
    db.create_all()
    _run_migrations()


def test_upgrade_adds_missing_columns(mdb):
    """Dropping migrated columns then running migrations restores every one."""
    # Simulate an old database: strip a spread of columns across tables.
    old_schema_drops = [
        ("jobs", "ai_fit_score"),
        ("jobs", "followup_draft"),
        ("jobs", "kit_ats_gap"),
        ("ai_config", "thinking_mode"),
        ("ai_config", "claude_buttons_enabled"),
        ("ai_config", "mcp_api_key_enc"),
        ("interviews", "prep_notes"),
        ("users", "jobs_default_sort"),
        ("search_runs", "last_triage_at"),
        ("smtp_config", "admin_email"),
        ("ai_provider_configs", "use_for_triage"),
    ]
    for table, col in old_schema_drops:
        db.session.execute(text(f"ALTER TABLE {table} DROP COLUMN {col}"))
    db.session.commit()

    # Confirm the simulated old schema really is missing them.
    for table, col in old_schema_drops:
        assert col not in _columns(table), f"{table}.{col} should be gone pre-migration"

    _run_migrations()

    # Every dropped column is back.
    for table, col in old_schema_drops:
        assert col in _columns(table), f"{table}.{col} not restored by migration"


def test_all_expected_columns_present_after_migration(mdb):
    """After a clean create + migrate, every column the migrations own exists."""
    _run_migrations()
    for table, cols in MIGRATED_COLUMNS.items():
        present = _columns(table)
        for col in cols:
            assert col in present, f"expected {table}.{col} after migration"


def test_migrations_are_idempotent(mdb):
    """Running migrations repeatedly never raises and never duplicates a column."""
    _run_migrations()
    before = {t: _columns(t) for t in MIGRATED_COLUMNS}
    # Re-run twice more; the duplicate-column path must be swallowed cleanly.
    _run_migrations()
    _run_migrations()
    after = {t: _columns(t) for t in MIGRATED_COLUMNS}
    assert before == after, "column layout changed on repeated migration runs"
    # No column name should appear twice in any table.
    for table, cols in after.items():
        assert len(cols) == len(set(cols)), f"duplicate column in {table}: {cols}"


def test_migrations_preserve_existing_rows(mdb):
    """A row written before migration survives the ALTER statements intact."""
    db.session.execute(text(
        "INSERT INTO jobs (company, title, status) "
        "VALUES ('Acme', 'Engineer', 'Saved')"
    ))
    db.session.commit()

    _run_migrations()

    row = db.session.execute(
        text("SELECT company, title, status FROM jobs")
    ).fetchone()
    assert row is not None, "job row was lost during migration"
    assert row[0] == "Acme" and row[1] == "Engineer" and row[2] == "Saved"


def test_legacy_mode_backfill(mdb):
    """Legacy `mode` column is migrated into the api_enabled/mcp_enabled flags."""
    # api mode -> api_enabled should flip to 1
    db.session.execute(text(
        "INSERT INTO ai_config (id, mode, api_enabled, mcp_enabled) "
        "VALUES (1, 'api', 0, 0)"
    ))
    db.session.commit()

    _run_migrations()

    row = db.session.execute(
        text("SELECT api_enabled, mcp_enabled FROM ai_config WHERE id=1")
    ).fetchone()
    assert row[0] == 1, "mode='api' should backfill api_enabled=1"
    assert row[1] == 0, "mcp_enabled should stay 0 for an api-mode config"


def test_legacy_mcp_mode_backfill(mdb):
    """mode='mcp' backfills mcp_enabled=1 AND bootstraps the Claude buttons.

    Regression guard: the claude_buttons_enabled bootstrap is guarded by the
    mcp_enabled=0 first-boot sentinel, so it must run before the mcp_enabled flip
    in _run_migrations(). If those statements are ever reordered so the flip runs
    first, the bootstrap goes dead and this test fails.
    """
    db.session.execute(text(
        "INSERT INTO ai_config (id, mode, api_enabled, mcp_enabled, claude_buttons_enabled) "
        "VALUES (1, 'mcp', 0, 0, 0)"
    ))
    db.session.commit()

    _run_migrations()

    row = db.session.execute(
        text("SELECT mcp_enabled, claude_buttons_enabled FROM ai_config WHERE id=1")
    ).fetchone()
    assert row[0] == 1, "mode='mcp' should backfill mcp_enabled=1"
    assert row[1] == 1, "mode='mcp' first boot should enable the Claude buttons"
